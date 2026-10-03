"""文档审阅发布候选测试：同读事务冻结、同写事务确认、冲突与幂等。"""
import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import db
from app import release_candidates as rc_mod
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def _make_doc(client, content="甲乙丙丁", title="发布候选测试文档"):
    r = client.post("/documents", json={"title": title, "content": content})
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


def _make_ann(client, doc, text, selection, body="批注"):
    start = text.index(selection)
    r = client.post(
        f"/documents/{doc}/annotations",
        json={"body": body, "selection": selection,
              "start": start, "end": start + len(selection)},
    )
    assert r.status_code == 201, r.text
    return r.json()["annotation_id"]


def _new_version(client, doc, content):
    r = client.post(f"/documents/{doc}/versions", json={"content": content})
    assert r.status_code == 201, r.text
    return r.json()


def _put_policy(client, name, strategy):
    r = client.post("/policies", json={"name": name, "strategy": strategy})
    assert r.status_code == 201, r.text
    return r.json()["revision"]


def _all_resolved_policy(name):
    # 无 unresolved 且无 ambiguous：v1 上新批注全部 resolved
    return {"all": [
        {"location_count": {"status": "unresolved", "op": "eq", "value": 0}},
        {"location_count": {"status": "ambiguous", "op": "eq", "value": 0}},
    ]}


def _create_candidate(client, doc, policy, revision=None, version=None, expected=201):
    body = {"policy": policy}
    if revision is not None:
        body["policy_revision"] = revision
    if version is not None:
        body["version_number"] = version
    r = client.post(f"/documents/{doc}/release-candidates", json=body)
    assert r.status_code == expected, r.text
    return r


def _setup_doc_with_anns(client, text="甲乙丙", selections=("甲", "乙", "丙")):
    doc = _make_doc(client, text)
    aids = [_make_ann(client, doc, text, s) for s in selections]
    return doc, aids


# ---------------------------------------------------------------------------
# 创建：门禁通过 → 不可变 candidate_id / 摘要 / 冻结内容
# ---------------------------------------------------------------------------


def test_create_candidate_locks_snapshot(client):
    doc, aids = _setup_doc_with_anns(client)
    rev = _put_policy(client, "候选-基本", _all_resolved_policy("候选-基本"))
    r = _create_candidate(client, doc, "候选-基本")
    body = r.json()

    # 不可变 candidate_id：随机 32 位十六进制
    cid = body["candidate_id"]
    assert isinstance(cid, str) and len(cid) == 32
    int(cid, 16)
    # 规范摘要：64 位十六进制，且可对冻结内容独立复算
    assert len(body["digest"]) == 64
    frozen = body["frozen"]
    assert rc_mod.compute_digest(frozen) == body["digest"]
    assert body["format"] == rc_mod.CANDIDATE_FORMAT
    assert body["status"] == "pending"
    assert body["released_at"] is None

    # 锁定策略修订、目标版本
    assert body["policy_name"] == "候选-基本"
    assert body["policy_revision"] == rev
    assert frozen["policy"] == {"name": "候选-基本", "revision": rev,
                                "strategy": _all_resolved_policy("候选-基本")}
    assert frozen["version"]["number"] == 1
    assert frozen["version"]["content"] == "甲乙丙"

    # 冻结门禁解释树（通过）与当前审阅状态
    assert frozen["gate"]["passed"] is True
    assert frozen["gate"]["explanation"]["result"] is True
    assert frozen["review_state"] == {
        "actionable": aids,
        "blocked": [],
        "completed": [],
    }
    # 六类事件水位：该文档各类只追加行的最大行 ID（无则 0）。
    # 行 ID 为库内自增（非按文档计数），这里只校验键集与相对关系。
    wm = frozen["watermark"]
    assert set(wm) == {
        "versions", "annotations", "locations",
        "confirmations", "dependencies", "completions",
    }
    assert wm["versions"] >= 1
    assert wm["annotations"] >= aids[-1]
    assert wm["locations"] >= 1
    assert wm["confirmations"] == wm["dependencies"] == wm["completions"] == 0


