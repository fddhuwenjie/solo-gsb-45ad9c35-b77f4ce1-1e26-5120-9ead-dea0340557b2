"""SQLite 存储层：可审计的版本化迁移。

启动流程（``init_db``）在**单个写事务**（BEGIN IMMEDIATE）内完成：

1. 读取 schema 版本（以审计表 ``schema_migrations`` 为准，``PRAGMA
   user_version`` 仅作镜像），识别全新库 / 无审计表的旧库 / 已纳管库；
2. 对旧库先做**非破坏性预检**（confirmations 重复确认、旧索引缺失、
   app_meta 缺行等）：重复确认必须列出 location 与确认行 ID 后拒绝启动，
   其余问题只产生告警并由后续迁移幂等补齐；
3. 按编号顺序执行未完成的迁移：每一步套 SAVEPOINT，成功追加
   ``applied`` 审计行，失败回滚该步（SAVEPOINT）并追加带编号、步骤名与
   错误信息的 ``failed`` 审计行后提交审计、拒绝启动；
4. 全部完成后做一次终态校验（所有编号 applied、表 / 唯一索引 / 游标密钥
   齐备且无重复确认），失败同样拒绝启动。

业务数据（版本、迁移结果、审阅依赖、完成记录、门禁策略修订、确认）一律
只追加（append-only）：不提供任何 UPDATE/DELETE 路径，人工确认以新行追加
到 confirmations 表（同一 location 至多一条，由唯一索引
idx_confirm_loc_unique 保证），同名门禁策略的每次提交都以递增 revision
追加到 policy_revisions 表。审阅包（review_packages）创建时冻结快照并落库，
成功回传后仅补记首次结果（applied_at / result_json 只写一次），同一
package_id 的重复回传原样返回首次结果，不重复追加确认或完成。发布候选
（release_candidates）同理：创建即冻结不可变快照，确认时仅在六类事件水位
未变且门禁仍通过时补记一次 released 结果，同一 candidate_id 的重复确认
原样返回首次结果。app_meta 保存服务级元数据（目前为版本范围检索游标的
HMAC 签名密钥，由迁移 004 随机生成一次，重启不变，不随游标外泄）。
"""
from __future__ import annotations

import logging
import os
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("app.db")

DB_PATH = os.environ.get("DB_PATH", os.path.join("data", "app.db"))

# ---------------------------------------------------------------------------
# 迁移审计表（append-only）
#
# 每个编号允许多行：失败重试时旧的 failed 行原样保留，成功后再追加
# applied 行——审计链不覆盖、不删除。某编号是否已完成以「是否存在
# applied 行」为准；当前 schema 版本 = 最大 applied 编号。
# ---------------------------------------------------------------------------
LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    version    INTEGER NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL CHECK (status IN ('applied', 'failed')),
    error      TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""

# ---------------------------------------------------------------------------
# 业务 schema（按迁移版本拆分；全部语句必须幂等，保证失败重试安全）
# ---------------------------------------------------------------------------

# 001：初始业务库（历史首个部署版本）。此时 confirmations 上只有非唯一
# 索引 idx_confirm_loc；唯一约束在 002 中补齐。
V1_DDL = """
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
-- 同一 location 至多一条确认，由 002 建立的唯一索引保证）
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
-- 记录首次结果：同一 package_id 再次回传原样返回，不写入。
CREATE TABLE IF NOT EXISTS review_packages (
    id           TEXT PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES documents (id),
    digest       TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    applied_at   TEXT,
    result_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_versions_doc    ON versions (document_id, number);
CREATE INDEX IF NOT EXISTS idx_locations_ann   ON locations (annotation_id);
CREATE INDEX IF NOT EXISTS idx_locations_ver   ON locations (version_id);
-- 注：001 时代 confirmations 上的非唯一索引 idx_confirm_loc 不由本迁移
-- 创建（旧库上补跑 001 时会与已落地的 002 冲突）；002 统一 DROP 旧索引、
-- 建唯一索引。全新库在 001→002 之间没有业务流量，无需该临时索引。
CREATE INDEX IF NOT EXISTS idx_dependencies_doc ON dependencies (document_id);
CREATE INDEX IF NOT EXISTS idx_completions_doc  ON completions (document_id);
CREATE INDEX IF NOT EXISTS idx_policy_name      ON policy_revisions (name, revision);
CREATE INDEX IF NOT EXISTS idx_review_packages_doc ON review_packages (document_id)
"""

