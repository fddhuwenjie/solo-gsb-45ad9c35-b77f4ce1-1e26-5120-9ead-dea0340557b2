"""批注驱动脱敏副本测试：区间合并 / 替换纯函数与端到端接口。"""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.redacting import EffectiveRange, build_redaction, merge_ranges


# ---------------------------------------------------------------------------
# 纯函数：区间合并
# ---------------------------------------------------------------------------


def _ivs(intervals):
    return [(iv.start, iv.end, list(iv.annotation_ids)) for iv in intervals]


def test_merge_overlapping_ranges():
    merged = merge_ranges([
        EffectiveRange(2, 2, 8),
        EffectiveRange(1, 5, 9),
        EffectiveRange(3, 4, 10),
    ])
    # [2,8) 与 [4,10) 重叠合并，[5,9) 被并入同一链 → [2,10)；乱序输入按起点排序
    assert _ivs(merged) == [(2, 10, [1, 2, 3])]


def test_merge_abutting_ranges_and_order():
    merged = merge_ranges([
        EffectiveRange(2, 10, 12),
        EffectiveRange(1, 4, 10),   # 首尾相接 [4,10)
        EffectiveRange(3, 12, 21),  # 与前区间首尾相接 → 继续合并
        EffectiveRange(4, 20, 22),  # 与 [12,21) 重叠
    ])
    assert _ivs(merged) == [(4, 22, [1, 2, 3, 4])]


def test_merge_contained_range_does_not_shrink():
    merged = merge_ranges([
        EffectiveRange(1, 0, 20),
        EffectiveRange(2, 5, 10),  # 被包含：终点不收缩，ID 并入
    ])
    assert _ivs(merged) == [(0, 20, [1, 2])]


def test_merge_disjoint_ranges_sorted():
    merged = merge_ranges([
        EffectiveRange(2, 30, 40),
        EffectiveRange(1, 0, 5),
        EffectiveRange(3, 10, 12),  # 与 [30,40) 间有空隙，不合并
    ])
    assert _ivs(merged) == [(0, 5, [1]), (10, 12, [3]), (30, 40, [2])]


def test_merge_deterministic_with_unsorted_input():
    a = merge_ranges([
        EffectiveRange(3, 6, 8),
        EffectiveRange(2, 0, 4),
        EffectiveRange(1, 3, 6),
    ])
    b = merge_ranges([
        EffectiveRange(1, 3, 6),
        EffectiveRange(3, 6, 8),
        EffectiveRange(2, 0, 4),
    ])
    assert _ivs(a) == _ivs(b) == [(0, 8, [1, 2, 3])]


# ---------------------------------------------------------------------------
# 纯函数：脱敏构建与区间映射
# ---------------------------------------------------------------------------


def test_build_redaction_fragments_and_placeholders():
    source = "姓名：张三，城市：北京。"
    intervals = merge_ranges([
        EffectiveRange(10, 3, 5),  # 张三
        EffectiveRange(20, 9, 11),  # 北京
    ])
    result = build_redaction(source, intervals, "[密]")

    assert result["redacted"] == "姓名：[密]，城市：[密]。"
    kinds = [(f["kind"], f["source_start"], f["source_end"],
              f["result_start"], f["result_end"]) for f in result["fragments"]]
    assert kinds == [
        ("kept", 0, 3, 0, 3),
        ("redacted", 3, 5, 3, 6),
        ("kept", 5, 9, 6, 10),
        ("redacted", 9, 11, 10, 13),
        ("kept", 11, 12, 13, 14),
    ]
    # 片段从 0 铺满源文本与结果文本
    assert result["fragments"][0]["source_start"] == 0
    assert result["fragments"][-1]["source_end"] == len(source)
    assert result["fragments"][-1]["result_end"] == len(result["redacted"])
    # 相邻片段首尾相接
    for a, b in zip(result["fragments"], result["fragments"][1:]):
        assert a["source_end"] == b["source_start"]
        assert a["result_end"] == b["result_start"]

    placeholders = result["placeholders"]
    assert [p["placeholder"] for p in placeholders] == ["[密]", "[密]"]
    assert placeholders[0]["annotation_ids"] == [10]
    assert placeholders[1]["annotation_ids"] == [20]
    assert placeholders[0]["result_start"] == 3
    assert placeholders[0]["result_end"] == 6


