"""可离线往返的审阅包：创建冻结、离线填写与回传应用。

**创建**（:func:`build_package`）在调用方持有的**同一个 SQLite 读事务**
（同一快照）中冻结指定批注的：

- 最新版上的迁移记录（位置 / 状态 / 候选范围 / 顺延引用 / 既有确认）；
- 完成状态（是否已有完成记录）；
- 全部传递前置批注的完成状态（含自动收录进同包的未完成传递前置项）。

只收录当时满足下列任一条件的条目：**可确认**（最新位置是非顺延、未确认
的歧义记录）、**可完成**（未完成且无未完成传递前置项），或**能由同包
前置项解锁**（其未完成传递前置项全部被收录进同一审阅包）；已完成且
不可确认的请求批注不收录（以 ``excluded`` 说明）。冻结内容规范化后
计算 SHA-256 摘要，随 ``package_id`` 一起返回并由调用方落库。

**离线填写**（:func:`collect_decision_errors`）完全不查数据库：审阅人拿到包
后离线逐项给出 ``confirm``（选定候选范围）或 ``complete``（标记完成）
决定；本函数只依据冻结包检查决定的结构与合法性（重复 / 多余 / 不可
确认 / 候选不符 / 缺决定解锁等），全部问题一次性带 ``decisions[i]`` /
``items[..]`` 定位返回，不写入任何数据。

**回传**（:func:`apply_package`）在调用方持有的**同一个 SQLite 写事务**
（BEGIN IMMEDIATE）中：

1. 逐项比对冻结快照与当前库：目标已被确认、完成状态变化（已完成 ↔
   未完成）、或新增版本使最新迁移记录位置改变（版本 / 行 ID / 状态 /
   范围 / 顺延引用任一变化）→ 整包 **409 冲突**，冲突逐条可定位，
   不写入任何确认或完成；
2. 无冲突时按包内依赖顺序应用决定：先按批注 ID 升序追加人工确认，
   再按前置 → 后续的拓扑顺序追加完成，后项可由同包前项解锁；任一
   决定非法（例如后项在其前置决定之后仍被阻塞）→ 全部回滚；
3. 成功后把首次结果写回包行；同一 ``package_id`` 的再次回传由
   :func:`replay_result` 原样返回首次结果，不重复写入。

摘要算法与迁移归档一致：规范化 JSON（键按字典序、紧凑分隔、UTF-8
不转义）的 SHA-256。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from .review import build_prereq_map, transitive_prerequisites

PACKAGE_FORMAT = "annotation-review-package/1"


class PackageError(Exception):
    """审阅包请求的一条可定位校验错误（路由层转为 HTTP 422）。

    - ``error``：机器可读错误码；
    - ``message``：含业务 ID 的中文说明；
    - ``path``：JSON 路径式定位（如 ``decisions[2]``、``items[1]``）；
    - ``annotation_id``：相关批注 ID（无则为 ``None``）。
    """

    def __init__(self, error: str, message: str, path: str, annotation_id=None):
        super().__init__(message)
        self.error = error
        self.message = message
        self.path = path
        self.annotation_id = annotation_id

    def to_dict(self) -> Dict[str, Any]:
        out = {"error": self.error, "message": self.message, "path": self.path}
        if self.annotation_id is not None:
            out["annotation_id"] = self.annotation_id
        return out


class PackageConflict(Exception):
    """回传时冻结快照与当前库不一致的一条冲突（路由层转为 HTTP 409）。

    冲突只针对包内条目，整包任一冲突都不写入；``path`` 定位到
    ``items[i]``，``annotation_id`` 给出业务批注 ID。
    """

    def __init__(self, error: str, message: str, path: str, annotation_id: int):
        super().__init__(message)
        self.error = error
        self.message = message
        self.path = path
        self.annotation_id = annotation_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": self.error,
            "message": self.message,
            "path": self.path,
            "annotation_id": self.annotation_id,
        }


# ---------------------------------------------------------------------------
# 摘要（与 app.archive 同算法：sort_keys + 紧凑分隔 + UTF-8 不转义）
# ---------------------------------------------------------------------------


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_digest(payload: Dict[str, Any]) -> str:
    return _sha256(canonical_json(payload))


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    """与 SQLite 默认 ``strftime('%Y-%m-%dT%H:%M:%fZ','now')`` 同形的
    UTC 毫秒时间戳（``YYYY-MM-DDTHH:MM:SS.mmmZ``）。"""
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# 创建：在调用方持有的读事务内冻结快照
# ---------------------------------------------------------------------------


def _document_edges(
    con: sqlite3.Connection, document_id: int
) -> List[Tuple[int, int]]:
    return [
        (r["prerequisite_id"], r["dependent_id"])
        for r in con.execute(
            "SELECT prerequisite_id, dependent_id FROM dependencies "
            "WHERE document_id = ? ORDER BY id",
            (document_id,),
        ).fetchall()
    ]


def _document_completed(con: sqlite3.Connection, document_id: int) -> Set[int]:
    return {
        r["annotation_id"]
        for r in con.execute(
            "SELECT annotation_id FROM completions WHERE document_id = ?",
            (document_id,),
        ).fetchall()
    }


def _latest_locations(
    con: sqlite3.Connection, annotation_ids: List[int]
) -> Dict[int, sqlite3.Row]:
    """每条批注在其文档最新版上的迁移记录。

    locations 对每个 (annotation_id, version_id) 唯一，且版本号随
    version_id 单调（版本按提交顺序分配自增 ID 与递增编号），因此
    该批注行 ID 最大的 location 即最新版上的记录。
    """
    if not annotation_ids:
        return {}
    placeholders = ",".join("?" for _ in annotation_ids)
    rows = con.execute(
        f"""
        SELECT l.*, v.number AS version_number
        FROM locations l
        JOIN versions v ON v.id = l.version_id
        WHERE l.id IN (
            SELECT MAX(id) FROM locations
            WHERE annotation_id IN ({placeholders})
            GROUP BY annotation_id
        )
        """,
        annotation_ids,
    ).fetchall()
    return {r["annotation_id"]: r for r in rows}


def _confirmation_by_location(
    con: sqlite3.Connection, location_ids: List[int]
) -> Dict[int, sqlite3.Row]:
    if not location_ids:
        return {}
    placeholders = ",".join("?" for _ in location_ids)
    rows = con.execute(
        f"SELECT * FROM confirmations WHERE location_id IN ({placeholders})",
        location_ids,
    ).fetchall()
    # 唯一索引保证每个 location 至多一条；保留 ORDER BY 仅为防御性确定
    return {r["location_id"]: r for r in rows}


def _is_confirmable(loc: Optional[sqlite3.Row], confirmation) -> bool:
    """最新位置可确认：非顺延的真实歧义记录且尚未人工确认。"""
    return (
        loc is not None
        and loc["status"] == "ambiguous"
        and loc["carried_from"] is None
        and confirmation is None
    )


def _freeze_location(loc: sqlite3.Row, conf: Optional[sqlite3.Row]) -> Dict[str, Any]:
    return {
        "location_id": loc["id"],
        "version_id": loc["version_id"],
        "version_number": loc["version_number"],
        "status": loc["status"],
        "start": loc["start"],
        "end": loc["end"],
        "left_match": loc["left_match"],
        "right_match": loc["right_match"],
        "score": loc["score"],
        "candidates": json.loads(loc["candidates"]),
        "carried_from": loc["carried_from"],
        "confirmation": (
            None
            if conf is None
            else {
                "confirmation_id": conf["id"],
                "start": conf["start"],
                "end": conf["end"],
                "note": conf["note"],
                "created_at": conf["created_at"],
            }
        ),
        "created_at": loc["created_at"],
    }


def build_package(
    con: sqlite3.Connection,
    document_id: int,
    requested_ids: List[int],
    *,
    note: Optional[str] = None,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """在调用方持有的读事务内冻结审阅包内容（不落库）。

    假设文档存在（由调用方在同一事务内先校验）。``requested_ids`` 中
    重复、不存在或跨文档的批注由调用方先行校验；本函数负责收录范围：

    - 最新位置可确认的请求批注 → 收录（即使已完成，确认只追加且仍可补）；
    - 已完成且不可确认的请求批注 → 不收录（excluded / already_completed）；
    - 未完成的请求批注 → 连同其全部未完成传递前置项一起收录
      （DAG 上这些前置项必然可完成或可由同包前置项解锁）。
    """
    requested = set(requested_ids)

    ann_rows = {
        r["id"]: r
        for r in con.execute(
            "SELECT * FROM annotations WHERE document_id = ? ORDER BY id",
            (document_id,),
        ).fetchall()
    }
    prereqs = build_prereq_map(_document_edges(con, document_id))
    completed = _document_completed(con, document_id)

    # 传递闭包：请求项 + 其全部传递前置项
    closure: Set[int] = set()
    for aid in requested_ids:
        closure.add(aid)
        closure.update(transitive_prerequisites(prereqs, aid))

    candidate_ids = sorted(closure)
    latest = _latest_locations(con, candidate_ids)
    confs = _confirmation_by_location(
        con, [loc["id"] for loc in latest.values()]
    )

    def confirmable(aid: int) -> bool:
        loc = latest.get(aid)
        return _is_confirmable(loc, confs.get(loc["id"]) if loc else None)

    included: Set[int] = set()
    for aid in candidate_ids:
        if aid not in ann_rows:  # 防御：跨文档 / 不存在由调用方先校验
            continue
        if confirmable(aid):
            included.add(aid)  # 可确认（已完成的歧义也允许补确认）
        elif aid not in completed:
            included.add(aid)  # 可完成，或可由同包前置项解锁
        # 已完成且不可确认：不收录

    excluded = []
    for aid in sorted(requested):
        if aid not in included:
            if aid in completed:
                excluded.append(
                    {
                        "annotation_id": aid,
                        "reason": "already_completed",
                        "message": f"批注 {aid} 已完成且最新位置无需确认，"
                        "不收录进审阅包",
                    }
                )
            else:
                # 理论不可达：未完成项的传递闭包必可在 DAG 上完成
                excluded.append(
                    {
                        "annotation_id": aid,
                        "reason": "not_actionable",
                        "message": f"批注 {aid} 在冻结时既不可确认也无法由同包"
                        "前置项解锁，不收录进审阅包",
                    }
                )

    items: List[Dict[str, Any]] = []
    # 每个已收录条目的全部传递前置（roles 与前置状态共用，避免重复遍历）
    transitive_of: Dict[int, Set[int]] = {
        aid: transitive_prerequisites(prereqs, aid) for aid in included
    }

    def _roles_for(aid: int) -> List[str]:
        """按固定顺序计算条目标签（顺序参与规范化摘要，必须稳定）。"""
        roles: List[str] = []
        if aid in requested:
            roles.append("requested")
        # 被包内其他已收录条目作为传递前置依赖
        if any(aid in transitive_of[other] for other in included if other != aid):
            roles.append("prerequisite")
        return roles

    # 全部传递前置状态（并集）：每条传递前置的完成状态与是否收录进同包。
    # 收录的前置同时出现在 items（带完整最新位置），这里给出全量的
    # 「completed / included」状态供离线审阅人核对解锁依据。
    all_transitive: Set[int] = set()
    for anc in transitive_of.values():
        all_transitive.update(anc)
    prerequisite_states: Dict[str, Dict[str, Any]] = {}
    for pre in sorted(all_transitive):
        prerequisite_states[str(pre)] = {
            "completed": pre in completed,
            "included": pre in included,
        }

    for aid in sorted(included):
        loc = latest.get(aid)
        conf = confs.get(loc["id"]) if loc else None
        item: Dict[str, Any] = {
            "annotation_id": aid,
            "body": ann_rows[aid]["body"],
            "selection": ann_rows[aid]["selection"],
            "roles": _roles_for(aid),
            "prerequisite_ids": sorted(prereqs.get(aid, ())),
            "completed": aid in completed,
            "latest_location": (
                _freeze_location(loc, conf) if loc is not None else None
            ),
        }
        items.append(item)

    frozen_at = created_at or _utc_now()
    payload = {
        "format": PACKAGE_FORMAT,
        "document_id": document_id,
        "note": note,
        "created_at": frozen_at,
        "latest_version_number": (
            con.execute(
                "SELECT COALESCE(MAX(number), 0) AS m FROM versions "
                "WHERE document_id = ?",
                (document_id,),
            ).fetchone()["m"]
        ),
        "items": items,
        "prerequisite_states": prerequisite_states,
        "excluded": excluded,
    }
    return payload


def new_package_id() -> str:
    """随机 package_id（32 位十六进制，与内容无关：同内容两次创建是两个包）。"""
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# 离线填写：只依据冻结包校验决定（不查库、不写入）
# ---------------------------------------------------------------------------


def _items_by_id(payload: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    return {item["annotation_id"]: item for item in payload["items"]}


def collect_decision_errors(
    payload: Dict[str, Any], decisions: List[Dict[str, Any]]
) -> List[PackageError]:
    """只依据冻结包检查全部决定，返回可定位错误列表（空 = 全部合法）。"""
    errors: List[PackageError] = []
    items = _items_by_id(payload)
    by_annotation: Dict[int, Dict[str, Any]] = {}

    for i, decision in enumerate(decisions):
        path = f"decisions[{i}]"
        aid = decision.get("annotation_id")
        action = decision.get("action")

        if aid in by_annotation:
            errors.append(
                PackageError(
                    "duplicate_decision",
                    f"批注 {aid} 在决定列表中出现多次（每条批注至多一个决定）",
                    path,
                    aid,
                )
            )
            continue
        by_annotation[aid] = decision

        item = items.get(aid)
        if item is None:
            errors.append(
                PackageError(
                    "decision_not_in_package",
                    f"批注 {aid} 不在审阅包收录条目中（回传决定只能针对包内条目）",
                    path,
                    aid,
                )
            )
            continue

        if action == "confirm":
            loc = item["latest_location"]
            if not (
                loc is not None
                and loc["status"] == "ambiguous"
                and loc["carried_from"] is None
                and loc["confirmation"] is None
            ):
                errors.append(
                    PackageError(
                        "not_confirmable",
                        f"批注 {aid} 冻结时最新位置不可确认（仅非顺延、未确认的"
                        "歧义记录可确认），不能给出 confirm 决定",
                        path,
                        aid,
                    )
                )
                continue
            start, end = decision.get("start"), decision.get("end")
            if not isinstance(start, int) or not isinstance(end, int):
                errors.append(
                    PackageError(
                        "missing_candidate_range",
                        f"批注 {aid} 的 confirm 决定必须给出候选范围 start / end"
                        "（非负整数，半开区间）",
                        path,
                        aid,
                    )
                )
                continue
            if not any(
                c["start"] == start and c["end"] == end for c in loc["candidates"]
            ):
                candidate_ranges = [[c["start"], c["end"]] for c in loc["candidates"]]
                errors.append(
                    PackageError(
                        "confirmation_not_candidate",
                        f"批注 {aid} 的 confirm 决定所选范围 [{start}, {end}) "
                        "不在冻结候选落点中，候选范围为 "
                        + json.dumps(candidate_ranges, ensure_ascii=False),
                        path,
                        aid,
                    )
                )
        elif action == "complete":
            if item["completed"]:
                errors.append(
                    PackageError(
                        "already_completed",
                        f"批注 {aid} 在包冻结时已有完成记录，无需 complete 决定"
                        "（完成记录只追加、重复提交幂等）",
                        path,
                        aid,
                    )
                )
        else:
            errors.append(
                PackageError(
                    "invalid_action",
                    f"批注 {aid} 的决定 action={action!r} 非法，"
                    "只支持 'confirm' 或 'complete'",
                    path,
                    aid,
                )
            )

    # 完成决定必须自洽：任一 complete 项在包内仍有未完成传递前置时，
    # 那些前置要么已在冻结前完成（不该出现在包内），要么必须在同包
    # 决定中一并 complete（后项由前项解锁）。依赖只看包内条目。
    prereqs = _package_prereqs(payload)
    completing = {aid for aid, d in by_annotation.items() if d.get("action") == "complete"}
    for aid in sorted(completing):
        if aid not in items or any(e.annotation_id == aid for e in errors):
            continue
        item = items[aid]
        missing = []
        for pre in sorted(transitive_prerequisites(prereqs, aid)):
            pre_item = items.get(pre)
            if pre_item is None:
                continue  # 冻结前已完成的前置（prerequisite_states），不阻塞
            if pre_item["completed"]:
                continue  # 冻结时已有完成记录
            if pre not in completing:
                missing.append(pre)
        if missing:
            idx = _item_index(payload, aid)
            errors.append(
                PackageError(
                    "missing_prerequisite_decisions",
                    f"批注 {aid} 的 complete 决定无法在同包内解锁：传递前置 "
                    f"{missing} 既未在冻结前完成，也未在本包决定中一并完成",
                    f"items[{idx}]",
                    aid,
                )
            )

    return errors


def _item_index(payload: Dict[str, Any], annotation_id: int) -> int:
    for i, item in enumerate(payload["items"]):
        if item["annotation_id"] == annotation_id:
            return i
    return -1


def _item_path(payload: Dict[str, Any], annotation_id: int) -> str:
    return f"items[{_item_index(payload, annotation_id)}]"


def _package_prereqs(payload: Dict[str, Any]) -> Dict[int, Set[int]]:
    """依据冻结条目重建「包内可见」的直接前置映射。

    每个条目在创建时冻结了全部直接前置（``prerequisite_ids``）；这里
    只保留同样收录进包的前置——未收录的前置在冻结前已完成
    （见 ``prerequisite_states``），不构成阻塞。
    """
    ids = [item["annotation_id"] for item in payload["items"]]
    id_set = set(ids)
    prereqs: Dict[int, Set[int]] = {aid: set() for aid in ids}
    for item in payload["items"]:
        aid = item["annotation_id"]
        for pre in item.get("prerequisite_ids", ()):
            if pre in id_set:
                prereqs[aid].add(pre)
    return prereqs


# ---------------------------------------------------------------------------
# 回传：冲突检测与同写事务按依赖顺序应用
# ---------------------------------------------------------------------------


def detect_conflicts(
    con: sqlite3.Connection,
    document_id: int,
    payload: Dict[str, Any],
    decisions_by_annotation: Dict[int, Dict[str, Any]],
) -> List[PackageConflict]:
    """在调用方持有的写事务（已取写锁）中逐项比对快照与当前库。

    冲突（整包任一存在即不写入）：

    - ``latest_location_changed``：新增版本等使最新迁移记录改变
      （版本 / 行 ID / 状态 / 范围 / 顺延引用）——所有条目都检查；
    - ``already_confirmed``：confirm 决定的目标冻结后被他人确认；
    - ``completion_state_changed``：条目完成状态与冻结时不一致；
    - ``external_blocker``：依赖边冻结后新增，complete 决定被包外
      未完成前置阻塞（快照不含该解锁依据）。
    """
    conflicts: List[PackageConflict] = []
    item_ids = [item["annotation_id"] for item in payload["items"]]
    latest = _latest_locations(con, item_ids)
    confs = _confirmation_by_location(con, [loc["id"] for loc in latest.values()])
    completed_now = _document_completed(con, document_id)
    edges_now = _document_edges(con, document_id)
    prereqs_now = build_prereq_map(edges_now)

    for i, item in enumerate(payload["items"]):
        aid = item["annotation_id"]
        path = f"items[{i}]"
        frozen_loc = item["latest_location"]
        cur_loc = latest.get(aid)
        cur_conf = confs.get(cur_loc["id"]) if cur_loc else None

        loc_changed = _location_signature(frozen_loc) != _location_signature(
            _freeze_location(cur_loc, cur_conf) if cur_loc is not None else None
        )
        if loc_changed:
            conflicts.append(
                PackageConflict(
                    "latest_location_changed",
                    _location_change_message(aid, frozen_loc, cur_loc, cur_conf),
                    path,
                    aid,
                )
            )

        was_completed = bool(item["completed"])
        is_completed = aid in completed_now
        if was_completed != is_completed:
            conflicts.append(
                PackageConflict(
                    "completion_state_changed",
                    f"批注 {aid} 的完成状态已变化：冻结时"
                    f"{'已完成' if was_completed else '未完成'}，"
                    f"当前{'已完成' if is_completed else '未完成'}，"
                    "审阅包基于过期快照，请重新创建",
                    path,
                    aid,
                )
            )

        decision = decisions_by_annotation.get(aid)
        if decision is not None and decision.get("action") == "confirm":
            frozen_conf = frozen_loc.get("confirmation") if frozen_loc else None
            current_conf = (
                {
                    "start": cur_conf["start"],
                    "end": cur_conf["end"],
                    "note": cur_conf["note"],
                    "created_at": cur_conf["created_at"],
                }
                if cur_conf is not None
                else None
            )
            if not loc_changed and frozen_conf is None and current_conf is not None:
                conflicts.append(
                    PackageConflict(
                        "already_confirmed",
                        f"批注 {aid} 的歧义记录在包冻结后已被人工确认"
                        f"（范围 [{current_conf['start']}, {current_conf['end']})，"
                        f"备注 {current_conf['note']!r}），确认只追加不可更改",
                        path,
                        aid,
                    )
                )

        if decision is not None and decision.get("action") == "complete" and not is_completed:
            blockers = sorted(
                pre
                for pre in transitive_prerequisites(prereqs_now, aid)
                if pre not in completed_now and pre not in _completing_ids(decisions_by_annotation)
            )
            if blockers:
                conflicts.append(
                    PackageConflict(
                        "external_blocker",
                        f"批注 {aid} 的 complete 决定当前被包外未完成前置 "
                        f"{blockers} 阻塞（依赖在包冻结后新增），"
                        "审阅包基于过期快照，请重新创建",
                        path,
                        aid,
                    )
                )

    return conflicts


def _completing_ids(decisions_by_annotation: Dict[int, Dict[str, Any]]) -> Set[int]:
    return {
        aid
        for aid, d in decisions_by_annotation.items()
        if d.get("action") == "complete"
    }


def _location_signature(loc: Optional[Dict[str, Any]]) -> Any:
    """最新位置的冲突指纹：版本 / 行 ID / 状态 / 范围 / 顺延引用。

    确认以独立行存在（由 already_confirmed 单独检测），且确认不改写
    locations 行，故不纳入位置指纹（否则补确认的包内已完成项会被误报）。
    """
    if loc is None:
        return None
    return (
        loc["version_id"],
        loc["version_number"],
        loc["location_id"],
        loc["status"],
        loc["start"],
        loc["end"],
        loc["carried_from"],
    )


def _location_change_message(aid, frozen_loc, cur_loc, cur_conf) -> str:
    if frozen_loc is None:
        return (
            f"批注 {aid} 冻结时没有最新迁移记录，当前已产生记录"
            f"（第 {cur_loc['version_number']} 版，状态 {cur_loc['status']}），"
            "最新位置已改变，请重新创建审阅包"
        )
    if cur_loc is None:
        return (
            f"批注 {aid} 冻结时最新位置在第 {frozen_loc['version_number']} 版，"
            "当前已无迁移记录，请重新创建审阅包"
        )
    details = []
    if cur_loc["version_number"] != frozen_loc["version_number"]:
        details.append(
            f"最新版本由第 {frozen_loc['version_number']} 版变为第 "
            f"{cur_loc['version_number']} 版（新增版本改变了最新位置）"
        )
    if cur_loc["status"] != frozen_loc["status"]:
        details.append(f"状态由 {frozen_loc['status']} 变为 {cur_loc['status']}")
    if (cur_loc["start"], cur_loc["end"]) != (frozen_loc["start"], frozen_loc["end"]):
        details.append(
            f"范围由 [{frozen_loc['start']}, {frozen_loc['end']}) 变为 "
            f"[{cur_loc['start']}, {cur_loc['end']})"
        )
    if cur_loc["carried_from"] != frozen_loc["carried_from"]:
        details.append(
            f"顺延引用由 {frozen_loc['carried_from']} 变为 {cur_loc['carried_from']}"
        )
    if cur_loc["id"] != frozen_loc["location_id"]:
        details.append(
            f"迁移记录行由 {frozen_loc['location_id']} 变为 {cur_loc['id']}"
        )
    return (
        f"批注 {aid} 的最新位置已变化：{'；'.join(details) or '迁移记录已替换'}，"
        "审阅包基于过期快照，请重新创建"
    )


def _ordered_completions(
    prereqs: Dict[int, Set[int]],
    completed: Set[int],
    completing: Set[int],
) -> List[int]:
    """complete 决定按前置 → 后续排序（Kahn；同层按批注 ID 升序）。

    就绪条件：全部直接前置都已完成——要么冻结前 / 此前批次已完成
    （``completed``），要么是本批已排入更前层的决定。图无环
    （依赖写入时已检测），剩余无法排序意味着冻结后新增边构成阻塞，
    返回的结果将少于 ``completing``，由调用方在写事务内报错回滚。
    """
    done = set(completed)
    remaining = set(completing)
    result: List[int] = []
    while remaining:
        ready = sorted(
            aid for aid in remaining if prereqs.get(aid, set()) <= done
        )
        if not ready:
            return result  # 调用方按 set(ordered) != completing 判定冲突
        result.extend(ready)
        done.update(ready)
        remaining.difference_update(ready)
    return result


def apply_package(
    con: sqlite3.Connection,
    document_id: int,
    payload: Dict[str, Any],
    package_id: str,
    decisions: List[Dict[str, Any]],
    *,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """在调用方持有的写事务内应用回传决定并写回首次结果。

    调用顺序：调用方先 BEGIN IMMEDIATE → 查重放包行 →
    :func:`collect_decision_errors`（离线，已在事务外完成）→
    :func:`detect_conflicts`（本事务内）→ 本函数。

    确认按批注 ID 升序插入；完成按包内依赖拓扑顺序插入，后项可由
    前项解锁。任一插入时发现状态非法（完成仍被阻塞、歧义已被确认、
    候选已不在记录中）都抛 :class:`PackageError` / :class:`PackageConflict`，
    由调用方回滚整个事务。
    """
    decisions_by_annotation = {d["annotation_id"]: d for d in decisions}
    items = _items_by_id(payload)

    edges = _document_edges(con, document_id)
    prereqs = build_prereq_map(edges)
    completed = _document_completed(con, document_id)

    def _is_blocked(aid: int, completed_set: Set[int]) -> bool:
        return aid not in completed_set and any(
            pre not in completed_set
            for pre in transitive_prerequisites(prereqs, aid)
        )

    blocked_before = {aid for aid in items if _is_blocked(aid, completed)}

    applied_confirmations: List[Dict[str, Any]] = []
    # 1) 人工确认：按批注 ID 升序（确认之间无依赖，顺序仅为结果稳定）
    for aid in sorted(
        a for a, d in decisions_by_annotation.items() if d.get("action") == "confirm"
    ):
        decision = decisions_by_annotation[aid]
        item = items[aid]
        loc_row = con.execute(
            "SELECT * FROM locations WHERE id = ?",
            (item["latest_location"]["location_id"],),
        ).fetchone()
        if loc_row is None:
            raise PackageConflict(
                "latest_location_changed",
                f"批注 {aid} 的冻结迁移记录已不存在",
                _item_path(payload, aid),
                aid,
            )
        existing = con.execute(
            "SELECT * FROM confirmations WHERE location_id = ? ORDER BY id",
            (loc_row["id"],),
        ).fetchall()
        if existing:
            raise PackageConflict(
                "already_confirmed",
                f"批注 {aid} 的歧义记录已被人工确认，确认只追加不可更改",
                _item_path(payload, aid),
                aid,
            )
        if loc_row["status"] != "ambiguous" or loc_row["carried_from"] is not None:
            raise PackageConflict(
                "latest_location_changed",
                f"批注 {aid} 的目标记录已不是非顺延歧义记录"
                f"（status={loc_row['status']}）",
                _item_path(payload, aid),
                aid,
            )
        candidates = json.loads(loc_row["candidates"])
        if not any(
            c["start"] == decision["start"] and c["end"] == decision["end"]
            for c in candidates
        ):
            raise PackageError(
                "confirmation_not_candidate",
                f"批注 {aid} 所选范围 [{decision['start']}, {decision['end']}) "
                "不在当前候选落点中",
                _item_path(payload, aid),
                aid,
            )
        cur = con.execute(
            'INSERT INTO confirmations (location_id, start, "end", note)'
            " VALUES (?, ?, ?, ?)",
            (
                loc_row["id"],
                decision["start"],
                decision["end"],
                decision.get("note"),
            ),
        )
        conf_row = con.execute(
            "SELECT * FROM confirmations WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        applied_confirmations.append(
            {
                "annotation_id": aid,
                "confirmation_id": conf_row["id"],
                "location_id": loc_row["id"],
                "start": conf_row["start"],
                "end": conf_row["end"],
            }
        )

    # 2) 完成：按当前库依赖图的拓扑顺序，后项可由同批前项解锁。
    #    依赖边在本事务中不会新增（回传只写 confirmations / completions），
    #    prereqs 沿用应用前快照；完成集合在每个插入后增量更新。
    completing = _completing_ids(decisions_by_annotation)
    ordered = _ordered_completions(prereqs, completed, completing)
    if set(ordered) != completing:
        still = sorted(completing - set(ordered))
        raise PackageConflict(
            "external_blocker",
            f"批注 {still} 的完成决定在应用时仍被未完成前置阻塞"
            "（依赖在包冻结后变化），整批回滚",
            _item_path(payload, still[0]) if still else "items",
            still[0] if still else -1,
        )

    applied_completions: List[Dict[str, Any]] = []
    for aid in ordered:
        decision = decisions_by_annotation[aid]
        existing = con.execute(
            "SELECT * FROM completions WHERE annotation_id = ?", (aid,)
        ).fetchone()
        if existing is not None:
            # 冲突检测应已拦截；兜底保证不产生重复行
            raise PackageConflict(
                "completion_state_changed",
                f"批注 {aid} 已存在完成记录",
                _item_path(payload, aid),
                aid,
            )
        incomplete = sorted(
            pre for pre in transitive_prerequisites(prereqs, aid) if pre not in completed
        )
        if incomplete:
            raise PackageConflict(
                "external_blocker",
                f"批注 {aid} 被阻塞：存在尚未完成的前置批注 {incomplete}",
                _item_path(payload, aid),
                aid,
            )
        cur = con.execute(
            "INSERT INTO completions (document_id, annotation_id, note)"
            " VALUES (?, ?, ?)",
            (document_id, aid, decision.get("note")),
        )
        row = con.execute(
            "SELECT * FROM completions WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        completed.add(aid)
        applied_completions.append(
            {
                "annotation_id": aid,
                "completion_id": row["id"],
                "note": row["note"],
            }
        )

    # 3) 立即解锁：应用前被阻塞、应用后可处理的批注（含包外同文档批注）
    ann_ids = [
        r["id"]
        for r in con.execute(
            "SELECT id FROM annotations WHERE document_id = ?", (document_id,)
        ).fetchall()
    ]
    actionable_after = {
        aid
        for aid in ann_ids
        if aid not in completed
        and all(pre in completed for pre in transitive_prerequisites(prereqs, aid))
    }
    unlocked = sorted(aid for aid in blocked_before if aid in actionable_after)

    result = {
        "package_id": package_id,
        "document_id": document_id,
        "applied_at": now or _utc_now(),
        "confirmation_count": len(applied_confirmations),
        "completion_count": len(applied_completions),
        "confirmations": sorted(applied_confirmations, key=lambda x: x["annotation_id"]),
        "completions": applied_completions,
        "unlocked_annotation_ids": unlocked,
    }
    con.execute(
        "UPDATE review_packages SET applied_at = ?, result_json = ? WHERE id = ?",
        (result["applied_at"], json.dumps(result, ensure_ascii=False), package_id),
    )
    return result


def replay_result(row: sqlite3.Row) -> Optional[Dict[str, Any]]:
    """已成功应用的包：返回首次结果（调用方加 ``already_applied=true``）。"""
    if row["applied_at"] is None:
        return None
    return json.loads(row["result_json"])
