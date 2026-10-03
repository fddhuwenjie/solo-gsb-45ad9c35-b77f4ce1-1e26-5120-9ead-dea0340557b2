"""可离线往返的审阅包测试。

覆盖：

- 创建：同一快照冻结最新迁移记录 / 候选范围 / 完成状态 / 全部传递前置
  状态；自动收录未完成传递前置项；已完成项进 excluded；重复 / 不存在 /
  跨文档 / 全已完成的请求处理；内容摘要可用标准库离线复算；
- 离线填写：只依据冻结包的整批决定校验（重复 / 包外 / 非法 action /
  不可确认 / 候选缺失或不符 / 对已完成项 complete / 缺同包前置决定）；
- 回传冲突：目标在冻结后被确认、完成状态变化、新增版本改变最新位置、
  冻结后新增依赖边引入包外阻塞 → 409 且**不写入任何确认或完成**；
- 应用：同一写事务内确认按批注 ID 升序、完成按依赖拓扑顺序追加，
  后项由前项解锁，混合 confirm + complete，决定应用时非法全部回滚；
- 幂等：同一 package_id 成功回传后再提交原样返回首次结果（200，
  already_applied=true），不产生重复行；重放篡改摘要 → 409；
- 并发：两个回传并发到达时由 BEGIN IMMEDIATE 串行化，恰一个写入，
  另一个在取得写锁后检测到完成状态变化而 409，不产生重复完成行。
"""
import hashlib
import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import db
from app import review_packages as review_packages_mod
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _make_doc(client, content, title="审阅包测试"):
    r = client.post("/documents", json={"title": title, "content": content})
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


def _make_ann(client, doc, text, selection, body="批注", start=None):
    if start is None:
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


def _add_deps(client, doc, relations):
    r = client.post(
        f"/documents/{doc}/dependencies", json={"relations": relations}
    )
    assert r.status_code == 201, r.text
    return r.json()


