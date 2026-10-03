"""并发回归：POST /documents/{id}/annotations 与 POST /documents/{id}/versions 交错。

修复前，批注接口在写事务之外读取并校验最新版：另一请求若在校验与写入之间
提交新版，批注仍挂到过期版本，而新版本没有该批注的迁移记录，破坏
「每条批注从创建版到末版每版恰有一条迁移记录」的完整性。

修复后，最新版读取、锚点校验与批注 / 初始迁移记录写入在同一个
BEGIN IMMEDIATE 写事务中完成：以取得写锁后的真实最新版为准——
选区仍匹配则绑定该版；不匹配则返回 422，且不留下批注或位置。

本文件用 monkeypatch 钩子 + threading.Event 做可控交错，确定性覆盖三种时序，
并以归档离线核验（其语义检查即包含上述逐版迁移记录不变量）收尾。
"""
import threading

import pytest
from fastapi.testclient import TestClient

from app import db, main
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


V1 = "第一段。目标句。结尾。"
SEL = "目标句"
START = V1.index(SEL)
# 尾部追加：锚点在新版中仍匹配；改写锚点处文本：不再匹配
V2_COMPATIBLE = V1 + "补充。"
V2_BREAKING = "第一段。已改写。结尾。"
PAYLOAD = {
    "body": "并发批注",
    "selection": SEL,
    "left_context": "第一段。",
    "right_context": "。",
    "start": START,
    "end": START + len(SEL),
}


def _make_doc(client, content=V1):
    r = client.post("/documents", json={"title": "并发文档", "content": content})
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


def _post_annotation(doc):
    # 独立线程使用独立 TestClient（惰性 portal，不重复跑 lifespan），
    # 对应线上两个请求各自持有独立连接的场景
    return TestClient(app).post(f"/documents/{doc}/annotations", json=PAYLOAD)


def _post_version(doc, content):
    return TestClient(app).post(f"/documents/{doc}/versions", json={"content": content})


