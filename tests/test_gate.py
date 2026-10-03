"""文档发布门禁测试：策略树校验（纯函数）与评估接口（同事务快照、解释树）。"""
import pytest
from fastapi.testclient import TestClient

from app import gate as gate_mod
from app.gate import GateSnapshot, evaluate_strategy, validate_strategy
from app.main import app


# ---------------------------------------------------------------------------
# 校验：合法策略
# ---------------------------------------------------------------------------


def loc(status, op="gte", value=1):
    return {"location_count": {"status": status, "op": op, "value": value}}


def rev(status, op="gte", value=1):
    return {"review_count": {"status": status, "op": op, "value": value}}


def test_validate_simple_leaves():
    for status in gate_mod.LOCATION_STATUSES:
        assert validate_strategy(loc(status)) == []
    for status in gate_mod.REVIEW_STATUSES:
        assert validate_strategy(rev(status)) == []
    for op in gate_mod.OPERATORS:
        assert validate_strategy(loc("resolved", op=op, value=0)) == []


def test_validate_combinators():
    assert validate_strategy({"all": [loc("resolved"), loc("confirmed")]}) == []
    assert validate_strategy({"any": [rev("actionable")]}) == []
    assert validate_strategy({"not": loc("unresolved")}) == []
    assert validate_strategy(
        {
            "all": [
                {"not": {"any": [loc("ambiguous"), loc("unresolved")]}},
                {"any": [rev("completed", "eq", 3), rev("blocked", "lte", 0)]},
            ]
        }
    ) == []


def test_validate_max_depth_and_nodes_accepted():
    # 深度恰好 8：8 个嵌套 not + 1 个叶子（not 深度贡献 7，叶子 1）
    tree = loc("resolved")
    for _ in range(gate_mod.MAX_DEPTH - 1):
        tree = {"not": tree}
    assert validate_strategy(tree) == []

    # 节点数恰好 64：1 个 all + 63 个叶子
    tree = {"all": [loc("resolved", value=i) for i in range(gate_mod.MAX_NODES - 1)]}
    assert validate_strategy(tree) == []
    # 再多 1 个叶子即 65 个节点 → 拒绝
    tree["all"].append(loc("resolved"))
    assert validate_strategy(tree)


# ---------------------------------------------------------------------------
# 校验：整棵拒绝的各种情形
# ---------------------------------------------------------------------------


def _errors(strategy):
    return validate_strategy(strategy)


def test_validate_root_must_be_object():
    errors = _errors([loc("resolved")])
    assert len(errors) == 1
    assert errors[0].error == "invalid_node"
    assert errors[0].path == "$"
    assert _errors("not")
    assert _errors(None)


def test_validate_unknown_field_at_root():
    errors = _errors({"all": [loc("resolved")], "mode": "strict"})
    assert any(e.error == "unknown_field" and e.path == "$" for e in errors)
    # 未知字段即整条拒绝
    assert _errors({"bogus": {}})


def test_validate_unknown_field_inside_leaf():
    strategy = {"all": [{"location_count": {
        "status": "resolved", "op": "eq", "value": 1, "unit": "条"}}]}
    errors = _errors(strategy)
    assert len(errors) == 1
    assert errors[0].error == "unknown_field"
    assert errors[0].path == "$.all[0].location_count.unit"


def test_validate_node_without_type_field():
    assert any(e.error == "invalid_node" for e in _errors({}))
    assert any(
        e.path == "$.any[0]"
        for e in _errors({"any": [{}]})
    )


def test_validate_node_with_multiple_type_fields():
    errors = _errors({"all": [loc("x")], "not": loc("y")})
    assert any(e.error == "ambiguous_node" and e.path == "$" for e in errors)


def test_validate_empty_combinations_rejected():
    errors = _errors({"all": []})
    assert len(errors) == 1
    assert errors[0].error == "empty_combination"
    assert errors[0].path == "$.all"

    errors = _errors({"any": []})
    assert errors[0].error == "empty_combination"
    assert errors[0].path == "$.any"


def test_validate_not_must_have_exactly_one_child():
    # not 子项是数组 → 拒绝（不是一个节点对象）
    errors = _errors({"not": [loc("resolved")]})
    assert len(errors) == 1
    assert errors[0].error == "invalid_not_child"
    assert errors[0].path == "$.not"
    # not 缺子项
    assert _errors({"not": None})
    # not 不接受“两个子项”的任何伪装；对象里同时含两个类型键同样拒绝
    assert _errors({"not": {"all": [loc("a")], "any": [loc("b")]}})


