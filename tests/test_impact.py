"""批注改动影响分析接口（/documents/{id}/impact）端到端测试。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _make_doc(client, content, title="影响分析测试文档"):
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


def _impact(client, doc, from_version, to_version):
    r = client.get(
        f"/documents/{doc}/impact",
        params={"from_version": from_version, "to_version": to_version},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_impact_classification_flow(client):
    v1 = "aaa bbb ccc ddd"
    doc = _make_doc(client, v1)
    ann1 = _make_ann(client, doc, v1, "bbb", left="aaa ", right=" ccc")  # [4,7)
    ann2 = _make_ann(client, doc, v1, "ccc", left=" ", right=" ddd")     # [8,11)
    ann3 = _make_ann(client, doc, v1, "ddd", left="ccc ", right="")      # [12,15)

    _new_version(client, doc, "aaa bXb ddd")
    data = _impact(client, doc, 1, 2)

    # 编辑片段：码点级、按位置升序、增删改三类
    assert data["segment_count"] == 2
    assert [
        (s["kind"], s["from_start"], s["from_end"], s["to_start"], s["to_end"])
        for s in data["segments"]
    ] == [("insert", 5, 5, 5, 6), ("delete", 6, 11, 7, 7)]
    assert data["segments"][0]["to_text"] == "X"
    assert data["segments"][1]["from_text"] == "b ccc"

    items = {a["annotation_id"]: a for a in data["annotations"]}

    # ann1 "bbb"：选区内插入 + 与删除重叠 → modified
    a1 = items[ann1]
    assert a1["classification"] == "modified"
    assert a1["reason"] is None
    assert a1["from"]["start"] == 4 and a1["from"]["end"] == 7
    assert a1["from"]["status"] == "resolved"
    assert a1["to"] == {"start": 4, "end": 7}
    assert [h["index"] for h in a1["hits"]] == [0, 1]

    # ann2 "ccc"：选区全部被删除 → deleted，投影折叠到删除发生处
    a2 = items[ann2]
    assert a2["classification"] == "deleted"
    assert a2["to"] == {"start": 7, "end": 7}
    assert [h["index"] for h in a2["hits"]] == [1]

    # ann3 "ddd"：选区未动，仅被区间外编辑平移 → shifted，无命中片段
    a3 = items[ann3]
    assert a3["classification"] == "shifted"
    assert a3["to"] == {"start": 8, "end": 11}
    assert a3["hits"] == []

    assert data["summary"] == {
        "unchanged": 0,
        "shifted": 1,
        "modified": 1,
        "deleted": 1,
        "indeterminate": 0,
    }


def test_impact_unchanged_and_boundary_insert(client):
    v1 = "aa bb"
    doc = _make_doc(client, v1)
    ann1 = _make_ann(client, doc, v1, "aa", left="", right=" bb")   # [0,2)
    ann2 = _make_ann(client, doc, v1, "bb", left="aa ", right="")   # [3,5)

    # v2：编辑都在选区之后（插入落在 ann2 终点边界）→ unchanged
    _new_version(client, doc, "aa bb cc")
    data = _impact(client, doc, 1, 2)
    items = {a["annotation_id"]: a for a in data["annotations"]}
    assert items[ann1]["classification"] == "unchanged"
    assert items[ann1]["to"] == {"start": 0, "end": 2}
    assert items[ann1]["hits"] == []
    # 插入恰好落在选区边界：不得算 modified
    assert items[ann2]["classification"] == "unchanged"
    assert items[ann2]["to"] == {"start": 3, "end": 5}
    assert [h["kind"] for h in items[ann2]["hits"]] == ["insert"]

    # v3：开头插入 → 两条批注整体平移 shifted
    _new_version(client, doc, "XXaa bb cc")
    data = _impact(client, doc, 2, 3)
    items = {a["annotation_id"]: a for a in data["annotations"]}
    assert items[ann1]["classification"] == "shifted"
    assert items[ann1]["to"] == {"start": 2, "end": 4}
    assert items[ann2]["classification"] == "shifted"
    assert items[ann2]["to"] == {"start": 5, "end": 7}

    # 同一版本对比：无编辑片段，全部 unchanged
    data = _impact(client, doc, 1, 1)
    assert data["segments"] == []
    assert all(a["classification"] == "unchanged" for a in data["annotations"])


def test_impact_indeterminate(client):
    v1 = "说：目标。你好，世界。"
    doc = _make_doc(client, v1)
    ann_amb = _make_ann(client, doc, v1, "目标", left="说：", right="。")
    ann_unr = _make_ann(client, doc, v1, "世界", left="，", right="。")

    # v2：ann_amb 两处并列 → ambiguous；ann_unr 选区消失 → unresolved
    v2 = "说：目标。说：目标。"
    _new_version(client, doc, v2)
    # ann_late 创建于第 2 版之后，在第 1 版没有迁移记录
    ann_late = _make_ann(client, doc, v2, "目标", left="说：", right="。", body="后建")
    # v3：ann_amb 歧义未确认 → 顺延占位
    _new_version(client, doc, "说：目标。")

    data = _impact(client, doc, 2, 1)
    items = {a["annotation_id"]: a for a in data["annotations"]}

    # 歧义未确认 → indeterminate，不猜测范围
    a = items[ann_amb]
    assert a["classification"] == "indeterminate"
    assert "歧义" in a["reason"]
    assert a["from"]["status"] == "ambiguous"
    assert a["from"]["start"] is None and a["from"]["end"] is None
    assert a["to"] is None and a["hits"] == []

    # 未解析 → indeterminate
    a = items[ann_unr]
    assert a["classification"] == "indeterminate"
    assert "未解析" in a["reason"]
    assert a["from"]["status"] == "unresolved"
    assert a["to"] is None and a["hits"] == []

    # 起始版没有迁移记录（批注创建于该版之后）→ indeterminate
    data = _impact(client, doc, 1, 2)
    items = {a["annotation_id"]: a for a in data["annotations"]}
    a = items[ann_late]
    assert a["classification"] == "indeterminate"
    assert "没有迁移记录" in a["reason"]
    assert a["from"] is None
    assert a["to"] is None and a["hits"] == []

    # 歧义顺延占位 → indeterminate，原因说明顺延
    data = _impact(client, doc, 3, 1)
    items = {a["annotation_id"]: a for a in data["annotations"]}
    a = items[ann_amb]
    assert a["classification"] == "indeterminate"
    assert "顺延" in a["reason"]

    # 确认歧义后 → 起始版位置生效为 confirmed，正常投影
    loc = client.get(f"/documents/{doc}/annotations/{ann_amb}/history").json()
    cand = None
    for h in loc["history"]:
        if h["version_number"] == 2:
            cand = h["candidates"][0]
    r = client.post(
        f"/documents/{doc}/annotations/{ann_amb}/confirmations",
        json={"version_number": 2, "start": cand["start"], "end": cand["end"]},
    )
    assert r.status_code == 201, r.text
    data = _impact(client, doc, 2, 1)
    items = {a["annotation_id"]: a for a in data["annotations"]}
    a = items[ann_amb]
    assert a["from"]["status"] == "confirmed"
    assert a["classification"] != "indeterminate"
    assert a["to"] is not None


def test_impact_stable_for_same_input(client):
    v1 = "第一段：天气很好。第二段：心情不错。第三段：天气很好。"
    doc = _make_doc(client, v1)
    _make_ann(client, doc, v1, "心情不错", left="第二段：", right="。")
    _new_version(client, doc, "新增开头。" + v1)
    _new_version(client, doc, "第二段：心情不错。第二段：心情不错。")

    first = _impact(client, doc, 1, 3)
    second = _impact(client, doc, 1, 3)
    assert first == second  # 相同输入 → 片段顺序与分类完全一致
    keys = [(s["from_start"], s["to_start"]) for s in first["segments"]]
    assert keys == sorted(keys)


def test_impact_not_found(client):
    doc = _make_doc(client, "abc")
    r = client.get(
        f"/documents/{doc}/impact", params={"from_version": 1, "to_version": 9}
    )
    assert r.status_code == 404
    r = client.get(
        f"/documents/999/impact", params={"from_version": 1, "to_version": 1}
    )
    assert r.status_code == 404


def test_impact_unicode_code_points(client):
    v1 = "🎉🎉目标🎉"
    doc = _make_doc(client, v1)
    ann = _make_ann(client, doc, v1, "目标", left="🎉", right="🎉")
    _new_version(client, doc, "字🎉🎉目标🎉")
    data = _impact(client, doc, 1, 2)
    item = data["annotations"][0]
    assert item["annotation_id"] == ann
    assert item["classification"] == "shifted"
    # emoji 按 1 码点计：插入 1 个码点，选区 [2,4) → [3,5)
    assert item["from"]["start"] == 2 and item["from"]["end"] == 4
    assert item["to"] == {"start": 3, "end": 5}
