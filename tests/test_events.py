"""文档增量事件流测试：首轮合并 / 续页分页 / 续拉与空轮推进 / 游标校验。"""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _make_doc(client, content, title="事件测试文档"):
    r = client.post("/documents", json={"title": title, "content": content})
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


def _make_ann(client, doc, text, selection, left="", right="", body="批注"):
    start = text.index(selection)
    r = client.post(
        f"/documents/{doc}/annotations",
        json={
            "body": body,
            "selection": selection,
            "left_context": left,
            "right_context": right,
            "start": start,
            "end": start + len(selection),
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["annotation_id"]


def _new_version(client, doc, content):
    r = client.post(f"/documents/{doc}/versions", json={"content": content})
    assert r.status_code == 201, r.text
    return r.json()


def _decode(cursor):
    """测试侧解码游标（与线格式一致：Base64URL 紧凑 JSON）。"""
    return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))


def _encode(payload):
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _get_events(client, doc, **params):
    r = client.get(f"/documents/{doc}/events", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _fetch_all_pages(client, doc, limit):
    """沿 next_cursor 翻完整轮，返回 (事件列表, 末页响应)。"""
    page = _get_events(client, doc, limit=limit)
    events = list(page["events"])
    while page["next_cursor"] is not None:
        assert page["resume_cursor"] is None  # 续页与续拉游标互斥
        page = _get_events(client, doc, limit=limit, cursor=page["next_cursor"])
        events.extend(page["events"])
    assert page["resume_cursor"] is not None  # 末页必须返回 resume_cursor
    return events, page


def _sort_key(event, type_rank):
    return (event["created_at"], type_rank[event["type"]], event["id"])


_TYPE_RANK = {
    "version": 1,
    "annotation": 2,
    "location": 3,
    "confirmation": 4,
    "dependency": 5,
    "completion": 6,
}


# ---------------------------------------------------------------------------
# 首轮：六类记录稳定合并
# ---------------------------------------------------------------------------


def test_first_round_merges_all_event_types(client):
    v1 = "第一段。第二段。"
    doc = _make_doc(client, v1)
    ann1 = _make_ann(client, doc, v1, "第一段", right="。", body="批注一")
    ann2 = _make_ann(client, doc, v1, "第二段", right="。", body="批注二")
    # v2 中“第一段”出现两次且上下文得分相同 → ambiguous
    _new_version(client, doc, "第一段。第一段。")
    r = client.post(
        f"/documents/{doc}/annotations/{ann1}/confirmations",
        json={"version_number": 2, "start": 4, "end": 7, "note": "取后一处"},
    )
    assert r.status_code == 201, r.text
    r = client.post(
        f"/documents/{doc}/dependencies",
        json={"relations": [{"prerequisite_id": ann1, "dependent_id": ann2}]},
    )
    assert r.status_code == 201, r.text
    r = client.post(f"/documents/{doc}/annotations/{ann1}/completions", json={})
    assert r.status_code == 201, r.text

    resp = _get_events(client, doc, limit=200)
    events = resp["events"]

    # 六类事件齐全：2 版本 + 2 批注 + 4 位置 + 1 确认 + 1 依赖 + 1 完成
    counts = {}
    for e in events:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    assert counts == {
        "version": 2,
        "annotation": 2,
        "location": 4,
        "confirmation": 1,
        "dependency": 1,
        "completion": 1,
    }
    assert resp["event_count"] == len(events) == 11

    # 稳定全序：(created_at, type_rank, id) 非递减
    keys = [_sort_key(e, _TYPE_RANK) for e in events]
    assert keys == sorted(keys)

    # 单页读尽：无 next_cursor，末页返回 resume_cursor
    assert resp["has_more"] is False
    assert resp["next_cursor"] is None
    assert resp["resume_cursor"] is not None

    # 窗口：首轮起点全零；resume_cursor 的已消费水位 = 本轮终点水位
    assert set(resp["window"]["from"].values()) == {0}
    assert _decode(resp["resume_cursor"]) == {
        "v": 1,
        "kind": "resume",
        "doc": doc,
        "from": resp["window"]["to"],
    }

    # 与归档 / 列表接口交叉核对：同一批行 ID，不多不少
    archive = client.get(f"/documents/{doc}/archive").json()["archive"]
    by_type = {}
    for e in events:
        by_type.setdefault(e["type"], []).append(e["id"])
    assert by_type["version"] == [v["id"] for v in archive["versions"]]
    assert by_type["annotation"] == [a["id"] for a in archive["annotations"]]
    assert by_type["location"] == [l["id"] for l in archive["locations"]]
    assert by_type["confirmation"] == [c["id"] for c in archive["confirmations"]]
    deps = client.get(f"/documents/{doc}/dependencies").json()["dependencies"]
    assert by_type["dependency"] == [d["dependency_id"] for d in deps]
    comps = client.get(f"/documents/{doc}/completions").json()["completions"]
    assert by_type["completion"] == [c["completion_id"] for c in comps]


def test_event_payloads_keep_stored_fields_only(client):
    """载荷保留落库时的稳定业务字段，不计算查询时刻的派生状态。"""
    v1 = "目标甲。目标乙。"
    doc = _make_doc(client, v1)
    ann = _make_ann(client, doc, v1, "目标甲", right="。")
    _new_version(client, doc, "目标甲。目标甲。")  # v2：歧义
    r = client.post(
        f"/documents/{doc}/annotations/{ann}/confirmations",
        json={"version_number": 2, "start": 4, "end": 7, "note": "取后者"},
    )
    assert r.status_code == 201, r.text

    events = _get_events(client, doc, limit=200)["events"]
    by_type = {}
    for e in events:
        by_type.setdefault(e["type"], []).append(e)

    # version：版本号与全文（稳定业务字段）
    v1_event = by_type["version"][0]
    assert v1_event["payload"]["version_number"] == 1
    assert v1_event["payload"]["content"] == v1
    assert v1_event["created_at"]

    # annotation：锚点原始字段（含原始码点偏移与创建版引用）
    ann_payload = by_type["annotation"][0]["payload"]
    assert ann_payload["annotation_id"] == ann
    assert ann_payload["selection"] == "目标甲"
    assert ann_payload["orig_start"] == v1.index("目标甲")
    assert ann_payload["orig_end"] == v1.index("目标甲") + 3

    # location：保留库中原始状态——歧义记录即使已确认，事件里仍是
    # ambiguous（confirmed 是查询时刻的派生状态，不出现在事件流中）
    v2_loc = by_type["location"][1]
    assert v2_loc["payload"]["status"] == "ambiguous"
    assert v2_loc["payload"]["start"] is None
    assert isinstance(v2_loc["payload"]["candidates"], list)
    assert len(v2_loc["payload"]["candidates"]) == 2
    assert v2_loc["payload"]["carried_from"] is None

    # confirmation / 其他类型：各自的稳定业务字段
    conf_payload = by_type["confirmation"][0]["payload"]
    assert conf_payload["location_id"] == v2_loc["id"]
    assert (conf_payload["start"], conf_payload["end"]) == (4, 7)
    assert conf_payload["note"] == "取后者"


def test_same_transaction_events_ordered_by_type_rank(client):
    """同一写事务产生的行 created_at 相同，按 type_rank 再按 id 定序。"""
    v1 = "甲乙丙"
    doc = _make_doc(client, v1)
    for i, ch in enumerate("甲乙丙"):
        _make_ann(client, doc, v1, ch, body=f"批注{ch}")
    _new_version(client, doc, "甲乙丙丁")  # 同事务：1 版本 + 3 位置

    events = _get_events(client, doc, limit=200)["events"]
    v2_event = next(
        e for e in events
        if e["type"] == "version" and e["payload"]["version_number"] == 2
    )
    v2_loc_events = [
        e for e in events
        if e["type"] == "location"
        and e["payload"]["version_id"] == v2_event["id"]
    ]
    assert len(v2_loc_events) == 3
    # 版本事件排在其迁移位置事件之前（created_at 相同则 type_rank 1 < 3）
    assert events.index(v2_event) < min(events.index(e) for e in v2_loc_events)
    # 同事务的位置事件按行 ID 升序
    assert [e["id"] for e in v2_loc_events] == sorted(
        e["id"] for e in v2_loc_events
    )


# ---------------------------------------------------------------------------
# 分页：next_cursor 沿同一窗口翻页
# ---------------------------------------------------------------------------


def test_pagination_walks_window_without_gaps_or_duplicates(client):
    v1 = "甲乙丙丁戊"
    doc = _make_doc(client, v1)
    for ch in "甲乙丙丁戊":
        _make_ann(client, doc, v1, ch)
    # 共 1 版本 + 5 批注 + 5 位置 = 11 个事件

    paged_events, last_page = _fetch_all_pages(client, doc, limit=4)
    assert len(paged_events) == 11

    # 与单页全量读取逐项一致（不漏、不重、不乱序）
    single = _get_events(client, doc, limit=200)["events"]
    assert paged_events == single
    assert len({(e["type"], e["id"]) for e in paged_events}) == 11

    # 末页：has_more=False，只给 resume_cursor
    assert last_page["has_more"] is False
    assert last_page["next_cursor"] is None
    assert last_page["resume_cursor"] is not None


def test_page_cursor_keeps_window_and_advances_last_key(client):
    v1 = "甲乙丙丁"
    doc = _make_doc(client, v1)
    for ch in "甲乙丙丁":
        _make_ann(client, doc, v1, ch)

    first = _get_events(client, doc, limit=3)
    cursor = _decode(first["next_cursor"])
    assert cursor["kind"] == "page"
    assert cursor["doc"] == doc
    assert cursor["from"] == first["window"]["from"]
    assert cursor["to"] == first["window"]["to"]
    last = first["events"][-1]
    assert cursor["last"] == [
        last["created_at"], _TYPE_RANK[last["type"]], last["id"]
    ]

    second = _get_events(client, doc, limit=3, cursor=first["next_cursor"])
    cursor2 = _decode(second["next_cursor"])
    # 起止水位始终沿用首轮固定值，只有最后排序键在推进
    assert cursor2["from"] == cursor["from"]
    assert cursor2["to"] == cursor["to"]
    assert cursor2["last"] > cursor["last"]
    # 第二页窗口与第一页一致
    assert second["window"] == first["window"]


def test_concurrent_writes_stay_out_of_fixed_window(client):
    """分页期间的并发新增留到下一轮：窗口始终沿用签发时的终点水位。"""
    v1 = "甲乙丙"
    doc = _make_doc(client, v1)
    for ch in "甲乙丙":
        _make_ann(client, doc, v1, ch)
    # 当前 1 + 3 + 3 = 7 个事件

    page1 = _get_events(client, doc, limit=3)
    assert len(page1["events"]) == 3

    # 分页中途并发新增：2 条批注（+2 批注 +2 位置）与 1 个新版（+1 版本 +5 位置）
    r = client.post(
        f"/documents/{doc}/annotations",
        json={"body": "新批注一", "selection": "甲", "start": 0, "end": 1},
    )
    assert r.status_code == 201, r.text
    r = client.post(
        f"/documents/{doc}/annotations",
        json={"body": "新批注二", "selection": "乙", "start": 1, "end": 2},
    )
    assert r.status_code == 201, r.text
    _new_version(client, doc, "甲乙丙丁")

    # 继续翻旧窗口：只能看到原来的 7 个事件，不含任何新增
    rest_events, last_page = [], None
    cursor = page1["next_cursor"]
    while cursor is not None:
        last_page = _get_events(client, doc, limit=3, cursor=cursor)
        rest_events.extend(last_page["events"])
        cursor = last_page["next_cursor"]
    assert len(page1["events"]) + len(rest_events) == 7
    assert all(e["payload"].get("body") not in ("新批注一", "新批注二")
               for e in rest_events if e["type"] == "annotation")

    # 末页的 resume_cursor 进入下一轮：新增事件全部出现
    nxt = _get_events(client, doc, limit=200, cursor=last_page["resume_cursor"])
    new_bodies = {
        e["payload"]["body"] for e in nxt["events"] if e["type"] == "annotation"
    }
    assert new_bodies == {"新批注一", "新批注二"}
    counts = {}
    for e in nxt["events"]:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    assert counts == {"annotation": 2, "location": 7, "version": 1}


# ---------------------------------------------------------------------------
# 续拉：resume_cursor 只取增量，空轮也推进水位
# ---------------------------------------------------------------------------


def test_resume_cursor_fetches_only_increment(client):
    v1 = "甲乙"
    doc = _make_doc(client, v1)
    _make_ann(client, doc, v1, "甲")

    first = _get_events(client, doc, limit=200)
    resume1 = first["resume_cursor"]
    wm1 = _decode(resume1)["from"]

    # 首轮之后新增：1 批注（+批注 +位置）+ 1 依赖 + 1 完成
    ann2 = _make_ann(client, doc, v1, "乙")
    ann1 = next(
        e["id"] for e in first["events"] if e["type"] == "annotation"
    )
    r = client.post(
        f"/documents/{doc}/dependencies",
        json={"relations": [{"prerequisite_id": ann1, "dependent_id": ann2}]},
    )
    assert r.status_code == 201, r.text
    r = client.post(f"/documents/{doc}/annotations/{ann1}/completions", json={})
    assert r.status_code == 201, r.text

    inc = _get_events(client, doc, cursor=resume1)
    counts = {}
    for e in inc["events"]:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    assert counts == {
        "annotation": 1,
        "location": 1,
        "dependency": 1,
        "completion": 1,
    }
    # 增量轮：起点 = 上轮已消费水位，终点 = 本快照新水位
    assert inc["window"]["from"] == wm1
    wm2 = _decode(inc["resume_cursor"])["from"]
    assert wm2 == inc["window"]["to"]
    assert wm2["annotations"] == wm1["annotations"] + 1
    assert wm2["dependencies"] == wm1["dependencies"] + 1
    assert wm2["completions"] == wm1["completions"] + 1


def test_empty_round_still_advances_watermark(client):
    doc = _make_doc(client, "甲乙")
    first = _get_events(client, doc, limit=200)
    resume = first["resume_cursor"]

    # 本文档无新增，但其他文档有写入（全局最大行 ID 前进）
    other = _make_doc(client, "别的文档")
    _make_ann(client, other, "别的文档", "别")

    empty = _get_events(client, doc, cursor=resume)
    assert empty["events"] == []
    assert empty["event_count"] == 0
    assert empty["next_cursor"] is None
    # 空轮同样推进水位：新 resume_cursor 的已消费水位严格前进
    old_wm = _decode(resume)["from"]
    new_wm = _decode(empty["resume_cursor"])["from"]
    assert new_wm["versions"] == old_wm["versions"] + 1
    assert new_wm["annotations"] == old_wm["annotations"] + 1
    assert new_wm["locations"] == old_wm["locations"] + 1

    # 再次空轮且无新写入：水位不再变化（游标幂等）
    again = _get_events(client, doc, cursor=empty["resume_cursor"])
    assert again["events"] == []
    assert _decode(again["resume_cursor"])["from"] == new_wm


# ---------------------------------------------------------------------------
# 游标校验：版本 / 结构 / 范围 / 跨文档 → 422
# ---------------------------------------------------------------------------


def test_cursor_undecodable(client):
    doc = _make_doc(client, "甲乙")
    for bad in ("not-a-cursor!!!", "%%%", _encode("not json").rstrip("=")):
        r = client.get(f"/documents/{doc}/events", params={"cursor": bad})
        assert r.status_code == 422, (bad, r.text)
    # Base64 合法但内容不是 JSON 对象
    r = client.get(
        f"/documents/{doc}/events",
        params={"cursor": _encode([1, 2, 3])},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "bad_cursor_structure"


def test_cursor_version_rejected(client):
    doc = _make_doc(client, "甲乙")
    wm = {t: 0 for t in (
        "versions", "annotations", "locations",
        "confirmations", "dependencies", "completions",
    )}
    token = _encode({"v": 2, "kind": "resume", "doc": doc, "from": wm})
    r = client.get(f"/documents/{doc}/events", params={"cursor": token})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "bad_cursor_version"


def test_cursor_structure_errors(client):
    doc = _make_doc(client, "甲乙")
    wm = {t: 0 for t in (
        "versions", "annotations", "locations",
        "confirmations", "dependencies", "completions",
    )}
    cases = [
        {"kind": "resume", "doc": doc, "from": wm},                 # 缺 v
        {"v": 1, "kind": "peek", "doc": doc, "from": wm},           # kind 非法
        {"v": 1, "kind": "resume", "doc": doc},                     # 缺 from
        {"v": 1, "kind": "resume", "doc": doc, "from": wm, "x": 1},  # 多字段
        {"v": 1, "kind": "page", "doc": doc, "from": wm},           # page 缺 to/last
        {"v": 1, "kind": "resume", "doc": "1", "from": wm},         # doc 类型错
        {"v": 1, "kind": "resume", "doc": doc,
         "from": {**wm, "versions": "0"}},                          # 水位类型错
        {"v": 1, "kind": "resume", "doc": doc,
         "from": {k: v for k, v in wm.items() if k != "versions"}},  # 水位缺表
        {"v": 1, "kind": "page", "doc": doc, "from": wm, "to": wm,
         "last": ["2026-01-01T00:00:00.000Z", 9, 1]},               # type_rank 越界
        {"v": 1, "kind": "page", "doc": doc, "from": wm, "to": wm,
         "last": ["2026-01-01T00:00:00.000Z", 1]},                  # last 缺元素
    ]
    for payload in cases:
        r = client.get(
            f"/documents/{doc}/events", params={"cursor": _encode(payload)}
        )
        assert r.status_code == 422, payload
        assert r.json()["detail"]["error"] == "bad_cursor_structure", payload


def test_cursor_range_errors(client):
    doc = _make_doc(client, "甲乙")
    wm = {t: 0 for t in (
        "versions", "annotations", "locations",
        "confirmations", "dependencies", "completions",
    )}
    ts = "2026-01-01T00:00:00.000Z"
    cases = [
        # 负水位
        {"v": 1, "kind": "resume", "doc": doc,
         "from": {**wm, "versions": -1}},
        # from > to
        {"v": 1, "kind": "page", "doc": doc,
         "from": {**wm, "versions": 5}, "to": {**wm, "versions": 3},
         "last": [ts, 1, 4]},
        # 最后排序键的行 ID 超出窗口终点
        {"v": 1, "kind": "page", "doc": doc,
         "from": wm, "to": {**wm, "versions": 3},
         "last": [ts, 1, 5]},
        # 最后排序键的行 ID 未越过窗口起点
        {"v": 1, "kind": "page", "doc": doc,
         "from": {**wm, "versions": 2}, "to": {**wm, "versions": 3},
         "last": [ts, 1, 2]},
        # 游标水位超过当前数据库水位（来自另一个数据库）
        {"v": 1, "kind": "resume", "doc": doc,
         "from": {**wm, "versions": 10 ** 9}},
    ]
    for payload in cases:
        r = client.get(
            f"/documents/{doc}/events", params={"cursor": _encode(payload)}
        )
        assert r.status_code == 422, payload
        assert r.json()["detail"]["error"] == "bad_cursor_range", payload


def test_cursor_cross_document_rejected(client):
    doc1 = _make_doc(client, "甲乙")
    doc2 = _make_doc(client, "丙丁")
    resume1 = _get_events(client, doc1, limit=200)["resume_cursor"]

    # 文档 1 的续拉游标不能用于文档 2
    r = client.get(f"/documents/{doc2}/events", params={"cursor": resume1})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "cross_document_cursor"

    # 文档 1 的续页游标同样不能跨文档
    for ch in "甲乙":
        _make_ann(client, doc1, "甲乙", ch)
    page_cursor = _get_events(client, doc1, limit=1)["next_cursor"]
    assert page_cursor is not None
    r = client.get(f"/documents/{doc2}/events", params={"cursor": page_cursor})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "cross_document_cursor"

    # 游标在所属文档上仍可用
    r = client.get(f"/documents/{doc1}/events", params={"cursor": resume1})
    assert r.status_code == 200


def test_limit_bounds(client):
    doc = _make_doc(client, "甲乙")
    assert client.get(f"/documents/{doc}/events", params={"limit": 0}).status_code == 422
    assert client.get(f"/documents/{doc}/events", params={"limit": 201}).status_code == 422
    assert client.get(f"/documents/{doc}/events", params={"limit": 1}).status_code == 200
    assert client.get(f"/documents/{doc}/events", params={"limit": 200}).status_code == 200


def test_events_of_missing_document(client):
    r = client.get("/documents/999999/events")
    assert r.status_code == 404


def test_read_only_feed_does_not_change_state(client):
    """事件流只读：反复拉取后归档摘要不变。"""
    doc = _make_doc(client, "甲乙丙")
    for ch in "甲乙丙":
        _make_ann(client, doc, "甲乙丙", ch)
    before = client.get(f"/documents/{doc}/archive").json()["digest"]
    _fetch_all_pages(client, doc, limit=2)
    after = client.get(f"/documents/{doc}/archive").json()["digest"]
    assert before == after
