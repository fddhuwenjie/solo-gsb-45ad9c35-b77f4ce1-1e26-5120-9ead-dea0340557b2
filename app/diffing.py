"""两版全文的码点级差异片段与批注范围投影。

- 编辑片段由 ``difflib.SequenceMatcher``（``autojunk=False``）在 Unicode 码点
  序列上计算：相同输入必然得到相同的片段序列与顺序（稳定性）；
- 片段分 insert / delete / replace 三类，同时携带起始版与目标版的码点区间；
- 投影与分类均为纯函数，不依赖数据库，便于确定性测试。
"""
from __future__ import annotations

import difflib
from dataclasses import asdict, dataclass
from typing import List, Optional, Tuple


@dataclass
class EditSegment:
    """两版全文间的一个编辑片段（增 / 删 / 改）。

    偏移均按 Unicode 码点计：``[from_start, from_end)`` 为起始版区间，
    ``[to_start, to_end)`` 为目标版区间。insert 的起始版区间为空（落点），
    delete 的目标版区间为空（折叠点）。
    """

    kind: str  # "insert" | "delete" | "replace"
    from_start: int
    from_end: int
    to_start: int
    to_end: int
    from_text: str  # 起始版被删除 / 被替换的文本（insert 时为 ""）
    to_text: str    # 目标版新增 / 替换后的文本（delete 时为 ""）

    def to_dict(self, index: int) -> dict:
        return {"index": index, **asdict(self)}


def compute_segments(from_text: str, to_text: str) -> List[EditSegment]:
    """生成两版全文间的编辑片段，按起始版位置升序（顺序稳定）。"""
    matcher = difflib.SequenceMatcher(None, from_text, to_text, autojunk=False)
    segments: List[EditSegment] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        segments.append(
            EditSegment(tag, i1, i2, j1, j2, from_text[i1:i2], to_text[j1:j2])
        )
    return segments


def project_range(
    segments: List[EditSegment], start: int, end: int
) -> Tuple[int, int]:
    """把起始版范围 ``[start, end)`` 投影到目标版，返回码点偏移对。

    - 完全在范围之前的片段只造成平移；
    - 落在边界上的插入（落点 == start 或 == end）不计入范围内部：
      落点为 start 时范围整体后移，落点为 end 时范围不受影响；
    - start / end 落在删除、替换片段内部时，分别折叠到该片段目标区间的
      起点 / 终点（此时范围必然已被分类为 modified 或 deleted）。
    """
    delta = 0
    proj_start: Optional[int] = None
    for seg in segments:
        if seg.kind == "insert":
            if seg.from_start <= start:
                delta += seg.to_end - seg.to_start
        elif seg.from_end <= start:
            delta += (seg.to_end - seg.to_start) - (seg.from_end - seg.from_start)
        elif seg.from_start < start:
            # start 严格位于删除 / 替换片段内部：折叠到其目标区间起点
            proj_start = seg.to_start
            break
    if proj_start is None:
        proj_start = start + delta

    delta = 0
    proj_end: Optional[int] = None
    for seg in segments:
        if seg.kind == "insert":
            if seg.from_start < end:
                delta += seg.to_end - seg.to_start
        elif seg.from_end <= end:
            delta += (seg.to_end - seg.to_start) - (seg.from_end - seg.from_start)
        elif seg.from_start < end:
            # end 严格位于删除 / 替换片段内部：折叠到其目标区间终点
            proj_end = seg.to_end
            break
    if proj_end is None:
        proj_end = end + delta
    return proj_start, proj_end


def classify_range(segments: List[EditSegment], start: int, end: int) -> str:
    """起始版范围 ``[start, end)`` 在两版差异下的分类。

    - ``deleted``   ：选区被删除片段完全覆盖（无替换重叠、无内部插入）；
    - ``modified``  ：选区与删除 / 替换片段重叠（部分删除、替换），
      或选区内部（不含边界）有插入；
    - ``shifted``   ：选区未被编辑触及，但偏移因区间外（含边界）编辑而变化；
    - ``unchanged`` ：选区未被编辑触及且偏移不变。
    """
    overlapping = [
        seg
        for seg in segments
        if seg.kind != "insert" and seg.from_start < end and seg.from_end > start
    ]
    inserts_inside = [
        seg
        for seg in segments
        if seg.kind == "insert" and start < seg.from_start < end
    ]
    if overlapping or inserts_inside:
        if (
            not inserts_inside
            and all(seg.kind == "delete" for seg in overlapping)
            and _fully_covered(overlapping, start, end)
        ):
            return "deleted"
        return "modified"
    proj_start, proj_end = project_range(segments, start, end)
    if proj_start == start and proj_end == end:
        return "unchanged"
    return "shifted"


def _fully_covered(overlapping: List[EditSegment], start: int, end: int) -> bool:
    """按位置升序、互不重叠的删除 / 替换片段是否完全覆盖 ``[start, end)``。"""
    pos = start
    for seg in overlapping:
        if seg.from_start > pos:
            return False
        pos = max(pos, seg.from_end)
        if pos >= end:
            return True
    return pos >= end


def hit_segment_indices(
    segments: List[EditSegment], start: int, end: int
) -> List[int]:
    """命中选区 ``[start, end)`` 的编辑片段下标（按位置升序，顺序稳定）。

    删除 / 替换片段与选区相交即命中；插入片段落点位于闭区间
    ``[start, end]``（含边界）即命中。区间外的编辑不命中，即使它
    造成了平移。
    """
    indices: List[int] = []
    for i, seg in enumerate(segments):
        if seg.kind == "insert":
            if start <= seg.from_start <= end:
                indices.append(i)
        elif seg.from_start < end and seg.from_end > start:
            indices.append(i)
    return indices
