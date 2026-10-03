"""批注驱动的脱敏副本生成（纯函数，不依赖数据库）。

- :func:`merge_ranges`：把多条批注在同一版本上的生效码点区间合并为**最大区间**
  ——重叠或首尾相接（``next.start <= cur.end``）的区间合并为一条，
  每个合并区间携带覆盖它的全部批注 ID（升序去重）；
- :func:`build_redaction`：合并后的区间按起点顺序，用同一个占位文本逐个替换，
  返回脱敏全文、源/结果码点半开区间映射（含未遮盖片段，二者各自铺满全文），
  以及每个占位出现位置（只出现一次）及其覆盖批注 ID。

所有偏移均按 Unicode 码点计（Python 字符串索引即码点索引）。
相同的源文本、区间与占位文本必然得到字节级一致的结果：
区间按 ``(起点, 终点, 批注 ID)`` 排序，批注 ID 一律升序。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple


@dataclass(frozen=True)
class EffectiveRange:
    """一条批注在目标版本上的生效范围（``[start, end)``，按码点计）。"""

    annotation_id: int
    start: int
    end: int


@dataclass(frozen=True)
class MergedInterval:
    """合并后的最大遮盖区间及其覆盖的批注 ID（升序）。"""

    start: int
    end: int
    annotation_ids: Tuple[int, ...]


def merge_ranges(ranges: Sequence[EffectiveRange]) -> List[MergedInterval]:
    """把重叠或首尾相接的范围合并为最大区间。

    入参可乱序；结果按起点升序。同一批注在合并区间内只列一次；
    合并后区间终点取相交链上的最大终点（包含关系不会截断外区间）。
    """
    ordered = sorted(ranges, key=lambda r: (r.start, r.end, r.annotation_id))
    merged: List[MergedInterval] = []
    for r in ordered:
        if merged and r.start <= merged[-1].end:
            last = merged[-1]
            merged[-1] = MergedInterval(
                start=last.start,
                end=max(last.end, r.end),
                annotation_ids=tuple(sorted(set(last.annotation_ids) | {r.annotation_id})),
            )
        else:
            merged.append(MergedInterval(r.start, r.end, (r.annotation_id,)))
    return merged


def build_redaction(
    source: str, intervals: Sequence[MergedInterval], placeholder: str
) -> Dict[str, Any]:
    """按起点顺序用 ``placeholder`` 替换各合并区间，生成脱敏副本与区间映射。

    返回字段：

    - ``redacted``：脱敏全文；
    - ``fragments``：按出现顺序排列的片段，**铺满源文本与结果文本**：
      - ``kept``（未遮盖）：源区间与结果区间等长、文本相同；
      - ``redacted``（遮盖）：源区间为被遮盖原文，结果区间为占位文本，
        携带 ``annotation_ids`` 与对应的 ``placeholder_index``；
    - ``placeholders``：占位文本的每次出现（每个占位只出现一次），
      含其在结果文本中的半开区间与覆盖它的批注 ID。

    区间均为 Unicode 码点半开区间 ``[start, end)``。
    """
    pieces: List[str] = []
    fragments: List[Dict[str, Any]] = []
    placeholders: List[Dict[str, Any]] = []

    source_pos = 0
    result_pos = 0

    def keep_until(end: int) -> None:
        nonlocal source_pos, result_pos
        if end <= source_pos:
            return
        length = end - source_pos
        fragments.append(
            {
                "index": len(fragments),
                "kind": "kept",
                "source_start": source_pos,
                "source_end": end,
                "result_start": result_pos,
                "result_end": result_pos + length,
            }
        )
        pieces.append(source[source_pos:end])
        source_pos = end
        result_pos += length

    for order, iv in enumerate(intervals):
        keep_until(iv.start)
        placeholder_start = result_pos
        placeholder_end = result_pos + len(placeholder)
        fragments.append(
            {
                "index": len(fragments),
                "kind": "redacted",
                "source_start": iv.start,
                "source_end": iv.end,
                "result_start": placeholder_start,
                "result_end": placeholder_end,
                "annotation_ids": list(iv.annotation_ids),
                "placeholder_index": order,
            }
        )
        placeholders.append(
            {
                "index": order,
                "placeholder": placeholder,
                "annotation_ids": list(iv.annotation_ids),
                "result_start": placeholder_start,
                "result_end": placeholder_end,
            }
        )
        pieces.append(placeholder)
        source_pos = iv.end
        result_pos = placeholder_end

    keep_until(len(source))

    return {
        "redacted": "".join(pieces),
        "fragments": fragments,
        "placeholders": placeholders,
    }