# 002：confirmations.location_id 唯一约束。先 DROP 旧非唯一索引（幂等），
# 再建唯一索引；旧库中的历史重复确认由预检拦在本步之前，不静默删改。
V2_DDL = """
DROP INDEX IF EXISTS idx_confirm_loc;
CREATE UNIQUE INDEX IF NOT EXISTS idx_confirm_loc_unique
    ON confirmations (location_id)
"""

# 003：文档审阅发布候选表（发布门禁之后引入）。创建即冻结不可变快照，
# 确认时仅补记一次 released 结果。
V3_DDL = """
CREATE TABLE IF NOT EXISTS release_candidates (
    id            TEXT PRIMARY KEY,
    document_id   INTEGER NOT NULL REFERENCES documents (id),
    digest        TEXT NOT NULL,
    frozen_json   TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    released_at   TEXT,
    result_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_release_candidates_doc ON release_candidates (document_id)
"""

# 004：服务级元数据表（单行单键）与版本范围检索游标的 HMAC-SHA256
# 签名密钥。密钥在本迁移内随机生成一次（INSERT OR IGNORE + 终态校验），
# 重启与迁移重试均不更换（游标跨重启仍可验签）；不随游标外泄。
V4_DDL = """
CREATE TABLE IF NOT EXISTS app_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL
)
"""

SEARCH_CURSOR_SECRET_KEY = "search_cursor_secret"


def _v4_ensure_secret(con: sqlite3.Connection) -> None:
    """补齐游标密钥：只追加式生成一次，重复执行不更换既有值。"""
    con.execute(
        "INSERT OR IGNORE INTO app_meta (key, value) VALUES (?, ?)",
        (SEARCH_CURSOR_SECRET_KEY, secrets.token_hex(32)),
    )


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    ddl: str
    # 迁移步内的非 DDL 动作（如随机密钥补齐）；同样必须幂等。
    after: Optional[Callable[[sqlite3.Connection], None]] = None
    # 判定该编号是否已经在「无审计表的旧库」中落地，用于旧库识别与基线补登。
    # 返回 True 时不重复执行 DDL，直接追加 applied 基线审计行。
    evidence: Optional[Callable[[sqlite3.Connection], bool]] = None


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone() is not None


def _index_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
        (name,),
    ).fetchone() is not None


def _v1_evidence(con: sqlite3.Connection) -> bool:
    # 001 的全部业务表 + 非唯一索引（唯一索引属于 002，故不在此检查）
    return all(
        _table_exists(con, t)
        for t in (
            "documents", "versions", "annotations", "locations",
            "confirmations", "dependencies", "completions",
            "policy_revisions", "review_packages",
        )
    ) and _index_exists(con, "idx_versions_doc")


def _v2_evidence(con: sqlite3.Connection) -> bool:
    # PRAGMA index_list 列：seq, name, unique, origin, partial
    for row in con.execute("PRAGMA index_list('confirmations')"):
        if row[1] == "idx_confirm_loc_unique":
            return row[2] == 1
    return False


def _v3_evidence(con: sqlite3.Connection) -> bool:
    return _table_exists(con, "release_candidates") and _index_exists(
        con, "idx_release_candidates_doc"
    )


def _v4_evidence(con: sqlite3.Connection) -> bool:
    if not _table_exists(con, "app_meta"):
        return False
    return con.execute(
        "SELECT 1 FROM app_meta WHERE key = ?",
        (SEARCH_CURSOR_SECRET_KEY,),
    ).fetchone() is not None


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial_schema", V1_DDL, evidence=_v1_evidence),
    Migration(2, "confirmations_location_unique", V2_DDL, evidence=_v2_evidence),
    Migration(3, "release_candidates", V3_DDL, evidence=_v3_evidence),
    Migration(4, "app_meta_cursor_secret", V4_DDL,
              after=_v4_ensure_secret, evidence=_v4_evidence),
)

