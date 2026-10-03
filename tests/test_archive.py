"""迁移归档导出与离线核验测试。

场景覆盖全部迁移状态：resolved / ambiguous / 顺延占位 / 人工确认 / unresolved，
以及创建于非第 1 版的批注。核验测试不查当前数据库（用归档与摘要本身自证）。
"""
from __future__ import annotations

import copy
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.archive import ARCHIVE_FORMAT, canonical_json, compute_digest


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 造数：一个覆盖全部状态的文档
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fixture(client):
    v1 = "第一段：天气很好。第二段：心情不错。第三段：天气很好。"
    r = client.post("/documents", json={"title": "归档测试文档", "content": v1})
    assert r.status_code == 201, r.text
    doc = r.json()["document_id"]

    # ann1 创建于第 1 版：选区“心情不错”在 [13, 17)
    r = client.post(
        f"/documents/{doc}/annotations",
        json={
            "body": "批注一",
            "selection": "心情不错",
            "left_context": "第二段：",
            "right_context": "。",
            "start": 13,
            "end": 17,
        },
    )
    ann1 = r.json()["annotation_id"]

    # v2：选区平移，唯一落点 → resolved
    r = client.post(f"/documents/{doc}/versions", json={"content": "新增开头。" + v1})
    assert r.json()["results"][0]["status"] == "resolved"

    # v3：两处相同上下文 → ambiguous（两个候选）
    v3 = "第二段：心情不错。第二段：心情不错。"
    r = client.post(f"/documents/{doc}/versions", json={"content": v3})
    res = r.json()["results"][0]
    assert res["status"] == "ambiguous"
    chosen = res["candidates"][1]
    assert len(res["candidates"]) == 2

    # v4：歧义未确认 → 顺延占位
    r = client.post(
        f"/documents/{doc}/versions", json={"content": "第二段：心情不错。"}
    )
    assert r.json()["results"][0]["carried_forward"] is True

    # 人工确认 v3 的歧义（取后一处候选）
    r = client.post(
        f"/documents/{doc}/annotations/{ann1}/confirmations",
        json={
            "version_number": 3,
            "start": chosen["start"],
            "end": chosen["end"],
            "note": "取后一处",
        },
    )
    assert r.status_code == 201, r.text

    # v5：确认后恢复自动迁移 → resolved；在 v5 上创建第二条批注
    v5 = "第二段：心情不错。"
    r = client.post(f"/documents/{doc}/versions", json={"content": v5})
    assert r.json()["results"][0]["status"] == "resolved"
    r = client.post(
        f"/documents/{doc}/annotations",
        json={
            "body": "批注二",
            "selection": "心情不错",
            "left_context": "第二段：",
            "right_context": "。",
            "start": 4,
            "end": 8,
        },
    )
    ann2 = r.json()["annotation_id"]

    # v6：选区消失 → 两条批注都 unresolved
    r = client.post(f"/documents/{doc}/versions", json={"content": "完全改写。"})
    statuses = {(x["annotation_id"], x["status"]) for x in r.json()["results"]}
    assert (ann1, "unresolved") in statuses
    assert (ann2, "unresolved") in statuses

    return {
        "doc": doc,
        "ann1": ann1,
        "ann2": ann2,
        "chosen": chosen,
    }


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _export(client, doc):
    r = client.get(f"/documents/{doc}/archive")
    assert r.status_code == 200, r.text
    data = r.json()
    return data["archive"], data["digest"]


def _verify(client, arch, digest):
    return client.post(
        "/archives/verify", json={"archive": arch, "digest": digest}
    )


