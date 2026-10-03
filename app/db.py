"""SQLite 存储层。

版本、迁移结果、审阅依赖、完成记录与门禁策略修订均只追加（append-only）：
不提供任何 UPDATE/DELETE 路径，人工确认以新行追加到 confirmations 表
（同一 location 至多一条，由唯一索引 idx_confirm_loc_unique 保证），
同名门禁策略的每次提交都以递增 revision 追加到 policy_revisions 表。
审阅包（review_packages）创建时冻结快照并落库，成功回传后仅补记首次
结果（applied_at / result_json 只写一次），同一 package_id 的重复回传
原样返回首次结果，不重复追加确认或完成。发布候选（release_candidates）
同理：创建即冻结不可变快照，确认时仅在六类事件水位未变且门禁仍通过时
补记一次 released 结果，同一 candidate_id 的重复确认原样返回首次结果。
app_meta 保存服务级元数据（目前为版本范围检索游标的 HMAC 签名密钥，
init_db 时随机生成一次，重启不变，不随游标外泄）。
"""
from __future__ import annotations

import os
import secrets
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", os.path.join("data", "app.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents (id),
    number      INTEGER NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (document_id, number)
);

CREATE TABLE IF NOT EXISTS annotations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id        INTEGER NOT NULL REFERENCES documents (id),
    body               TEXT NOT NULL,
    selection          TEXT NOT NULL,
    left_context       TEXT NOT NULL DEFAULT '',
    right_context      TEXT NOT NULL DEFAULT '',
    orig_start         INTEGER NOT NULL,
    orig_end           INTEGER NOT NULL,
    created_version_id INTEGER NOT NULL REFERENCES versions (id),
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 每条批注在每个版本上的迁移结果（只追加）
CREATE TABLE IF NOT EXISTS locations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    annotation_id INTEGER NOT NULL REFERENCES annotations (id),
    version_id    INTEGER NOT NULL REFERENCES versions (id),
    status        TEXT NOT NULL CHECK (status IN ('resolved', 'ambiguous', 'unresolved')),
    start         INTEGER,
    "end"         INTEGER,
    left_match    INTEGER,
    right_match   INTEGER,
    score         INTEGER,
    candidates    TEXT NOT NULL DEFAULT '[]',  -- JSON：全部候选范围及各自匹配依据
    carried_from  INTEGER REFERENCES locations (id),  -- 非空表示歧义未确认而顺延
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (annotation_id, version_id)
);

-- 人工确认（只追加；确认后对应 location 的生效状态变为 confirmed；
-- 同一 location 至多一条确认，由唯一索引 idx_confirm_loc_unique 保证）
CREATE TABLE IF NOT EXISTS confirmations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL REFERENCES locations (id),
    start       INTEGER NOT NULL,
    "end"       INTEGER NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 批注间的审阅依赖边：前置批注 → 后续批注（只追加，限同一文档内，无环）
CREATE TABLE IF NOT EXISTS dependencies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     INTEGER NOT NULL REFERENCES documents (id),
    prerequisite_id INTEGER NOT NULL REFERENCES annotations (id),
    dependent_id    INTEGER NOT NULL REFERENCES annotations (id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (document_id, prerequisite_id, dependent_id),
    CHECK (prerequisite_id <> dependent_id)
);

-- 批注完成记录（只追加；一条批注至多一条，重复提交幂等）
CREATE TABLE IF NOT EXISTS completions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id   INTEGER NOT NULL REFERENCES documents (id),
    annotation_id INTEGER NOT NULL REFERENCES annotations (id),
    note          TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (annotation_id)
);

-- 文档发布门禁策略修订（按名称只追加：同名提交一律生成递增 revision，
-- 旧修订不可改、不可删；strategy_json 为提交时原样保留的策略树）
CREATE TABLE IF NOT EXISTS policy_revisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    strategy_json TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (name, revision)
);

-- 可离线往返的审阅包：创建时在同一个读事务中冻结指定批注（含自动收录的
-- 传递前置项）的最新迁移记录、候选范围、完成状态与全部传递前置状态，
-- package_id 与内容摘要在创建后即固定；payload_json 为冻结内容本身，
-- 离线回传只携带摘要与逐项决定。成功回传后 applied_at / result_json
-- 记录首次结果：同一 package_id 再次回传原样返回，不重复写入。
CREATE TABLE IF NOT EXISTS review_packages (
    id           TEXT PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES documents (id),
    digest       TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    applied_at   TEXT,
    result_json  TEXT
);

