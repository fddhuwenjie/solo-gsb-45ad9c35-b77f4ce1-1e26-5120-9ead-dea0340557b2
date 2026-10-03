"""审阅依赖编排测试：依赖批量校验、环检测、审阅计划、完成记录。"""
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.review import (
    build_prereq_map,
    find_cycle_path,
    topological_waves,
    transitive_prerequisites,
)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _make_doc(client, content, title="审阅测试文档"):
    r = client.post("/documents", json={"title": title, "content": content})
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


def _make_ann(client, doc, text, selection, body="批注"):
    start = text.index(selection)
    r = client.post(
        f"/documents/{doc}/annotations",
        json={
            "body": body,
            "selection": selection,
            "start": start,
            "end": start + len(selection),
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["annotation_id"]


def _doc_with_annotations(client, n):
    """新建文档并创建 n 条批注，返回 (document_id, [annotation_id...])（编号升序）。"""
    text = "abcdefghijklmnopqrstuvwxyz"[: max(n, 1)]
    doc = _make_doc(client, text)
    ids = [_make_ann(client, doc, text, ch, body=f"批注{ch}") for ch in text[:n]]
    return doc, ids


def _add_deps(client, doc, relations):
    return client.post(
        f"/documents/{doc}/dependencies", json={"relations": relations}
    )


def _deps(client, doc):
    r = client.get(f"/documents/{doc}/dependencies")
    assert r.status_code == 200, r.text
    return r.json()["dependencies"]


def _plan(client, doc):
    r = client.get(f"/documents/{doc}/review-plan")
    assert r.status_code == 200, r.text
    return r.json()


def _complete(client, doc, ann, note=None):
    return client.post(
        f"/documents/{doc}/annotations/{ann}/completions", json={"note": note}
    )


# ---------------------------------------------------------------------------
# 图算法单元测试
# ---------------------------------------------------------------------------


def test_find_cycle_path_none_on_dag():
    assert find_cycle_path([]) is None
    assert find_cycle_path([(1, 2), (2, 3), (1, 3)]) is None
    # 经过环但自身不在环上的节点不影响结果
    assert find_cycle_path([(1, 2), (2, 3), (3, 4)]) is None


def test_find_cycle_path_self_loop():
    assert find_cycle_path([(3, 3)]) == [3, 3]


def test_find_cycle_path_rotates_to_smallest_id():
    # 环 {2, 3, 5}：无论边以何种顺序给出，路径都以最小编号 2 开头并闭环
    edges = [(5, 2), (3, 5), (2, 3)]
    assert find_cycle_path(edges) == [2, 3, 5, 2]


def test_find_cycle_path_first_cycle_in_ascending_order():
    # 两个不相交的环 {4, 5} 与 {2, 3}：按编号升序遍历，返回首条（含最小编号 2）
    edges = [(4, 5), (5, 4), (2, 3), (3, 2)]
    assert find_cycle_path(edges) == [2, 3, 2]


def test_find_cycle_path_cycle_not_through_smaller_node():
    # 1 通向环但自身不在环上：环 {2, 3} 的路径以 2 开头
    edges = [(1, 2), (2, 3), (3, 2)]
    assert find_cycle_path(edges) == [2, 3, 2]


def test_find_cycle_path_dense_dag():
    # 密集 DAG：i → j（i < j）全连接，旧实现枚举全部简单路径会指数爆炸；
    # n=200 时简单路径数已无法枚举，线性算法必须快速判定无环
    n = 200
    edges = [(i, j) for i in range(n) for j in range(i + 1, n)]
    t0 = time.perf_counter()
    result = find_cycle_path(edges)
    elapsed = time.perf_counter() - t0
    assert result is None
    assert elapsed < 2.0, f"密集 DAG 校验耗时 {elapsed:.3f}s，仍存在组合爆炸"


def test_find_cycle_path_dense_dag_with_closing_edge():
    # 密集 DAG 上加一条首尾闭合边：首条闭环沿编号升序直通
    n = 100
    edges = [(i, j) for i in range(n) for j in range(i + 1, n)]
    edges.append((n - 1, 0))
    result = find_cycle_path(edges)
    assert result == list(range(n)) + [0]


def test_find_cycle_path_overlapping_cycles():
    # 交叠多环：{2,3,4} 与 {2,3} 共享边 2→3，邻接升序先闭合两环
    edges = [
        (2, 3),
        (3, 4),
        (4, 2),
        (3, 2),
    ]
    assert find_cycle_path(edges) == [2, 3, 2]


def test_find_cycle_path_deep_overlapping_cycles():
    # 交叠多环：大环 {1,2,3,4} 与不经过 1 的小环 {2,3,4} 共享路径，
    # 起点 1 的 DFS 中邻接升序先探 2→3→4，由 4→1 闭合大环
    edges = [
        (1, 2),
        (2, 3),
        (3, 4),
        (4, 1),
        (4, 2),
    ]
    assert find_cycle_path(edges) == [1, 2, 3, 4, 1]


def test_find_cycle_path_smaller_start_ignores_ring_below_it():
    # 交叠多环：{2,3} 小环与 {2,4,5} 大环并存于通向 1 的链路上，
    # 1 不在任何环上，必须继续枚举更大起点，最终返回以 2 开头的小环
    edges = [
        (1, 2),
        (2, 3),
        (3, 2),
        (2, 4),
        (4, 5),
        (5, 2),
    ]
    assert find_cycle_path(edges) == [2, 3, 2]


def test_find_cycle_path_disjoint_cycles_returns_smallest():
    # 不交叠多环：{1,2}、{3,4,5}、{6,7} 并存，只返回以全局最小编号 1 开头的环
    edges = [
        (3, 4),
        (4, 5),
        (5, 3),
        (6, 7),
        (7, 6),
        (1, 2),
        (2, 1),
    ]
    assert find_cycle_path(edges) == [1, 2, 1]


def test_find_cycle_path_self_loop_with_larger_disjoint_cycle():
    # 自环节点 2 与更大不交叠环 {4,5} 并存：自环也是合法闭环，取最小起点
    edges = [(4, 5), (5, 4), (2, 2)]
    assert find_cycle_path(edges) == [2, 2]


def test_transitive_prerequisites():
    prereqs = build_prereq_map([(1, 2), (2, 3), (1, 3), (3, 4)])
    assert transitive_prerequisites(prereqs, 4) == {1, 2, 3}
    assert transitive_prerequisites(prereqs, 2) == {1}
    assert transitive_prerequisites(prereqs, 1) == set()


def test_topological_waves():
    prereqs = build_prereq_map([(1, 3), (2, 3), (3, 4)])
    assert topological_waves([1, 2, 3, 4, 5], prereqs, set()) == [
        [1, 2, 5],
        [3],
        [4],
    ]
    # 完成记录立即反映到分层：完成 1、2 后 3 进入第 0 层
    assert topological_waves([1, 2, 3, 4], prereqs, {1, 2}) == [[3], [4]]
    with pytest.raises(ValueError):
        topological_waves([1, 2], build_prereq_map([(1, 2), (2, 1)]), set())


# ---------------------------------------------------------------------------
# 依赖关系批量提交
# ---------------------------------------------------------------------------


def test_create_dependencies_batch_success(client):
    doc, (a1, a2, a3) = _doc_with_annotations(client, 3)
    r = _add_deps(
        client,
        doc,
        [
            {"prerequisite_id": a1, "dependent_id": a2},
            {"prerequisite_id": a2, "dependent_id": a3},
        ],
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["created_count"] == 2
    assert [
        (d["prerequisite_id"], d["dependent_id"]) for d in data["dependencies"]
    ] == [(a1, a2), (a2, a3)]

    deps = _deps(client, doc)
    assert len(deps) == 2
    assert all(d["created_at"] for d in deps)


def test_create_dependencies_cross_document_rejected(client):
    doc1, (a1,) = _doc_with_annotations(client, 1)
    doc2, (a2,) = _doc_with_annotations(client, 1)
    r = _add_deps(client, doc1, [{"prerequisite_id": a1, "dependent_id": a2}])
    assert r.status_code == 422
    assert "跨文档引用" in r.json()["detail"]
    assert _deps(client, doc1) == []  # 任何关系都不得落库
    assert _deps(client, doc2) == []


def test_create_dependencies_unknown_annotation(client):
    doc, (a1,) = _doc_with_annotations(client, 1)
    r = _add_deps(client, doc, [{"prerequisite_id": a1, "dependent_id": 999999}])
    assert r.status_code == 404
    assert _deps(client, doc) == []


def test_create_dependencies_self_dependency_rejected(client):
    doc, (a1,) = _doc_with_annotations(client, 1)
    r = _add_deps(client, doc, [{"prerequisite_id": a1, "dependent_id": a1}])
    assert r.status_code == 422
    assert "自依赖" in r.json()["detail"]
    assert _deps(client, doc) == []


def test_create_dependencies_duplicate_edge_in_batch_rejected(client):
    doc, (a1, a2) = _doc_with_annotations(client, 2)
    rel = {"prerequisite_id": a1, "dependent_id": a2}
    r = _add_deps(client, doc, [rel, rel])
    assert r.status_code == 422
    assert "重复边" in r.json()["detail"]
    assert _deps(client, doc) == []


def test_create_dependencies_existing_edge_conflict(client):
    doc, (a1, a2) = _doc_with_annotations(client, 2)
    rel = {"prerequisite_id": a1, "dependent_id": a2}
    assert _add_deps(client, doc, [rel]).status_code == 201
    r = _add_deps(client, doc, [rel])
    assert r.status_code == 409
    assert "重复边" in r.json()["detail"]
    assert len(_deps(client, doc)) == 1


def test_create_dependencies_cycle_in_batch_rejected(client):
    doc, (a1, a2, a3) = _doc_with_annotations(client, 3)
    r = _add_deps(
        client,
        doc,
        [
            {"prerequisite_id": a2, "dependent_id": a3},
            {"prerequisite_id": a3, "dependent_id": a1},
            {"prerequisite_id": a1, "dependent_id": a2},
        ],
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    # 环 {a1, a2, a3}（编号升序创建）：闭环路径以最小编号 a1 开头
    assert detail["cycle"] == [a1, a2, a3, a1]
    assert str(a1) in detail["message"]
    assert _deps(client, doc) == []  # 任何关系都不得落库


def test_create_dependencies_cycle_with_existing_edges_rejected(client):
    doc, (a1, a2, a3) = _doc_with_annotations(client, 3)
    # 第一批：a1→a2、a2→a3 合法落库
    r = _add_deps(
        client,
        doc,
        [
            {"prerequisite_id": a1, "dependent_id": a2},
            {"prerequisite_id": a2, "dependent_id": a3},
        ],
    )
    assert r.status_code == 201
    # 第二批：a3→a1 与已落库边构成环 → 整批拒绝
    r = _add_deps(client, doc, [{"prerequisite_id": a3, "dependent_id": a1}])
    assert r.status_code == 422
    assert r.json()["detail"]["cycle"] == [a1, a2, a3, a1]
    assert len(_deps(client, doc)) == 2  # 既有边不受影响，新边未落库


def test_create_dependencies_reports_first_cycle_by_smallest_id(client):
    doc, (a1, a2, a3, a4) = _doc_with_annotations(client, 4)
    # 同一批中含两个环 {a3, a4} 与 {a1, a2}：返回首条（以最小编号 a1 开头）
    r = _add_deps(
        client,
        doc,
        [
            {"prerequisite_id": a3, "dependent_id": a4},
            {"prerequisite_id": a4, "dependent_id": a3},
            {"prerequisite_id": a2, "dependent_id": a1},
            {"prerequisite_id": a1, "dependent_id": a2},
        ],
    )
    assert r.status_code == 422
    assert r.json()["detail"]["cycle"] == [a1, a2, a1]
    assert _deps(client, doc) == []


def test_create_dependencies_empty_batch_rejected(client):
    doc, _ = _doc_with_annotations(client, 1)
    r = _add_deps(client, doc, [])
    assert r.status_code == 422


def test_create_dependencies_dense_dag_batch_succeeds(client):
    # 密集无环依赖集：i → j（i < j）全连接（26 条批注，325 条边）。
    # 旧实现枚举简单路径会在合并已存边与本批边时组合爆炸、阻塞写入。
    doc, ids = _doc_with_annotations(client, 26)
    relations = [
        {"prerequisite_id": ids[i], "dependent_id": ids[j]}
        for i in range(len(ids))
        for j in range(i + 1, len(ids))
    ]
    t0 = time.perf_counter()
    r = _add_deps(client, doc, relations)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 201, r.text
    assert elapsed < 2.0, f"密集 DAG 批量校验耗时 {elapsed:.3f}s"
    data = r.json()
    assert data["created_count"] == len(relations)
    # 原子写入：返回顺序与提交顺序一致，且全部落库
    assert [
        (d["prerequisite_id"], d["dependent_id"]) for d in data["dependencies"]
    ] == [(rel["prerequisite_id"], rel["dependent_id"]) for rel in relations]
    assert len(_deps(client, doc)) == len(relations)


def test_create_dependencies_dense_dag_across_batches_succeeds(client):
    # 已落库的密集边（三角矩阵的一半）与本批新边合并后仍是密集 DAG：
    # 合并集合同样不得触发组合爆炸
    doc, ids = _doc_with_annotations(client, 20)
    first_batch = [
        {"prerequisite_id": ids[i], "dependent_id": ids[j]}
        for i in range(0, len(ids), 2)
        for j in range(i + 1, len(ids))
    ]
    second_batch = [
        {"prerequisite_id": ids[i], "dependent_id": ids[j]}
        for i in range(1, len(ids), 2)
        for j in range(i + 1, len(ids))
    ]
    assert _add_deps(client, doc, first_batch).status_code == 201
    t0 = time.perf_counter()
    r = _add_deps(client, doc, second_batch)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 201, r.text
    assert elapsed < 2.0, f"合并已有边后的密集 DAG 校验耗时 {elapsed:.3f}s"
    assert r.json()["created_count"] == len(second_batch)
    assert len(_deps(client, doc)) == len(first_batch) + len(second_batch)


def test_create_dependencies_dense_dag_closed_by_new_edge_rejected(client):
    # 已落库密集 DAG（i → j, i < j），新边「最大 → 最小」与已有边共同成环：
    # 整批拒绝，新边不落库，闭环路径沿编号升序
    doc, ids = _doc_with_annotations(client, 20)
    relations = [
        {"prerequisite_id": ids[i], "dependent_id": ids[j]}
        for i in range(len(ids))
        for j in range(i + 1, len(ids))
    ]
    assert _add_deps(client, doc, relations).status_code == 201
    before = len(_deps(client, doc))

    t0 = time.perf_counter()
    r = _add_deps(
        client, doc, [{"prerequisite_id": ids[-1], "dependent_id": ids[0]}]
    )
    elapsed = time.perf_counter() - t0
    assert r.status_code == 422, r.text
    assert elapsed < 2.0
    assert r.json()["detail"]["cycle"] == list(ids) + [ids[0]]
    deps = _deps(client, doc)
    assert len(deps) == before  # 新边未落库
    assert (ids[-1], ids[0]) not in {
        (d["prerequisite_id"], d["dependent_id"]) for d in deps
    }


def test_create_dependencies_overlapping_cycles_rejected(client):
    # 交叠多环：{a1,a2,a3} 与 {a1,a2} 共享边 a1→a2，
    # 邻接升序分支先闭合两环 [a1, a2, a1]
    doc, (a1, a2, a3, a4) = _doc_with_annotations(client, 4)
    r = _add_deps(
        client,
        doc,
        [
            {"prerequisite_id": a1, "dependent_id": a2},
            {"prerequisite_id": a2, "dependent_id": a3},
            {"prerequisite_id": a3, "dependent_id": a1},
            {"prerequisite_id": a2, "dependent_id": a1},
            # 无关的合法边不得落库（整批原子拒绝）
            {"prerequisite_id": a1, "dependent_id": a4},
        ],
    )
    assert r.status_code == 422
    assert r.json()["detail"]["cycle"] == [a1, a2, a1]
    assert _deps(client, doc) == []


def test_create_dependencies_disjoint_cycles_rejected(client):
    # 不交叠多环：{a1,a2}、{a3,a4,a5}、{a6,a7} 并存，
    # 只返回以全局最小编号 a1 开头的环，整批不落库
    doc, ids = _doc_with_annotations(client, 7)
    a1, a2, a3, a4, a5, a6, a7 = ids
    r = _add_deps(
        client,
        doc,
        [
            {"prerequisite_id": a3, "dependent_id": a4},
            {"prerequisite_id": a4, "dependent_id": a5},
            {"prerequisite_id": a5, "dependent_id": a3},
            {"prerequisite_id": a6, "dependent_id": a7},
            {"prerequisite_id": a7, "dependent_id": a6},
            {"prerequisite_id": a1, "dependent_id": a2},
            {"prerequisite_id": a2, "dependent_id": a1},
        ],
    )
    assert r.status_code == 422
    assert r.json()["detail"]["cycle"] == [a1, a2, a1]
    assert _deps(client, doc) == []


def test_create_dependencies_cycle_mixed_existing_and_new_rejected(client):
    # 环由已落库边与本批新边共同构成：已有 a1→a2、a2→a3，
    # 另一条已有边 a3→a4；本批提交 a4→a1（环）与合法新边 a1→a5，
    # 整批拒绝且新边均不落库
    doc, (a1, a2, a3, a4, a5) = _doc_with_annotations(client, 5)
    assert _add_deps(
        client,
        doc,
        [
            {"prerequisite_id": a1, "dependent_id": a2},
            {"prerequisite_id": a2, "dependent_id": a3},
            {"prerequisite_id": a3, "dependent_id": a4},
        ],
    ).status_code == 201

    r = _add_deps(
        client,
        doc,
        [
            {"prerequisite_id": a4, "dependent_id": a1},
            {"prerequisite_id": a1, "dependent_id": a5},
        ],
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["cycle"] == [a1, a2, a3, a4, a1]
    deps = _deps(client, doc)
    assert len(deps) == 3  # 仅保留第一批已落库边
    assert {(d["prerequisite_id"], d["dependent_id"]) for d in deps} == {
        (a1, a2),
        (a2, a3),
        (a3, a4),
    }


# ---------------------------------------------------------------------------
# 审阅计划
# ---------------------------------------------------------------------------


def _build_diamond(client):
    """构造 i1→i3、i2→i3、i3→i4、i4→i5 的依赖图，i6 无依赖。"""
    doc, ids = _doc_with_annotations(client, 6)
    i1, i2, i3, i4, i5, i6 = ids
    r = _add_deps(
        client,
        doc,
        [
            {"prerequisite_id": i1, "dependent_id": i3},
            {"prerequisite_id": i2, "dependent_id": i3},
            {"prerequisite_id": i3, "dependent_id": i4},
            {"prerequisite_id": i4, "dependent_id": i5},
        ],
    )
    assert r.status_code == 201, r.text
    return doc, ids


def test_review_plan_classification_and_batches(client):
    doc, (i1, i2, i3, i4, i5, i6) = _build_diamond(client)
    plan = _plan(client, doc)

    assert plan["summary"] == {
        "total": 6,
        "completed": 0,
        "actionable": 3,
        "blocked": 3,
        "batch_count": 4,
    }
    assert [a["annotation_id"] for a in plan["actionable"]] == [i1, i2, i6]
    # 无依赖的批注同样可处理，直接前置为空
    assert plan["actionable"][0]["prerequisite_ids"] == []

    blocked = {b["annotation_id"]: b for b in plan["blocked"]}
    # 被阻塞项列出所有尚未完成的传递前置项
    assert blocked[i3]["incomplete_prerequisites"] == [i1, i2]
    assert blocked[i3]["prerequisite_ids"] == [i1, i2]
    assert blocked[i4]["incomplete_prerequisites"] == [i1, i2, i3]
    assert blocked[i5]["incomplete_prerequisites"] == [i1, i2, i3, i4]

    # 拓扑层级：同层互不依赖，可并行
    assert plan["batches"] == [
        {"level": 0, "annotation_ids": [i1, i2, i6]},
        {"level": 1, "annotation_ids": [i3]},
        {"level": 2, "annotation_ids": [i4]},
        {"level": 3, "annotation_ids": [i5]},
    ]


def test_review_plan_empty_document(client):
    doc = _make_doc(client, "空文档")
    plan = _plan(client, doc)
    assert plan["summary"] == {
        "total": 0,
        "completed": 0,
        "actionable": 0,
        "blocked": 0,
        "batch_count": 0,
    }
    assert plan["batches"] == []


def test_review_plan_not_found(client):
    assert client.get("/documents/999999/review-plan").status_code == 404


# ---------------------------------------------------------------------------
# 完成记录
# ---------------------------------------------------------------------------


def test_completion_unlock_flow(client):
    doc, (i1, i2, i3, i4, i5, i6) = _build_diamond(client)

    # 完成 i1：i3 仍被 i2 阻塞，无新解锁
    r = _complete(client, doc, i1, note="已审阅")
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["already_completed"] is False
    assert data["unlocked_annotation_ids"] == []
    assert data["note"] == "已审阅"

    # 完成 i2：立即解锁 i3
    r = _complete(client, doc, i2)
    assert r.status_code == 201
    assert r.json()["unlocked_annotation_ids"] == [i3]

    # 计划立即反映：i1、i2 已完成，i3 可处理，i4/i5 的阻塞列表收缩
    plan = _plan(client, doc)
    assert [c["annotation_id"] for c in plan["completed"]] == [i1, i2]
    assert [a["annotation_id"] for a in plan["actionable"]] == [i3, i6]
    blocked = {b["annotation_id"]: b for b in plan["blocked"]}
    assert blocked[i4]["incomplete_prerequisites"] == [i3]
    assert blocked[i5]["incomplete_prerequisites"] == [i3, i4]
    assert plan["batches"] == [
        {"level": 0, "annotation_ids": [i3, i6]},
        {"level": 1, "annotation_ids": [i4]},
        {"level": 2, "annotation_ids": [i5]},
    ]

    # 依次完成剩余前置，逐步解锁
    assert _complete(client, doc, i3).json()["unlocked_annotation_ids"] == [i4]
    assert _complete(client, doc, i4).json()["unlocked_annotation_ids"] == [i5]
    assert _complete(client, doc, i5).json()["unlocked_annotation_ids"] == []
    assert _complete(client, doc, i6).status_code == 201

    plan = _plan(client, doc)
    assert plan["summary"]["completed"] == 6
    assert plan["actionable"] == []
    assert plan["blocked"] == []
    assert plan["batches"] == []


def test_completion_idempotent(client):
    doc, (a1,) = _doc_with_annotations(client, 1)
    r = _complete(client, doc, a1, note="第一次")
    assert r.status_code == 201
    first = r.json()

    # 重复提交：返回 200 与原记录，不新增行
    r = _complete(client, doc, a1, note="第二次")
    assert r.status_code == 200, r.text
    again = r.json()
    assert again["already_completed"] is True
    assert again["completion_id"] == first["completion_id"]
    assert again["note"] == "第一次"
    assert again["unlocked_annotation_ids"] == []

    completions = client.get(f"/documents/{doc}/completions").json()["completions"]
    assert len(completions) == 1
    assert completions[0]["annotation_id"] == a1


def test_completion_blocked_annotation_rejected(client):
    doc, (a1, a2, a3) = _doc_with_annotations(client, 3)
    assert _add_deps(
        client,
        doc,
        [
            {"prerequisite_id": a1, "dependent_id": a2},
            {"prerequisite_id": a2, "dependent_id": a3},
        ],
    ).status_code == 201

    # a3 被阻塞（传递前置 a1、a2 均未完成）→ 409，并列出全部未完成传递前置项
    r = _complete(client, doc, a3)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["incomplete_prerequisites"] == [a1, a2]
    assert client.get(f"/documents/{doc}/completions").json()["completions"] == []

    # 完成 a1 后 a3 仍被 a2 阻塞
    assert _complete(client, doc, a1).status_code == 201
    r = _complete(client, doc, a3)
    assert r.status_code == 409
    assert r.json()["detail"]["incomplete_prerequisites"] == [a2]


def test_completion_not_found(client):
    doc, _ = _doc_with_annotations(client, 1)
    r = _complete(client, doc, 999999)
    assert r.status_code == 404
    assert client.get("/documents/999999/completions").status_code == 404
    assert client.get("/documents/999999/dependencies").status_code == 404
