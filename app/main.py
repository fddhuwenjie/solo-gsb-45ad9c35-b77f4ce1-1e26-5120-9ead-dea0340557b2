"""文档修订批注续接 API。

- 保存基准文本（文档第 1 版）、批注正文与锚点
  （选区 + 左右上下文 + 按 Unicode 码点计的原始偏移）；
- 提交新版全文时，为每条批注枚举选区的全部精确落点，
  按「左上下文后缀匹配长度 + 右上下文前缀匹配长度」之和排序：
  唯一最高分 → resolved（自动续接）；
  并列最高分 → ambiguous（返回全部候选范围，需人工确认）；
  找不到选区 → unresolved；
- 歧义批注在人工确认前不参与后续版本的自动迁移（顺延为占位记录）；
- 版本与迁移结果只追加，可查询任意两版间每条批注的范围、状态与匹配依据；
- 改动影响分析：指定起止版本，基于两版全文生成稳定的码点级编辑片段，
  把起始版每条批注的范围投影到目标版并分类
  （unchanged / shifted / modified / deleted / indeterminate）；
- 审阅依赖编排：批量提交同一文档内「前置批注 → 后续批注」关系
  （整批校验跨文档引用、自依赖、重复边与环，任一失败则全部不落库）；
  审阅计划按依赖图与完成记录把批注划分为可处理 / 被阻塞 / 已完成，
  并按拓扑层级给出可并行批次；完成记录只追加、重复提交幂等，
  完成前置批注后立即解锁符合条件的后续批注；
- 迁移归档：同一读事务导出文档元数据、全部版本、批注、迁移记录与人工
  确认的规范 JSON 及 SHA-256 摘要；核验接口只依据归档与摘要离线复算
  摘要并检查版本连续性、记录归属、每版迁移记录、顺延引用与人工确认的
  一致性，缺行、串文档、篡改或摘要不符时返回可定位错误，不写入数据；
- 文档发布门禁：按名称只追加地保存嵌套策略修订（all / any / not 与
  location_count / review_count 叶子，深度与节点数受限）；评估接口在
  同一个 SQLite 读事务内锁定策略修订、目标文档版本与当前审阅状态，
  返回不短路的同形解释树（实际值、阈值与参与计数的批注 ID）；
- 批注驱动的脱敏副本：指定文档版本、批注 ID 集合与占位文本，在同一个
  SQLite 读事务内读取版本全文与各批注的生效位置（只接受 resolved /
  confirmed），把重叠或首尾相接的范围合并为最大区间并按起点顺序替换，
  返回脱敏全文与源 / 结果码点半开区间映射（未遮盖片段同样可追溯）；
  跨文档、缺少位置、ambiguous / unresolved 等情况整批返回可定位错误，
  不猜测、不输出半成品，接口只读、不写入数据，相同快照与请求结果字节一致。
- 文档增量事件流：版本、批注、迁移位置、人工确认、依赖与完成记录按
  (created_at, type_rank, id) 稳定合并；首轮在同一个 SQLite 读事务中固定
  六张表最大行 ID 作为水位向量，续页游标封装文档 ID、起止水位与最后排序
  键，只读取 (起点, 终点] 且始终沿用终点水位（并发新增留到下一轮），
  末页返回 resume_cursor 续拉增量，空轮也推进水位；游标版本、结构、范围
  错误或跨文档使用返回 422；事件载荷只保留行落库时的稳定业务字段。
- 文档批注覆盖图：指定文档版本，在同一个 SQLite 读事务中读取该版全文与
  全部迁移位置，把 [0, 文本码点长度) 划分为连续、无重叠的最大片段；
  resolved / confirmed 计入 definite，未确认且非顺延的 ambiguous 先按批注
  合并重叠候选再计入 possible（同一 ID 不重复），unresolved、顺延占位与
  该版尚不存在的批注列入 exclusions；片段状态 covered / uncertain /
  uncovered 无缝铺满全文，接口只读、相同快照字节一致。
- 批量偏移换算：为浏览器与外部工具把 codepoint / utf16 / utf8_byte
  三种单位的半开区间批量互转（每请求至多 200 个）；先按目标版本全文
  一次构建边界映射，整批 O(n + m)；越界、start > end、端点落入 UTF-16
  代理对或 UTF-8 多字节字符内部时整批 422 并指出条目与端点，绝不取整；
  接口只读，相同快照结果稳定。
- 可离线往返的审阅包：创建时在同一个 SQLite 读事务中冻结指定批注的
  最新迁移记录、候选范围、完成状态与全部传递前置状态（自动收录未完成
  传递前置项），生成 package_id 与内容摘要，只收录当时可确认、可完成
  或能由同包前置项解锁的条目；回传携带原摘要与逐项 confirm / complete
  决定，目标已被确认、完成状态变化或新增版本改变最新位置时整包 409
  且不写入，无冲突时在同一个写事务中按依赖顺序追加确认与完成
  （后项可由前项解锁，任一决定非法全部回滚），同一 package_id 成功
  回传后原样返回首次结果、不重复写入。
- 文档审阅发布候选：调用方提交文档版本与门禁策略修订，服务在同一个
  SQLite 读事务中固定该版本、策略修订、门禁解释树、当前审阅状态与该
  文档六类事件（版本 / 批注 / 迁移 / 确认 / 依赖 / 完成）的水位；
  初始门禁未通过即拒绝创建且不落候选，通过后生成不可变 candidate_id
  与规范摘要。确认时在同一个写事务中重读水位与门禁快照：候选创建后
  该文档出现任一六类事件、或门禁不再通过，即返回可定位冲突且不改变
  候选状态；仅水位未变且门禁仍通过时追加一次 released 结果，重复确认
  同一 candidate_id 原样返回首次结果、不重复写入。
- 版本范围字面子串检索：审阅人在同一文档的版本号区间内按 Unicode 码点
  做字面子串匹配（保留重叠命中），每个命中返回版本号、半开范围、可复现
  的前后文片段以及与该范围相交的批注 ID 与生效状态（resolved / confirmed /
  ambiguous；确认范围优先于迁移候选，ambiguous 返回相交候选）；每页至多
  100 个命中并按版本号、起点、终点稳定排序；游标在同一个 SQLite 读事务中
  冻结版本上界与批注 / 位置 / 确认快照水位，分页期间并发新增留到下一轮，
  篡改、跨文档或筛选条件不一致的游标返回 422。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from . import archive as archive_mod
from . import coordinates as coords_mod
from . import db
from . import events as events_mod
from . import gate as gate_mod
from . import release_candidates as rc_mod
from . import review_packages as rp_mod
from . import search as search_mod
from .diffing import (
    classify_range,
    compute_segments,
    hit_segment_indices,
    project_range,
)
from .coverage import (
    AmbiguousRange,
    DefiniteRange,
    Exclusion,
    REASON_AMBIGUOUS_CARRIED,
    REASON_LOCATION_MISSING,
    REASON_UNRESOLVED,
    build_coverage,
)
from .matching import locate
from .redacting import EffectiveRange, build_redaction, merge_ranges
from .review import (
    find_cycle_path,
    topological_waves,
    transitive_prerequisites,
)
from .schemas import (
    AnnotationCreate,
    ArchiveVerifyRequest,
    CompletionCreate,
    ConfirmationCreate,
    CoordinateBatchCreate,
    DependencyBatchCreate,
    DocumentCreate,
    PolicyCreate,
    RedactionCreate,
    ReleaseCandidateCreate,
    ReviewPackageCreate,
    ReviewPackageSubmit,
    SearchRequest,
    VersionCreate,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 每次启动尝试都先以"未就绪"开始：迁移或校验失败时，
    # 绝不能残留上一次成功启动的 db_ready=True（探针误判放行业务流量）。
    app.state.db_ready = False
    app.state.schema_version = None
    # 版本化迁移：预检（非破坏性）→ 单个写事务内按编号执行未完成迁移 →
    # 结构校验。任一步失败或校验不通过都会抛 MigrationError，启动中止，
    # 服务不会进入可接受业务请求的状态（无任何路由得到服务）。
    result = db.init_db()
    app.state.db_ready = True
    app.state.schema_version = result.version
    yield


app = FastAPI(
    title="文档修订批注续接 API",
    version="1.11.0",
    description=(
        "基于选区 + 左右上下文锚点的批注跨版本续接，"
        "以及批注间的审阅依赖编排（依赖图校验、审阅计划、完成记录）。"
        "所有偏移均按 Unicode 码点计；版本、迁移结果、依赖关系、完成记录"
        "与门禁策略修订只追加。"
        "另提供可离线核验的迁移归档：同一读事务导出规范 JSON 与 SHA-256 摘要，"
        "核验只依据归档与摘要本身，不查数据库、不写入数据；"
        "以及文档发布门禁：策略修订按名称只追加，评估在同一读事务内锁定"
        "策略修订、目标版本与当前审阅状态，返回同形解释树；"
        "以及批注驱动的脱敏副本：同一读事务读取版本与生效位置，"
        "重叠或首尾相接的范围合并替换，返回脱敏全文与码点区间映射；"
        "以及文档增量事件流：六类只追加记录稳定合并，以水位向量 + "
        "续页 / 续拉游标分页，窗口内不漏、不重、不乱序；"
        "以及文档批注覆盖图：同一读事务读取版本全文与全部迁移位置，"
        "把全文划分为 covered / uncertain / uncovered 最大片段并无缝铺满；"
        "以及批量偏移换算：同一读事务按目标版本全文一次构建 "
        "codepoint / utf16 / utf8_byte 边界映射，整批 O(n+m) 互转半开区间，"
        "代理对或多字节字符内部、越界、start>end 整批 422，绝不取整；"
        "以及可离线往返的审阅包：同一读事务冻结最新迁移记录、候选范围、"
        "完成状态与全部传递前置状态并生成 package_id 与内容摘要，"
        "回传时冲突整包 409 不写入，无冲突在同一写事务按依赖顺序追加"
        "确认与完成，重复回传原样返回首次结果；"
        "以及文档审阅发布候选：同一读事务固定版本、策略修订、门禁解释树、"
        "审阅状态与六类事件水位并生成不可变 candidate_id 与摘要，"
        "确认时同写事务重读水位与门禁，有任一六类事件或门禁不再通过即"
        "可定位冲突且不改变候选，水位未变且门禁通过才追加一次 released，"
        "重复确认原样返回首次结果；"
        "以及版本范围字面子串检索：按 Unicode 码点字面匹配并保留重叠命中，"
        "逐命中返回半开范围、可复现前后文与相交批注的生效状态，"
        "每页至多 100 个、按版本号/起点/终点稳定排序，"
        "签名游标冻结版本上界与批注/位置快照，并发新增留到下一轮，"
        "篡改、跨文档或筛选不一致的游标返回 422。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(sqlite3.IntegrityError)
async def integrity_error_handler(request, exc):
    return JSONResponse(status_code=409, content={"detail": f"数据冲突：{exc}"})


def get_db():
    con = db.connect()
    try:
        yield con
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 查询辅助
# ---------------------------------------------------------------------------


def _get_document(con: sqlite3.Connection, document_id: int) -> sqlite3.Row:
    doc = con.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if doc is None:
        raise HTTPException(404, f"文档 {document_id} 不存在")
    return doc


def _get_annotation(
    con: sqlite3.Connection, document_id: int, annotation_id: int
) -> sqlite3.Row:
    ann = con.execute(
        "SELECT * FROM annotations WHERE id = ? AND document_id = ?",
        (annotation_id, document_id),
    ).fetchone()
    if ann is None:
        raise HTTPException(404, f"文档 {document_id} 中不存在批注 {annotation_id}")
    return ann


def _get_version(con: sqlite3.Connection, document_id: int, number: int) -> sqlite3.Row:
    v = con.execute(
        "SELECT * FROM versions WHERE document_id = ? AND number = ?",
        (document_id, number),
    ).fetchone()
    if v is None:
        raise HTTPException(404, f"文档 {document_id} 不存在第 {number} 版")
    return v


def _latest_version(con: sqlite3.Connection, document_id: int) -> sqlite3.Row:
    return con.execute(
        "SELECT * FROM versions WHERE document_id = ? ORDER BY number DESC LIMIT 1",
        (document_id,),
    ).fetchone()


def _confirmation_of(con: sqlite3.Connection, location_id: int) -> Optional[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM confirmations WHERE location_id = ? ORDER BY id DESC LIMIT 1",
        (location_id,),
    ).fetchone()


def _location_out(con: sqlite3.Connection, loc: sqlite3.Row) -> dict:
    """迁移记录的完整视图：范围、状态与匹配依据（含确认后的生效值）。"""
    version_number = con.execute(
        "SELECT number FROM versions WHERE id = ?", (loc["version_id"],)
    ).fetchone()["number"]
    conf = _confirmation_of(con, loc["id"])
    out = {
        "location_id": loc["id"],
        "annotation_id": loc["annotation_id"],
        "version_number": version_number,
        "status": loc["status"],
        "start": loc["start"],
        "end": loc["end"],
        "left_match": loc["left_match"],
        "right_match": loc["right_match"],
        "score": loc["score"],
        "candidates": json.loads(loc["candidates"]),
        "carried_forward": loc["carried_from"] is not None,
        "carried_from_location_id": loc["carried_from"],
        "confirmation": None,
        "created_at": loc["created_at"],
    }
    if conf is not None:
        # 确认后的生效状态与范围
        out["status"] = "confirmed"
        out["start"] = conf["start"]
        out["end"] = conf["end"]
        out["confirmation"] = {
            "start": conf["start"],
            "end": conf["end"],
            "note": conf["note"],
            "created_at": conf["created_at"],
        }
    return out


def _annotation_out(con: sqlite3.Connection, ann: sqlite3.Row) -> dict:
    latest = con.execute(
        """
        SELECT l.* FROM locations l
        JOIN versions v ON v.id = l.version_id
        WHERE l.annotation_id = ?
        ORDER BY v.number DESC LIMIT 1
        """,
        (ann["id"],),
    ).fetchone()
    created_number = con.execute(
        "SELECT number FROM versions WHERE id = ?", (ann["created_version_id"],)
    ).fetchone()["number"]
    return {
        "annotation_id": ann["id"],
        "document_id": ann["document_id"],
        "body": ann["body"],
        "anchor": {
            "selection": ann["selection"],
            "left_context": ann["left_context"],
            "right_context": ann["right_context"],
            "start": ann["orig_start"],
            "end": ann["orig_end"],
            "version_number": created_number,
        },
        "created_at": ann["created_at"],
        "latest": _location_out(con, latest) if latest else None,
    }


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------


@app.get("/")
def root():
    return {
        "service": "文档修订批注续接 API",
        "version": "1.11.0",
        "docs": "/docs",
        "offsets": "所有偏移均按 Unicode 码点计",
        "archive_format": archive_mod.ARCHIVE_FORMAT,
    }


@app.get("/health")
def health():
    # 迁移未完成 / 校验失败时 lifespan 已中止启动；此处为防御性兜底：
    # 任何异常启动路径下健康检查都不能报 ok，探针不应把实例放入流量。
    if not getattr(app.state, "db_ready", False):
        return JSONResponse(
            status_code=503, content={"status": "unavailable", "reason": "schema 迁移未完成"}
        )
    return {"status": "ok", "schema_version": getattr(app.state, "schema_version", None)}


# ---------------------------------------------------------------------------
# 文档与版本
# ---------------------------------------------------------------------------


@app.post("/documents", status_code=201)
def create_document(payload: DocumentCreate, con=Depends(get_db)):
    """新建文档；content 作为基准文本保存为第 1 版。"""
    with db.transaction(con):
        cur = con.execute("INSERT INTO documents (title) VALUES (?)", (payload.title,))
        document_id = cur.lastrowid
        cur = con.execute(
            "INSERT INTO versions (document_id, number, content) VALUES (?, 1, ?)",
            (document_id, payload.content),
        )
        version_id = cur.lastrowid
    return {
        "document_id": document_id,
        "title": payload.title,
        "version": {"version_id": version_id, "version_number": 1},
    }


@app.get("/documents/{document_id}")
def get_document(document_id: int, con=Depends(get_db)):
    doc = _get_document(con, document_id)
    versions = con.execute(
        """
        SELECT number, length(content) AS length, created_at
        FROM versions WHERE document_id = ? ORDER BY number
        """,
        (document_id,),
    ).fetchall()
    ann_count = con.execute(
        "SELECT COUNT(*) AS c FROM annotations WHERE document_id = ?", (document_id,)
    ).fetchone()["c"]
    return {
        "document_id": doc["id"],
        "title": doc["title"],
        "created_at": doc["created_at"],
        "annotation_count": ann_count,
        "versions": [dict(v) for v in versions],
    }


@app.get("/documents/{document_id}/versions")
def list_versions(document_id: int, con=Depends(get_db)):
    _get_document(con, document_id)
    rows = con.execute(
        """
        SELECT id, number, length(content) AS length, created_at
        FROM versions WHERE document_id = ? ORDER BY number
        """,
        (document_id,),
    ).fetchall()
    return {
        "document_id": document_id,
        "versions": [
            {
                "version_id": r["id"],
                "version_number": r["number"],
                "length": r["length"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


@app.get("/documents/{document_id}/versions/{number}")
def get_version(document_id: int, number: int, con=Depends(get_db)):
    """某版全文及该版上每条批注的迁移结果。"""
    _get_document(con, document_id)
    v = _get_version(con, document_id, number)
    locs = con.execute(
        "SELECT * FROM locations WHERE version_id = ? ORDER BY annotation_id",
        (v["id"],),
    ).fetchall()
    return {
        "document_id": document_id,
        "version_number": v["number"],
        "content": v["content"],
        "created_at": v["created_at"],
        "locations": [_location_out(con, loc) for loc in locs],
    }


@app.get("/documents/{document_id}/versions/{number}/coverage")
def version_coverage(document_id: int, number: int, con=Depends(get_db)):
    """文档批注覆盖图：把该版全文划分为连续、无重叠的最大片段（只读）。

    版本全文与该版全部批注的迁移位置在**同一个 SQLite 读事务**（同一快照）
    中读出，接口不写入任何数据。分类规则：

    - ``resolved`` / ``confirmed``（人工确认以确认范围为准）→ **definite**；
    - 未确认且**非顺延**的 ``ambiguous``：先按批注把候选范围中重叠 / 首尾
      相接的合并为最大区间，再作为 **possible** 登记（同一 ID 不重复）；
    - ``unresolved``、歧义顺延占位、该版尚不存在迁移记录（批注创建于更晚
      版本）的批注不覆盖任何位置，列入 ``exclusions`` 并说明原因。

    每个片段返回 ``[start, end)``、``text``、升序的 ``definite_ids`` /
    ``possible_ids`` 与 ``status``：有 definite 为 ``covered``，仅有 possible
    为 ``uncertain``，均空为 ``uncovered``。相邻片段仅在状态与两组 ID 完全
    相同时合并。片段从 0 无缝铺满到文本码点长度（空文本为单个空片段）。
    相同快照下结果序列化后字节一致。
    """
    with db.read_transaction(con):
        _get_document(con, document_id)
        version = _get_version(con, document_id, number)
        text = version["content"]

        anns = con.execute(
            "SELECT id FROM annotations WHERE document_id = ? ORDER BY id",
            (document_id,),
        ).fetchall()

        definite: list = []
        ambiguous: list = []
        exclusions: list = []
        for ann_row in anns:
            annotation_id = ann_row["id"]
            loc = con.execute(
                "SELECT * FROM locations WHERE annotation_id = ? AND version_id = ?",
                (annotation_id, version["id"]),
            ).fetchone()
            if loc is None:
                exclusions.append(
                    Exclusion(
                        annotation_id=annotation_id,
                        reason=REASON_LOCATION_MISSING,
                        message=(
                            f"批注 {annotation_id} 在第 {number} 版没有迁移记录"
                            "（该批注创建于更晚版本），不参与覆盖图"
                        ),
                    )
                )
                continue

            conf = _confirmation_of(con, loc["id"])
            if conf is not None:
                definite.append(
                    DefiniteRange(annotation_id, conf["start"], conf["end"])
                )
            elif loc["status"] == "resolved":
                definite.append(
                    DefiniteRange(annotation_id, loc["start"], loc["end"])
                )
            elif loc["status"] == "ambiguous":
                if loc["carried_from"] is not None:
                    exclusions.append(
                        Exclusion(
                            annotation_id=annotation_id,
                            reason=REASON_AMBIGUOUS_CARRIED,
                            message=(
                                f"批注 {annotation_id} 在第 {number} 版为歧义顺延"
                                "占位记录（原始歧义未经人工确认），不提供范围"
                            ),
                        )
                    )
                else:
                    candidates = tuple(
                        (c["start"], c["end"])
                        for c in json.loads(loc["candidates"])
                    )
                    ambiguous.append(
                        AmbiguousRange(
                            annotation_id=annotation_id, candidates=candidates
                        )
                    )
            else:  # unresolved
                exclusions.append(
                    Exclusion(
                        annotation_id=annotation_id,
                        reason=REASON_UNRESOLVED,
                        message=(
                            f"批注 {annotation_id} 在第 {number} 版未解析"
                            "（选区在该版没有精确落点），不提供范围"
                        ),
                    )
                )

        coverage = build_coverage(text, definite, ambiguous)
        segments = coverage["segments"]

    return {
        "document_id": document_id,
        "version_number": number,
        "source_length": len(text),
        "summary": {
            "segment_count": len(segments),
            "covered": sum(1 for s in segments if s["status"] == "covered"),
            "uncertain": sum(1 for s in segments if s["status"] == "uncertain"),
            "uncovered": sum(1 for s in segments if s["status"] == "uncovered"),
            "excluded": len(exclusions),
        },
        "segments": segments,
        "exclusions": [
            {
                "annotation_id": e.annotation_id,
                "reason": e.reason,
                "message": e.message,
            }
            for e in exclusions
        ],
    }


@app.post(
    "/documents/{document_id}/versions/{number}/coordinates", status_code=200
)
def convert_coordinates(
    document_id: int,
    number: int,
    payload: CoordinateBatchCreate,
    con=Depends(get_db),
):
    """批量偏移换算：codepoint / utf16 / utf8_byte 半开区间互转（只读）。

    为浏览器（JavaScript 按 UTF-16 码元计偏移）与外部工具（按 UTF-8
    字节计偏移）换算选区端点，避免 emoji 等字符让选区错位。版本全文在
    **同一个 SQLite 读事务**（同一快照）中读出后，先对全文做一次线性
    扫描构建三种单位的边界映射（:func:`coords_mod.build_coordinate_map`），
    随后每个端点 O(1) 查表，整批复杂度 **O(n + m)**（n 为全文码点数，
    m 为区间数，至多 200）。

    请求声明 ``input_unit``（``codepoint`` / ``utf16`` / ``utf8_byte``）
    并提交 1～200 个半开区间 ``[start, end)``；响应 ``results`` 逐项
    （``index`` 为原请求下标）给出三种单位各自的对应范围。

    以下情况**整批返回 422**（``detail.errors`` 一次性列出全部问题，
    每条带 ``index`` 与 ``endpoint`` 条目端点定位，不做任何取整猜测）：

    - 端点越界（``endpoint_out_of_range``，合法端点为 0..该单位总长）；
    - ``start > end``（``start_after_end``，允许空区间 ``[x, x)``）；
    - UTF-16 端点落在代理对两个码元之间
      （``inside_surrogate_pair``）；
    - UTF-8 端点落在多字节字符的字节中间
      （``inside_multibyte_sequence``）。

    组合字符是多个独立码点，码点边界即合法边界。接口不写入任何数据，
    同一版本快照（版本内容只追加、不可变）下相同请求结果字节一致。
    文档或版本不存在返回 404。
    """
    with db.read_transaction(con):
        _get_document(con, document_id)
        version = _get_version(con, document_id, number)
        text = version["content"]
        cdata = coords_mod.build_coordinate_map(text)
        outcome = coords_mod.convert_batch(
            cdata,
            payload.input_unit,
            [(iv.start, iv.end) for iv in payload.intervals],
        )

    if outcome["errors"]:
        raise HTTPException(
            422,
            detail={
                "message": "偏移换算请求校验失败，整批未换算（不对任何端点取整）",
                "input_unit": payload.input_unit,
                "lengths": {
                    coords_mod.UNIT_CODEPOINT: cdata.codepoint_length,
                    coords_mod.UNIT_UTF16: cdata.utf16_length,
                    coords_mod.UNIT_UTF8_BYTE: cdata.utf8_length,
                },
                "errors": outcome["errors"],
            },
        )

    return {
        "document_id": document_id,
        "version_number": number,
        "input_unit": payload.input_unit,
        "lengths": {
            coords_mod.UNIT_CODEPOINT: cdata.codepoint_length,
            coords_mod.UNIT_UTF16: cdata.utf16_length,
            coords_mod.UNIT_UTF8_BYTE: cdata.utf8_length,
        },
        "results": outcome["results"],
    }


@app.post("/documents/{document_id}/versions", status_code=201)
def create_version(document_id: int, payload: VersionCreate, con=Depends(get_db)):
    """提交新版全文，并为每条批注执行一次续接迁移（结果只追加）。

    规则：
    - 上一条「真实」迁移结果为 ambiguous 且未确认 → 不参与本次自动迁移，
      追加一条顺延占位记录（carried_forward=true）；
    - 其余（resolved / 已确认 / unresolved）均重新匹配：
      选区重新出现时 unresolved 批注也会自动恢复。
    """
    _get_document(con, document_id)
    results = []
    with db.transaction(con):
        number = con.execute(
            "SELECT COALESCE(MAX(number), 0) + 1 AS n FROM versions WHERE document_id = ?",
            (document_id,),
        ).fetchone()["n"]
        cur = con.execute(
            "INSERT INTO versions (document_id, number, content) VALUES (?, ?, ?)",
            (document_id, number, payload.content),
        )
        version_id = cur.lastrowid

        annotations = con.execute(
            "SELECT * FROM annotations WHERE document_id = ? ORDER BY id",
            (document_id,),
        ).fetchall()

        for ann in annotations:
            # 最近一次真实（非顺延）迁移结果
            prev = con.execute(
                """
                SELECT l.* FROM locations l
                JOIN versions v ON v.id = l.version_id
                WHERE l.annotation_id = ? AND l.carried_from IS NULL
                ORDER BY v.number DESC LIMIT 1
                """,
                (ann["id"],),
            ).fetchone()

            if (
                prev is not None
                and prev["status"] == "ambiguous"
                and _confirmation_of(con, prev["id"]) is None
            ):
                # 歧义未确认：不参与自动迁移，顺延占位
                con.execute(
                    """
                    INSERT INTO locations (annotation_id, version_id, status, carried_from)
                    VALUES (?, ?, 'ambiguous', ?)
                    """,
                    (ann["id"], version_id, prev["id"]),
                )
                results.append(
                    {
                        "annotation_id": ann["id"],
                        "status": "ambiguous",
                        "carried_forward": True,
                        "detail": "存在未确认的歧义，本次不参与自动迁移",
                    }
                )
                continue

            status, best, candidates = locate(
                payload.content,
                ann["selection"],
                ann["left_context"],
                ann["right_context"],
            )
            candidates_json = json.dumps(
                [c.to_dict() for c in candidates], ensure_ascii=False
            )
            if status == "resolved":
                con.execute(
                    """
                    INSERT INTO locations
                        (annotation_id, version_id, status, start, "end",
                         left_match, right_match, score, candidates)
                    VALUES (?, ?, 'resolved', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ann["id"],
                        version_id,
                        best.start,
                        best.end,
                        best.left_match,
                        best.right_match,
                        best.score,
                        candidates_json,
                    ),
                )
                results.append(
                    {
                        "annotation_id": ann["id"],
                        "status": "resolved",
                        "carried_forward": False,
                        "start": best.start,
                        "end": best.end,
                        "left_match": best.left_match,
                        "right_match": best.right_match,
                        "score": best.score,
                        "candidates": [c.to_dict() for c in candidates],
                    }
                )
            elif status == "ambiguous":
                top_score = candidates[0].score
                con.execute(
                    """
                    INSERT INTO locations
                        (annotation_id, version_id, status, score, candidates)
                    VALUES (?, ?, 'ambiguous', ?, ?)
                    """,
                    (ann["id"], version_id, top_score, candidates_json),
                )
                results.append(
                    {
                        "annotation_id": ann["id"],
                        "status": "ambiguous",
                        "carried_forward": False,
                        "score": top_score,
                        "candidates": [c.to_dict() for c in candidates],
                        "detail": "多个候选并列最高分，需人工确认",
                    }
                )
            else:  # unresolved
                con.execute(
                    """
                    INSERT INTO locations (annotation_id, version_id, status, candidates)
                    VALUES (?, ?, 'unresolved', '[]')
                    """,
                    (ann["id"], version_id),
                )
                results.append(
                    {
                        "annotation_id": ann["id"],
                        "status": "unresolved",
                        "carried_forward": False,
                        "detail": "新版本中找不到选区的精确落点",
                    }
                )

    summary = {}
    for r in results:
        key = "carried" if r.get("carried_forward") else r["status"]
        summary[key] = summary.get(key, 0) + 1
    return {
        "document_id": document_id,
        "version": {"version_id": version_id, "version_number": number},
        "summary": summary,
        "results": results,
    }