def _recompute(arch) -> str:
    """测试侧独立复算（只依赖标准库，模拟离线核验方）。"""
    raw = json.dumps(arch, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _loc_at(arch, ann_index, version_number):
    """取第 ann_index 条批注在第 version_number 版的迁移记录。"""
    aid = arch["annotations"][ann_index]["id"]
    version_id = next(
        v["id"] for v in arch["versions"] if v["number"] == version_number
    )
    return next(
        loc
        for loc in arch["locations"]
        if loc["annotation_id"] == aid and loc["version_id"] == version_id
    )


def _error_codes(resp):
    return [e["error"] for e in resp.json()["detail"]["errors"]]


def _errors(resp):
    return resp.json()["detail"]["errors"]


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


def test_export_unknown_document_404(client, fixture):
    r = client.get("/documents/999999/archive")
    assert r.status_code == 404


def test_export_structure_and_ordering(client, fixture):
    arch, digest = _export(client, fixture["doc"])

    assert arch["format"] == ARCHIVE_FORMAT
    assert arch["document"]["id"] == fixture["doc"]
    assert len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest)

    # 6 个版本、2 条批注；ann1 每版一条（6），ann2 创建于 v5（2），共 8 条
    assert [v["number"] for v in arch["versions"]] == [1, 2, 3, 4, 5, 6]
    assert len(arch["annotations"]) == 2
    assert len(arch["locations"]) == 8
    assert len(arch["confirmations"]) == 1

    # 各列表按行 ID 升序
    for key in ("versions", "annotations", "locations", "confirmations"):
        ids = [row["id"] for row in arch[key]]
        assert ids == sorted(ids)

    # 固定字段集合
    assert set(arch["document"]) == {"id", "title", "created_at"}
    assert set(arch["versions"][0]) == {
        "id", "document_id", "number", "content", "created_at",
    }
    assert set(arch["annotations"][0]) == {
        "id", "document_id", "body", "selection", "left_context",
        "right_context", "orig_start", "orig_end", "created_version_id",
        "created_at",
    }
    assert set(_loc_at(arch, 0, 1)) == {
        "id", "annotation_id", "version_id", "status", "start", "end",
        "left_match", "right_match", "score", "candidates",
        "carried_from", "created_at",
    }
    assert set(arch["confirmations"][0]) == {
        "id", "location_id", "start", "end", "note", "created_at",
    }

    # candidates 已解析为候选数组，而非 JSON 字符串
    v3 = _loc_at(arch, 0, 3)
    assert isinstance(v3["candidates"], list)
    assert set(v3["candidates"][0]) == {
        "start", "end", "left_match", "right_match", "score",
    }

    # 顺延记录的 carried_from 指向 v3 的真实歧义记录
    v4 = _loc_at(arch, 0, 4)
    assert v4["carried_from"] == v3["id"]


def test_export_digest_independently_recomputable(client, fixture):
    arch, digest = _export(client, fixture["doc"])
    # 用标准库按公开规则独立复算，与服务返回一致
    assert _recompute(arch) == digest
    # 规范化序列化确定性
    assert compute_digest(arch) == digest
    assert canonical_json(arch) == canonical_json(copy.deepcopy(arch))


# ---------------------------------------------------------------------------
# 核验通过
# ---------------------------------------------------------------------------


def test_verify_roundtrip(client, fixture):
    arch, digest = _export(client, fixture["doc"])
    r = _verify(client, arch, digest)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "valid": True,
        "format": ARCHIVE_FORMAT,
        "document_id": fixture["doc"],
        "digest": digest,
        "counts": {
            "versions": 6,
            "annotations": 2,
            "locations": 8,
            "confirmations": 1,
        },
    }


def test_verify_accepts_uppercase_digest(client, fixture):
    arch, digest = _export(client, fixture["doc"])
    r = _verify(client, arch, digest.upper())
    assert r.status_code == 200, r.text
    assert r.json()["valid"] is True


def test_verify_is_offline_minimal_archive_not_in_db(client, fixture):
    """文档 ID 在当前数据库中根本不存在，核验仍通过——证明不查库。"""
    arch = {
        "format": ARCHIVE_FORMAT,
        "document": {
            "id": 987654,
            "title": "数据库里不存在的文档",
            "created_at": "2026-01-01T00:00:00.000Z",
        },
        "versions": [
            {
                "id": 1,
                "document_id": 987654,
                "number": 1,
                "content": "x",
                "created_at": "2026-01-01T00:00:00.000Z",
            }
        ],
        "annotations": [],
        "locations": [],
        "confirmations": [],
    }
    r = _verify(client, arch, _recompute(arch))
    assert r.status_code == 200, r.text
    assert r.json()["counts"] == {
        "versions": 1,
        "annotations": 0,
        "locations": 0,
        "confirmations": 0,
    }


