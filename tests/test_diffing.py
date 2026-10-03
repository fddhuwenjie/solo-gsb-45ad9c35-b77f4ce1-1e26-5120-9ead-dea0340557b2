"""差异片段与范围投影核心逻辑（纯函数）测试。"""
from app.diffing import (
    classify_range,
    compute_segments,
    hit_segment_indices,
    project_range,
)


def _kinds(segments):
    return [(s.kind, s.from_start, s.from_end, s.to_start, s.to_end) for s in segments]


def test_segments_kinds_and_texts():
    segments = compute_segments("hello world", "hello brave new world")
    assert _kinds(segments) == [("insert", 6, 6, 6, 16)]
    assert segments[0].from_text == ""
    assert segments[0].to_text == "brave new "

    segments = compute_segments("abc", "aXc")
    assert _kinds(segments) == [("replace", 1, 2, 1, 2)]
    assert segments[0].from_text == "b"
    assert segments[0].to_text == "X"

    segments = compute_segments("abc", "ac")
    assert _kinds(segments) == [("delete", 1, 2, 1, 1)]
    assert segments[0].from_text == "b"
    assert segments[0].to_text == ""


def test_segments_empty_for_identical_texts():
    assert compute_segments("完全相同", "完全相同") == []


def test_segments_ordered_and_deterministic():
    a = "第一段：天气很好。第二段：心情不错。第三段：天气很好。"
    b = "新增开头。第一段：天气不错。第二段：心情不错哦。"
    first = compute_segments(a, b)
    second = compute_segments(a, b)
    assert first == second  # 相同输入 → 相同片段序列
    keys = [(s.from_start, s.to_start) for s in first]
    assert keys == sorted(keys)  # 片段按位置升序


def test_segments_use_unicode_code_points():
    # emoji 是 BMP 之外的字符，仍按 1 个码点计
    segments = compute_segments("a🎉bc", "a🎉Xbc")
    assert _kinds(segments) == [("insert", 2, 2, 2, 3)]
    assert segments[0].to_text == "X"


def test_unchanged_when_no_edits_or_edits_after():
    segments = compute_segments("aa bb", "aa bb cc")  # 末尾插入
    assert classify_range(segments, 0, 2) == "unchanged"
    assert project_range(segments, 0, 2) == (0, 2)
    # 插入恰好落在选区终点边界：不算 modified，范围不变
    assert classify_range(segments, 3, 5) == "unchanged"
    assert project_range(segments, 3, 5) == (3, 5)
    # 边界插入仍计入命中片段（便于审计），但不影响分类
    assert hit_segment_indices(segments, 3, 5) == [0]
    assert hit_segment_indices(segments, 0, 2) == []


def test_shifted_by_edits_before_range():
    segments = compute_segments("aa bb cc", "XXaa bb cc")  # 开头插入
    assert classify_range(segments, 3, 5) == "shifted"
    assert project_range(segments, 3, 5) == (5, 7)
    # 区间外的编辑不命中
    assert hit_segment_indices(segments, 3, 5) == []


def test_shifted_by_boundary_insert_at_start():
    # 插入恰好落在选区起点边界：不算 modified，选区整体后移
    segments = compute_segments("abcdef", "abXYcdef")
    assert classify_range(segments, 2, 4) == "shifted"
    assert project_range(segments, 2, 4) == (4, 6)
    assert hit_segment_indices(segments, 2, 4) == [0]  # 边界插入计入命中


def test_shifted_by_delete_before_range():
    segments = compute_segments("XYabcdef", "abcdef")
    assert classify_range(segments, 4, 6) == "shifted"
    assert project_range(segments, 4, 6) == (2, 4)


def test_modified_by_insert_inside():
    segments = compute_segments("abcdef", "abcXYdef")  # 选区内部插入
    assert classify_range(segments, 2, 6) == "modified"
    assert project_range(segments, 2, 6) == (2, 8)  # 投影范围随内部插入扩展


def test_modified_by_partial_delete():
    segments = compute_segments("abcdef", "abcf")  # 删除选区中部 "de"
    assert classify_range(segments, 2, 6) == "modified"
    assert project_range(segments, 2, 6) == (2, 4)


def test_modified_by_replace_partial_and_full():
    # 部分替换
    segments = compute_segments("abcdef", "abcXef")
    assert classify_range(segments, 2, 6) == "modified"
    assert project_range(segments, 2, 6) == (2, 6)
    # 整体替换也算 modified（替换 ≠ 全部删除）
    segments = compute_segments("abcdef", "abXY")
    assert classify_range(segments, 2, 6) == "modified"
    assert project_range(segments, 2, 6) == (2, 4)


def test_deleted_when_fully_covered_by_delete():
    segments = compute_segments("abcdef", "ab")
    assert classify_range(segments, 2, 6) == "deleted"
    # 全部被删除时投影折叠为空区间，位于删除发生处
    assert project_range(segments, 2, 6) == (2, 2)
    assert hit_segment_indices(segments, 2, 6) == [0]


def test_deleted_spanning_whole_selection_with_context_edits():
    # "aaa bbb ccc ddd" → "aaa bXb ddd"：difflib 对齐为 插入 X + 删除 "b ccc"
    segments = compute_segments("aaa bbb ccc ddd", "aaa bXb ddd")
    assert classify_range(segments, 8, 11) == "deleted"  # "ccc" 全被删除
    assert project_range(segments, 8, 11) == (7, 7)
    assert classify_range(segments, 4, 7) == "modified"  # "bbb" 与删除重叠
    assert classify_range(segments, 12, 15) == "shifted"  # "ddd" 仅平移
    assert project_range(segments, 12, 15) == (8, 11)


def test_projection_with_emoji_offsets():
    segments = compute_segments("🎉目标🎉", "🎉🎉目标🎉")
    assert classify_range(segments, 1, 3) == "shifted"
    assert project_range(segments, 1, 3) == (2, 4)


def test_classification_deterministic_for_same_input():
    a = "甲：目标一。乙：目标二。丙：目标三。"
    b = "甲：目X标一。丙：目标三。"
    ranges = [(2, 5), (8, 11), (14, 17)]  # 目标一 / 目标二 / 目标三
    first = [classify_range(compute_segments(a, b), s, e) for s, e in ranges]
    second = [classify_range(compute_segments(a, b), s, e) for s, e in ranges]
    assert first == second  # 相同输入 → 相同分类
    assert first == ["modified", "deleted", "shifted"]