# ---------------------------------------------------------------------------
# 批注
# ---------------------------------------------------------------------------


@app.post("/documents/{document_id}/annotations", status_code=201)
def create_annotation(document_id: int, payload: AnnotationCreate, con=Depends(get_db)):
    """在当前最新版上创建批注；锚点会与基准文本严格校验。

    最新版读取、锚点校验与批注 / 初始迁移记录的写入在同一个
    BEGIN IMMEDIATE 写事务中完成：以取得写锁后的真实最新版为准，
    并发提交的新版不会夹在校验与写入之间（否则批注会挂到过期版本，
    而新版本缺少该批注的迁移记录，破坏逐版迁移记录的完整性）。
    校验失败则整体回滚，不留下批注或位置。
    """
    _get_document(con, document_id)
    with db.transaction(con):
        version = _latest_version(con, document_id)
        text = version["content"]

        if payload.end <= payload.start:
            raise HTTPException(422, "end 必须大于 start")
        if payload.end > len(text):
            raise HTTPException(
                422, f"end 超出文本长度（当前版本共 {len(text)} 个码点）"
            )
        actual = text[payload.start : payload.end]
        if actual != payload.selection:
            raise HTTPException(
                422,
                f"选区与偏移不符：text[{payload.start}:{payload.end}]={actual!r}，"
                f"而 selection={payload.selection!r}（偏移按 Unicode 码点计）",
            )
        if payload.left_context:
            left_start = max(0, payload.start - len(payload.left_context))
            if text[left_start : payload.start] != payload.left_context:
                raise HTTPException(422, "left_context 与选区左侧实际文本不符")
        if payload.right_context:
            if text[payload.end : payload.end + len(payload.right_context)] != (
                payload.right_context
            ):
                raise HTTPException(422, "right_context 与选区右侧实际文本不符")

        left_match = len(payload.left_context)
        right_match = len(payload.right_context)
        initial_candidate = {
            "start": payload.start,
            "end": payload.end,
            "left_match": left_match,
            "right_match": right_match,
            "score": left_match + right_match,
        }
        cur = con.execute(
            """
            INSERT INTO annotations
                (document_id, body, selection, left_context, right_context,
                 orig_start, orig_end, created_version_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                payload.body,
                payload.selection,
                payload.left_context,
                payload.right_context,
                payload.start,
                payload.end,
                version["id"],
            ),
        )
        annotation_id = cur.lastrowid
        cur = con.execute(
            """
            INSERT INTO locations
                (annotation_id, version_id, status, start, "end",
                 left_match, right_match, score, candidates)
            VALUES (?, ?, 'resolved', ?, ?, ?, ?, ?, ?)
            """,
            (
                annotation_id,
                version["id"],
                payload.start,
                payload.end,
                left_match,
                right_match,
                left_match + right_match,
                json.dumps([initial_candidate], ensure_ascii=False),
            ),
        )
        location_id = cur.lastrowid

    loc = con.execute("SELECT * FROM locations WHERE id = ?", (location_id,)).fetchone()
    ann = con.execute(
        "SELECT * FROM annotations WHERE id = ?", (annotation_id,)
    ).fetchone()
    return {
        "annotation_id": annotation_id,
        "anchor": _annotation_out(con, ann)["anchor"],
        "location": _location_out(con, loc),
    }


@app.get("/documents/{document_id}/annotations")
def list_annotations(document_id: int, con=Depends(get_db)):
    """文档下全部批注及其在最新版上的生效状态。"""
    _get_document(con, document_id)
    anns = con.execute(
        "SELECT * FROM annotations WHERE document_id = ? ORDER BY id",
        (document_id,),
    ).fetchall()
    return {
        "document_id": document_id,
        "annotations": [_annotation_out(con, ann) for ann in anns],
    }


@app.get("/documents/{document_id}/annotations/{annotation_id}")
def get_annotation(document_id: int, annotation_id: int, con=Depends(get_db)):
    _get_document(con, document_id)
    ann = _get_annotation(con, document_id, annotation_id)
    return _annotation_out(con, ann)


@app.get("/documents/{document_id}/annotations/{annotation_id}/history")
def annotation_history(document_id: int, annotation_id: int, con=Depends(get_db)):
    """一条批注跨全部版本的范围、状态与匹配依据（按版本升序）。"""
    _get_document(con, document_id)
    ann = _get_annotation(con, document_id, annotation_id)
    locs = con.execute(
        """
        SELECT l.* FROM locations l
        JOIN versions v ON v.id = l.version_id
        WHERE l.annotation_id = ?
        ORDER BY v.number
        """,
        (annotation_id,),
    ).fetchall()
    return {
        "document_id": document_id,
        "annotation_id": annotation_id,
        "body": ann["body"],
        "anchor": _annotation_out(con, ann)["anchor"],
        "history": [_location_out(con, loc) for loc in locs],
    }


@app.post(
    "/documents/{document_id}/annotations/{annotation_id}/confirmations",
    status_code=201,
)
def confirm_annotation(
    document_id: int,
    annotation_id: int,
    payload: ConfirmationCreate,
    con=Depends(get_db),
):
    """人工确认歧义批注：从候选范围中选定一个（只追加，确认后恢复自动迁移）。

    目标记录读取、状态 / 顺延 / 既有确认检查、候选校验与确认写入在同一个
    BEGIN IMMEDIATE 写事务中完成：并发确认同一歧义记录时，后到者在取得
    写锁后重新校验，稳定返回 409；confirmations.location_id 上的唯一索引
    （迁移 2 建立，见 app.migrations）在 SQLite 层兜底，保证每个 location
    至多一条确认——
    不会覆盖首条确认，也不会留下重复行。
    """
    with db.transaction(con):
        _get_document(con, document_id)
        _get_annotation(con, document_id, annotation_id)
        version = _get_version(con, document_id, payload.version_number)
        loc = con.execute(
            "SELECT * FROM locations WHERE annotation_id = ? AND version_id = ?",
            (annotation_id, version["id"]),
        ).fetchone()
        if loc is None:
            raise HTTPException(404, "该版本没有此批注的迁移记录")
        if loc["status"] != "ambiguous":
            raise HTTPException(409, f"该记录状态为 {loc['status']}，无需确认")
        if loc["carried_from"] is not None:
            raise HTTPException(
                409,
                "该记录是歧义顺延的占位记录，请确认原始歧义记录"
                f"（carried_from_location_id={loc['carried_from']}）",
            )
        if _confirmation_of(con, loc["id"]) is not None:
            raise HTTPException(409, "该歧义记录已确认过，确认结果只追加不可更改")

        candidates = json.loads(loc["candidates"])
        if not any(
            c["start"] == payload.start and c["end"] == payload.end for c in candidates
        ):
            raise HTTPException(
                422,
                "所选范围不在候选列表中，候选范围为："
                + json.dumps(
                    [[c["start"], c["end"]] for c in candidates], ensure_ascii=False
                ),
            )

        cur = con.execute(
            'INSERT INTO confirmations (location_id, start, "end", note) VALUES (?, ?, ?, ?)',
            (loc["id"], payload.start, payload.end, payload.note),
        )
        confirmation_id = cur.lastrowid
    return {
        "confirmation_id": confirmation_id,
        "location": _location_out(con, loc),
    }


# ---------------------------------------------------------------------------
# 任意两版对比
# ---------------------------------------------------------------------------


@app.get("/documents/{document_id}/compare")
def compare_versions(
    document_id: int,
    from_version: int = Query(ge=1),
    to_version: int = Query(ge=1),
    con=Depends(get_db),
):
    """任意两版之间，每条批注的范围、状态与匹配依据。"""
    _get_document(con, document_id)
    v_from = _get_version(con, document_id, from_version)
    v_to = _get_version(con, document_id, to_version)
    anns = con.execute(
        "SELECT * FROM annotations WHERE document_id = ? ORDER BY id",
        (document_id,),
    ).fetchall()
    items = []
    for ann in anns:
        from_loc = con.execute(
            "SELECT * FROM locations WHERE annotation_id = ? AND version_id = ?",
            (ann["id"], v_from["id"]),
        ).fetchone()
        to_loc = con.execute(
            "SELECT * FROM locations WHERE annotation_id = ? AND version_id = ?",
            (ann["id"], v_to["id"]),
        ).fetchone()
        items.append(
            {
                "annotation_id": ann["id"],
                "body": ann["body"],
                "selection": ann["selection"],
                "from": _location_out(con, from_loc) if from_loc else None,
                "to": _location_out(con, to_loc) if to_loc else None,
            }
        )
    return {
        "document_id": document_id,
        "from_version": from_version,
        "to_version": to_version,
        "annotations": items,
    }


# ---------------------------------------------------------------------------
# 批注改动影响分析
# ---------------------------------------------------------------------------

_CLASSIFICATIONS = ("unchanged", "shifted", "modified", "deleted", "indeterminate")


@app.get("/documents/{document_id}/impact")
def impact_analysis(
    document_id: int,
    from_version: int = Query(ge=1),
    to_version: int = Query(ge=1),
    con=Depends(get_db),
):
    """批注改动影响分析：把起始版每条批注的范围投影到目标版并分类。

    - 基于两版全文生成稳定的码点级编辑片段（insert / delete / replace）；
    - 每条批注分类为 unchanged / shifted / modified / deleted / indeterminate，
      并返回原范围、投影范围与命中的编辑片段；
    - 起始版位置歧义、未解析或缺失时不猜测范围，返回 indeterminate 及原因；
    - 相同输入的片段顺序与分类必然相同（确定性计算，结果不落库）。
    """
    _get_document(con, document_id)
    v_from = _get_version(con, document_id, from_version)
    v_to = _get_version(con, document_id, to_version)

    segments = compute_segments(v_from["content"], v_to["content"])
    segment_dicts = [seg.to_dict(i) for i, seg in enumerate(segments)]

    anns = con.execute(
        "SELECT * FROM annotations WHERE document_id = ? ORDER BY id",
        (document_id,),
    ).fetchall()

    summary = {key: 0 for key in _CLASSIFICATIONS}
    items = []
    for ann in anns:
        loc_row = con.execute(
            "SELECT * FROM locations WHERE annotation_id = ? AND version_id = ?",
            (ann["id"], v_from["id"]),
        ).fetchone()

        classification = "indeterminate"
        reason = None
        from_view = None
        to_view = None
        hits: list = []

        if loc_row is None:
            reason = (
                f"该批注在起始版第 {from_version} 版没有迁移记录"
                "（批注创建于该版之后），不提供投影范围"
            )
        else:
            loc = _location_out(con, loc_row)
            from_view = {
                "location_id": loc["location_id"],
                "status": loc["status"],
                "start": loc["start"],
                "end": loc["end"],
            }
            if loc["status"] in ("resolved", "confirmed"):
                start, end = loc["start"], loc["end"]
                proj_start, proj_end = project_range(segments, start, end)
                classification = classify_range(segments, start, end)
                to_view = {"start": proj_start, "end": proj_end}
                hits = [
                    segment_dicts[i]
                    for i in hit_segment_indices(segments, start, end)
                ]
            elif loc["status"] == "ambiguous":
                if loc["carried_forward"]:
                    reason = (
                        f"起始版第 {from_version} 版中该批注为歧义顺延占位记录"
                        "（原始歧义未经人工确认），不提供投影范围"
                    )
                else:
                    reason = (
                        f"起始版第 {from_version} 版中该批注位置歧义且未人工确认"
                        "（存在多个并列最高分的候选范围），不提供投影范围"
                    )
            else:  # unresolved
                reason = (
                    f"起始版第 {from_version} 版中该批注未解析"
                    "（选区在该版没有精确落点），不提供投影范围"
                )

        summary[classification] += 1
        items.append(
            {
                "annotation_id": ann["id"],
                "body": ann["body"],
                "selection": ann["selection"],
                "classification": classification,
                "reason": reason,
                "from": from_view,
                "to": to_view,
                "hits": hits,
            }
        )

    return {
        "document_id": document_id,
        "from_version": from_version,
        "to_version": to_version,
        "segment_count": len(segment_dicts),
        "segments": segment_dicts,
        "summary": summary,
        "annotations": items,
    }


# ---------------------------------------------------------------------------
# 批注驱动的脱敏副本
# ---------------------------------------------------------------------------


def _redaction_error(index: int, annotation_id: int, error: str, message: str) -> dict:
    return {
        "error": error,
        "message": message,
        "path": f"annotation_ids[{index}]",
        "annotation_id": annotation_id,
    }


@app.post("/documents/{document_id}/redact")
def create_redaction(
    document_id: int, payload: RedactionCreate, con=Depends(get_db)
):
    """生成批注驱动的脱敏副本（只读，不写入任何数据）。

    在**同一个 SQLite 读事务**（同一快照）内读取目标版本全文与每条批注在
    该版本上的生效位置；只接受生效状态为 ``resolved`` / ``confirmed`` 且
    范围有效的批注。以下情况整批拒绝（422，``detail.errors`` 一次性返回
    全部可定位错误），不猜测、不输出半成品：

    - 请求中重复出现的批注 ID（``duplicate_annotation``）；
    - 批注不存在（``annotation_not_found``）或属于其他文档
      （``cross_document``）；
    - 该版本上没有迁移记录（``location_missing``，如批注创建于更晚版本）；
    - 生效位置为 ``ambiguous``（含顺延占位）/ ``unresolved``
      （``not_effective``），或记录缺少有效范围（``invalid_range``）。

    通过的生效范围中，重叠或首尾相接的区间合并为最大区间，按起点顺序
    用同一个占位文本替换：每个占位只出现一次，并列出覆盖它的批注 ID。
    响应返回脱敏全文与源 / 结果 Unicode 码点半开区间映射
    （``fragments`` 同时覆盖遮盖与未遮盖片段，各自铺满全文）。
    相同快照与相同请求必然得到字节一致的结果。
    """
    with db.read_transaction(con):
        _get_document(con, document_id)
        version = _get_version(con, document_id, payload.version_number)
        text = version["content"]

        errors: list = []
        unique_ids: set = set()
        # 重复 ID：每个出现位置都报告；先收集全部输入错误再读位置
        for index, annotation_id in enumerate(payload.annotation_ids):
            if annotation_id in unique_ids:
                errors.append(
                    _redaction_error(
                        index,
                        annotation_id,
                        "duplicate_annotation",
                        f"批注 {annotation_id} 在 annotation_ids 中重复出现",
                    )
                )
            unique_ids.add(annotation_id)

        ranges: list = []
        for annotation_id in sorted(unique_ids):
            ann = con.execute(
                "SELECT * FROM annotations WHERE id = ?", (annotation_id,)
            ).fetchone()
            if ann is None:
                index = payload.annotation_ids.index(annotation_id)
                errors.append(
                    _redaction_error(
                        index,
                        annotation_id,
                        "annotation_not_found",
                        f"批注 {annotation_id} 不存在",
                    )
                )
                continue
            if ann["document_id"] != document_id:
                index = payload.annotation_ids.index(annotation_id)
                errors.append(
                    _redaction_error(
                        index,
                        annotation_id,
                        "cross_document",
                        f"跨文档引用：批注 {annotation_id} 属于文档 "
                        f"{ann['document_id']}，不能在文档 {document_id} 中脱敏",
                    )
                )
                continue

            loc = con.execute(
                "SELECT * FROM locations WHERE annotation_id = ? AND version_id = ?",
                (annotation_id, version["id"]),
            ).fetchone()
            index = payload.annotation_ids.index(annotation_id)
            if loc is None:
                errors.append(
                    _redaction_error(
                        index,
                        annotation_id,
                        "location_missing",
                        f"批注 {annotation_id} 在第 {payload.version_number} 版"
                        "没有迁移记录（该批注创建于更晚版本），无法脱敏",
                    )
                )
                continue

            conf = _confirmation_of(con, loc["id"])
            if conf is not None:
                status, start, end = "confirmed", conf["start"], conf["end"]
            else:
                status, start, end = loc["status"], loc["start"], loc["end"]
            if status not in ("resolved", "confirmed"):
                if status == "ambiguous" and loc["carried_from"] is not None:
                    detail = "位置为歧义顺延占位（原始歧义未经人工确认）"
                elif status == "ambiguous":
                    detail = "位置歧义且未经人工确认（存在多个并列最高分的候选范围）"
                else:
                    detail = "位置未解析（选区在该版没有精确落点）"
                errors.append(
                    _redaction_error(
                        index,
                        annotation_id,
                        "not_effective",
                        f"批注 {annotation_id} 在第 {payload.version_number} 版的"
                        f"生效状态为 {status}：{detail}，不提供脱敏范围",
                    )
                )
                continue
            if (
                start is None
                or end is None
                or not (0 <= start < end <= len(text))
            ):
                errors.append(
                    _redaction_error(
                        index,
                        annotation_id,
                        "invalid_range",
                        f"批注 {annotation_id} 在第 {payload.version_number} 版的"
                        f"生效范围 [{start}, {end}) 无效"
                        f"（文本长度 {len(text)} 个码点）",
                    )
                )
                continue
            ranges.append(EffectiveRange(annotation_id, start, end))

        if errors:
            # 稳定排序：按请求位置，其次按错误码
            errors.sort(key=lambda e: (e["path"], e["error"]))
            raise HTTPException(
                422,
                detail={
                    "message": "脱敏请求校验失败，整批未处理（未生成脱敏副本）",
                    "errors": errors,
                },
            )

        intervals = merge_ranges(ranges)
        result = build_redaction(text, intervals, payload.placeholder)

    return {
        "document_id": document_id,
        "version_number": payload.version_number,
        "placeholder": payload.placeholder,
        "annotation_ids": sorted(unique_ids),
        "source_length": len(text),
        "redacted": result["redacted"],
        "redacted_interval_count": len(intervals),
        "fragments": result["fragments"],
        "placeholders": result["placeholders"],
    }


# ---------------------------------------------------------------------------
# 审阅依赖编排
# ---------------------------------------------------------------------------


def _dependency_out(row: sqlite3.Row) -> dict:
    return {
        "dependency_id": row["id"],
        "prerequisite_id": row["prerequisite_id"],
        "dependent_id": row["dependent_id"],
        "created_at": row["created_at"],
    }


def _completion_out(row: sqlite3.Row, already_completed: bool, unlocked: list) -> dict:
    return {
        "completion_id": row["id"],
        "document_id": row["document_id"],
        "annotation_id": row["annotation_id"],
        "note": row["note"],
        "created_at": row["created_at"],
        "already_completed": already_completed,
        "unlocked_annotation_ids": unlocked,
    }


def _review_state(con: sqlite3.Connection, document_id: int):
    """文档当前的审阅状态：可处理 / 被阻塞 / 已完成 三类的批注编号集合。

    被阻塞 = 未完成且存在尚未完成的传递前置项；可处理 = 未完成且无阻塞。
    实现统一在 :mod:`app.release_candidates`（发布候选创建 / 确认共用）。
    """
    return rc_mod.review_statuses(con, document_id)


@app.post("/documents/{document_id}/dependencies", status_code=201)
def create_dependencies(
    document_id: int, payload: DependencyBatchCreate, con=Depends(get_db)
):
    """批量提交同一文档内「前置批注 → 后续批注」的审阅依赖关系。

    整批写入前依次校验（任一失败则全部不落库）：
    跨文档引用（422）→ 自依赖（422）→ 批内/已存重复边（422/409）→ 环（422）。
    发现环时按批注编号升序遍历，返回首条以环中最小编号开头的闭环路径。
    """
    _get_document(con, document_id)
    relations = payload.relations
    with db.transaction(con):
        # 1. 引用校验：批注必须存在且属于当前文档（禁止跨文档引用）
        for rel in relations:
            for aid in (rel.prerequisite_id, rel.dependent_id):
                row = con.execute(
                    "SELECT id, document_id FROM annotations WHERE id = ?", (aid,)
                ).fetchone()
                if row is None:
                    raise HTTPException(404, f"批注 {aid} 不存在")
                if row["document_id"] != document_id:
                    raise HTTPException(
                        422,
                        f"跨文档引用：批注 {aid} 属于文档 {row['document_id']}，"
                        f"不能在文档 {document_id} 中建立依赖",
                    )
        # 2. 自依赖
        for rel in relations:
            if rel.prerequisite_id == rel.dependent_id:
                raise HTTPException(
                    422, f"自依赖：批注 {rel.prerequisite_id} 不能依赖自身"
                )
        # 3. 批内重复边
        seen = set()
        for rel in relations:
            edge = (rel.prerequisite_id, rel.dependent_id)
            if edge in seen:
                raise HTTPException(
                    422,
                    f"重复边：前置批注 {edge[0]} → 后续批注 {edge[1]} "
                    "在本批中出现多次",
                )
            seen.add(edge)
        # 4. 与已落库边重复
        existing_edges = [
            (r["prerequisite_id"], r["dependent_id"])
            for r in con.execute(
                "SELECT prerequisite_id, dependent_id FROM dependencies "
                "WHERE document_id = ?",
                (document_id,),
            ).fetchall()
        ]
        existing_set = set(existing_edges)
        for edge in seen:
            if edge in existing_set:
                raise HTTPException(
                    409,
                    f"重复边：前置批注 {edge[0]} → 后续批注 {edge[1]} 已存在",
                )
        # 5. 环检测（已落库边 ∪ 本批新边）
        cycle = find_cycle_path(existing_edges + sorted(seen))
        if cycle is not None:
            raise HTTPException(
                422,
                detail={
                    "message": "依赖关系存在环："
                    + " → ".join(str(n) for n in cycle),
                    "cycle": cycle,
                },
            )
        # 6. 全部校验通过，整批写入
        created = []
        for rel in relations:
            cur = con.execute(
                "INSERT INTO dependencies (document_id, prerequisite_id, dependent_id)"
                " VALUES (?, ?, ?)",
                (document_id, rel.prerequisite_id, rel.dependent_id),
            )
            row = con.execute(
                "SELECT * FROM dependencies WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            created.append(_dependency_out(row))
    return {
        "document_id": document_id,
        "created_count": len(created),
        "dependencies": created,
    }


@app.get("/documents/{document_id}/dependencies")
def list_dependencies(document_id: int, con=Depends(get_db)):
    """文档下全部审阅依赖边（按写入顺序）。"""
    _get_document(con, document_id)
    rows = con.execute(
        "SELECT * FROM dependencies WHERE document_id = ? ORDER BY id",
        (document_id,),
    ).fetchall()
    return {
        "document_id": document_id,
        "dependencies": [_dependency_out(r) for r in rows],
    }


@app.get("/documents/{document_id}/review-plan")
def review_plan(document_id: int, con=Depends(get_db)):
    """审阅计划：按依赖图与完成记录划分可处理 / 被阻塞 / 已完成。

    - 被阻塞项列出所有尚未完成的传递前置项（按编号升序）；
    - 未完成批注按拓扑层级划分为可并行批次（batches）：
      第 0 层即可处理项，第 k 层在其前置全部落入更低层后可并行处理；
    - 只读的确定性计算，结果不落库。
    """
    _get_document(con, document_id)
    anns = con.execute(
        "SELECT * FROM annotations WHERE document_id = ? ORDER BY id",
        (document_id,),
    ).fetchall()
    ann_ids, prereqs, completed, _, _ = _review_state(con, document_id)
    completion_rows = {
        r["annotation_id"]: r
        for r in con.execute(
            "SELECT * FROM completions WHERE document_id = ?", (document_id,)
        ).fetchall()
    }

    actionable, blocked, done = [], [], []
    for ann in anns:
        aid = ann["id"]
        comp = completion_rows.get(aid)
        if comp is not None:
            done.append(
                {
                    "annotation_id": aid,
                    "body": ann["body"],
                    "note": comp["note"],
                    "completed_at": comp["created_at"],
                }
            )
            continue
        item = {
            "annotation_id": aid,
            "body": ann["body"],
            "prerequisite_ids": sorted(prereqs.get(aid, ())),
        }
        incomplete = sorted(
            a for a in transitive_prerequisites(prereqs, aid) if a not in completed
        )
        if incomplete:
            item["incomplete_prerequisites"] = incomplete
            blocked.append(item)
        else:
            actionable.append(item)

    waves = topological_waves(ann_ids, prereqs, completed)
    return {
        "document_id": document_id,
        "summary": {
            "total": len(anns),
            "completed": len(done),
            "actionable": len(actionable),
            "blocked": len(blocked),
            "batch_count": len(waves),
        },
        "actionable": actionable,
        "blocked": blocked,
        "completed": done,
        "batches": [
            {"level": level, "annotation_ids": wave}
            for level, wave in enumerate(waves)
        ],
    }


@app.post(
    "/documents/{document_id}/annotations/{annotation_id}/completions",
    status_code=201,
)
def create_completion(
    document_id: int,
    annotation_id: int,
    payload: CompletionCreate,
    response: Response,
    con=Depends(get_db),
):
    """标记批注审阅完成（只追加，重复提交幂等返回 200 与原记录）。

    - 被阻塞（存在未完成传递前置项）的批注不可完成（409）；
    - 完成前置批注后立即解锁符合条件的后续批注，
      响应中的 unlocked_annotation_ids 列出本次新解锁的批注。
    """
    _get_document(con, document_id)
    _get_annotation(con, document_id, annotation_id)
    with db.transaction(con):
        existing = con.execute(
            "SELECT * FROM completions WHERE annotation_id = ?", (annotation_id,)
        ).fetchone()
        if existing is not None:
            # 幂等：重复提交返回原记录，不新增行、不改变状态
            response.status_code = 200
            return _completion_out(existing, already_completed=True, unlocked=[])

        _, prereqs, completed, _, blocked = _review_state(con, document_id)
        if annotation_id in blocked:
            incomplete = sorted(
                a
                for a in transitive_prerequisites(prereqs, annotation_id)
                if a not in completed
            )
            raise HTTPException(
                409,
                detail={
                    "message": f"批注 {annotation_id} 被阻塞："
                    "存在尚未完成的前置批注",
                    "incomplete_prerequisites": incomplete,
                },
            )

        cur = con.execute(
            "INSERT INTO completions (document_id, annotation_id, note)"
            " VALUES (?, ?, ?)",
            (document_id, annotation_id, payload.note),
        )
        row = con.execute(
            "SELECT * FROM completions WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        # 立即解锁：完成前被阻塞、完成后可处理的批注
        _, _, _, actionable_after, _ = _review_state(con, document_id)
        unlocked = sorted(aid for aid in blocked if aid in actionable_after)
    return _completion_out(row, already_completed=False, unlocked=unlocked)


@app.get("/documents/{document_id}/completions")
def list_completions(document_id: int, con=Depends(get_db)):
    """文档下全部完成记录（只追加，按写入顺序）。"""
    _get_document(con, document_id)
    rows = con.execute(
        "SELECT * FROM completions WHERE document_id = ? ORDER BY id",
        (document_id,),
    ).fetchall()
    return {
        "document_id": document_id,
        "completions": [
            {
                "completion_id": r["id"],
                "annotation_id": r["annotation_id"],
                "note": r["note"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# 迁移归档：规范导出与离线核验
# ---------------------------------------------------------------------------

_DIGEST_DESCRIPTION = (
    "摘要算法：sha256(json.dumps(archive, ensure_ascii=False, "
    "sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()"
)


@app.get("/documents/{document_id}/archive")
def export_archive(document_id: int, con=Depends(get_db)):
    """导出指定文档的迁移归档及其 SHA-256 摘要。

    文档元数据、全部版本、批注、迁移记录与人工确认在**同一个 SQLite 读事务**
    （同一快照）中取出；各列表按行 ID 升序、字段固定，归档内容规范化后
    计算 SHA-256 摘要。摘要应在导出后另行保存，与归档分开存放或转交。
    """
    with db.read_transaction(con):
        _get_document(con, document_id)
        arch = archive_mod.build_archive(con, document_id)
    digest = archive_mod.compute_digest(arch)
    return {
        "document_id": document_id,
        "format": archive_mod.ARCHIVE_FORMAT,
        "digest": digest,
        "digest_algorithm": _DIGEST_DESCRIPTION,
        "archive": arch,
    }


@app.post("/archives/verify")
def verify_archive_endpoint(payload: ArchiveVerifyRequest):
    """离线核验迁移归档：复算摘要并检查内部一致性。

    **不查当前数据库、不写入任何数据**——只依据请求中的归档与摘要：

    1. 复算摘要（摘要不符或归档被篡改 → ``digest_mismatch``）；
    2. 版本编号必须从 1 开始连续无重复；
    3. 版本、批注归属归档文档（防串文档），迁移记录的批注 / 版本引用、
       确认的迁移记录引用、批注的创建版引用均须存在；
    4. 每条批注从创建版到末版，每版恰有一条迁移记录；
    5. 顺延引用（``carried_from``）须指向同一批注较早版本的真实歧义记录
       （非顺延、``status=ambiguous``）；
    6. 人工确认须指向非顺延的歧义记录，且所选范围属于其候选落点。

    任一失败返回 422，``detail.errors`` 列出全部可定位错误
    （``error`` 错误码、``message`` 业务说明、``path`` JSON 路径定位）。
    """
    arch = payload.archive.model_dump(mode="json")
    errors = archive_mod.verify_archive(arch, payload.digest)
    if errors:
        raise HTTPException(
            422,
            detail={"valid": False, "errors": [e.to_dict() for e in errors]},
        )
    return {
        "valid": True,
        "format": arch["format"],
        "document_id": arch["document"]["id"],
        "digest": payload.digest.strip().lower(),
        "counts": {
            "versions": len(arch["versions"]),
            "annotations": len(arch["annotations"]),
            "locations": len(arch["locations"]),
            "confirmations": len(arch["confirmations"]),
        },
    }


# ---------------------------------------------------------------------------
# 可离线往返的审阅包：同读事务冻结 → 离线填写 → 同写事务回传
# ---------------------------------------------------------------------------


@app.post("/documents/{document_id}/review-packages", status_code=201)
def create_review_package(
    document_id: int, payload: ReviewPackageCreate, con=Depends(get_db)
):
    """创建可离线往返的审阅包。

    指定批注的最新迁移记录、候选范围、完成状态与全部传递前置状态在
    **同一个 SQLite 读事务**（同一快照）中冻结；未完成批注的未完成
    传递前置项自动收录进同包（保证后项可由同包前置项解锁）。只收录
    当时可确认、可完成或能由同包前置项解锁的条目；已完成且不可确认
    的请求批注在 ``excluded`` 中说明（不作为错误）。

    冻结内容（``package``）规范化后计算 SHA-256 摘要，与随机
    ``package_id`` 一起落库；审阅人离线保存 package + digest 后即可
    逐项填写 confirm / complete 决定，回传时见
    ``POST /review-packages/submit``。
    """
    requested = payload.annotation_ids
    duplicates = sorted({aid for aid in requested if requested.count(aid) > 1})
    if duplicates:
        raise HTTPException(
            422,
            detail={
                "message": "审阅包创建请求校验失败，未冻结任何内容",
                "errors": [
                    {
                        "error": "duplicate_annotation",
                        "message": f"批注 {aid} 在 annotation_ids 中重复出现",
                        "path": "annotation_ids",
                        "annotation_id": aid,
                    }
                    for aid in duplicates
                ],
            },
        )

    package = None
    with db.read_transaction(con):
        _get_document(con, document_id)
        errors = []
        for i, aid in enumerate(requested):
            row = con.execute(
                "SELECT id, document_id FROM annotations WHERE id = ?", (aid,)
            ).fetchone()
            if row is None:
                errors.append(
                    {
                        "error": "annotation_not_found",
                        "message": f"批注 {aid} 不存在",
                        "path": f"annotation_ids[{i}]",
                        "annotation_id": aid,
                    }
                )
            elif row["document_id"] != document_id:
                errors.append(
                    {
                        "error": "cross_document",
                        "message": f"批注 {aid} 属于文档 {row['document_id']}，"
                        f"不能加入文档 {document_id} 的审阅包",
                        "path": f"annotation_ids[{i}]",
                        "annotation_id": aid,
                    }
                )
        if errors:
            status = 404 if any(e["error"] == "annotation_not_found" for e in errors) else 422
            raise HTTPException(
                status,
                detail={"message": "审阅包创建请求校验失败，未冻结任何内容", "errors": errors},
            )

        package = rp_mod.build_package(
            con, document_id, requested, note=payload.note
        )
        if not package["items"]:            raise HTTPException(
                422,
                detail={
                    "message": "审阅包没有可收录的条目：请求的批注在冻结时均已"
                    "完成且最新位置无需确认，未创建审阅包",
                    "errors": [
                        {
                            "error": e["reason"],
                            "message": e["message"],
                            "path": "annotation_ids",
                            "annotation_id": e["annotation_id"],
                        }
                        for e in package["excluded"]
                    ],
                },
            )

    # 摘要 / package_id 计算无需在事务内（只读结果已在内存中）
    digest = rp_mod.compute_digest(package)
    package_id = rp_mod.new_package_id()

    # 冻结完成后落库（独立短写事务；读事务内只做只读查询，满足
    # 「冻结发生在同一读事务」的要求）。冻结与落库之间即使有并发写入，
    # 也只影响下一次创建的包：本包的 payload / digest 已是不可变快照。
    with db.transaction(con):
        con.execute(
            "INSERT INTO review_packages (id, document_id, digest, payload_json)"
            " VALUES (?, ?, ?, ?)",
            (
                package_id,
                document_id,
                digest,
                json.dumps(package, ensure_ascii=False),
            ),
        )
        row = con.execute(
            "SELECT created_at FROM review_packages WHERE id = ?", (package_id,)
        ).fetchone()

    return {
        "package_id": package_id,
        "document_id": document_id,
        "format": rp_mod.PACKAGE_FORMAT,
        "digest": digest,
        "digest_algorithm": _DIGEST_DESCRIPTION,
        "created_at": row["created_at"],
        "included_count": len(package["items"]),
        "excluded": package["excluded"],
        "package": package,
    }


@app.post("/review-packages/submit", status_code=201)
def submit_review_package(payload: ReviewPackageSubmit, response: Response, con=Depends(get_db)):
    """回传离线填写的审阅包：冲突整包拒绝，无冲突按依赖顺序一次写入。

    1. **离线结构校验**（不依赖当前数据库快照，422 一次性返回全部
       带 ``decisions[i]`` / ``items[i]`` 定位的问题）：决定重复 /
       针对包外条目 / action 非法 / 对不可确认项 confirm / 候选范围
       缺失或不属于冻结候选 / 对冻结时已完成项 complete / complete
       缺少同包前置决定；
    2. **同写事务冲突检测**（BEGIN IMMEDIATE，409 整包不写入）：
       目标已被人工确认、完成状态变化，或新增版本等使最新迁移记录
       改变（版本 / 行 ID / 状态 / 范围 / 顺延引用）；冲突逐条定位；
    3. **按依赖顺序应用**：确认按批注 ID 升序追加，完成按前置 →
       后续拓扑顺序追加（后项由同包前项解锁）；任一决定在应用时
       非法 → 全部回滚；
    4. **幂等重放**：同一 package_id 成功回传后再次提交，原样返回
       首次结果（200，``already_applied=true``），不重复写入；
       重放时携带的 digest / decisions 与首次不同则 409。
    """
    package_id = payload.package_id
    digest = payload.digest.strip().lower()

    lookup = con.execute(
        "SELECT id, document_id FROM review_packages WHERE id = ?", (package_id,)
    ).fetchone()
    if lookup is None:
        raise HTTPException(404, f"审阅包 {package_id} 不存在（package_id 无法识别）")
    document_id = lookup["document_id"]
    decisions = [d.model_dump(exclude_none=True) for d in payload.decisions]

    # 离线结构 / 语义校验：只依据冻结包，不查当前库（也不需要写锁）。
    # 包行内容在写事务外预读一次用于校验；权威比对在下面的写事务内重做。
    package = json.loads(
        con.execute(
            "SELECT payload_json FROM review_packages WHERE id = ?", (package_id,)
        ).fetchone()["payload_json"]
    )
    decision_errors = rp_mod.collect_decision_errors(package, decisions)
    if decision_errors:
        raise HTTPException(
            422,
            detail={
                "message": "审阅包回传决定校验失败，整包未处理（未写入任何确认或完成）",
                "package_id": package_id,
                "errors": [e.to_dict() for e in decision_errors],
            },
        )

    decisions_by_annotation = {d["annotation_id"]: d for d in decisions}
    with db.transaction(con):
        # 取得写锁后重读包行：消除「事务外检查 applied_at」与真正写入之间的
        # 竞态——并发回传在此处被 BEGIN IMMEDIATE 串行化，后到者必然看到
        # 先到者提交的 applied_at / result_json，从而走幂等重放而非重复写入。
        row = con.execute(
            "SELECT * FROM review_packages WHERE id = ?", (package_id,)
        ).fetchone()

        if row["applied_at"] is not None:
            if digest != row["digest"]:
                raise HTTPException(
                    409,
                    detail={
                        "message": "审阅包已成功回传过；本次携带的摘要与首次不同，"
                        "不能覆盖首次结果",
                        "errors": [
                            {
                                "error": "replay_mismatch",
                                "message": "重复回传的 digest 与首次回传不一致",
                                "path": "digest",
                            }
                        ],
                    },
                )
            response.status_code = 200
            result = rp_mod.replay_result(row)
            result["already_applied"] = True
            return result

        if digest != row["digest"]:
            raise HTTPException(
                422,
                detail={
                    "message": "审阅包摘要不符：冻结内容被改动，或摘要不是创建时所得；"
                    "本次回传未做任何写入",
                    "errors": [
                        {
                            "error": "package_digest_mismatch",
                            "message": "提交摘要与创建时冻结内容的 SHA-256 不一致",
                            "path": "digest",
                        }
                    ],
                },
            )

        package = json.loads(row["payload_json"])
        conflicts = rp_mod.detect_conflicts(
            con, document_id, package, decisions_by_annotation
        )
        if conflicts:
            raise HTTPException(
                409,
                detail={
                    "message": "审阅包冻结后目标状态已变化，整包未处理"
                    "（未写入任何确认或完成），请重新创建审阅包",
                    "package_id": package_id,
                    "conflicts": [c.to_dict() for c in conflicts],
                },
            )
        result = rp_mod.apply_package(
            con, document_id, package, package_id, decisions
        )

    result["already_applied"] = False
    return result


# ---------------------------------------------------------------------------
# 文档发布门禁：策略修订（按名称只追加）与同事务快照评估
# ---------------------------------------------------------------------------


def _policy_revision_out(row: sqlite3.Row) -> dict:
    return {
        "name": row["name"],
        "revision": row["revision"],
        "strategy": json.loads(row["strategy_json"]),
        "created_at": row["created_at"],
    }


def _get_policy_revision(
    con: sqlite3.Connection, name: str, revision: Optional[int] = None
) -> sqlite3.Row:
    """取指定名称的某一策略修订；``revision`` 为 None 时取最新修订。"""
    if revision is None:
        row = con.execute(
            "SELECT * FROM policy_revisions WHERE name = ? "
            "ORDER BY revision DESC LIMIT 1",
            (name,),
        ).fetchone()
    else:
        row = con.execute(
            "SELECT * FROM policy_revisions WHERE name = ? AND revision = ?",
            (name, revision),
        ).fetchone()
    if row is None:
        if revision is None:
            raise HTTPException(404, f"门禁策略 {name!r} 不存在（尚无任何修订）")
        raise HTTPException(
            404, f"门禁策略 {name!r} 不存在第 {revision} 版修订"
        )
    return row


def _gate_snapshot(con: sqlite3.Connection, document_id: int, version_id: int):
    """在读事务快照内汇总门禁评估输入：目标版位置状态 + 当前审阅状态。

    位置状态取目标版本上每条批注迁移记录的**生效**状态（有人工确认时为
    confirmed；顺延占位仍是 ambiguous；该版本无迁移记录的批注不计入任何
    位置状态）；审阅状态按当前依赖图与完成记录划分。所有 SELECT 必须由
    调用方包在同一个读事务中。实现统一在 :mod:`app.release_candidates`
    （发布候选创建 / 确认复评共用同一快照口径）。
    """
    return rc_mod.gate_snapshot(con, document_id, version_id)


@app.post("/policies", status_code=201)
def create_policy(payload: PolicyCreate, con=Depends(get_db)):
    """提交文档发布门禁策略：语法校验通过后**按名称只追加**一个修订。

    - 策略节点只允许 all / any / not 与 location_count / review_count 叶子，
      叶子字段固定为 status、op、value；
    - 未知字段、空组合、not 子项不是一个、深度超过 8 或节点超过 64 时
      **整条拒绝**（422，``detail.errors`` 带 JSON 路径定位，不落任何修订）；
    - 同名重复提交一律生成递增的新修订（revision 从 1 起），
      旧修订不修改、不删除；策略树按提交原样保存（子节点顺序即提交顺序）。
    """
    errors = gate_mod.validate_strategy(payload.strategy)
    if errors:
        raise HTTPException(
            422,
            detail={
                "message": f"门禁策略 {payload.name!r} 校验失败，本次修订未落库",
                "errors": [e.to_dict() for e in errors],
            },
        )
    strategy_json = json.dumps(payload.strategy, ensure_ascii=False)
    with db.transaction(con):
        revision = con.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 AS r "
            "FROM policy_revisions WHERE name = ?",
            (payload.name,),
        ).fetchone()["r"]
        con.execute(
            "INSERT INTO policy_revisions (name, revision, strategy_json) "
            "VALUES (?, ?, ?)",
            (payload.name, revision, strategy_json),
        )
        row = con.execute(
            "SELECT * FROM policy_revisions WHERE name = ? AND revision = ?",
            (payload.name, revision),
        ).fetchone()
    return _policy_revision_out(row)


@app.get("/policies")
def list_policies(con=Depends(get_db)):
    """列出全部门禁策略的最新修订（按名称升序；修订历史按名称另查）。"""
    rows = con.execute(
        """
        SELECT p.* FROM policy_revisions p
        JOIN (SELECT name, MAX(revision) AS revision
              FROM policy_revisions GROUP BY name) AS latest
          ON latest.name = p.name AND latest.revision = p.revision
        ORDER BY p.name
        """
    ).fetchall()
    return {"policies": [_policy_revision_out(row) for row in rows]}


@app.get("/policies/{name}")
def list_policy_revisions(name: str, con=Depends(get_db)):
    """某名称门禁策略的全部修订（只追加，按修订号升序）。"""
    _get_policy_revision(con, name)
    rows = con.execute(
        "SELECT * FROM policy_revisions WHERE name = ? ORDER BY revision",
        (name,),
    ).fetchall()
    return {"name": name, "revisions": [_policy_revision_out(row) for row in rows]}


@app.get("/policies/{name}/revisions/{revision}")
def get_policy_revision(name: str, revision: int, con=Depends(get_db)):
    """取某名称门禁策略的指定修订（审计用）。"""
    return _policy_revision_out(_get_policy_revision(con, name, revision))


@app.get("/documents/{document_id}/gate/evaluate")
def evaluate_gate(
    document_id: int,
    policy: str = Query(..., description="门禁策略名称"),
    revision: Optional[int] = Query(None, ge=1, description="策略修订号；缺省取最新"),
    version: Optional[int] = Query(
        None, ge=1, description="目标文档版本号；缺省取最新版本"
    ),
    con=Depends(get_db),
):
    """在同一快照上评估发布门禁策略，返回同形解释树。

    策略修订、目标文档版本、目标版全部批注的生效位置状态与当前审阅状态
    （actionable / blocked / completed）在**同一个 SQLite 读事务**内读取，
    评估期间不会混入并发写入。解释树：

    - 与策略树同形，``all`` / ``any`` **不短路**（每个子节点都被评估）；
    - 叶子列出 ``actual``（实际计数）、``value``（阈值）、``op`` 与
      参与计数的批注 ID（``annotation_ids``，按编号升序）；
    - 每个节点带布尔 ``result``，根节点结果即整体门禁结论 ``passed``；
    - 相同快照（同一修订、同一版本、同一审阅状态）下结果与 ID 顺序稳定。
    """
    with db.read_transaction(con):
        _get_document(con, document_id)
        policy_row = _get_policy_revision(con, policy, revision)
        if version is None:
            version_row = _latest_version(con, document_id)
            if version_row is None:
                raise HTTPException(404, f"文档 {document_id} 没有任何版本")
        else:
            version_row = _get_version(con, document_id, version)
        strategy = json.loads(policy_row["strategy_json"])
        snapshot = _gate_snapshot(con, document_id, version_row["id"])
        tree = gate_mod.evaluate_strategy(strategy, snapshot)
    return {
        "document_id": document_id,
        "policy_name": policy_row["name"],
        "policy_revision": policy_row["revision"],
        "version_number": version_row["number"],
        "passed": tree["result"],
        "explanation": tree,
    }


# ---------------------------------------------------------------------------
# 文档审阅发布候选：同读事务冻结 → 同写事务确认发布
# ---------------------------------------------------------------------------


def _candidate_out(row: sqlite3.Row, frozen: dict) -> dict:
    return {
        "candidate_id": row["id"],
        "document_id": row["document_id"],
        "format": rc_mod.CANDIDATE_FORMAT,
        "digest": row["digest"],
        "digest_algorithm": _DIGEST_DESCRIPTION,
        "created_at": row["created_at"],
        "released_at": row["released_at"],
        "status": "released" if row["released_at"] is not None else "pending",
        "policy_name": frozen["policy"]["name"],
        "policy_revision": frozen["policy"]["revision"],
        "version_number": frozen["version"]["number"],
        "watermark": frozen["watermark"],
        "frozen": frozen,
    }


@app.post("/documents/{document_id}/release-candidates", status_code=201)
def create_release_candidate(
    document_id: int, payload: ReleaseCandidateCreate, con=Depends(get_db)
):
    """创建文档审阅发布候选：提交文档版本与门禁策略修订。

    目标版本、策略修订、门禁解释树、当前审阅状态（actionable / blocked /
    completed）与该文档六类事件（版本 / 批注 / 迁移位置 / 人工确认 /
    依赖 / 完成记录）的水位在**同一个 SQLite 读事务**（同一快照）内固定。

    - 初始门禁未通过 → **409 拒绝创建，不生成 candidate_id、不落候选**，
      响应仍给出解释树（``passed=false``）便于定位未通过节点；
    - 门禁通过 → 生成不可变 ``candidate_id``（随机 32 位十六进制）与
      规范化 SHA-256 ``digest``，冻结内容（``frozen``）随候选行落库；
      ``policy_revision`` / ``version_number`` 缺省分别锁定最新策略修订 /
      最新版本；策略 / 版本 / 文档不存在返回 404，文档无版本返回 409。

    创建后用 ``POST /release-candidates/{candidate_id}/confirm`` 确认发布。
    """
    with db.read_transaction(con):
        _get_document(con, document_id)
        try:
            frozen = rc_mod.build_candidate(
                con,
                document_id,
                payload.policy,
                payload.policy_revision,
                payload.version_number,
                note=payload.note,
            )
        except rc_mod.GateRejected as exc:
            body = rc_mod.gate_rejected_response(exc)
            body["document_id"] = document_id
            raise HTTPException(409, detail=body)
        except rc_mod.CandidateError as exc:
            raise HTTPException(
                exc.status_code,
                detail={"message": exc.message, "errors": [exc.to_dict()]},
            )

    # 摘要 / candidate_id 计算无需在事务内（只读结果已在内存中）
    digest = rc_mod.compute_digest(frozen)
    candidate_id = rc_mod.new_candidate_id()

    # 冻结完成后落库（独立短写事务；读事务内只做只读查询，满足
    # 「固定发生在同一读事务」的要求）。冻结与落库之间即使有并发写入，
    # 也只影响下一次创建：本候选的 frozen / digest 已是不可变快照，
    # 确认时会以冻结水位检测出该写入并报冲突。
    with db.transaction(con):
        con.execute(
            "INSERT INTO release_candidates (id, document_id, digest, frozen_json)"
            " VALUES (?, ?, ?, ?)",
            (
                candidate_id,
                document_id,
                digest,
                json.dumps(frozen, ensure_ascii=False),
            ),
        )
        row = con.execute(
            "SELECT * FROM release_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()

    return _candidate_out(row, frozen)


@app.post("/release-candidates/{candidate_id}/confirm", status_code=201)
def confirm_release_candidate(
    candidate_id: str, response: Response, con=Depends(get_db)
):
    """确认发布候选：水位未变且门禁仍通过才追加一次 released 结果。

    在**同一个写事务**（BEGIN IMMEDIATE）中重新读取候选行、该文档六类
    事件水位与门禁快照：

    - 候选创建后该文档出现新的版本 / 批注 / 迁移 / 确认 / 依赖 /
      完成记录（任一水位上涨），或冻结的策略修订在目标版本 + 当前快照上
      **门禁不再通过** → **409 冲突**（``detail.conflicts`` 逐条可定位：
      水位冲突定位 ``watermark.<表名>`` 并附冻结 / 当前水位，门禁冲突
      定位 ``gate`` 并附当前解释树），**不改变候选状态、不写入结果**；
    - 六类水位全部未变且门禁仍通过 → 追加一次 ``released`` 结果
      （released_at / result_json 只写一次），返回 201；
    - 重复确认同一 ``candidate_id``：原样返回首次结果（200，
      ``already_released=true``），不重复写入；candidate_id 无法识别 → 404。
    """
    lookup = con.execute(
        "SELECT id FROM release_candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    if lookup is None:
        raise HTTPException(
            404, f"发布候选 {candidate_id} 不存在（candidate_id 无法识别）"
        )

    with db.transaction(con):
        # 取得写锁后重读候选行：并发确认在此处被 BEGIN IMMEDIATE 串行化，
        # 后到者必然看到先到者提交的 released_at / result_json，从而走
        # 幂等重放而非重复写入。
        row = con.execute(
            "SELECT * FROM release_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        frozen = json.loads(row["frozen_json"])

        if row["released_at"] is not None:
            response.status_code = 200
            result = rc_mod.replay_result(row)
            result["already_released"] = True
            return result

        conflicts = rc_mod.detect_confirm_conflicts(con, frozen)
        if conflicts:
            raise HTTPException(
                409,
                detail={
                    "message": "发布候选创建后该文档状态已变化或门禁不再通过，"
                    "本次确认未发布、候选状态未改变，请重新创建候选",
                    "candidate_id": candidate_id,
                    "conflicts": [c.to_dict() for c in conflicts],
                },
            )

        result = rc_mod.persist_release(
            con, candidate_id, frozen, row["digest"]
        )

    result["already_released"] = False
    return result


# ---------------------------------------------------------------------------
# 文档增量事件流：六类只追加记录的稳定合并分页馈送
# ---------------------------------------------------------------------------


def _cursor_error(exc: events_mod.CursorError) -> HTTPException:
    return HTTPException(
        422, detail={"error": exc.error, "message": exc.message}
    )


@app.get("/documents/{document_id}/events")
def document_events(
    document_id: int,
    limit: int = Query(50, ge=1, le=200, description="每页事件数（1～200）"),
    cursor: Optional[str] = Query(
        None, description="上一页返回的 next_cursor / resume_cursor"
    ),
    con=Depends(get_db),
):
    """文档增量事件流（只读）：版本、批注、迁移位置、人工确认、依赖、完成
    记录按 ``(created_at, type_rank, id)`` **稳定合并**（全序，不漏 / 不重 /
    不乱序）。

    - **首轮**（不带 ``cursor``）：在**同一个 SQLite 读事务**（同一快照）中
      固定六张表最大行 ID 作为终点水位向量 ``to``（起点 ``from`` 为全零），
      读取窗口 ``(from, to]`` 内属于本文档的事件；
    - **分页**（``next_cursor``，kind=page）：游标封装文档 ID、起止水位与
      最后排序键，只返回窗口内排序键严格大于该键的事件；**始终沿用签发时的
      终点水位**，分页期间并发新增的行（自增 ID 大于终点水位）留到下一轮；
    - **续拉**（``resume_cursor``，kind=resume）：窗口读尽（末页）时返回；
      携带已消费水位，本次调用重新固定终点水位，只读 ``(from, 新 to]``，
      **空轮同样推进水位**（末页仍返回新的 resume_cursor）。

    游标为 Base64URL 紧凑 JSON，自带版本号；游标版本不支持、结构损坏、
    水位范围矛盾或**跨文档使用**一律 422。事件 ``payload`` 只保留对应行
    落库时的稳定业务字段（例如迁移位置保留库中的 resolved / ambiguous /
    unresolved 原始状态与顺延引用），**不计算查询时刻的派生状态**。
    """
    with db.read_transaction(con):
        _get_document(con, document_id)
        ceiling = events_mod.current_watermark(con)

        if cursor is None:
            window_from = events_mod.zero_watermark()
            window_to = ceiling
            last_key = None
        else:
            try:
                parsed = events_mod.decode_cursor(cursor, document_id)
                window_from = parsed["from"]
                if parsed["kind"] == "resume":
                    # 新一轮：沿用已消费水位为起点，在本快照固定新的终点水位
                    events_mod.ensure_within(window_from, ceiling)
                    window_to = ceiling
                    last_key = None
                else:
                    # 续页：窗口与终点水位完全沿用游标，不受并发新增影响
                    events_mod.ensure_within(parsed["to"], ceiling)
                    window_to = parsed["to"]
                    last_key = parsed["last"]
            except events_mod.CursorError as exc:
                raise _cursor_error(exc)

        events, last_sort_key, has_more = events_mod.fetch_page(
            con, document_id, window_from, window_to, last_key, limit
        )

    if has_more:
        # 窗口未尽：签发续页游标（同一窗口推进最后排序键）
        next_cursor = events_mod.make_page_cursor(
            document_id, window_from, window_to, last_sort_key
        )
        resume_cursor = None
    else:
        # 末页（含事件为空的空轮）：签发续拉游标，已消费水位推进到窗口终点
        next_cursor = None
        resume_cursor = events_mod.make_resume_cursor(document_id, window_to)

    return {
        "document_id": document_id,
        "limit": limit,
        "event_count": len(events),
        "has_more": has_more,
        "events": events,
        "window": {"from": window_from, "to": window_to},
        "next_cursor": next_cursor,
        "resume_cursor": resume_cursor,
    }


# ---------------------------------------------------------------------------
# 版本范围字面子串检索：命中位置与相交批注状态
# ---------------------------------------------------------------------------


def _search_cursor_error(exc: search_mod.CursorError) -> HTTPException:
    return HTTPException(
        422, detail={"error": exc.error, "message": exc.message}
    )


@app.post("/documents/{document_id}/search")
def search_document(
    document_id: int, payload: SearchRequest, con=Depends(get_db)
):
    """在同一文档的指定版本范围内字面子串检索（只读）。

    在版本号区间 ``[from_version, to_version]`` 内逐版按 **Unicode 码点**
    做字面匹配（保留重叠命中）；每个命中返回版本号、半开范围
    ``[start, end)``、可复现的前后文片段，以及在该版本上**与该范围相交**
    的批注 ID 与生效状态：

    - 有人工确认 → ``confirmed``，以确认范围为准（确认范围优先于迁移
      候选）；``resolved`` → 迁移记录自带范围；
    - 未确认且非顺延的 ``ambiguous`` → 列出与命中相交的**候选范围**；
    - ``unresolved`` 与歧义顺延占位在该版本没有范围，不会出现在
      ``annotations`` 中。

    命中按 ``(版本号, 起点, 终点)`` 稳定排序，每页**至多 100** 个。首轮在
    **同一个 SQLite 读事务**中固定 versions / annotations / locations /
    confirmations 四表单文档水位快照与版本上界；``next_cursor``（续页）
    始终沿用该快照——分页期间并发新增的版本或批注 / 确认**留到下一轮**；
    窗口读尽后返回 ``resume_cursor``，携带它调用即在新快照中开启下一轮
    （空轮也会签发新的 resume_cursor）。游标经 HMAC-SHA256 签名：
    篡改、跨文档使用或携带的筛选条件与签发时不一致一律 **422**。
    """
    with db.read_transaction(con):
        _get_document(con, document_id)
        secret = search_mod.get_secret(con)
        ceiling = search_mod.current_watermark(con, document_id)

        if payload.cursor is None:
            query = payload.query
            from_version = payload.from_version
            explicit_to = payload.to_version
            radius = payload.context_radius
            wm = ceiling
            after: Optional[tuple] = None
        else:
            try:
                parsed = search_mod.decode_cursor(payload.cursor, secret)
            except search_mod.CursorError as exc:
                raise _search_cursor_error(exc)
            if parsed["doc"] != document_id:
                raise HTTPException(
                    422,
                    detail={
                        "error": "cross_document_cursor",
                        "message": f"游标属于文档 {parsed['doc']}，不能用于文档 "
                        f"{document_id} 的检索（游标不可跨文档使用）",
                    },
                )
            # 筛选条件一致性：query / from_version 必须逐字一致；可选字段
            # （to_version / context_radius）在请求中**省略**时视为沿用游标
            # 值（方便客户端只回传 cursor），显式给出但与冻结值不同才拒绝。
            mismatches = [
                name
                for name, same in (
                    ("query", payload.query == parsed["q"]),
                    ("from_version", payload.from_version == parsed["vf"]),
                    (
                        "to_version",
                        payload.to_version is None
                        or payload.to_version == parsed["vt"],
                    ),
                    (
                        "context_radius",
                        "context_radius" not in payload.model_fields_set
                        or payload.context_radius == parsed["r"],
                    ),
                )
                if not same
            ]
            if mismatches:
                raise HTTPException(
                    422,
                    detail={
                        "error": "filter_mismatch",
                        "message": "请求的筛选条件与游标冻结值不一致："
                        + "、".join(mismatches)
                        + "（续用游标时必须原样携带签发时的 "
                        "query / from_version / to_version / context_radius；"
                        "新的筛选条件请重新发起首轮检索）",
                        "fields": mismatches,
                    },
                )

            query = parsed["q"]
            from_version = parsed["vf"]
            radius = parsed["r"]
            try:
                # 续页 / 续拉都不接受水位超过当前库（游标来自其他库等）
                search_mod.ensure_within(parsed["wm"], ceiling)
            except search_mod.CursorError as exc:
                raise _search_cursor_error(exc)
            if parsed["kind"] == "page":
                # 续页：快照、版本上界与最后排序键全部沿用游标
                wm = parsed["wm"]
                after = parsed["after"]
                explicit_to = None if parsed["td"] else parsed["vt"]
            else:
                # 续拉：在本快照开启下一轮，新增版本 / 批注 / 确认由此进入。
                # vt=None（首轮缺省最新版）时下一轮仍缺省取新快照最新版；
                # vt=具体版本号时显式上界保持不变。
                wm = ceiling
                after = None
                explicit_to = parsed["vt"]

        latest = con.execute(
            "SELECT number FROM versions WHERE document_id = ? AND id <= ? "
            "ORDER BY number DESC LIMIT 1",
            (document_id, wm["versions"]),
        ).fetchone()
        if latest is None:
            raise HTTPException(404, f"文档 {document_id} 在当前快照中没有任何版本")
        latest_number = latest["number"]
        if from_version > latest_number:
            raise HTTPException(
                422,
                detail={
                    "error": "version_out_of_range",
                    "message": f"from_version={from_version} 超出当前快照的最新"
                    f"版本号 {latest_number}",
                },
            )
        to_version = explicit_to if explicit_to is not None else latest_number
        if to_version < from_version:
            raise HTTPException(
                422,
                detail={
                    "error": "invalid_version_range",
                    "message": f"版本范围非法：from_version={from_version} 大于 "
                    f"to_version={to_version}",
                },
            )
        if to_version > latest_number:
            raise HTTPException(
                422,
                detail={
                    "error": "version_out_of_range",
                    "message": f"to_version={to_version} 超出当前快照的最新"
                    f"版本号 {latest_number}（续页期间新增的版本已留到下一轮，"
                    "请用 resume_cursor 续拉）",
                },
            )

        versions = search_mod.load_versions(
            con, document_id, wm, from_version, to_version
        )
        effective_by_version = {
            version["id"]: search_mod.load_effective(con, version["id"], wm)
            for version in versions
        }
        hits, keys = search_mod.build_hits(
            versions, query, radius, effective_by_version
        )
        page, last_key, has_more = search_mod.select_page(hits, keys, after)

    if has_more:
        # 续页游标记录本轮实际检索上界；td 标注它是否由缺省最新版解析而来，
        # 以便该轮读尽派生 resume 游标时恢复「缺省最新版」语义。
        next_cursor = search_mod.make_page_cursor(
            document_id, query, from_version, to_version, radius, wm,
            last_key, secret, to_default=explicit_to is None,
        )
        resume_cursor = None
    else:
        next_cursor = None
        # 记录调用方显式给出的上界（缺省最新版时为 None，续拉重新取最新）
        resume_cursor = search_mod.make_resume_cursor(
            document_id, query, from_version, explicit_to, radius, wm, secret
        )

    return {
        "document_id": document_id,
        "query": query,
        "from_version": from_version,
        "to_version": to_version,
        "context_radius": radius,
        "page_size": search_mod.PAGE_SIZE,
        "hit_count": len(page),
        "total_in_snapshot": len(hits),
        "has_more": has_more,
        "hits": page,
        "snapshot": {
            "latest_version": latest_number,
            "watermark": wm,
        },
        "next_cursor": next_cursor,
        "resume_cursor": resume_cursor,
    }