def test_validate_all_children_must_be_nodes():
    errors = _errors({"all": [loc("resolved"), 42]})
    assert any(
        e.error == "invalid_node" and e.path == "$.all[1]" for e in errors
    )
    errors = _errors({"any": ["x"]})
    assert errors[0].error == "invalid_node"


def test_validate_leaf_status_enum():
    errors = _errors(loc("done"))
    assert len(errors) == 1
    assert errors[0].error == "invalid_status"
    assert errors[0].path == "$.location_count.status"

    errors = _errors(rev("resolved"))
    assert errors[0].error == "invalid_status"
    assert errors[0].path == "$.review_count.status"


def test_validate_leaf_op_enum():
    errors = _errors({"location_count": {"status": "resolved", "op": "lt", "value": 1}})
    assert len(errors) == 1
    assert errors[0].error == "invalid_op"
    assert errors[0].path == "$.location_count.op"


def test_validate_leaf_value_nonneg_int():
    base = {"status": "resolved", "op": "eq"}
    for bad in (-1, 1.5, "1", True, None):
        errors = _errors({"location_count": {**base, "value": bad}})
        assert any(
            e.error == "invalid_value" and e.path == "$.location_count.value"
            for e in errors
        ), bad
    for missing in ({}, {"status": "resolved"}, {"op": "eq", "value": 1}):
        errors = _errors({"review_count": missing})
        assert any(e.error == "missing_field" for e in errors)


def test_validate_leaf_must_be_object():
    errors = _errors({"location_count": [1, 2, 3]})
    assert errors[0].error == "invalid_leaf"
    assert errors[0].path == "$.location_count"


def test_validate_collects_multiple_leaf_errors():
    # status、op、value 同时非法 → 一次性全部报告
    errors = _errors({"location_count": {"status": "x", "op": "y", "value": -1}})
    codes = {e.error for e in errors}
    assert {"invalid_status", "invalid_op", "invalid_value"} <= codes


def test_validate_depth_exceeded():
    tree = loc("resolved")
    for _ in range(gate_mod.MAX_DEPTH):  # 深度 9
        tree = {"not": tree}
    errors = _errors(tree)
    assert len(errors) == 1
    assert errors[0].error == "depth_exceeded"
    assert errors[0].path == "$"


def test_validate_nodes_exceeded():
    tree = {"all": [loc("resolved") for _ in range(gate_mod.MAX_NODES + 1)]}
    errors = _errors(tree)
    assert any(e.error == "nodes_exceeded" and e.path == "$" for e in errors)


def test_validate_both_limits_can_report_together():
    # 深链每层再挂 8 个叶子：深度 9 且节点数 1 + 9*8 = 73 > 64
    chain = loc("resolved")
    for i in range(8):
        chain = {"all": [chain] + [loc("resolved", value=j) for j in range(8)]}
    errors = {(e.error, e.path) for e in _errors(chain)}
    assert ("depth_exceeded", "$") in errors
    assert ("nodes_exceeded", "$") in errors


# ---------------------------------------------------------------------------
# 评估：纯函数
# ---------------------------------------------------------------------------


def _snapshot(location=None, review=None):
    loc_defaults = {status: set() for status in gate_mod.LOCATION_STATUSES}
    rev_defaults = {status: set() for status in gate_mod.REVIEW_STATUSES}
    if location:
        loc_defaults.update(location)
    if review:
        rev_defaults.update(review)
    return GateSnapshot(
        location_ids_by_status=loc_defaults,
        review_ids_by_status=rev_defaults,
    )


def test_evaluate_leaf_lists_actual_threshold_and_ids():
    snap = _snapshot(location={"resolved": {3, 1, 2}}, review={"completed": {1}})
    out = evaluate_strategy(loc("resolved", "gte", 2), snap)
    assert out["result"] is True
    assert out["location_count"] == {
        "status": "resolved",
        "op": "gte",
        "value": 2,
        "actual": 3,
        "annotation_ids": [1, 2, 3],  # 参与计数的 ID 升序
    }

    out = evaluate_strategy(loc("resolved", "lte", 2), snap)
    assert out["result"] is False
    out = evaluate_strategy(loc("resolved", "eq", 3), snap)
    assert out["result"] is True
    # 不参与计数的状态：空列表、actual=0
    out = evaluate_strategy(loc("unresolved", "eq", 0), snap)
    assert out["result"] is True
    assert out["location_count"]["annotation_ids"] == []


