"""请求体的 Pydantic 模型。所有偏移均按 Unicode 码点计。"""
from __future__ import annotations

from typing import Any, Annotated, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class DocumentCreate(BaseModel):
    """新建文档，content 即基准文本（第 1 版）。"""

    title: str = Field(min_length=1, max_length=200)
    content: str


class VersionCreate(BaseModel):
    """提交新版全文。"""

    content: str


class AnnotationCreate(BaseModel):
    """新建批注：正文 + 锚点（选区、左右上下文、原始码点偏移）。"""

    body: str = Field(min_length=1, description="批注正文")
    selection: str = Field(min_length=1, description="选区文本")
    left_context: str = Field("", description="选区左侧上下文")
    right_context: str = Field("", description="选区右侧上下文")
    start: int = Field(ge=0, description="选区起点（码点偏移，含）")
    end: int = Field(ge=0, description="选区终点（码点偏移，不含）")


class ConfirmationCreate(BaseModel):
    """人工确认歧义批注：从候选范围中选定一个。"""

    version_number: int = Field(ge=1, description="歧义所在的版本号")
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    note: Optional[str] = Field(None, description="确认备注")


class DependencyRelation(BaseModel):
    """一条「前置批注 → 后续批注」的审阅依赖关系。"""

    prerequisite_id: int = Field(ge=1, description="前置批注 ID（需先完成）")
    dependent_id: int = Field(ge=1, description="后续批注 ID（前置完成前被阻塞）")


class DependencyBatchCreate(BaseModel):
    """批量提交同一文档内的审阅依赖关系。

    整批校验（跨文档引用、自依赖、重复边、环），任一失败则全部不落库。
    """

    relations: List[DependencyRelation] = Field(min_length=1)


class CompletionCreate(BaseModel):
    """标记批注审阅完成（只追加，重复提交幂等）。"""

    note: Optional[str] = Field(None, description="完成备注")


class RedactionCreate(BaseModel):
    """批注驱动的脱敏副本请求：指定版本、批注 ID 集合与占位文本。

    只接受该版本上生效状态为 ``resolved`` / ``confirmed`` 且范围有效的
    批注；跨文档、缺少位置、范围无效、ambiguous（含顺延占位）或
    unresolved 的批注整批拒绝，不生成任何半成品。
    """

    version_number: int = Field(ge=1, description="文档版本号（在该版的生效位置上脱敏）")
    annotation_ids: List[Annotated[int, Field(ge=1)]] = Field(
        min_length=1, description="要遮盖的批注 ID 集合（重复 ID 整批拒绝）"
    )
    placeholder: str = Field(
        min_length=1, description="遮盖选区用的占位文本（按 Unicode 码点替换）"
    )


class CoordinateInterval(BaseModel):
    """一个半开区间 ``[start, end)``（允许空区间 ``[x, x)``）。

    偏移以请求级 ``input_unit`` 为准（codepoint / utf16 / utf8_byte）；
    端点必须对齐码点边界，范围校验由 ``app.coordinates`` 整批完成，
    故这里不做 ``ge=0`` 限制，以便把越界值作为带条目标定位的
    422 业务错误返回（而非 schema 报错）。
    """

    start: int
    end: int


class CoordinateBatchCreate(BaseModel):
    """批量偏移换算请求：声明输入单位并提交至多 200 个半开区间。"""

    input_unit: Literal["codepoint", "utf16", "utf8_byte"] = Field(
        description=(
            "区间端点的计量单位：codepoint=Unicode 码点（本服务统一单位），"
            "utf16=UTF-16 码元（JavaScript 字符串偏移），"
            "utf8_byte=UTF-8 字节偏移"
        )
    )
    intervals: List[CoordinateInterval] = Field(
        min_length=1, max_length=200, description="待换算的半开区间（1～200 个）"
    )


class PolicyCreate(BaseModel):
    """提交文档发布门禁策略（按名称只追加修订）。

    ``strategy`` 为策略树根节点对象；语法（节点形态、叶子字段、取值域、
    深度 / 节点数上限）由 ``app.gate`` 一次性校验，任一非法即整修订拒绝。
    """

    name: str = Field(min_length=1, max_length=200, description="策略名称")
    strategy: Dict[str, Any] = Field(description="策略树根节点（all / any / not / 叶子）")


# ---------------------------------------------------------------------------
# 迁移归档（导出结构与离线核验请求）
#
# 归档各列表按行 ID 升序、字段固定；模型一律 extra="forbid"，
# 多一个字段即视为结构错误，保证摘要复算基于与导出时完全一致的内容。
# ---------------------------------------------------------------------------


class ArchiveDocument(BaseModel):
    """归档中的文档元数据。"""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    title: str
    created_at: str


class ArchiveVersion(BaseModel):
    """归档中的一版全文。"""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    document_id: int = Field(ge=1)
    number: int = Field(ge=1)
    content: str
    created_at: str


class ArchiveAnnotation(BaseModel):
    """归档中的一条批注及其锚点。"""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    document_id: int = Field(ge=1)
    body: str
    selection: str
    left_context: str
    right_context: str
    orig_start: int = Field(ge=0)
    orig_end: int = Field(ge=0)
    created_version_id: int = Field(ge=1)
    created_at: str


class ArchiveCandidate(BaseModel):
    """迁移记录中的一个候选落点及其匹配依据。"""

    model_config = ConfigDict(extra="forbid")

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    left_match: int = Field(ge=0)
    right_match: int = Field(ge=0)
    score: int = Field(ge=0)


