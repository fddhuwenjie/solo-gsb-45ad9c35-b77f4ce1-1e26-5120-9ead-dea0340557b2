"""并发回归：POST /documents/{id}/annotations/{aid}/confirmations 并发确认同一歧义记录。

修复前，「是否已确认」检查与候选校验发生在 BEGIN IMMEDIATE 之前，且
confirmations.location_id 无唯一约束：两个请求可分别通过检查、各自选定
不同候选并都返回 201，confirmations 表留下两条记录，后续查询只取其中一条。

修复后，目标记录读取、状态 / 顺延 / 既有确认检查、候选校验与插入在同一个
BEGIN IMMEDIATE 写事务中完成，且 location_id 上有唯一索引兜底：
竞争失败者稳定返回 409，不覆盖首条确认、不留重复行。

本文件用 monkeypatch 钩子 + threading.Event 做可控交错：把先到请求阻塞在
「写事务内、既有确认检查」处（此时它已持有写锁），确认后到请求被串行化在
锁外后再放行——确定性覆盖「不同候选并发」与「相同候选并发」两种时序，
并以归档离线核验收尾。
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


V1 = "开头。说：目标。结尾。"
SEL = "目标"
# 两处落点的左右上下文完全相同 → 并列最高分，ambiguous
V2 = "说：目标。说：目标。"


def _make_ambiguous(client):
    """建文档 + 批注 + 触发歧义的第 2 版，返回 (doc_id, ann_id, candidates)。"""
    r = client.post("/documents", json={"title": "并发确认", "content": V1})
    assert r.status_code == 201, r.text
    doc = r.json()["document_id"]
    start = V1.index(SEL)
    r = client.post(
        f"/documents/{doc}/annotations",
        json={
            "body": "并发确认批注",
            "selection": SEL,
            "left_context": "说：",
            "right_context": "。",
            "start": start,
            "end": start + len(SEL),
        },
    )
    assert r.status_code == 201, r.text
    ann = r.json()["annotation_id"]
    r = client.post(f"/documents/{doc}/versions", json={"content": V2})
    assert r.status_code == 201, r.text
    result = r.json()["results"][0]
    assert result["status"] == "ambiguous"
    assert len(result["candidates"]) == 2
    return doc, ann, result["candidates"]


def _post_confirmation(doc, ann, candidate, note):
    # 独立线程使用独立 TestClient（惰性 portal，不重复跑 lifespan），
    # 对应线上两个请求各自持有独立连接的场景
    return TestClient(app).post(
        f"/documents/{doc}/annotations/{ann}/confirmations",
        json={
            "version_number": 2,
            "start": candidate["start"],
            "end": candidate["end"],
            "note": note,
        },
    )


def _confirmation_rows(location_id):
    con = db.connect()
    try:
        return con.execute(
            "SELECT * FROM confirmations WHERE location_id = ? ORDER BY id",
            (location_id,),
        ).fetchall()
    finally:
        con.close()


def _assert_archive_consistent(client, doc):
    """归档离线核验：确认与迁移记录、候选落点的引用一致性全部通过。"""
    exported = client.get(f"/documents/{doc}/archive")
    assert exported.status_code == 200, exported.text
    data = exported.json()
    r = client.post(
        "/archives/verify",
        json={"archive": data["archive"], "digest": data["digest"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["valid"] is True
    assert r.json()["counts"]["confirmations"] == 1


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


def _run_concurrent_confirmations(client, monkeypatch, first_pick, second_pick):
    """可控交错地并发确认同一歧义记录，返回 (先到响应, 后到响应, doc, ann, candidates)。

    先到请求被闸门阻塞在写事务内的既有确认检查处（已持有写锁）；
    后到请求此时发起，被串行化在写锁之外；放行后先到者完成插入并提交，
    后到者取得写锁后重新读取到已存在的确认。
    """
    doc, ann, candidates = _make_ambiguous(client)
    gate = _OneShotGate()
    original = main._confirmation_of

    def gated_confirmation_of(con, location_id):
        # 修复后该检查在 BEGIN IMMEDIATE 写事务内执行：
        # 先到请求此时已持有写锁，后到请求无法插队
        gate.wait_inside()
        return original(con, location_id)

    monkeypatch.setattr(main, "_confirmation_of", gated_confirmation_of)

    first_outcome = {}
    t_first = threading.Thread(
        target=lambda: first_outcome.setdefault(
            "r", _post_confirmation(doc, ann, first_pick(candidates), "先到")
        )
    )
    t_first.start()
    assert gate.entered.wait(timeout=10)

    second_outcome = {}
    t_second = threading.Thread(
        target=lambda: second_outcome.setdefault(
            "r", _post_confirmation(doc, ann, second_pick(candidates), "后到")
        )
    )
    t_second.start()
    try:
        # 写锁被先到请求持有：后到请求无法插队完成
        t_second.join(timeout=1)
        assert t_second.is_alive(), "后到请求不应在先到请求持锁期间完成"
    finally:
        gate.release.set()
        t_first.join(timeout=10)
        t_second.join(timeout=10)

    return first_outcome["r"], second_outcome["r"], doc, ann, candidates


def _assert_single_confirmation_kept(client, doc, ann, first, second, winner):
    """恰一条 201、一条 409；库中恰有首条确认且未被覆盖；后续查询一致。"""
    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text

    loc = first.json()["location"]
    assert loc["status"] == "confirmed"
    assert loc["start"] == winner["start"]
    assert loc["end"] == winner["end"]
    assert loc["confirmation"]["note"] == "先到"

    # 数据库层：该 location 恰有一条确认，且是先到者的选择（未被覆盖、无重复行）
    rows = _confirmation_rows(loc["location_id"])
    assert len(rows) == 1
    assert (rows[0]["start"], rows[0]["end"], rows[0]["note"]) == (
        winner["start"],
        winner["end"],
        "先到",
    )

    # 后续查询一致地看到同一条确认
    detail = client.get(f"/documents/{doc}/annotations/{ann}").json()
    assert detail["latest"]["status"] == "confirmed"
    assert detail["latest"]["start"] == winner["start"]
    assert detail["latest"]["confirmation"]["note"] == "先到"
    hist = client.get(f"/documents/{doc}/annotations/{ann}/history").json()["history"]
    confirmed = [h for h in hist if h["status"] == "confirmed"]
    assert len(confirmed) == 1
    assert confirmed[0]["confirmation"]["note"] == "先到"

    _assert_archive_consistent(client, doc)


def test_concurrent_confirmations_different_candidates(client, monkeypatch):
    """两个请求选定不同候选：恰一个 201，另一个 409；只保留首条确认。"""
    first, second, doc, ann, candidates = _run_concurrent_confirmations(
        client,
        monkeypatch,
        first_pick=lambda cs: cs[0],
        second_pick=lambda cs: cs[1],
    )
    _assert_single_confirmation_kept(client, doc, ann, first, second, candidates[0])


def test_concurrent_confirmations_same_candidate(client, monkeypatch):
    """两个请求选定同一候选：同样恰一个 201、一个 409，无重复行。"""
    first, second, doc, ann, candidates = _run_concurrent_confirmations(
        client,
        monkeypatch,
        first_pick=lambda cs: cs[0],
        second_pick=lambda cs: cs[0],
    )
    _assert_single_confirmation_kept(client, doc, ann, first, second, candidates[0])
