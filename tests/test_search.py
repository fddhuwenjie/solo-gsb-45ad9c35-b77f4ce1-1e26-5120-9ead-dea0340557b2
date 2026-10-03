"""版本范围字面子串检索 API 与游标快照测试。"""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import search as sm


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _make_doc(client, content, title="检索文档"):
    r = client.post("/documents", json={"title": title, "content": content})
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


def _ann(client, doc, text, selection, start=None, end=None, body="批注",
         left="", right=""):
    if start is None:
        start = text.index(selection)
    if end is None:
        end = start + len(selection)
    r = client.post(
        f"/documents/{doc}/annotations",
        json={
            "body": body,
            "selection": selection,
            "left_context": left,
            "right_context": right,
            "start": start,
            "end": end,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["annotation_id"]


def _version(client, doc, content):
    r = client.post(f"/documents/{doc}/versions", json={"content": content})
    assert r.status_code == 201, r.text
    return r.json()["version"]["version_number"]


def _search(client, doc, **body):
    return client.post(f"/documents/{doc}/search", json=body)


def _drain(client, doc, body, expect_pages):
    """逐页取尽，返回 (全部命中, 各页 total_in_snapshot)。"""
    all_hits = []
    totals = []
    cursor = None
    rounds = 0
    while True:
        req = dict(body)
        if cursor is not None:
            req["cursor"] = cursor
        r = _search(client, doc, **req)
        assert r.status_code == 200, r.text
        j = r.json()
        all_hits.extend(j["hits"])
        totals.append(j["total_in_snapshot"])
        rounds += 1
        if j["next_cursor"]:
            assert j["resume_cursor"] is None
            cursor = j["next_cursor"]
            assert rounds <= expect_pages
            continue
        assert j["has_more"] is False
        return all_hits, totals, j["resume_cursor"]


# ---------------------------------------------------------------------------
# 匹配与命中内容
# ---------------------------------------------------------------------------


def test_overlapping_literal_hits_preserved(client):
    text = "aaaaa😀"
    doc = _make_doc(client, text)
    r = _search(client, doc, query="aa", from_version=1)
    assert r.status_code == 200, r.text
    j = r.json()
    # "aaaaa" 上 "aa" 的重叠起点全部保留：0..3
    assert [(h["start"], h["end"]) for h in j["hits"]] == [
        (0, 2), (1, 3), (2, 4), (3, 5)
    ]
    assert all(h["version_number"] == 1 for h in j["hits"])
    # 命中文本即 query，半开范围长度等于码点长度
    assert all(h["context"]["match"] == "aa" for h in j["hits"])


def test_emoji_codepoint_offsets(client):
    text = "ab😀😎cd"
    doc = _make_doc(client, text)
    r = _search(client, doc, query="😀", from_version=1)
    j = r.json()
    assert [(h["start"], h["end"]) for h in j["hits"]] == [(2, 3)]
    assert j["hits"][0]["context"]["match"] == "😀"

    r = _search(client, doc, query="😀😎", from_version=1)
    j = r.json()
    assert [(h["start"], h["end"]) for h in j["hits"]] == [(2, 4)]
    # 片段按码点切片：emoji 不被截开
    assert j["hits"][0]["context"]["snippet"] == text


def test_search_version_range_and_sort_order(client):
    v1 = "abab"
    doc = _make_doc(client, v1)
    v2 = "ab"
    _version(client, doc, v2)
    v3 = "ababab"
    _version(client, doc, v3)

    # 区间 [2,3]：第 1 版的命中不出现；按 (版本号, 起点, 终点) 排序
    r = _search(client, doc, query="ab", from_version=2, to_version=3)
    j = r.json()
    keys = [(h["version_number"], h["start"], h["end"]) for h in j["hits"]]
    assert keys == [
        (2, 0, 2),
        (3, 0, 2), (3, 2, 4), (3, 4, 6),
    ]
    assert keys == sorted(keys)
    assert j["from_version"] == 2 and j["to_version"] == 3

    # 缺省 to_version 取最新版
    r = _search(client, doc, query="ab", from_version=1)
    assert r.status_code == 200
    assert r.json()["to_version"] == 3


def test_context_snippet_reproducible(client):
    text = "0123456789ABCDEF目标XYZ"
    doc = _make_doc(client, text)
    start = text.index("目标")
    r = _search(
        client, doc, query="目标", from_version=1, context_radius=4
    )
    h = r.json()["hits"][0]
    ctx = h["context"]
    # before/after 各至多 4 个码点，拼回即 snippet，相对偏移可复原高亮
    assert ctx["before"] == "CDEF"
    assert ctx["after"] == "XYZ"
    assert ctx["snippet"] == "CDEF目标XYZ"
    assert (
        ctx["snippet"][
            ctx["match_start_in_snippet"]:ctx["match_end_in_snippet"]
        ]
        == "目标"
    )
    assert ctx["snippet_start"] == start - 4
    assert ctx["snippet_end"] == start + 2 + 3
    assert ctx["truncated_left"] is True
    assert ctx["truncated_right"] is False  # 右侧只剩 3 个码点，触文本边界

    # radius=0：片段就是命中文本
    r = _search(client, doc, query="目标", from_version=1, context_radius=0)
    ctx = r.json()["hits"][0]["context"]
    assert ctx["snippet"] == "目标"
    assert ctx["before"] == "" and ctx["after"] == ""
    assert ctx["match_start_in_snippet"] == 0


def test_no_match(client):
    doc = _make_doc(client, "没有目标文字")
    r = _search(client, doc, query="缺失", from_version=1)
    j = r.json()
    assert j["hits"] == []
    assert j["hit_count"] == 0
    assert j["total_in_snapshot"] == 0
    assert j["has_more"] is False
    # 空轮也签发续拉游标
    assert j["next_cursor"] is None and j["resume_cursor"]


def test_request_validation(client):
    doc = _make_doc(client, "abc")
    # 空 query
    r = _search(client, doc, query="", from_version=1)
    assert r.status_code == 422
    # from > to
    r = _search(client, doc, query="a", from_version=2, to_version=1)
    assert r.status_code == 422
    # 超出最新版本
    r = _search(client, doc, query="a", from_version=9)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "version_out_of_range"
    # radius 越界
    r = _search(client, doc, query="a", from_version=1, context_radius=81)
    assert r.status_code == 422


def test_document_not_found(client):
    r = client.post(
        "/documents/999999/search",
        json={"query": "a", "from_version": 1},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 相交批注状态
# ---------------------------------------------------------------------------


def _setup_statuses(client):
    """三版：resolved / ambiguous(+confirmed) / unresolved+carried。"""
    v1 = "xab"
    doc = _make_doc(client, v1)
    # 批注 A：v1 上 [1,3) resolved；v2 两处相同左上下文 → ambiguous
    ann = _ann(client, doc, v1, "ab", start=1, left="x")
    _version(client, doc, "xabxab")        # v2：候选 [1,3) 与 [4,6)
    _version(client, doc, "zzz")          # v3：选区消失 → unresolved
    return doc, ann


def test_resolved_ambiguous_unresolved_intersections(client):
    doc, ann = _setup_statuses(client)
    r = _search(client, doc, query="ab", from_version=1, to_version=3)
    all_hits = r.json()["hits"]
    by_version = {h["version_number"]: h for h in all_hits}

    # v1 resolved：返回自带范围
    a1 = by_version[1]["annotations"]
    assert a1 == [
        {"annotation_id": ann, "status": "resolved", "start": 1, "end": 3}
    ]

    # v2 ambiguous：每个命中只返回与它相交的候选，不挑选、不扩大到整批候选
    v2_hits = [h for h in all_hits if h["version_number"] == 2]
    by_start = {h["start"]: h for h in v2_hits}
    assert by_start[1]["annotations"] == [
        {
            "annotation_id": ann,
            "status": "ambiguous",
            "candidates": [{"start": 1, "end": 3}],
        }
    ]
    assert by_start[4]["annotations"] == [
        {
            "annotation_id": ann,
            "status": "ambiguous",
            "candidates": [{"start": 4, "end": 6}],
        }
    ]

    # v3 unresolved：没有范围，不出现在任何命中上（本版也没有 ab 命中）
    assert 3 not in {h["version_number"] for h in r.json()["hits"]}


def test_ambiguous_reports_only_intersecting_candidates(client):
    doc, ann = _setup_statuses(client)
    # "xabxab" 中 "a" 出现在 1、4（两处候选起点），无候选外的命中
    r = _search(client, doc, query="a", from_version=2, to_version=2)
    by_start = {h["start"]: h for h in r.json()["hits"]}
    assert set(by_start) == {1, 4}
    assert by_start[1]["annotations"] == [
        {"annotation_id": ann, "status": "ambiguous",
         "candidates": [{"start": 1, "end": 3}]}
    ]
    assert by_start[4]["annotations"] == [
        {"annotation_id": ann, "status": "ambiguous",
         "candidates": [{"start": 4, "end": 6}]}
    ]

    # 半开相交补充：查 "b"（位置 2、5），与候选 [1,3)/[4,6) 各真相交
    r = _search(client, doc, query="b", from_version=2, to_version=2)
    by_start = {h["start"]: h for h in r.json()["hits"]}
    assert set(by_start) == {2, 5}
    assert by_start[2]["annotations"][0]["status"] == "ambiguous"


def test_confirmed_range_takes_priority(client):
    doc, ann = _setup_statuses(client)
    # 人工确认 v2 的候选 [1,3)
    r = client.post(
        f"/documents/{doc}/annotations/{ann}/confirmations",
        json={"version_number": 2, "start": 1, "end": 3},
    )
    assert r.status_code == 201, r.text

    r = _search(client, doc, query="ab", from_version=2, to_version=2)
    # 候选 [4,6) 上的命中不再标 ambiguous；[1,3) 命中按确认范围标 confirmed
    by_start = {h["start"]: h for h in r.json()["hits"]}
    assert by_start[1]["annotations"] == [
        {"annotation_id": ann, "status": "confirmed", "start": 1, "end": 3}
    ]
    assert by_start[4]["annotations"] == []


def test_carried_ambiguous_has_no_range(client):
    # 歧义未确认即顺延：v2 ambiguous → v3 顺延占位（占位记录没有任何范围）
    v1 = "xab"
    doc = _make_doc(client, v1)
    _ann(client, doc, v1, "ab", start=1, left="x")
    _version(client, doc, "xabxab")  # v2：两处候选 → ambiguous
    _version(client, doc, "xabxab")  # v3：顺延占位
    r = _search(client, doc, query="ab", from_version=3, to_version=3)
    assert {h["start"] for h in r.json()["hits"]} == {1, 4}
    assert all(h["annotations"] == [] for h in r.json()["hits"])


def test_half_open_range_endpoint_touch_not_intersecting(client):
    text = "目标。后缀"
    doc = _make_doc(client, text)
    _ann(client, doc, text, "目标", start=0, end=2)
    # "。" 位于 [2,3)：与 [0,2) 仅端点相接（半开），不相交
    r = _search(client, doc, query="。", from_version=1)
    assert r.json()["hits"][0]["annotations"] == []
    # "标" 位于 [1,2)：与 [0,2) 真相交
    r = _search(client, doc, query="标", from_version=1)
    assert r.json()["hits"][0]["annotations"][0]["status"] == "resolved"


# ---------------------------------------------------------------------------
# 分页与快照冻结
# ---------------------------------------------------------------------------


def test_pagination_over_100_hits(client):
    # 每版 60 个重叠 "aa" 命中（"a"*61），两版共 120
    doc = _make_doc(client, "a" * 61)
    _version(client, doc, "a" * 61)

    body = {"query": "aa", "from_version": 1}
    hits, totals, resume = _drain(client, doc, body, expect_pages=2)
    assert len(hits) == 120
    assert totals == [120, 120]  # 冻结快照下 total 稳定
    keys = [(h["version_number"], h["start"], h["end"]) for h in hits]
    assert keys == sorted(keys)
    assert len(set(keys)) == 120  # 不重不漏
    assert resume  # 末页返回 resume_cursor


def test_concurrent_version_inserted_during_pagination_deferred(client):
    # "a"*71 有 70 个重叠 "aa" 命中；两版 140 > 100 保证分页
    doc = _make_doc(client, "a" * 71)
    _version(client, doc, "a" * 71)

    body = {"query": "aa", "from_version": 1}
    r1 = _search(client, doc, **body)
    j1 = r1.json()
    assert j1["hit_count"] == 100 and j1["has_more"] is True
    assert j1["total_in_snapshot"] == 140
    frozen_wm = dict(j1["snapshot"]["watermark"])
    assert j1["snapshot"]["latest_version"] == 2

    # 分页期间并发提交第 3 版（又 70 个命中）
    _version(client, doc, "a" * 71)

    # 续页沿用冻结快照：仍只有 140，不含 v3
    r2 = _search(client, doc, **body, cursor=j1["next_cursor"])
    assert r2.status_code == 200, r2.text
    j2 = r2.json()
    assert j2["hit_count"] == 40
    assert j2["total_in_snapshot"] == 140
    assert j2["snapshot"]["latest_version"] == 2
    assert j2["snapshot"]["watermark"] == frozen_wm
    assert all(h["version_number"] <= 2 for h in j2["hits"])
    assert j2["next_cursor"] is None and j2["resume_cursor"]

    # 续拉开新快照：v3 进入结果（共 210；首页 100 条仍只覆盖 v1/v2）
    r3 = _search(client, doc, **body, cursor=j2["resume_cursor"])
    assert r3.status_code == 200, r3.text
    j3 = r3.json()
    assert j3["snapshot"]["latest_version"] == 3
    assert j3["total_in_snapshot"] == 210
    assert all(h["version_number"] <= 2 for h in j3["hits"])
    assert j3["has_more"] is True

    # 沿新快照的续页取尽：v1/v2 各 70 占满新快照前 140，v3 的 70 个命中
    # 在后续页全部出现；新快照整体不重不漏共 210。
    new_hits = list(j3["hits"])
    token = j3["next_cursor"]
    pages = 1
    while token is not None:
        jp = _search(client, doc, **body, cursor=token).json()
        new_hits.extend(jp["hits"])
        pages += 1
        token = jp["next_cursor"]
        if token is None:
            assert jp["resume_cursor"]
    assert pages == 3
    assert len(new_hits) == 210
    keys = [(h["version_number"], h["start"], h["end"]) for h in new_hits]
    assert keys == sorted(keys) and len(set(keys)) == 210
    assert len([k for k in keys if k[0] == 3]) == 70


def test_concurrent_confirmation_deferred_then_visible(client):
    # 需要分页的文档（两版各 70 个 "aa" 命中），v1 上挂一条覆盖部分命中的批注
    doc = _make_doc(client, "a" * 71)
    ann = _ann(client, doc, "a" * 71, "aa", start=0, end=2)
    _version(client, doc, "a" * 71)  # v2：选区大量落点且无上下文 → ambiguous
    body = {"query": "aa", "from_version": 1}
    r1 = _search(client, doc, **body)
    j1 = r1.json()
    assert j1["has_more"] is True
    # 首条命中（v1 [0,2)）在冻结快照中是 resolved
    first = j1["hits"][0]
    assert first["version_number"] == 1 and first["start"] == 0
    assert first["annotations"] == [
        {"annotation_id": ann, "status": "resolved", "start": 0, "end": 2}
    ]

    # 分页期间并发确认 v2 的某条歧义候选
    v2_candidates = client.get(f"/documents/{doc}/annotations/{ann}/history").json()
    v2_loc = next(h for h in v2_candidates["history"] if h["version_number"] == 2)
    chosen = v2_loc["candidates"][0]
    r = client.post(
        f"/documents/{doc}/annotations/{ann}/confirmations",
        json={"version_number": 2, "start": chosen["start"], "end": chosen["end"]},
    )
    assert r.status_code == 201, r.text

    # 续页沿用冻结快照：v2 命中仍标 ambiguous（新增确认留到下一轮）
    r2 = _search(client, doc, **body, cursor=j1["next_cursor"])
    assert r2.status_code == 200, r2.text
    j2 = r2.json()
    v2_page_hits = [h for h in j2["hits"] if h["version_number"] == 2]
    assert v2_page_hits, "续页应包含 v2 的命中"
    assert all(
        all(a["status"] == "ambiguous" for a in h["annotations"])
        for h in v2_page_hits
    )
    resume = j2["resume_cursor"]

    # 续拉开新快照：确认生效，相交命中按确认范围标 confirmed
    r3 = _search(client, doc, **body, cursor=resume)
    assert r3.status_code == 200, r3.text
    found = [
        a
        for h in r3.json()["hits"]
        for a in h["annotations"]
        if a["status"] == "confirmed"
    ]
    assert found and all(a["annotation_id"] == ann for a in found)
    assert (found[0]["start"], found[0]["end"]) == (chosen["start"], chosen["end"])


def test_resume_cursor_empty_round_advances(client):
    doc = _make_doc(client, "静态文本没有目标词")
    r1 = _search(client, doc, query="缺失", from_version=1)
    j1 = r1.json()
    assert j1["hits"] == [] and j1["resume_cursor"]
    wm1 = j1["snapshot"]["watermark"]

    # 无新增的空轮：重新固定快照（水位相同），仍签发新的 resume_cursor
    # （水位与筛选均未变时令牌字节一致——它编码的就是冻结参数 + 水位；
    # 语义上每次调用都重新固定了快照，客户端可原样继续轮询）
    r2 = _search(
        client, doc, query="缺失", from_version=1, cursor=j1["resume_cursor"]
    )
    assert r2.status_code == 200
    j2 = r2.json()
    assert j2["hits"] == [] and j2["resume_cursor"]
    assert j2["snapshot"]["watermark"] == wm1

    # 空轮后出现新版本（含目标词）：同一 resume 游标续拉即取到新增命中
    _version(client, doc, "静态文本没有目标词，但是这里有缺失二字")
    r3 = _search(
        client, doc, query="缺失", from_version=1, cursor=j2["resume_cursor"]
    )
    assert r3.status_code == 200, r3.text
    j3 = r3.json()
    assert j3["snapshot"]["latest_version"] == 2
    assert [h["version_number"] for h in j3["hits"]] == [2]


# ---------------------------------------------------------------------------
# 游标安全：篡改 / 跨文档 / 筛选不一致 / 结构损坏
# ---------------------------------------------------------------------------


def _b64url_json(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_cursor_tampered_signature_mismatch(client):
    doc = _make_doc(client, "a" * 61)
    _version(client, doc, "a" * 61)  # 两版共 120 命中 → 产生 next_cursor
    j = _search(client, doc, query="aa", from_version=1).json()
    token = j["next_cursor"]
    # 翻转载荷末位（重编码保持合法 Base64/JSON 结构）
    pad = token + "=" * (-len(token) % 4)
    envelope = json.loads(base64.urlsafe_b64decode(pad))
    envelope["d"]["after"][1] += 1
    raw = json.dumps(envelope, separators=(",", ":")).encode()
    tampered = _b64url_json(raw)
    r = _search(client, doc, query="aa", from_version=1, cursor=tampered)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "cursor_tampered"


def test_cursor_garbage_returns_structure_error(client):
    doc = _make_doc(client, "abc")
    for bad in ["not-base64!!!", "eyJhIjoxfQ", ""]:  # 末项为空触发 schema 422
        r = _search(client, doc, query="a", from_version=1, cursor=bad)
        assert r.status_code == 422, (bad, r.text)
    # 合法 base64 但内容不是信封
    r = _search(
        client, doc, query="a", from_version=1, cursor=_b64url_json(b"{}")
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "bad_cursor_structure"


def test_cursor_cross_document(client):
    doc_a = _make_doc(client, "a" * 61, title="A")
    _version(client, doc_a, "a" * 61)
    doc_b = _make_doc(client, "a" * 61, title="B")
    _version(client, doc_b, "a" * 61)
    token = _search(client, doc_a, query="aa", from_version=1).json()["next_cursor"]
    r = _search(client, doc_b, query="aa", from_version=1, cursor=token)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "cross_document_cursor"


def test_cursor_filter_mismatch(client):
    doc = _make_doc(client, "a" * 61)
    _version(client, doc, "a" * 61)
    token = _search(client, doc, query="aa", from_version=1).json()["next_cursor"]

    # query 被替换
    r = _search(client, doc, query="a", from_version=1, cursor=token)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "filter_mismatch"
    assert "query" in r.json()["detail"]["fields"]

    # 同 query 但上下文半径不同
    r = _search(
        client, doc, query="aa", from_version=1, context_radius=3, cursor=token
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "filter_mismatch"
    assert "context_radius" in r.json()["detail"]["fields"]

    # from_version 不同
    r = _search(client, doc, query="aa", from_version=1, to_version=9, cursor=token)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "filter_mismatch"

    # 一致则正常续页
    r = _search(client, doc, query="aa", from_version=1, cursor=token)
    assert r.status_code == 200, r.text


def test_cursor_from_other_database_range_error(client):
    doc = _make_doc(client, "abc")
    # 手工签发一个水位远超当前库的游标
    from app import db
    con = db.connect()
    try:
        with db.read_transaction(con):
            secret = sm.get_secret(con)
        token = sm.make_page_cursor(
            doc, "a", 1, 1, 16,
            {"versions": 10 ** 9, "annotations": 0, "locations": 0,
             "confirmations": 0},
            (1, 0, 1), secret,
        )
    finally:
        con.close()
    r = _search(client, doc, query="a", from_version=1, to_version=1, cursor=token)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "bad_cursor_range"


def test_unsupported_cursor_version(client):
    doc = _make_doc(client, "abc")
    con_token = base64.urlsafe_b64encode(
        json.dumps({"v": 99, "d": {}, "s": "x"}, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    r = _search(client, doc, query="a", from_version=1, cursor=con_token)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "bad_cursor_version"


# ---------------------------------------------------------------------------
# 模块级纯函数
# ---------------------------------------------------------------------------


def test_find_literal_hits_overlap():
    assert sm.find_literal_hits("aaaa", "aa") == [0, 1, 2]
    assert sm.find_literal_hits("aba", "a") == [0, 2]
    assert sm.find_literal_hits("abc", "z") == []
    assert sm.find_literal_hits("😀😀", "😀") == [0, 1]


def test_intersecting_statuses():
    effective = {
        1: {"status": "resolved", "ranges": [(2, 5)]},
        2: {"status": "confirmed", "ranges": [(10, 12)]},
        3: {"status": "ambiguous", "ranges": [(0, 2), (8, 9)]},
        4: {"status": "resolved", "ranges": [(100, 101)]},
    }
    # 命中 [4, 9)：与 1 相交（4<5）、与 3 的第二候选相交、与 2 不相交
    items = sm._intersecting(effective, 4, 9)
    assert [i["annotation_id"] for i in items] == [1, 3]
    assert items[0] == {"annotation_id": 1, "status": "resolved",
                       "start": 2, "end": 5}
    assert items[1] == {"annotation_id": 3, "status": "ambiguous",
                       "candidates": [{"start": 8, "end": 9}]}
    # 完全不相交
    assert sm._intersecting(effective, 50, 60) == []


def test_cursor_roundtrip_and_secret_persists():
    from app import db
    con = db.connect()
    try:
        with db.read_transaction(con):
            secret = sm.get_secret(con)
        token = sm.make_page_cursor(
            7, "q", 1, 3, 8,
            {"versions": 5, "annotations": 4, "locations": 9,
             "confirmations": 2},
            (2, 11, 12), secret,
        )
        parsed = sm.decode_cursor(token, secret)
        assert parsed["kind"] == "page"
        assert parsed["doc"] == 7 and parsed["after"] == (2, 11, 12)
        assert parsed["wm"]["locations"] == 9
    finally:
        con.close()

    # 密钥存库：另起连接取到同一密钥（跨重启游标仍可验签）
    con2 = db.connect()
    try:
        with db.read_transaction(con2):
            secret2 = sm.get_secret(con2)
        assert secret2 == secret
        sm.decode_cursor(token, secret2)
    finally:
        con2.close()

    # 另一个文档的密钥（全局同一服务密钥）签发的游标跨文档归属由路由判
    with pytest.raises(sm.CursorError):
        sm.decode_cursor(token, b"0" * 64)