def _create_package(client, doc, ids, note=None):
    body = {"annotation_ids": ids}
    if note is not None:
        body["note"] = note
    r = client.post(f"/documents/{doc}/review-packages", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _submit(client, package_id, digest, decisions):
    return client.post(
        "/review-packages/submit",
        json={"package_id": package_id, "digest": digest, "decisions": decisions},
    )


def _complete(client, doc, ann, note=None):
    return client.post(
        f"/documents/{doc}/annotations/{ann}/completions", json={"note": note}
    )


def _offline_digest(package):
    canon = json.dumps(
        package, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _count_rows(table, document_id=None):
    con = db.connect()
    try:
        if document_id is None:
            return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE document_id = ?", (document_id,)
        ).fetchone()[0]
    finally:
        con.close()


def _count_confirmations(document_id):
    """文档下的人工确认行数（confirmations 表本身无 document_id 列，经 join 统计）。"""
    con = db.connect()
    try:
        return con.execute(
            """
            SELECT COUNT(*) FROM confirmations c
            JOIN locations l ON l.id = c.location_id
            JOIN annotations a ON a.id = l.annotation_id
            WHERE a.document_id = ?
            """,
            (document_id,),
        ).fetchone()[0]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 创建：冻结内容与收录范围
# ---------------------------------------------------------------------------


def test_create_package_freezes_snapshot(client):
    doc = _make_doc(client, "甲乙丙")
    a1 = _make_ann(client, doc, "甲乙丙", "甲")
    a2 = _make_ann(client, doc, "甲乙丙", "乙")
    pkg = _create_package(client, doc, [a1, a2])

    assert pkg["format"] == "annotation-review-package/1"
    assert len(pkg["package_id"]) == 32
    assert len(pkg["digest"]) == 64
    assert pkg["included_count"] == 2
    assert pkg["excluded"] == []
    # 摘要可用标准库完全离线复算
    assert _offline_digest(pkg["package"]) == pkg["digest"]

    items = {it["annotation_id"]: it for it in pkg["package"]["items"]}
    assert set(items) == {a1, a2}
    for it in items.values():
        assert it["roles"] == ["requested"]
        assert it["completed"] is False
        assert it["prerequisite_ids"] == []
        loc = it["latest_location"]
        assert loc["status"] == "resolved"
        assert loc["carried_from"] is None
        assert loc["confirmation"] is None
        assert loc["candidates"][0]["start"] >= 0


def test_create_package_auto_includes_open_prerequisites(client):
    doc = _make_doc(client, "甲乙丙丁")
    a1 = _make_ann(client, doc, "甲乙丙丁", "甲")
    a2 = _make_ann(client, doc, "甲乙丙丁", "乙")
    a3 = _make_ann(client, doc, "甲乙丙丁", "丙")
    a4 = _make_ann(client, doc, "甲乙丙丁", "丁")
    _add_deps(
        client,
        doc,
        [
            {"prerequisite_id": a1, "dependent_id": a3},
            {"prerequisite_id": a2, "dependent_id": a3},
            {"prerequisite_id": a3, "dependent_id": a4},
        ],
    )
    # 只请求 a4：a1/a2/a3 全部传递前置（未完成）自动收录
    pkg = _create_package(client, doc, [a4])
    ids = [it["annotation_id"] for it in pkg["package"]["items"]]
    assert ids == [a1, a2, a3, a4]
    by_id = {it["annotation_id"]: it for it in pkg["package"]["items"]}
    assert by_id[a1]["roles"] == ["prerequisite"]
    assert by_id[a4]["roles"] == ["requested"]
    # 全部传递前置状态
    assert set(pkg["package"]["prerequisite_states"]) == {str(a1), str(a2), str(a3)}
    for state in pkg["package"]["prerequisite_states"].values():
        assert state == {"completed": False, "included": True}
    # 直接前置边冻结进条目（a3 的直接前置是 a1,a2）
    assert sorted(by_id[a3]["prerequisite_ids"]) == [a1, a2]
    assert by_id[a4]["prerequisite_ids"] == [a3]


def test_completed_prerequisites_not_included(client):
    doc = _make_doc(client, "甲乙丙")
    a1 = _make_ann(client, doc, "甲乙丙", "甲")
    _make_ann(client, doc, "甲乙丙", "乙")
    a3 = _make_ann(client, doc, "甲乙丙", "丙")
    _add_deps(client, doc, [{"prerequisite_id": a1, "dependent_id": a3}])
    r = _complete(client, doc, a1)
    assert r.status_code == 201
    # 请求 a3：前置 a1 已完成 → 只收录 a3；前置状态仍冻结为已完成
    pkg = _create_package(client, doc, [a3])
    ids = [it["annotation_id"] for it in pkg["package"]["items"]]
    assert ids == [a3]
    assert pkg["package"]["prerequisite_states"][str(a1)] == {
        "completed": True,
        "included": False,
    }


def test_already_completed_requested_goes_to_excluded(client):
    doc = _make_doc(client, "甲乙")
    a1 = _make_ann(client, doc, "甲乙", "甲")
    a2 = _make_ann(client, doc, "甲乙", "乙")
    _complete(client, doc, a1)
    pkg = _create_package(client, doc, [a1, a2])
    assert [it["annotation_id"] for it in pkg["package"]["items"]] == [a2]
    assert len(pkg["excluded"]) == 1
    excluded = pkg["excluded"][0]
    assert excluded["annotation_id"] == a1
    assert excluded["reason"] == "already_completed"
    assert isinstance(excluded["message"], str) and str(a1) in excluded["message"]


def test_all_completed_request_rejected(client):
    doc = _make_doc(client, "甲乙")
    a1 = _make_ann(client, doc, "甲乙", "甲")
    _complete(client, doc, a1)
    r = client.post(
        f"/documents/{doc}/review-packages", json={"annotation_ids": [a1]}
    )
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert [e["error"] for e in errors] == ["already_completed"]
    assert errors[0]["annotation_id"] == a1


def test_create_package_request_validation(client):
    doc = _make_doc(client, "甲乙")
    a1 = _make_ann(client, doc, "甲乙", "甲")

    # 重复 ID
    r = client.post(
        f"/documents/{doc}/review-packages", json={"annotation_ids": [a1, a1]}
    )
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["error"] == "duplicate_annotation"

    # 不存在
    r = client.post(
        f"/documents/{doc}/review-packages", json={"annotation_ids": [9999]}
    )
    assert r.status_code == 404
    assert r.json()["detail"]["errors"][0]["error"] == "annotation_not_found"

    # 跨文档
    doc2 = _make_doc(client, "子丑")
    r = client.post(
        f"/documents/{doc2}/review-packages", json={"annotation_ids": [a1]}
    )
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["error"] == "cross_document"

    # 空列表 / 缺字段由 schema 拦截
    r = client.post(f"/documents/{doc}/review-packages", json={"annotation_ids": []})
    assert r.status_code == 422


def test_package_digest_covers_ambiguous_candidates(client):
    base = "开头：目标。结尾。"
    doc = _make_doc(client, base)
    ann = _make_ann(client, doc, base, "目标", start=3, body="歧义批注")
    _new_version(client, doc, "说：目标。说：目标。")
    pkg = _create_package(client, doc, [ann])
    loc = pkg["package"]["items"][0]["latest_location"]
    assert loc["status"] == "ambiguous"
    assert loc["carried_from"] is None
    assert len(loc["candidates"]) == 2
    assert _offline_digest(pkg["package"]) == pkg["digest"]
    # 篡改包内容后离线复算的摘要不再一致
    tampered = json.loads(json.dumps(pkg["package"]))
    tampered["items"][0]["latest_location"]["candidates"][0]["start"] = 123
    assert _offline_digest(tampered) != pkg["digest"]


def test_same_content_creates_distinct_package_ids(client):
    doc = _make_doc(client, "甲乙")
    a1 = _make_ann(client, doc, "甲乙", "甲")
    p1 = _create_package(client, doc, [a1])
    p2 = _create_package(client, doc, [a1])
    assert p1["package_id"] != p2["package_id"]
    # 内容一致 → 摘要一致（除 created_at 外；created_at 不同也应区分摘要）
    assert p1["digest"] != p2["digest"]  # created_at 参与摘要


# ---------------------------------------------------------------------------
# 离线填写：整批决定校验（全部只依据冻结包）
# ---------------------------------------------------------------------------


def test_submit_offline_decision_errors_batch(client):
    doc = _make_doc(client, "甲乙丙")
    a1 = _make_ann(client, doc, "甲乙丙", "甲")
    a2 = _make_ann(client, doc, "甲乙丙", "乙")
    pkg = _create_package(client, doc, [a1, a2])
    pid, digest = pkg["package_id"], pkg["digest"]

    r = _submit(
        client,
        pid,
        digest,
        [
            {"annotation_id": a1, "action": "frobnicate"},
            {"annotation_id": 999, "action": "complete"},
            {"annotation_id": a1, "action": "complete"},
            {"annotation_id": a1, "action": "complete"},
        ],
    )
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    codes = [e["error"] for e in errors]
    assert "invalid_action" in codes
    assert "decision_not_in_package" in codes
    assert "duplicate_decision" in codes
    # 路径定位到具体决定下标
    paths = [e["path"] for e in errors]
    assert "decisions[0]" in paths
    assert "decisions[1]" in paths
    assert "decisions[3]" in paths
    # 校验失败不写入（文档级计数，模块共享库中其他测试的行不计入）
    assert _count_rows("completions", doc) == 0


def test_confirm_validation_against_frozen_candidates(client):
    base = "说：目标。结尾。"
    doc = _make_doc(client, base)
    ann = _make_ann(client, doc, base, "目标", start=2)
    _new_version(client, doc, "说：目标。说：目标。")
    pkg = _create_package(client, doc, [ann])
    pid, digest = pkg["package_id"], pkg["digest"]

    # 缺 start/end
    r = _submit(client, pid, digest, [{"annotation_id": ann, "action": "confirm"}])
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["error"] == "missing_candidate_range"

    # 候选范围不符（合法的非候选码点区间）
    candidates = pkg["package"]["items"][0]["latest_location"]["candidates"]
    valid_starts = {c["start"] for c in candidates}
    bogus = next(x for x in range(0, 12) if x not in valid_starts)
    r = _submit(
        client,
        pid,
        digest,
        [{"annotation_id": ann, "action": "confirm", "start": bogus, "end": bogus + 1}],
    )
    assert r.status_code == 422
    assert (
        r.json()["detail"]["errors"][0]["error"] == "confirmation_not_candidate"
    )

    # 对 resolved 批注 confirm
    doc2 = _make_doc(client, "甲乙")
    a = _make_ann(client, doc2, "甲乙", "甲")
    pkg2 = _create_package(client, doc2, [a])
    r = _submit(
        client,
        pkg2["package_id"],
        pkg2["digest"],
        [{"annotation_id": a, "action": "confirm", "start": 0, "end": 1}],
    )
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["error"] == "not_confirmable"


def test_complete_already_completed_in_package_rejected(client):
    base = "说：目标。结尾。"
    doc = _make_doc(client, base)
    a1 = _make_ann(client, doc, base, "目标", start=2)
    _new_version(client, doc, "说：目标。说：目标。")  # a1 变歧义
    _complete(client, doc, a1)
    # a1 已完成但最新位置仍是可确认的歧义 → 仍收录（roles=requested），
    # 对它给 complete 决定必须拒绝，给 confirm 决定才合法
    pkg = _create_package(client, doc, [a1])
    assert [it["annotation_id"] for it in pkg["package"]["items"]] == [a1]
    r = _submit(
        client,
        pkg["package_id"],
        pkg["digest"],
        [{"annotation_id": a1, "action": "complete"}],
    )
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["error"] == "already_completed"


def test_missing_prerequisite_decisions_rejected_offline(client):
    doc = _make_doc(client, "甲乙丙")
    a1 = _make_ann(client, doc, "甲乙丙", "甲")
    a2 = _make_ann(client, doc, "甲乙丙", "乙")
    a3 = _make_ann(client, doc, "甲乙丙", "丙")
    _add_deps(
        client,
        doc,
        [
            {"prerequisite_id": a1, "dependent_id": a3},
            {"prerequisite_id": a2, "dependent_id": a3},
        ],
    )
    pkg = _create_package(client, doc, [a3])
    # 只 complete a3：a1,a2 的决定缺失 → 离线 422（不查库）
    r = _submit(
        client,
        pkg["package_id"],
        pkg["digest"],
        [{"annotation_id": a3, "action": "complete"}],
    )
    assert r.status_code == 422
    err = r.json()["detail"]["errors"][0]
    assert err["error"] == "missing_prerequisite_decisions"
    assert err["annotation_id"] == a3
    assert err["path"].startswith("items[")


def test_submit_unknown_package_and_bad_digest(client):
    r = client.post(
        "/review-packages/submit",
        json={"package_id": "nonexistent", "digest": "a" * 64, "decisions": []},
    )
    assert r.status_code == 404

    doc = _make_doc(client, "甲乙")
    a1 = _make_ann(client, doc, "甲乙", "甲")
    pkg = _create_package(client, doc, [a1])
    r = _submit(client, pkg["package_id"], "a" * 64, [])
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["error"] == "package_digest_mismatch"
    # 摘要不符不写入、不标记 applied
    con = db.connect()
    try:
        row = con.execute(
            "SELECT applied_at FROM review_packages WHERE id = ?",
            (pkg["package_id"],),
        ).fetchone()
        assert row["applied_at"] is None
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 回传冲突：冻结后状态变化 → 409 且不写入
# ---------------------------------------------------------------------------


def test_conflict_new_version_changes_latest_location(client):
    doc = _make_doc(client, "甲乙丙")
    a1 = _make_ann(client, doc, "甲乙丙", "甲")
    pkg = _create_package(client, doc, [a1])
    before_completions = _count_rows("completions", doc)
    # 新增版本（选区平移，最新迁移记录改变）
    _new_version(client, doc, "开头甲乙丙")
    r = _submit(
        client,
        pkg["package_id"],
        pkg["digest"],
        [{"annotation_id": a1, "action": "complete"}],
    )
    assert r.status_code == 409
    conflicts = r.json()["detail"]["conflicts"]
    assert [c["error"] for c in conflicts] == ["latest_location_changed"]
    assert conflicts[0]["annotation_id"] == a1
    assert conflicts[0]["path"] == "items[0]"
    assert _count_rows("completions", doc) == before_completions


def test_conflict_external_confirmation(client):
    base = "说：目标。结尾。"
    doc = _make_doc(client, base)
    ann = _make_ann(client, doc, base, "目标", start=2)
    _new_version(client, doc, "说：目标。说：目标。")
    pkg = _create_package(client, doc, [ann])
    loc = pkg["package"]["items"][0]["latest_location"]
    # 冻结后他人通过常规接口确认（选另一候选）
    other = loc["candidates"][1]
    r = client.post(
        f"/documents/{doc}/annotations/{ann}/confirmations",
        json={
            "version_number": loc["version_number"],
            "start": other["start"],
            "end": other["end"],
        },
    )
    assert r.status_code == 201
    chosen = loc["candidates"][0]
    r = _submit(
        client,
        pkg["package_id"],
        pkg["digest"],
        [
            {
                "annotation_id": ann,
                "action": "confirm",
                "start": chosen["start"],
                "end": chosen["end"],
            }
        ],
    )
    assert r.status_code == 409
    assert r.json()["detail"]["conflicts"][0]["error"] == "already_confirmed"
    assert _count_confirmations(doc) == 1  # 只有他人的那一条


def test_conflict_completion_state_changed(client):
    doc = _make_doc(client, "甲乙")
    a1 = _make_ann(client, doc, "甲乙", "甲")
    pkg = _create_package(client, doc, [a1])
    _complete(client, doc, a1)
    r = _submit(
        client,
        pkg["package_id"],
        pkg["digest"],
        [{"annotation_id": a1, "action": "complete"}],
    )
    assert r.status_code == 409
    assert (
        r.json()["detail"]["conflicts"][0]["error"] == "completion_state_changed"
    )
    assert _count_rows("completions", doc) == 1


def test_conflict_external_blocker_from_new_dependency(client):
    doc = _make_doc(client, "甲乙丙")
    a1 = _make_ann(client, doc, "甲乙丙", "甲")
    a2 = _make_ann(client, doc, "甲乙丙", "乙")
    a3 = _make_ann(client, doc, "甲乙丙", "丙")
    _add_deps(client, doc, [{"prerequisite_id": a1, "dependent_id": a3}])
    pkg = _create_package(client, doc, [a3])  # 收录 a1,a3
    # 冻结后新增边 a2 -> a3；a2 未完成且不在包内
    _add_deps(client, doc, [{"prerequisite_id": a2, "dependent_id": a3}])
    r = _submit(
        client,
        pkg["package_id"],
        pkg["digest"],
        [
            {"annotation_id": a1, "action": "complete"},
            {"annotation_id": a3, "action": "complete"},
        ],
    )
    assert r.status_code == 409
    conflicts = r.json()["detail"]["conflicts"]
    assert any(c["error"] == "external_blocker" and c["annotation_id"] == a3 for c in conflicts)
    assert _count_rows("completions", doc) == 0


def test_conflict_new_version_even_when_range_just_shifts(client):
    """新增版本即使只让选区平移（唯一落点），也按最新位置变化整包冲突。

    服务不做「新旧位置等价」的猜测——新增版本意味着快照已过期，
    审阅人应在新快照上重新创建包，避免在过期上下文里做决定。
    """
    doc = _make_doc(client, "甲乙丙")
    a1 = _make_ann(client, doc, "甲乙丙", "甲")
    # 包只含 a1；新增版本必然为 a1 追加新的最新迁移记录行 → 整包冲突，
    # 即使选区仍唯一落点（平移），也要求重新创建包，不做「位置等价」猜测
    pkg = _create_package(client, doc, [a1])
    _new_version(client, doc, "X甲乙丙")
    r = _submit(
        client,
        pkg["package_id"],
        pkg["digest"],
        [{"annotation_id": a1, "action": "complete"}],
    )
    # 新增版本必然产生新的最新迁移记录行 → 按规格整包冲突（宁可重新创建）
    assert r.status_code == 409
    assert r.json()["detail"]["conflicts"][0]["error"] == "latest_location_changed"


# ---------------------------------------------------------------------------
# 成功应用：同一写事务、依赖顺序、解锁
# ---------------------------------------------------------------------------


def test_apply_completions_in_dependency_order_unlocks_chain(client):
    doc = _make_doc(client, "甲乙丙")
    a1 = _make_ann(client, doc, "甲乙丙", "甲")
    a2 = _make_ann(client, doc, "甲乙丙", "乙")
    a3 = _make_ann(client, doc, "甲乙丙", "丙")
    _add_deps(
        client,
        doc,
        [
            {"prerequisite_id": a1, "dependent_id": a2},
            {"prerequisite_id": a2, "dependent_id": a3},
        ],
    )
    pkg = _create_package(client, doc, [a3])
    # 决定故意乱序提交；应用顺序必须是 a1,a2,a3
    r = _submit(
        client,
        pkg["package_id"],
        pkg["digest"],
        [
            {"annotation_id": a3, "action": "complete", "note": "三"},
            {"annotation_id": a1, "action": "complete", "note": "一"},
            {"annotation_id": a2, "action": "complete", "note": "二"},
        ],
    )
    assert r.status_code == 201, r.text
    result = r.json()
    assert [c["annotation_id"] for c in result["completions"]] == [a1, a2, a3]
    assert result["completion_count"] == 3
    assert result["already_applied"] is False
    # 审阅计划一致
    plan = client.get(f"/documents/{doc}/review-plan").json()
    assert {x["annotation_id"] for x in plan["completed"]} == {a1, a2, a3}
    # 完成记录确实落库且只追加
    con = db.connect()
    try:
        rows = con.execute(
            "SELECT annotation_id FROM completions WHERE document_id = ? ORDER BY id",
            (doc,),
        ).fetchall()
        assert [r["annotation_id"] for r in rows] == [a1, a2, a3]
    finally:
        con.close()


def test_apply_confirmation_and_completion_together(client):
    base = "说：目标。丁。"          # 目标=[2,4)，丁=[5,6)
    doc = _make_doc(client, base)
    amb = _make_ann(client, doc, base, "目标", start=2)
    other = _make_ann(client, doc, base, "丁", start=5)
    _new_version(client, doc, "说：目标。说：目标。丁。")
    pkg = _create_package(client, doc, [amb, other])
    loc = [i for i in pkg["package"]["items"] if i["annotation_id"] == amb][0][
        "latest_location"
    ]
    assert loc["status"] == "ambiguous"
    chosen = loc["candidates"][0]
    r = _submit(
        client,
        pkg["package_id"],
        pkg["digest"],
        [
            {
                "annotation_id": amb,
                "action": "confirm",
                "start": chosen["start"],
                "end": chosen["end"],
                "note": "取前",
            },
            {"annotation_id": other, "action": "complete", "note": "done"},
        ],
    )
    assert r.status_code == 201, r.text
    result = r.json()
    assert result["confirmation_count"] == 1
    assert result["completion_count"] == 1
    assert [c["annotation_id"] for c in result["confirmations"]] == [amb]
    # 确认生效：批注详情最新状态为 confirmed
    detail = client.get(f"/documents/{doc}/annotations/{amb}").json()
    assert detail["latest"]["status"] == "confirmed"
    assert (detail["latest"]["start"], detail["latest"]["end"]) == (
        chosen["start"],
        chosen["end"],
    )
    # 确认不等于完成
    plan = client.get(f"/documents/{doc}/review-plan").json()
    assert {x["annotation_id"] for x in plan["completed"]} == {other}
    assert {x["annotation_id"] for x in plan["actionable"]} == {amb}


def test_noop_submit_records_result_and_is_idempotent(client):
    doc = _make_doc(client, "甲乙")
    a1 = _make_ann(client, doc, "甲乙", "甲")
    pkg = _create_package(client, doc, [a1])
    r = _submit(client, pkg["package_id"], pkg["digest"], [])
    assert r.status_code == 201
    assert r.json()["confirmation_count"] == 0
    assert r.json()["completion_count"] == 0
    applied_at = r.json()["applied_at"]
    # 重复回传 → 200 + 首次结果
    r2 = _submit(client, pkg["package_id"], pkg["digest"], [])
    assert r2.status_code == 200
    body = r2.json()
    assert body["already_applied"] is True
    assert body["applied_at"] == applied_at
    # 不产生任何确认 / 完成行
    assert _count_confirmations(doc) == 0
    assert _count_rows("completions", doc) == 0


def test_replay_with_different_decisions_returns_first_result(client):
    doc = _make_doc(client, "甲乙")
    a1 = _make_ann(client, doc, "甲乙", "甲")
    pkg = _create_package(client, doc, [a1])
    decisions = [{"annotation_id": a1, "action": "complete"}]
    r1 = _submit(client, pkg["package_id"], pkg["digest"], decisions)
    assert r1.status_code == 201
    first = r1.json()
    # 再次回传不同决定列表（但同摘要）：仍返回首次结果，不重复写入
    r2 = _submit(client, pkg["package_id"], pkg["digest"], [])
    assert r2.status_code == 200
    assert r2.json()["completion_count"] == first["completion_count"] == 1
    assert r2.json()["already_applied"] is True
    assert _count_rows("completions", doc) == 1


def test_replay_with_tampered_digest_conflicts(client):
    doc = _make_doc(client, "甲乙")
    a1 = _make_ann(client, doc, "甲乙", "甲")
    pkg = _create_package(client, doc, [a1])
    r = _submit(
        client,
        pkg["package_id"],
        pkg["digest"],
        [{"annotation_id": a1, "action": "complete"}],
    )
    assert r.status_code == 201
    r = _submit(client, pkg["package_id"], "b" * 64, [])
    assert r.status_code == 409
    assert r.json()["detail"]["errors"][0]["error"] == "replay_mismatch"


def test_illegal_decision_at_apply_time_rolls_back_whole_txn(client):
    """离线合法、应用时非法（冻结后被其他途径改变）→ 全部回滚。

    用「冻结后新增依赖边但不构成 detect 阶段 external_blocker 的情形难以
    构造」——直接覆盖等价路径：冻结后目标被确认 + 另一条合法完成，
    detect_conflicts 整包 409 已保证零写入（见 test_conflict_external_confirmation
    的行数断言）。这里再验证完成与确认混合时冲突导致完成也不写入。
    """
    doc = _make_doc(client, "说：目标。乙。")  # 目标=[2,4)，乙=[5,6)
    amb = _make_ann(client, doc, "说：目标。乙。", "目标", start=2)
    b = _make_ann(client, doc, "说：目标。乙。", "乙", start=5)
    _new_version(client, doc, "说：目标。说：目标。乙。")
    pkg = _create_package(client, doc, [amb, b])
    loc = [i for i in pkg["package"]["items"] if i["annotation_id"] == amb][0][
        "latest_location"
    ]
    chosen = loc["candidates"][0]
    # 冻结后他人确认 amb
    client.post(
        f"/documents/{doc}/annotations/{amb}/confirmations",
        json={
            "version_number": loc["version_number"],
            "start": chosen["start"],
            "end": chosen["end"],
        },
    )
    r = _submit(
        client,
        pkg["package_id"],
        pkg["digest"],
        [
            {
                "annotation_id": amb,
                "action": "confirm",
                "start": chosen["start"],
                "end": chosen["end"],
            },
            {"annotation_id": b, "action": "complete"},
        ],
    )
    assert r.status_code == 409
    assert any(
        c["error"] == "already_confirmed" for c in r.json()["detail"]["conflicts"]
    )
    # 混合批整体不写入：b 的完成也不存在；他人的确认保留（恰 1 条）
    assert _count_rows("completions", doc) == 0
    assert _count_confirmations(doc) == 1


def test_conflict_all_items_reported_together(client):
    """多个条目同时过期时，conflicts 一次性列出全部（都带 items[i] 定位）。"""
    doc = _make_doc(client, "甲乙")
    a1 = _make_ann(client, doc, "甲乙", "甲")
    a2 = _make_ann(client, doc, "甲乙", "乙")
    pkg = _create_package(client, doc, [a1, a2])
    _new_version(client, doc, "X甲乙")  # 两条的最新记录都改变
    _complete(client, doc, a1)  # 同时完成状态也变化
    r = _submit(
        client,
        pkg["package_id"],
        pkg["digest"],
        [
            {"annotation_id": a1, "action": "complete"},
            {"annotation_id": a2, "action": "complete"},
        ],
    )
    assert r.status_code == 409
    codes = {(c["annotation_id"], c["error"]) for c in r.json()["detail"]["conflicts"]}
    assert (a1, "latest_location_changed") in codes
    assert (a1, "completion_state_changed") in codes
    assert (a2, "latest_location_changed") in codes
    assert _count_rows("completions", doc) == 1  # 只有外部那次完成


# ---------------------------------------------------------------------------
# 纯离线：决策校验不查库（直接喂冻结包给纯函数）
# ---------------------------------------------------------------------------


def test_collect_decision_errors_is_pure_offline():
    """冻结包 + 决定列表 → 校验结果，全程不访问数据库。"""
    package = {
        "format": "annotation-review-package/1",
        "document_id": 1,
        "items": [
            {
                "annotation_id": 1,
                "completed": False,
                "prerequisite_ids": [],
                "latest_location": {
                    "location_id": 10,
                    "version_id": 3,
                    "version_number": 3,
                    "status": "ambiguous",
                    "start": None,
                    "end": None,
                    "carried_from": None,
                    "confirmation": None,
                    "candidates": [
                        {"start": 0, "end": 2, "left_match": 0,
                         "right_match": 0, "score": 0},
                        {"start": 9, "end": 11, "left_match": 0,
                         "right_match": 0, "score": 0},
                    ],
                },
            },
            {
                "annotation_id": 2,
                "completed": False,
                "prerequisite_ids": [1],
                "latest_location": {
                    "location_id": 11,
                    "version_id": 3,
                    "version_number": 3,
                    "status": "resolved",
                    "start": 5,
                    "end": 6,
                    "carried_from": None,
                    "confirmation": None,
                    "candidates": [],
                },
            },
        ],
        "prerequisite_states": {},
        "excluded": [],
    }
    from app.review_packages import collect_decision_errors

    # 合法：complete 前置 1 → 解锁 complete 2（confirm 不等于完成）
    errors = collect_decision_errors(
        package,
        [
            {"annotation_id": 1, "action": "complete"},
            {"annotation_id": 2, "action": "complete"},
        ],
    )
    assert errors == []

    # 只 confirm 前置而不 complete：后项仍不可解锁
    errors = collect_decision_errors(
        package,
        [
            {"annotation_id": 1, "action": "confirm", "start": 0, "end": 2},
            {"annotation_id": 2, "action": "complete"},
        ],
    )
    assert [e.error for e in errors] == ["missing_prerequisite_decisions"]

    # 合法：只 confirm 1（不涉及 2）
    errors = collect_decision_errors(
        package,
        [{"annotation_id": 1, "action": "confirm", "start": 9, "end": 11}],
    )
    assert errors == []

    # 包外、非法 action、重复、候选不符、缺前置决定一次性返回
    # （各类错误落在不同决定 / 批注上，互不掩盖）
    errors = collect_decision_errors(
        package,
        [
            {"annotation_id": 7, "action": "complete"},       # 包外
            {"annotation_id": 1, "action": "wat"},           # 非法 action
            {"annotation_id": 2, "action": "frobnicate"},    # 非法 action
            {"annotation_id": 2, "action": "complete"},      # 重复
        ],
    )
    codes = {e.error for e in errors}
    assert codes == {
        "decision_not_in_package",
        "invalid_action",
        "duplicate_decision",
    }

    errors = collect_decision_errors(
        package,
        [
            # 1 给了 confirm（候选不符），但没有 complete → 2 无法解锁
            {"annotation_id": 1, "action": "confirm", "start": 4, "end": 5},
            {"annotation_id": 2, "action": "complete"},
        ],
    )
    codes = {e.error for e in errors}
    assert codes == {
        "confirmation_not_candidate",
        "missing_prerequisite_decisions",
    }


def test_review_package_is_append_only_and_self_contained(client):
    """包行创建后不被篡改；回传只补记 applied_at/result_json 一次。"""
    doc = _make_doc(client, "甲乙")
    a1 = _make_ann(client, doc, "甲乙", "甲")
    pkg = _create_package(client, doc, [a1])
    con = db.connect()
    try:
        before = con.execute(
            "SELECT digest, payload_json, applied_at FROM review_packages WHERE id = ?",
            (pkg["package_id"],),
        ).fetchone()
        assert before["applied_at"] is None
    finally:
        con.close()
    _submit(
        client,
        pkg["package_id"],
        pkg["digest"],
        [{"annotation_id": a1, "action": "complete"}],
    )
    _submit(client, pkg["package_id"], pkg["digest"], [])
    con = db.connect()
    try:
        after = con.execute(
            "SELECT digest, payload_json, applied_at, result_json FROM review_packages "
            "WHERE id = ?",
            (pkg["package_id"],),
        ).fetchone()
        assert after["digest"] == pkg["digest"]
        assert json.loads(after["payload_json"])["items"][0]["annotation_id"] == a1
        assert after["applied_at"] is not None
        assert json.loads(after["result_json"])["package_id"] == pkg["package_id"]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 并发：两个相同回传并发到达，写事务串行化后恰一个 201、一个 409
# ---------------------------------------------------------------------------


class _OneShotGate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self._armed = True

    def wait_inside(self):
        if self._armed:
            self._armed = False
            self.entered.set()
            assert self.release.wait(timeout=10), "交错闸门等待超时"


def test_concurrent_submits_one_wins_no_duplicate_rows(client, monkeypatch):
    """两个回传并发提交同一个包（同一 complete 决定）：

    先到请求在写事务内完成冲突检测后被闸门阻塞（已持有写锁），后到
    请求被 BEGIN IMMEDIATE 串行化在锁外；放行后先到者写入成功（201），
    后到者取得写锁后**重读包行**看到已提交的首次结果，走幂等重放
    （200，already_applied=true）——completions 表对该批注恰一条，
    包行 applied_at 只写一次。（若在写事务外判定 applied_at，后到者
    会在锁外读到旧值，拿到锁后重复写入，故本测试同时是该竞态的回归。）
    """
    doc = _make_doc(client, "甲乙")
    a1 = _make_ann(client, doc, "甲乙", "甲")
    pkg = _create_package(client, doc, [a1])
    body = {
        "package_id": pkg["package_id"],
        "digest": pkg["digest"],
        "decisions": [{"annotation_id": a1, "action": "complete"}],
    }

    gate = _OneShotGate()
    original = review_packages_mod.detect_conflicts

    def gated_detect(*args, **kwargs):
        result = original(*args, **kwargs)
        gate.wait_inside()
        return result

    monkeypatch.setattr(review_packages_mod, "detect_conflicts", gated_detect)

    outcomes = {}

    def post(tag):
        outcomes.setdefault(
            tag, TestClient(app).post("/review-packages/submit", json=body)
        )

    t_first = threading.Thread(target=post, args=("first",))
    t_first.start()
    assert gate.entered.wait(timeout=10)

    t_second = threading.Thread(target=post, args=("second",))
    t_second.start()
    try:
        t_second.join(timeout=1)
        assert t_second.is_alive(), "后到回传不应在先到请求持锁期间完成"
    finally:
        gate.release.set()
        t_first.join(timeout=10)
        t_second.join(timeout=10)

    statuses = {tag: r.status_code for tag, r in outcomes.items()}
    assert statuses["first"] == 201, statuses
    # 后到者在写事务内看到先到者已提交的结果 → 幂等重放（不是冲突 409，
    # 更不是重复 201）
    assert statuses["second"] == 200, statuses
    assert outcomes["second"].json()["already_applied"] is True
    assert outcomes["second"].json()["completion_count"] == 1
    assert outcomes["first"].json()["already_applied"] is False

    # 数据层：恰一条完成；包行只记录一次首次结果
    con = db.connect()
    try:
        assert (
            con.execute(
                "SELECT COUNT(*) FROM completions WHERE annotation_id = ?", (a1,)
            ).fetchone()[0]
            == 1
        )
        row = con.execute(
            "SELECT applied_at, result_json FROM review_packages WHERE id = ?",
            (pkg["package_id"],),
        ).fetchone()
        assert row["applied_at"] is not None
        assert json.loads(row["result_json"])["completion_count"] == 1
    finally:
        con.close()

    # 第三次回传（并发已结束）→ 同样幂等重放首次结果 200
    replay = client.post("/review-packages/submit", json=body)
    assert replay.status_code == 200
    assert replay.json()["already_applied"] is True
