"""版本化、可审计的 SQLite schema 迁移。

启动流程（全部发生在服务接受任何业务请求之前，单线程执行）：

1. **非破坏性预检**（只读，不持有写锁）：``PRAGMA integrity_check``、
   ``confirmations`` 历史重复（必须列出 location 与确认行 ID 后拒绝启动）、
   旧索引缺失与 ``app_meta`` 游标密钥行缺失（仅记录，可安全升级）。
2. 幂等建立审计表 ``schema_migrations``（含只追加触发器与
   "每版本至多一条 applied" 的部分唯一索引）。
3. 在**单个写事务**（``BEGIN IMMEDIATE``）中读取当前 schema 版本，
   按编号顺序执行所有未完成迁移：每步先建 SAVEPOINT，成功则在同事务
   内追加一条 ``applied`` 审计行；任一步失败即回滚该步（SAVEPOINT 级）、
   在同事务追加带错误文本的 ``failed`` 审计行后提交，并拒绝启动。
4. 全部迁移完成后、提交前做结构校验：必需表 / 索引（含唯一索引）齐全、
   旧非唯一索引已移除、游标密钥行存在、无重复确认、版本连续无缺号、
   ``PRAGMA foreign_key_check`` 无违例；校验失败整体回滚且拒绝启动。

迁移重试天然幂等：已 applied 的版本不会重跑；失败步在修复后重启时
重跑，所有迁移语句本身也均为 IF NOT EXISTS / INSERT OR IGNORE /
DROP IF EXISTS 的非破坏写法，只追加业务数据（versions / annotations /
locations / confirmations / dependencies / completions /
policy_revisions / review_packages / release_candidates）原样保留。
"""
from __future__ import annotations

import logging
import secrets
import sqlite3
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("app.migrations")

# 当前 schema 版本；新增迁移时追加 MIGRATIONS 并把这里 +1。
CURRENT_VERSION = 3

SECRET_KEY = "search_cursor_secret"

# ---------------------------------------------------------------------------
# 审计表：迁移尝试只追加（UPDATE/DELETE 由触发器拒绝）
# ---------------------------------------------------------------------------

AUDIT_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        version     INTEGER NOT NULL CHECK (version >= 1),
        name        TEXT NOT NULL,
        status      TEXT NOT NULL CHECK (status IN ('applied', 'failed')),
        error       TEXT,
        started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        finished_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        CHECK ((status = 'failed' AND error IS NOT NULL)
               OR (status = 'applied' AND error IS NULL))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_schema_migrations_version"
    "    ON schema_migrations (version, id)",
    # 同一版本至多一条 applied；失败行（failed）可多条，完整保留每次尝试。
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_schema_migrations_applied"
    "    ON schema_migrations (version) WHERE status = 'applied'",
    """
    CREATE TRIGGER IF NOT EXISTS trg_schema_migrations_no_update
    BEFORE UPDATE ON schema_migrations
    BEGIN
        SELECT RAISE(ABORT, 'schema_migrations 为只追加审计表，禁止 UPDATE');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_schema_migrations_no_delete
    BEFORE DELETE ON schema_migrations
    BEGIN
        SELECT RAISE(ABORT, 'schema_migrations 为只追加审计表，禁止 DELETE');
    END
    """,
)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class MigrationError(RuntimeError):
    """预检 / 迁移执行 / 迁移后校验失败：服务必须拒绝启动。"""


# ---------------------------------------------------------------------------
# 迁移 1：初始 schema（即最早部署版本的库结构）
#
# 说明：confirmations 上这里建立的是**旧版非唯一索引** idx_confirm_loc，
# 由迁移 2 在预检通过后替换为唯一索引；app_meta 表由迁移 3 建立。
# 全部语句均为 IF NOT EXISTS：对已部署旧库只补齐缺失对象，不触碰数据。
# ---------------------------------------------------------------------------