def test_build_redaction_merged_interval_lists_all_ids():
    source = "abcdef"
    intervals = merge_ranges([
        EffectiveRange(2, 1, 4),
        EffectiveRange(1, 2, 5),  # 重叠合并为 [1,5) → bcde
    ])
    result = build_redaction(source, intervals, "X")
    assert result["redacted"] == "aXf"
    redacted = [f for f in result["fragments"] if f["kind"] == "redacted"]
    assert len(redacted) == 1
    assert redacted[0]["source_start"] == 1 and redacted[0]["source_end"] == 5
    assert redacted[0]["annotation_ids"] == [1, 2]
    assert result["placeholders"][0]["annotation_ids"] == [1, 2]


def test_build_redaction_unicode_code_points():
    source = "a🎉bc"  # 🎉 是 BMP 之外的字符，仍按 1 个码点计
    intervals = merge_ranges([EffectiveRange(1, 1, 2)])
    result = build_redaction(source, intervals, "▢")
    assert result["redacted"] == "a▢bc"
    frag = result["fragments"][1]
    assert (frag["source_start"], frag["source_end"]) == (1, 2)
    assert (frag["result_start"], frag["result_end"]) == (1, 2)


def test_build_redaction_deterministic_bytes():
    source = "甲乙丙丁"
    intervals = merge_ranges([
        EffectiveRange(2, 2, 3),
        EffectiveRange(1, 0, 1),
    ])
    first = build_redaction(source, intervals, "●")
    second = build_redaction(source, intervals, "●")
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )


def test_build_redaction_no_ranges_is_identity():
    source = "原样文本"
    result = build_redaction(source, [], "X")
    assert result["redacted"] == source
    assert result["placeholders"] == []
    assert len(result["fragments"]) == 1
    assert result["fragments"][0]["kind"] == "kept"
    assert (result["fragments"][0]["source_start"],
            result["fragments"][0]["source_end"]) == (0, len(source))


# ---------------------------------------------------------------------------
# 端到端接口
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _make_doc(client, content, title="脱敏测试文档"):
    r = client.post("/documents", json={"title": title, "content": content})
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


