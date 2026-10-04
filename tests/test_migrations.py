"""版本化、可审计 schema 迁移的回归测试。

覆盖：
- 全新库：顺序应用全部迁移，审计行完整，重启幂等；
- 旧版库（缺表 / 旧索引 / app_meta 缺行）：保留全部只追加数据，
  补齐唯一约束、索引与游标密钥；
- 已部署旧库文件的形态（缺 release_candidates / app_meta）；
- 单步失败回滚该步、记录 failed 审计行，修复后重试幂等成功；
- 迁移后校验失败整体回滚并拒绝启动；
- FastAPI lifespan：迁移失败时不接受业务请求，/health 返回 503；
- 审计表只追加（UPDATE/DELETE 被触发器拒绝）。
"""
import os
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db, migrations
from app.main import app


def _use_db(tmp_path, monkeypatch):
    path = str(tmp_path / "app.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def _audit_rows(con, status=None):
    rows = migrations.read_audit(con)
    if status is not None:
        rows = [r for r in rows if r["status"] == status]
    return rows


# ---------------------------------------------------------------------------
# 全新库
# ---------------------------------------------------------------------------


def test_fresh_db_applies_all_versions(tmp_path, monkeypatch):
    _use_db(tmp_path, monkeypatch)
    result = db.init_db()
    assert result.version == migrations.CURRENT_VERSION
    assert set(result.applied_versions) == {1, 2, 3}

    con = db.connect()
    try:
        applied = _audit_rows(con, "applied")
        assert [r["version"] for r in applied] == [1, 2, 3]
        assert all(r["error"] is None for r in applied)
        assert not _audit_rows(con, "failed")
        # 审计行带编号、名称、状态与时间戳
        row = applied[0]
        assert row["name"] == "initial_schema"
        assert row["started_at"] and row["finished_at"]

        # 结构校验依赖的最终对象齐全
        for table in migrations.REQUIRED_TABLES:
            assert con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
        # 游标密钥已生成（非空 hex）
        secret = con.execute(
            "SELECT value FROM app_meta WHERE key=?",
            (migrations.SECRET_KEY,),
        ).fetchone()
        assert secret is not None and len(secret["value"]) == 64
    finally:
        con.close()


def test_restart_is_idempotent_and_keeps_secret(tmp_path, monkeypatch):
    _use_db(tmp_path, monkeypatch)
    db.init_db()
    con = db.connect()
    try:
        secret_before = con.execute(
            "SELECT value FROM app_meta WHERE key=?", (migrations.SECRET_KEY,)
        ).fetchone()["value"]
        applied_before = _audit_rows(con, "applied")
    finally:
        con.close()

    result = db.init_db()  # 重启
    assert result.applied_versions == ()

    con = db.connect()
    try:
        secret_after = con.execute(
            "SELECT value FROM app_meta WHERE key=?", (migrations.SECRET_KEY,)
        ).fetchone()["value"]
        assert secret_after == secret_before
        # 没有新增 applied 行
        assert _audit_rows(con, "applied") == applied_before
    finally:
        con.close()


def test_audit_table_is_append_only(tmp_path, monkeypatch):
    _use_db(tmp_path, monkeypatch)
    db.init_db()
    con = db.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE schema_migrations SET status='failed' WHERE version=1")
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("DELETE FROM schema_migrations WHERE version=1")
    finally:
        con.close()


def test_higher_schema_version_refuses_start(tmp_path, monkeypatch):
    _use_db(tmp_path, monkeypatch)
    db.init_db()
    con = db.connect()
    try:
        # 伪造一条来自更高版本的 applied 行（绕过部分唯一索引直接插）
        con.execute(
            "INSERT INTO schema_migrations (version, name, status, error)"
            " VALUES (?, ?, 'applied', NULL)",
            (migrations.CURRENT_VERSION + 1, "future"),
        )
    finally:
        con.close()

    with pytest.raises(migrations.MigrationError, match="高于本服务支持的最高版本"):
        db.init_db()


# ---------------------------------------------------------------------------
# 旧版库平滑升级
# ---------------------------------------------------------------------------


def _build_legacy_db(path, seeded_rows=None):
    """构造已部署旧库：迁移 1 的表结构 + 旧非唯一确认索引，无审计表、
    无 release_candidates/app_meta（模拟缺表、旧索引、app_meta 缺行）。

    使用 isolation_level=None 的自动提交连接，保证结构与种子行落盘
    （与真实已部署旧库一致：全部内容均为已提交数据）。
    """
    con = sqlite3.connect(path, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        for statement in migrations.DDL_V1:
            con.execute(statement)
        con.execute("DROP INDEX idx_release_candidates_doc")
        con.execute("DROP TABLE release_candidates")
        if seeded_rows:
            for item in seeded_rows:
                sql, params = item if len(item) == 2 else (item[0], ())
                con.execute(sql, params)
    finally:
        con.close()


def test_legacy_db_upgrades_and_preserves_append_only_data(tmp_path, monkeypatch):
    path = _use_db(tmp_path, monkeypatch)
    _build_legacy_db(path, seeded_rows=[
        ("INSERT INTO documents (id, title) VALUES (?, ?)", (10, "旧文档")),
        ("INSERT INTO versions (document_id, number, content) VALUES (?, 1, '甲：目标一')", (10,)),
        ("INSERT INTO annotations "
         "(id, document_id, body, selection, orig_start, orig_end, created_version_id)"
         " VALUES (?, ?, '批注', '目标', 2, 4, ?)", (20, 10, 1)),
        ("INSERT INTO locations "
         "(annotation_id, version_id, status, start, \"end\", score, candidates)"
         " VALUES (?, 1, 'resolved', 2, 4, 5, '[]')", (20,)),
        ("INSERT INTO confirmations "
         "(id, location_id, start, \"end\", note) VALUES (?, ?, 2, 4, '旧确认')", (30, 1)),
        ("INSERT INTO annotations "
         "(id, document_id, body, selection, orig_start, orig_end, created_version_id)"
         " VALUES (?, ?, '批注2', '甲', 0, 1, ?)", (21, 10, 1)),
        ("INSERT INTO dependencies (document_id, prerequisite_id, dependent_id)"
         " VALUES (?, 20, 21)", (10,)),
        ("INSERT INTO completions (document_id, annotation_id, note) VALUES (?, 20, 'done')", (10,)),
        ("INSERT INTO policy_revisions (name, revision, strategy_json)"
         " VALUES ('default', 1, '{}')", ()),
        ("INSERT INTO review_packages (id, document_id, digest, payload_json)"
         " VALUES ('pkg-1', ?, 'd1', '{}')", (10,)),
    ])

    db.init_db()

    con = db.connect()
    try:
        # 审计：1..3 全部 applied，无 failed
        assert [r["version"] for r in _audit_rows(con, "applied")] == [1, 2, 3]
        assert not _audit_rows(con, "failed")

        # 只追加数据全部原样保留（含显式 ID）
        assert con.execute("SELECT title FROM documents WHERE id=10").fetchone()["title"] == "旧文档"
        assert con.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM annotations").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 1
        c = con.execute("SELECT start, \"end\", note FROM confirmations WHERE id=30").fetchone()
        assert tuple(c) == (2, 4, "旧确认")
        assert con.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM completions").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM policy_revisions").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM review_packages").fetchone()[0] == 1

        # 补齐：release_candidates 表 + 索引、唯一确认索引、app_meta 密钥
        indexes = {r[1]: r[2] for r in con.execute("PRAGMA index_list('confirmations')")}
        assert indexes.get("idx_confirm_loc_unique") == 1
        assert "idx_confirm_loc" not in indexes
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_release_candidates_doc'"
        ).fetchone()
        assert con.execute(
            "SELECT length(value) FROM app_meta WHERE key=?", (migrations.SECRET_KEY,)
        ).fetchone()[0] == 64

        # 旧非唯一索引删除后，新重复确认被拒绝
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                'INSERT INTO confirmations (location_id, start, "end") VALUES (1, 9, 9)'
            )
    finally:
        con.close()


