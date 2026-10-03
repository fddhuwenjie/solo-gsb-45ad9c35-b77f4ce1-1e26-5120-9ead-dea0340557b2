"""API 端到端测试：创建 → 迁移 → 歧义 → 顺延 → 确认 → 恢复 → 对比。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _make_doc(client, content, title="测试文档"):
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


def test_full_migration_flow(client):
    v1 = "第一段：天气很好。第二段：心情不错。第三段：天气很好。"
    doc = _make_doc(client, v1)
    sel = "心情不错"
    ann = _make_ann(client, doc, v1, sel, left="第二段：", right="。")

    # 初始位置 resolved
    detail = client.get(f"/documents/{doc}/annotations/{ann}").json()
    assert detail["latest"]["status"] == "resolved"
    assert detail["latest"]["start"] == v1.index(sel)

    # v2：选区唯一落点平移 → 自动续接 resolved
    v2 = "新增的开头。" + v1
    res = _new_version(client, doc, v2)["results"][0]
    assert res["status"] == "resolved"
    assert res["start"] == v2.index(sel)
    assert res["end"] == v2.index(sel) + len(sel)

    # v3：两处落点上下文完全相同 → ambiguous，返回全部候选范围
    v3 = "第二段：心情不错。第二段：心情不错。"
    res = _new_version(client, doc, v3)["results"][0]
    assert res["status"] == "ambiguous"
    assert len(res["candidates"]) == 2
    assert len({c["score"] for c in res["candidates"]}) == 1  # 并列最高分
    chosen = res["candidates"][1]

    # v4：歧义未确认 → 不参与自动迁移（顺延占位），即使新版文本已唯一
    v4 = "第二段：心情不错。"
    res = _new_version(client, doc, v4)["results"][0]
    assert res["status"] == "ambiguous"
    assert res["carried_forward"] is True

    # 人工确认 v3 的歧义
    r = client.post(
        f"/documents/{doc}/annotations/{ann}/confirmations",
        json={
            "version_number": 3,
            "start": chosen["start"],
            "end": chosen["end"],
            "note": "取后一处",
        },
    )
    assert r.status_code == 201, r.text
    loc = r.json()["location"]
    assert loc["status"] == "confirmed"
    assert loc["start"] == chosen["start"]

    # v5：确认后恢复自动迁移 → resolved
    res = _new_version(client, doc, v4)["results"][0]
    assert res["status"] == "resolved"
    assert res["start"] == v4.index(sel)

    # 历史：5 个版本各一条记录，只追加
    hist = client.get(f"/documents/{doc}/annotations/{ann}/history").json()["history"]
    assert [h["version_number"] for h in hist] == [1, 2, 3, 4, 5]
    assert [h["status"] for h in hist] == [
        "resolved",
        "resolved",
        "confirmed",
        "ambiguous",   # v4 顺延占位，保持歧义待确认
        "resolved",
    ]
    assert hist[3]["carried_forward"] is True
    assert hist[2]["confirmation"]["note"] == "取后一处"

    # 任意两版对比：范围、状态与匹配依据
    cmp = client.get(
        f"/documents/{doc}/compare", params={"from_version": 1, "to_version": 3}
    ).json()
    item = cmp["annotations"][0]
    assert item["from"]["status"] == "resolved"
    assert item["to"]["status"] == "confirmed"
    assert len(item["to"]["candidates"]) == 2  # 匹配依据保留全部候选

    cmp = client.get(
        f"/documents/{doc}/compare", params={"from_version": 3, "to_version": 5}
    ).json()
    item = cmp["annotations"][0]
    assert item["from"]["status"] == "confirmed"
    assert item["to"]["status"] == "resolved"


def test_unresolved_and_reappear(client):
    v1 = "你好，世界。"
    doc = _make_doc(client, v1)
    _make_ann(client, doc, v1, "世界", left="，", right="。")

    # 选区消失 → unresolved
    res = _new_version(client, doc, "你好。")["results"][0]
    assert res["status"] == "unresolved"

    # 选区重新出现 → 自动恢复 resolved
    res = _new_version(client, doc, "大家好，世界。")["results"][0]
    assert res["status"] == "resolved"
    assert res["start"] == "大家好，世界。".index("世界")


def test_unicode_code_point_offsets(client):
    v1 = "🎉🎉目标🎉"
    doc = _make_doc(client, v1)
    ann = _make_ann(client, doc, v1, "目标", left="🎉", right="🎉")
    detail = client.get(f"/documents/{doc}/annotations/{ann}").json()
    assert detail["latest"]["start"] == 2  # emoji 按 1 码点计

    v2 = "字🎉🎉目标🎉"
    res = _new_version(client, doc, v2)["results"][0]
    assert res["status"] == "resolved"
    assert res["start"] == 3


def test_annotation_anchor_validation(client):
    v1 = "abcdef"
    doc = _make_doc(client, v1)

    # 选区与偏移不符
    r = client.post(
        f"/documents/{doc}/annotations",
        json={"body": "x", "selection": "zzz", "start": 0, "end": 3},
    )
    assert r.status_code == 422

    # end <= start
    r = client.post(
        f"/documents/{doc}/annotations",
        json={"body": "x", "selection": "c", "start": 3, "end": 3},
    )
    assert r.status_code == 422

    # 越界
    r = client.post(
        f"/documents/{doc}/annotations",
        json={"body": "x", "selection": "ef", "start": 4, "end": 99},
    )
    assert r.status_code == 422

    # 左上下文与实际文本不符
    r = client.post(
        f"/documents/{doc}/annotations",
        json={
            "body": "x",
            "selection": "cd",
            "left_context": "ZZ",
            "start": 2,
            "end": 4,
        },
    )
    assert r.status_code == 422

    # 空 selection
    r = client.post(
        f"/documents/{doc}/annotations",
        json={"body": "x", "selection": "", "start": 0, "end": 0},
    )
    assert r.status_code == 422


def test_confirmation_errors(client):
    v1 = "说：目标。"
    doc = _make_doc(client, v1)
    ann = _make_ann(client, doc, v1, "目标", left="说：", right="。")

    # v2 两处完全相同 → ambiguous
    v2 = "说：目标。说：目标。"
    res = _new_version(client, doc, v2)["results"][0]
    assert res["status"] == "ambiguous"
    cand = res["candidates"][0]

    # 确认 resolved 记录（v1）→ 409
    r = client.post(
        f"/documents/{doc}/annotations/{ann}/confirmations",
        json={"version_number": 1, "start": 2, "end": 4},
    )
    assert r.status_code == 409

    # 范围不在候选中 → 422
    r = client.post(
        f"/documents/{doc}/annotations/{ann}/confirmations",
        json={"version_number": 2, "start": 0, "end": 2},
    )
    assert r.status_code == 422

    # v3：歧义顺延占位 → 确认占位记录 409
    _new_version(client, doc, v2)
    r = client.post(
        f"/documents/{doc}/annotations/{ann}/confirmations",
        json={"version_number": 3, "start": cand["start"], "end": cand["end"]},
    )
    assert r.status_code == 409

    # 正常确认 v2 → 201；重复确认 → 409
    r = client.post(
        f"/documents/{doc}/annotations/{ann}/confirmations",
        json={"version_number": 2, "start": cand["start"], "end": cand["end"]},
    )
    assert r.status_code == 201
    r = client.post(
        f"/documents/{doc}/annotations/{ann}/confirmations",
        json={"version_number": 2, "start": cand["start"], "end": cand["end"]},
    )
    assert r.status_code == 409


def test_not_found(client):
    assert client.get("/documents/999").status_code == 404
    doc = _make_doc(client, "abc")
    assert client.get(f"/documents/{doc}/versions/9").status_code == 404
    assert client.get(f"/documents/{doc}/annotations/9").status_code == 404
    r = client.get(
        f"/documents/{doc}/compare", params={"from_version": 1, "to_version": 7}
    )
    assert r.status_code == 404


def test_versions_listing_and_content(client):
    doc = _make_doc(client, "第一版")
    _new_version(client, doc, "第二版")
    versions = client.get(f"/documents/{doc}/versions").json()["versions"]
    assert [v["version_number"] for v in versions] == [1, 2]
    v2 = client.get(f"/documents/{doc}/versions/2").json()
    assert v2["content"] == "第二版"
