# -*- coding: utf-8 -*-
"""批量偏移换算测试：边界映射纯函数 + 端到端接口。

覆盖 emoji（BMP 外，需 UTF-16 代理对 / UTF-8 四字节）、组合字符
（基字符 + 组合附加符号，是两个独立码点）与中文（BMP 三字节）。
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.coordinates import (
    ERR_ENDPOINT_OUT_OF_RANGE,
    ERR_INSIDE_MULTIBYTE_SEQUENCE,
    ERR_INSIDE_SURROGATE_PAIR,
    ERR_START_AFTER_END,
    build_coordinate_map,
    convert_batch,
)
from app.main import app

# a + 🎉(U+1F389) + e + ́(U+0301 组合尖音符) + 中(U+4E2D)
TEXT = "a" + chr(0x1F389) + "e" + chr(0x0301) + "中"
# 各码点宽度（码点 / UTF-16 码元 / UTF-8 字节）：
#   a=1/1/1  🎉=1/2/4  e=1/1/1  ́=1/1/2  中=1/1/3
CP_LEN, U16_LEN, U8_LEN = 5, 6, 11


# ---------------------------------------------------------------------------
# 纯函数：边界映射
# ---------------------------------------------------------------------------


def test_map_lengths_for_emoji_combining_and_chinese():
    cdata = build_coordinate_map(TEXT)
    assert (cdata.codepoint_length, cdata.utf16_length, cdata.utf8_length) == (
        CP_LEN,
        U16_LEN,
        U8_LEN,
    )
    # 每个码点边界都在正向表中给出对应的 UTF-16 / UTF-8 偏移
    assert list(cdata.cp_to_utf16) == [0, 1, 3, 4, 5, 6]
    assert list(cdata.cp_to_utf8) == [0, 1, 5, 6, 8, 11]


def test_map_reverse_tables_mark_only_character_interiors():
    cdata = build_coordinate_map(TEXT)
    # UTF-16：代理对占据码元 [1,3)，只有 2 是内部
    assert list(cdata.utf16_to_cp) == [0, 1, -1, 2, 3, 4, 5]
    # UTF-8：🎉 占字节 [1,5)（内部 2,3,4）；组合符 [6,8)（内部 7）；
    # 中占 [8,11)（内部 9,10）
    assert list(cdata.utf8_to_cp) == [
        0, 1, -1, -1, -1, 2, 3, -1, 4, -1, -1, 5
    ]


def test_map_empty_text_is_single_zero_boundary():
    cdata = build_coordinate_map("")
    assert cdata.codepoint_length == cdata.utf16_length == cdata.utf8_length == 0
    assert list(cdata.utf16_to_cp) == [0]
    assert list(cdata.utf8_to_cp) == [0]
    out = convert_batch(cdata, "utf16", [(0, 0)])
    assert out["results"] == [
        {
            "index": 0,
            "codepoint": {"start": 0, "end": 0},
            "utf16": {"start": 0, "end": 0},
            "utf8_byte": {"start": 0, "end": 0},
        }
    ]


def test_map_ascii_has_no_interior_boundaries():
    cdata = build_coordinate_map("abc")
    assert (cdata.utf16_length, cdata.utf8_length) == (3, 3)
    assert list(cdata.utf16_to_cp) == list(cdata.utf8_to_cp) == [0, 1, 2, 3]


def test_map_two_byte_and_three_byte_bmp_characters():
    # ä(U+00E4)=2 字节；中(U+4E2D)=3 字节；都在 BMP 内，UTF-16 各 1 码元
    cdata = build_coordinate_map("ä中")
    assert (cdata.codepoint_length, cdata.utf16_length, cdata.utf8_length) == (
        2, 2, 5
    )
    assert list(cdata.utf8_to_cp) == [0, -1, 1, -1, -1, 2]
    assert list(cdata.utf16_to_cp) == [0, 1, 2]


# ---------------------------------------------------------------------------
# 纯函数：合法换算
# ---------------------------------------------------------------------------


def test_convert_utf16_intervals_around_emoji():
    cdata = build_coordinate_map(TEXT)
    out = convert_batch(cdata, "utf16", [(1, 3), (0, 4), (4, 6)])
    assert out["errors"] == []
    # 🎉 独占：码元 [1,3) → 码点 [1,2)，字节 [1,5)
    assert out["results"][0] == {
        "index": 0,
        "codepoint": {"start": 1, "end": 2},
        "utf16": {"start": 1, "end": 3},
        "utf8_byte": {"start": 1, "end": 5},
    }
    # a🎉e：码元 [0,4) → 码点 [0,3)，字节 [0,6)
    assert out["results"][1]["codepoint"] == {"start": 0, "end": 3}
    assert out["results"][1]["utf8_byte"] == {"start": 0, "end": 6}
    # 组合符 + 中：码元 [4,6) → 码点 [3,5)，字节 [6,11)
    assert out["results"][2]["codepoint"] == {"start": 3, "end": 5}
    assert out["results"][2]["utf8_byte"] == {"start": 6, "end": 11}


def test_convert_utf8_intervals_with_combining_mark():
    cdata = build_coordinate_map(TEXT)
    # é = e + 组合符：字节 [5,8) → 码点 [2,4)
    out = convert_batch(cdata, "utf8_byte", [(5, 8)])
    assert out["results"][0]["codepoint"] == {"start": 2, "end": 4}
    assert out["results"][0]["utf16"] == {"start": 3, "end": 5}
    # 基字符与组合符之间是合法码点边界：e 单独 [5,6)，组合符单独 [6,8)
    out = convert_batch(cdata, "utf8_byte", [(5, 6), (6, 8)])
    assert out["results"][0]["codepoint"] == {"start": 2, "end": 3}
    assert out["results"][1]["codepoint"] == {"start": 3, "end": 4}


def test_convert_codepoint_is_identity_and_gives_other_units():
    cdata = build_coordinate_map(TEXT)
    out = convert_batch(cdata, "codepoint", [(0, 1), (1, 2), (4, 5), (0, 5)])
    assert out["errors"] == []
    assert [r["codepoint"] for r in out["results"]] == [
        {"start": 0, "end": 1},
        {"start": 1, "end": 2},
        {"start": 4, "end": 5},
        {"start": 0, "end": 5},
    ]
    # 中：码点 [4,5) → UTF-16 [5,6) → UTF-8 [8,11)
    assert out["results"][2]["utf16"] == {"start": 5, "end": 6}
    assert out["results"][2]["utf8_byte"] == {"start": 8, "end": 11}
    # 全文
    assert out["results"][3]["utf16"] == {"start": 0, "end": U16_LEN}
    assert out["results"][3]["utf8_byte"] == {"start": 0, "end": U8_LEN}


def test_convert_empty_interval_is_allowed():
    cdata = build_coordinate_map(TEXT)
    for unit in ("codepoint", "utf16", "utf8_byte"):
        boundary = {"codepoint": 2, "utf16": 3, "utf8_byte": 5}[unit]
        out = convert_batch(cdata, unit, [(boundary, boundary)])
        assert out["errors"] == []
        r = out["results"][0]
        assert r["codepoint"]["start"] == r["codepoint"]["end"]


def test_convert_preserves_request_order_and_index():
    cdata = build_coordinate_map(TEXT)
    pairs = [(4, 5), (0, 1), (1, 2), (0, 5)]
    out = convert_batch(cdata, "codepoint", pairs)
    assert [r["index"] for r in out["results"]] == [0, 1, 2, 3]
    assert [r["codepoint"] for r in out["results"]] == [
        {"start": 4, "end": 5},
        {"start": 0, "end": 1},
        {"start": 1, "end": 2},
        {"start": 0, "end": 5},
    ]


def test_all_aligned_boundaries_round_trip():
    text = "中" + chr(0x1F389) + "ä文" + chr(0x0301) + chr(0x1F600)
    cdata = build_coordinate_map(text)
    n = len(text)
    # 以码点边界为全集，分别用 UTF-16 / UTF-8 表达再换算回来，全部一致
    for unit, forward in (
        ("utf16", cdata.cp_to_utf16),
        ("utf8_byte", cdata.cp_to_utf8),
    ):
        pairs = [(i, j) for i in range(n + 1) for j in range(i, n + 1)]
        out = convert_batch(cdata, unit, [(forward[i], forward[j]) for i, j in pairs])
        assert out["errors"] == []
        for r, (i, j) in zip(out["results"], pairs):
            assert r["codepoint"] == {"start": i, "end": j}


# ---------------------------------------------------------------------------
# 纯函数：整批拒绝
# ---------------------------------------------------------------------------


def test_reject_utf16_offset_inside_surrogate_pair():
    cdata = build_coordinate_map(TEXT)
    # 🎉 的代理对占码元 [1,3)，偏移 2 在两个码元之间
    out = convert_batch(cdata, "utf16", [(2, 3), (0, 1)])
    assert out["results"] == []
    assert [(e["path"], e["error"]) for e in out["errors"]] == [
        ("intervals[0].start", ERR_INSIDE_SURROGATE_PAIR)
    ]
    message = out["errors"][0]["message"]
    assert "代理对" in message and "U+1F389" in message


def test_reject_utf8_offset_inside_multibyte_character():
    cdata = build_coordinate_map(TEXT)
    # 🎉 占字节 [1,5)；2/3/4 都在四字节字符内部
    out = convert_batch(cdata, "utf8_byte", [(2, 5), (0, 9)])
    assert out["results"] == []
    assert [(e["path"], e["error"]) for e in out["errors"]] == [
        ("intervals[0].start", ERR_INSIDE_MULTIBYTE_SEQUENCE),
        ("intervals[1].end", ERR_INSIDE_MULTIBYTE_SEQUENCE),
    ]
    assert "多字节字符内部" in out["errors"][0]["message"]
    assert "U+1F389" in out["errors"][0]["message"]
    # 中文三字节内部（中占 [8,11)）
    out = convert_batch(cdata, "utf8_byte", [(0, 10)])
    assert out["errors"][0]["error"] == ERR_INSIDE_MULTIBYTE_SEQUENCE
    assert "[8, 11)" in out["errors"][0]["message"]


def test_reject_out_of_range_endpoints_for_all_units():
    cdata = build_coordinate_map(TEXT)
    out = convert_batch(cdata, "codepoint", [(-1, CP_LEN), (0, CP_LEN + 1)])
    assert [(e["path"], e["error"]) for e in out["errors"]] == [
        ("intervals[0].start", ERR_ENDPOINT_OUT_OF_RANGE),
        ("intervals[1].end", ERR_ENDPOINT_OUT_OF_RANGE),
    ]
    out = convert_batch(cdata, "utf16", [(0, U16_LEN + 1)])
    assert out["errors"][0]["error"] == ERR_ENDPOINT_OUT_OF_RANGE
    out = convert_batch(cdata, "utf8_byte", [(U8_LEN + 1, U8_LEN + 1)])
    assert [e["error"] for e in out["errors"]] == [ERR_ENDPOINT_OUT_OF_RANGE] * 2
    # 端点恰好在总长处合法（半开区间终点）
    assert convert_batch(cdata, "utf8_byte", [(0, U8_LEN)])["errors"] == []


def test_reject_start_after_end_but_allow_same():
    cdata = build_coordinate_map(TEXT)
    out = convert_batch(cdata, "codepoint", [(3, 2)])
    assert out["results"] == []
    err = out["errors"][0]
    assert err["error"] == ERR_START_AFTER_END
    assert err["path"] == "intervals[0]"
    assert convert_batch(cdata, "codepoint", [(3, 3)])["errors"] == []


def test_batch_collects_all_errors_sorted_by_index_and_endpoint():
    cdata = build_coordinate_map(TEXT)
    out = convert_batch(
        cdata,
        "utf16",
        [
            (0, 1),       # 0 合法
            (2, 3),       # 1 start 在代理对内部
            (0, 99),      # 2 end 越界
            (5, 4),       # 3 start>end（端点本身都合法）
        ],
    )
    assert out["results"] == []
    assert [(e["index"], e["endpoint"], e["error"]) for e in out["errors"]] == [
        (1, "start", ERR_INSIDE_SURROGATE_PAIR),
        (2, "end", ERR_ENDPOINT_OUT_OF_RANGE),
        (3, "interval", ERR_START_AFTER_END),
    ]


def test_one_bad_interval_rejects_entire_batch_even_if_others_valid():
    cdata = build_coordinate_map(TEXT)
    out = convert_batch(cdata, "utf8_byte", [(0, 1), (3, 5), (8, 11)])
    assert out["results"] == []
    assert len(out["errors"]) == 1
    assert out["errors"][0]["index"] == 1


def test_conversion_is_deterministic():
    cdata = build_coordinate_map(TEXT)
    pairs = [(3, 0), (1, 3), (5, 6)]
    a = convert_batch(cdata, "utf16", pairs)
    b = convert_batch(cdata, "utf16", pairs)
    assert json.dumps(a, ensure_ascii=False, sort_keys=True) == json.dumps(
        b, ensure_ascii=False, sort_keys=True
    )


def test_lone_surrogate_text_does_not_crash_and_uses_wtf8_width():
    # Python 字符串允许孤立代理项：UTF-16 占 1 码元，WTF-8 占 3 字节
    text = "x" + chr(0xD800) + "y"
    cdata = build_coordinate_map(text)
    assert (cdata.codepoint_length, cdata.utf16_length, cdata.utf8_length) == (
        3, 3, 5
    )
    out = convert_batch(cdata, "utf8_byte", [(0, 4)])
    assert out["results"][0]["codepoint"] == {"start": 0, "end": 2}
    # WTF-8 三字节内部同样拒绝
    out = convert_batch(cdata, "utf8_byte", [(2, 4)])
    assert out["errors"][0]["error"] == ERR_INSIDE_MULTIBYTE_SEQUENCE


# ---------------------------------------------------------------------------
# 端到端接口
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _make_doc(client, content, title="偏移换算文档"):
    r = client.post("/documents", json={"title": title, "content": content})
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


def _convert(client, doc, number, unit, intervals):
    return client.post(
        f"/documents/{doc}/versions/{number}/coordinates",
        json={"input_unit": unit, "intervals": intervals},
    )


def test_api_utf16_batch_from_browser_offsets(client):
    doc = _make_doc(client, TEXT)
    r = _convert(
        client,
        doc,
        1,
        "utf16",
        [
            {"start": 1, "end": 3},   # 🎉
            {"start": 3, "end": 5},   # e + 组合符
            {"start": 0, "end": 6},   # 全文
        ],
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["document_id"] == doc
    assert data["version_number"] == 1
    assert data["input_unit"] == "utf16"
    assert data["lengths"] == {
        "codepoint": 5, "utf16": 6, "utf8_byte": 11
    }
    assert [item["index"] for item in data["results"]] == [0, 1, 2]
    assert data["results"][0] == {
        "index": 0,
        "codepoint": {"start": 1, "end": 2},
        "utf16": {"start": 1, "end": 3},
        "utf8_byte": {"start": 1, "end": 5},
    }
    assert data["results"][1]["codepoint"] == {"start": 2, "end": 4}
    assert data["results"][2]["utf8_byte"] == {"start": 0, "end": 11}


def test_api_utf8_batch_from_external_tool(client):
    doc = _make_doc(client, TEXT)
    r = _convert(
        client,
        doc,
        1,
        "utf8_byte",
        [{"start": 8, "end": 11}, {"start": 0, "end": 1}],
    )
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    # 中：字节 [8,11) → 码点 [4,5)、码元 [5,6)
    assert results[0]["codepoint"] == {"start": 4, "end": 5}
    assert results[0]["utf16"] == {"start": 5, "end": 6}
    assert results[1]["codepoint"] == {"start": 0, "end": 1}


def test_api_codepoint_input_round_trips(client):
    doc = _make_doc(client, TEXT)
    r = _convert(
        client, doc, 1, "codepoint", [{"start": 1, "end": 2}]
    )
    assert r.status_code == 200
    item = r.json()["results"][0]
    assert item["codepoint"] == {"start": 1, "end": 2}
    assert item["utf16"] == {"start": 1, "end": 3}
    assert item["utf8_byte"] == {"start": 1, "end": 5}


def test_api_targets_a_specific_version_snapshot(client):
    doc = _make_doc(client, TEXT)
    v2 = "纯中文第二版"
    r = client.post(f"/documents/{doc}/versions", json={"content": v2})
    assert r.status_code == 201
    # 第 1 版与第 2 版长度各不相同，换算以路径指定版本为准
    r1 = _convert(client, doc, 1, "codepoint", [{"start": 0, "end": 5}])
    r2 = _convert(client, doc, 2, "utf8_byte", [{"start": 0, "end": 18}])
    assert r1.json()["lengths"]["codepoint"] == 5
    assert r2.status_code == 200
    assert r2.json()["results"][0]["codepoint"] == {"start": 0, "end": 6}
    assert r2.json()["lengths"] == {"codepoint": 6, "utf16": 6, "utf8_byte": 18}


def test_api_422_inside_surrogate_pair_points_to_item_and_endpoint(client):
    doc = _make_doc(client, TEXT)
    r = _convert(
        client, doc, 1, "utf16",
        [{"start": 0, "end": 1}, {"start": 2, "end": 3}],
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["input_unit"] == "utf16"
    assert detail["lengths"]["utf16"] == 6
    err = detail["errors"][0]
    assert err["error"] == ERR_INSIDE_SURROGATE_PAIR
    assert err["path"] == "intervals[1].start"
    assert err["index"] == 1 and err["endpoint"] == "start" and err["value"] == 2
    assert "整批未换算" in detail["message"]


def test_api_422_inside_utf8_multibyte(client):
    doc = _make_doc(client, TEXT)
    r = _convert(
        client,
        doc,
        1,
        "utf8_byte",
        [{"start": 9, "end": 11}],  # 9 在「中」三字节内部
    )
    assert r.status_code == 422
    err = r.json()["detail"]["errors"][0]
    assert err["error"] == ERR_INSIDE_MULTIBYTE_SEQUENCE
    assert err["path"] == "intervals[0].start"


def test_api_422_out_of_range_and_start_after_end_collected(client):
    doc = _make_doc(client, TEXT)
    r = _convert(
        client,
        doc,
        1,
        "codepoint",
        [{"start": 0, "end": 99}, {"start": 4, "end": 2}],
    )
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert [(e["path"], e["error"]) for e in errors] == [
        ("intervals[0].end", ERR_ENDPOINT_OUT_OF_RANGE),
        ("intervals[1]", ERR_START_AFTER_END),
    ]


def test_api_404_for_missing_document_or_version(client):
    doc = _make_doc(client, TEXT)
    assert _convert(client, doc, 99, "codepoint",
                    [{"start": 0, "end": 1}]).status_code == 404
    assert _convert(client, 999999, 1, "codepoint",
                    [{"start": 0, "end": 1}]).status_code == 404


def test_api_schema_limits(client):
    doc = _make_doc(client, TEXT)
    # 空批次
    r = _convert(client, doc, 1, "codepoint", [])
    assert r.status_code == 422
    # 超过 200 个
    r = _convert(
        client, doc, 1, "codepoint",
        [{"start": 0, "end": 0}] * 201,
    )
    assert r.status_code == 422
    # 200 个合法
    r = _convert(
        client, doc, 1, "codepoint",
        [{"start": 0, "end": 0}] * 200,
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["results"]) == 200
    # 非法单位 / 缺字段 / 非整型
    for body in (
        {"input_unit": "byte", "intervals": [{"start": 0, "end": 1}]},
        {"input_unit": "codepoint", "intervals": [{"start": 0}]},
        {"input_unit": "codepoint", "intervals": [{"start": "x", "end": 1}]},
    ):
        assert client.post(
            f"/documents/{doc}/versions/1/coordinates", json=body
        ).status_code == 422


def test_api_read_only_and_byte_stable(client):
    doc = _make_doc(client, TEXT)
    body = {
        "input_unit": "utf16",
        "intervals": [{"start": 1, "end": 3}, {"start": 0, "end": 6}],
    }
    r1 = client.post(f"/documents/{doc}/versions/1/coordinates", json=body)
    r2 = client.post(f"/documents/{doc}/versions/1/coordinates", json=body)
    assert r1.status_code == r2.status_code == 200
    canonical = lambda r: json.dumps(  # noqa: E731
        r.json(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert canonical(r1) == canonical(r2)
    # 只读：换算不产生批注 / 迁移记录
    assert client.get(f"/documents/{doc}").json()["annotation_count"] == 0
    assert client.get(f"/documents/{doc}/versions/1").json()["locations"] == []


def test_api_empty_document_version(client):
    doc = _make_doc(client, "")
    r = _convert(
        client, doc, 1, "utf8_byte", [{"start": 0, "end": 0}]
    )
    assert r.status_code == 200
    assert r.json()["lengths"] == {"codepoint": 0, "utf16": 0, "utf8_byte": 0}
    r = _convert(client, doc, 1, "utf16", [{"start": 0, "end": 1}])
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["error"] == ERR_ENDPOINT_OUT_OF_RANGE