SCHEMA_VERSION = max(m.version for m in MIGRATIONS)

# 全部迁移 DDL 的拼接视图（等同历史 init_db 的一次性建库脚本，供旧测试与
# 工具使用；新库的正式路径仍是逐编号迁移 + 审计）。
SCHEMA = "\n\n".join(m.ddl.strip().rstrip(";") + ";" for m in MIGRATIONS)


class MigrationError(RuntimeError):
    """迁移预检 / 执行 / 终态校验失败：服务必须拒绝启动、不接受业务请求。"""


@dataclass
class MigrationReport:
    """一次 init_db 的可审计结果（供日志与就绪探针使用）。"""

    fresh: bool = False
    legacy: bool = False
    applied: list[int] = field(default_factory=list)
    stamped: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def version(self) -> int:
        return SCHEMA_VERSION


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


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


def _ledger_record(
    con: sqlite3.Connection,
    version: int,
    name: str,
    status: str,
    error: str = "",
    note: str = "",
) -> None:
    con.execute(
        """
        INSERT INTO schema_migrations (version, name, status, error, note)
        VALUES (?, ?, ?, ?, ?)
        """,
        (version, name, status, error[:2000], note),
    )


def applied_versions(con: sqlite3.Connection) -> set[int]:
    if not _table_exists(con, "schema_migrations"):
        return set()
    return {
        row[0]
        for row in con.execute(
            "SELECT DISTINCT version FROM schema_migrations WHERE status = 'applied'"
        )
    }


