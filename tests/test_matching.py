"""匹配核心逻辑（纯函数）测试。"""
from app.matching import find_occurrences, locate


def test_find_occurrences_overlapping():
    assert find_occurrences("aaaa", "aa") == [0, 1, 2]


def test_find_occurrences_empty_selection():
    assert find_occurrences("abc", "") == []


def test_offsets_are_unicode_code_points():
    # emoji 是 BMP 之外的字符，仍按 1 个码点计
    text = "a🎉bc🎉de"
    assert find_occurrences(text, "🎉") == [1, 4]
    assert find_occurrences(text, "c🎉") == [3]


def test_locate_unresolved():
    status, best, candidates = locate("你好，世界。", "不存在", "", "")
    assert status == "unresolved"
    assert best is None
    assert candidates == []


def test_locate_resolved_unique_by_context():
    # "X" 出现两次，左上下文 "b " 与右上下文 " c" 共同使第一处唯一最高
    text = "ab X cd X ab"
    status, best, candidates = locate(text, "X", "b ", " c")
    assert status == "resolved"
    assert (best.start, best.end) == (3, 4)
    assert best.left_match == 2 and best.right_match == 2 and best.score == 4
    assert len(candidates) == 2
    # 候选按分数降序
    assert candidates[0].score >= candidates[1].score


def test_locate_ambiguous_tie_returns_all_candidates():
    text = "a X a X a"
    status, best, candidates = locate(text, "X", "a ", " a")
    assert status == "ambiguous"
    assert best is None
    assert [c.start for c in candidates] == [2, 6]
    assert len({c.score for c in candidates}) == 1  # 并列最高分


def test_locate_single_occurrence_zero_context_still_resolved():
    status, best, _ = locate("完全不同的文本", "完全", "左侧", "右侧")
    assert status == "resolved"
    assert best.score == 0
    assert (best.start, best.end) == (0, 2)


def test_left_suffix_and_right_prefix_lengths():
    # 左上下文只匹配上后缀、右上下文只匹配上前缀时按实际长度计分
    text = "zz第二段：目标。第三xx"
    status, best, _ = locate(text, "目标", "甲甲第二段：", "。第三乙乙")
    assert status == "resolved"
    assert best.left_match == len("第二段：")
    assert best.right_match == len("。第三")