def test_candidate_ids_are_random_even_for_identical_snapshot(client):
    doc, _ = _setup_doc_with_anns(client, text="甲乙", selections=("甲", "乙"))
    _put_policy(client, "候选-随机ID", _all_resolved_policy("候选-随机ID"))
    c1 = _create_candidate(client, doc, "候选-随机ID").json()["candidate_id"]
    c2 = _create_candidate(client, doc, "候选-随机ID").json()["candidate_id"]
    assert c1 != c2  # 与内容无关：同内容两次创建是两个候选


def test_create_locks_explicit_policy_revision_and_version(client):
    text = "甲乙"
    doc, _ = _setup_doc_with_anns(client, text=text, selections=("甲", "乙"))
    name = "候选-锁定旧修订"
    rev1 = _put_policy(client, name, _all_resolved_policy(name))
    # 同名再提交：追加第 2 版（要求 2 条 completed，当前并不满足）
    rev2 = _put_policy(
        client, name,
        {"review_count": {"status": "completed", "op": "gte", "value": 2}},
    )
    # 新版本使乙 unresolved（甲仍 resolved）
    _new_version(client, doc, "甲X")

    # 显式锁定 rev1 + 第 1 版：旧版上没有 unresolved → 通过
    body = _create_candidate(client, doc, name, revision=rev1, version=1).json()
    assert body["policy_revision"] == rev1
    assert body["version_number"] == 1

    # 缺省取最新 rev2：门禁不通过 → 409
    r = client.post(
        f"/documents/{doc}/release-candidates", json={"policy": name}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["policy_revision"] == rev2


# ---------------------------------------------------------------------------
# 创建：初始门禁未通过 → 拒绝创建且不落候选
# ---------------------------------------------------------------------------


def test_create_rejected_when_gate_fails_and_no_row_persisted(client):
    doc, aids = _setup_doc_with_anns(client)
    name = "候选-初始不过"
    _put_policy(
        client, name,
        {"review_count": {"status": "completed", "op": "eq", "value": 3}},
    )
    r = client.post(f"/documents/{doc}/release-candidates", json={"policy": name})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["passed"] is False
    errors = detail["errors"]
    assert len(errors) == 1 and errors[0]["error"] == "gate_not_passed"
    tree = detail["explanation"]
    assert tree["result"] is False
    # 叶子实际计数 0，阈值 3，参与计数的批注 ID 为空
    assert tree["review_count"]["actual"] == 0
    assert tree["review_count"]["value"] == 3
    assert tree["review_count"]["annotation_ids"] == []
    assert "candidate_id" not in detail

    # 拒绝创建不得落候选行
    con = db.connect()
    try:
        n = con.execute(
            "SELECT COUNT(*) AS n FROM release_candidates WHERE document_id = ?",
            (doc,),
        ).fetchone()["n"]
    finally:
        con.close()
    assert n == 0


def test_create_location_gate_failure_explanation(client):
    text = "甲乙"
    doc, _ = _setup_doc_with_anns(client, text=text, selections=("甲", "乙"))
    _new_version(client, doc, "甲X")  # 乙 unresolved
    name = "候选-不许unresolved"
    _put_policy(
        client, name,
        {"location_count": {"status": "unresolved", "op": "eq", "value": 0}},
    )
    r = client.post(f"/documents/{doc}/release-candidates", json={"policy": name})
    assert r.status_code == 409
    leaf = r.json()["detail"]["explanation"]["location_count"]
    assert leaf["actual"] == 1
    # 评估目标是最新版（第 2 版），乙在该版 unresolved


def test_create_errors_not_found(client):
    doc = _make_doc(client)
    # 策略不存在
    r = client.post(
        f"/documents/{doc}/release-candidates", json={"policy": "幽灵策略"}
    )
    assert r.status_code == 404
    assert r.json()["detail"]["errors"][0]["error"] == "policy_not_found"
    # 策略修订不存在
    _put_policy(client, "存在策略", _all_resolved_policy("存在策略"))
    r = client.post(
        f"/documents/{doc}/release-candidates",
        json={"policy": "存在策略", "policy_revision": 99},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["errors"][0]["error"] == "policy_not_found"
    # 版本不存在
    r = client.post(
        f"/documents/{doc}/release-candidates",
        json={"policy": "存在策略", "version_number": 99},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["errors"][0]["error"] == "version_not_found"
    # 文档不存在
    r = client.post(
        "/documents/999999/release-candidates", json={"policy": "存在策略"}
    )
    assert r.status_code == 404


def test_create_no_version_conflict(client):
    # 文档没有任何版本（经正常接口无法构造：建文档即第 1 版；直接插文档行）
    con = db.connect()
    try:
        with db.transaction(con):
            cur = con.execute(
                "INSERT INTO documents (title) VALUES (?)", ("空版本文档",)
            )
            doc = cur.lastrowid
    finally:
        con.close()
    _put_policy(client, "空版本策略", _all_resolved_policy("空版本策略"))
    r = client.post(
        f"/documents/{doc}/release-candidates", json={"policy": "空版本策略"}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["errors"][0]["error"] == "no_version"


# ---------------------------------------------------------------------------
# 确认：水位未变且门禁通过 → 追加一次 released
# ---------------------------------------------------------------------------


def test_confirm_success_appends_single_result(client):
    doc, _ = _setup_doc_with_anns(client, text="甲", selections=("甲",))
    _put_policy(client, "候选-确认成功", _all_resolved_policy("候选-确认成功"))
    cid = _create_candidate(client, doc, "候选-确认成功").json()["candidate_id"]

    r = client.post(f"/release-candidates/{cid}/confirm")
    assert r.status_code == 201
    body = r.json()
    assert body["outcome"] == "released"
    assert body["already_released"] is False
    assert body["candidate_id"] == cid
    released_at = body["released_at"]
    assert isinstance(released_at, str) and released_at

    # 候选行状态变为 released（冻结内容不变）
    con = db.connect()
    try:
        row = con.execute(
            "SELECT released_at, result_json, frozen_json, digest "
            "FROM release_candidates WHERE id = ?",
            (cid,),
        ).fetchone()
    finally:
        con.close()
    assert row["released_at"] == released_at
    result = json.loads(row["result_json"])
    assert result["outcome"] == "released"
    assert result["released_at"] == released_at
    assert result["digest"] == row["digest"]
    assert json.loads(row["frozen_json"])["gate"]["passed"] is True


def test_confirm_idempotent_replay(client):
    doc, _ = _setup_doc_with_anns(client, text="甲", selections=("甲",))
    _put_policy(client, "候选-幂等", _all_resolved_policy("候选-幂等"))
    cid = _create_candidate(client, doc, "候选-幂等").json()["candidate_id"]

    first = client.post(f"/release-candidates/{cid}/confirm")
    assert first.status_code == 201
    second = client.post(f"/release-candidates/{cid}/confirm")
    assert second.status_code == 200
    b1, b2 = first.json(), second.json()
    assert b2["already_released"] is True
    assert b2["released_at"] == b1["released_at"]
    assert b2["digest"] == b1["digest"]

    # 只写了一次结果行（released_at 未被改写；结果 JSON 只有首次内容）
    con = db.connect()
    try:
        n = con.execute(
            "SELECT COUNT(*) AS n FROM release_candidates WHERE id = ? "
            "AND released_at IS NOT NULL",
            (cid,),
        ).fetchone()["n"]
    finally:
        con.close()
    assert n == 1


def test_concurrent_confirm_single_release():
    # 并发确认同一候选：BEGIN IMMEDIATE 串行化，先到者 201 发布，
    # 后到者取得写锁后重读候选行走幂等重放（200），结果只写一次。
    # 每个线程使用独立 TestClient（各自持有独立 portal 线程），避免共享
    # TestClient 的线程池把同步依赖的 enter/exit 派到不同 worker 线程而
    # 触发 sqlite3 check_same_thread（那只是测试夹具层面的限制，与
    # uvicorn 下每请求独立连接的真实行为无关）。
    text = "甲"
    with TestClient(app) as setup:
        doc = _make_doc(setup, text)
        _make_ann(setup, doc, text, "甲")
        _put_policy(setup, "候选-并发确认", _all_resolved_policy("候选-并发确认"))
        cid = _create_candidate(setup, doc, "候选-并发确认").json()["candidate_id"]

    outcomes = []
    outcomes_lock = threading.Lock()

    def worker():
        with TestClient(app) as local:
            r = local.post(f"/release-candidates/{cid}/confirm")
        with outcomes_lock:
            outcomes.append((r.status_code, r.json()))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(outcomes) == 4
    statuses = sorted(code for code, _ in outcomes)
    assert statuses == [200, 200, 200, 201]
    released_ats = {body["released_at"] for _, body in outcomes}
    assert len(released_ats) == 1
    assert all(body["outcome"] == "released" for _, body in outcomes)

    con = db.connect()
    try:
        row = con.execute(
            "SELECT released_at, result_json FROM release_candidates WHERE id = ?",
            (cid,),
        ).fetchone()
    finally:
        con.close()
    assert row["released_at"] is not None
    assert json.loads(row["result_json"])["released_at"] == released_ats.pop()


def test_confirm_unknown_candidate_404(client):
    r = client.post("/release-candidates/" + "0" * 32 + "/confirm")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 确认：六类事件任一新增 → watermark_changed，可定位且状态不变
# ---------------------------------------------------------------------------


def _pending_candidate(client, policy_name):
    text = "甲乙丙"
    doc, aids = _setup_doc_with_anns(client, text=text)
    _put_policy(client, policy_name, _all_resolved_policy(policy_name))
    cid = _create_candidate(client, doc, policy_name).json()["candidate_id"]
    return doc, aids, cid


def _candidate_watermark(cid):
    con = db.connect()
    try:
        row = con.execute(
            "SELECT frozen_json FROM release_candidates WHERE id = ?", (cid,)
        ).fetchone()
    finally:
        con.close()
    return json.loads(row["frozen_json"])["watermark"]


def _assert_conflict(client, cid, error, path, expected=409):
    r = client.post(f"/release-candidates/{cid}/confirm")
    assert r.status_code == expected, r.text
    conflicts = r.json()["detail"]["conflicts"]
    match = [c for c in conflicts if c["error"] == error and c["path"] == path]
    assert len(match) == 1, conflicts
    return conflicts, match[0]


def test_confirm_conflict_new_version(client):
    doc, aids, cid = _pending_candidate(client, "候选-冲突版本")
    before = _candidate_watermark(cid)
    # 内容不变的新版（版本号 +1）：三条批注仍 resolved，门禁复评仍通过；
    # 但提交新版同时追加版本行与三条新迁移行 → 两类水位冲突
    _new_version(client, doc, "甲乙丙")
    conflicts, c = _assert_conflict(client, cid, "watermark_changed",
                                   "watermark.versions")
    assert c["event_type"] == "version"
    assert c["frozen_watermark"] == before["versions"]
    assert c["current_watermark"] > before["versions"]
    assert {x["path"] for x in conflicts} == {
        "watermark.versions", "watermark.locations"
    }


def test_confirm_conflict_new_annotation(client):
    doc, aids, cid = _pending_candidate(client, "候选-冲突批注")
    before = _candidate_watermark(cid)
    new_aid = _make_ann(client, doc, "甲乙丙", "丙")  # 同选区新批注（v1 上 resolved）
    # 创建批注同时追加批注行与其在 v1 上的初始迁移行 → 两类水位冲突
    conflicts, c = _assert_conflict(client, cid, "watermark_changed",
                                    "watermark.annotations")
    assert c["event_type"] == "annotation"
    assert c["frozen_watermark"] == before["annotations"]
    assert c["current_watermark"] >= new_aid
    assert {x["path"] for x in conflicts} == {
        "watermark.annotations", "watermark.locations"
    }


def test_confirm_conflict_new_location(client):
    doc, aids, cid = _pending_candidate(client, "候选-冲突迁移")
    before = _candidate_watermark(cid)
    # 提交新版触发全部批注迁移（3 条新 locations）
    _new_version(client, doc, "x甲乙丙")
    conflicts, c = _assert_conflict(client, cid, "watermark_changed",
                                    "watermark.locations")
    assert c["event_type"] == "location"
    assert c["frozen_watermark"] == before["locations"]
    assert c["current_watermark"] == before["locations"] + 3
    # 版本也同时新增 → 两类水位冲突都列出
    paths = {x["path"] for x in conflicts}
    assert paths == {"watermark.versions", "watermark.locations"}


def test_confirm_conflict_new_confirmation(client):
    # v1 唯一落点 resolved；v2 同选区出现两次 → 非顺延的真实歧义（可确认）
    text = "前缀目标"
    doc = _make_doc(client, text)
    aid = _make_ann(client, doc, text, "目标")
    _new_version(client, doc, "目标目标")  # v2 两处相同上下文 → ambiguous
    name = "候选-冲突确认"
    # 允许 ambiguous 存在的通过策略（无 unresolved 即可）
    _put_policy(
        client, name,
        {"location_count": {"status": "unresolved", "op": "eq", "value": 0}},
    )
    cid = _create_candidate(client, doc, name).json()["candidate_id"]
    before = _candidate_watermark(cid)

    # 取 v2 歧义记录的候选范围并人工确认
    hist = client.get(
        f"/documents/{doc}/annotations/{aid}/history"
    ).json()
    v2 = next(x for x in hist["history"] if x["version_number"] == 2)
    assert v2["status"] == "ambiguous" and v2["carried_forward"] is False
    cand = v2["candidates"][0]
    r = client.post(
        f"/documents/{doc}/annotations/{aid}/confirmations",
        json={"version_number": 2, "start": cand["start"],
              "end": cand["end"], "note": "确认"},
    )
    assert r.status_code in (200, 201), r.text
    _, c = _assert_conflict(client, cid, "watermark_changed",
                            "watermark.confirmations")
    assert c["event_type"] == "confirmation"
    assert c["frozen_watermark"] == before["confirmations"]
    assert c["current_watermark"] > before["confirmations"]


def test_confirm_conflict_new_dependency(client):
    doc, aids, cid = _pending_candidate(client, "候选-冲突依赖")
    r = client.post(
        f"/documents/{doc}/dependencies",
        json={"relations": [{"prerequisite_id": aids[0],
                             "dependent_id": aids[1]}]},
    )
    assert r.status_code == 201, r.text
    _, c = _assert_conflict(client, cid, "watermark_changed",
                            "watermark.dependencies")
    assert c["event_type"] == "dependency"


def test_confirm_conflict_new_completion(client):
    doc, aids, cid = _pending_candidate(client, "候选-冲突完成")
    before = _candidate_watermark(cid)
    r = client.post(
        f"/documents/{doc}/annotations/{aids[0]}/completions", json={}
    )
    assert r.status_code in (200, 201), r.text
    _, c = _assert_conflict(client, cid, "watermark_changed",
                            "watermark.completions")
    assert c["event_type"] == "completion"
    assert c["frozen_watermark"] == before["completions"]
    assert c["current_watermark"] > before["completions"]


def test_confirm_conflict_does_not_change_candidate(client):
    doc, aids, cid = _pending_candidate(client, "候选-冲突不改状态")
    client.post(
        f"/documents/{doc}/annotations/{aids[0]}/completions", json={}
    )
    # 连续两次确认都 409，候选仍 pending
    for _ in range(2):
        r = client.post(f"/release-candidates/{cid}/confirm")
        assert r.status_code == 409
    con = db.connect()
    try:
        row = con.execute(
            "SELECT released_at, result_json FROM release_candidates WHERE id = ?",
            (cid,),
        ).fetchone()
    finally:
        con.close()
    assert row["released_at"] is None
    assert row["result_json"] is None


# ---------------------------------------------------------------------------
# 确认：六类事件未变但门禁不再通过（直接改库构造，含防御性复评）
# ---------------------------------------------------------------------------


def test_confirm_conflict_gate_no_longer_passes_without_watermark_change(client):
    # 策略「unresolved 数 == 0」在 v1 通过；候选创建后不改六类事件水位，
    # 直接把一条已落库的迁移记录改为 unresolved（模拟绕过 API 的状态变更），
    # 写事务内复评门禁必须拦截。
    text = "甲乙"
    doc = _make_doc(client, text)
    aid1 = _make_ann(client, doc, text, "甲")
    aid2 = _make_ann(client, doc, text, "乙")
    name = "候选-复评拦截"
    _put_policy(
        client, name,
        {"location_count": {"status": "unresolved", "op": "eq", "value": 0}},
    )
    cid = _create_candidate(client, doc, name).json()["candidate_id"]

    # 找到批注 aid2 在 v1 上的 location 行，直接改成 unresolved
    con = db.connect()
    try:
        with db.transaction(con):
            con.execute(
                "UPDATE locations SET status = 'unresolved', start = NULL, "
                '"end" = NULL WHERE annotation_id = ? AND version_id = ('
                "SELECT id FROM versions WHERE document_id = ? AND number = 1)",
                (aid2, doc),
            )
    finally:
        con.close()

    r = client.post(f"/release-candidates/{cid}/confirm")
    assert r.status_code == 409
    conflicts = r.json()["detail"]["conflicts"]
    # 六类水位都未变（UPDATE 不改行 ID）→ 无水位冲突，仅门禁冲突
    assert [c["error"] for c in conflicts] == ["gate_not_passed"]
    gate_c = conflicts[0]
    assert gate_c["path"] == "gate"
    assert gate_c["explanation"]["result"] is False
    assert gate_c["explanation"]["location_count"]["actual"] == 1
    assert gate_c["explanation"]["location_count"]["annotation_ids"] == [aid2]


def test_confirm_reports_both_watermark_and_gate_conflicts(client):
    # 水位变化与门禁失效同时存在时，两类冲突一次性返回
    text = "甲乙"
    doc = _make_doc(client, text)
    aid1 = _make_ann(client, doc, text, "甲")
    aid2 = _make_ann(client, doc, text, "乙")
    name = "候选-双重冲突"
    _put_policy(
        client, name,
        {"location_count": {"status": "unresolved", "op": "eq", "value": 0}},
    )
    cid = _create_candidate(client, doc, name).json()["candidate_id"]
    # 新增完成记录（推高 completions 水位），同时把另一条 location 直接改
    # unresolved（UPDATE 不改水位，但使门禁复评失败）
    con = db.connect()
    try:
        with db.transaction(con):
            con.execute(
                "INSERT INTO completions (document_id, annotation_id, note)"
                " VALUES (?, ?, NULL)",
                (doc, aid1),
            )
            con.execute(
                "UPDATE locations SET status = 'unresolved', start = NULL, "
                '"end" = NULL WHERE annotation_id = ? AND version_id = ('
                "SELECT id FROM versions WHERE document_id = ? AND number = 1)",
                (aid2, doc),
            )
    finally:
        con.close()
    r = client.post(f"/release-candidates/{cid}/confirm")
    assert r.status_code == 409
    errors = {c["error"] for c in r.json()["detail"]["conflicts"]}
    assert errors == {"watermark_changed", "gate_not_passed"}


# ---------------------------------------------------------------------------
# 摘要 / 冻结确定性
# ---------------------------------------------------------------------------


def test_digest_is_canonical_sha256_and_stable(client):
    doc, _ = _setup_doc_with_anns(client, text="甲", selections=("甲",))
    name = "候选-摘要"
    _put_policy(client, name, _all_resolved_policy(name))
    body = _create_candidate(client, doc, name).json()
    import hashlib

    expected = hashlib.sha256(
        json.dumps(body["frozen"], ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert body["digest"] == expected


def test_other_documents_events_do_not_conflict(client):
    # 单文档水位：其他文档的六类事件不影响候选确认
    doc, _ = _setup_doc_with_anns(client, text="甲", selections=("甲",))
    name = "候选-跨文档隔离"
    _put_policy(client, name, _all_resolved_policy(name))
    cid = _create_candidate(client, doc, name).json()["candidate_id"]

    other = _make_doc(client, content="子丑", title="另一篇文档")
    _make_ann(client, other, "子丑", "子")
    _new_version(client, other, "子寅")
    r = client.post(f"/release-candidates/{cid}/confirm")
    assert r.status_code == 201
    assert r.json()["outcome"] == "released"
