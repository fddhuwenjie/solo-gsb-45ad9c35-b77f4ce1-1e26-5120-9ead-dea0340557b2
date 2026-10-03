"""文档增量事件流：水位向量、稳定合并排序与续页 / 续拉游标。

六类只追加记录（版本、批注、迁移位置、人工确认、依赖、完成记录）按
``(created_at, type_rank, id)`` 稳定合并为单文档事件流：

- **首轮**（不带游标）：在调用方持有的同一个 SQLite 读事务（同一快照）中
  固定六张表的最大行 ID 作为**终点水位** ``to``，起点 ``from`` 为全零，
  读取窗口 ``(from, to]`` 内属于本文档的全部事件；
- **续页**（``kind=page`` 游标）：游标封装文档 ID、起止水位与最后排序键，
  只读取窗口 ``(from, to]`` 内排序键严格大于 ``last`` 的事件——始终沿用
  签发时的终点水位，窗口固定后并发新增的行（自增 ID 必然大于终点水位）
  留到下一轮，不漏、不重、不乱序；
- **续拉**（``kind=resume`` 游标）：窗口读尽（末页）时签发，携带已消费
  水位；下次调用时在新的读事务中重新固定终点水位，读取
  ``(from, 新 to]``——没有新事件的**空轮同样推进水位**。

游标为 Base64URL 编码的紧凑 JSON，自带版本号；版本不支持、结构损坏、
水位范围矛盾或跨文档使用一律抛 :class:`CursorError`（路由层转为 422）。

事件载荷只保留对应行落库时的稳定业务字段（如迁移位置保留库中的
resolved / ambiguous / unresolved 原始状态与顺延引用），不计算查询时刻的
派生状态（不在事件中合并人工确认、不派生 carried_forward 布尔等）。
"""
from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

CURSOR_VERSION = 1

# 六类事件对应的表（同时是水位向量的固定键）与归并序号：
# created_at 相同时按 type_rank 定序，再按行 ID 定序，得到全局全序。
TABLES = (
    "versions",
    "annotations",
    "locations",
    "confirmations",
    "dependencies",
    "completions",
)
TYPE_OF_RANK: Dict[int, str] = {
    1: "version",
    2: "annotation",
    3: "location",
    4: "confirmation",
    5: "dependency",
    6: "completion",
}
TABLE_OF_RANK: Dict[int, str] = {
    1: "versions",
    2: "annotations",
    3: "locations",
    4: "confirmations",
    5: "dependencies",
    6: "completions",
}
RANK_OF_TABLE = {table: rank for rank, table in TABLE_OF_RANK.items()}
RANK_OF_TYPE = {etype: rank for rank, etype in TYPE_OF_RANK.items()}

# 排序键 = (created_at, type_rank, 行 ID)
SortKey = Tuple[str, int, int]
Watermark = Dict[str, int]


class CursorError(Exception):
    """游标版本 / 结构 / 范围校验失败（路由层转为 HTTP 422）。

    - ``error``：机器可读错误码；
    - ``message``：中文说明。
    """

    def __init__(self, error: str, message: str):
        super().__init__(message)
        self.error = error
        self.message = message


# ---------------------------------------------------------------------------
# 水位向量
# ---------------------------------------------------------------------------


def zero_watermark() -> Watermark:
    """首轮起点水位：六张表都从 0 开始（窗口为左开区间，ID 从 1 起均包含）。"""
    return {table: 0 for table in TABLES}


def current_watermark(con: sqlite3.Connection) -> Watermark:
    """六张表当前的最大行 ID（空表取 0）。

    取的是全表最大 ID（locations / confirmations 没有文档列，也只能取
    全表）；文档归属在事件查询时另行过滤。调用方必须持有读事务，使六个
    MAX(id) 与随后的事件读取落在同一快照上。
    """
    return {
        table: con.execute(
            f"SELECT COALESCE(MAX(id), 0) AS m FROM {table}"
        ).fetchone()["m"]
        for table in TABLES
    }


