"""文档发布门禁：策略树校验与快照评估（纯函数，不直接落库）。

策略节点只允许以下五种形态：

- ``{"all": [...]}``：全部子节点为真（评估时**不短路**，逐节点全评估）；
- ``{"any": [...]}``：任一子节点为真（评估时**不短路**）；
- ``{"not": {...}}``：唯一子节点为假；
- ``{"location_count": {status, op, value}}``：目标版本上各批注生效位置
  状态计数的叶子；
- ``{"review_count": {status, op, value}}``：当前审阅状态计数的叶子。

叶子字段固定为 ``status`` / ``op`` / ``value``：

- location 状态限 ``resolved`` / ``confirmed`` / ``ambiguous`` /
  ``unresolved``（``ambiguous`` 含人工未确认的顺延占位）；
- review 状态限 ``actionable`` / ``blocked`` / ``completed``；
- 比较算子限 ``eq`` / ``lte`` / ``gte``（实际值为计数，参与计数的
  批注 ID 按编号升序在解释树中列出）。

整棵策略一次性校验，出现以下任一情况**整条拒绝**：未知字段、叶子字段
缺失或非法、``all`` / ``any`` 为空组合、``not`` 子项不是恰好一个、
树深超过 :data:`MAX_DEPTH`（=8，叶子深度为 1）、节点总数超过
:data:`MAX_NODES`（=64）。

评估（:func:`evaluate_strategy`）由调用方在**同一个 SQLite 读事务**
（同一快照）内提供策略修订、目标版本上的位置生效状态与当前审阅状态，
本模块只做确定的纯计算：返回与策略**同形**的解释树；相同快照下
评估结果与各 ID 列表顺序必然稳定。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

# 结构上限
MAX_DEPTH = 8
MAX_NODES = 64

COMBINATORS = ("all", "any", "not")
LEAF_KINDS = ("location_count", "review_count")
NODE_KEYS = COMBINATORS + LEAF_KINDS

LOCATION_STATUSES = ("resolved", "confirmed", "ambiguous", "unresolved")
REVIEW_STATUSES = ("actionable", "blocked", "completed")
OPERATORS = ("eq", "lte", "gte")

_LEAF_FIELDS = ("status", "op", "value")


@dataclass
class GateValidationError:
    """一条策略语法错误（带 JSON 路径式定位）。

    - ``error``：机器可读错误码；
    - ``message``：中文说明；
    - ``path``：根节点为 ``$``，子节点如 ``$.all[0].not``。
    """

    error: str
    message: str
    path: str

    def to_dict(self) -> Dict[str, str]:
        return {"error": self.error, "message": self.message, "path": self.path}


def _is_nonneg_int(value: Any) -> bool:
    # bool 是 int 的子类型，须显式排除
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_leaf(kind: str, value: Any, path: str) -> List[GateValidationError]:
    """校验叶子节点 ``{status, op, value}``；字段固定、取值受限。"""
    out: List[GateValidationError] = []
    if not isinstance(value, dict):
        out.append(
            GateValidationError(
                "invalid_leaf",
                f"{kind} 叶子必须是包含 status / op / value 的对象",
                path,
            )
        )
        return out
    for name in sorted(value):
        if name not in _LEAF_FIELDS:
            out.append(
                GateValidationError(
                    "unknown_field",
                    f"{kind} 叶子存在未知字段 {name!r}；叶子字段固定为 "
                    "status、op、value",
                    f"{path}.{name}",
                )
            )
    status = value.get("status")
    op = value.get("op")
    threshold = value.get("value")
    allowed = LOCATION_STATUSES if kind == "location_count" else REVIEW_STATUSES
    if "status" not in value:
        out.append(
            GateValidationError("missing_field", f"{kind} 叶子缺少 status", path)
        )
    elif status not in allowed:
        out.append(
            GateValidationError(
                "invalid_status",
                f"{kind} 叶子的 status 必须是 {', '.join(allowed)} 之一，"
                f"实际为 {status!r}",
                f"{path}.status",
            )
        )
    if "op" not in value:
        out.append(
            GateValidationError("missing_field", f"{kind} 叶子缺少 op", path)
        )
    elif op not in OPERATORS:
        out.append(
            GateValidationError(
                "invalid_op",
                f"op 必须是 {', '.join(OPERATORS)} 之一，实际为 {op!r}",
                f"{path}.op",
            )
        )
    if "value" not in value:
        out.append(
            GateValidationError("missing_field", f"{kind} 叶子缺少 value", path)
        )
    elif not _is_nonneg_int(threshold):
        out.append(
            GateValidationError(
                "invalid_value",
                f"value 必须是非负整数，实际为 {threshold!r}",
                f"{path}.value",
            )
        )
    return out


def validate_strategy(strategy: Any) -> List[GateValidationError]:
    """校验策略树；返回全部错误（空列表表示合法）。

    错误尽量一次性收集：结构非法的节点不再下钻（其子孙不再重复报错），
    深度与节点总数的超限在遍历后统一报告。
    """
    if not isinstance(strategy, dict):
        return [
            GateValidationError(
                "invalid_node", "策略根节点必须是对象（JSON object）", "$"
            )
        ]

    errors: List[GateValidationError] = []
    node_count = 0

    def visit(node: Any, path: str) -> Optional[int]:
        """返回以 ``node`` 为根的子树深度（叶子为 1）；结构非法时返回 None。"""
        nonlocal node_count
        if not isinstance(node, dict):
            errors.append(
                GateValidationError("invalid_node", "策略节点必须是对象", path)
            )
            return None
        node_count += 1

        known = [key for key in node if key in NODE_KEYS]
        unknown = [key for key in node if key not in NODE_KEYS]
        if unknown:
            errors.append(
                GateValidationError(
                    "unknown_field",
                    "存在未知字段 "
                    + ", ".join(repr(key) for key in sorted(unknown))
                    + f"；节点只允许 {', '.join(NODE_KEYS)} 之一",
                    path,
                )
            )
            return None
        if not known:
            errors.append(
                GateValidationError(
                    "invalid_node",
                    "节点缺少策略类型字段（应恰好包含 "
                    f"{', '.join(NODE_KEYS)} 之一）",
                    path,
                )
            )
            return None
        if len(known) > 1:
            errors.append(
                GateValidationError(
                    "ambiguous_node",
                    "节点同时出现多个策略类型字段 "
                    + ", ".join(repr(key) for key in known)
                    + "，每个节点必须恰好选择一个",
                    path,
                )
            )
            return None

        key = known[0]
        child_path = f"{path}.{key}"
        value = node[key]

        if key in ("all", "any"):
            if not isinstance(value, list):
                errors.append(
                    GateValidationError(
                        "invalid_children",
                        f"{key!r} 的值必须是子节点数组",
                        child_path,
                    )
                )
                return None
            if not value:
                errors.append(
                    GateValidationError(
                        "empty_combination", f"{key!r} 不允许空组合", child_path
                    )
                )
                return None
            child_depths: List[int] = []
            for i, child in enumerate(value):
                depth = visit(child, f"{child_path}[{i}]")
                if depth is not None:
                    child_depths.append(depth)
            if len(child_depths) != len(value):
                return None
            return 1 + max(child_depths)

        if key == "not":
            if not isinstance(value, dict):
                errors.append(
                    GateValidationError(
                        "invalid_not_child",
                        "not 的子项必须是恰好一个策略节点对象",
                        child_path,
                    )
                )
                return None
            depth = visit(value, child_path)
            return None if depth is None else 1 + depth

        # 叶子：location_count / review_count
        leaf_errors = _validate_leaf(key, value, child_path)
        errors.extend(leaf_errors)
        return 1 if not leaf_errors else None

    root_depth = visit(strategy, "$")

    if root_depth is not None and root_depth > MAX_DEPTH:
        errors.append(
            GateValidationError(
                "depth_exceeded",
                f"策略深度为 {root_depth}，超过上限 {MAX_DEPTH}（叶子深度为 1）",
                "$",
            )
        )
    if node_count > MAX_NODES:
        errors.append(
            GateValidationError(
                "nodes_exceeded",
                f"策略节点总数为 {node_count}，超过上限 {MAX_NODES}",
                "$",
            )
        )
    return errors


# ---------------------------------------------------------------------------
# 评估
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateSnapshot:
    """同一读事务快照内锁定的门禁评估输入。

    - ``location_ids_by_status``：目标版本上每条批注的**生效**位置状态 →
      批注 ID 集合（confirmed 取人工确认后的生效状态；ambiguous 含顺延
      占位；该版本没有迁移记录的批注不参与任何状态计数）；
    - ``review_ids_by_status``：当前审阅状态（actionable / blocked /
      completed）→ 批注 ID 集合。
    """

    location_ids_by_status: Dict[str, Set[int]]
    review_ids_by_status: Dict[str, Set[int]]


def evaluate_strategy(
    strategy: Dict[str, Any], snapshot: GateSnapshot
) -> Dict[str, Any]:
    """在给定快照上评估策略树，返回与策略同形的解释树。

    每个节点都带布尔 ``result``；叶子另列 ``actual``（实际计数）、
    ``value``（阈值）、``op``、``status`` 与参与计数且按编号升序的
    ``annotation_ids``；组合节点列出各子节点的解释（**不短路**，全部
    子节点都会出现在解释树中）。相同快照下输出完全确定。
    """

    def visit(node: Dict[str, Any]) -> Dict[str, Any]:
        key = next(iter(k for k in node if k in NODE_KEYS))
        if key == "all":
            children = [visit(child) for child in node["all"]]
            return {"all": children, "result": all(c["result"] for c in children)}
        if key == "any":
            children = [visit(child) for child in node["any"]]
            return {"any": children, "result": any(c["result"] for c in children)}
        if key == "not":
            child = visit(node["not"])
            return {"not": child, "result": not child["result"]}

        leaf = node[key]
        status, op, threshold = leaf["status"], leaf["op"], leaf["value"]
        source = (
            snapshot.location_ids_by_status
            if key == "location_count"
            else snapshot.review_ids_by_status
        )
        ids = sorted(source.get(status, set()))
        actual = len(ids)
        if op == "eq":
            result = actual == threshold
        elif op == "lte":
            result = actual <= threshold
        else:  # gte
            result = actual >= threshold
        return {
            key: {
                "status": status,
                "op": op,
                "value": threshold,
                "actual": actual,
                "annotation_ids": ids,
            },
            "result": result,
        }

    return visit(strategy)