def test_verify_does_not_write(client, fixture):
    doc = fixture["doc"]
    _, digest_before = _export(client, doc)
    arch, digest = _export(client, doc)

    # 合法核验与非法核验（422）都不得改动数据
    assert _verify(client, arch, digest).status_code == 200
    tampered = copy.deepcopy(arch)
    assert _verify(client, tampered, "0" * 64).status_code == 422

    _, digest_after = _export(client, doc)
    assert digest_after == digest_before


# ---------------------------------------------------------------------------
# 摘要不符 / 篡改
# ---------------------------------------------------------------------------


def test_verify_detects_tampering_digest_mismatch(client, fixture):
    arch, digest = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    bad["versions"][-1]["content"] = "被篡改的全文"
    r = _verify(client, bad, digest)
    assert r.status_code == 422
    errors = _errors(r)
    assert [e["error"] for e in errors] == ["digest_mismatch"]
    assert errors[0]["path"] == "archive"


def test_verify_wrong_digest(client, fixture):
    arch, _ = _export(client, fixture["doc"])
    r = _verify(client, arch, "0" * 64)
    assert r.status_code == 422
    assert _error_codes(r) == ["digest_mismatch"]


def test_verify_malformed_digest_shape(client, fixture):
    arch, _ = _export(client, fixture["doc"])
    r = _verify(client, arch, "not-a-sha256")
    assert r.status_code == 422  # Pydantic 结构校验，定位到 digest


def test_verify_extra_field_rejected(client, fixture):
    arch, digest = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    bad["unexpected"] = 1
    r = _verify(client, bad, digest)
    assert r.status_code == 422


def test_verify_missing_section_rejected(client, fixture):
    arch, digest = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    del bad["locations"]
    r = _verify(client, bad, digest)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 版本编号连续
# ---------------------------------------------------------------------------


def test_verify_version_gap(client, fixture):
    arch, _ = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    v3_id = next(v["id"] for v in bad["versions"] if v["number"] == 3)
    # 同时删掉第 3 版、其上的迁移记录与确认，只保留版本缺口；
    # v4 顺延记录原本指向 v3 的歧义记录，一并解除以免引入次生错误
    bad["versions"] = [v for v in bad["versions"] if v["number"] != 3]
    bad["locations"] = [
        loc for loc in bad["locations"] if loc["version_id"] != v3_id
    ]
    bad["confirmations"] = []
    _loc_at(bad, 0, 4)["carried_from"] = None
    r = _verify(client, bad, _recompute(bad))
    assert r.status_code == 422
    assert _error_codes(r) == ["version_gap"]
    assert "不连续" in _errors(r)[0]["message"]


def test_verify_duplicate_version_number(client, fixture):
    arch, _ = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    next(v for v in bad["versions"] if v["number"] == 3)["number"] = 2
    r = _verify(client, bad, _recompute(bad))
    assert r.status_code == 422
    codes = _error_codes(r)
    assert "duplicate_version_number" in codes
    assert "version_gap" in codes


def test_verify_empty_versions(client, fixture):
    arch = {
        "format": ARCHIVE_FORMAT,
        "document": {
            "id": 1, "title": "t", "created_at": "2026-01-01T00:00:00.000Z",
        },
        "versions": [],
        "annotations": [],
        "locations": [],
        "confirmations": [],
    }
    r = _verify(client, arch, _recompute(arch))
    assert r.status_code == 422
    assert _error_codes(r) == ["no_versions"]


# ---------------------------------------------------------------------------
# 归属一致 / 串文档 / 缺行
# ---------------------------------------------------------------------------