def test_evaluate_all_and_any_do_not_short_circuit():
    strategy = {
        "all": [
            loc("resolved", "eq", 0),       # False（有 3 条 resolved）
            {"not": loc("unresolved", "gte", 1)},  # True（没有 unresolved）
            rev("blocked", "gte", 1),        # True（有 1 条 blocked）
        ]
    }
    snap = _snapshot(
        location={"resolved": {1, 5, 9}, "unresolved": set()},
        review={"blocked": {4}},
    )
    out = evaluate_strategy(strategy, snap)
    assert out["result"] is False
    # 三个子节点全部出现（不因第一个为 False 而短路）
    assert len(out["all"]) == 3
    assert [child["result"] for child in out["all"]] == [False, True, True]

    strategy = {"any": [loc("resolved", "eq", 0), rev("completed", "eq", 2)]}
    out = evaluate_strategy(strategy, _snapshot(
        location={"resolved": {1, 5, 9}}, review={"completed": {2, 4}}
    ))
    assert out["result"] is True
    assert [child["result"] for child in out["any"]] == [False, True]


def test_evaluate_not():
    snap = _snapshot(location={"unresolved": {7}})
    # 内层「unresolved 计数 = 0」为 False（实际有 1 条），取反为 True
    out = evaluate_strategy({"not": loc("unresolved", "eq", 0)}, snap)
    assert out["result"] is True
    assert out["not"]["result"] is False
    # 内层「confirmed ≥ 1」为 False，取反为 True
    out = evaluate_strategy({"not": loc("confirmed", "gte", 1)}, snap)
    assert out["result"] is True


def test_evaluate_same_snapshot_is_stable():
    snap = _snapshot(location={"resolved": {9, 2, 7, 1}}, review={"blocked": {5, 3}})
    strategy = {
        "all": [
            {"any": [loc("resolved", "gte", 3), rev("blocked", "eq", 2)]},
            {"not": rev("completed", "gte", 1)},
        ]
    }
    first = evaluate_strategy(strategy, snap)
    second = evaluate_strategy(strategy, snap)
    assert first == second
    ids = first["all"][0]["any"][0]["location_count"]["annotation_ids"]
    assert ids == [1, 2, 7, 9]
    assert first["all"][0]["any"][1]["review_count"]["annotation_ids"] == [3, 5]


# ---------------------------------------------------------------------------
# 接口端到端
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _make_doc(client, content, title="门禁测试文档"):
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


def _new_version(client, doc, content):
    r = client.post(f"/documents/{doc}/versions", json={"content": content})
    assert r.status_code == 201, r.text
    return r.json()


def _put_policy(client, name, strategy):
    return client.post("/policies", json={"name": name, "strategy": strategy})


def test_api_create_policy_rejected_with_path_errors(client):
    bad = {
        "all": [
            {"location_count": {"status": "nope", "op": "eq", "value": 0}},
            {"not": []},
        ]
    }
    r = _put_policy(client, "非法策略", bad)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    paths = {e["path"]: e["error"] for e in detail["errors"]}
    assert paths["$.all[0].location_count.status"] == "invalid_status"
    assert paths["$.all[1].not"] == "invalid_not_child"
    # 非法策略不得落库
    assert client.get("/policies/非法策略").status_code == 404


def test_api_create_policy_unknown_field_and_limits_rejected(client):
    r = _put_policy(client, "未知字段", {"all": [], "extra": 1})
    assert r.status_code == 422
    assert any(e["error"] == "unknown_field" for e in r.json()["detail"]["errors"])

    deep = loc("resolved")
    for _ in range(8):
        deep = {"not": deep}
    r = _put_policy(client, "太深", deep)
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["error"] == "depth_exceeded"

    wide = {"all": [loc("resolved") for _ in range(65)]}
    r = _put_policy(client, "太宽", wide)
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["error"] == "nodes_exceeded"