class ArchiveLocation(BaseModel):
    """归档中的一条迁移记录（某批注在某版上的落点）。"""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    annotation_id: int = Field(ge=1)
    version_id: int = Field(ge=1)
    status: Literal["resolved", "ambiguous", "unresolved"]
    start: Optional[int] = Field(None, ge=0)
    end: Optional[int] = Field(None, ge=0)
    left_match: Optional[int] = Field(None, ge=0)
    right_match: Optional[int] = Field(None, ge=0)
    score: Optional[int] = Field(None, ge=0)
    candidates: List[ArchiveCandidate]
    carried_from: Optional[int] = Field(
        None, ge=1, description="非空表示歧义未确认而顺延，指向原始歧义记录"
    )
    created_at: str


class ArchiveConfirmation(BaseModel):
    """归档中的一条人工确认。"""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    location_id: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    note: Optional[str]
    created_at: str


class Archive(BaseModel):
    """一份完整的迁移归档（导出接口的 archive 字段原样）。"""

    model_config = ConfigDict(extra="forbid")

    format: str
    document: ArchiveDocument
    versions: List[ArchiveVersion]
    annotations: List[ArchiveAnnotation]
    locations: List[ArchiveLocation]
    confirmations: List[ArchiveConfirmation]


class ArchiveVerifyRequest(BaseModel):
    """离线核验请求：归档 + 导出时另行保存的摘要。"""

    archive: Archive
    digest: str = Field(
        pattern="^[0-9a-fA-F]{64}$",
        description="导出时返回的 SHA-256 摘要（64 位十六进制）",
    )


# ---------------------------------------------------------------------------
# 可离线往返的审阅包
# ---------------------------------------------------------------------------


class ReviewPackageCreate(BaseModel):
    """创建审阅包：指定要离线审阅的批注 ID 集合（非空、不可重复）。

    未完成批注的未完成传递前置项会被自动收录进同包；已完成且不可
    确认的请求批注在响应的 ``excluded`` 中说明，不作为错误。
    """

    annotation_ids: List[Annotated[int, Field(ge=1)]] = Field(
        min_length=1, description="要离线审阅的批注 ID 集合（自动收录未完成传递前置项）"
    )
    note: Optional[str] = Field(None, description="审阅包备注（随快照冻结）")


class ReviewDecision(BaseModel):
    """离线填写的一条审阅决定：确认歧义（confirm）或标记完成（complete）。

    ``action`` 取值域在 :func:`app.review_packages.collect_decision_errors`
    中整批校验（连同其余决定问题一次性带定位返回），故此处放宽为 str，
    非法值不提前由 schema 拦截。
    """

    annotation_id: int = Field(ge=1)
    action: str = Field(description="决定类型：confirm（确认歧义候选）或 complete（标记完成）")
    start: Optional[int] = Field(
        None, ge=0, description="confirm：所选候选范围起点（码点，含）"
    )
    end: Optional[int] = Field(
        None, ge=0, description="confirm：所选候选范围终点（码点，不含）"
    )
    note: Optional[str] = Field(None, description="确认 / 完成备注")


class ReviewPackageSubmit(BaseModel):
    """审阅包回传：package_id、创建时的内容摘要与逐项决定。

    决定的结构 / 合法性先离线对照冻结包校验（422，带 decisions[i] /
    items[i] 定位）；通过后在写事务内检测冻结后的目标变化（409），
    无冲突才按依赖顺序一次性写入。
    """

    package_id: str = Field(min_length=1, description="创建审阅包时返回的 package_id")
    digest: str = Field(
        pattern="^[0-9a-fA-F]{64}$",
        description="创建审阅包时返回的内容摘要（64 位十六进制 SHA-256）",
    )
    decisions: List[ReviewDecision] = Field(
        default_factory=list, description="逐项审阅决定（confirm / complete），可为空"
    )


class SearchRequest(BaseModel):
    """版本范围字面子串检索请求（只读，分页大小固定 100）。

    ``query`` 在 ``[from_version, to_version]`` 内逐版按 Unicode 码点做
    字面匹配（保留重叠命中）；``to_version`` 缺省取首轮快照内的最新版。
    ``context_radius`` 为前后文片段单侧最大码点数（0～80，缺省 16）。
    ``cursor`` 为上一页返回的 next_cursor 或上一轮返回的 resume_cursor；
    携带游标时其余筛选字段必须与游标冻结值完全一致（否则 422）。
    """

    query: str = Field(
        min_length=1,
        max_length=256,
        description="要字面检索的文字（按 Unicode 码点匹配，保留重叠命中）",
    )
    from_version: int = Field(ge=1, description="版本范围下界（含）")
    to_version: Optional[int] = Field(
        None, ge=1, description="版本范围上界（含）；缺省取首轮快照最新版"
    )
    context_radius: int = Field(
        default=16, ge=0, le=80, description="前后文片段单侧最大码点数（0～80）"
    )
    cursor: Optional[str] = Field(
        None, description="next_cursor（续页）或 resume_cursor（续拉下一轮）"
    )


class ReleaseCandidateCreate(BaseModel):
    """创建文档审阅发布候选：提交文档版本与门禁策略修订。

    服务在同一个 SQLite 读事务中固定该版本、策略修订、门禁解释树、
    当前审阅状态与该文档六类事件的水位；初始门禁未通过则拒绝创建、
    不落候选（409）。``policy_revision`` / ``version_number`` 缺省分别
    锁定最新策略修订 / 文档最新版本。
    """

    policy: str = Field(min_length=1, description="门禁策略名称")
    policy_revision: Optional[int] = Field(
        None, ge=1, description="策略修订号；缺省锁定最新修订"
    )
    version_number: Optional[int] = Field(
        None, ge=1, description="目标文档版本号；缺省锁定最新版本"
    )
    note: Optional[str] = Field(None, description="候选备注（随快照冻结）")