DDL_V1: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS documents (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        title      TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS versions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL REFERENCES documents (id),
        number      INTEGER NOT NULL,
        content     TEXT NOT NULL,
        created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE (document_id, number)
    )
    """,
    """
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
    )
    """,
    """
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
        candidates    TEXT NOT NULL DEFAULT '[]',
        carried_from  INTEGER REFERENCES locations (id),
        created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE (annotation_id, version_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS confirmations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        location_id INTEGER NOT NULL REFERENCES locations (id),
        start       INTEGER NOT NULL,
        "end"       INTEGER NOT NULL,
        note        TEXT,
        created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dependencies (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id     INTEGER NOT NULL REFERENCES documents (id),
        prerequisite_id INTEGER NOT NULL REFERENCES annotations (id),
        dependent_id    INTEGER NOT NULL REFERENCES annotations (id),
        created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE (document_id, prerequisite_id, dependent_id),
        CHECK (prerequisite_id <> dependent_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS completions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id   INTEGER NOT NULL REFERENCES documents (id),
        annotation_id INTEGER NOT NULL REFERENCES annotations (id),
        note          TEXT,
        created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE (annotation_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS policy_revisions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL,
        revision      INTEGER NOT NULL,
        strategy_json TEXT NOT NULL,
        created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE (name, revision)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review_packages (
        id           TEXT PRIMARY KEY,
        document_id  INTEGER NOT NULL REFERENCES documents (id),
        digest       TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        applied_at   TEXT,
        result_json  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS release_candidates (
        id            TEXT PRIMARY KEY,
        document_id   INTEGER NOT NULL REFERENCES documents (id),
        digest        TEXT NOT NULL,
        frozen_json   TEXT NOT NULL,
        created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        released_at   TEXT,
        result_json   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_versions_doc    ON versions (document_id, number)",
    "CREATE INDEX IF NOT EXISTS idx_locations_ann   ON locations (annotation_id)",
    "CREATE INDEX IF NOT EXISTS idx_locations_ver   ON locations (version_id)",
    "CREATE INDEX IF NOT EXISTS idx_confirm_loc     ON confirmations (location_id)",
    "CREATE INDEX IF NOT EXISTS idx_dependencies_doc ON dependencies (document_id)",
    "CREATE INDEX IF NOT EXISTS idx_completions_doc  ON completions (document_id)",
    "CREATE INDEX IF NOT EXISTS idx_policy_name      ON policy_revisions (name, revision)",
    "CREATE INDEX IF NOT EXISTS idx_review_packages_doc ON review_packages (document_id)",
    "CREATE INDEX IF NOT EXISTS idx_release_candidates_doc ON release_candidates (document_id)",
)


DDL_APP_META = """
CREATE TABLE IF NOT EXISTS app_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL
)
"""


def _exec_script_statements(con: sqlite3.Connection, statements: tuple[str, ...]) -> None:
    """逐条执行 DDL（**不能**用 executescript：它会隐式提交外层写事务）。"""
    for statement in statements:
        con.execute(statement)


def _migrate_initial_schema(con: sqlite3.Connection) -> None:
    """迁移 1：建立初始表与索引（对旧库仅补齐缺失对象，全部只追加数据原样保留）。"""
    _exec_script_statements(con, DDL_V1)


def _assert_no_duplicate_confirmations(con: sqlite3.Connection) -> None:
    """建立 confirmations.location_id 唯一索引前的非破坏性预检。

    历史版本没有唯一约束，并发确认可能在同一 location 上留下多条确认。
    检测到历史重复时**不静默删改**：抛出带可定位 ``location_id`` 与全部
    确认行 ID 的 MigrationError，由人工核对清理后重启（重试幂等）。
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
        raise MigrationError(
            "confirmations 表存在历史重复确认（同一迁移记录多条确认），"
            "已拒绝启动；唯一约束未建立，也未删改任何数据；"
            "请人工核对并清理以下位置后重新启动（重试幂等）：" + detail
        )


def _migrate_unique_confirmation_index(con: sqlite3.Connection) -> None:
    """迁移 2：confirmations.location_id 改为唯一索引。

    先复检历史重复（与启动前预检同一套非破坏性检查），再
    DROP 旧非唯一索引 + CREATE 唯一索引，两者均幂等，
    不触碰任何确认行。
    """
    _assert_no_duplicate_confirmations(con)
    con.execute("DROP INDEX IF EXISTS idx_confirm_loc")
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_confirm_loc_unique"
        "    ON confirmations (location_id)"
    )


def _migrate_app_meta_secret(con: sqlite3.Connection) -> None:
    """迁移 3：建立 app_meta 并补齐检索游标 HMAC 密钥。

    INSERT OR IGNORE：新库生成一次；旧库升级时补齐一行；已生成的密钥
    永不更换（游标跨重启仍可验签），密钥不随游标外泄。
    """
    con.execute(DDL_APP_META)
    con.execute(
        "INSERT OR IGNORE INTO app_meta (key, value) VALUES (?, ?)",
        (SECRET_KEY, secrets.token_hex(32)),
    )


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial_schema", _migrate_initial_schema),
    Migration(2, "confirmations_unique_index", _migrate_unique_confirmation_index),
    Migration(3, "app_meta_cursor_secret", _migrate_app_meta_secret),
)


# ---------------------------------------------------------------------------
# 预检 / 校验清单
# ---------------------------------------------------------------------------

# 最终版本必须存在的全部业务表（审计表 schema_migrations 另行保证）。
REQUIRED_TABLES: tuple[str, ...] = (
    "documents",
    "versions",
    "annotations",
    "locations",
    "confirmations",
    "dependencies",
    "completions",
    "policy_revisions",
    "review_packages",
    "release_candidates",
    "app_meta",
)

# 最终版本必须存在的索引：名称 → 是否唯一。
REQUIRED_INDEXES: dict[str, bool] = {
    "idx_versions_doc": False,
    "idx_locations_ann": False,
    "idx_locations_ver": False,
    "idx_confirm_loc_unique": True,
    "idx_dependencies_doc": False,
    "idx_completions_doc": False,
    "idx_policy_name": False,
    "idx_review_packages_doc": False,
    "idx_release_candidates_doc": False,
}

# 旧版索引名：最终版本中不得残留。
FORBIDDEN_INDEXES: tuple[str, ...] = ("idx_confirm_loc",)


@dataclass
class PreflightReport:
    """非破坏性预检结果（重复确认为致命项，直接抛错，不出现在这里）。"""

    integrity_ok: bool = True
    missing_indexes: list[str] = field(default_factory=list)
    app_meta_table_exists: bool = False
    app_meta_secret_present: bool = False

    @property
    def upgrade_needed(self) -> bool:
        return bool(
            self.missing_indexes
            or not self.app_meta_table_exists
            or not self.app_meta_secret_present
        )


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BootstrapResult:
    version: int
    applied_versions: tuple[int, ...]


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _index_exists(con: sqlite3.Connection, name: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _preflight(con: sqlite3.Connection) -> PreflightReport:
    """只读、非破坏性预检；发现重复确认等无法自动修复的问题即拒绝启动。"""
    integrity = con.execute("PRAGMA integrity_check").fetchone()
    integrity_ok = integrity is not None and integrity[0] == "ok"
    if not integrity_ok:
        detail = integrity[0] if integrity is not None else "无结果"
        raise MigrationError(f"数据库完整性检查失败（PRAGMA integrity_check）：{detail}")

    # 致命项：历史重复确认必须先经人工处理，绝不静默删改。
    _assert_no_duplicate_confirmations(con)

    report = PreflightReport(integrity_ok=True)

    present_indexes = {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    report.missing_indexes = sorted(
        name for name in REQUIRED_INDEXES if name not in present_indexes
    )

    report.app_meta_table_exists = _table_exists(con, "app_meta")
    if report.app_meta_table_exists:
        row = con.execute(
            "SELECT 1 FROM app_meta WHERE key = ?", (SECRET_KEY,)
        ).fetchone()
        report.app_meta_secret_present = row is not None

    return report


def _read_applied_versions(con: sqlite3.Connection) -> set[int]:
    return {
        row[0]
        for row in con.execute(
            "SELECT version FROM schema_migrations WHERE status = 'applied'"
        ).fetchall()
    }


def _record(
    con: sqlite3.Connection, migration: Migration, status: str, error: str | None
) -> None:
    con.execute(
        """
        INSERT INTO schema_migrations (version, name, status, error)
        VALUES (?, ?, ?, ?)
        """,
        (migration.version, migration.name, status, error),
    )


def _verify_schema(con: sqlite3.Connection) -> None:
    """迁移全部完成后、提交前的结构校验；任一不满足即整体回滚并拒绝启动。"""
    problems: list[str] = []

    for table in REQUIRED_TABLES:
        if not _table_exists(con, table):
            problems.append(f"缺少必需表 {table}")

    for name, required_unique in REQUIRED_INDEXES.items():
        row = con.execute(
            "SELECT "
            "EXISTS(SELECT 1 FROM sqlite_master WHERE type='index' AND name=?) AS present,"
            "COALESCE((SELECT \"unique\" FROM pragma_index_list('confirmations')"
            "          WHERE name=?), 0) AS is_unique",
            (name, name),
        ).fetchone()
        if not row["present"]:
            problems.append(f"缺少必需索引 {name}")
        elif required_unique and row["is_unique"] != 1:
            problems.append(f"索引 {name} 存在但不是唯一索引")

    for name in FORBIDDEN_INDEXES:
        if _index_exists(con, name):
            problems.append(f"旧索引 {name} 不应残留")

    secret = con.execute(
        "SELECT value FROM app_meta WHERE key = ?", (SECRET_KEY,)
    ).fetchone() if _table_exists(con, "app_meta") else None
    if secret is None or not secret[0]:
        problems.append(f"app_meta 缺少游标密钥行（key={SECRET_KEY}）")

    # 重复确认复检（正常情况下唯一索引已使该状态不可达，纵深防御）。
    try:
        _assert_no_duplicate_confirmations(con)
    except MigrationError as exc:
        problems.append(str(exc))

    applied = _read_applied_versions(con)
    expected = set(range(1, CURRENT_VERSION + 1))
    if applied != expected:
        problems.append(
            f"schema 版本不连续：applied={sorted(applied)}，"
            f"应为 {sorted(expected)}"
        )

    fk_violations = con.execute("PRAGMA foreign_key_check").fetchall()
    if fk_violations:
        sample = "; ".join(str(tuple(row)) for row in fk_violations[:5])
        problems.append(
            f"外键完整性检查发现 {len(fk_violations)} 条违例，示例：{sample}"
        )

    if problems:
        raise MigrationError(
            "迁移后校验失败，已整体回滚且不接受业务请求；请根据以下问题"
            "修复或从备份恢复后重新启动：" + "；".join(problems)
        )


def _record_preflight_failure(con: sqlite3.Connection, exc: Exception) -> None:
    """预检致命失败（如重复确认）也落一条 failed 审计行后再拒绝启动。

    编号记为负责建立该唯一约束的迁移（2）；预检未执行任何结构变更，
    该行只承担可审计责任，修复后重启会以新的 failed/applied 行追加。
    """
    error_text = f"{type(exc).__name__}: {exc}"[:4000]
    try:
        con.execute("BEGIN IMMEDIATE")
        _record(con, MIGRATIONS[1], "failed", error_text)
        con.execute("COMMIT")
    except sqlite3.Error:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass


def run_migrations(con: sqlite3.Connection) -> BootstrapResult:
    """执行预检 + 版本化迁移 + 校验。失败抛 MigrationError，调用方不得对外服务。"""
    # 审计表先行（幂等 DDL）：保证随后的预检拒绝也能留下可审计记录。
    for statement in AUDIT_DDL:
        con.execute(statement)

    try:
        report = _preflight(con)
    except MigrationError as exc:
        _record_preflight_failure(con, exc)
        log.error("schema 预检失败，拒绝启动：%s", exc)
        raise
    if report.upgrade_needed:
        log.warning(
            "预检发现可安全升级项（不删改任何业务数据）：缺失索引=%s，"
            "app_meta 表存在=%s，游标密钥行存在=%s",
            report.missing_indexes or "无",
            report.app_meta_table_exists,
            report.app_meta_secret_present,
        )
    else:
        log.info("schema 预检通过，未发现需要修复的历史问题")

    # 单个写事务：读取版本 → 顺序执行未完成迁移 → 记录编号/状态/错误。
    con.execute("BEGIN IMMEDIATE")
    committed = False
    try:
        applied = _read_applied_versions(con)
        ahead = sorted(v for v in applied if v > CURRENT_VERSION)
        if ahead:
            raise MigrationError(
                f"数据库 schema 版本 {ahead} 高于本服务支持的最高版本 "
                f"{CURRENT_VERSION}，拒绝启动以免降级破坏数据"
            )

        newly_applied: list[int] = []
        for migration in MIGRATIONS:
            if migration.version in applied:
                continue
            savepoint = f"sp_migration_{migration.version}"
            con.execute(f"SAVEPOINT {savepoint}")
            try:
                migration.apply(con)
            except Exception as exc:  # 该步失败：回滚该步，记录 failed 后提交
                con.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                con.execute(f"RELEASE SAVEPOINT {savepoint}")
                error_text = f"{type(exc).__name__}: {exc}"[:4000]
                _record(con, migration, "failed", error_text)
                con.execute("COMMIT")
                committed = True
                log.error(
                    "迁移 %d（%s）失败，已回滚该步并记录审计：%s",
                    migration.version, migration.name, error_text,
                )
                raise MigrationError(
                    f"schema 迁移 {migration.version}（{migration.name}）失败，"
                    f"已回滚该步；服务拒绝启动。修复后重新启动即可从该步"
                    f"幂等重试。错误：{error_text}"
                ) from exc
            else:
                con.execute(f"RELEASE SAVEPOINT {savepoint}")
                _record(con, migration, "applied", None)
                newly_applied.append(migration.version)
                log.info("迁移 %d（%s）完成", migration.version, migration.name)

        _verify_schema(con)
        con.execute("COMMIT")
        committed = True
    finally:
        if not committed:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass

    if newly_applied:
        log.info("schema 已迁移至版本 %d（本次应用：%s）",
                 CURRENT_VERSION, newly_applied)
    else:
        log.info("schema 已在版本 %d，无待执行迁移", CURRENT_VERSION)
    return BootstrapResult(version=CURRENT_VERSION, applied_versions=tuple(newly_applied))


def read_audit(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """读取全部迁移审计行（按时间顺序），供排障与测试断言使用。"""
    return con.execute(
        """
        SELECT id, version, name, status, error, started_at, finished_at
        FROM schema_migrations
        ORDER BY id
        """
    ).fetchall()
