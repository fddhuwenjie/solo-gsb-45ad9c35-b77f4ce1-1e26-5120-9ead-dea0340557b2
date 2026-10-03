"""文档批注覆盖图测试：纯函数（候选合并 / 扫描线切片）与端到端接口。"""
import pytest
from fastapi.testclient import TestClient

from app.coverage import (
    AmbiguousRange,
    DefiniteRange,
    build_coverage,
    merge_candidate_ranges,
)
from app.main import app


# ---------------------------------------------------------------------------
# 纯函数：候选范围合并
# ---------------------------------------------------------------------------


def test_merge_candidate_ranges_overlap_abut_contain():
    # 乱序、重叠 [1,3)∩[0,2)、首尾相接 [5,6)+[6,8)
    assert merge_candidate_ranges([(1, 3), (0, 2), (5, 6), (6, 8)]) == [
        (0, 3),
        (5, 8),
    ]
    # 包含关系不收缩外区间
    assert merge_candidate_ranges([(0, 10), (2, 4)]) == [(0, 10)]
    # 不相交保持分离、按起点排序
    assert merge_candidate_ranges([(8, 9), (0, 1), (3, 4)]) == [
        (0, 1),
        (3, 4),
        (8, 9),
    ]


# ---------------------------------------------------------------------------
# 纯函数：覆盖图切片
# ---------------------------------------------------------------------------


def _simple(seg):
    return (
        seg["start"],
        seg["end"],
        seg["status"],
        tuple(seg["definite_ids"]),
        tuple(seg["possible_ids"]),
    )


def test_empty_text_single_uncovered_segment():
    result = build_coverage("", [], [])
    assert _simple(result["segments"][0]) == (0, 0, "uncovered", (), ())
    assert len(result["segments"]) == 1


def test_no_ranges_whole_text_uncovered():
    result = build_coverage("abcdef", [], [])
    assert [_simple(s) for s in result["segments"]] == [
        (0, 6, "uncovered", (), ())
    ]


def test_definite_splits_into_three_segments():
    result = build_coverage("abcdef", [DefiniteRange(7, 1, 4)], [])
    assert [_simple(s) for s in result["segments"]] == [
        (0, 1, "uncovered", (), ()),
        (1, 4, "covered", (7,), ()),
        (4, 6, "uncovered", (), ()),
    ]
    assert [s["text"] for s in result["segments"]] == ["a", "bcd", "ef"]


def test_possible_only_is_uncertain():
    result = build_coverage(
        "abcdef", [], [AmbiguousRange(3, ((1, 3),))]
    )
    assert [_simple(s) for s in result["segments"]] == [
        (0, 1, "uncovered", (), ()),
        (1, 3, "uncertain", (), (3,)),
        (3, 6, "uncovered", (), ()),
    ]


def test_definite_dominates_possible_on_overlap():
    result = build_coverage(
        "abcdefgh",
        [DefiniteRange(1, 0, 5)],
        [AmbiguousRange(2, ((3, 7),))],
    )
    assert [_simple(s) for s in result["segments"]] == [
        (0, 3, "covered", (1,), ()),
        (3, 5, "covered", (1,), (2,)),
        (5, 7, "uncertain", (), (2,)),
        (7, 8, "uncovered", (), ()),
    ]


def test_overlapping_definite_ids_sorted_and_adjacent_not_merged():
    # [0,4) id1、[2,6) id2、[4,8) id3：4 处 id1 结束 / id3 开始成批生效
    result = build_coverage(
        "abcdefgh",
        [DefiniteRange(1, 0, 4), DefiniteRange(2, 2, 6), DefiniteRange(3, 4, 8)],
        [],
    )
    assert [_simple(s) for s in result["segments"]] == [
        (0, 2, "covered", (1,), ()),
        (2, 4, "covered", (1, 2), ()),
        (4, 6, "covered", (2, 3), ()),
        (6, 8, "covered", (3,), ()),
    ]


