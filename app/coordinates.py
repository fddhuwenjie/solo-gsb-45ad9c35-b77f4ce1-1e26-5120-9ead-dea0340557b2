"""批量偏移换算（纯函数，不依赖数据库）。

浏览器（JavaScript 字符串按 UTF-16 码元计偏移）与外部工具（常见按
UTF-8 字节计偏移）提交的选区端点，需要换算成本服务统一使用的
**Unicode 码点**偏移，否则遇到 emoji 等 BMP 外字符或多字节字符时
选区会错位。

先对目标版本全文做一次线性扫描构建 :class:`CoordinateMap` 边界映射，
随后每个区间端点 O(1) 查表换算，整批复杂度 O(n + m)（n 为全文码点数，
m 为区间数）。映射区分三类边界：

================= ============================== ==========================
单位              端点合法条件                   端点落在字符内部时
----------------- ------------------------------ --------------------------
``codepoint``     0 ≤ 值 ≤ 码点长度              —（码点边界天然连续）
``utf16``         0 ≤ 值 ≤ UTF-16 码元长度且     ``inside_surrogate_pair``
                  不落在代理对两个码元之间
``utf8_byte``     0 ≤ 值 ≤ UTF-8 字节长度且     ``inside_multibyte_sequence``
                  不落在多字节字符的字节中间
================= ============================== ==========================

越界（``endpoint_out_of_range``）、``start > end``
（``start_after_end``）或端点落入字符内部时**整批拒绝**，只报告、
绝不取整猜测。组合字符（如 ``e + U+0301``）是两个独立码点，二者之间
的端点是合法码点边界（按码点 / UTF-16 均可对齐），不算字符内部。

Python 字符串中的孤立代理项（lone surrogate）无法用标准 UTF-8 表示，
按 ``errors='surrogatepass'`` 的 WTF-8 字节宽度（3 字节）计入，
保证任何文本都不会让映射构建崩溃，且结果确定。
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Sequence, Tuple

Unit = Literal["codepoint", "utf16", "utf8_byte"]

UNIT_CODEPOINT: Unit = "codepoint"
UNIT_UTF16: Unit = "utf16"
UNIT_UTF8_BYTE: Unit = "utf8_byte"
UNITS: Tuple[Unit, ...] = (UNIT_CODEPOINT, UNIT_UTF16, UNIT_UTF8_BYTE)

# 整批校验的机器可读错误码
ERR_ENDPOINT_OUT_OF_RANGE = "endpoint_out_of_range"
ERR_START_AFTER_END = "start_after_end"
ERR_INSIDE_SURROGATE_PAIR = "inside_surrogate_pair"
ERR_INSIDE_MULTIBYTE_SEQUENCE = "inside_multibyte_sequence"

_UNIT_LABELS = {
    UNIT_CODEPOINT: "Unicode 码点",
    UNIT_UTF16: "UTF-16 码元",
    UNIT_UTF8_BYTE: "UTF-8 字节",
}

# 反向表中标记「不是边界」的哨兵值
_INTERIOR = -1

# 错误排序权重：start → end → 区间整体（start>end）
_ENDPOINT_RANK = {"start": 0, "end": 1, "interval": 2}


@dataclass(frozen=True)
class CoordinateMap:
    """全文一次扫描得到的三种单位边界映射。

    - ``cp_to_utf16`` / ``cp_to_utf8``：码点边界 k → 该边界对应的
      UTF-16 码元偏移 / UTF-8 字节偏移（长度 n + 1）；
    - ``utf16_to_cp`` / ``utf8_to_cp``：反向表（长度为对应单位总长 + 1），
      合法边界处存码点下标，落在代理对 / 多字节字符内部的位置存 -1。

    反向表用 32 位整型数组（每元素 4 字节）而不是 Python 整型字典，
    构建 O(n)、端点查表 O(1)，内存约 32n 字节。
    """

    text: str
    codepoint_length: int
    utf16_length: int
    utf8_length: int
    cp_to_utf16: array
    cp_to_utf8: array
    utf16_to_cp: array
    utf8_to_cp: array


def _char_widths(ordinal: int) -> Tuple[int, int]:
    """返回单个码点的 (UTF-16 码元宽度, UTF-8 字节宽度)。"""
    width16 = 2 if ordinal > 0xFFFF else 1
    if ordinal < 0x80:
        width8 = 1
    elif ordinal < 0x800:
        width8 = 2
    elif 0xD800 <= ordinal <= 0xDFFF:
        # 孤立代理项：WTF-8（surrogatepass）编码为 3 字节
        width8 = 3
    elif ordinal < 0x10000:
        width8 = 3
    else:
        width8 = 4
    return width16, width8


def build_coordinate_map(text: str) -> CoordinateMap:
    """对全文做一次线性扫描，构建三种单位的边界映射（O(n)）。"""
    n = len(text)

    # 正向表：随扫描累加，边界 0 也在表中
    cp_to_utf16 = array("i", [0])
    cp_to_utf8 = array("i", [0])
    total16 = 0
    total8 = 0
    for ch in text:
        w16, w8 = _char_widths(ord(ch))
        total16 += w16
        total8 += w8
        cp_to_utf16.append(total16)
        cp_to_utf8.append(total8)

    # 反向表：先全部置为「内部」哨兵，再在每个码点边界处填码点下标
    utf16_to_cp = array("i", [_INTERIOR]) * (total16 + 1)
    utf8_to_cp = array("i", [_INTERIOR]) * (total8 + 1)
    utf16_to_cp[0] = 0
    utf8_to_cp[0] = 0
    pos16 = 0
    pos8 = 0
    for index, ch in enumerate(text, start=1):
        w16, w8 = _char_widths(ord(ch))
        pos16 += w16
        pos8 += w8
        utf16_to_cp[pos16] = index
        utf8_to_cp[pos8] = index

    return CoordinateMap(
        text=text,
        codepoint_length=n,
        utf16_length=total16,
        utf8_length=total8,
        cp_to_utf16=cp_to_utf16,
        cp_to_utf8=cp_to_utf8,
        utf16_to_cp=utf16_to_cp,
        utf8_to_cp=utf8_to_cp,
    )


def _lengths(cdata: CoordinateMap) -> Dict[str, int]:
    return {
        UNIT_CODEPOINT: cdata.codepoint_length,
        UNIT_UTF16: cdata.utf16_length,
        UNIT_UTF8_BYTE: cdata.utf8_length,
    }


def classify_offset(
    cdata: CoordinateMap, unit: Unit, value: int
) -> Tuple[str, int]:
    """校验单个端点并换算为码点偏移。

    返回 ``(状态, 码点偏移)``：成功时状态为 ``"ok"``；否则状态为
    :data:`ERR_ENDPOINT_OUT_OF_RANGE` / :data:`ERR_INSIDE_SURROGATE_PAIR`
    / :data:`ERR_INSIDE_MULTIBYTE_SEQUENCE`，码点偏移无意义（为 -1）。
    """
    if unit == UNIT_CODEPOINT:
        if 0 <= value <= cdata.codepoint_length:
            return "ok", value
        return ERR_ENDPOINT_OUT_OF_RANGE, -1

    if unit == UNIT_UTF16:
        if not (0 <= value <= cdata.utf16_length):
            return ERR_ENDPOINT_OUT_OF_RANGE, -1
        cp = cdata.utf16_to_cp[value]
        if cp == _INTERIOR:
            return ERR_INSIDE_SURROGATE_PAIR, -1
        return "ok", cp

    # utf8_byte
    if not (0 <= value <= cdata.utf8_length):
        return ERR_ENDPOINT_OUT_OF_RANGE, -1
    cp = cdata.utf8_to_cp[value]
    if cp == _INTERIOR:
        return ERR_INSIDE_MULTIBYTE_SEQUENCE, -1
    return "ok", cp


def _endpoint_error(
    cdata: CoordinateMap,
    unit: Unit,
    index: int,
    endpoint: str,
    value: int,
    error: str,
) -> Dict[str, Any]:
    """构造端点类错误（越界 / 代理对内部 / 多字节字符内部）。"""
    total = _lengths(cdata)[unit]
    label = _UNIT_LABELS[unit]
    if error == ERR_ENDPOINT_OUT_OF_RANGE:
        message = (
            f"条目 {index} 的 {endpoint}={value} 越界：该版全文按{label}计"
            f"长度为 {total}，合法端点范围为 0..{total}"
        )
    elif error == ERR_INSIDE_SURROGATE_PAIR:
        # value-1 一定是该代理对的起始边界（内部仅 value = 起点+1）
        cp_index = cdata.utf16_to_cp[value - 1]
        pair_start = value - 1
        message = (
            f"条目 {index} 的 {endpoint}={value} 落在 UTF-16 代理对内部："
            f"码位 U+{ord(cdata.text[cp_index]):04X}（码点位置 {cp_index}）"
            f"占据码元区间 [{pair_start}, {pair_start + 2})，"
            "端点必须对齐码点边界，不做取整猜测"
        )
    else:  # ERR_INSIDE_MULTIBYTE_SEQUENCE
        # 向左找到该字符的起始字节边界（反向表中第一个非哨兵值）
        boundary = value
        while cdata.utf8_to_cp[boundary] == _INTERIOR:
            boundary -= 1
        cp_index = cdata.utf8_to_cp[boundary]
        char_end = cdata.cp_to_utf8[cp_index + 1]
        message = (
            f"条目 {index} 的 {endpoint}={value} 落在 UTF-8 多字节字符内部："
            f"码位 U+{ord(cdata.text[cp_index]):04X}（码点位置 {cp_index}）"
            f"占据字节区间 [{boundary}, {char_end})，"
            "端点必须对齐码点边界，不做取整猜测"
        )
    return {
        "error": error,
        "message": message,
        "path": f"intervals[{index}].{endpoint}",
        "index": index,
        "endpoint": endpoint,
        "unit": unit,
        "value": value,
    }


def _order_error(index: int, start: int, end: int, unit: Unit) -> Dict[str, Any]:
    return {
        "error": ERR_START_AFTER_END,
        "message": (
            f"条目 {index} 的 start={start} 大于 end={end}："
            "半开区间要求 start <= end（允许空区间 [x, x)）"
        ),
        "path": f"intervals[{index}]",
        "index": index,
        "endpoint": "interval",
        "unit": unit,
        "start": start,
        "end": end,
    }


def _ranges(cdata: CoordinateMap, cp_start: int, cp_end: int) -> Dict[str, Any]:
    """给出一个码点半开区间在三种单位下的对应范围。"""
    return {
        UNIT_CODEPOINT: {"start": cp_start, "end": cp_end},
        UNIT_UTF16: {
            "start": cdata.cp_to_utf16[cp_start],
            "end": cdata.cp_to_utf16[cp_end],
        },
        UNIT_UTF8_BYTE: {
            "start": cdata.cp_to_utf8[cp_start],
            "end": cdata.cp_to_utf8[cp_end],
        },
    }


def convert_batch(
    cdata: CoordinateMap, unit: Unit, intervals: Sequence[Tuple[int, int]]
) -> Dict[str, Any]:
    """整批换算半开区间；任一条目不合法则整批拒绝。

    返回 ``{"errors": [...], "results": [...]}``：

    - 全部合法时 ``errors`` 为空，``results`` 逐项给出三种单位的对应
      范围与原索引（``index`` 即请求下标，响应顺序与请求一致）；
    - 存在越界、``start > end`` 或端点落入代理对 / 多字节字符内部时，
      ``results`` 为空，``errors`` 一次性列出全部问题（每条带请求条目
      与端点定位），按 ``(index, start/end/区间)`` 稳定排序。
    """
    errors: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []

    for index, (raw_start, raw_end) in enumerate(intervals):
        start_status, cp_start = classify_offset(cdata, unit, raw_start)
        end_status, cp_end = classify_offset(cdata, unit, raw_end)
        if start_status != "ok":
            errors.append(
                _endpoint_error(
                    cdata, unit, index, "start", raw_start, start_status
                )
            )
        if end_status != "ok":
            errors.append(
                _endpoint_error(cdata, unit, index, "end", raw_end, end_status)
            )
        if start_status == "ok" and end_status == "ok":
            if cp_start > cp_end:
                errors.append(_order_error(index, raw_start, raw_end, unit))
            else:
                item: Dict[str, Any] = {"index": index}
                item.update(_ranges(cdata, cp_start, cp_end))
                results.append(item)

    if errors:
        errors.sort(key=lambda e: (e["index"], _ENDPOINT_RANK[e["endpoint"]]))
        return {"errors": errors, "results": []}
    return {"errors": [], "results": results}
