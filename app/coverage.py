"""文档批注覆盖图（纯函数，不依赖数据库）。

把指定版本全文 ``[0, len(text))`` 划分为连续、无重叠的**最大片段**，
每个码点位置恰好落入一个片段，片段的状态由覆盖该位置的批注决定：

- ``covered``   ：至少有一条 **definite** 批注（``resolved`` / ``confirmed``）；
- ``uncertain`` ：没有 definite，但至少有一条 **possible** 批注
  （未确认且**非顺延**的 ``ambiguous``，其候选范围先按批注合并重叠候选）；
- ``uncovered`` ：两类都没有。

未覆盖全文的批注（``unresolved``、歧义顺延占位、该版尚不存在迁移记录的
批注）不产生任何区间，由调用方列入 ``exclusions`` 并说明原因。

片段携带升序去重的 ``definite_ids`` / ``possible_ids``；**相邻片段仅在
状态与两组 ID 完全相同时合并**为最大片段。所有偏移均按 Unicode 码点计
（Python 字符串索引即码点索引）。相同输入必然得到字节级一致的结果：
事件按位置排序、同位置事件作为一批同时生效，ID 集合一律升序。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

# exclusions 的机器可读原因码
REASON_UNRESOLVED = "unresolved"
REASON_AMBIGUOUS_CARRIED = "ambiguous_carried"
REASON_LOCATION_MISSING = "location_missing"


@dataclass(frozen=True)
class DefiniteRange:
    """一条生效位置确定的批注（resolved / confirmed）。"""

    annotation_id: int
    start: int
    end: int


@dataclass(frozen=True)
class AmbiguousRange:
    """一条未确认且非顺延的歧义批注：候选可能落在 ``candidates`` 任一范围。

    ``candidates`` 为 ``(start, end)`` 半开区间序列；计算前先把同一批注
    重叠或首尾相接的候选合并为最大区间，避免同一 ID 重复登记。
    """

    annotation_id: int
    candidates: Tuple[Tuple[int, int], ...]


@dataclass(frozen=True)
class Exclusion:
    """该版上不计入覆盖图的一条批注及其原因。"""

    annotation_id: int
    reason: str
    message: str


def merge_candidate_ranges(
    ranges: Sequence[Tuple[int, int]],
) -> List[Tuple[int, int]]:
    """把同一批注的候选范围中重叠或首尾相接的合并为最大区间。

    入参可乱序；结果按起点升序。包含关系不会截断外区间。
    """
    ordered = sorted(ranges, key=lambda r: (r[0], r[1]))
    merged: List[Tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def build_coverage(
    text: str,
    definite: Sequence[DefiniteRange],
    ambiguous: Sequence[AmbiguousRange],
) -> Dict[str, object]:
    """扫描线计算覆盖图：把全文切成状态与 ID 集合一致的最大片段。

    - 每条 definite 批注在其 ``[start, end)`` 上登记一次 ID；
    - 每条 ambiguous 批注先合并其重叠候选，再在每个合并候选区间上登记
      一次 ID（即便多个候选落在同一位置，该 ID 也只出现一次）；
    - 同一位置的全部开始 / 结束事件作为一批同时生效，再判定状态，
      因此首尾相接的区间不会在边界产生瞬态片段；
    - 相邻片段仅在 ``(状态, definite_ids, possible_ids)`` 完全相同时合并。

    返回 ``{"segments": [...]}``；空文本也返回唯一的空片段
    ``[0, 0)`` / ``uncovered``，保证片段序列从 0 无缝铺满到文本长度。
    """
    # 事件 = (位置, 增量, 批注 ID, 类别)；类别 0=definite，1=possible。
    # 同位置事件作为一批应用，增量 +1 / -1 的先后不影响集合结果。
    events: List[Tuple[int, int, int, int]] = []
    for r in definite:
        events.append((r.start, 1, r.annotation_id, 0))
        events.append((r.end, -1, r.annotation_id, 0))
    for a in ambiguous:
        for start, end in merge_candidate_ranges(a.candidates):
            events.append((start, 1, a.annotation_id, 1))
            events.append((end, -1, a.annotation_id, 1))
    events.sort(key=lambda e: (e[0], e[1]))

    definite_active: set = set()
    possible_active: set = set()

    def current_state() -> Tuple[str, Tuple[int, ...], Tuple[int, ...]]:
        d = tuple(sorted(definite_active))
        p = tuple(sorted(possible_active))
        status = "covered" if d else ("uncertain" if p else "uncovered")
        return status, d, p

    # 先只记录 (start, end, status, definite_ids, possible_ids)，文本最后统一切片
    raw: List[Tuple[int, int, str, Tuple[int, ...], Tuple[int, ...]]] = []

    def close(pos: int) -> None:
        """以当前状态闭合 [cursor, pos)；与前一片段等价则并入（最大片段）。"""
        if pos <= cursor:
            return
        status, d_ids, p_ids = current_state()
        if raw and raw[-1][2:] == (status, d_ids, p_ids):
            prev = raw[-1]
            raw[-1] = (prev[0], pos, status, d_ids, p_ids)
        else:
            raw.append((cursor, pos, status, d_ids, p_ids))

    cursor = 0
    i = 0
    while i < len(events):
        pos = events[i][0]
        close(pos)
        cursor = pos
        # 应用该位置上的全部事件后再重算状态。半开区间 [p,p) 为空：同一 ID
        # 在此位置既开始又结束时两者相抵（既不加入也不移除），故先把事件汇总
        # 为「只开始 / 只结束」两个集合，而不是逐条 add / discard。
        starts = (set(), set())
        ends = (set(), set())
        while i < len(events) and events[i][0] == pos:
            _, delta, annotation_id, group = events[i]
            (ends[group] if delta < 0 else starts[group]).add(annotation_id)
            i += 1
        for group, active in ((0, definite_active), (1, possible_active)):
            cancel = starts[group] & ends[group]
            active |= starts[group] - cancel
            active -= ends[group] - cancel

    # 收尾到文本末尾（无任何区间时整段、空文本时空片段）
    if cursor < len(text) or not raw:
        status, d_ids, p_ids = current_state()
        raw.append((cursor, len(text), status, d_ids, p_ids))

    segments = [
        {
            "index": index,
            "start": start,
            "end": end,
            "text": text[start:end],
            "definite_ids": list(d_ids),
            "possible_ids": list(p_ids),
            "status": status,
        }
        for index, (start, end, status, d_ids, p_ids) in enumerate(raw)
    ]
    return {"segments": segments}