def test_deployed_db_shape_migrates(tmp_path, monkeypatch):
    """仓库内 data/app.db 形态（有唯一索引但缺两张表）可平滑升级。"""
    source = os.path.join(os.path.dirname(db.__file__), os.pardir, "data", "app.db")
    source = os.path.normpath(source)
    if not os.path.exists(source):
        pytest.skip("仓库未附带 data/app.db")
    target = _use_db(tmp_path, monkeypatch)
    shutil.copy2(source, target)
    # 旧库可能带 WAL/SHM 侧车文件；一并复制
    for suffix in ("-wal", "-shm"):
        sidecar = source + suffix
        if os.path.exists(sidecar):
            shutil.copy2(sidecar, target + suffix)

    before = {}
    con = sqlite3.connect(target)
    for table in (
        "documents", "versions", "annotations", "locations",
        "confirmations", "dependencies", "completions",
        "policy_revisions", "review_packages",
    ):
        before[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    con.close()

    db.init_db()

    con = db.connect()
    try:
        assert [r["version"] for r in _audit_rows(con, "applied")] == [1, 2, 3]
        for table, count in before.items():
            assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='release_candidates'"
        ).fetchone()
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_meta'"
        ).fetchone()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 重复确认预检（致命，非破坏性）
# ---------------------------------------------------------------------------