# 单文档水位：各表只统计属于该文档的行的最大 ID。versions / annotations /
# dependencies / completions 直接带 document_id；locations 经批注归属限定，
# confirmations 再经迁移记录 → 批注归属限定。只追加表上该值对每文档单调
# 不减（行插入后不可删除 / 修改），适合作发布候选的乐观并发水位。
_DOC_WATERMARK_SQL = {
    "versions": "SELECT COALESCE(MAX(id), 0) AS m FROM versions WHERE document_id = ?",
    "annotations": "SELECT COALESCE(MAX(id), 0) AS m FROM annotations "
    "WHERE document_id = ?",
    "locations": """
        SELECT COALESCE(MAX(l.id), 0) AS m
        FROM locations l
        JOIN annotations a ON a.id = l.annotation_id
        WHERE a.document_id = ?
    """,
    "confirmations": """
        SELECT COALESCE(MAX(c.id), 0) AS m
        FROM confirmations c
        JOIN locations l ON l.id = c.location_id
        JOIN annotations a ON a.id = l.annotation_id
        WHERE a.document_id = ?
    """,
    "dependencies": "SELECT COALESCE(MAX(id), 0) AS m FROM dependencies "
    "WHERE document_id = ?",
    "completions": "SELECT COALESCE(MAX(id), 0) AS m FROM completions "
    "WHERE document_id = ?",
}


def document_watermark(con: sqlite3.Connection, document_id: int) -> Watermark:
    """六张表中**属于指定文档**的行的当前最大 ID（空取 0）。

    与 :func:`current_watermark` 的全表水位不同，本文档以外的写入不会推高
    本水位，供发布候选在创建读事务与确认写事务中做「该文档六类事件是否
    有新增」的精确比较。调用方必须持有事务，使六个 MAX(id) 与同事务内的
    其他读取（门禁快照等）落在同一快照上。
    """
    return {
        table: con.execute(sql, (document_id,)).fetchone()["m"]
        for table, sql in _DOC_WATERMARK_SQL.items()
    }


def ensure_within(wm: Watermark, ceiling: Watermark) -> None:
    """校验 ``wm`` 各表水位不超过 ``ceiling``（越界 → 范围错误）。

    续拉时已消费水位不能超过当前库的最大 ID（游标来自另一个数据库等
    情况）；续页时终点水位同样不能超过当前库——只追加保证正常游标永远
    满足这一不变量。
    """
    for table in TABLES:
        if wm[table] > ceiling[table]:
            raise CursorError(
                "bad_cursor_range",
                f"游标 {table} 水位 {wm[table]} 超过当前最大行 ID "
                f"{ceiling[table]}（游标可能来自另一个数据库）",
            )


# ---------------------------------------------------------------------------
# 事件读取
# ---------------------------------------------------------------------------

# 六个分支各自只取本窗口 ((from_t, to_t] 行 ID) 内、属于本文档的排序键；
# 外层统一按全序排序并按最后排序键翻页。locations / confirmations 经
# annotations 限定文档，不会串入其他文档的行。
_KEY_QUERY = """
SELECT type_rank, id, created_at FROM (
    SELECT 1 AS type_rank, id, created_at
    FROM versions
    WHERE document_id = ? AND id > ? AND id <= ?
    UNION ALL
    SELECT 2 AS type_rank, id, created_at
    FROM annotations
    WHERE document_id = ? AND id > ? AND id <= ?
    UNION ALL
    SELECT 3 AS type_rank, l.id, l.created_at
    FROM locations l
    JOIN annotations a ON a.id = l.annotation_id
    WHERE a.document_id = ? AND l.id > ? AND l.id <= ?
    UNION ALL
    SELECT 4 AS type_rank, c.id, c.created_at
    FROM confirmations c
    JOIN locations l ON l.id = c.location_id
    JOIN annotations a ON a.id = l.annotation_id
    WHERE a.document_id = ? AND c.id > ? AND c.id <= ?
    UNION ALL
    SELECT 5 AS type_rank, id, created_at
    FROM dependencies
    WHERE document_id = ? AND id > ? AND id <= ?
    UNION ALL
    SELECT 6 AS type_rank, id, created_at
    FROM completions
    WHERE document_id = ? AND id > ? AND id <= ?
)
{key_filter}
ORDER BY created_at, type_rank, id
LIMIT ?
"""


