"""锚点匹配核心逻辑。

所有偏移均按 Unicode 码点计（Python ``str`` 的下标天然就是码点下标，
BMP 之外的字符如 emoji 也算 1 个码点）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import List, Optional, Tuple


@dataclass
class Candidate:
    """选区在新文本中的一个精确落点及其匹配依据。"""

    start: int
    end: int
    left_match: int   # 左侧上下文后缀的匹配长度（码点数）
    right_match: int  # 右侧上下文前缀的匹配长度（码点数）
    score: int        # left_match + right_match

    def to_dict(self) -> dict:
        return asdict(self)


def find_occurrences(text: str, selection: str) -> List[int]:
    """枚举 ``selection`` 在 ``text`` 中的全部精确落点（允许重叠）。"""
    if not selection:
        return []
    starts: List[int] = []
    i = text.find(selection)
    while i != -1:
        starts.append(i)
        i = text.find(selection, i + 1)
    return starts


def _left_match(text: str, start: int, left_context: str) -> int:
    """候选点之前与左上下文后缀逐字符相同的长度。"""
    n = 0
    limit = min(start, len(left_context))
    while n < limit and text[start - 1 - n] == left_context[len(left_context) - 1 - n]:
        n += 1
    return n


def _right_match(text: str, end: int, right_context: str) -> int:
    """候选点之后与右上下文前缀逐字符相同的长度。"""
    n = 0
    limit = min(len(text) - end, len(right_context))
    while n < limit and text[end + n] == right_context[n]:
        n += 1
    return n


def locate(
    text: str,
    selection: str,
    left_context: str,
    right_context: str,
) -> Tuple[str, Optional[Candidate], List[Candidate]]:
    """在新文本 ``text`` 中为锚点定位。

    返回 ``(status, best, candidates)``：

    - ``resolved``   ：存在唯一最高分候选，``best`` 为该候选，自动续接；
    - ``ambiguous``  ：多个候选并列最高分，``best`` 为 ``None``，
      ``candidates`` 含全部候选范围（按分数降序、起点升序）；
    - ``unresolved`` ：选区在新文本中没有任何精确落点。
    """
    starts = find_occurrences(text, selection)
    if not starts:
        return "unresolved", None, []

    candidates: List[Candidate] = []
    for start in starts:
        end = start + len(selection)
        left = _left_match(text, start, left_context)
        right = _right_match(text, end, right_context)
        candidates.append(Candidate(start, end, left, right, left + right))

    candidates.sort(key=lambda c: (-c.score, c.start))
    top_score = candidates[0].score
    tops = [c for c in candidates if c.score == top_score]
    if len(tops) == 1:
        return "resolved", tops[0], candidates
    return "ambiguous", None, candidates