def test_duplicate_confirmations_preflight_refuses(tmp_path, monkeypatch):
    path = _use_db(tmp_path, monkeypatch)
    _build_legacy_db(path)
    con = sqlite3.connect(path, isolation_level=None)
    try:
        con.execute(
            "INSERT INTO documents (id, title) VALUES (1, 'd')")
        con.execute(
            "INSERT INTO versions (document_id, number, content) VALUES (1, 1, 'xx')")
        con.execute(
            "INSERT INTO annotations (id, document_id, body, selection,"
            " orig_start, orig_end, created_version_id) VALUES (1, 1, 'a', 'x', 0, 1, 1)")
        con.execute(
            "INSERT INTO locations (id, annotation_id, version_id, status, candidates)"
            " VALUES (1, 1, 1, 'ambiguous', '[]')")
        con.execute(
            'INSERT INTO confirmations (id, location_id, start, "end") VALUES (1, 1, 0, 1)')
        con.execute(
            'INSERT INTO confirmations (id, location_id, start, "end") VALUES (2, 1, 0, 1)')
    finally:
        con.close()

    with pytest.raises(migrations.MigrationError) as excinfo:
        db.init_db()
    assert "location_id=1" in str(excinfo.value)
    assert "1" in str(excinfo.value) and "2" in str(excinfo.value)

    con = sqlite3.connect(path)
    try:
        # 非破坏性：重复行原样保留
        assert con.execute(
            "SELECT COUNT(*) FROM confirmations WHERE location_id=1"
        ).fetchone()[0] == 2
        # 预检失败也可审计：v2 留一条 failed（含定位信息），无任何 applied
        audit = con.execute(
            "SELECT version, status, error FROM schema_migrations"
        ).fetchall()
        assert [row[:2] for row in audit] == [(2, "failed")]
        assert "location_id=1" in audit[0][2]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 单步失败：回滚该步 + failed 审计；修复后重试幂等
# ---------------------------------------------------------------------------


