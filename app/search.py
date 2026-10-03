"""指定版本范围内的字面子串检索：命中位置与相交批注状态。

审阅人在**同一文档**的一个版本号区间 ``[from_version, to_version]`` 内检索
一段文字（``query``），模块逐版做按 **Unicode 码点**的字面子串匹配
（Python ``str`` 下标天然是码点下标，emoji / 代理对 / 多字节字符都按 1 个
码点计），**保留重叠命中**（``"aaa"`` 查 ``"aa"`` 得到两个起点）。每个命中
返回版本号、半开范围 ``[start, end)``、可复现的前后文片段，以及在该版本上
与该范围相交的批注 ID 与生效状态。

批注在某一版本上的**生效范围 / 状态**口径（与脱敏、覆盖图模块一致）：

- 有人工确认 → ``confirmed``，以**确认范围**为准（确认范围优先于该迁移
  记录自带的 resolved 范围或歧义候选）；
- ``resolved`` → 该记录自带范围，状态 ``resolved``；
- 未确认且非顺延的 ``ambiguous`` → 逐个检查其**全部候选范围**，任一候选
  与命中区间相交即登记为 ``ambiguous``（返回相交候选，不挑选、不猜测）；
- ``unresolved`` 与歧义**顺延占位**（``carried_from`` 非空）在该版本上
  没有任何范围，不可能与命中区间相交，因此不会出现在 ``annotations`` 中
  （顺延原始歧义在其真实落点版本上仍按上面的规则参与相交判定）。

快照与游标（与 ``app.events`` 的水位游标同一设计）：

- **首轮**：在调用方持有的同一个 SQLite 读事务（同一快照）中固定
  versions / annotations / locations / confirmations 四张表属于本文档的
  最大行 ID（**快照水位向量**）；版本上界取快照内最新版本号，区间内的
  版本全文、迁移记录、候选与确认都只读取行 ID 不超过水位的行；
- **续页**（``kind=page``）：游标冻结版本上界、筛选条件与快照水位，
  只返回排序键 ``(版本号, 起点, 终点)`` 严格大于 ``after`` 的命中；
  **始终沿用签发时的快照与版本上界**——分页期间并发新增的版本、批注、
  迁移记录与确认（只追加表上自增 ID 必然更大）一律**留到下一轮**；
- **续拉**（``kind=resume``）：上一轮读尽后签发；携带时在新的读事务中
  重新固定快照水位与版本上界（``to_version`` 缺省时重新取最新版），
  新增版本与新增批注 / 确认由此进入结果，没有新数据的空轮也会签发新的
  resume 游标。

游标为 Base64URL 紧凑 JSON，载荷经服务启动时自动生成的 HMAC-SHA256 密钥
签名（密钥存于 ``app_meta`` 表，不随游标外泄）；签名不符（篡改）、
Base64/JSON/信封结构损坏、版本号不支持、跨文档使用、续页水位超过当前
数据库或携带的筛选条件与请求不一致，一律抛 :class:`CursorError`
（路由层转为 422）。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import sqlite3
from bisect import bisect_right
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import events as events_mod

CURSOR_VERSION = 1

PAGE_SIZE = 100
DEFAULT_CONTEXT_RADIUS = 16
MAX_CONTEXT_RADIUS = 80
MAX_QUERY_LENGTH = 256

SECRET_KEY = "search_cursor_secret"

# 检索快照依赖的四张只追加表（依赖边 / 完成记录不影响范围与相交状态）
WATERMARK_TABLES = ("versions", "annotations", "locations", "confirmations")

# 命中全序排序键：(版本号, 起点, 终点)
HitKey = Tuple[int, int, int]
Watermark = Dict[str, int]

_EFFECTIVE_SQL = """
SELECT l.annotation_id          AS annotation_id,
       l.status                 AS status,
       l.start                  AS start,
       l."end"                  AS end,
       l.candidates             AS candidates,
       l.carried_from           AS carried_from,
       c.start                  AS conf_start,
       c."end"                  AS conf_end
FROM locations l
LEFT JOIN confirmations c
       ON c.location_id = l.id AND c.id <= :conf_wm
WHERE l.version_id = :version_id
  AND l.id <= :loc_wm
  AND l.annotation_id <= :ann_wm
