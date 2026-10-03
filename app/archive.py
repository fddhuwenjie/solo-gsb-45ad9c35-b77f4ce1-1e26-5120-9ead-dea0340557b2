"""迁移归档：规范导出、SHA-256 摘要与离线核验。

导出（:func:`build_archive`）在调用方持有的**同一个 SQLite 读事务**中取出
文档元数据、全部版本、批注、迁移记录与人工确认，按固定字段、ID 升序组装；
摘要为规范化 JSON 的 SHA-256：

    digest = sha256(json.dumps(archive, ensure_ascii=False,
                               sort_keys=True, separators=(",", ":")))

规范化规则（键按字典序、紧凑分隔、UTF-8 不转义）公开且只依赖标准库，
任何人拿到归档与摘要都可离线复算。

核验（:func:`verify_archive`）**不查数据库、不写入任何数据**，只依据归档与
摘要本身：复算摘要，并检查版本编号连续、各记录归属一致、每条批注从创建版
到末版每版恰有一条迁移记录、顺延引用合法、人工确认合法。所有问题以
:class:`ArchiveError`（带 JSON 路径式定位）返回。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Dict, List, Optional

ARCHIVE_FORMAT = "annotation-archive/1"

# 固定导出字段（显式列出，不依赖 SELECT * 的列序）
_DOCUMENT_FIELDS = ("id", "title", "created_at")
_VERSION_FIELDS = ("id", "document_id", "number", "content", "created_at")
_ANNOTATION_FIELDS = (
    "id",
    "document_id",
    "body",
    "selection",
    "left_context",
    "right_context",
    "orig_start",
    "orig_end",
    "created_version_id",
    "created_at",
)
_LOCATION_FIELDS = (
    "id",
    "annotation_id",
    "version_id",
    "status",
    "start",
    "end",
    "left_match",
    "right_match",
    "score",
    "candidates",
    "carried_from",
    "created_at",
)
_CONFIRMATION_FIELDS = ("id", "location_id", "start", "end", "note", "created_at")


class ArchiveError(Exception):
    """归档核验发现的一条可定位错误。

    - ``error``：机器可读的错误码；
    - ``message``：含业务 ID 的中文说明；
    - ``path``：JSON 路径式定位（如 ``locations[3]``、``annotations[1]``）。
    """

    def __init__(self, error: str, message: str, path: str):
        super().__init__(message)
        self.error = error
        self.message = message
        self.path = path

    def to_dict(self) -> Dict[str, str]:
        return {"error": self.error, "message": self.message, "path": self.path}


def canonical_json(obj: Any) -> str:
    """规范化 JSON：键按字典序、紧凑分隔、非 ASCII 不转义。

    相同内容（含嵌套字典的键集合）必然得到相同字节序列；
    数组顺序按归档中固定的 ID 升序，不做重排。
    """
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_digest(archive: Dict[str, Any]) -> str:
    """归档摘要：规范化 JSON 的 UTF-8 字节的 SHA-256（小写十六进制）。"""
    return hashlib.sha256(canonical_json(archive).encode("utf-8")).hexdigest()


def build_archive(con: sqlite3.Connection, document_id: int) -> Dict[str, Any]:
    """在调用方持有的读事务内组装指定文档的归档。

    所有列表均按行 ID 升序；假设文档存在（由调用方在同一事务内先校验）。
    """
    doc = con.execute(
        f"SELECT {', '.join(_DOCUMENT_FIELDS)} FROM documents WHERE id = ?",
        (document_id,),
    ).fetchone()

    versions = con.execute(
        f"SELECT {', '.join(_VERSION_FIELDS)} FROM versions "
        "WHERE document_id = ? ORDER BY id",
        (document_id,),
    ).fetchall()

    annotations = con.execute(
        f"SELECT {', '.join(_ANNOTATION_FIELDS)} FROM annotations "
        "WHERE document_id = ? ORDER BY id",
        (document_id,),
    ).fetchall()

    # 迁移记录与确认经由 annotations 限定在本文档内，
    # 不会把别的文档中碰巧 ID 相同的行带进归档（防串文档）。
    location_rows = con.execute(
        """
        SELECT l.id, l.annotation_id, l.version_id, l.status, l.start, l."end",
               l.left_match, l.right_match, l.score, l.candidates,
               l.carried_from, l.created_at
        FROM locations l
        JOIN annotations a ON a.id = l.annotation_id
        WHERE a.document_id = ?
        ORDER BY l.id
        """,
        (document_id,),
    ).fetchall()

    confirmation_rows = con.execute(
        """
        SELECT c.id, c.location_id, c.start, c."end", c.note, c.created_at
        FROM confirmations c
        JOIN locations l ON l.id = c.location_id
        JOIN annotations a ON a.id = l.annotation_id
        WHERE a.document_id = ?
        ORDER BY c.id
        """,
        (document_id,),
    ).fetchall()

    locations: List[Dict[str, Any]] = []
    for row in location_rows:
        item = {key: row[key] for key in _LOCATION_FIELDS if key != "candidates"}
        # candidates 在库中以 JSON 文本保存，归档中解析为数组，保证归档自描述
        item["candidates"] = json.loads(row["candidates"])
        locations.append(item)

    return {
        "format": ARCHIVE_FORMAT,
        "document": {key: doc[key] for key in _DOCUMENT_FIELDS},
        "versions": [{key: row[key] for key in _VERSION_FIELDS} for row in versions],
        "annotations": [
            {key: row[key] for key in _ANNOTATION_FIELDS} for row in annotations
        ],
        "locations": locations,
        "confirmations": [
            {key: row[key] for key in _CONFIRMATION_FIELDS}
            for row in confirmation_rows
        ],
    }


def verify_archive(archive: Dict[str, Any], digest: str) -> List[ArchiveError]:
    """离线核验归档与摘要。

    返回错误列表（空列表表示通过）。不查数据库、不写入数据。
    顺序：先复算摘要（不符则只报摘要问题，避免对被篡改内容做无意义的语义
    检查），再做结构语义检查；语义检查尽量一次性返回全部问题。
    """
    if compute_digest(archive) != digest.strip().lower():
        return [
            ArchiveError(
                "digest_mismatch",
                "归档内容与摘要不符：归档在导出后被篡改，或摘要不是本次导出所得"
                "（应为规范化 JSON 的 SHA-256 小写十六进制摘要）",
                "archive",
            )
        ]

    errors: List[ArchiveError] = []

    doc = archive["document"]
    document_id = doc["id"]
    versions = archive["versions"]
    annotations = archive["annotations"]
    locations = archive["locations"]
    confirmations = archive["confirmations"]

    if archive.get("format") != ARCHIVE_FORMAT:
        errors.append(
            ArchiveError(
                "bad_format",
                f"归档格式标识应为 {ARCHIVE_FORMAT!r}，实际为 {archive.get('format')!r}",
                "format",
            )
        )

    # ------------------------------------------------------------------
    # 1. 版本编号连续：按升序必须恰好是 1..N
    # ------------------------------------------------------------------
    if not versions:
        errors.append(
            ArchiveError(
                "no_versions", "归档不包含任何版本（文档至少应有第 1 版）", "versions"
            )
        )

    version_by_id: Dict[int, Dict[str, Any]] = {}
    for i, version in enumerate(versions):
        vid = version["id"]
        if vid in version_by_id:
            errors.append(
                ArchiveError(
                    "duplicate_version_id",
                    f"版本行 ID {vid} 重复出现",
                    f"versions[{i}]",
                )
            )
        version_by_id[vid] = version
        if version["document_id"] != document_id:
            errors.append(
                ArchiveError(
                    "cross_document",
                    f"第 {version['number']} 版（行 ID {vid}）属于文档 "
                    f"{version['document_id']}，与归档文档 {document_id} 不一致"
                    "（串文档）",
                    f"versions[{i}]",
                )
            )

    version_sequence_ok = bool(versions)
    ordered = sorted(
        [(i, v) for i, v in enumerate(versions)],
        key=lambda pair: (pair[1]["number"], pair[0]),
    )
    for rank, (idx, version) in enumerate(ordered, start=1):
        if version["number"] != rank:
            version_sequence_ok = False
            errors.append(
                ArchiveError(
                    "version_gap",
                    f"版本编号不连续：按升序第 {rank} 个版本的编号为 "
                    f"{version['number']}（应为 {rank}）",
                    f"versions[{idx}]",
                )
            )
            break
    # 编号重复也使连续性失去意义
    numbers = [v["number"] for _, v in ordered]
    for rank, number in enumerate(numbers):
        if rank > 0 and number == numbers[rank - 1]:
            version_sequence_ok = False
            idx, _ = ordered[rank]
            errors.append(
                ArchiveError(
                    "duplicate_version_number",
                    f"版本编号 {number} 重复出现",
                    f"versions[{idx}]",
                )
            )

    # ------------------------------------------------------------------
    # 2. 批注归属与创建版引用
    # ------------------------------------------------------------------
    annotation_by_id: Dict[int, Dict[str, Any]] = {}
    for i, ann in enumerate(annotations):
        aid = ann["id"]
        if aid in annotation_by_id:
            errors.append(
                ArchiveError(
                    "duplicate_annotation_id",
                    f"批注行 ID {aid} 重复出现",
                    f"annotations[{i}]",
                )
            )
        annotation_by_id[aid] = ann
        if ann["document_id"] != document_id:
            errors.append(
                ArchiveError(
                    "cross_document",
                    f"批注 {aid} 属于文档 {ann['document_id']}，"
                    f"与归档文档 {document_id} 不一致（串文档）",
                    f"annotations[{i}]",
                )
            )
        if ann["created_version_id"] not in version_by_id:
            errors.append(
                ArchiveError(
                    "dangling_created_version",
                    f"批注 {aid} 的创建版本行 ID {ann['created_version_id']} "
                    "不在归档版本中（缺行或引用了其他文档的版本）",
                    f"annotations[{i}]",
                )
            )

    # ------------------------------------------------------------------
    # 3. 迁移记录归属
    # ------------------------------------------------------------------
    location_by_id: Dict[int, Dict[str, Any]] = {}
    locations_by_annotation: Dict[int, List[Dict[str, Any]]] = {}
    for i, loc in enumerate(locations):
        lid = loc["id"]
        if lid in location_by_id:
            errors.append(
                ArchiveError(
                    "duplicate_location_id",
                    f"迁移记录行 ID {lid} 重复出现",
                    f"locations[{i}]",
                )
            )
        location_by_id[lid] = loc
        locations_by_annotation.setdefault(loc["annotation_id"], []).append(loc)

        if loc["annotation_id"] not in annotation_by_id:
            errors.append(
                ArchiveError(
                    "dangling_annotation",
                    f"迁移记录 {lid} 引用的批注 {loc['annotation_id']} "
                    "不在归档批注中（缺行或串文档）",
                    f"locations[{i}]",
                )
            )
        if loc["version_id"] not in version_by_id:
            errors.append(
                ArchiveError(
                    "dangling_version",
                    f"迁移记录 {lid} 引用的版本行 ID {loc['version_id']} "
                    "不在归档版本中（缺行或串文档）",
                    f"locations[{i}]",
                )
            )

    # ------------------------------------------------------------------
    # 4. 每条批注从创建版到末版每版恰有一条迁移记录
    #    （版本编号不连续时跳过，避免把版本缺口放大成批量的缺行误报；
    #    引用悬空的记录逐条跳过，其问题已在前面各节报告）
    # ------------------------------------------------------------------
    if version_sequence_ok:
        last_number = max(v["number"] for v in versions)
        for i, ann in enumerate(annotations):
            aid = ann["id"]
            created = version_by_id.get(ann["created_version_id"])
            if created is None:
                continue  # 已报 dangling_created_version
            created_number = created["number"]
            seen_numbers: Dict[int, int] = {}
            for loc in locations_by_annotation.get(aid, []):
                version = version_by_id.get(loc["version_id"])
                if version is None:
                    continue  # 已报 dangling_version
                number = version["number"]
                if number in seen_numbers:
                    errors.append(
                        ArchiveError(
                            "duplicate_location",
                            f"批注 {aid} 在第 {number} 版有多条迁移记录"
                            f"（行 ID {seen_numbers[number]} 与 {loc['id']}），"
                            "每版应恰有一条",
                            _path_of_location(locations, loc["id"]),
                        )
                    )
                else:
                    seen_numbers[number] = loc["id"]
            for number in range(created_number, last_number + 1):
                if number not in seen_numbers:
                    errors.append(
                        ArchiveError(
                            "missing_location",
                            f"批注 {aid} 缺少第 {number} 版的迁移记录"
                            f"（该批注创建于第 {created_number} 版，末版为第 {last_number} 版）",
                            f"annotations[{i}]",
                        )
                    )
            for number in seen_numbers:
                if number < created_number:
                    errors.append(
                        ArchiveError(
                            "unexpected_location",
                            f"批注 {aid} 在第 {number} 版存在迁移记录，"
                            f"早于其创建版第 {created_number} 版",
                            _path_of_location(locations, seen_numbers[number]),
                        )
                    )

    # ------------------------------------------------------------------
    # 5. 顺延引用：同一批注、较早版本、真实歧义记录
    #    （非顺延且 status=ambiguous）
    # ------------------------------------------------------------------
    for i, loc in enumerate(locations):
        carried_from = loc["carried_from"]
        if carried_from is None:
            continue
        target = location_by_id.get(carried_from)
        if target is None:
            errors.append(
                ArchiveError(
                    "dangling_carried_from",
                    f"迁移记录 {loc['id']} 的顺延引用指向不存在的迁移记录 "
                    f"{carried_from}（缺行）",
                    f"locations[{i}]",
                )
            )
            continue
        if target["annotation_id"] != loc["annotation_id"]:
            errors.append(
                ArchiveError(
                    "bad_carried_forward",
                    f"迁移记录 {loc['id']} 的顺延引用指向批注 "
                    f"{target['annotation_id']} 的记录 {carried_from}，"
                    f"必须指向同一批注 {loc['annotation_id']} 的较早记录",
                    f"locations[{i}]",
                )
            )
            continue
        this_version = version_by_id.get(loc["version_id"])
        target_version = version_by_id.get(target["version_id"])
        if this_version is not None and target_version is not None:
            if target_version["number"] >= this_version["number"]:
                errors.append(
                    ArchiveError(
                        "bad_carried_forward",
                        f"迁移记录 {loc['id']}（第 {this_version['number']} 版）的"
                        f"顺延引用指向第 {target_version['number']} 版的记录 "
                        f"{carried_from}，必须指向较早版本的真实歧义记录",
                        f"locations[{i}]",
                    )
                )
        if target["status"] != "ambiguous" or target["carried_from"] is not None:
            reasons = []
            if target["status"] != "ambiguous":
                reasons.append(f"其状态为 {target['status']}（应为 ambiguous）")
            if target["carried_from"] is not None:
                reasons.append("其本身也是顺延占位记录")
            errors.append(
                ArchiveError(
                    "bad_carried_forward",
                    f"迁移记录 {loc['id']} 的顺延引用目标 {carried_from} 不是"
                    f"真实歧义记录：{'，'.join(reasons)}",
                    f"locations[{i}]",
                )
            )

    # ------------------------------------------------------------------
    # 6. 人工确认：指向非顺延歧义记录，且所选范围属于候选落点
    # ------------------------------------------------------------------
    for i, conf in enumerate(confirmations):
        loc = location_by_id.get(conf["location_id"])
        if loc is None:
            errors.append(
                ArchiveError(
                    "dangling_confirmation",
                    f"人工确认 {conf['id']} 指向不存在的迁移记录 "
                    f"{conf['location_id']}（缺行）",
                    f"confirmations[{i}]",
                )
            )
            continue
        if loc["status"] != "ambiguous" or loc["carried_from"] is not None:
            reasons = []
            if loc["status"] != "ambiguous":
                reasons.append(f"该记录状态为 {loc['status']}（应为 ambiguous）")
            if loc["carried_from"] is not None:
                reasons.append("该记录是顺延占位记录，请确认原始歧义记录")
            errors.append(
                ArchiveError(
                    "bad_confirmation",
                    f"人工确认 {conf['id']} 指向的迁移记录 {loc['id']} "
                    f"不是非顺延的歧义记录：{'；'.join(reasons)}",
                    f"confirmations[{i}]",
                )
            )
            continue
        if not any(
            candidate["start"] == conf["start"] and candidate["end"] == conf["end"]
            for candidate in loc["candidates"]
        ):
            candidate_ranges = [
                [candidate["start"], candidate["end"]]
                for candidate in loc["candidates"]
            ]
            errors.append(
                ArchiveError(
                    "confirmation_not_candidate",
                    f"人工确认 {conf['id']} 所选范围 [{conf['start']}, {conf['end']}) "
                    f"不在迁移记录 {loc['id']} 的候选落点中，候选范围为 "
                    f"{json.dumps(candidate_ranges, ensure_ascii=False)}",
                    f"confirmations[{i}]",
                )
            )

    return errors


def _path_of_location(
    locations: List[Dict[str, Any]], location_id: Optional[int]
) -> str:
    """按行 ID 在归档 locations 数组中的下标生成 JSON 路径式定位。"""
    if location_id is None:
        return "locations"
    for i, loc in enumerate(locations):
        if loc["id"] == location_id:
            return f"locations[{i}]"
    return "locations"