def test_verify_cross_document_version(client, fixture):
    arch, _ = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    bad["versions"][2]["document_id"] = 999999
    r = _verify(client, bad, _recompute(bad))
    assert r.status_code == 422
    errors = _errors(r)
    assert [e["error"] for e in errors] == ["cross_document"]
    assert errors[0]["path"] == "versions[2]"
    assert "串文档" in errors[0]["message"]


def test_verify_cross_document_annotation(client, fixture):
    arch, _ = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    bad["annotations"][0]["document_id"] = 999999
    r = _verify(client, bad, _recompute(bad))
    assert r.status_code == 422
    assert [e["error"] for e in _errors(r)] == ["cross_document"]


def test_verify_dangling_created_version(client, fixture):
    arch, _ = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    bad["annotations"][1]["created_version_id"] = 555555  # ann2 创建版悬空
    r = _verify(client, bad, _recompute(bad))
    assert r.status_code == 422
    assert _error_codes(r) == ["dangling_created_version"]
    assert _errors(r)[0]["path"] == "annotations[1]"


def test_verify_dangling_annotation(client, fixture):
    arch, _ = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    # ann2 的 v6 迁移记录指向不存在的批注（同时导致 ann2 缺 v6 一行）
    target = _loc_at(bad, 1, 6)
    target["annotation_id"] = 888888
    r = _verify(client, bad, _recompute(bad))
    assert r.status_code == 422
    codes = _error_codes(r)
    assert "dangling_annotation" in codes
    assert "missing_location" in codes  # ann2 在第 6 版缺行


def test_verify_dangling_version(client, fixture):
    arch, _ = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    _loc_at(bad, 0, 2)["version_id"] = 777777
    r = _verify(client, bad, _recompute(bad))
    assert r.status_code == 422
    codes = _error_codes(r)
    assert "dangling_version" in codes
    assert "missing_location" in codes


def test_verify_dangling_confirmation(client, fixture):
    arch, _ = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    bad["confirmations"][0]["location_id"] = 666666
    r = _verify(client, bad, _recompute(bad))
    assert r.status_code == 422
    assert _error_codes(r) == ["dangling_confirmation"]
    assert _errors(r)[0]["path"] == "confirmations[0]"


def test_verify_missing_location(client, fixture):
    arch, _ = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    v4_id = _loc_at(bad, 0, 4)["id"]
    bad["locations"] = [loc for loc in bad["locations"] if loc["id"] != v4_id]
    r = _verify(client, bad, _recompute(bad))
    assert r.status_code == 422
    errors = _errors(r)
    assert [e["error"] for e in errors] == ["missing_location"]
    assert "批注" in errors[0]["message"] and "第 4 版" in errors[0]["message"]
    assert errors[0]["path"] == "annotations[0]"


def test_verify_duplicate_location(client, fixture):
    arch, _ = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    original = _loc_at(bad, 0, 6)
    clone = copy.deepcopy(original)
    clone["id"] = 999999  # 同批注同版本的第二条记录
    bad["locations"].append(clone)
    r = _verify(client, bad, _recompute(bad))
    assert r.status_code == 422
    assert _error_codes(r) == ["duplicate_location"]


def test_verify_location_before_creation_version(client, fixture):
    """批注创建版之前不应有它的迁移记录（伪造 ann2 在 v3 的记录）。"""
    arch, _ = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    v3_id = next(v["id"] for v in bad["versions"] if v["number"] == 3)
    bad["locations"].append(
        {
            "id": 999998,
            "annotation_id": fixture["ann2"],
            "version_id": v3_id,
            "status": "unresolved",
            "start": None,
            "end": None,
            "left_match": None,
            "right_match": None,
            "score": None,
            "candidates": [],
            "carried_from": None,
            "created_at": "2026-01-01T00:00:00.000Z",
        }
    )
    r = _verify(client, bad, _recompute(bad))
    assert r.status_code == 422
    assert _error_codes(r) == ["unexpected_location"]


# ---------------------------------------------------------------------------
# 顺延引用
# ---------------------------------------------------------------------------