ORDER BY l.annotation_id
"""


class CursorError(Exception):
    """游标解码 / 签名 / 归属 / 范围 / 筛选一致性校验失败（路由层 → 422）。

    - ``error``：机器可读错误码；
    - ``message``：中文说明。
    """

    def __init__(self, error: str, message: str):
        super().__init__(message)
        self.error = error
        self.message = message


# ---------------------------------------------------------------------------
# 密钥
# ---------------------------------------------------------------------------


def get_secret(con: sqlite3.Connection) -> bytes:
    """读取游标 HMAC 密钥（``init_db`` 已保证行存在；读事务内调用）。"""
    row = con.execute(
        "SELECT value FROM app_meta WHERE key = ?", (SECRET_KEY,)
    ).fetchone()
    if row is None:  # 理论不可达：init_db 启动时 INSERT OR IGNORE
        raise RuntimeError("检索游标签名密钥未初始化（app_meta 缺少行）")
    return row["value"].encode("ascii")


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------


def current_watermark(con: sqlite3.Connection, document_id: int) -> Watermark:
    """四张表中**属于本文档**的行当前最大 ID（空取 0）。

    复用 :mod:`app.events` 的单文档水位口径（locations 经批注归属、
    confirmations 经迁移记录 → 批注归属限定），只截取检索依赖的四张表。
    调用方必须持有读事务，使四个 MAX(id) 与随后的全文 / 记录读取落在
    同一 SQLite 快照上。
    """
    doc_wm = events_mod.document_watermark(con, document_id)
    return {table: doc_wm[table] for table in WATERMARK_TABLES}


def load_versions(
    con: sqlite3.Connection,
    document_id: int,
    wm: Watermark,
    from_number: int,
    to_number: int,
) -> List[sqlite3.Row]:
    """读取快照内、版本号在 ``[from_number, to_number]`` 的版本行（升序）。

    同时以 ``id <= 水位`` 限定：续页沿用旧快照时，分页期间并发提交的新版
    本（行 ID 超出冻结水位）不会夹入本轮。
    """
    return con.execute(
        """
        SELECT id, number, content FROM versions
        WHERE document_id = ? AND id <= ?
          AND number >= ? AND number <= ?
        ORDER BY number
        """,
        (document_id, wm["versions"], from_number, to_number),
    ).fetchall()


def load_effective(
    con: sqlite3.Connection, version_id: int, wm: Watermark
) -> Dict[int, Dict[str, Any]]:
    """某版本上各批注的**生效范围集合**（只读取快照水位内的行）。

    返回 ``{annotation_id: {"status": ..., "ranges": [(start, end), ...]}}``：

    - confirmed：ranges 为确认范围单元素，状态 ``confirmed``；
    - resolved：ranges 为迁移记录范围单元素，状态 ``resolved``；
    - 未确认且非顺延的 ambiguous：ranges 为全部候选范围，状态 ``ambiguous``；
    - unresolved 与歧义顺延占位没有范围，不收录（不可能与任何区间相交）。
    """
    rows = con.execute(
        _EFFECTIVE_SQL,
        {
            "version_id": version_id,
            "conf_wm": wm["confirmations"],
            "loc_wm": wm["locations"],
            "ann_wm": wm["annotations"],
        },
    ).fetchall()
    effective: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        annotation_id = row["annotation_id"]
        if row["conf_start"] is not None:
            # 人工确认优先：确认范围覆盖迁移记录自带范围 / 歧义候选
            effective[annotation_id] = {
                "status": "confirmed",
                "ranges": [(row["conf_start"], row["conf_end"])],
            }
            continue
        if row["status"] == "resolved":
            effective[annotation_id] = {
                "status": "resolved",
                "ranges": [(row["start"], row["end"])],
            }
        elif row["status"] == "ambiguous" and row["carried_from"] is None:
            candidates = tuple(
                (c["start"], c["end"]) for c in json.loads(row["candidates"])
            )
            effective[annotation_id] = {
                "status": "ambiguous",
                "ranges": candidates,
            }
        # unresolved / 歧义顺延占位：无范围，不收录
    return effective


# ---------------------------------------------------------------------------
# 字面子串匹配与前后文
# ---------------------------------------------------------------------------


def find_literal_hits(text: str, query: str) -> List[int]:
    """枚举 ``query`` 在 ``text`` 中的全部字面子串落点（码点，保留重叠）。

    每次命中后起点只前进 **1** 个码点，因此 ``"aaaa"`` 查 ``"aa"`` 返回
    ``[0, 1, 2]``；空串由请求 schema 拒绝（min_length=1）。
    """
    starts: List[int] = []
    i = text.find(query)
    while i != -1:
        starts.append(i)
        i = text.find(query, i + 1)
    return starts


def build_context(
    text: str, start: int, end: int, radius: int
) -> Dict[str, Any]:
    """命中位置的可复现前后文片段（码点半开区间，边界自动截断）。

    - ``before`` / ``match`` / ``after``：左侧上下文（至多 ``radius`` 个
      码点）/ 命中文本 / 右侧上下文，三者按顺序拼回即为 ``snippet``；
    - ``snippet_start`` / ``snippet_end``：片段在版本全文中的半开范围；
    - ``match_start_in_snippet`` / ``match_end_in_snippet``：命中在片段内
      的相对码点偏移（可直接在 ``snippet`` 上复原高亮）；
    - ``truncated_left`` / ``truncated_right``：对应侧是否因触及文本边界
      而短于 ``radius``。

    版本全文只追加、不可变，``radius`` 随游标冻结：相同快照 + 相同请求
    得到字节一致的片段。
    """
    snippet_start = max(0, start - radius)
    snippet_end = min(len(text), end + radius)
    return {
        "before": text[snippet_start:start],
        "match": text[start:end],
        "after": text[end:snippet_end],
        "snippet": text[snippet_start:snippet_end],
        "snippet_start": snippet_start,
        "snippet_end": snippet_end,
        "match_start_in_snippet": start - snippet_start,
        "match_end_in_snippet": end - snippet_start,
        "truncated_left": snippet_start > 0,
        "truncated_right": snippet_end < len(text),
    }


def _intersecting(
    effective: Dict[int, Dict[str, Any]], start: int, end: int
) -> List[Dict[str, Any]]:
    """与半开区间 ``[start, end)`` 相交（半开判定，端点相接不算）的批注。

    confirmed / resolved 至多带一个范围；ambiguous 只列出**相交的**候选
    范围。按批注 ID 升序，输出稳定。
    """
    items: List[Dict[str, Any]] = []
    for annotation_id in sorted(effective):
        info = effective[annotation_id]
        hit_ranges = [
            (rstart, rend)
            for rstart, rend in info["ranges"]
            if rstart < end and rend > start
        ]
        if not hit_ranges:
            continue
        if info["status"] == "ambiguous":
            items.append(
                {
                    "annotation_id": annotation_id,
                    "status": "ambiguous",
                    "candidates": [
                        {"start": rstart, "end": rend}
                        for rstart, rend in hit_ranges
                    ],
                }
            )
        else:
            rstart, rend = hit_ranges[0]
            items.append(
                {
                    "annotation_id": annotation_id,
                    "status": info["status"],
                    "start": rstart,
                    "end": rend,
                }
            )
    return items


def build_hits(
    versions: Sequence[sqlite3.Row],
    query: str,
    context_radius: int,
    effective_by_version: Dict[int, Dict[int, Dict[str, Any]]],
) -> Tuple[List[Dict[str, Any]], List[HitKey]]:
    """对区间内各版全文做字面匹配并组装命中（含前后文与相交批注）。

    返回 ``(hits, keys)``：``hits`` 已按 ``(版本号, 起点, 终点)`` 稳定
    排序，``keys`` 为与之对齐的排序键（供游标键集分页做二分）。
    """
    hits: List[Dict[str, Any]] = []
    keys: List[HitKey] = []
    qlen = len(query)
    for version in versions:
        number = version["number"]
        text = version["content"]
        effective = effective_by_version.get(version["id"], {})
        for start in find_literal_hits(text, query):
            end = start + qlen
            hits.append(
                {
                    "version_number": number,
                    "start": start,
                    "end": end,
                    "context": build_context(text, start, end, context_radius),
                    "annotations": _intersecting(effective, start, end),
                }
            )
            keys.append((number, start, end))
    return hits, keys


def select_page(
    hits: List[Dict[str, Any]],
    keys: List[HitKey],
    after: Optional[HitKey],
) -> Tuple[List[Dict[str, Any]], Optional[HitKey], bool]:
    """键集分页：返回排序键严格大于 ``after`` 的至多 ``PAGE_SIZE`` 个命中。

    返回 ``(page, last_key, has_more)``：``has_more`` 表示之后仍有命中
    （应签发续页游标）。全序键 + 二分定位，不依赖游标中的行下标。
    """
    begin = 0 if after is None else bisect_right(keys, after)
    page = hits[begin : begin + PAGE_SIZE]
    has_more = begin + PAGE_SIZE < len(hits)
    last_key = keys[begin + len(page) - 1] if page else None
    return page, last_key, has_more


# ---------------------------------------------------------------------------
# 游标编解码（HMAC-SHA256 签名，防篡改 / 跨文档 / 筛选篡改）
# ---------------------------------------------------------------------------


def _canonical(payload: Dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def encode_cursor(payload: Dict[str, Any], secret: bytes) -> str:
    """签名并编码：Base64URL({v:1, d:<载荷>, s:<HMAC-SHA256 十六进制>})。"""
    envelope = {
        "v": CURSOR_VERSION,
        "d": payload,
        "s": hmac.new(secret, _canonical(payload), hashlib.sha256).hexdigest(),
    }
    raw = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_page_cursor(
    document_id: int,
    query: str,
    from_version: int,
    to_version: int,
    context_radius: int,
    wm: Watermark,
    after: HitKey,
    secret: bytes,
    to_default: bool = False,
) -> str:
    """续页游标：冻结筛选条件、版本上界、快照水位与最后排序键。

    ``to_version`` 为实际检索上界（缺省最新版时为首轮解析出的具体版本号），
    续页严格沿用；``to_default`` 标记该上界是否来自缺省，供该轮读尽时
    派生 resume 游标（缺省最新版的轮次续拉时重新取新快照最新版）。
    """
    return encode_cursor(
        {
            "kind": "page",
            "doc": document_id,
            "q": query,
            "vf": from_version,
            "vt": to_version,
            "td": bool(to_default),
            "r": context_radius,
            "wm": {table: wm[table] for table in WATERMARK_TABLES},
            "after": [after[0], after[1], after[2]],
        },
        secret,
    )


def make_resume_cursor(
    document_id: int,
    query: str,
    from_version: int,
    to_version: Optional[int],
    context_radius: int,
    consumed_wm: Watermark,
    secret: bytes,
) -> str:
    """续拉游标：一轮读尽（含空轮）时签发，携带已消费快照水位。

    ``to_version`` 为 None 表示首轮时上界即缺省最新版，续拉下一轮同样
    重新取最新版；否则携带显式上界供筛选一致性校验。
    """
    return encode_cursor(
        {
            "kind": "resume",
            "doc": document_id,
            "q": query,
            "vf": from_version,
            "vt": to_version,
            "r": context_radius,
            "wm": {table: consumed_wm[table] for table in WATERMARK_TABLES},
        },
        secret,
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_wm(value: Any) -> Watermark:
    if not isinstance(value, dict) or set(value.keys()) != set(WATERMARK_TABLES):
        raise CursorError(
            "bad_cursor_structure",
            f"游标快照水位应是恰好包含 {list(WATERMARK_TABLES)} 四个键的对象",
        )
    wm: Watermark = {}
    for table in WATERMARK_TABLES:
        v = value[table]
        if not _is_int(v) or v < 0:
            raise CursorError(
                "bad_cursor_structure",
                f"游标快照水位 wm.{table} 应为非负整数",
            )
        wm[table] = v
    return wm


def decode_cursor(token: str, secret: bytes) -> Dict[str, Any]:
    """解码并完整校验游标（不含请求级筛选一致性比对，见 :func:`match_filter`）。

    返回 ``{"kind", "doc", "q", "vf", "vt", "r", "wm", "after"}``；
    resume 游标 ``after`` 为 None。任何结构 / 签名 / 版本问题都抛
    :class:`CursorError`。
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        envelope = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise CursorError(
            "bad_cursor_structure",
            "游标无法解码：应为 Base64URL 编码的紧凑 JSON",
        )

    if not isinstance(envelope, dict) or set(envelope.keys()) != {"v", "d", "s"}:
        raise CursorError(
            "bad_cursor_structure", "游标信封应恰好包含 v / d / s 三个字段"
        )
    if not _is_int(envelope["v"]):
        raise CursorError("bad_cursor_structure", "游标版本 v 应为整数")
    if envelope["v"] != CURSOR_VERSION:
        raise CursorError(
            "bad_cursor_version",
            f"不支持的游标版本 {envelope['v']}（当前支持版本 {CURSOR_VERSION}）",
        )
    if not isinstance(envelope["s"], str):
        raise CursorError("bad_cursor_structure", "游标签名 s 应为十六进制字符串")

    data = envelope["d"]
    if not isinstance(data, dict):
        raise CursorError("bad_cursor_structure", "游标载荷 d 应为 JSON 对象")

    expected_keys = {"kind", "doc", "q", "vf", "vt", "r", "wm"}
    kind = data.get("kind")
    if kind == "page":
        expected_keys |= {"after", "td"}
    elif kind != "resume":
        raise CursorError(
            "bad_cursor_structure",
            "游标 kind 应为 'page'（续页）或 'resume'（续拉）",
        )
    if set(data.keys()) != expected_keys:
        raise CursorError(
            "bad_cursor_structure",
            f"{kind} 游标载荷字段应恰好为 {sorted(expected_keys)}",
        )

    # 先验签再读业务字段：任何载荷篡改都表现为签名不符
    signature = hmac.new(secret, _canonical(data), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, envelope["s"]):
        raise CursorError(
            "cursor_tampered",
            "游标签名校验失败：载荷内容与签发时不一致（游标可能已被篡改）",
        )

    if not _is_int(data["doc"]) or data["doc"] < 1:
        raise CursorError("bad_cursor_structure", "游标 doc 应为正整数")
    if not isinstance(data["q"], str) or not data["q"]:
        raise CursorError("bad_cursor_structure", "游标 q 应为非空字符串")
    if len(data["q"]) > MAX_QUERY_LENGTH:
        raise CursorError("bad_cursor_structure", "游标 q 超过最大检索长度")
    if not _is_int(data["vf"]) or data["vf"] < 1:
        raise CursorError("bad_cursor_structure", "游标 vf 应为 >=1 的版本号")
    if data["vt"] is not None and (not _is_int(data["vt"]) or data["vt"] < 1):
        raise CursorError("bad_cursor_structure", "游标 vt 应为 >=1 的版本号或 null")
    if not isinstance(data.get("td", False), bool):
        raise CursorError("bad_cursor_structure", "游标 td 应为布尔值")
    if data["vt"] is not None and data["vf"] > data["vt"]:
        raise CursorError(
            "bad_cursor_range",
            f"游标版本范围矛盾：vf={data['vf']} 大于 vt={data['vt']}",
        )
    if not _is_int(data["r"]) or not (0 <= data["r"] <= MAX_CONTEXT_RADIUS):
        raise CursorError(
            "bad_cursor_structure",
            f"游标 r 应为 0..{MAX_CONTEXT_RADIUS} 的上下文笔点数",
        )
    wm = _check_wm(data["wm"])

    after: Optional[HitKey] = None
    if kind == "page":
        a = data["after"]
        if not (
            isinstance(a, list)
            and len(a) == 3
            and all(_is_int(v) for v in a)
            and a[0] >= 1
            and a[1] >= 0
            and a[2] >= 1
        ):
            raise CursorError(
                "bad_cursor_structure",
                "游标 after 应为 [版本号(>=1), 起点(>=0), 终点(>=1)]",
            )
        after = (a[0], a[1], a[2])

    return {
        "kind": kind,
        "doc": data["doc"],
        "q": data["q"],
        "vf": data["vf"],
        "vt": data["vt"],
        "td": data.get("td", False),
        "r": data["r"],
        "wm": wm,
        "after": after,
    }


def ensure_within(wm: Watermark, ceiling: Watermark) -> None:
    """续页冻结水位不能超过当前库最大值（游标来自其他库等情况 → 422）。"""
    for table in WATERMARK_TABLES:
        if wm[table] > ceiling[table]:
            raise CursorError(
                "bad_cursor_range",
                f"游标快照水位 wm.{table}={wm[table]} 超过当前最大行 ID "
                f"{ceiling[table]}（游标可能来自另一个数据库）",
            )
