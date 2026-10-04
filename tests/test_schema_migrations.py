"""版本化、可审计迁移流程回归。

覆盖：
- 全新库：单写事务内顺序执行 001..N，全部记 applied、user_version 镜像；
- 已部署旧库（无审计表）：只追加数据原样保留，已落地编号按证据补登
  legacy-baseline，未落地编号（唯一约束 / release_candidates / app_meta 与
  游标密钥）正常迁移；缺失的旧索引由 001 幂等补齐；
- 旧库预检发现 confirmations 重复：列出 location_id 与确认行 ID 拒绝启动，
  不删数据、不建唯一索引，审计记 002 failed(note=preflight)，重试幂等；
- 单步失败：SAVEPOINT 只回滚该步，审计记编号/名/错误，修复后续跑成功，
  已 applied 的编号不重复执行；
- 终态校验失败：拒绝启动并留哨兵审计；重启校验通过后就绪；
- 重启幂等：二次 init_db 不新增审计行、游标密钥不变；
- HTTP 门禁：未就绪时业务请求 503，/health 存活、/readyz 报详情。
"""
import sqlite3
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app, _STATE


def _use_db(tmp_path, monkeypatch, name="app.db"):
    path = str(tmp_path / name)
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def _ledger_rows(con):
    return [
        (r["version"], r["name"], r["status"], r["note"])
        for r in con.execute(
            "SELECT version, name, status, note FROM schema_migrations ORDER BY id"
        )
    ]