def test_api_policy_revisions_append_only_by_name(client):
    name = "追加策略"
    r1 = _put_policy(client, name, loc("resolved", "gte", 1))
    assert r1.status_code == 201, r1.text
    assert r1.json()["revision"] == 1
    # 同名再提交：追加为第 2 版，第 1 版原样保留
    r2 = _put_policy(client, name, {"not": loc("unresolved", "gte", 1)})
    assert r2.status_code == 201
    assert r2.json()["revision"] == 2

    revs = client.get(f"/policies/{name}").json()["revisions"]
    assert [r["revision"] for r in revs] == [1, 2]
    assert revs[0]["strategy"] == loc("resolved", "gte", 1)
    assert revs[1]["strategy"] == {"not": loc("unresolved", "gte", 1)}

    one = client.get(f"/policies/{name}/revisions/1")
    assert one.status_code == 200
    assert one.json()["revision"] == 1

    assert client.get(f"/policies/{name}/revisions/9").status_code == 404
    assert client.get("/policies/不存在的策略").status_code == 404


def test_api_list_policies_latest_sorted_by_name(client):
    _put_policy(client, "策略-甲", loc("resolved"))
    _put_policy(client, "策略-甲", rev("completed"))  # 同名第二修订
    _put_policy(client, "策略-乙", loc("confirmed"))
    r = client.get("/policies")
    assert r.status_code == 200
    by_name = {p["name"]: p for p in r.json()["policies"]}
    assert by_name["策略-甲"]["revision"] == 2
    assert by_name["策略-甲"]["strategy"] == rev("completed")
    assert by_name["策略-乙"]["revision"] == 1
    names = [p["name"] for p in r.json()["policies"]]
    assert names == sorted(names)