def _payload(rank: int, row: sqlite3.Row) -> Dict[str, Any]:
    """组装单条事件载荷：只取落库行的稳定业务字段，不计算任何派生状态。"""
    if rank == 1:  # version
        return {
            "version_id": row["id"],
            "version_number": row["number"],
            "content": row["content"],
        }
    if rank == 2:  # annotation
        return {
            "annotation_id": row["id"],
            "body": row["body"],
            "selection": row["selection"],
            "left_context": row["left_context"],
            "right_context": row["right_context"],
            "orig_start": row["orig_start"],
            "orig_end": row["orig_end"],
            "created_version_id": row["created_version_id"],
        }
    if rank == 3:  # location：保留库中原始状态（不合并人工确认）
        return {
            "location_id": row["id"],
            "annotation_id": row["annotation_id"],
            "version_id": row["version_id"],
            "status": row["status"],
            "start": row["start"],
            "end": row["end"],
            "left_match": row["left_match"],
            "right_match": row["right_match"],
            "score": row["score"],
            "candidates": json.loads(row["candidates"]),
            "carried_from": row["carried_from"],
        }
    if rank == 4:  # confirmation
        return {
            "confirmation_id": row["id"],
            "location_id": row["location_id"],
            "start": row["start"],
            "end": row["end"],
            "note": row["note"],
        }
    if rank == 5:  # dependency
        return {
            "dependency_id": row["id"],
            "prerequisite_id": row["prerequisite_id"],
            "dependent_id": row["dependent_id"],
        }
    # 6: completion
    return {
        "completion_id": row["id"],
        "annotation_id": row["annotation_id"],
        "note": row["note"],
    }


def _fetch_rows(
    con: sqlite3.Connection, rank: int, ids: List[int]
) -> Dict[int, sqlite3.Row]:
    table = TABLE_OF_RANK[rank]
    placeholders = ", ".join("?" for _ in ids)
    rows = con.execute(
        f"SELECT * FROM {table} WHERE id IN ({placeholders})", ids
    ).fetchall()
    return {row["id"]: row for row in rows}


def fetch_page(
    con: sqlite3.Connection,
    document_id: int,
    window_from: Watermark,
    window_to: Watermark,
    last_key: Optional[SortKey],
    limit: int,
) -> Tuple[List[Dict[str, Any]], Optional[SortKey], bool]:
    """读取窗口 ``(window_from, window_to]`` 内、排序键严格大于 ``last_key``
    的一页事件。

    返回 ``(events, last_sort_key, has_more)``：多取一个排序键探测窗口是否
    还有后续（``has_more=True`` 表示应签发续页游标，否则窗口已尽）。
    调用方必须持有读事务；六个分支、排序键探测与行读取都在该快照内。
    """
    params: List[Any] = []
    for rank in range(1, 7):
        table = TABLE_OF_RANK[rank]
        params.extend((document_id, window_from[table], window_to[table]))
    key_filter = ""
    if last_key is not None:
        # 行值比较：created_at / type_rank / id 三元组严格大于最后排序键
        key_filter = "WHERE (created_at, type_rank, id) > (?, ?, ?)"
        params.extend((last_key[0], last_key[1], last_key[2]))
    params.append(limit + 1)

    keys = con.execute(
        _KEY_QUERY.format(key_filter=key_filter), params
    ).fetchall()
    has_more = len(keys) > limit
    keys = keys[:limit]

    ids_by_rank: Dict[int, List[int]] = {}
    for key in keys:
        ids_by_rank.setdefault(key["type_rank"], []).append(key["id"])
    rows_by_rank = {
        rank: _fetch_rows(con, rank, ids) for rank, ids in ids_by_rank.items()
    }

    events: List[Dict[str, Any]] = []
    for key in keys:
        rank = key["type_rank"]
        row = rows_by_rank[rank][key["id"]]
        events.append(
            {
                "type": TYPE_OF_RANK[rank],
                "id": key["id"],
                "created_at": key["created_at"],
                "payload": _payload(rank, row),
            }
        )
    last_sort_key: Optional[SortKey] = (
        (keys[-1]["created_at"], keys[-1]["type_rank"], keys[-1]["id"])
        if keys
        else None
    )
    return events, last_sort_key, has_more


# ---------------------------------------------------------------------------
# 游标编解码
# ---------------------------------------------------------------------------


def encode_cursor(payload: Dict[str, Any]) -> str:
    """紧凑 JSON → URL 安全 Base64（去掉填充，query 参数中可直接传递）。"""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_page_cursor(
    document_id: int,
    window_from: Watermark,
    window_to: Watermark,
    last_key: SortKey,
) -> str:
    """续页游标：窗口固定，只推进最后排序键（终点水位始终沿用）。"""
    return encode_cursor(
        {
            "v": CURSOR_VERSION,
            "kind": "page",
            "doc": document_id,
            "from": {table: window_from[table] for table in TABLES},
            "to": {table: window_to[table] for table in TABLES},
            "last": [last_key[0], last_key[1], last_key[2]],
        }
    )


def make_resume_cursor(document_id: int, consumed_to: Watermark) -> str:
    """续拉游标：窗口读尽（含空轮）时签发，已消费水位作为下一轮起点。"""
    return encode_cursor(
        {
            "v": CURSOR_VERSION,
            "kind": "resume",
            "doc": document_id,
            "from": {table: consumed_to[table] for table in TABLES},
        }
    )