def test_abutting_definite_ranges_keep_separate_ids():
    # 首尾相接但 ID 不同 → 不合并
    result = build_coverage(
        "abcd", [DefiniteRange(1, 0, 2), DefiniteRange(2, 2, 4)], []
    )
    assert [_simple(s) for s in result["segments"]] == [
        (0, 2, "covered", (1,), ()),
        (2, 4, "covered", (2,), ()),
    ]


def test_ambiguous_overlapping_candidates_merged_same_id_once():
    # 同批注候选 [0,2) 与 [1,3) 重叠 → 合并为 [0,3)，possible_ids 只出现一次
    result = build_coverage(
        "abcdef", [], [AmbiguousRange(9, ((0, 2), (1, 3)))]
    )
    assert [_simple(s) for s in result["segments"]] == [
        (0, 3, "uncertain", (), (9,)),
        (3, 6, "uncovered", (), ()),
    ]


def test_ambiguous_abutting_candidates_merged_into_one_segment():
    result = build_coverage(
        "abcdef", [], [AmbiguousRange(9, ((0, 2), (2, 4)))]
    )
    assert [_simple(s) for s in result["segments"]] == [
        (0, 4, "uncertain", (), (9,)),
        (4, 6, "uncovered", (), ()),
    ]


def test_disjoint_candidates_same_id_on_both_segments():
    result = build_coverage(
        "abcdefgh", [], [AmbiguousRange(5, ((1, 3), (5, 7)))]
    )
    assert [_simple(s) for s in result["segments"]] == [
        (0, 1, "uncovered", (), ()),
        (1, 3, "uncertain", (), (5,)),
        (3, 5, "uncovered", (), ()),
        (5, 7, "uncertain", (), (5,)),
        (7, 8, "uncovered", (), ()),
    ]


def test_zero_length_interval_covers_nothing():
    # 半开区间 [p,p) 为空：同位置开始 / 结束相抵，不覆盖任何码点
    result = build_coverage("a", [DefiniteRange(1, 0, 0)], [])
    assert [_simple(s) for s in result["segments"]] == [
        (0, 1, "uncovered", (), ())
    ]
    # possible 的空候选同样不覆盖
    result = build_coverage("a", [], [AmbiguousRange(1, ((0, 0),))])
    assert [_simple(s) for s in result["segments"]] == [
        (0, 1, "uncovered", (), ())
    ]
    # 空区间落在另一批注的真实范围内：不改变其 ID 集合
    result = build_coverage(
        "abc", [DefiniteRange(2, 0, 3), DefiniteRange(1, 1, 1)], []
    )
    assert [_simple(s) for s in result["segments"]] == [
        (0, 3, "covered", (2,), ())
    ]


def test_unicode_codepoint_offsets_and_text():
    text = "甲：😀乙"  # 4 个码点：甲(0) ：(1) 😀(2) 乙(3)
    assert len(text) == 4
    result = build_coverage(text, [DefiniteRange(1, 2, 3)], [])
    seg = result["segments"][1]
    assert (seg["start"], seg["end"], seg["text"]) == (2, 3, "😀")
    assert result["segments"][0]["text"] == "甲："
    assert result["segments"][-1]["text"] == "乙"


def test_all_segments_tile_full_text_and_text_matches():
    text = "abcdefghij"
    result = build_coverage(
        text,
        [DefiniteRange(1, 1, 3), DefiniteRange(3, 6, 8)],
        [AmbiguousRange(2, ((2, 7),))],
    )
    segs = result["segments"]
    assert segs[0]["start"] == 0 and segs[-1]["end"] == len(text)
    for a, b in zip(segs, segs[1:]):
        assert a["end"] == b["start"]
    assert "".join(s["text"] for s in segs) == text
    for i, s in enumerate(segs):
        assert s["index"] == i
        assert s["text"] == text[s["start"]:s["end"]]


# ---------------------------------------------------------------------------
# 端到端接口
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _make_doc(client, content, title="覆盖图测试"):
    r = client.post("/documents", json={"title": title, "content": content})
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