-- 文档审阅发布候选：创建时在同一个读事务中固定文档版本、门禁策略修订、
-- 门禁解释树、当前审阅状态与该文档六类事件的水位（frozen_json），
-- candidate_id 与规范摘要创建后不可变；初始门禁未通过则不创建、不落行。
-- 确认时在同一个写事务中重读水位与门禁快照：任一水位变化（候选创建后
-- 该文档出现版本 / 批注 / 迁移 / 确认 / 依赖 / 完成记录）或门禁不再
-- 通过即冲突、不改变候选；仅水位未变且门禁仍通过时追加一次 released
-- 结果（released_at / result_json 只写一次），重复确认原样返回首次结果。
CREATE TABLE IF NOT EXISTS release_candidates (
    id            TEXT PRIMARY KEY,
    document_id   INTEGER NOT NULL REFERENCES documents (id),
    digest        TEXT NOT NULL,
    frozen_json   TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    released_at   TEXT,
    result_json   TEXT
);

CREATE INDEX IF NOT EXISTS idx_versions_doc    ON versions (document_id, number);
CREATE INDEX IF NOT EXISTS idx_locations_ann   ON locations (annotation_id);
CREATE INDEX IF NOT EXISTS idx_locations_ver   ON locations (version_id);
-- 每个 location 至多一条人工确认：唯一索引取代旧版的非唯一索引
-- idx_confirm_loc（DROP + CREATE 均幂等；既有库中的历史重复由
-- init_db 在执行本脚本前预检并报出，见 _assert_no_duplicate_confirmations）
DROP INDEX IF EXISTS idx_confirm_loc;
CREATE UNIQUE INDEX IF NOT EXISTS idx_confirm_loc_unique
    ON confirmations (location_id);
CREATE INDEX IF NOT EXISTS idx_dependencies_doc ON dependencies (document_id);
CREATE INDEX IF NOT EXISTS idx_completions_doc  ON completions (document_id);
CREATE INDEX IF NOT EXISTS idx_policy_name      ON policy_revisions (name, revision);
CREATE INDEX IF NOT EXISTS idx_review_packages_doc ON review_packages (document_id);
CREATE INDEX IF NOT EXISTS idx_release_candidates_doc ON release_candidates (document_id);

-- 服务级元数据（单行单键）：保存版本范围检索游标的 HMAC-SHA256 签名密钥。
-- 密钥在 init_db 时随机生成一次，重启不变（游标跨重启仍可验签）；不随
-- 游标外泄，篡改游标载荷会导致签名不符 → 422。
CREATE TABLE IF NOT EXISTS app_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    # isolation_level=None：自动提交；多语句写入用 transaction() 显式包裹
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


@contextmanager
def transaction(con: sqlite3.Connection):
    """BEGIN IMMEDIATE 事务：写操作串行化，保证版本号等自增逻辑无竞态。"""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except Exception:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


@contextmanager
def read_transaction(con: sqlite3.Connection):
    """BEGIN 只读事务：多次 SELECT 落在同一快照（WAL 下快照隔离）。

    供迁移归档导出使用：文档元数据、版本、批注、迁移记录与人工确认
    必须在同一个读事务中取出，避免导出过程中并发写入造成行间不一致。
    """
    con.execute("BEGIN")
    try:
        yield con
    except Exception:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def _assert_no_duplicate_confirmations(con: sqlite3.Connection) -> None:
    """建立 confirmations.location_id 唯一索引前的预检（既有库平滑迁移）。

    历史版本没有该唯一约束，并发确认可能在同一 location 上留下多条确认。
    检测到这类历史重复时**不静默删改**：抛出带可定位 location_id 与确认
    行 ID 的 RuntimeError，由人工核对清理后再启动；无重复（或全新库，
    表尚不存在）时直接返回，随后由 SCHEMA 建立唯一索引。

    init_db 在服务接受请求之前单线程执行，预检与建索引之间不会夹入并发写入。
    """
    table_exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'confirmations'"
    ).fetchone()
    if table_exists is None:
        return
    duplicates = con.execute(
        """
        SELECT location_id, GROUP_CONCAT(id) AS confirmation_ids
        FROM confirmations
        GROUP BY location_id
        HAVING COUNT(*) > 1
        ORDER BY location_id
        """
    ).fetchall()
    if duplicates:
        detail = "；".join(
            f"location_id={row['location_id']}"
            f"（确认行 ID：{row['confirmation_ids']}）"
            for row in duplicates
        )
        raise RuntimeError(
            "confirmations 表存在历史重复确认（同一迁移记录多条确认），"
            "唯一约束未建立，也未删改任何数据；"
            "请人工核对并清理以下位置后再启动：" + detail
        )


def init_db() -> None:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    con = connect()
    try:
        _assert_no_duplicate_confirmations(con)
        con.executescript(SCHEMA)
        # 检索游标签名密钥：只追加式生成一次（INSERT OR IGNORE + 重读），
        # 既有库与已运行环境升级时自动补齐，生成后永不更换。
        con.execute(
            "INSERT OR IGNORE INTO app_meta (key, value) VALUES (?, ?)",
            ("search_cursor_secret", secrets.token_hex(32)),
        )
    finally:
        con.close()