def _is_pos_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _check_watermark(value: Any, field: str) -> Watermark:
    """校验游标中的水位向量：结构问题 / 负值分别归类。"""
    if not isinstance(value, dict) or set(value.keys()) != set(TABLES):
        raise CursorError(
            "bad_cursor_structure",
            f"游标 {field} 水位应是恰好包含 {list(TABLES)} 六个键的对象",
        )
    wm: Watermark = {}
    for table in TABLES:
        v = value[table]
        if isinstance(v, bool) or not isinstance(v, int):
            raise CursorError(
                "bad_cursor_structure",
                f"游标 {field}.{table} 水位应为非负整数",
            )
        if v < 0:
            raise CursorError(
                "bad_cursor_range",
                f"游标 {field}.{table} 水位不能为负数（实际为 {v}）",
            )
        wm[table] = v
    return wm


def _check_last_key(value: Any) -> SortKey:
    """校验最后排序键 ``[created_at, type_rank(1..6), id(>=1)]``。"""
    if not (
        isinstance(value, list)
        and len(value) == 3
        and isinstance(value[0], str)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
        and 1 <= value[1] <= 6
        and isinstance(value[2], int)
        and not isinstance(value[2], bool)
        and value[2] >= 1
    ):
        raise CursorError(
            "bad_cursor_structure",
            "游标 last 应为 [created_at, type_rank(1..6 的整数), id(>=1 的整数)]",
        )
    return (value[0], value[1], value[2])


def decode_cursor(token: str, document_id: int) -> Dict[str, Any]:
    """解码并完整校验游标。

    任何版本不支持 / 解码失败 / 结构错误 / 范围矛盾 / 跨文档使用都抛
    :class:`CursorError`。返回 ``{"kind", "from", "to", "last"}``：
    ``kind=resume`` 时 ``to`` / ``last`` 为 None（终点水位由调用方在新的
    读事务中固定）。
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        data = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise CursorError(
            "bad_cursor_structure",
            "游标无法解码：应为 Base64URL 编码的紧凑 JSON",
        )

    if not isinstance(data, dict):
        raise CursorError("bad_cursor_structure", "游标应为 JSON 对象")
    if "v" not in data:
        raise CursorError("bad_cursor_structure", "游标缺少版本字段 v")
    version = data["v"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise CursorError("bad_cursor_structure", "游标版本 v 应为整数")
    if version != CURSOR_VERSION:
        raise CursorError(
            "bad_cursor_version",
            f"不支持的游标版本 {version}（当前支持版本 {CURSOR_VERSION}）",
        )

    kind = data.get("kind")
    if kind not in ("page", "resume"):
        raise CursorError(
            "bad_cursor_structure",
            "游标 kind 应为 'page'（续页）或 'resume'（续拉）",
        )
    expected_keys = {"v", "kind", "doc", "from"}
    if kind == "page":
        expected_keys |= {"to", "last"}
    if set(data.keys()) != expected_keys:
        raise CursorError(
            "bad_cursor_structure",
            f"{kind} 游标字段应恰好为 {sorted(expected_keys)}",
        )

    doc = data["doc"]
    if isinstance(doc, bool) or not isinstance(doc, int) or doc < 1:
        raise CursorError("bad_cursor_structure", "游标 doc 应为正整数")
    if doc != document_id:
        raise CursorError(
            "cross_document_cursor",
            f"游标属于文档 {doc}，不能用于文档 {document_id} 的事件流"
            "（游标不可跨文档使用）",
        )

    window_from = _check_watermark(data["from"], "from")
    result: Dict[str, Any] = {
        "kind": kind,
        "from": window_from,
        "to": None,
        "last": None,
    }
    if kind == "resume":
        return result

    window_to = _check_watermark(data["to"], "to")
    last_key = _check_last_key(data["last"])
    for table in TABLES:
        if window_from[table] > window_to[table]:
            raise CursorError(
                "bad_cursor_range",
                f"水位范围矛盾：from.{table}={window_from[table]} 大于 "
                f"to.{table}={window_to[table]}",
            )
    table_of_last = TABLE_OF_RANK[last_key[1]]
    if not (window_from[table_of_last] < last_key[2] <= window_to[table_of_last]):
        raise CursorError(
            "bad_cursor_range",
            f"最后排序键（type_rank={last_key[1]}, id={last_key[2]}）的行 ID "
            f"不在窗口 (from.{table_of_last}, to.{table_of_last}] 内",
        )
    result["to"] = window_to
    result["last"] = last_key
    return result
