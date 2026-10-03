"""文档审阅发布候选：同读事务冻结 → 同写事务确认发布。

**创建**（:func:`build_candidate`）由调用方在**同一个 SQLite 读事务**
（同一快照）中调用，一次性固定：

- 目标文档版本（版本号缺省取最新版）；
- 门禁策略修订（名称 + revision，revision 缺省取最新修订）与其策略树；
- 该快照上评估出的门禁**解释树**（与策略树同形，不短路）；
- 当前审阅状态（actionable / blocked / completed 的批注 ID 集合）；
- 该文档六类只追加事件（版本 / 批注 / 迁移位置 / 人工确认 / 依赖 /
  完成记录）的**单文档水位向量**——各表中属于本文档的行的最大 ID。

初始门禁未通过时由调用方直接拒绝（:class:`GateRejected`），**不生成
candidate_id、不落候选行**；门禁通过后，冻结内容规范化计算 SHA-256
摘要（算法与迁移归档、审阅包一致），并由 :func:`new_candidate_id` 生成
随机 ``candidate_id``。candidate_id、摘要与冻结内容一经创建不可变。

**确认**（:func:`detect_confirm_conflicts` + :func:`release_candidate`）
由调用方在**同一个写事务**（BEGIN IMMEDIATE）中先重读候选行、再重读
水位与门禁快照：

- 候选创建后该文档六类事件中任一类的水位上涨（出现新的版本、批注、
  迁移、确认、依赖或完成记录）→ ``watermark_changed`` 冲突，逐表可定位；
- 冻结的策略修订 / 目标版本在当前快照上门禁不再通过
  → ``gate_not_passed`` 冲突，附当前解释树；

任一冲突都不改变候选状态、不写入发布结果。仅当六类水位全部未变且门禁
仍通过时，:func:`release_candidate` 追加一次 ``released`` 结果
（released_at / result_json 只写一次）。同一 ``candidate_id`` 重复确认
由 :func:`replay_result` 原样返回首次结果，不重复写入。

水位取**单文档**水位（:func:`app.events.document_watermark`）而非全表
水位：其他文档的写入不会误报冲突；六类表对每文档只追加，行 ID 单调，
因此「该文档六类水位均未变」与「门禁输入未变」在本服务的全部写入路径
下等价（水位未变时复评门禁必然仍通过——复评为防御性兜底，也防直接改库）。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from . import events as events_mod
from . import gate as gate_mod
from .review import build_prereq_map, transitive_prerequisites

CANDIDATE_FORMAT = "document-release-candidate/1"


class CandidateError(Exception):
    """发布候选请求的可定位校验错误（路由层转为 HTTP 4xx）。

    - ``error``：机器可读错误码；
    - ``message``：含业务 ID 的中文说明；
    - ``path``：JSON 路径式定位（无则为 ``None``）。
    """

    def __init__(self, error: str, message: str, path: Optional[str] = None,
                 status_code: int = 422):
        super().__init__(message)
        self.error = error
        self.message = message
        self.path = path
        self.status_code = status_code

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"error": self.error, "message": self.message}
        if self.path is not None:
            out["path"] = self.path
        return out


class GateRejected(CandidateError):
    """创建时门禁初始评估未通过（路由层转为 409，不落候选）。

    携带冻结时的策略修订、目标版本与解释树，供调用方原样返回。
    """

    def __init__(self, message: str, policy_name: str, policy_revision: int,
                 version_number: int, tree: Dict[str, Any]):
        super().__init__("gate_not_passed", message, None, status_code=409)
        self.policy_name = policy_name
        self.policy_revision = policy_revision
        self.version_number = version_number
        self.tree = tree


class CandidateConflict(Exception):
    """确认时发现的一条冲突（路由层转为 HTTP 409）。

    - ``error``：``watermark_changed`` / ``gate_not_passed``；
    - ``path``：水位冲突定位到 ``watermark.<表名>``，门禁冲突为 ``gate``；
    - ``message``：中文说明。
    """

    def __init__(self, error: str, message: str, path: str, extra: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.error = error
        self.message = message
        self.path = path
        self.extra = extra

    def to_dict(self) -> Dict[str, Any]:
        out = {"error": self.error, "message": self.message, "path": self.path}
        if self.extra:
            out.update(self.extra)
        return out


# ---------------------------------------------------------------------------
# 摘要 / ID / 时间（与 review_packages / archive 同算法）
# ---------------------------------------------------------------------------


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_digest(payload: Dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def new_candidate_id() -> str:
    """随机 candidate_id（32 位十六进制，与内容无关：同内容两次创建是两个候选）。"""
    return uuid.uuid4().hex


def _utc_now() -> str:
    """与 SQLite 默认 ``strftime('%Y-%m-%dT%H:%M:%fZ','now')`` 同形的
    UTC 毫秒时间戳（``YYYY-MM-DDTHH:MM:SS.mmmZ``）。"""
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# 快照内汇总：审阅状态与门禁快照（main._gate_snapshot 共用同一实现）
# ---------------------------------------------------------------------------


def review_statuses(
    con: sqlite3.Connection, document_id: int
) -> Tuple[List[int], Dict[int, Set[int]], Set[int], Set[int], Set[int]]:
    """文档当前审阅状态：``(ann_ids, prereqs, completed, actionable, blocked)``。

    被阻塞 = 未完成且存在尚未完成的传递前置项；可处理 = 未完成且无阻塞。
    调用方必须持有事务（读或写），使各 SELECT 落在同一快照上。
    """
    ann_ids = [
        r["id"]
        for r in con.execute(
            "SELECT id FROM annotations WHERE document_id = ? ORDER BY id",
            (document_id,),
        ).fetchall()
    ]
    edges = [
        (r["prerequisite_id"], r["dependent_id"])
        for r in con.execute(
            "SELECT prerequisite_id, dependent_id FROM dependencies "
            "WHERE document_id = ? ORDER BY id",
            (document_id,),
        ).fetchall()
    ]
    completed = {
        r["annotation_id"]
        for r in con.execute(
            "SELECT annotation_id FROM completions WHERE document_id = ?",
            (document_id,),
        ).fetchall()
    }
    prereqs = build_prereq_map(edges)
    actionable, blocked = set(), set()
    for aid in ann_ids:
        if aid in completed:
            continue
        incomplete = [
            a for a in transitive_prerequisites(prereqs, aid) if a not in completed
        ]
        (blocked if incomplete else actionable).add(aid)
    return ann_ids, prereqs, completed, actionable, blocked


def gate_snapshot(con: sqlite3.Connection, document_id: int, version_id: int):
    """在事务快照内汇总门禁评估输入：目标版位置状态 + 当前审阅状态。

    位置状态取目标版本上每条批注迁移记录的**生效**状态（有人工确认时为
    confirmed；顺延占位仍是 ambiguous；该版本无迁移记录的批注不计入任何
    位置状态）；审阅状态按当前依赖图与完成记录划分。所有 SELECT 必须由
    调用方包在同一个事务中。
    """
    loc_rows = con.execute(
        """
        SELECT l.annotation_id, l.status,
               EXISTS (SELECT 1 FROM confirmations c WHERE c.location_id = l.id)
                   AS has_confirmation
        FROM locations l
        JOIN annotations a ON a.id = l.annotation_id
        WHERE l.version_id = ? AND a.document_id = ?
        """,
        (version_id, document_id),
    ).fetchall()
    location_ids = {status: set() for status in gate_mod.LOCATION_STATUSES}
    for row in loc_rows:
        status = "confirmed" if row["has_confirmation"] else row["status"]
        location_ids[status].add(row["annotation_id"])

    ann_ids, _, completed, actionable, blocked = review_statuses(con, document_id)
    review_ids = {
        "actionable": set(actionable),
        "blocked": set(blocked),
        "completed": set(completed),
    }
    # 防御：审阅状态以文档全部批注为全集
    assert actionable | blocked | completed == set(ann_ids)
    return gate_mod.GateSnapshot(
        location_ids_by_status=location_ids,
        review_ids_by_status=review_ids,
    )


def _review_status_freeze(actionable: Set[int], blocked: Set[int],
                          completed: Set[int]) -> Dict[str, List[int]]:
    return {
        "actionable": sorted(actionable),
        "blocked": sorted(blocked),
        "completed": sorted(completed),
    }


# ---------------------------------------------------------------------------
# 创建：在调用方持有的读事务内固定全部快照
# ---------------------------------------------------------------------------


def build_candidate(
    con: sqlite3.Connection,
    document_id: int,
    policy_name: str,
    policy_revision: Optional[int],
    version_number: Optional[int],
    *,
    note: Optional[str] = None,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """在调用方持有的读事务内冻结发布候选（不落库、不生成 ID）。

    假设文档存在（由调用方在同一事务内先校验）。策略修订不存在抛
    :class:`CandidateError`（``policy_not_found``，404）；目标版本不存在
    抛 :class:`CandidateError`（``version_not_found``，404）；文档没有
    任何版本且未指定版本号抛 ``no_version``（409）。门禁未通过抛
    :class:`GateRejected`（409），调用方不得落候选。
    """
    # 1. 策略修订（revision 缺省取最新；不存在 → 404）
    if policy_revision is None:
        policy_row = con.execute(
            "SELECT * FROM policy_revisions WHERE name = ? "
            "ORDER BY revision DESC LIMIT 1",
            (policy_name,),
        ).fetchone()
    else:
        policy_row = con.execute(
            "SELECT * FROM policy_revisions WHERE name = ? AND revision = ?",
            (policy_name, policy_revision),
        ).fetchone()
    if policy_row is None:
        if policy_revision is None:
            raise CandidateError(
                "policy_not_found",
                f"门禁策略 {policy_name!r} 不存在（尚无任何修订），"
                "不能创建发布候选",
                "policy",
                status_code=404,
            )
        raise CandidateError(
            "policy_not_found",
            f"门禁策略 {policy_name!r} 不存在第 {policy_revision} 版修订",
            "policy_revision",
            status_code=404,
        )
    strategy = json.loads(policy_row["strategy_json"])

    # 2. 目标版本（缺省最新版）
    if version_number is None:
        version_row = con.execute(
            "SELECT * FROM versions WHERE document_id = ? ORDER BY number DESC LIMIT 1",
            (document_id,),
        ).fetchone()
        if version_row is None:
            raise CandidateError(
                "no_version",
                f"文档 {document_id} 还没有任何版本，门禁无法评估，"
                "不能创建发布候选",
                "version",
                status_code=409,
            )
    else:
        version_row = con.execute(
            "SELECT * FROM versions WHERE document_id = ? AND number = ?",
            (document_id, version_number),
        ).fetchone()
        if version_row is None:
            raise CandidateError(
                "version_not_found",
                f"文档 {document_id} 不存在第 {version_number} 版",
                "version",
                status_code=404,
            )

    # 3. 同一快照：六类事件的单文档水位、门禁解释树、当前审阅状态
    watermark = events_mod.document_watermark(con, document_id)
    snapshot = gate_snapshot(con, document_id, version_row["id"])
    tree = gate_mod.evaluate_strategy(strategy, snapshot)
    # snapshot.review_ids_by_status 即本快照的审阅状态（ID 升序输出）
    review_freeze = _review_status_freeze(
        snapshot.review_ids_by_status["actionable"],
        snapshot.review_ids_by_status["blocked"],
        snapshot.review_ids_by_status["completed"],
    )
    annotation_count = sum(
        len(ids) for ids in snapshot.review_ids_by_status.values()
    )

    if not tree["result"]:
        raise GateRejected(
            "门禁未通过：目标版本在当前审阅状态上不满足策略 "
            f"{policy_row['name']!r} 第 {policy_row['revision']} 版修订，"
            "发布候选未创建（不落候选）",
            policy_row["name"],
            policy_row["revision"],
            version_row["number"],
            tree,
        )

    return {
        "format": CANDIDATE_FORMAT,
        "document_id": document_id,
        "note": note,
        "created_at": created_at or _utc_now(),
        "policy": {
            "name": policy_row["name"],
            "revision": policy_row["revision"],
            "strategy": strategy,
        },
        "version": {
            "version_id": version_row["id"],
            "number": version_row["number"],
            "content": version_row["content"],
        },
        "gate": {
            "passed": True,
            "explanation": tree,
        },
        "review_state": review_freeze,
        "annotation_count": annotation_count,
        "watermark": {table: watermark[table] for table in events_mod.TABLES},
    }


def gate_rejected_response(exc: GateRejected) -> Dict[str, Any]:
    """门禁拒绝时路由层返回的 409 响应体（含解释树，便于定位未通过节点）。"""
    return {
        "message": exc.message,
        "document_id": None,
        "policy_name": exc.policy_name,
        "policy_revision": exc.policy_revision,
        "version_number": exc.version_number,
        "passed": False,
        "explanation": exc.tree,
        "errors": [exc.to_dict()],
    }


# ---------------------------------------------------------------------------
# 确认：同写事务内重读水位与门禁快照
# ---------------------------------------------------------------------------


# 六类事件的中文名（冲突信息可定位到人话）
_WATERMARK_LABELS = {
    "versions": "版本",
    "annotations": "批注",
    "locations": "迁移位置",
    "confirmations": "人工确认",
    "dependencies": "审阅依赖",
    "completions": "完成记录",
}


def detect_confirm_conflicts(
    con: sqlite3.Connection, frozen: Dict[str, Any]
) -> List[CandidateConflict]:
    """在调用方持有的写事务（已取写锁）中重读水位与门禁快照并比对。

    返回冲突列表（空 = 可以发布）：

    - 六类事件中该文档任一表水位高于冻结值 → 每类一条
      ``watermark_changed``（``path=watermark.<table>``，附冻结 / 当前水位
      与该类事件中文名）；
    - 冻结的策略修订在冻结版本 + 当前快照上复评不再通过 → 一条
      ``gate_not_passed``（``path=gate``，附当前解释树）。
    """
    document_id = frozen["document_id"]
    conflicts: List[CandidateConflict] = []

    frozen_wm = frozen["watermark"]
    current_wm = events_mod.document_watermark(con, document_id)
    for table in events_mod.TABLES:
        old, new = frozen_wm[table], current_wm[table]
        if new != old:
            label = _WATERMARK_LABELS[table]
            conflicts.append(
                CandidateConflict(
                    "watermark_changed",
                    f"候选创建后文档 {document_id} 出现新的{label}记录："
                    f"{label}水位由 {old} 变为 {new}（有新的只追加行），"
                    "候选基于过期快照，请重新创建后再确认发布",
                    f"watermark.{table}",
                    extra={
                        "event_type": events_mod.TYPE_OF_RANK[events_mod.RANK_OF_TABLE[table]],
                        "table": table,
                        "frozen_watermark": old,
                        "current_watermark": new,
                    },
                )
            )

    strategy = frozen["policy"]["strategy"]
    version_id = frozen["version"]["version_id"]
    snapshot = gate_snapshot(con, document_id, version_id)
    tree = gate_mod.evaluate_strategy(strategy, snapshot)
    if not tree["result"]:
        conflicts.append(
            CandidateConflict(
                "gate_not_passed",
                "门禁不再通过：策略 "
                f"{frozen['policy']['name']!r} 第 {frozen['policy']['revision']} "
                f"版修订在目标第 {frozen['version']['number']} 版与当前审阅状态上"
                "评估结果为假，候选不可发布（状态未改变），请重新创建候选",
                "gate",
                extra={"explanation": tree},
            )
        )
    return conflicts


def persist_release(
    con: sqlite3.Connection,
    candidate_id: str,
    frozen: Dict[str, Any],
    digest: str,
    *,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """组装 released 结果并写回候选行（在调用方写事务内，只写一次）。

    调用前必须已通过 :func:`detect_confirm_conflicts`（无冲突）；候选的
    冻结内容不修改，只补写 released_at / result_json。
    """
    result = {
        "candidate_id": candidate_id,
        "document_id": frozen["document_id"],
        "outcome": "released",
        "released_at": now or _utc_now(),
        "policy_name": frozen["policy"]["name"],
        "policy_revision": frozen["policy"]["revision"],
        "version_number": frozen["version"]["number"],
        "digest": digest,
    }
    con.execute(
        "UPDATE release_candidates SET released_at = ?, result_json = ? WHERE id = ?",
        (result["released_at"], json.dumps(result, ensure_ascii=False), candidate_id),
    )
    return result


def replay_result(row: sqlite3.Row) -> Optional[Dict[str, Any]]:
    """已发布的候选：返回首次结果（调用方加 ``already_released=true``）。"""
    if row["released_at"] is None:
        return None
    return json.loads(row["result_json"])