def test_api_evaluate_location_counts_and_passed(client):
    text = "甲乙丙"
    doc = _make_doc(client, text)
    a1 = _make_ann(client, doc, text, "甲")
    a2 = _make_ann(client, doc, text, "乙")
    a3 = _make_ann(client, doc, text, "丙")

    # 新版：甲、乙落点唯一 resolved；丙找不到选区 unresolved
    v2 = "甲乙丁"
    _new_version(client, doc, v2)

    _put_policy(
        client, "发布条件",
        {
            "all": [
                loc("resolved", "gte", 2),
                loc("unresolved", "eq", 0),
            ],
        },
    )
    r = client.get(f"/documents/{doc}/gate/evaluate", params={"policy": "发布条件"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["version_number"] == 2
    assert data["policy_revision"] == 1
    assert data["passed"] is False  # 第 2 个叶子：有 1 条 unresolved
    leaves = data["explanation"]["all"]
    assert leaves[0]["result"] is True
    assert leaves[0]["location_count"]["actual"] == 2
    assert leaves[0]["location_count"]["annotation_ids"] == [a1, a2]
    assert leaves[1]["result"] is False
    assert leaves[1]["location_count"]["actual"] == 1
    assert leaves[1]["location_count"]["annotation_ids"] == [a3]


def test_api_evaluate_ambiguous_and_confirmed_status(client):
    text = "目标目标"
    doc = _make_doc(client, text)
    a1 = _make_ann(client, doc, text, "目标")
    # 相同上下文两处并列 → ambiguous
    _new_version(client, doc, "目标目标")
    r = client.get(f"/documents/{doc}/gate/evaluate", params={
        "policy": "ambiguous 门禁",
    })
    assert r.status_code == 404  # 策略尚不存在
    _put_policy(
        client, "ambiguous 门禁",
        {"all": [loc("ambiguous", "eq", 1), loc("confirmed", "eq", 0)]},
    )
    r = client.get(
        f"/documents/{doc}/gate/evaluate", params={"policy": "ambiguous 门禁"}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["passed"] is True
    leaf = data["explanation"]["all"][0]["location_count"]
    assert leaf["actual"] == 1 and leaf["annotation_ids"] == [a1]

    # 人工确认后再评估：同一版本上 ambiguous 0 条、confirmed 1 条
    candidates = client.get(f"/documents/{doc}/versions/2").json()["locations"][0]
    chosen = candidates["candidates"][0]
    cr = client.post(
        f"/documents/{doc}/annotations/{a1}/confirmations",
        json={"version_number": 2, "start": chosen["start"], "end": chosen["end"]},
    )
    assert cr.status_code == 201, cr.text
    r = client.get(
        f"/documents/{doc}/gate/evaluate", params={"policy": "ambiguous 门禁"}
    )
    data = r.json()
    assert data["passed"] is False
    statuses = [
        child["location_count"]["status"] for child in data["explanation"]["all"]
    ]
    actuals = {
        child["location_count"]["status"]: child["location_count"]["actual"]
        for child in data["explanation"]["all"]
    }
    assert statuses == ["ambiguous", "confirmed"]
    assert actuals == {"ambiguous": 0, "confirmed": 1}


def test_api_evaluate_review_counts_with_dependencies(client):
    text = "abc"
    doc = _make_doc(client, text)
    a1 = _make_ann(client, doc, text, "a")
    a2 = _make_ann(client, doc, text, "b")
    a3 = _make_ann(client, doc, text, "c")
    dep = client.post(
        f"/documents/{doc}/dependencies",
        json={"relations": [
            {"prerequisite_id": a1, "dependent_id": a2},
            {"prerequisite_id": a2, "dependent_id": a3},
        ]},
    )
    assert dep.status_code == 201, dep.text
    # 完成 a1：completed=1, actionable={a2}, blocked={a3}
    assert client.post(
        f"/documents/{doc}/annotations/{a1}/completions", json={}
    ).status_code == 201

    _put_policy(
        client, "审阅门禁",
        {
            "any": [
                rev("blocked", "gte", 1),
                {"not": rev("completed", "eq", 3)},
            ]
        },
    )
    r = client.get(f"/documents/{doc}/gate/evaluate", params={"policy": "审阅门禁"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["passed"] is True
    blocked_leaf, not_leaf = data["explanation"]["any"]
    assert blocked_leaf["review_count"]["actual"] == 1
    assert blocked_leaf["review_count"]["annotation_ids"] == [a3]
    assert not_leaf["not"]["review_count"]["actual"] == 1
    assert not_leaf["not"]["review_count"]["annotation_ids"] == [a1]


def test_api_evaluate_pinned_revision_and_version(client):
    text = "甲"
    doc = _make_doc(client, text)
    a1 = _make_ann(client, doc, text, "甲")
    _new_version(client, doc, "乙")  # 第 2 版 unresolved
    _new_version(client, doc, "甲")  # 第 3 版恢复 resolved
    _put_policy(client, "版本锁定", loc("unresolved", "gte", 1))
    _put_policy(client, "版本锁定", loc("resolved", "gte", 1))  # rev2

    # 锁定修订 1 + 版本 2：有 unresolved → 通过
    r = client.get(f"/documents/{doc}/gate/evaluate", params={
        "policy": "版本锁定", "revision": 1, "version": 2,
    })
    assert r.status_code == 200
    data = r.json()
    assert data["policy_revision"] == 1
    assert data["version_number"] == 2
    assert data["passed"] is True
    assert data["explanation"]["location_count"]["annotation_ids"] == [a1]

    # 默认最新修订 + 最新版本：resolved 门禁在第 3 版通过
    r = client.get(f"/documents/{doc}/gate/evaluate", params={"policy": "版本锁定"})
    data = r.json()
    assert data["policy_revision"] == 2 and data["version_number"] == 3
    assert data["passed"] is True

    # 最新修订（要求 resolved）锁定到第 2 版：不通过
    r = client.get(f"/documents/{doc}/gate/evaluate", params={
        "policy": "版本锁定", "revision": 2, "version": 2,
    })
    assert r.json()["passed"] is False

    assert client.get(f"/documents/{doc}/gate/evaluate", params={
        "policy": "版本锁定", "revision": 99,
    }).status_code == 404
    assert client.get(f"/documents/{doc}/gate/evaluate", params={
        "policy": "版本锁定", "version": 99,
    }).status_code == 404
    assert client.get("/documents/999999/gate/evaluate", params={
        "policy": "版本锁定",
    }).status_code == 404


def test_api_evaluate_stability_same_snapshot(client):
    text = "甲乙丙丁"
    doc = _make_doc(client, text)
    for ch in text:
        _make_ann(client, doc, text, ch)
    _new_version(client, doc, "甲乙X丁")  # 丙 unresolved
    strategy = {
        "all": [
            loc("resolved", "gte", 1),
            {"any": [loc("unresolved", "eq", 1), rev("actionable", "gte", 4)]},
            {"not": loc("confirmed", "gte", 1)},
        ]
    }
    _put_policy(client, "稳定性", strategy)
    params = {"policy": "稳定性", "version": 2}
    r1 = client.get(f"/documents/{doc}/gate/evaluate", params=params).json()
    r2 = client.get(f"/documents/{doc}/gate/evaluate", params=params).json()
    assert r1 == r2