def _seed_minimal_legacy(path, *, indexes=(), unique=False, duplicates=0):
    """构造无审计表的旧版库：核心业务表 + 可选择的索引，可带重复确认。"""
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT 't');
        CREATE TABLE versions (id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL,
            number INTEGER NOT NULL, content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT 't', UNIQUE(document_id, number));
        CREATE TABLE annotations (id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL,
            body TEXT NOT NULL, selection TEXT NOT NULL, left_context TEXT NOT NULL DEFAULT '',
            right_context TEXT NOT NULL DEFAULT '', orig_start INTEGER NOT NULL,
            orig_end INTEGER NOT NULL, created_version_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT 't');
        CREATE TABLE locations (id INTEGER PRIMARY KEY AUTOINCREMENT, annotation_id INTEGER NOT NULL,
            version_id INTEGER NOT NULL, status TEXT NOT NULL, start INTEGER, "end" INTEGER,
            left_match INTEGER, right_match INTEGER, score INTEGER,
            candidates TEXT NOT NULL DEFAULT '[]', carried_from INTEGER,
            created_at TEXT NOT NULL DEFAULT 't', UNIQUE(annotation_id, version_id));
        CREATE TABLE confirmations (id INTEGER PRIMARY KEY AUTOINCREMENT, location_id INTEGER NOT NULL,
            start INTEGER NOT NULL, "end" INTEGER NOT NULL, note TEXT,
            created_at TEXT NOT NULL DEFAULT 't');
        CREATE TABLE dependencies (id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL,
            prerequisite_id INTEGER NOT NULL, dependent_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT 't',
            UNIQUE(document_id, prerequisite_id, dependent_id),
            CHECK (prerequisite_id <> dependent_id));
        CREATE TABLE completions (id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL,
            annotation_id INTEGER NOT NULL, note TEXT, created_at TEXT NOT NULL DEFAULT 't',
            UNIQUE(annotation_id));
        CREATE TABLE policy_revisions (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            revision INTEGER NOT NULL, strategy_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT 't', UNIQUE(name, revision));
        CREATE TABLE review_packages (id TEXT PRIMARY KEY, document_id INTEGER NOT NULL,
            digest TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT 't',
            applied_at TEXT, result_json TEXT);
        """
    )
    # 只追加业务数据
    con.execute("INSERT INTO documents (title) VALUES ('旧库文档')")
    con.execute("INSERT INTO versions (document_id, number, content) VALUES (1, 1, '正文')")
    con.execute(
        """INSERT INTO annotations
           (document_id, body, selection, orig_start, orig_end, created_version_id)
           VALUES (1, '批注', '正', 0, 1, 1)"""
    )
    con.execute(
        "INSERT INTO locations (annotation_id, version_id, status, candidates) "
        "VALUES (1, 1, 'ambiguous', '[]')"
    )
    for ddl in indexes:
        con.execute(ddl)
    # 001 时代：非唯一索引；unique=True 时升级为唯一索引（同 v2 结果）
    con.execute("CREATE INDEX idx_confirm_loc ON confirmations (location_id)")
    if unique:
        con.execute("DROP INDEX idx_confirm_loc")
        con.execute(
            "CREATE UNIQUE INDEX idx_confirm_loc_unique ON confirmations (location_id)"
        )
    for i in range(duplicates):
        con.execute(
            'INSERT INTO confirmations (location_id, start, "end") VALUES (1, ?, ?)',
            (i, i + 1),
        )
    con.commit()
    con.close()


_V1_INDEXES = (
    "CREATE INDEX idx_versions_doc ON versions (document_id, number)",
    "CREATE INDEX idx_locations_ann ON locations (annotation_id)",
    "CREATE INDEX idx_locations_ver ON locations (version_id)",
    "CREATE INDEX idx_dependencies_doc ON dependencies (document_id)",
    "CREATE INDEX idx_completions_doc ON completions (document_id)",
    "CREATE INDEX idx_policy_name ON policy_revisions (name, revision)",
    "CREATE INDEX idx_review_packages_doc ON review_packages (document_id)",
)


# ---------------------------------------------------------------------------
# 全新库
# ---------------------------------------------------------------------------


def test_fresh_db_runs_all_versions_in_order(tmp_path, monkeypatch):
    _use_db(tmp_path, monkeypatch)
    report = db.init_db()

    assert report.fresh is True
    assert report.legacy is False
    assert report.applied == [1, 2, 3, 4]
    assert report.stamped == []

    con = db.connect()
    try:
        rows = _ledger_rows(con)
        assert [r[:3] for r in rows] == [
            (1, "initial_schema", "applied"),
            (2, "confirmations_location_unique", "applied"),
            (3, "release_candidates", "applied"),
            (4, "app_meta_cursor_secret", "applied"),
        ]
        assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        info = db.readiness(con)
        assert info["ready"] is True
        assert info["applied_versions"] == [1, 2, 3, 4]
        assert info["failed"] == []
    finally:
        con.close()


def test_reboot_is_idempotent_and_secret_stable(tmp_path, monkeypatch):
    _use_db(tmp_path, monkeypatch)
    db.init_db()
    con = db.connect()
    secret_before = con.execute(
        "SELECT value FROM app_meta WHERE key = ?",
        (db.SEARCH_CURSOR_SECRET_KEY,),
    ).fetchone()[0]
    con.close()

    report = db.init_db()
    assert report.applied == [] and report.stamped == []

    con = db.connect()
    try:
        # 审计行数量不变，密钥不更换
        assert con.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 4
        secret_after = con.execute(
            "SELECT value FROM app_meta WHERE key = ?",
            (db.SEARCH_CURSOR_SECRET_KEY,),
        ).fetchone()[0]
        assert secret_after == secret_before
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 旧库升级
# ---------------------------------------------------------------------------


def test_legacy_db_preserves_append_only_data(tmp_path, monkeypatch):
    path = _use_db(tmp_path, monkeypatch)
    # 与 data/app.db 同形：已有唯一索引，但缺 release_candidates / app_meta
    _seed_minimal_legacy(path, indexes=_V1_INDEXES, unique=True)
    con0 = sqlite3.connect(path)
    con0.execute(
        "INSERT INTO completions (document_id, annotation_id, note) VALUES (1, 1, '旧完成')"
    )
    con0.execute(
        "INSERT INTO review_packages (id, document_id, digest, payload_json) "
        "VALUES ('pkg1', 1, 'dig', '{}')"
    )
    con0.commit()
    con0.close()

    report = db.init_db()
    assert report.legacy is True
    assert report.stamped == [1, 2]          # 已落地 → 补登基线
    assert report.applied == [3, 4]          # 缺失 → 真正迁移

    con = db.connect()
    try:
        # 只追加数据原样保留
        assert con.execute("SELECT title FROM documents").fetchone()[0] == "旧库文档"
        assert con.execute(
            "SELECT note FROM completions WHERE annotation_id = 1"
        ).fetchone()[0] == "旧完成"
        assert con.execute(
            "SELECT digest FROM review_packages WHERE id = 'pkg1'"
        ).fetchone()[0] == "dig"
        # 补齐的结构
        assert db._v2_evidence(con)
        assert db._v3_evidence(con)
        assert db._v4_evidence(con)
        assert "idx_confirm_loc" not in {
            r[1] for r in con.execute("PRAGMA index_list('confirmations')")
        }
        rows = _ledger_rows(con)
        assert [r for r in rows if r[2] == "applied"] == [
            (1, "initial_schema", "applied", "legacy-baseline"),
            (2, "confirmations_location_unique", "applied", "legacy-baseline"),
            (3, "release_candidates", "applied", ""),
            (4, "app_meta_cursor_secret", "applied", ""),
        ]
        assert con.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        con.close()


def test_legacy_db_with_missing_indexes_fills_them_non_destructively(
    tmp_path, monkeypatch
):
    path = _use_db(tmp_path, monkeypatch)
    # 只有 confirmations 的旧非唯一索引，其余 001 索引全部缺失
    _seed_minimal_legacy(path)
    report = db.init_db()

    # 001 证据不足（缺 idx_versions_doc）→ 执行幂等 DDL 补齐，而非直接补登
    assert 1 in report.applied
    assert any("idx_versions_doc" in w for w in report.warnings)

    con = db.connect()
    try:
        assert db._index_exists(con, "idx_versions_doc")
        assert db._index_exists(con, "idx_locations_ann")
        assert db._index_exists(con, "idx_completions_doc")
        # 数据仍在
        assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert db.readiness(con)["ready"] is True
    finally:
        con.close()


def test_legacy_db_with_app_meta_table_but_no_secret_row(tmp_path, monkeypatch):
    path = _use_db(tmp_path, monkeypatch)
    _seed_minimal_legacy(path, indexes=_V1_INDEXES, unique=True)
    con0 = sqlite3.connect(path)
    con0.execute("CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con0.execute("INSERT INTO app_meta (key, value) VALUES ('other', 'kept')")
    con0.commit()
    con0.close()

    report = db.init_db()
    assert report.applied == [3, 4]
    assert any("app_meta 缺少 search_cursor_secret 行" in w for w in report.warnings)

    con = db.connect()
    try:
        # 既有行不覆盖，只 INSERT OR IGNORE 补密钥行
        assert con.execute(
            "SELECT value FROM app_meta WHERE key = 'other'"
        ).fetchone()[0] == "kept"
        assert db._v4_evidence(con)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 重复确认：拒绝启动
# ---------------------------------------------------------------------------


def test_duplicate_confirmations_abort_with_audit_and_preserve_rows(
    tmp_path, monkeypatch
):
    path = _use_db(tmp_path, monkeypatch)
    _seed_minimal_legacy(path, duplicates=2)

    with pytest.raises(db.MigrationError) as exc:
        db.init_db()
    message = str(exc.value)
    assert "location_id=1" in message
    assert "确认行 ID：1,2" in message

    con = db.connect()
    try:
        # 数据未删改、唯一索引未建、旧非唯一索引保留
        assert [
            r[0] for r in con.execute(
                "SELECT id FROM confirmations ORDER BY id"
            )
        ] == [1, 2]
        assert not db._index_exists(con, "idx_confirm_loc_unique")
        assert db._index_exists(con, "idx_confirm_loc")
        # 预检失败也留审计：002 failed / preflight，且无 001 之后编号被执行
        rows = _ledger_rows(con)
        assert rows == [
            (2, "confirmations_location_unique", "failed", "preflight")
        ]
        assert db.readiness(con)["ready"] is False
        assert con.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        con.close()

    # 人工清理（保留首条）后重试 → 成功；failed 审计保留，applied 追加
    con = sqlite3.connect(path)
    con.execute("DELETE FROM confirmations WHERE id = 2")
    con.commit()
    con.close()

    db.init_db()
    con = db.connect()
    try:
        rows = _ledger_rows(con)
        assert rows[0] == (2, "confirmations_location_unique", "failed", "preflight")
        assert (2, "confirmations_location_unique", "applied", "") in rows
        assert db._v2_evidence(con)
        assert db.readiness(con)["ready"] is True
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 单步失败：回滚该步、审计、幂等重试
# ---------------------------------------------------------------------------


def test_step_failure_rolls_back_step_and_retries(tmp_path, monkeypatch):
    _use_db(tmp_path, monkeypatch)

    orig_migrations = db.MIGRATIONS
    mig3 = next(m for m in db.MIGRATIONS if m.version == 3)

    def boom(con):
        raise sqlite3.OperationalError("注入的 003 失败")

    monkeypatch.setattr(
        db, "MIGRATIONS",
        tuple(replace(mig3, after=boom) if m.version == 3 else m
              for m in db.MIGRATIONS),
    )

    with pytest.raises(db.MigrationError, match="003"):
        db.init_db()

    con = db.connect()
    try:
        # 003 的 DDL 随 SAVEPOINT 一并回滚；001/002 保留
        assert not db._table_exists(con, "release_candidates")
        rows = _ledger_rows(con)
        assert rows == [
            (1, "initial_schema", "applied", ""),
            (2, "confirmations_location_unique", "applied", ""),
            (3, "release_candidates", "failed", ""),
        ]
        failed = con.execute(
            "SELECT error FROM schema_migrations WHERE status = 'failed'"
        ).fetchone()
        assert "OperationalError" in failed["error"] and "注入的 003 失败" in failed["error"]
        assert con.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        con.close()

    # 恢复后重试：只跑 003/004，failed 行原样保留
    # （只还原 MIGRATIONS，不能 monkeypatch.undo()：那会一并还原 DB_PATH）
    monkeypatch.setattr(db, "MIGRATIONS", orig_migrations)
    report = db.init_db()
    assert report.applied == [3, 4]
    con = db.connect()
    try:
        statuses = [
            (r["version"], r["status"])
            for r in con.execute("SELECT version, status FROM schema_migrations ORDER BY id")
        ]
        assert statuses == [
            (1, "applied"), (2, "applied"), (3, "failed"),
            (3, "applied"), (4, "applied"),
        ]
        assert con.execute("PRAGMA user_version").fetchone()[0] == 4
        assert db.readiness(con)["ready"] is True
    finally:
        con.close()


def test_post_verify_failure_blocks_and_recovers(tmp_path, monkeypatch):
    _use_db(tmp_path, monkeypatch)
    db.init_db()  # 先全部成功

    calls = {"n": 0}
    real_verify = db._verify_final_state

    def flaky(con):
        calls["n"] += 1
        if calls["n"] == 1:
            raise db.MigrationError("注入的终态校验失败")
        return real_verify(con)

    monkeypatch.setattr(db, "_verify_final_state", flaky)
    with pytest.raises(db.MigrationError, match="终态校验失败"):
        db.init_db()

    con = db.connect()
    try:
        sentinel = con.execute(
            "SELECT version, name, status FROM schema_migrations WHERE version = 0"
        ).fetchall()
        assert [tuple(r) for r in sentinel] == [(0, "post_verify", "failed")]
    finally:
        con.close()

    # 只还原被打桩的函数（不能 monkeypatch.undo()：那会还原 DB_PATH）
    monkeypatch.setattr(db, "_verify_final_state", real_verify)
    db.init_db()
    con = db.connect()
    try:
        assert db.readiness(con)["ready"] is True  # 历史 failed 不永久阻塞
    finally:
        con.close()


# ---------------------------------------------------------------------------
# HTTP 门禁
# ---------------------------------------------------------------------------


def test_business_requests_503_until_ready(tmp_path, monkeypatch):
    _use_db(tmp_path, monkeypatch)
    db.init_db()

    saved = dict(_STATE)
    _STATE["ready"] = False
    try:
        c = TestClient(app)
        r = c.post("/documents", json={"title": "x", "content": "x"})
        assert r.status_code == 503
        assert r.headers.get("retry-after") == "30"
        r = c.get("/health")
        assert r.status_code == 200
        r = c.get("/readyz")
        assert r.status_code == 503
        assert r.json()["schema_version"] == db.SCHEMA_VERSION
    finally:
        _STATE.update(saved)


def test_lifespan_aborts_on_failed_migration(tmp_path, monkeypatch):
    path = _use_db(tmp_path, monkeypatch)
    _seed_minimal_legacy(path, duplicates=2)

    with pytest.raises(db.MigrationError):
        with TestClient(app):
            pass  # lifespan 抛出 → 服务不进入接受请求状态
