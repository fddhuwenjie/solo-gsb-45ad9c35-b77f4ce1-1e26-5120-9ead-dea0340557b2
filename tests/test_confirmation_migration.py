"""confirmations.location_id 唯一约束（迁移 2）的建库 / 迁移回归。

- 全新库：init_db 顺序执行迁移 1..3，直接建立唯一索引，重复插入被拒绝；
- 无历史重复的旧版库（只有非唯一索引 idx_confirm_loc）：
  迁移 1 补齐其余对象、迁移 2 平滑建立唯一约束，已有确认行原样保留；
- 含历史重复的旧版库：迁移 2 预检不静默删改，抛出可定位到
  location_id 与确认行 ID 的错误，重复行原样保留，服务拒绝启动。
"""
import sqlite3

import pytest

from app import db
from app import migrations


def _use_db(tmp_path, monkeypatch):
    path = str(tmp_path / "app.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def _build_old_style_db(path):
    """模拟旧版库：迁移 1 的初始结构（含旧非唯一索引 idx_confirm_loc）。

    自动提交连接，确保结构落盘（模拟真实已部署旧库）。
    """
    con = sqlite3.connect(path, isolation_level=None)
    try:
        for statement in migrations.DDL_V1:
            con.execute(statement)
        con.execute("DROP TABLE IF EXISTS app_meta")
    finally:
        con.close()


def _seed_chain(con):
    """插入 文档 → 版本 → 批注 → 歧义迁移记录 各一行，返回 location_id。"""
    cur = con.execute("INSERT INTO documents (title) VALUES ('迁移测试')")
    document_id = cur.lastrowid
    cur = con.execute(
        "INSERT INTO versions (document_id, number, content) VALUES (?, 1, '文本')",
        (document_id,),
    )
    version_id = cur.lastrowid
    cur = con.execute(
        """
        INSERT INTO annotations
            (document_id, body, selection, orig_start, orig_end, created_version_id)
        VALUES (?, '批注', '文', 0, 1, ?)
        """,
        (document_id, version_id),
    )
    annotation_id = cur.lastrowid
    cur = con.execute(
        """
        INSERT INTO locations (annotation_id, version_id, status, score, candidates)
        VALUES (?, ?, 'ambiguous', 0, '[]')
        """,
        (annotation_id, version_id),
    )
    return cur.lastrowid


def _insert_confirmation(con, location_id, start, end, note=None):
    cur = con.execute(
        'INSERT INTO confirmations (location_id, start, "end", note) VALUES (?, ?, ?, ?)',
        (location_id, start, end, note),
    )
    return cur.lastrowid


def _indexes(con, table="confirmations"):
    return {
        row["name"]: row for row in con.execute(f"PRAGMA index_list('{table}')")
    }


def test_fresh_db_has_unique_index(tmp_path, monkeypatch):
    """全新库：init_db 建立唯一索引；同一 location 的第二条确认被拒绝。"""
    _use_db(tmp_path, monkeypatch)
    db.init_db()
    con = db.connect()
    try:
        indexes = _indexes(con)
        assert indexes["idx_confirm_loc_unique"]["unique"] == 1
        # 唯一索引同时承担原非唯一索引的查询路径，旧索引不再保留
        assert "idx_confirm_loc" not in indexes

        location_id = _seed_chain(con)
        _insert_confirmation(con, location_id, 0, 1)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_confirmation(con, location_id, 2, 3)
        # 首条确认未被覆盖
        rows = con.execute(
            "SELECT * FROM confirmations WHERE location_id = ?", (location_id,)
        ).fetchall()
        assert [(r["start"], r["end"]) for r in rows] == [(0, 1)]
    finally:
        con.close()


def test_existing_db_without_duplicates_migrates(tmp_path, monkeypatch):
    """无重复的既有库：平滑建立唯一约束，已有确认行原样保留。"""
    path = _use_db(tmp_path, monkeypatch)
    _build_old_style_db(path)
    con = db.connect()
    try:
        location_id = _seed_chain(con)
        first_id = _insert_confirmation(con, location_id, 4, 6, note="历史确认")
    finally:
        con.close()

    db.init_db()  # 平滑迁移，不报错

    con = db.connect()
    try:
        indexes = _indexes(con)
        assert indexes["idx_confirm_loc_unique"]["unique"] == 1
        assert "idx_confirm_loc" not in indexes
        rows = con.execute(
            "SELECT * FROM confirmations WHERE location_id = ?", (location_id,)
        ).fetchall()
        assert [(r["id"], r["start"], r["end"], r["note"]) for r in rows] == [
            (first_id, 4, 6, "历史确认")
        ]
        # 迁移后唯一约束生效
        with pytest.raises(sqlite3.IntegrityError):
            _insert_confirmation(con, location_id, 0, 1)
    finally:
        con.close()


def test_existing_db_with_duplicates_refuses_and_reports(tmp_path, monkeypatch):
    """含历史重复的既有库：不静默删改，报出可定位的 location 与确认行。"""
    path = _use_db(tmp_path, monkeypatch)
    _build_old_style_db(path)
    con = db.connect()
    try:
        location_id = _seed_chain(con)
        first_id = _insert_confirmation(con, location_id, 0, 1)
        second_id = _insert_confirmation(con, location_id, 2, 3)
    finally:
        con.close()

    with pytest.raises(migrations.MigrationError) as excinfo:
        db.init_db()
    message = str(excinfo.value)
    assert f"location_id={location_id}" in message
    assert str(first_id) in message
    assert str(second_id) in message

    # 不静默删改：两条重复行原样保留，唯一索引未建立
    con = db.connect()
    try:
        rows = con.execute(
            "SELECT * FROM confirmations WHERE location_id = ? ORDER BY id",
            (location_id,),
        ).fetchall()
        assert [r["id"] for r in rows] == [first_id, second_id]
        assert "idx_confirm_loc_unique" not in _indexes(con)

        # 预检失败同样可审计：v2 留 failed 行（错误文本可定位），无任何 applied
        audit = migrations.read_audit(con)
        assert not [r for r in audit if r["status"] == "applied"]
        failed = [r for r in audit if r["status"] == "failed"]
        assert len(failed) == 1 and failed[0]["version"] == 2
        assert f"location_id={location_id}" in failed[0]["error"]
    finally:
        con.close()