def test_failed_step_rolls_back_and_retry_is_idempotent(tmp_path, monkeypatch):
    _use_db(tmp_path, monkeypatch)
    real_migrations = migrations.MIGRATIONS

    calls = {"count": 0}
    original_apply = real_migrations[2].apply

    def flaky_v3(con):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("模拟密钥生成失败")
        original_apply(con)

    broken = real_migrations[:2] + (
        migrations.Migration(3, "app_meta_cursor_secret", flaky_v3),
    )
    monkeypatch.setattr(migrations, "MIGRATIONS", broken)

    with pytest.raises(migrations.MigrationError, match="迁移 3"):
        db.init_db()

    con = db.connect()
    try:
        # 该步回滚：app_meta 未建立，密钥行不存在
        assert not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_meta'"
        ).fetchone()
        # 1、2 已 applied；3 有一条 failed 且错误文本入库
        assert [r["version"] for r in _audit_rows(con, "applied")] == [1, 2]
        failed = _audit_rows(con, "failed")
        assert len(failed) == 1
        assert failed[0]["version"] == 3
        assert "模拟密钥生成失败" in failed[0]["error"]

        # 部分唯一索引允许 failed 与后续 applied 共存（此处尚只有 failed）
        v3_rows = [r for r in _audit_rows(con) if r["version"] == 3]
        assert [(r["status"], r["id"]) for r in v3_rows] == [
            ("failed", failed[0]["id"])
        ]
    finally:
        con.close()

    # 修复后重启：1、2 不重跑，仅重试 3；审计保留旧 failed + 新 applied
    monkeypatch.setattr(migrations, "MIGRATIONS", real_migrations)
    result = db.init_db()
    assert result.applied_versions == (3,)

    con = db.connect()
    try:
        assert [r["version"] for r in _audit_rows(con, "applied")] == [1, 2, 3]
        v3_rows = [r for r in _audit_rows(con) if r["version"] == 3]
        assert [r["status"] for r in v3_rows] == ["failed", "applied"]
        assert con.execute(
            "SELECT 1 FROM app_meta WHERE key=?", (migrations.SECRET_KEY,)
        ).fetchone()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 校验失败：整体回滚，拒绝启动
# ---------------------------------------------------------------------------


def test_verify_failure_rolls_back_whole_transaction(tmp_path, monkeypatch):
    _use_db(tmp_path, monkeypatch)
    db.init_db()

    real_verify = migrations._verify_schema

    def broken_verify(con):
        # 模拟"补齐对象被外部删除"：校验阶段发现必需表缺失
        con.execute("DROP TABLE app_meta")
        real_verify(con)

    monkeypatch.setattr(migrations, "_verify_schema", broken_verify)

    # 无待执行迁移，但每次启动仍强制校验
    with pytest.raises(migrations.MigrationError, match="迁移后校验失败"):
        db.init_db()

    con = db.connect()
    try:
        # 校验失败整体回滚：DROP 没有生效
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_meta'"
        ).fetchone()
        assert con.execute(
            "SELECT value FROM app_meta WHERE key=?", (migrations.SECRET_KEY,)
        ).fetchone()
        # 无多余审计行（本次没有执行任何迁移步）
        assert len(_audit_rows(con)) == 3
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 服务就绪门禁
# ---------------------------------------------------------------------------


def test_service_does_not_serve_when_migration_fails(tmp_path, monkeypatch):
    _use_db(tmp_path, monkeypatch)

    def boom(con):
        raise RuntimeError("启动期迁移故障")

    monkeypatch.setattr(migrations, "MIGRATIONS", (
        migrations.Migration(1, "initial_schema", boom),
        *migrations.MIGRATIONS[1:],
    ))

    with pytest.raises(Exception):
        with TestClient(app) as client:
            client.get("/health")

    # 未进入 lifespan 就绪状态时，健康检查兜底返回 503
    assert getattr(app.state, "db_ready", False) is False
    response = TestClient(app).get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


def test_service_serves_after_successful_migration(tmp_path, monkeypatch):
    _use_db(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "schema_version": migrations.CURRENT_VERSION}

        # 业务请求正常（API、端口、路径不变）
        created = client.post("/documents", json={"title": "迁移后文档", "content": "正文"})
        assert created.status_code == 201