def test_verify_carried_forward_must_target_real_ambiguous(client, fixture):
    arch, _ = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    # v4 顺延记录改指向 v5 的 resolved 记录（同批注、但更晚且非歧义）
    v4 = _loc_at(bad, 0, 4)
    v5 = _loc_at(bad, 0, 5)
    v4["carried_from"] = v5["id"]
    r = _verify(client, bad, _recompute(bad))
    assert r.status_code == 422
    errors = _errors(r)
    assert all(e["error"] == "bad_carried_forward" for e in errors)
    assert len(errors) == 2  # 指向更晚版本 + 不是真实歧义记录
    assert errors[0]["path"] == "locations[" + str(
        [loc["id"] for loc in bad["locations"]].index(v4["id"])
    ) + "]"


def test_verify_carried_forward_cross_annotation(client, fixture):
    arch, _ = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    # v4 顺延记录改指向 ann2 在 v5 的记录（不同批注）
    _loc_at(bad, 0, 4)["carried_from"] = _loc_at(bad, 1, 5)["id"]
    r = _verify(client, bad, _recompute(bad))
    assert r.status_code == 422
    errors = _errors(r)
    assert [e["error"] for e in errors] == ["bad_carried_forward"]
    assert "同一批注" in errors[0]["message"]


def test_verify_carried_forward_dangling(client, fixture):
    arch, _ = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    _loc_at(bad, 0, 4)["carried_from"] = 424242
    r = _verify(client, bad, _recompute(bad))
    assert r.status_code == 422
    assert _error_codes(r) == ["dangling_carried_from"]


# ---------------------------------------------------------------------------
# 人工确认
# ---------------------------------------------------------------------------


def test_verify_confirmation_must_target_non_carried_ambiguous(client, fixture):
    arch, _ = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    # 确认改指向 v4 的顺延占位记录
    v4 = _loc_at(bad, 0, 4)
    bad["confirmations"][0]["location_id"] = v4["id"]
    r = _verify(client, bad, _recompute(bad))
    assert r.status_code == 422
    errors = _errors(r)
    assert [e["error"] for e in errors] == ["bad_confirmation"]
    assert "顺延占位" in errors[0]["message"]


def test_verify_confirmation_on_resolved_rejected(client, fixture):
    arch, _ = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    # 确认改指向 v5 的 resolved 记录
    bad["confirmations"][0]["location_id"] = _loc_at(bad, 0, 5)["id"]
    r = _verify(client, bad, _recompute(bad))
    assert r.status_code == 422
    assert _error_codes(r) == ["bad_confirmation"]


def test_verify_confirmation_range_must_be_candidate(client, fixture):
    arch, _ = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    bad["confirmations"][0]["start"] = 0
    bad["confirmations"][0]["end"] = 1  # v3 候选为 [4,8) 与 [13,17)
    r = _verify(client, bad, _recompute(bad))
    assert r.status_code == 422
    errors = _errors(r)
    assert [e["error"] for e in errors] == ["confirmation_not_candidate"]
    assert "候选落点" in errors[0]["message"]
    assert errors[0]["path"] == "confirmations[0]"


def test_verify_multiple_errors_reported_together(client, fixture):
    """摘要合法但内容有多类问题时，一次性给出全部可定位错误。"""
    arch, _ = _export(client, fixture["doc"])
    bad = copy.deepcopy(arch)
    # 问题 1：版本串文档
    bad["versions"][0]["document_id"] = 999999
    # 问题 2：删掉 v4 顺延记录 → ann1 缺第 4 版
    v4_id = _loc_at(bad, 0, 4)["id"]
    bad["locations"] = [loc for loc in bad["locations"] if loc["id"] != v4_id]
    # 问题 3：确认范围不在候选中
    bad["confirmations"][0]["start"] = 0
    bad["confirmations"][0]["end"] = 1
    r = _verify(client, bad, _recompute(bad))
    assert r.status_code == 422
    codes = set(_error_codes(r))
    assert {"cross_document", "missing_location", "confirmation_not_candidate"} <= codes
    # 每条错误都带定位
    assert all(e["path"] for e in _errors(r))