def _assert_archive_invariant(client, doc):
    """完整性不变量：每条批注从创建版到末版，每版恰有一条迁移记录。

    直接复用归档离线核验（其语义检查的第 4 节即该校验），
    任一成功批注的逐版迁移记录必须完整。
    """
    exported = client.get(f"/documents/{doc}/archive")
    assert exported.status_code == 200, exported.text
    data = exported.json()
    r = client.post(
        "/archives/verify",
        json={"archive": data["archive"], "digest": data["digest"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["valid"] is True


class _OneShotGate:
    """一次性交错闸门：首次调用时阻塞，直到测试主线程放行；后续调用直接通过。"""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self._armed = True

    def wait_inside(self):
        if self._armed:
            self._armed = False
            self.entered.set()
            assert self.release.wait(timeout=10), "交错闸门等待超时"


def test_annotation_binds_to_real_latest_version(client, monkeypatch):
    """新版在批注取得写锁之前提交：批注以真实最新版为准绑定（选区仍匹配）。"""
    doc = _make_doc(client)
    gate = _OneShotGate()
    original = main._get_document

    def gated_get_document(con, document_id):
        # 批注接口在开启写事务之前调用：闸门保证新版先落库
        gate.wait_inside()
        return original(con, document_id)

    monkeypatch.setattr(main, "_get_document", gated_get_document)

    outcome = {}
    t = threading.Thread(target=lambda: outcome.setdefault("r", _post_annotation(doc)))
    t.start()
    try:
        assert gate.entered.wait(timeout=10)
        # 另一请求在批注开启写事务之前提交新版（尾部追加，锚点仍匹配）
        r = client.post(f"/documents/{doc}/versions", json={"content": V2_COMPATIBLE})
        assert r.status_code == 201, r.text
    finally:
        gate.release.set()
        t.join(timeout=10)

    r = outcome["r"]
    assert r.status_code == 201, r.text
    # 以取得写锁后的真实最新版（第 2 版）为准，而不是过期的第 1 版
    assert r.json()["anchor"]["version_number"] == 2
    assert r.json()["location"]["version_number"] == 2
    assert r.json()["location"]["status"] == "resolved"
    _assert_archive_invariant(client, doc)


def test_annotation_rejected_when_real_latest_breaks_anchor(client, monkeypatch):
    """新版在批注取得写锁之前提交且选区不再匹配：422，且不留批注或位置。"""
    doc = _make_doc(client)
    gate = _OneShotGate()
    original = main._get_document

    def gated_get_document(con, document_id):
        gate.wait_inside()
        return original(con, document_id)

    monkeypatch.setattr(main, "_get_document", gated_get_document)

    outcome = {}
    t = threading.Thread(target=lambda: outcome.setdefault("r", _post_annotation(doc)))
    t.start()
    try:
        assert gate.entered.wait(timeout=10)
        # 新版改写了锚点处的文本：原偏移上不再是该选区
        r = client.post(f"/documents/{doc}/versions", json={"content": V2_BREAKING})
        assert r.status_code == 201, r.text
    finally:
        gate.release.set()
        t.join(timeout=10)

    r = outcome["r"]
    assert r.status_code == 422, r.text
    # 不得留下批注或位置：接口与库两层核对
    anns = client.get(f"/documents/{doc}/annotations").json()["annotations"]
    assert anns == []
    con = db.connect()
    try:
        ann_rows = con.execute(
            "SELECT COUNT(*) AS c FROM annotations WHERE document_id = ?", (doc,)
        ).fetchone()["c"]
        loc_rows = con.execute(
            """
            SELECT COUNT(*) AS c FROM locations l
            JOIN annotations a ON a.id = l.annotation_id
            WHERE a.document_id = ?
            """,
            (doc,),
        ).fetchone()["c"]
    finally:
        con.close()
    assert ann_rows == 0
    assert loc_rows == 0
    _assert_archive_invariant(client, doc)


def test_version_commit_cannot_overtake_annotation_write_txn(client, monkeypatch):
    """批注已取得写锁：新版提交被串行化到其后，逐版迁移记录保持完整。"""
    doc = _make_doc(client)
    gate = _OneShotGate()
    original = main._latest_version

    def gated_latest_version(con, document_id):
        row = original(con, document_id)
        # 此时批注请求已持有写锁（BEGIN IMMEDIATE 之后、写入之前）
        gate.wait_inside()
        return row

    monkeypatch.setattr(main, "_latest_version", gated_latest_version)

    ann_outcome = {}
    t_ann = threading.Thread(
        target=lambda: ann_outcome.setdefault("r", _post_annotation(doc))
    )
    t_ann.start()
    assert gate.entered.wait(timeout=10)

    ver_outcome = {}
    t_ver = threading.Thread(
        target=lambda: ver_outcome.setdefault("r", _post_version(doc, V2_COMPATIBLE))
    )
    t_ver.start()
    try:
        # 写锁被批注请求持有：版本提交无法插队完成
        t_ver.join(timeout=1)
        assert t_ver.is_alive(), "版本提交不应在批注写事务持锁期间完成"
    finally:
        gate.release.set()
        t_ann.join(timeout=10)
        t_ver.join(timeout=10)

    r = ann_outcome["r"]
    assert r.status_code == 201, r.text
    # 批注持锁时新版尚未存在：绑定第 1 版
    assert r.json()["anchor"]["version_number"] == 1
    assert ver_outcome["r"].status_code == 201, ver_outcome["r"].text

    # 随后的第 2 版提交为该批注补齐迁移记录：每版恰有一条
    ann_id = r.json()["annotation_id"]
    hist = client.get(f"/documents/{doc}/annotations/{ann_id}/history").json()[
        "history"
    ]
    assert [h["version_number"] for h in hist] == [1, 2]
    assert all(h["status"] == "resolved" for h in hist)
    _assert_archive_invariant(client, doc)