def _make_ann(client, doc, selection, start, end, body="批注", left="", right=""):
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


def _new_version(client, doc, content):
    r = client.post(f"/documents/{doc}/versions", json={"content": content})
    assert r.status_code == 201, r.text
    return r.json()


def test_coverage_endpoint_basic(client):
    # v1：目标一 / 目标二 / 目标三 各一条批注
    v1 = "目标一，目标二，目标三"
    doc = _make_doc(client, v1)
    a1 = _make_ann(client, doc, "目标一", 0, 3, right="，")
    a2 = _make_ann(client, doc, "目标二", 4, 7, left="，", right="，")
    a3 = _make_ann(client, doc, "目标三", 8, 11, left="，")

    # v2：目标一唯一落点；目标二两处分數相同 → ambiguous（候选 [4,7)/[9,12)）；
    #     目标三消失 → unresolved
    v2 = "目标一，目标二X，目标二X"
    assert len(v2) == 13
    res = _new_version(client, doc, v2)["results"]
    by_id = {r["annotation_id"]: r for r in res}
    assert by_id[a1]["status"] == "resolved"
    assert by_id[a2]["status"] == "ambiguous"
    assert by_id[a2]["carried_forward"] is False
    assert [c["start"] for c in by_id[a2]["candidates"]] == [4, 9]
    assert by_id[a3]["status"] == "unresolved"

    # 批注 a4 创建于第 2 版（落在第一个 X 上）→ 第 1 版 coverage 应报 missing
    a4 = _make_ann(client, doc, "X", 7, 8, body="晚创建批注")

    # ---- 第 1 版：a1/a2/a3 definite 铺满选区，a4 尚不存在 ----
    r1 = client.get(f"/documents/{doc}/versions/1/coverage")
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["source_length"] == len(v1)
    simple = [(s["start"], s["end"], s["status"],
               tuple(s["definite_ids"]), tuple(s["possible_ids"]))
              for s in body1["segments"]]
    assert simple == [
        (0, 3, "covered", (a1,), ()),
        (3, 4, "uncovered", (), ()),
        (4, 7, "covered", (a2,), ()),
        (7, 8, "uncovered", (), ()),
        (8, 11, "covered", (a3,), ()),
    ]
    assert [e["annotation_id"] for e in body1["exclusions"]] == [a4]
    assert body1["exclusions"][0]["reason"] == "location_missing"
    assert "".join(s["text"] for s in body1["segments"]) == v1

    # ---- 第 2 版：definite / uncertain / uncovered / exclusions ----
    r2 = client.get(f"/documents/{doc}/versions/2/coverage")
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    simple = [(s["start"], s["end"], s["status"],
               tuple(s["definite_ids"]), tuple(s["possible_ids"]))
              for s in body2["segments"]]
    assert simple == [
        (0, 3, "covered", (a1,), ()),       # 目标一
        (3, 4, "uncovered", (), ()),        # ，
        (4, 7, "uncertain", (), (a2,)),     # 目标二（候选一）
        (7, 8, "covered", (a4,), ()),       # X
        (8, 9, "uncovered", (), ()),        # ，
        (9, 12, "uncertain", (), (a2,)),    # 目标二（候选二）
        (12, 13, "uncovered", (), ()),      # X
    ]
    for seg in body2["segments"]:
        assert seg["text"] == v2[seg["start"]:seg["end"]]
    assert "".join(s["text"] for s in body2["segments"]) == v2
    assert body2["summary"] == {
        "segment_count": 7,
        "covered": 2,
        "uncertain": 2,
        "uncovered": 3,
        "excluded": 1,
    }
    exclusions = {e["annotation_id"]: e for e in body2["exclusions"]}
    assert set(exclusions) == {a3}
    assert exclusions[a3]["reason"] == "unresolved"

    # 相同快照多次调用结果字节一致
    assert client.get(f"/documents/{doc}/versions/2/coverage").content == r2.content

    # 只读：coverage 调用前后该版迁移记录条数不变
    locs_before = len(client.get(f"/documents/{doc}/versions/2").json()["locations"])
    client.get(f"/documents/{doc}/versions/2/coverage")
    locs_after = len(client.get(f"/documents/{doc}/versions/2").json()["locations"])
    assert locs_before == locs_after