def _make_ann(client, doc, text, selection, index=0, body="批注"):
    start = -1
    for _ in range(index + 1):
        start = text.index(selection, start + 1)
    r = client.post(
        f"/documents/{doc}/annotations",
        json={
            "body": body,
            "selection": selection,
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


def _redact(client, doc, version, ids, placeholder="[密]"):
    return client.post(
        f"/documents/{doc}/redact",
        json={
            "version_number": version,
            "annotation_ids": ids,
            "placeholder": placeholder,
        },
    )


def test_api_basic_redaction(client):
    text = "姓名：张三，城市：北京。"
    doc = _make_doc(client, text)
    a1 = _make_ann(client, doc, text, "张三")
    a2 = _make_ann(client, doc, text, "北京")

    r = _redact(client, doc, 1, [a1, a2], "***")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["redacted"] == "姓名：***，城市：***。"
    assert data["annotation_ids"] == [a1, a2]
    assert data["redacted_interval_count"] == 2

    redacted_frags = [f for f in data["fragments"] if f["kind"] == "redacted"]
    kept_frags = [f for f in data["fragments"] if f["kind"] == "kept"]
    assert [f["annotation_ids"] for f in redacted_frags] == [[a1], [a2]]
    # 未遮盖片段也可追溯
    assert kept_frags and all(
        f["source_end"] - f["source_start"] == f["result_end"] - f["result_start"]
        for f in kept_frags
    )
    # 占位每次出现都列出，且每个只出现一次
    assert [p["result_start"] for p in data["placeholders"]] == [3, 10]
    assert all(p["placeholder"] == "***" for p in data["placeholders"])


def test_api_overlapping_and_abutting_ranges_merged(client):
    text = "甲乙丙丁戊"
    doc = _make_doc(client, text)
    a1 = _make_ann(client, doc, text, "甲乙丙")          # [0,3)
    a2 = _make_ann(client, doc, text, "丙丁")            # [2,4)，与 a1 重叠
    a3 = _make_ann(client, doc, text, "戊")             # [4,5)，与合并区间首尾相接

    r = _redact(client, doc, 1, [a1, a2, a3], "█")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["redacted"] == "█"
    assert data["redacted_interval_count"] == 1
    frag = next(f for f in data["fragments"] if f["kind"] == "redacted")
    assert (frag["source_start"], frag["source_end"]) == (0, 5)
    assert frag["annotation_ids"] == [a1, a2, a3]
    assert data["placeholders"][0]["annotation_ids"] == [a1, a2, a3]


def test_api_redaction_pins_version_and_uses_resolved_location(client):
    v1 = "心情不错在这"
    doc = _make_doc(client, v1)
    a1 = _make_ann(client, doc, v1, "心情不错")
    # 第 2 版选区平移
    v2 = "开头心情不错结尾"
    _new_version(client, doc, v2)

    r1 = _redact(client, doc, 1, [a1], "X")
    assert r1.json()["redacted"] == "X在这"
    r2 = _redact(client, doc, 2, [a1], "X")
    assert r2.json()["redacted"] == "开头X结尾"


def test_api_confirmed_annotation_redactable(client):
    text = "目标目标"
    doc = _make_doc(client, text)
    a1 = _make_ann(client, doc, text, "目标")  # 创建在第一处 [0,2)
    # 第 2 版两处并列 → ambiguous
    _new_version(client, doc, "目标目标")
    candidates = client.get(f"/documents/{doc}/versions/2").json()["locations"][0]
    chosen = candidates["candidates"][1]  # 确认后一处
    cr = client.post(
        f"/documents/{doc}/annotations/{a1}/confirmations",
        json={"version_number": 2, "start": chosen["start"], "end": chosen["end"]},
    )
    assert cr.status_code == 201

    r = _redact(client, doc, 2, [a1], "■")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["redacted"] == "目标■"
    frag = next(f for f in data["fragments"] if f["kind"] == "redacted")
    assert (frag["source_start"], frag["source_end"]) == (
        chosen["start"], chosen["end"]
    )


def test_api_ambiguous_unresolved_carried_rejected(client):
    text = "目标目标"
    doc = _make_doc(client, text)
    a1 = _make_ann(client, doc, text, "目标")
    _new_version(client, doc, "目标目标")  # ambiguous
    # 另一条 unresolved
    doc2_text = "甲乙"
    d2 = _make_doc(client, doc2_text)
    a2 = _make_ann(client, d2, doc2_text, "甲")
    _new_version(client, d2, "乙乙")  # 甲 unresolved

    r = _redact(client, doc, 2, [a1], "X")
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert [e["error"] for e in errors] == ["not_effective"]
    assert "ambiguous" in errors[0]["message"]
    assert errors[0]["annotation_id"] == a1
    assert errors[0]["path"] == "annotation_ids[0]"

    # ambiguous 未确认再出新版 → 顺延占位，同样拒绝
    _new_version(client, doc, "目标目标")
    r = _redact(client, doc, 3, [a1], "X")
    errors = r.json()["detail"]["errors"]
    assert errors[0]["error"] == "not_effective"
    assert "顺延" in errors[0]["message"]

    r = _redact(client, d2, 2, [a2], "X")
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["error"] == "not_effective"


def test_api_missing_location_annotation_created_later(client):
    text = "第一版文本"
    doc = _make_doc(client, text)
    _new_version(client, doc, "第二版文本")
    # 在第 2 版上创建批注 → 第 1 版没有迁移记录
    a1 = _make_ann(client, doc, "第二版文本", "第二版")
    r = _redact(client, doc, 1, [a1], "X")
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["error"] == "location_missing"


def test_api_annotation_not_found_and_cross_document(client):
    d1 = _make_doc(client, "文档一内容")
    d2_text = "文档二内容"
    d2 = _make_doc(client, d2_text)
    a2 = _make_ann(client, d2, d2_text, "文档二")

    r = _redact(client, d1, 1, [999999], "X")
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["error"] == "annotation_not_found"

    r = _redact(client, d1, 1, [a2], "X")
    assert r.status_code == 422
    err = r.json()["detail"]["errors"][0]
    assert err["error"] == "cross_document"
    assert err["annotation_id"] == a2


def test_api_duplicate_annotation_ids_rejected(client):
    text = "甲乙丙"
    doc = _make_doc(client, text)
    a1 = _make_ann(client, doc, text, "甲")
    r = _redact(client, doc, 1, [a1, a1], "X")
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert [e["path"] for e in errors] == ["annotation_ids[1]"]
    assert errors[0]["error"] == "duplicate_annotation"


def test_api_batch_collects_all_errors_with_locations(client):
    text = "甲乙丙"
    doc = _make_doc(client, text)
    a1 = _make_ann(client, doc, text, "甲")
    _new_version(client, doc, "乙乙丙")  # 甲 unresolved
    # 一次请求同时犯：重复、不存在、跨文档、not_effective —— 全部一次性返回
    other = _make_doc(client, "另一篇")
    a_other = _make_ann(client, other, "另一篇", "另一")
    r = client.post(
        f"/documents/{doc}/redact",
        json={
            "version_number": 2,
            "annotation_ids": [999999, a1, a1, a_other],
            "placeholder": "X",
        },
    )
    assert r.status_code == 422, r.text
    errors = r.json()["detail"]["errors"]
    by_path = {e["path"]: e["error"] for e in errors}
    assert by_path == {
        "annotation_ids[0]": "annotation_not_found",
        "annotation_ids[1]": "not_effective",
        "annotation_ids[2]": "duplicate_annotation",
        "annotation_ids[3]": "cross_document",
    }
    # 错误按请求位置排序
    assert [e["path"] for e in errors] == sorted(
        by_path, key=lambda p: int(p.rsplit("[", 1)[1][:-1])
    )


def test_api_redaction_is_read_only_and_byte_stable(client):
    text = "甲乙丙丁"
    doc = _make_doc(client, text)
    a1 = _make_ann(client, doc, text, "乙")
    a2 = _make_ann(client, doc, text, "丁")

    body = {"version_number": 1, "annotation_ids": [a2, a1], "placeholder": "●"}
    r1 = client.post(f"/documents/{doc}/redact", json=body)
    r2 = client.post(f"/documents/{doc}/redact", json=body)
    assert r1.status_code == r2.status_code == 200
    # 字节一致（键序、ID 序、片段序都稳定，与请求中 ID 顺序无关）
    b1 = json.dumps(r1.json(), ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")).encode("utf-8")
    b2 = json.dumps(r2.json(), ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")).encode("utf-8")
    assert b1 == b2

    # 只读：脱敏不新增任何迁移记录
    locs = client.get(f"/documents/{doc}/versions/1").json()["locations"]
    assert [l["annotation_id"] for l in locs] == [a1, a2]
    assert client.get(f"/documents/{doc}").json()["annotation_count"] == 2


def test_api_redaction_empty_annotation_ids_rejected_by_schema(client):
    doc = _make_doc(client, "abc")
    r = client.post(
        f"/documents/{doc}/redact",
        json={"version_number": 1, "annotation_ids": [], "placeholder": "X"},
    )
    assert r.status_code == 422

    r = client.post(
        f"/documents/{doc}/redact",
        json={"version_number": 1, "annotation_ids": [1], "placeholder": ""},
    )
    assert r.status_code == 422


def test_api_redaction_version_or_document_missing(client):
    doc = _make_doc(client, "abc")
    r = _redact(client, doc, 99, [1], "X")
    assert r.status_code == 404
    assert _redact(client, 999999, 1, [1], "X").status_code == 404


def test_api_redaction_mapping_reconstructs_both_texts(client):
    text = "机密A中间机密B结尾"
    doc = _make_doc(client, text)
    a1 = _make_ann(client, doc, text, "机密A")
    a2 = _make_ann(client, doc, text, "机密B")
    data = _redact(client, doc, 1, [a1, a2], "[X]").json()

    # 用映射分别重建源文本与结果文本（kept 片段文本取自源）
    rebuilt_source = "".join(
        text[f["source_start"]:f["source_end"]] if f["kind"] == "kept"
        else text[f["source_start"]:f["source_end"]]
        for f in data["fragments"]
    )
    rebuilt_result = "".join(
        text[f["source_start"]:f["source_end"]] if f["kind"] == "kept"
        else "[X]"
        for f in data["fragments"]
    )
    assert rebuilt_source == text
    assert rebuilt_result == data["redacted"] == "[X]中间[X]结尾"