def migration_history(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """返回完整迁移审计行（编号、状态、错误、时间），按落库顺序。"""
    if not _table_exists(con, "schema_migrations"):
        return []
    return con.execute(
        """
        SELECT id, version, name, status, error, note, created_at
        FROM schema_migrations
        ORDER BY id
        """
    ).fetchall()


# ---------------------------------------------------------------------------
# 非破坏性预检（只追加数据原样保留，绝不静默删改）
# ---------------------------------------------------------------------------


def _duplicate_confirmations(con: sqlite3.Connection) -> list[sqlite3.Row]:
    if not _table_exists(con, "confirmations"):
        return []
    return con.execute(
        """
        SELECT location_id, GROUP_CONCAT(id) AS confirmation_ids
        FROM confirmations
        GROUP BY location_id
        HAVING COUNT(*) > 1
        ORDER BY location_id
        """
    ).fetchall()


def _assert_no_duplicate_confirmations(con: sqlite3.Connection) -> None:
    """建立 confirmations.location_id 唯一索引前的预检（既有库平滑迁移）。

    历史版本没有该唯一约束，并发确认可能在同一 location 上留下多条确认。
    检测到这类历史重复时**不静默删改**：抛出带可定位 location_id 与确认
    行 ID 的 MigrationError，由人工核对清理后再启动；无重复（或全新库，
    表尚不存在）时直接返回，随后由 002 建立唯一索引。

    迁移流程在服务接受请求之前单线程执行，预检与建索引之间不会夹入并发写入。
    """
    duplicates = _duplicate_confirmations(con)
    if duplicates:
        detail = "；".join(
            f"location_id={row['location_id']}"
            f"（确认行 ID：{row['confirmation_ids']}）"
            for row in duplicates
        )
        raise MigrationError(
            "confirmations 表存在历史重复确认（同一迁移记录多条确认），"
            "唯一约束未建立，也未删改任何数据；"
            "请人工核对并清理以下位置后再启动：" + detail
        )


def _preflight(con: sqlite3.Connection, legacy: bool, report: MigrationReport) -> None:
    """对已有库做非破坏性预检。

    - 重复确认：致命，拒绝启动（列出 location_id 与确认行 ID）；
    - 旧索引缺失、app_meta 缺行 / 缺表、发布候选表缺失：仅告警，
      由对应编号迁移幂等补齐（CREATE INDEX IF NOT EXISTS / INSERT OR IGNORE）。
    """
    _assert_no_duplicate_confirmations(con)

    if not legacy:
        return

    if _table_exists(con, "confirmations") and not _index_exists(
        con, "idx_confirm_loc"
    ) and not _index_exists(con, "idx_confirm_loc_unique"):
        report.warnings.append(
            "预检：confirmations 缺少 location_id 索引，"
            "将由迁移 002 补齐唯一索引"
        )
    for table, index, ver in (
        ("versions", "idx_versions_doc", "001"),
        ("locations", "idx_locations_ann", "001"),
        ("locations", "idx_locations_ver", "001"),
        ("dependencies", "idx_dependencies_doc", "001"),
        ("completions", "idx_completions_doc", "001"),
        ("policy_revisions", "idx_policy_name", "001"),
        ("review_packages", "idx_review_packages_doc", "001"),
    ):
        if _table_exists(con, table) and not _index_exists(con, index):
            report.warnings.append(
                f"预检：旧索引 {index} 缺失，将由迁移 {ver} 非破坏补齐"
            )
    if not _table_exists(con, "release_candidates"):
        report.warnings.append(
            "预检：release_candidates 表缺失，将由迁移 003 补齐（不改动既有数据）"
        )
    if not _table_exists(con, "app_meta"):
        report.warnings.append(
            "预检：app_meta 表缺失，将由迁移 004 建表并生成游标密钥"
        )
    elif con.execute(
        "SELECT 1 FROM app_meta WHERE key = ?",
        (SEARCH_CURSOR_SECRET_KEY,),
    ).fetchone() is None:
        report.warnings.append(
            "预检：app_meta 缺少 search_cursor_secret 行，"
            "将由迁移 004 以 INSERT OR IGNORE 补齐（不覆盖既有行）"
        )


# ---------------------------------------------------------------------------
# 终态校验
# ---------------------------------------------------------------------------


def _verify_final_state(con: sqlite3.Connection) -> None:
    applied = applied_versions(con)
    expected = {m.version for m in MIGRATIONS}
    missing = sorted(expected - applied)
    if missing:
        raise MigrationError(
            f"迁移终态校验失败：以下编号无 applied 记录：{missing}；"
            "服务拒绝接受业务请求"
        )

    problems: list[str] = []
    if not _v2_evidence(con):
        problems.append("confirmations.location_id 唯一索引缺失或不是唯一索引")
    if _index_exists(con, "idx_confirm_loc"):
        problems.append("旧非唯一索引 idx_confirm_loc 仍存在（应已被 002 取代）")
    if not _v3_evidence(con):
        problems.append("release_candidates 表或其文档索引缺失")
    if not _v4_evidence(con):
        problems.append("app_meta 游标密钥行缺失")
    if _duplicate_confirmations(con):
        problems.append("confirmations 仍存在同一 location_id 的重复确认")
    if problems:
        raise MigrationError(
            "迁移终态校验失败，服务拒绝接受业务请求：" + "；".join(problems)
        )


# ---------------------------------------------------------------------------
# 迁移主流程
# ---------------------------------------------------------------------------


def _run_migrations(
    con: sqlite3.Connection, report: MigrationReport
) -> None:
    """单个写事务内的迁移实现（调用方负责 BEGIN/COMMIT/连接关闭）。"""
    con.execute(LEDGER_DDL)
    done = applied_versions(con)

    # 识别无审计表的旧库：审计表刚建立且尚无任何 applied 记录，
    # 但业务表已存在 → 按各编号证据补登基线，未落地的编号正常执行。
    if not done and _table_exists(con, "documents"):
        report.legacy = True

    try:
        _preflight(con, legacy=report.legacy, report=report)
    except MigrationError as exc:
        # 预检（重复确认）发生在 002 建唯一索引之前：记 002 failed 审计行，
        # 不删改任何数据，外层提交审计后拒绝启动。
        _ledger_record(con, 2, "confirmations_location_unique", "failed",
                       error=str(exc), note="preflight")
        raise
    for w in report.warnings:
        logger.warning(w)

    for mig in MIGRATIONS:
        if mig.version in done:
            continue

        if report.legacy and mig.evidence is not None and mig.evidence(con):
            # 旧库中该版本已落地：不重复执行 DDL，只追加基线审计行。
            _ledger_record(
                con, mig.version, mig.name, "applied",
                note="legacy-baseline",
            )
            report.stamped.append(mig.version)
            done.add(mig.version)
            continue

        sp = f"mig_{mig.version}"
        con.execute(f"SAVEPOINT {sp}")
        try:
            for statement in mig.ddl.split(";"):
                if statement.strip():
                    con.execute(statement)
            if mig.after is not None:
                mig.after(con)
        except Exception as exc:
            # 只回滚这一步；failed 审计行留在外层事务中，随审计一并提交
            con.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            _ledger_record(
                con, mig.version, mig.name, "failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise MigrationError(
                f"迁移 {mig.version:03d}（{mig.name}）执行失败，该步已回滚；"
                f"错误：{type(exc).__name__}: {exc}；"
                "失败已记入 schema_migrations，服务拒绝启动，"
                "修复后重试为幂等操作"
            ) from exc
        else:
            con.execute(f"RELEASE SAVEPOINT {sp}")
            _ledger_record(con, mig.version, mig.name, "applied")
            report.applied.append(mig.version)
            done.add(mig.version)

    try:
        _verify_final_state(con)
    except MigrationError as exc:
        # 校验失败不对应任何单步回滚：以哨兵编号 0 留一条 failed 审计，
        # 下一次启动会重新终态校验（全编号已 applied 时）。
        _ledger_record(con, 0, "post_verify", "failed", error=str(exc))
        raise
    # user_version 作为 schema 版本的冗余镜像（与审计表在同一事务内原子更新）
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def init_db() -> MigrationReport:
    """启动时迁移：单写事务、可审计、失败拒绝启动。

    - 全新库：建审计表后顺序执行全部迁移并记录 applied；
    - 已纳管库：只执行缺少 applied 记录的编号（重试幂等）；
    - 无审计表的旧库：非破坏性预检后，已落地编号以 evidence 补登基线
      （只追加数据原样保留），未落地编号正常迁移（补唯一约束、索引、
      release_candidates、app_meta 与游标密钥）。

    任一预检致命错误、迁移步失败或终态校验失败都抛出 MigrationError，
    由调用方（lifespan）阻止服务接受业务请求。
    """
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    con = connect()
    report = MigrationReport(fresh=_is_empty_db(con))
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            _run_migrations(con, report)
        except MigrationError:
            # 该步已回滚；保留 failed 审计行后提交审计，再拒绝启动
            con.execute("COMMIT")
            raise
        except Exception as exc:
            # 未预期错误：整体回滚（含可能写入的审计行），拒绝启动
            con.execute("ROLLBACK")
            raise MigrationError(
                f"迁移过程发生未预期错误，事务已整体回滚：{exc}"
            ) from exc
        else:
            con.execute("COMMIT")
    finally:
        con.close()

    logger.info(
        "数据库迁移完成：version=%d applied=%s stamped=%s warnings=%d",
        SCHEMA_VERSION, report.applied, report.stamped, len(report.warnings),
    )
    return report


def _is_empty_db(con: sqlite3.Connection) -> bool:
    """库文件中没有任何用户对象（页全空的全新库）。"""
    row = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchone()
    return row[0] == 0


def readiness(con: sqlite3.Connection) -> dict:
    """就绪探针数据：版本、applied/failed 记录与终态校验结论。

    历史 failed 行不永久阻塞就绪：只要 1..SCHEMA_VERSION 全部存在 applied
    记录且证据齐备（唯一索引、release_candidates、游标密钥、无重复确认），
    即视为就绪；失败仍原样保留在审计表中供排查。
    """
    applied = sorted(applied_versions(con))
    history = migration_history(con)
    failed = [
        {
            "version": r["version"],
            "name": r["name"],
            "error": r["error"],
            "created_at": r["created_at"],
        }
        for r in history
        if r["status"] == "failed"
    ]
    ready = applied == list(range(1, SCHEMA_VERSION + 1))
    if ready:
        try:
            _verify_final_state(con)
        except MigrationError as exc:
            ready = False
            verify_error = str(exc)
        else:
            verify_error = None
    else:
        verify_error = None
    return {
        "ready": ready,
        "schema_version": SCHEMA_VERSION,
        "applied_versions": applied,
        "failed": failed,
        "verify_error": verify_error,
    }