def test_coverage_carried_placeholder_then_confirmation(client):
    v1 = "目标一，目标二，目标三"
    doc = _make_doc(client, v1)
    a1 = _make_ann(client, doc, "目标一", 0, 3, right="，")
    a2 = _make_ann(client, doc, "目标二", 4, 7, left="，", right="，")
    a3 = _make_ann(client, doc, "目标三", 8, 11, left="，")
    _new_version(client, doc, "目标一，目标二X，目标二X")  # a2 ambiguous

    # 未确认就提交第 3 版 → a2 顺延占位；v3 不含「目标三」→ a3 仍 unresolved
    v3 = "全新的一版：目标一，没有别的目标。"
    res3 = _new_version(client, doc, v3)["results"]
    carried = {r["annotation_id"]: r for r in res3}[a2]
    assert carried["carried_forward"] is True

    r = client.get(f"/documents/{doc}/versions/3/coverage").json()
    exclusions = {e["annotation_id"]: e["reason"] for e in r["exclusions"]}
    assert exclusions == {a2: "ambiguous_carried", a3: "unresolved"}
    # 顺延占位不提供任何范围：全文只有 a1 一处 covered，没有 uncertain
    assert all(s["status"] != "uncertain" for s in r["segments"])
    covered = [s for s in r["segments"] if s["status"] == "covered"]
    assert [(s["start"], s["end"], s["definite_ids"]) for s in covered] == [
        (v3.index("目标一"), v3.index("目标一") + 3, [a1])
    ]
    assert "".join(s["text"] for s in r["segments"]) == v3

    # 在第 2 版确认 a2 的候选 [4,7) → 第 2 版覆盖图中 [4,7) 变 definite；
    # 第 3 版是顺延占位，确认原始歧义不追溯赋予其范围，仍计入 exclusions
    cr = client.post(
        f"/documents/{doc}/annotations/{a2}/confirmations",
        json={"version_number": 2, "start": 4, "end": 7},
    )
    assert cr.status_code == 201, cr.text
    body2 = client.get(f"/documents/{doc}/versions/2/coverage").json()
    by_start = {s["start"]: s for s in body2["segments"]}
    assert by_start[4]["status"] == "covered"
    assert by_start[4]["definite_ids"] == [a2]
    # 未选中的候选 [9,12) 不再是 possible：位置 7 起整段与相邻 uncovered
    # 合并为最大片段 [7,13)（覆盖两个 X 与中间标点）
    assert by_start[7]["status"] == "uncovered"
    assert by_start[7]["end"] == 13
    assert all(s["status"] != "uncertain" for s in body2["segments"])
    assert all(e["annotation_id"] != a2 for e in body2["exclusions"])

    r3 = client.get(f"/documents/{doc}/versions/3/coverage").json()
    assert any(
        e["annotation_id"] == a2 and e["reason"] == "ambiguous_carried"
        for e in r3["exclusions"]
    )

    # 确认后提交第 4 版 → a2 恢复自动迁移并 resolved，成为 definite
    v4 = "目标一，目标二，目标三"
    _new_version(client, doc, v4)
    body4 = client.get(f"/documents/{doc}/versions/4/coverage").json()
    statuses = {(s["start"], s["end"]): s["status"] for s in body4["segments"]}
    assert statuses[(4, 7)] == "covered"
    seg = next(s for s in body4["segments"] if s["start"] == 4)
    assert seg["definite_ids"] == [a2]
    assert not any(e["annotation_id"] == a2 for e in body4["exclusions"])


def test_coverage_404_for_missing_doc_and_version(client):
    assert client.get("/documents/999999/versions/1/coverage").status_code == 404
    doc = _make_doc(client, "abc")
    assert client.get(f"/documents/{doc}/versions/7/coverage").status_code == 404
