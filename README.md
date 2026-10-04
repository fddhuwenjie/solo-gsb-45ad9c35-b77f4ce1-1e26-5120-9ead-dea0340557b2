# 文档修订批注续接 API

基于 **Python + FastAPI + SQLite** 的批注跨版本续接与审阅依赖编排服务。

为文档中的批注保存锚点（**选区 + 左右上下文 + 按 Unicode 码点计的原始偏移**），
当文档提交新版全文时，自动把每条批注续接到新文本中的最佳落点；
歧义时交人工确认，所有版本与迁移结果**只追加**，可随时审计任意两版间每条批注的范围、状态与匹配依据。

同时支持**审阅依赖编排**：批量提交同一文档内「前置批注 → 后续批注」的依赖关系
（整批校验跨文档引用、自依赖、重复边与环，任一失败则全部不落库），
审阅计划按依赖图与完成记录把批注划分为**可处理 / 被阻塞 / 已完成**，
并按拓扑层级给出可并行批次；完成记录只追加、重复提交幂等，
完成前置批注后立即解锁符合条件的后续批注。

还支持**可离线核验的迁移归档**：在同一个 SQLite 读事务中导出文档元数据、
全部版本、批注、迁移记录与人工确认的规范 JSON 及其 SHA-256 摘要；
核验只依据归档与另行保存的摘要本身，**不查当前数据库、不写入任何数据**，
缺行、串文档、篡改或摘要不符时返回带 JSON 路径定位的错误。

最后支持**文档发布门禁**：按名称只追加地保存嵌套发布策略修订
（`all` / `any` / `not` 组合 `location_count` / `review_count` 叶子，
未知字段、空组合、`not` 子项不是一个、深度超过 8 或节点超过 64 时整条拒绝）；
评估接口在**同一个 SQLite 读事务**内锁定策略修订、目标文档版本与当前审阅状态，
返回与策略**同形**、`all`/`any` **不短路**的解释树
（叶子列出实际值、阈值与参与计数的批注 ID），相同快照下结果与 ID 顺序稳定。

此外支持**批注驱动的脱敏副本**：请求指定文档版本、批注 ID 集合与占位文本，
在**同一个 SQLite 读事务**（同一快照）中读取版本全文与各批注在该版上的生效
位置；只接受状态为 `resolved` / `confirmed` 的有效范围，批注跨文档、缺少位置、
位置 `ambiguous`（含顺延占位）/ `unresolved` 或请求 ID 重复时**整批**返回带
请求下标定位的错误，不猜测、不输出半成品。重叠或首尾相接的范围合并为最大区间，
按起点顺序用同一占位文本替换，每个占位只出现一次并列出覆盖它的批注 ID；
响应返回脱敏全文及源 / 结果 Unicode 码点半开区间映射（**未遮盖片段也可追溯**）。
接口只读、**不写入任何数据**，相同快照与相同请求必然得到字节一致的结果。

最后提供**文档增量事件流**（`GET /documents/{id}/events`）：版本、批注、迁移
位置、人工确认、依赖与完成记录六类只追加记录按 `(created_at, type_rank, id)`
**稳定合并**；首轮在**同一个 SQLite 读事务**中固定六张表最大行 ID 作为水位
向量，续页游标封装文档 ID、起止水位与最后排序键，只读取 `(起点, 终点]` 且
**始终沿用终点水位**（分页期间的并发新增留到下一轮，不漏、不重、不乱序）；
末页返回 `resume_cursor` 用于续拉增量，**空轮也推进水位**；游标版本、结构、
范围错误或跨文档使用一律 422，事件载荷只保留对应行落库时的稳定业务字段。

此外提供**批量偏移换算**（`POST /documents/{id}/versions/{n}/coordinates`）：
浏览器（JavaScript 字符串按 UTF-16 码元计偏移）与外部工具（常见按 UTF-8
字节计偏移）提交的选区端点，可批量换算为本服务统一的 Unicode 码点偏移，
避免 emoji 等字符让选区错位。请求声明输入单位（`codepoint` / `utf16` /
`utf8_byte`）并提交至多 200 个半开区间，服务先按目标版本全文**一次扫描
构建边界映射**，随后逐端点 O(1) 查表，整批复杂度 **O(n+m)**；响应逐项
给出三种单位的对应范围与原索引。越界、`start > end`、端点落在 UTF-16
代理对或 UTF-8 多字节字符内部时**整批返回 422** 并指出条目与端点，
绝不取整猜测。接口只读，同一快照结果稳定。

最后支持**可离线往返的审阅包**（`POST /documents/{id}/review-packages`
创建、`POST /review-packages/submit` 回传）：创建时在**同一个 SQLite
读事务**中冻结指定批注的最新迁移记录、候选范围、完成状态与全部传递前置
状态（未完成的传递前置项自动收录进同包），只收录当时可确认、可完成或
能由同包前置项解锁的条目，生成 `package_id` 与规范化 JSON 的 SHA-256
**内容摘要**；审阅人可完全离线保存包与摘要并逐项填写 `confirm` /
`complete` 决定。回传携带原摘要与决定：若目标在冻结后已被确认、完成
状态变化，或新增版本改变了最新位置，**整包返回带 `items[i]` 定位的
409 冲突且不写入**；无冲突时在**同一个 SQLite 写事务**中先按批注 ID
升序追加确认、再按前置→后续的拓扑顺序追加完成（**后项可由同包前项
解锁**），任一决定非法则全部回滚。同一 `package_id` 成功回传后再次
提交**原样返回首次结果（200，`already_applied=true`），不重复写入**。

此外支持**文档审阅发布候选**（`POST /documents/{id}/release-candidates`
创建、`POST /release-candidates/{candidate_id}/confirm` 确认）：创建时在
**同一个 SQLite 读事务**中固定文档版本、门禁策略修订、门禁解释树、当前
审阅状态与该文档**六类事件**（版本 / 批注 / 迁移 / 确认 / 依赖 / 完成）
的水位，初始门禁未通过即拒绝创建且不落候选，通过后生成不可变
`candidate_id` 与规范摘要。确认时在**同一个 SQLite 写事务**中重读水位与
门禁快照：候选创建后该文档出现任一六类事件、或门禁不再通过，即返回
**可定位冲突（409，水位冲突定位到 `watermark.<表名>`、门禁冲突定位
`gate`）且不改变候选状态**；仅水位未变且门禁仍通过时追加**一次**
`released` 结果。重复确认同一 `candidate_id` 原样返回首次结果（200，
`already_released=true`），不重复写入。

## 快速开始

一条命令在 8000 端口启动（二选一）：

```bash
docker compose up --build
# 或
docker build -t annotation-api . && docker run -p 8000:8000 annotation-api
```

启动后：

- 交互式 API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

数据通过 Compose 命名卷 `annotation-data` 持久化（SQLite 文件 `/data/app.db`）。

### 本地开发（不用 Docker）

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000   # 数据库默认写入 ./data/app.db
pytest                                       # 运行测试（316 个用例）
```

可用环境变量 `DB_PATH` 指定 SQLite 文件路径。

## 数据库版本化迁移与升级

SQLite schema 的建立与升级采用**版本化、可审计迁移**（实现见 `app/migrations.py`），
不再使用整块建表脚本。服务启动时（FastAPI lifespan，在接受任何业务请求之前）：

1. **非破坏性预检**（只读）：`PRAGMA integrity_check` 完整性检查；
   `confirmations` 历史重复检查；旧索引缺失、`app_meta` 游标密钥行缺失登记。
   - 发现同一 `location_id` 多条确认等**无法自动修复**的问题时，**拒绝启动**，
     错误信息逐条列出 `location_id` 与确认行 ID，绝不静默删改。
2. 在**单个 `BEGIN IMMEDIATE` 写事务**中读取当前 schema 版本，按编号顺序执行
   所有未完成迁移；每步先建 SAVEPOINT：
   - 成功：同事务内向 `schema_migrations` 追加一条 `applied` 审计行
     （编号、名称、状态、起止时间戳）；
   - 失败：**回滚该步**、同事务追加带错误文本的 `failed` 审计行后提交，
     随后中止启动。
3. 全部完成后、提交前做**结构校验**：必需表 / 唯一索引齐全、旧非唯一索引
   已移除、游标密钥行存在、无重复确认、版本连续、`PRAGMA foreign_key_check`
   无违例；任一不满足则**整体回滚**并拒绝启动。

迁移未完成或校验失败时，进程退出，**不接受任何业务请求**；
`GET /health` 在实例未就绪时返回 `503 {"status":"unavailable"}`。

### 审计表 schema_migrations

只追加（UPDATE/DELETE 被触发器拒绝），每次尝试都永久留痕：

| 列 | 含义 |
|---|---|
| `version` | 迁移编号（1 起，严格递增） |
| `name` | 迁移名（如 `confirmations_unique_index`） |
| `status` | `applied` 或 `failed`（每版本至多一条 `applied`，失败可多条） |
| `error` | 失败时的错误类型与文本（成功为 NULL） |
| `started_at` / `finished_at` | UTC 时间戳 |

当前内置迁移：

| 版本 | 名称 | 内容 |
|---|---|---|
| 1 | `initial_schema` | 建立初始表与索引（旧库仅补齐缺失对象，全部只追加数据原样保留） |
| 2 | `confirmations_unique_index` | 预检重复确认后，把非唯一索引 `idx_confirm_loc` 替换为唯一索引 `idx_confirm_loc_unique` |
| 3 | `app_meta_cursor_secret` | 建立 `app_meta` 并补齐检索游标 HMAC 密钥（`INSERT OR IGNORE`，只生成一次） |

### 升级（已部署旧库 / 新库，命令完全不变）

新库与旧库都使用相同的 Compose 命令、端口（8000）与 API，无需额外操作：

```bash
docker compose up --build        # 数据卷 annotation-data 中的旧库自动平滑升级
docker compose logs api          # 查看 "迁移 N（…）完成 / schema 已迁移至版本 N"
```

查看迁移结果与审计记录：

```bash
docker compose exec api python - <<'PY'
import sqlite3
con = sqlite3.connect("/data/app.db")
for row in con.execute(
    "SELECT version, name, status, error, started_at FROM schema_migrations ORDER BY id"
):
    print(row)
PY
```

**升级前建议备份**（复制出卷内文件后再启动新版本）：

```bash
docker run --rm -v annotation-data:/data -v "$PWD":/backup alpine \
    cp /data/app.db /backup/app.db.before-upgrade
```

### 失败恢复示例

**情形一：历史重复确认（预检拒绝，数据原样保留）。** 日志示例：

```
schema 预检失败，拒绝启动：confirmations 表存在历史重复确认…：
location_id=42（确认行 ID：107,108）
```

核对后保留正确的一条（示例保留行 ID 107，删除重复行 108），重新启动即可——
迁移重试幂等，会从失败步继续，已 applied 的版本不会重跑：

```bash
docker compose exec api python - <<'PY'
import sqlite3
con = sqlite3.connect("/data/app.db", isolation_level=None)
con.execute("DELETE FROM confirmations WHERE id = 108")   # 确认后删除重复行
con.execute('PRAGMA foreign_key_check')                  # 可选：再次自检
con.close()
PY
docker compose restart api
```

**情形二：某一步迁移失败（该步已回滚，审计留 failed 行）。**
日志会给出编号、名称与错误文本；修复根因（磁盘空间、文件权限等）后
`docker compose restart api` 即可幂等重试。审计中同一版本会看到
`failed` 在前、修复后 `applied` 在后，历史可追溯。

**情形三：结构校验失败（整个写事务回滚，库回到启动前状态）。**
从备份恢复后重新启动：

```bash
docker run --rm -v annotation-data:/data -v "$PWD":/backup alpine \
    sh -c 'cp /backup/app.db.before-upgrade /data/app.db && rm -f /data/app.db-wal /data/app.db-shm'
docker compose up --build
```

> 所有迁移语句均为 `IF NOT EXISTS` / `INSERT OR IGNORE` / `DROP IF EXISTS`
> 的非破坏写法，versions、annotations、locations、confirmations、dependencies、
> completions、policy_revisions、review_packages、release_candidates 等
> 只追加业务数据不会被删除或覆盖；游标密钥生成一次后跨重启不变。

## 核心概念

### 锚点（Anchor）

创建批注时保存：

| 字段 | 含义 |
|---|---|
| `selection` | 选区文本 |
| `left_context` / `right_context` | 选区左右上下文 |
| `start` / `end` | 原始偏移，**按 Unicode 码点计**（`end` 不含；emoji 等 BMP 外字符算 1 个码点） |

> ⚠️ 前端若用 JavaScript 计算偏移，注意 JS 字符串按 UTF-16 码元计，
> 遇到 emoji 等字符会与本服务的码点偏移不一致，需先换算。可直接调用
> [`POST /documents/{id}/versions/{n}/coordinates`](#批量偏移换算codepoint--utf16--utf8_byte)
> 把 UTF-16（或外部工具的 UTF-8）区间批量换算为码点区间（见下文示例）。

创建时服务端会严格校验 `text[start:end] == selection` 及左右上下文与基准文本一致，否则返回 422。

### 续接算法

提交新版全文时，对每条批注：

1. 在新文本中**枚举选区的全部精确落点**（允许重叠）；
2. 对每个落点计算得分 = **左上下文后缀匹配长度 + 右上下文前缀匹配长度**（码点数）；
3. 按得分降序（同分按起点升序）排序：
   - **唯一最高分** → `resolved`，自动续接到该落点；
   - **并列最高分** → `ambiguous`，返回全部候选范围，等待人工确认；
   - **找不到选区** → `unresolved`（选区在后续版本重新出现时会自动恢复）。

### 状态机

```
resolved ──迁移──→ resolved / ambiguous / unresolved
ambiguous ──未确认──→ 下一版顺延占位（carried_forward=true，不参与自动迁移）
ambiguous ──人工确认──→ confirmed ──下一版起恢复自动迁移──→ resolved / ...
unresolved ──选区重现──→ resolved
```

- `ambiguous` 批注在确认前**不参与**后续版本的自动迁移，每个新版本只追加一条顺延占位记录；
- 确认是对某一版歧义记录从候选范围中选定一个范围，确认结果同样只追加、不可更改；
- 同一歧义记录至多一条确认：检查与写入在同一个写事务中完成，且 `confirmations.location_id`
  有唯一索引兜底，并发确认时后到者稳定返回 409，不会覆盖首条或留下重复行；
- 确认占位记录（顺延产生的）会被拒绝（409），请确认原始歧义记录。

### 只追加（append-only）

- 版本：每次提交全文生成新版本号，旧版本内容不可改；
- 迁移结果：每条批注在每个版本上各有一条 `locations` 记录，只插入不更新；
- 确认：写入独立的 `confirmations` 表，查询时计算生效状态（`confirmed`）。

### 批注改动影响分析

`GET /documents/{id}/impact?from_version=&to_version=` 基于**两版全文**生成
**稳定的 Unicode 码点级编辑片段**（`insert` / `delete` / `replace`，按位置升序，
相同输入必然得到相同的片段序列与分类），并把**起始版**每条批注的范围投影到**目标版**：

| 分类 | 含义 |
|---|---|
| `unchanged` | 选区未被编辑触及，偏移也不变 |
| `shifted` | 选区内容未变，偏移仅受区间外（含边界）编辑影响而平移 |
| `modified` | 选区被部分删除、被替换，或选区**内部**有插入 |
| `deleted` | 原选区全部被删除（投影折叠为空区间，位于删除发生处） |
| `indeterminate` | 起始版位置歧义 / 未解析 / 缺失，**不猜测范围**，`reason` 说明原因 |

规则要点：

- **插入恰好落在选区边界**（起点或终点）**不算 `modified`**：落在起点时选区整体后移
  （`shifted`），落在终点时选区不受影响（`unchanged`）；只有严格落在选区**内部**的
  插入才是 `modified`；
- **替换**（哪怕整体替换）一律是 `modified`，只有纯粹被删除片段完全覆盖才是 `deleted`；
- 每条批注返回：原范围（`from`，含起始版迁移状态）、投影范围（`to`）、
  命中的编辑片段（`hits`，删除/替换与选区相交、插入落点在闭区间 `[start, end]`
  即命中；区间外的编辑不命中，即使它造成了平移）；
- `indeterminate` 时 `to` 为 `null`、`hits` 为空，`from` 给出起始版状态
  （`ambiguous` / `unresolved`，或起始版无迁移记录时整体为 `null`）；
- 该接口为只读的确定性计算，结果不落库，可重复调用审计。

### 审阅依赖编排

在同一文档内为批注建立「**前置批注 → 后续批注**」的审阅依赖：
后续批注须等全部前置批注完成后才可处理。

**批量提交依赖**（`POST /documents/{id}/dependencies`）：一次提交同一文档内多条关系，
整批写入前依次校验，**任一失败则任何关系都不落库**：

| 校验 | 失败响应 |
|---|---|
| 引用的批注不存在 | 404 |
| 跨文档引用（批注属于其他文档） | 422 |
| 自依赖（前置 = 后续） | 422 |
| 本批内重复边 | 422 |
| 与已落库边重复 | 409 |
| 环（已落库边 ∪ 本批新边） | 422，`detail.cycle` 给出闭环路径 |

环检测按**批注编号升序遍历**（起点升序、邻接点升序），返回**首条以环中最小编号开头
的闭环路径**，如 `{"detail": {"message": "依赖关系存在环：1 → 3 → 4 → 1", "cycle": [1, 3, 4, 1]}}`；
相同输入必然得到相同路径。依赖边只追加，不提供修改 / 删除。

**审阅计划**（`GET /documents/{id}/review-plan`）：按依赖图与完成记录把文档全部批注
划分为三类，只读计算、不落库：

- `actionable`（可处理）：未完成，且所有传递前置项均已完成（无依赖的批注天然可处理）；
- `blocked`（被阻塞）：存在尚未完成的传递前置项，
  `incomplete_prerequisites` 按编号升序列出**所有**尚未完成的传递前置项；
- `completed`（已完成）：已有完成记录。

`batches` 把未完成批注按**拓扑层级**划分为可并行批次：第 0 层即当前可处理项，
第 k 层在其前置全部落入更低层后可并行处理；同层批注互不依赖。

**完成记录**（`POST /documents/{id}/annotations/{aid}/completions`）：

- 只追加：一条批注至多一条完成记录，不提供修改 / 删除；
- 重复提交**幂等**：返回 200 与原记录（`already_completed=true`），不新增行；
- 被阻塞的批注不可完成（409，`detail.incomplete_prerequisites` 列出未完成前置项）；
- 完成前置批注后**立即解锁**符合条件的后续批注：响应的
  `unlocked_annotation_ids` 列出本次新解锁的批注，审阅计划同步反映。

### 迁移归档导出与离线核验

**导出**（`GET /documents/{id}/archive`）在**同一个 SQLite 读事务**（同一快照）
中取出文档的全部审计数据，各列表**按行 ID 升序**、字段固定：

| 归档字段 | 内容 |
|---|---|
| `format` | 归档格式标识（`annotation-archive/1`） |
| `document` | 文档元数据（id / title / created_at） |
| `versions` | 全部版本（含每版全文） |
| `annotations` | 全部批注及锚点（选区、左右上下文、原始偏移、创建版引用） |
| `locations` | 全部迁移记录（状态、范围、得分、候选落点、顺延引用） |
| `confirmations` | 全部人工确认（所选范围、备注） |

响应在 `archive` 之外另行给出 `digest`，摘要算法公开且只依赖标准库：

```python
import hashlib, json

digest = hashlib.sha256(
    json.dumps(archive, ensure_ascii=False,
               sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
```

即「**键按字典序、紧凑分隔、UTF-8 不转义**」的规范化 JSON 的 SHA-256。
摘要应在导出后**另行保存**（与归档分开存放），日后随归档一起提交核验。

**核验**（`POST /archives/verify`，请求体 `{"archive": ..., "digest": ...}`）
**不查当前数据库、不写入任何数据**，只复算摘要并检查归档内部一致性：

1. **摘要复算**：归档任一字节被改动而摘要未随之更新 → `digest_mismatch`；
2. **版本编号连续**：版本号必须从 1 开始、连续、无重复（`version_gap` /
   `duplicate_version_number`）；
3. **归属一致（防串文档、缺行）**：版本与批注的 `document_id` 必须等于归档文档
   （`cross_document`）；迁移记录引用的批注 / 版本、批注引用的创建版、
   确认引用的迁移记录、顺延引用的目标记录都必须在归档中存在
   （`dangling_*`）；
4. **每版恰有一条迁移记录**：每条批注从其**创建版到末版**，每个版本号恰有一条
   `locations` 记录（缺行 `missing_location`、多行 `duplicate_location`、
   早于创建版 `unexpected_location`）；
5. **顺延引用合法**：`carried_from` 必须指向**同一批注、较早版本的真实歧义记录**
   （该记录本身非顺延且 `status=ambiguous`，否则 `bad_carried_forward`）；
6. **人工确认合法**：确认必须指向**非顺延的歧义记录**（`bad_confirmation`），
   且所选 `[start, end)` 必须属于该记录的候选落点
   （`confirmation_not_candidate`）。

通过时返回 `{"valid": true, "document_id": ..., "digest": ..., "counts": {...}}`；
失败返回 422，`detail.errors` **一次性列出全部问题**，每条含：

| 字段 | 含义 |
|---|---|
| `error` | 机器可读错误码（见上） |
| `message` | 含业务 ID / 版本号的中文说明 |
| `path` | JSON 路径式定位，如 `versions[2]`、`annotations[1]`、`locations[5]`、`confirmations[0]` |

> 摘要不符意味着归档来源已不可信，此时只报 `digest_mismatch`，
> 不再对被篡改内容做进一步语义检查。

### 文档发布门禁

发布门禁用一棵**嵌套策略树**表达「什么情况下文档可以发布」。策略按**名称**
管理（全局命名，不绑定某一篇文档），同名提交**只追加修订**：每次合法提交
生成一个从 1 起递增的新 `revision`，旧修订不可改、不可删；评估时可以锁定
任意历史修订。

**节点语法**（每个节点恰好带下列一个键）：

| 节点 | 形态 | 语义 |
|---|---|---|
| 全部满足 | `{"all": [节点, ...]}` | 子节点全真才为真 |
| 任一满足 | `{"any": [节点, ...]}` | 子节点任一为真即可 |
| 取反 | `{"not": 节点}` | 唯一子节点为假时为真 |
| 位置计数叶子 | `{"location_count": {"status", "op", "value"}}` | 目标版本上该生效位置状态的批注数 |
| 审阅计数叶子 | `{"review_count": {"status", "op", "value"}}` | 当前该审阅状态的批注数 |

叶子字段**固定**为 `status` / `op` / `value` 三个，取值域：

- `location_count.status`：`resolved` / `confirmed` / `ambiguous` / `unresolved`
  （`confirmed` 是人工确认后的生效状态；顺延占位仍计为 `ambiguous`；
  目标版本上没有迁移记录的批注——如创建于更晚版本——不参与任何位置计数）；
- `review_count.status`：`actionable` / `blocked` / `completed`；
- `op`：`eq`（等于）/ `lte`（不超过）/ `gte`（不少于），`value` 为非负整数阈值。

**整条拒绝**的条件（422，`detail.errors` 一次性列出全部问题，带 `$...` JSON
路径定位，本次修订不落库）：

- 未知字段（节点上出现五种键以外的字段，或叶子里出现 status/op/value 以外字段）；
- 节点无法识别（不是对象、缺少类型字段、同时带多个类型字段）；
- `all` / `any` 为空组合或子项不是节点；`not` 的子项不是恰好一个节点；
- 叶子 status / op 超出取值域、value 不是非负整数、或缺少必需字段；
- 树深超过 **8**（叶子深度为 1，每层组合加 1）；节点总数超过 **64**。

**评估**（`GET /documents/{id}/gate/evaluate?policy=&revision=&version=`）：
策略修订、目标版本（缺省最新版）、目标版上全部批注的生效位置状态、当前审阅
状态在**同一个 SQLite 读事务**（同一快照）内读出。响应为：

- `passed`：根节点布尔结论；
- `explanation`：与策略**同形**的解释树，每个节点都带 `result`；
  `all` / `any` **不短路**——无论兄弟节点结果如何，每个子节点都被完整评估并
  出现在解释树中；
- 叶子解释为 `{status, op, value, actual, annotation_ids}`：`actual` 是实际计数，
  `annotation_ids` 是参与计数的批注 ID（按编号升序）；
- `revision` / `version` 缺省分别取最新修订 / 最新版本；策略、修订、版本或
  文档不存在返回 404。

相同快照（同一策略修订、同一目标版本、同一审阅状态）重复评估，结果及各
ID 列表顺序必然相同。

### 文档审阅发布候选

发布候选把「**某文档版本 + 某门禁策略修订，在当前审阅状态下通过**」这一
结论冻结成不可变快照，供稍后正式确认发布；确认时服务用冻结水位识别候选
创建后的任何相关变更，保证不会凭过期快照发布。

**创建**（`POST /documents/{id}/release-candidates`，请求体
`{"policy", "policy_revision"?, "version_number"?, "note"?}`）在**同一个
SQLite 读事务**（同一快照）内一次性固定：

| 冻结字段 | 内容 |
|---|---|
| `version` | 目标版本（版本号、版本行 ID、全文；`version_number` 缺省锁定最新版） |
| `policy` | 门禁策略名称、修订号（`policy_revision` 缺省锁定最新修订）与策略树原文 |
| `gate.explanation` | 该快照上评估出的**门禁解释树**（与策略同形、不短路，含各叶子实际计数） |
| `review_state` | 当前审阅状态：`actionable` / `blocked` / `completed` 的批注 ID（升序） |
| `watermark` | 该文档**六类事件**（版本 / 批注 / 迁移位置 / 人工确认 / 依赖 / 完成记录）的单文档水位：各表中属于本文档的行的最大 ID |

- **初始门禁未通过 → 409 拒绝创建，不生成 candidate_id、不落候选行**；
  响应仍返回 `passed=false` 与解释树，便于定位是哪个叶子没过（附
  `actual` 实际计数与参与计数的批注 ID）；
- 门禁通过 → 生成不可变 `candidate_id`（随机 32 位十六进制，与内容无关：
  同内容两次创建是两个候选）与 `digest`（冻结内容规范化 JSON 的
  SHA-256，算法与迁移归档 / 审阅包相同），`frozen` 随候选行落库，
  此后候选内容不再改变。策略 / 版本 / 文档不存在返回 404，文档没有任何
  版本返回 409（`no_version`）。

**确认**（`POST /release-candidates/{candidate_id}/confirm`，无请求体）
在**同一个写事务**（BEGIN IMMEDIATE）中重新读取候选行、六类事件水位与
门禁快照：

- 候选创建后该文档出现**任一**六类事件新行（新版本、新批注、新迁移、
  新确认、新依赖或新完成记录；水位为各表中属于本文档行的最大 ID，其他
  文档的写入不影响本候选），或冻结的策略修订在目标版本 + 当前快照上
  **门禁不再通过** → **409 冲突，不改变候选状态、不写发布结果**；
- `detail.conflicts` 逐条可定位：

  | 冲突 error | path | 附加上下文 |
  |---|---|---|
  | `watermark_changed` | `watermark.versions` / `watermark.annotations` / `watermark.locations` / `watermark.confirmations` / `watermark.dependencies` / `watermark.completions` | `event_type`（version/annotation/location/confirmation/dependency/completion）、`frozen_watermark`、`current_watermark` |
  | `gate_not_passed` | `gate` | 当前快照上的 `explanation` 解释树 |

- **仅当六类水位全部未变且门禁仍通过**时，追加**一次** `released` 结果
  （候选行的 `released_at` / `result_json` 只写一次，201）；
- **幂等**：同一 `candidate_id` 重复确认原样返回首次结果（200，
  `already_released=true`，`released_at` 与摘要均为首次值），不重复写入；
  无法识别的 `candidate_id` 返回 404。

> 水位取**单文档**水位（不是事件流的全表水位）：六类表对每文档只追加，
> 行 ID 单调，因此「该文档六类水位均未变」即「门禁输入未变」；确认时仍在
> 当前快照上**复评一次门禁**作为防御性兜底（也能识别绕过 API 的直接改库）。

### 批注驱动的脱敏副本

`POST /documents/{id}/redact` 在**不写入任何数据**的前提下，按指定版本上
批注的生效位置生成脱敏文本。请求体：

| 字段 | 含义 |
|---|---|
| `version_number` | 目标文档版本号（取批注在**该版**的生效位置） |
| `annotation_ids` | 要遮盖的批注 ID 集合（非空；重复 ID 整批拒绝） |
| `placeholder` | 占位文本（非空，按 Unicode 码点整体替换选区） |

**整批校验**——以下任一情况都返回 422，`detail.errors` 一次性列出全部问题
（不生成任何脱敏内容），每条错误带 `error` 机器码、`message` 中文说明、
`path` 请求定位（`annotation_ids[i]`）与 `annotation_id`：

| 情况 | 错误码 |
|---|---|
| 批注 ID 在请求中重复 | `duplicate_annotation` |
| 批注不存在 | `annotation_not_found` |
| 批注属于其他文档（跨文档） | `cross_document` |
| 目标版本上没有迁移记录（如批注创建于更晚版本） | `location_missing` |
| 生效位置为 `ambiguous`（含歧义顺延占位）/ `unresolved` | `not_effective` |
| 生效范围缺失或越界（防御性校验） | `invalid_range` |

文档或目标版本不存在返回 404；`annotation_ids` 为空、占位为空等请求结构
问题由 schema 直接返回 422。

**替换与合并**：版本全文与全部生效位置在**同一个 SQLite 读事务**（同一快照）
内读出。人工确认过的位置以确认范围（生效状态 `confirmed`）为准。

- 重叠或**首尾相接**（`next.start == prev.end`）的范围合并为**最大区间**，
  合并区间携带覆盖它的全部批注 ID（升序去重）；
- 合并区间按**起点升序**，用同一个占位文本逐个替换：**每个占位只出现一次**；
- 响应 `placeholders[i]` 列出该次占位的文本、在结果文本中的码点半开区间
  `[result_start, result_end)` 以及覆盖它的 `annotation_ids`。

**可追溯映射**：响应 `fragments` 按顺序铺满源文本与结果文本，二者均从 0
开始、相邻片段首尾相接：

- `{"kind": "kept", source_start, source_end, result_start, result_end}`：
  未遮盖片段，源区间与结果区间等长（未遮盖片段同样可追溯）；
- `{"kind": "redacted", ...source/result 区间, annotation_ids, placeholder_index}`：
  遮盖片段，源区间是被遮盖原文，结果区间对应占位文本。

全部偏移均为 **Unicode 码点半开区间**（emoji 等 BMP 外字符算 1 个码点）。
用 `fragments` 即可从源文本重建脱敏文本，或反向核对每段原文的去向。
计算完全确定：相同快照（同一版本内容、同一批批注位置）与相同请求，
响应序列化后**字节一致**（与请求中 ID 的排列顺序无关）。

### 文档批注覆盖图

`GET /documents/{id}/versions/{n}/coverage` 只读地给出某一版全文上**每一段
文本**分别被哪些批注覆盖，适合做覆盖可视化、审阅盲区检查。版本全文与该版
全部迁移位置在**同一个 SQLite 读事务**（同一快照）中读出，不写入任何数据。

每条批注在该版上归入三类之一：

| 该版生效情况 | 归类 | 说明 |
|---|---|---|
| `resolved` / `confirmed` | **definite** | 人工确认过的位置以确认范围为准（状态 `confirmed`） |
| `ambiguous` 未确认且**非顺延** | **possible** | 先把该批注候选范围中重叠 / 首尾相接的合并为最大区间，再逐个登记；同一 ID 在同一片段只出现一次 |
| `unresolved` | 不覆盖 | 列入 `exclusions`，原因 `unresolved` |
| `ambiguous` 顺延占位 | 不覆盖 | 原始歧义未经确认、本版未参与迁移；列入 `exclusions`，原因 `ambiguous_carried` |
| 该版没有迁移记录 | 不覆盖 | 批注创建于更晚版本；列入 `exclusions`，原因 `location_missing` |

响应 `segments` 把 `[0, 文本码点长度)` 划分为连续、无重叠的**最大片段**
（空文本也是单个 `[0, 0)` 空片段），每段含：

- `start` / `end`：Unicode 码点半开区间，`text` 为该段原文；
- `definite_ids` / `possible_ids`：覆盖该段的批注 ID，均**升序去重**；
- `status`：有 definite → `covered`；没有 definite 但有 possible →
  `uncertain`；两类都空 → `uncovered`。

**相邻片段仅在状态与两组 ID 完全相同时才合并**，因此首尾相接但批注不同的
两段仍是两个片段。片段从 0 无缝铺满到文本长度，相邻片段首尾相接，
`"".join(seg["text"] …)` 可还原全文。`exclusions` 按批注 ID 升序，逐条给出
`reason` 机器码与中文 `message`。相同快照下响应序列化后**字节一致**。
文档或版本不存在返回 404。

### 文档增量事件流

`GET /documents/{id}/events?limit=&cursor=` 把文档的六类**只追加**记录合并为
一条按时间稳定排序的事件流，适合做增量同步 / 审计订阅：

| `type` | 来源表 | 归并序号 `type_rank` | `payload` 稳定业务字段 |
|---|---|---|---|
| `version` | 版本 | 1 | `version_id`、`version_number`、`content` |
| `annotation` | 批注 | 2 | `annotation_id`、`body`、`selection`、`left_context`、`right_context`、`orig_start`、`orig_end`、`created_version_id` |
| `location` | 迁移位置 | 3 | `location_id`、`annotation_id`、`version_id`、`status`、`start`、`end`、`left_match`、`right_match`、`score`、`candidates`、`carried_from` |
| `confirmation` | 人工确认 | 4 | `confirmation_id`、`location_id`、`start`、`end`、`note` |
| `dependency` | 依赖 | 5 | `dependency_id`、`prerequisite_id`、`dependent_id` |
| `completion` | 完成记录 | 6 | `completion_id`、`annotation_id`、`note` |

排序键是 **`(created_at, type_rank, id)` 全序**：`created_at` 相同（同一写
事务内的行可能落在同一毫秒）时按类型序号定序，再按行 ID 定序，因此不重、
不漏、不乱序。每个事件统一形如
`{"type", "id", "created_at", "payload"}`。

> **载荷只反映落库时的状态**：例如迁移位置事件保留库中的
> `resolved` / `ambiguous` / `unresolved` 原始状态与顺延引用 `carried_from`，
> 不会因为后来追加了人工确认而变成 `confirmed`（确认本身是一条独立的
> `confirmation` 事件）；`carried_forward`、`version_number` 等查询时刻
> 派生值也不出现在载荷中。

**水位向量与读事务**：六张表各取一个最大行 ID，组成水位向量
`{versions, annotations, locations, confirmations, dependencies, completions}`。
首轮在**同一个 SQLite 读事务**（同一快照）中固定终点水位 `to`（起点 `from`
为全零），随后该轮所有分页只读取窗口 **`(from, to]`**。只追加 + 自增 ID
单调保证窗口内的行集此后永不变（旧窗口不丢行，新行 ID 必然更大）。

**两种游标**（均为 Base64URL 紧凑 JSON，自带版本字段 `v`）：

- `next_cursor`（`kind=page`，**续页**）：封装文档 ID、起止水位与**最后
  排序键**；下一页返回窗口内排序键严格大于它的事件。窗口与终点水位
  **始终沿用**首轮固定值——分页期间并发写入的行**留到下一轮**，绝不夹入
  当前轮。窗口未读尽时每页返回 `next_cursor`；
- `resume_cursor`（`kind=resume`，**续拉**）：**末页**（窗口读尽，包括空
  窗口）返回，携带已消费水位；用它再调用时，服务在新的读事务中重新固定
  终点水位，读取 `(from, 新 to]`。**没有新事件的空轮也会推进水位**
  （末页仍返回新的 `resume_cursor`），因此客户端可以反复轮询而不会重放
  旧事件，也不会漏掉其他文档写入推高全局行 ID 之后的新事件。

响应字段：

| 字段 | 含义 |
|---|---|
| `events` | 本页事件（至多 `limit` 条），按全序升序 |
| `event_count` / `has_more` | 本页条数 / 窗口内是否还有后续页 |
| `window.from` / `window.to` | 本轮窗口的起止水位向量（透明可审计） |
| `next_cursor` | 续页游标；窗口读尽时为 `null` |
| `resume_cursor` | 续拉游标；窗口未读尽时为 `null`（二者互斥） |

`limit` 限 **1～200**（默认 50，越界由参数校验直接返回 422）。游标出现
下列情况一律返回 **422**（`detail.error` 为机器码）：

| 情况 | `error` |
|---|---|
| Base64 / JSON 无法解码、字段缺失或多余、水位键不全、类型不对 | `bad_cursor_structure` |
| 游标版本号不支持 | `bad_cursor_version` |
| 负水位、`from > to`、最后排序键不在窗口内、水位超过当前数据库 | `bad_cursor_range` |
| 游标属于另一篇文档（不可跨文档使用） | `cross_document_cursor` |

文档不存在返回 404。接口只读，不会创建或修改任何记录。

### 批量偏移换算（codepoint / utf16 / utf8_byte）

`POST /documents/{id}/versions/{n}/coordinates` 只读地把目标版本全文上的一批
半开区间在三种偏移单位之间互转，专为两类客户端准备：

| 客户端 | 原生偏移单位 | 请求值 |
|---|---|---|
| 浏览器 JavaScript（`selectionStart`、`Range`、字符串索引） | UTF-16 码元 | `"utf16"` |
| Python / Rust 等按 Unicode 标量值遍历的工具 | Unicode 码点 | `"codepoint"` |
| C / Go / 字节级存储、`Content-Range` 类外部工具 | UTF-8 字节 | `"utf8_byte"` |

请求体：

| 字段 | 含义 |
|---|---|
| `input_unit` | 区间端点的输入单位：`codepoint` / `utf16` / `utf8_byte` |
| `intervals` | 半开区间 `[start, end)` 列表，**1～200 个**；允许空区间 `[x, x)` |

服务先按目标版本全文做**一次线性扫描构建边界映射**，之后每个端点 O(1)
查表，整批复杂度 **O(n + m)**（n 为全文码点数、m 为区间数）。响应：

- `lengths`：全文在三种单位下的总长度（便于客户端自检）；
- `results[i]`：按请求顺序逐项返回，带原下标 `index`，并给出
  `codepoint` / `utf16` / `utf8_byte` 三个 `{start, end}` 对应范围。

**整批校验**——任一区间有问题则整批 422（`detail.errors` 一次性列出全部
问题），每条错误带 `error` 机器码、`index`（条目下标）、`endpoint`
（`start` / `end` / `interval`）、`path`（如 `intervals[3].start`）与
中文 `message`，**绝不取整猜测**：

| 情况 | 错误码 |
|---|---|
| 端点超出 `0..该单位总长`（含负数） | `endpoint_out_of_range` |
| `start > end`（空区间 `[x, x)` 合法） | `start_after_end` |
| UTF-16 端点落在代理对的两个码元之间 | `inside_surrogate_pair` |
| UTF-8 端点落在多字节字符的字节中间 | `inside_multibyte_sequence` |

> **组合字符不算字符内部**：例如 `é`（`e` + U+0301 组合尖音符）是两个
> 独立码点，二者之间的端点是合法边界（按码点与 UTF-16 都能对齐），
> 只有代理对内部与 UTF-8 续字节位置才是非法端点。

文档或版本不存在返回 404；空批次、超过 200 个、非法单位等结构问题由
schema 直接返回 422。接口不写入任何数据；版本内容只追加、不可变，
同一版本下相同请求的响应序列化后**字节一致**。

### 可离线往返的审阅包

审阅包把「一批批注在某一时刻的审阅上下文」冻结成一个**自描述 JSON**，
审阅人离线逐项做出「**确认歧义**（confirm）/ **标记完成**（complete）」
决定后再回传；服务用创建时的摘要识别快照，并保证过期快照整包不写入。

**创建**（`POST /documents/{id}/review-packages`，请求体
`{"annotation_ids": [...], "note": ...}`）在**同一个 SQLite 读事务**
（同一快照）中冻结每个条目的：

| 冻结字段 | 内容 |
|---|---|
| `latest_location` | 该批注在文档最新版上的迁移记录：状态、范围、得分、**全部候选落点**、顺延引用与既有确认 |
| `completed` | 冻结时是否已有完成记录 |
| `prerequisite_ids` | 直接前置批注 ID |
| `roles` | `requested`（请求指定）/ `prerequisite`（自动收录的传递前置） |
| `prerequisite_states` | 全部传递前置的 `{completed, included}` 状态（含未收录的已完成前置，作为解锁依据） |

**收录规则**——未完成批注的**未完成传递前置项会自动收录**进同包，
因此每个被收录项都满足下列之一：**当时可确认**（最新位置是非顺延、
未确认的歧义记录，已完成批注也允许补确认）、**当时可完成**（无未完成
前置），或**能由同包前置项解锁**（其未完成前置全部在包内）。已完成且
不可确认的请求批注不收录，在响应 `excluded` 中逐条说明
（`already_completed`）；请求批注全部已完成时整条请求 422、不创建包。

响应给出 `package_id`（随机 32 位十六进制，与内容无关——同内容两次
创建是两个包）、`digest`（规范化 JSON 的 SHA-256，算法与迁移归档
相同）与完整 `package`。`package` + `digest` 可拷贝到任意离线环境，
摘要能用标准库独立复算：

```python
digest = hashlib.sha256(
    json.dumps(package, ensure_ascii=False, sort_keys=True,
               separators=(",", ":")).encode("utf-8")).hexdigest()
```

**离线填写**：审阅人只需在 `decisions` 中逐项给出决定，无需查库：

| action | 必填字段 | 含义 |
|---|---|---|
| `confirm` | `start` / `end`（+ 可选 `note`） | 从该条目冻结的候选落点中选定一个 |
| `complete` | 可选 `note` | 标记批注审阅完成 |

**回传**（`POST /review-packages/submit`，请求体
`{"package_id", "digest", "decisions"}`）分三关：

1. **离线结构 / 语义校验（422，不写库）**：只依据冻结包，一次性返回
   全部带定位的问题——重复决定（`decisions[i]`）、针对包外批注
   （`decision_not_in_package`）、非法 `action`、对不可确认项
   `confirm`（`not_confirmable`）、候选范围缺失 / 不属于冻结候选
   （`missing_candidate_range` / `confirmation_not_candidate`）、对冻结时
   已完成项 `complete`（`already_completed`），以及 `complete` 决定的
   未完成传递前置没有在同包决定中一并完成
   （`missing_prerequisite_decisions`，定位 `items[i]`）；
2. **同写事务冲突检测（409，整包不写入）**：在 BEGIN IMMEDIATE 取得
   写锁后逐项比对当前库与冻结快照，冲突一次性列在 `detail.conflicts`，
   每条带 `items[i]` / `annotation_id`：

   | 冲突 error | 触发条件 |
   |---|---|
   | `latest_location_changed` | 新增版本等使最新迁移记录改变（版本 / 行 ID / 状态 / 范围 / 顺延引用任一不同） |
   | `already_confirmed` | `confirm` 目标在冻结后已被他人人工确认 |
   | `completion_state_changed` | 条目完成状态与冻结时不一致（已完成 ↔ 未完成） |
   | `external_blocker` | 冻结后新增依赖边，使 `complete` 决定被包外未完成前置阻塞 |

3. **按依赖顺序应用（201）**：确认按批注 ID 升序追加，完成按
   **前置 → 后续的拓扑顺序**追加——决定列表的提交顺序无关，后项由
   同包前项即时解锁；应用阶段再次核对每条记录（他人持锁期间的变化），
   任一决定非法都**整体回滚**。响应返回追加的 `confirmations` /
   `completions` 与 `unlocked_annotation_ids`（应用前被阻塞、应用后可
   处理的批注，含包外同文档批注）。

**幂等**：审阅包行只追加；成功回传只补记一次 `applied_at` / 首次结果。
同一 `package_id` 再次提交（即使决定列表不同）返回 **200** 与首次结果
（`already_applied=true`），不产生任何新行；重放时携带的摘要与首次
不同则 409（`replay_mismatch`）。创建后被篡改的包会在回传第一关因
`package_digest_mismatch` 被 422 拒绝。无法识别的 `package_id` 返回
404。

### 版本范围字面子串检索

`POST /documents/{id}/search` 让审阅人在**同一文档的指定版本范围**
`[from_version, to_version]` 内检索一段文字，直接看到**命中位置与该处的
批注状态**。请求体：

| 字段 | 含义 |
|---|---|
| `query` | 要检索的文字，**1～256 个 Unicode 码点**；按字面子串匹配 |
| `from_version` / `to_version` | 版本号闭区间；`to_version` 缺省取首轮快照内最新版 |
| `context_radius` | 前后文片段单侧最大码点数，**0～80，缺省 16** |
| `cursor` | 上一页的 `next_cursor`（续页）或上一轮的 `resume_cursor`（续拉） |

**匹配规则**：逐版在全文上做按 **Unicode 码点**的字面匹配（Python 字符串
下标口径，emoji 等 BMP 外字符算 1 个码点，不做任何归一化 / 大小写折叠），
**保留重叠命中**——例如 `"aaaaa"` 查 `"aa"` 得到起点 `0,1,2,3` 四个命中。
命中按 **`(版本号, 起点, 终点)` 稳定排序**，每一页**至多 100** 个。

每个命中形如：

```json
{
  "version_number": 2,
  "start": 4, "end": 6,
  "context": {
    "before": "第二段：", "match": "心情", "after": "不错。第三段",
    "snippet": "第二段：心情不错。第三段",
    "snippet_start": 0, "snippet_end": 12,
    "match_start_in_snippet": 4, "match_end_in_snippet": 6,
    "truncated_left": false, "truncated_right": true
  },
  "annotations": [
    {"annotation_id": 7, "status": "confirmed", "start": 4, "end": 8}
  ]
}
```

`context` 的各片段只依赖**不可变的版本全文**与随游标冻结的
`context_radius`，相同快照 + 相同请求字节一致；
`match_start_in_snippet` 可直接在 `snippet` 上复原高亮。

**相交批注的生效状态**——列出该版本上与命中半开范围相交的批注（端点
相接不算相交），按批注 ID 升序：

| 状态 | 生效范围口径 |
|---|---|
| `confirmed` | 该版本迁移记录已被人工确认：**以确认范围为准**（确认范围优先于迁移记录自带范围与歧义候选） |
| `resolved` | 自动续接成功：迁移记录自带范围 |
| `ambiguous` | 未确认且非顺延：逐个检查全部候选，只返回**与命中相交的候选**（`candidates`），不挑选、不猜测 |
| `unresolved` / 歧义顺延占位 | 该版本上没有范围，**不会出现在 `annotations` 中** |

**快照冻结与分页**（与[文档增量事件流](#文档增量事件流)同一水位设计）：
首轮在**同一个 SQLite 读事务**（同一快照）中固定
`versions / annotations / locations / confirmations` 四张表属于本文档的
最大行 ID（响应 `snapshot.watermark` 透明返回）与版本上界。

- `next_cursor`（**续页**）：冻结版本上界、筛选条件、快照水位与最后
  排序键 `(版本号, 起点, 终点)`；续页始终沿用该快照——**分页期间并发
  新增的版本或批注 / 确认一律留到下一轮**，同轮各页的
  `total_in_snapshot` 恒定，不重不漏；
- `resume_cursor`（**续拉**）：一轮读尽（含没有命中的空轮）后返回；
  携带它调用即在新的读事务中重新固定快照与版本上界，新增版本 /
  批注 / 确认由此进入结果。首轮显式给出的 `to_version` 在续拉时保持
  不变；缺省最新版的检索，续拉时重新取当时最新版。

游标是 Base64URL 紧凑 JSON，载荷经服务启动时自动生成并存库的
**HMAC-SHA256 密钥**签名（密钥不随游标外泄，重启不变）。下列情况一律
**422**（`detail.error` 为机器码）：

| 情况 | `error` |
|---|---|
| Base64 / JSON / 信封结构损坏、字段缺失或多余、水位类型不对 | `bad_cursor_structure` |
| 游标版本号不支持 | `bad_cursor_version` |
| 签名不符（载荷被篡改） | `cursor_tampered` |
| 游标属于另一篇文档（不可跨文档使用） | `cross_document_cursor` |
| 携带的 `query` / `from_version` / `to_version` / `context_radius` 与游标冻结值不一致（`fields` 指出字段名；可选字段省略时视为沿用） | `filter_mismatch` |
| 冻结水位超过当前数据库（游标来自其他库）、游标内版本范围矛盾 | `bad_cursor_range` |
| 请求版本号区间非法或超出快照最新版 | `invalid_version_range` / `version_out_of_range` |

文档不存在返回 404；空 `query`、超长 `query`、越界 `context_radius`
由 schema 直接 422。接口只读、不写入任何业务数据（仅启动时一次性
初始化游标签名密钥）。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/documents` | 新建文档，`content` 即基准文本（第 1 版） |
| GET | `/documents/{id}` | 文档详情与版本列表 |
| POST | `/documents/{id}/annotations` | 在最新版上创建批注（校验锚点） |
| GET | `/documents/{id}/annotations` | 全部批注及最新生效状态 |
| GET | `/documents/{id}/annotations/{aid}` | 单条批注详情 |
| GET | `/documents/{id}/annotations/{aid}/history` | 该批注跨全部版本的范围/状态/匹配依据 |
| POST | `/documents/{id}/annotations/{aid}/confirmations` | 人工确认歧义（选定候选范围） |
| POST | `/documents/{id}/versions` | 提交新版全文，触发全部批注的续接迁移 |
| GET | `/documents/{id}/versions` | 版本列表 |
| GET | `/documents/{id}/versions/{n}` | 某版全文及该版全部迁移结果 |
| POST | `/documents/{id}/versions/{n}/coordinates` | 批量偏移换算：codepoint/utf16/utf8_byte 半开区间互转（整批校验代理对/多字节内部/越界，只读） |
| GET | `/documents/{id}/versions/{n}/coverage` | 文档批注覆盖图：全文划分为 covered/uncertain/uncovered 最大片段，含 definite/possible 批注 ID 与 exclusions（只读） |
| GET | `/documents/{id}/compare?from_version=&to_version=` | 任意两版间每条批注的范围、状态与匹配依据 |
| GET | `/documents/{id}/impact?from_version=&to_version=` | 批注改动影响分析：码点级编辑片段 + 起始版每条批注范围到目标版的投影与分类 |
| POST | `/documents/{id}/dependencies` | 批量提交审阅依赖关系（整批校验跨文档引用/自依赖/重复边/环，失败则全部不落库） |
| GET | `/documents/{id}/dependencies` | 文档下全部审阅依赖边 |
| GET | `/documents/{id}/review-plan` | 审阅计划：可处理/被阻塞/已完成分类 + 按拓扑层级的可并行批次 |
| POST | `/documents/{id}/annotations/{aid}/completions` | 标记批注完成（只追加、幂等，立即解锁后续批注） |
| GET | `/documents/{id}/completions` | 文档下全部完成记录（只追加） |
| GET | `/documents/{id}/archive` | 同一读事务导出规范 JSON 归档与 SHA-256 摘要 |
| POST | `/archives/verify` | 离线核验归档（不查库、不写入；缺行/串文档/篡改/摘要不符返回可定位错误） |
| POST | `/policies` | 提交发布门禁策略（语法整树校验，按名称只追加修订） |
| GET | `/policies` | 全部策略的最新修订（按名称升序） |
| GET | `/policies/{name}` | 某策略的全部修订（按修订号升序） |
| GET | `/policies/{name}/revisions/{revision}` | 取某策略的指定修订 |
| GET | `/documents/{id}/gate/evaluate?policy=&revision=&version=` | 同一读事务快照评估门禁，返回不短路的同形解释树 |
| POST | `/documents/{id}/release-candidates` | 创建发布候选：同一读事务固定版本/策略修订/门禁解释树/审阅状态/六类事件水位；门禁不过则 409 不落候选 |
| POST | `/release-candidates/{candidate_id}/confirm` | 确认发布：同写事务重读水位与门禁，任一六类事件新增或门禁不再通过即可定位 409 且不改候选；水位未变且门禁通过才追加一次 released；重复确认返回首次结果 |
| POST | `/documents/{id}/redact` | 批注驱动的脱敏副本：合并生效范围替换为占位，返回脱敏全文与源/结果码点区间映射（只读） |
| GET | `/documents/{id}/events?limit=&cursor=` | 文档增量事件流：六类只追加记录按 (created_at, type_rank, id) 稳定合并，水位向量 + 续页/续拉游标分页（只读） |
| POST | `/documents/{id}/review-packages` | 创建可离线往返的审阅包：同一读事务冻结最新迁移记录/候选/完成状态/全部传递前置，返回 package_id 与内容摘要 |
| POST | `/review-packages/submit` | 回传审阅包：离线校验决定(422)→同写事务冲突检测(409,整包不写入)→按依赖顺序追加确认与完成；重复回传返回首次结果 |
| POST | `/documents/{id}/search` | 版本范围字面子串检索：按 Unicode 码点字面匹配（保留重叠命中），逐命中返回半开范围、可复现前后文与相交批注的生效状态；每页至多 100 个，签名游标冻结版本上界与批注/位置快照（只读） |

## 完整示例

```bash
B=http://localhost:8000

# 1. 新建文档（基准文本为第 1 版）
curl -X POST $B/documents -H 'Content-Type: application/json' -d '{
  "title": "产品需求文档",
  "content": "第一段：天气很好。第二段：心情不错。第三段：天气很好。"
}'
# → {"document_id": 1, ...}

# 2. 创建批注：选区“心情不错”位于码点偏移 [13, 17)
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d '{
  "body": "建议润色",
  "selection": "心情不错",
  "left_context": "第二段：",
  "right_context": "。",
  "start": 13, "end": 17
}'
# → {"annotation_id": 1, "location": {"status": "resolved", ...}}

# 3. 提交第 2 版：选区平移，唯一落点 → resolved
curl -X POST $B/documents/1/versions -H 'Content-Type: application/json' -d '{
  "content": "新增开头。第一段：天气很好。第二段：心情不错。第三段：天气很好。"
}'
# → results[0]: {"status": "resolved", "start": 18, "end": 22, "score": 5, ...}

# 4. 第 3 版出现两处相同上下文 → ambiguous，返回全部候选范围
curl -X POST $B/documents/1/versions -H 'Content-Type: application/json' -d '{
  "content": "第二段：心情不错。第二段：心情不错。"
}'
# → results[0]: {"status": "ambiguous",
#     "candidates": [{"start":4,"end":8,"score":5,...},
#                    {"start":13,"end":17,"score":5,...}]}

# 5. 歧义未确认时提交第 4 版 → 顺延占位，不参与自动迁移
curl -X POST $B/documents/1/versions -H 'Content-Type: application/json' -d '{
  "content": "第二段：心情不错。"
}'
# → results[0]: {"status": "ambiguous", "carried_forward": true}

# 6. 批注改动影响分析（确认前调用）：起始版位置歧义未确认 → indeterminate，不猜测范围
curl "$B/documents/1/impact?from_version=3&to_version=2"
# → annotations[0]: {"classification":"indeterminate",
#     "reason":"起始版第 3 版中该批注位置歧义且未人工确认（存在多个并列最高分的候选范围），不提供投影范围",
#     "from":{"status":"ambiguous","start":null,"end":null,...},"to":null,"hits":[]}

# 7. 人工确认第 3 版的歧义：选定候选 (13, 17)
curl -X POST $B/documents/1/annotations/1/confirmations \
  -H 'Content-Type: application/json' -d '{
  "version_number": 3, "start": 13, "end": 17, "note": "取后一处"
}'
# → location.status = "confirmed"

# 8. 确认后再提交新版 → 恢复自动迁移
curl -X POST $B/documents/1/versions -H 'Content-Type: application/json' -d '{
  "content": "第二段：心情不错。"
}'
# → results[0]: {"status": "resolved", "start": 4, "end": 8}

# 9. 查询任意两版间每条批注的范围、状态与匹配依据
curl "$B/documents/1/compare?from_version=1&to_version=5"

# 10. 单条批注的完整跨版本历史
curl $B/documents/1/annotations/1/history

# 11. 批注改动影响分析：第 1 版 → 第 2 版（开头插入 5 码点，选区整体平移）
curl "$B/documents/1/impact?from_version=1&to_version=2"
# → segments: [{"index":0,"kind":"insert","from_start":0,"from_end":0,
#     "to_start":0,"to_end":5,"from_text":"","to_text":"新增开头。"}]
# → annotations[0]: {"classification":"shifted",
#     "from":{"status":"resolved","start":13,"end":17,...},
#     "to":{"start":18,"end":22},"hits":[]}
```

影响分析的分类全集示例（unchanged / modified / deleted / shifted 各一条）：

```bash
# 基准文本（第 1 版）：甲：目标一。乙：目标二。丙：目标三。丁：目标四。
# 依次对“目标一/二/三/四”创建 4 条批注后，提交第 2 版：
#   甲：目标一。乙：目X标二。丁：目标四。
curl "$B/documents/2/impact?from_version=1&to_version=2"
# → segments: [{"index":0,"kind":"insert","from_start":9,"from_end":9,
#                "to_start":9,"to_end":10,"from_text":"","to_text":"X"},
#              {"index":1,"kind":"delete","from_start":11,"from_end":17,
#                "to_start":12,"to_end":12,"from_text":"。丙：目标三","to_text":""}]
# → annotations:
#     目标一 [2,5)  → unchanged，投影 [2,5)，hits []
#     目标二 [8,11) → modified，投影 [8,12)，hits [插入 X]（选区内部插入）
#     目标三 [14,17)→ deleted， 投影 [12,12)（折叠到删除处），hits [删除"。丙：目标三"]
#     目标四 [20,23)→ shifted， 投影 [15,18)，hits []（仅被区间外编辑平移）
# → summary: {"unchanged":1,"shifted":1,"modified":1,"deleted":1,"indeterminate":0}
```

审阅依赖编排完整流程示例：

```bash
B=http://localhost:8000

# 1. 新建文档并创建 4 条批注（甲/乙/丙/丁，编号依次为 1..4）
curl -X POST $B/documents -H 'Content-Type: application/json' -d '{
  "title": "审阅示例", "content": "甲乙丙丁"
}'
# → {"document_id": 1, ...}
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d \
  '{"body": "审阅甲", "selection": "甲", "start": 0, "end": 1}'   # → annotation_id 1
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d \
  '{"body": "审阅乙", "selection": "乙", "start": 1, "end": 2}'   # → annotation_id 2
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d \
  '{"body": "审阅丙", "selection": "丙", "start": 2, "end": 3}'   # → annotation_id 3
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d \
  '{"body": "审阅丁", "selection": "丁", "start": 3, "end": 4}'   # → annotation_id 4

# 2. 批量提交依赖：甲→丙、乙→丙、丙→丁（一次请求整批写入）
curl -X POST $B/documents/1/dependencies -H 'Content-Type: application/json' -d '{
  "relations": [
    {"prerequisite_id": 1, "dependent_id": 3},
    {"prerequisite_id": 2, "dependent_id": 3},
    {"prerequisite_id": 3, "dependent_id": 4}
  ]
}'
# → 201 {"created_count": 3, "dependencies": [...]}

# 3. 再提交「丁→甲」会与已落库边构成环 → 422，整批不落库，
#    返回以环中最小编号开头的闭环路径
curl -X POST $B/documents/1/dependencies -H 'Content-Type: application/json' -d '{
  "relations": [{"prerequisite_id": 4, "dependent_id": 1}]
}'
# → 422 {"detail": {"message": "依赖关系存在环：1 → 3 → 4 → 1",
#                   "cycle": [1, 3, 4, 1]}}

# 4. 审阅计划：甲、乙可处理；丙被 [1,2] 阻塞；丁被 [1,2,3] 阻塞
curl $B/documents/1/review-plan
# → summary: {"total":4,"completed":0,"actionable":2,"blocked":2,"batch_count":3}
# → blocked[0]: {"annotation_id":3, "incomplete_prerequisites":[1,2], ...}
# → blocked[1]: {"annotation_id":4, "incomplete_prerequisites":[1,2,3], ...}
# → batches: [{"level":0,"annotation_ids":[1,2]},
#             {"level":1,"annotation_ids":[3]},
#             {"level":2,"annotation_ids":[4]}]

# 5. 被阻塞的丁不可完成 → 409
curl -X POST $B/documents/1/annotations/4/completions \
  -H 'Content-Type: application/json' -d '{}'
# → 409 {"detail": {"message": "批注 4 被阻塞：存在尚未完成的前置批注",
#                   "incomplete_prerequisites": [1, 2, 3]}}

# 6. 完成前置批注：完成甲不解锁丙（乙未完成）；完成乙立即解锁丙
curl -X POST $B/documents/1/annotations/1/completions \
  -H 'Content-Type: application/json' -d '{"note": "通过"}'
# → 201 {"already_completed": false, "unlocked_annotation_ids": []}
curl -X POST $B/documents/1/annotations/2/completions \
  -H 'Content-Type: application/json' -d '{}'
# → 201 {"already_completed": false, "unlocked_annotation_ids": [3]}

# 7. 重复提交完成记录幂等：返回 200 与原记录，不新增行
curl -X POST $B/documents/1/annotations/2/completions \
  -H 'Content-Type: application/json' -d '{}'
# → 200 {"already_completed": true, "unlocked_annotation_ids": []}

# 8. 查看全部依赖边与完成记录（均只追加）
curl $B/documents/1/dependencies
curl $B/documents/1/completions
```

迁移归档导出与离线核验示例（沿用前面含歧义、顺延与确认的文档 1）：

```bash
B=http://localhost:8000

# 1. 导出：同一读事务取得元数据 / 全部版本 / 批注 / 迁移记录 / 人工确认
#    归档保存到 archive.json，摘要另行保存（只提取 digest 字符串）
curl -s $B/documents/1/archive -o archive.json
python3 -c "import json; print(json.load(open('archive.json'))['digest'])" > digest.txt
cat archive.json | python3 -m json.tool | head -20
# → {
#       "document_id": 1,
#       "format": "annotation-archive/1",
#       "digest": "9f2c...（64 位十六进制 SHA-256）",
#       "archive": {"format": "annotation-archive/1", "document": {...},
#                   "versions": [...], "annotations": [...],
#                   "locations": [...], "confirmations": [...]},
#       ...
#   }

# 2. 核验：把归档与导出时另行保存的摘要一起提交（不查当前数据库）
python3 - <<'PY'
import json, urllib.request
body = json.dumps({
    "archive": json.load(open("archive.json"))["archive"],
    "digest": open("digest.txt").read().strip(),
}).encode("utf-8")
req = urllib.request.Request(
    "http://localhost:8000/archives/verify", data=body,
    headers={"Content-Type": "application/json"})
print(urllib.request.urlopen(req).read().decode())
# → {"valid":true,"format":"annotation-archive/1","document_id":1,
#    "digest":"9f2c...","counts":{"versions":5,"annotations":1,
#                                 "locations":5,"confirmations":1}}
PY

# 3. 完全离线（不经过本服务）自行复算摘要核对
python3 - <<'PY'
import hashlib, json
data = json.load(open("archive.json"))
canonical = json.dumps(data["archive"], ensure_ascii=False,
                       sort_keys=True, separators=(",", ":"))
actual = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
print("摘要一致" if actual == open("digest.txt").read().strip() else "摘要不符")
PY

# 4. 篡改一例：改掉某版全文后用原摘要核验 → 422，定位 digest_mismatch
python3 - <<'PY'
import json, urllib.error, urllib.request
data = json.load(open("archive.json"))
data["archive"]["versions"][0]["content"] += "被人改过"
req = urllib.request.Request(
    "http://localhost:8000/archives/verify",
    data=json.dumps({"archive": data["archive"],
                     "digest": open("digest.txt").read().strip()}).encode("utf-8"),
    headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(req)
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode())
# → 422 {"detail":{"valid":false,"errors":[
#        {"error":"digest_mismatch","message":"归档内容与摘要不符：……",
#         "path":"archive"}]}}
PY

# 5. 内容缺失一例：删掉一条迁移记录并用新摘要提交（模拟缺行的归档）
#    → 422，errors[0] 定位到具体批注与缺失版本
#    {"error":"missing_location",
#     "message":"批注 1 缺少第 4 版的迁移记录（该批注创建于第 1 版，末版为第 5 版）",
#     "path":"annotations[0]"}
```

文档发布门禁完整流程示例：

```bash
B=http://localhost:8000

# 1. 新建文档「甲乙丙」并对三个字各建一条批注（批注 1/2/3），提交第 2 版：
#    甲、乙唯一落点 → resolved；丙在新版中消失 → unresolved
curl -X POST $B/documents -H 'Content-Type: application/json' -d '{
  "title": "门禁示例", "content": "甲乙丙"
}'
i=0
for ch in 甲 乙 丙; do
  curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d "{
    \"body\": \"批注$ch\", \"selection\": \"$ch\",
    \"start\": $i, \"end\": $((i+1))
  }"
  i=$((i+1))
done
curl -X POST $B/documents/1/versions -H 'Content-Type: application/json' -d '{
  "content": "甲乙丁"
}'

# 2. 提交发布策略「放行条件」：至少 2 条 resolved，且没有 unresolved；
#    all / not / location_count 各节点恰有一个类型键，叶子字段固定
curl -X POST $B/policies -H 'Content-Type: application/json' -d '{
  "name": "放行条件",
  "strategy": {
    "all": [
      {"location_count": {"status": "resolved",   "op": "gte", "value": 2}},
      {"not": {"location_count": {"status": "unresolved", "op": "gte", "value": 1}}}
    ]
  }
}'
# → 201 {"name":"放行条件","revision":1,"strategy":{...}}

# 3. 在第 2 版上评估：有 1 条 unresolved → passed=false；
#    all 不短路：两个子节点的解释都在；叶子列出实际值/阈值/参与计数的批注 ID
curl "$B/documents/1/gate/evaluate?policy=%E6%94%BE%E8%A1%8C%E6%9D%A1%E4%BB%B6"
# → {"document_id":1,"policy_name":"放行条件","policy_revision":1,
#    "version_number":2,"passed":false,
#    "explanation":{"all":[
#      {"location_count":{"status":"resolved","op":"gte","value":2,
#                         "actual":2,"annotation_ids":[1,2]},"result":true},
#      {"not":{"location_count":{"status":"unresolved","op":"gte","value":1,
#                                "actual":1,"annotation_ids":[3]},"result":false},
#      ],"result":false}}

# 4. 策略只按名称追加修订：同名再提交得到 revision=2，旧修订原样保留
curl -X POST $B/policies -H 'Content-Type: application/json' -d '{
  "name": "放行条件",
  "strategy": {"all": [
    {"location_count": {"status": "resolved", "op": "gte", "value": 3}},
    {"review_count":   {"status": "blocked",  "op": "eq",  "value": 0}}
  ]}
}'
curl "$B/policies/%E6%94%BE%E8%A1%8C%E6%9D%A1%E4%BB%B6"
# → {"name":"放行条件","revisions":[{"revision":1,...},{"revision":2,...}]}

# 评估时可锁定历史修订与历史版本（默认均取最新）
curl "$B/documents/1/gate/evaluate?policy=%E6%94%BE%E8%A1%8C%E6%9D%A1%E4%BB%B6&revision=1&version=2"

# 5. 非法策略整条拒绝：422 一次性给出带 JSON 路径的全部错误，本次修订不落库。
#    下例同时犯了「空组合」与「review 叶子用了 location 状态」两个错误：
curl -X POST $B/policies -H 'Content-Type: application/json' -d '{
  "name": "坏策略",
  "strategy": {"all": [
    {"review_count": {"status": "resolved", "op": "eq", "value": 0}}
  ], "extra": 1}
}'
# → 422 {"detail":{"message":"门禁策略 '坏策略' 校验失败，本次修订未落库",
#                  "errors":[{"error":"unknown_field","path":"$",...}, ...]}}
# 其他典型拒绝：{"all":[]}（empty_combination，$.all）、
# {"not":[]}（invalid_not_child，$.not）、深度 > 8（depth_exceeded）、
# 节点 > 64（nodes_exceeded）、叶子 value 为 -1 / true / 1.5（invalid_value）。
```

批注驱动的脱敏副本示例：

```bash
B=http://localhost:8000

# 1. 新建文档并创建 3 条批注：姓名（1）、姓名中的一部分（2，与 1 重叠）、城市（3）
curl -X POST $B/documents -H 'Content-Type: application/json' -d '{
  "title": "脱敏示例",
  "content": "联系人：张三丰，城市：武当山。"
}'
# “张三丰”位于 [4,7)
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d '{
  "body": "姓名", "selection": "张三丰", "start": 4, "end": 7
}'
# “张三”位于 [4,6)，与批注 1 重叠
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d '{
  "body": "姓氏", "selection": "张三", "start": 4, "end": 6
}'
# “武当山”位于 [11,14)
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d '{
  "body": "城市", "selection": "武当山", "start": 11, "end": 14
}'

# 2. 生成脱敏副本：重叠的 [4,7)/[4,6) 合并为一个最大区间，只替换一次；
#    占位每次出现都在 placeholders 中列出覆盖它的批注 ID
curl -X POST $B/documents/1/redact -H 'Content-Type: application/json' -d '{
  "version_number": 1,
  "annotation_ids": [3, 1, 2],
  "placeholder": "【密】"
}'
# → 200
# {"document_id":1,"version_number":1,"placeholder":"【密】",
#  "annotation_ids":[1,2,3],"source_length":15,"redacted":"联系人：【密】，城市：【密】。",
#  "redacted_interval_count":2,
#  "fragments":[
#    {"index":0,"kind":"kept","source_start":0,"source_end":4,"result_start":0,"result_end":4},
#    {"index":1,"kind":"redacted","source_start":4,"source_end":7,"result_start":4,"result_end":7,
#     "annotation_ids":[1,2],"placeholder_index":0},
#    {"index":2,"kind":"kept","source_start":7,"source_end":11,"result_start":7,"result_end":11},
#    {"index":3,"kind":"redacted","source_start":11,"source_end":14,"result_start":11,"result_end":14,
#     "annotation_ids":[3],"placeholder_index":1},
#    {"index":4,"kind":"kept","source_start":14,"source_end":15,"result_start":14,"result_end":15}],
#  "placeholders":[
#    {"index":0,"placeholder":"【密】","annotation_ids":[1,2],"result_start":4,"result_end":7},
#    {"index":1,"placeholder":"【密】","annotation_ids":[3],"result_start":11,"result_end":14}]}
# 注：区间均为 Unicode 码点半开区间；fragments 中 kept / redacted 各自铺满源文本与结果文本，
#    未遮盖片段（kind=kept）同样可追溯；请求 ID 顺序不影响结果（响应中一律升序）。

# 3. 整批校验：重复 ID、批注不存在 / 跨文档、位置未就绪一次性全部定位返回
curl -X POST $B/documents/1/redact -H 'Content-Type: application/json' -d '{
  "version_number": 1,
  "annotation_ids": [1, 1, 999],
  "placeholder": "X"
}'
# → 422 {"detail":{"message":"脱敏请求校验失败，整批未处理（未生成脱敏副本）",
#                  "errors":[
#   {"error":"duplicate_annotation","path":"annotation_ids[1]","annotation_id":1,...},
#   {"error":"annotation_not_found","path":"annotation_ids[2]","annotation_id":999,...}]}}

# 4. ambiguous / unresolved（含歧义顺延占位）不猜测范围 → 422 not_effective；
#    人工确认后生效状态为 confirmed，即可用确认范围脱敏：
curl -X POST $B/documents/1/redact -H 'Content-Type: application/json' -d '{
  "version_number": 1, "annotation_ids": [1], "placeholder": ""
}'
# → 422（schema 拒绝空占位）

# 5. 只读：脱敏不改变任何数据；相同快照 + 相同请求多次调用结果字节一致。
```

文档批注覆盖图示例：

```bash
B=http://localhost:8000

# 1. 新建文档「目标一，目标二，目标三」（11 个码点），对三个目标各建一条
#    批注（编号 1/2/3）；提交第 2 版「目标一，目标二X，目标二X」（13 个码点）：
#    目标一唯一落点 → resolved；目标二两处分数相同 → ambiguous；目标三消失 → unresolved
curl -X POST $B/documents -H 'Content-Type: application/json' -d '{
  "title": "覆盖图示例",
  "content": "目标一，目标二，目标三"
}'
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d \
  '{"body":"批注一","selection":"目标一","start":0,"end":3}'
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d \
  '{"body":"批注二","selection":"目标二","start":4,"end":7}'
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d \
  '{"body":"批注三","selection":"目标三","start":8,"end":11}'
curl -X POST $B/documents/1/versions -H 'Content-Type: application/json' -d \
  '{"content":"目标一，目标二X，目标二X"}'
# 第 2 版上再创建一条批注（编号 4，选区为第一个 X [7,8)）
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d \
  '{"body":"批注四","selection":"X","start":7,"end":8}'

# 2. 第 1 版覆盖图：1/2/3 三段 covered，标点 uncovered；
#    批注 4 创建于第 2 版 → exclusions（location_missing），不覆盖任何位置
curl $B/documents/1/versions/1/coverage
# → {"document_id":1,"version_number":1,"source_length":11,
#    "summary":{"segment_count":5,"covered":3,"uncertain":0,"uncovered":2,"excluded":1},
#    "segments":[
#      {"index":0,"start":0,"end":3,"text":"目标一","definite_ids":[1],"possible_ids":[],"status":"covered"},
#      {"index":1,"start":3,"end":4,"text":"，","definite_ids":[],"possible_ids":[],"status":"uncovered"},
#      {"index":2,"start":4,"end":7,"text":"目标二","definite_ids":[2],"possible_ids":[],"status":"covered"},
#      {"index":3,"start":7,"end":8,"text":"，","definite_ids":[],"possible_ids":[],"status":"uncovered"},
#      {"index":4,"start":8,"end":11,"text":"目标三","definite_ids":[3],"possible_ids":[],"status":"covered"}],
#    "exclusions":[{"annotation_id":4,"reason":"location_missing",
#                   "message":"批注 4 在第 1 版没有迁移记录（该批注创建于更晚版本），不参与覆盖图"}]}

# 3. 第 2 版覆盖图：目标一 covered（1）、两个「目标二」uncertain（possible=2，
#    同一 ID 各出现一次）、第一个 X covered（4）、末尾 X uncovered；批注 3 排除
curl $B/documents/1/versions/2/coverage
# → {"version_number":2,"source_length":13,
#    "summary":{"segment_count":7,"covered":2,"uncertain":2,"uncovered":3,"excluded":1},
#    "segments":[
#      {"index":0,"start":0,"end":3,"text":"目标一","definite_ids":[1],"possible_ids":[],"status":"covered"},
#      {"index":1,"start":3,"end":4,"text":"，",    "definite_ids":[],"possible_ids":[],"status":"uncovered"},
#      {"index":2,"start":4,"end":7,"text":"目标二","definite_ids":[],"possible_ids":[2],"status":"uncertain"},
#      {"index":3,"start":7,"end":8,"text":"X",    "definite_ids":[4],"possible_ids":[],"status":"covered"},
#      {"index":4,"start":8,"end":9,"text":"，",    "definite_ids":[],"possible_ids":[],"status":"uncovered"},
#      {"index":5,"start":9,"end":12,"text":"目标二","definite_ids":[],"possible_ids":[2],"status":"uncertain"},
#      {"index":6,"start":12,"end":13,"text":"X",  "definite_ids":[],"possible_ids":[],"status":"uncovered"}],
#    "exclusions":[{"annotation_id":3,"reason":"unresolved",
#                   "message":"批注 3 在第 2 版未解析（选区在该版没有精确落点），不提供范围"}]}

# 4. 歧义未确认时先提交第 3 版 → 顺延占位：批注 2 在该版不参与迁移、
#    不提供任何范围，exclusions 原因为 ambiguous_carried（没有 uncertain 片段）
curl -X POST $B/documents/1/versions -H 'Content-Type: application/json' -d \
  '{"content":"全新的一版：目标一，没有别的目标。"}'
curl $B/documents/1/versions/3/coverage
# → 全文仅「目标一」一段 covered；没有 uncertain 片段
#   exclusions: [
#     {"annotation_id":2,"reason":"ambiguous_carried", ...},
#     {"annotation_id":3,"reason":"unresolved", ...},
#     {"annotation_id":4,"reason":"unresolved", ...}]

# 5. 人工确认批注 2 在第 2 版的候选 [4,7)：该版覆盖图中该段升为 covered
#    （definite_ids=[2]），未选中的 [9,12) 不再是 possible；
#    已顺延过的第 3 版不追溯赋予范围，仍为 ambiguous_carried
curl -X POST $B/documents/1/annotations/2/confirmations \
  -H 'Content-Type: application/json' -d \
  '{"version_number":2,"start":4,"end":7}'
curl $B/documents/1/versions/2/coverage
# → segments:
#     [0,3)  目标一  covered   definite=[1]
#     [3,4)  ，      uncovered
#     [4,7)  目标二  covered   definite=[2]
#     [7,8)  X       covered   definite=[4]（该批注不受批注 2 确认影响）
#     [8,13) ，目标二X uncovered（原 possible 的另一候选已被确认结果排除，
#                                 与相邻 uncovered 合并为最大片段）
#   exclusions 中不再出现批注 2

# 6. 只读且确定：segments 从 0 无缝铺满 source_length，"".join(text) 还原全文；
#    相同快照多次调用响应字节一致。
```

文档增量事件流示例（**首轮 → 分页 → 续拉**）：

```bash
B=http://localhost:8000

# 1. 新建文档并创建两条批注：此时已有 1 个 version、2 个 annotation、
#    2 个 location 事件，共 5 个
curl -X POST $B/documents -H 'Content-Type: application/json' -d '{
  "title": "事件流示例", "content": "甲。乙。"
}'
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d \
  '{"body":"批注甲","selection":"甲","start":0,"end":1}'
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d \
  '{"body":"批注乙","selection":"乙","start":2,"end":3}'

# 2. 【首轮】不带 cursor；limit=2 每页 2 条。
#    同一读事务固定终点水位向量 window.to，事件按
#    (created_at, type_rank, id) 全序返回；窗口未尽 → next_cursor
curl "$B/documents/1/events?limit=2"
# → {"document_id":1,"limit":2,"event_count":2,"has_more":true,
#    "events":[
#      {"type":"version","id":1,"created_at":"2026-09-27T07:40:40.542Z",
#       "payload":{"version_id":1,"version_number":1,"content":"甲。乙。"}},
#      {"type":"annotation","id":1,"created_at":"...",
#       "payload":{"annotation_id":1,"body":"批注甲","selection":"甲",
#                  "left_context":"","right_context":"",
#                  "orig_start":0,"orig_end":1,"created_version_id":1}}],
#    "window":{"from":{"versions":0,"annotations":0,...},
#              "to":  {"versions":1,"annotations":2,"locations":2,
#                      "confirmations":0,"dependencies":0,"completions":0}},
#    "next_cursor":"eyJ2IjoxLCJraW5kIjoicGFnZSIs...（续页游标）",
#    "resume_cursor":null}

# 3. 【分页】带 next_cursor 继续翻页：窗口与终点水位完全沿用首轮，
#    只按游标中的最后排序键向后取；即使此刻有并发写入也不会夹入本轮
curl "$B/documents/1/events?limit=2&cursor=eyJ2IjoxLCJraW5kIjoicGFnZSIs..."
# → events: [location#1, annotation#2]，仍返回 next_cursor（窗口还有 1 条）
curl "$B/documents/1/events?limit=2&cursor=eyJ2IjoxLCJraW5kIjoicGFnZSIs..."
# → events: [location#2]，has_more=false；末页不再给 next_cursor，
#    改给 resume_cursor（其 from 即首轮终点水位）：
# → {"event_count":1,"has_more":false,
#    "next_cursor":null,
#    "resume_cursor":"eyJ2IjoxLCJraW5kIjoicmVzdW1lIiwiZG9jIjox..."}

# 4. 期间产生新写入：提交第 2 版（1 个 version + 2 个 location）、
#    一条依赖、一条完成记录
curl -X POST $B/documents/1/versions -H 'Content-Type: application/json' -d \
  '{"content":"甲。乙。丙。"}'
curl -X POST $B/documents/1/dependencies -H 'Content-Type: application/json' -d '{
  "relations":[{"prerequisite_id":1,"dependent_id":2}]
}'
curl -X POST $B/documents/1/annotations/1/completions \
  -H 'Content-Type: application/json' -d '{"note":"完成"}'

# 5. 【续拉】用 resume_cursor 调用：在新的读事务固定新终点水位，
#    只返回 (已消费水位, 新水位] 内的增量——旧事件一条都不重放
curl "$B/documents/1/events?cursor=eyJ2IjoxLCJraW5kIjoicmVzdW1lIiwiZG9jIjox..."
# → {"event_count":5,"has_more":false,
#    "events":[
#      {"type":"version","id":2,...,"payload":{"version_number":2,"content":"甲。乙。丙。"}},
#      {"type":"location","id":3,...,"payload":{"status":"resolved",...,"carried_from":null}},
#      {"type":"location","id":4,...},
#      {"type":"dependency","id":1,...,"payload":{"prerequisite_id":1,"dependent_id":2}},
#      {"type":"completion","id":1,...,"payload":{"annotation_id":1,"note":"完成"}}],
#    "window":{"from":{"versions":1,"annotations":2,"locations":2,...},
#              "to":  {"versions":2,"annotations":2,"locations":4,
#                      "dependencies":1,"completions":1,...}},
#    "next_cursor":null,"resume_cursor":"eyJ2IjoxLCJraW5kIjoicmVzdW1l..."}

# 6. 【空轮】没有新事件时也可轮询：events 为空，但 resume_cursor 的水位
#    会推进到当前最大行 ID（其他文档的写入也会推高全表水位）；无任何写入
#    时新旧 resume_cursor 相同（幂等），可原样继续轮询
curl "$B/documents/1/events?cursor=eyJ2IjoxLCJraW5kIjoicmVzdW1l..."
# → {"event_count":0,"events":[],"next_cursor":null,
#    "resume_cursor":"...（水位已推进或保持不变）"}

# 7. 游标非法：无法解码 / 版本不支持 / 结构损坏 / 范围矛盾 / 跨文档 → 422
curl "$B/documents/1/events?cursor=garbage%21%21%21"
# → 422 {"detail":{"error":"bad_cursor_structure",
#                  "message":"游标无法解码：应为 Base64URL 编码的紧凑 JSON"}}
curl "$B/documents/2/events?cursor=eyJ2IjoxLCJraW5kIjoicmVzdW1l...文档1的游标"
# → 422 {"detail":{"error":"cross_document_cursor",
#                  "message":"游标属于文档 1，不能用于文档 2 的事件流（游标不可跨文档使用）"}}
```

批量偏移换算示例（含 **emoji、组合字符与中文**）：

```bash
B=http://localhost:8000

# 1. 新建文档，全文为：a 🎉 é（= e + U+0301 组合尖音符） 中
#    5 个码点；宽度（码点 / UTF-16 码元 / UTF-8 字节）：
#      a=1/1/1   🎉=1/2/4   e=1/1/1   ́=1/1/2   中=1/1/3
#    合计：5 码点 / 6 UTF-16 码元 / 11 UTF-8 字节
python3 - <<'PY'
import json, urllib.request
content = "a" + "\U0001F389" + "e" + "́" + "中"
body = json.dumps({"title": "偏移换算示例", "content": content}).encode("utf-8")
req = urllib.request.Request("http://localhost:8000/documents", data=body,
                             headers={"Content-Type": "application/json"})
print(urllib.request.urlopen(req).read().decode())
PY
# → {"document_id": 1, ...}

# 2. 浏览器按 UTF-16 码元提交选区：🎉 占码元 [1,3)，é 占 [3,5)
#    一次批量换算为码点 / UTF-8，results 顺序与原索引一致
curl -X POST $B/documents/1/versions/1/coordinates \
  -H 'Content-Type: application/json' -d '{
  "input_unit": "utf16",
  "intervals": [
    {"start": 1, "end": 3},
    {"start": 3, "end": 5},
    {"start": 0, "end": 6}
  ]
}'
# → 200
# {"document_id":1,"version_number":1,"input_unit":"utf16",
#  "lengths":{"codepoint":5,"utf16":6,"utf8_byte":11},
#  "results":[
#    {"index":0,"codepoint":{"start":1,"end":2},
#                 "utf16":{"start":1,"end":3},"utf8_byte":{"start":1,"end":5}},
#    {"index":1,"codepoint":{"start":2,"end":4},
#                 "utf16":{"start":3,"end":5},"utf8_byte":{"start":5,"end":8}},
#    {"index":2,"codepoint":{"start":0,"end":5},
#                 "utf16":{"start":0,"end":6},"utf8_byte":{"start":0,"end":11}}]}
# 即：🎉 对应码点 [1,2)；é（e+组合符）对应码点 [2,4)，可直接用于创建批注。

# 3. 外部工具按 UTF-8 字节提交：组合符两侧的边界合法（e=[5,6)，́=[6,8)）
curl -X POST $B/documents/1/versions/1/coordinates \
  -H 'Content-Type: application/json' -d '{
  "input_unit": "utf8_byte",
  "intervals": [{"start": 5, "end": 6}, {"start": 6, "end": 8},
                {"start": 8, "end": 11}]
}'
# → results: e→码点[2,3)，́→码点[3,4)，中→码点[4,5)（UTF-16 [5,6)）

# 4. 端点落在 UTF-16 代理对内部（🎉 的两个码元之间，偏移 2）→ 整批 422，
#    指出条目与端点，不取整
curl -X POST $B/documents/1/versions/1/coordinates \
  -H 'Content-Type: application/json' -d '{
  "input_unit": "utf16",
  "intervals": [{"start": 0, "end": 1}, {"start": 2, "end": 3}]
}'
# → 422 {"detail":{"message":"偏移换算请求校验失败，整批未换算（不对任何端点取整）",
#                  "input_unit":"utf16",
#                  "lengths":{"codepoint":5,"utf16":6,"utf8_byte":11},
#                  "errors":[{"error":"inside_surrogate_pair",
#                    "message":"条目 1 的 start=2 落在 UTF-16 代理对内部：码位 U+1F389……",
#                    "path":"intervals[1].start","index":1,"endpoint":"start",
#                    "unit":"utf16","value":2}]}}

# 5. 端点落在 UTF-8 多字节字符内部（「中」占字节 [8,11)，9 是续字节）→ 422
curl -X POST $B/documents/1/versions/1/coordinates \
  -H 'Content-Type: application/json' -d '{
  "input_unit": "utf8_byte",
  "intervals": [{"start": 9, "end": 11}]
}'
# → 422 errors[0]: {"error":"inside_multibyte_sequence",
#                   "path":"intervals[0].start", ... "码位 U+4E2D……字节区间 [8, 11)"}

# 6. 越界与 start>end 在同一批中一次性全部定位（允许空区间 [x,x)）
curl -X POST $B/documents/1/versions/1/coordinates \
  -H 'Content-Type: application/json' -d '{
  "input_unit": "codepoint",
  "intervals": [{"start": 0, "end": 99}, {"start": 4, "end": 2}]
}'
# → 422 errors:
#   [{"error":"endpoint_out_of_range","path":"intervals[0].end", ...},
#    {"error":"start_after_end",      "path":"intervals[1]", ...}]

# 7. 只读且确定：不新增批注 / 迁移记录；相同请求多次调用结果字节一致。
```

可离线往返的审阅包完整示例（**创建 → 离线保存与填写 → 回传 → 幂等重放**）：

```bash
B=http://localhost:8000

# 1. 新建文档「甲乙丙」并建 3 条批注（1/2/3），依赖 1→3、2→3；
#    再提交第 2 版「甲丙甲」：批注 1 两处并列 → ambiguous（候选 [0,1) 与 [2,3)）
curl -X POST $B/documents -H 'Content-Type: application/json' -d '{
  "title": "离线审阅包示例", "content": "甲乙丙"
}'
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d \
  '{"body":"批注甲","selection":"甲","start":0,"end":1}'
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d \
  '{"body":"批注乙","selection":"乙","start":1,"end":2}'
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d \
  '{"body":"批注丙","selection":"丙","start":2,"end":3}'
curl -X POST $B/documents/1/dependencies -H 'Content-Type: application/json' -d '{
  "relations": [
    {"prerequisite_id": 1, "dependent_id": 3},
    {"prerequisite_id": 2, "dependent_id": 3}
  ]
}'
curl -X POST $B/documents/1/versions -H 'Content-Type: application/json' -d \
  '{"content":"甲丙甲"}'

# 2. 【创建】只请求批注 3：未完成的传递前置 1、2 被自动收录进同包。
#    同一读事务冻结三条批注的最新迁移记录 / 候选 / 完成状态 / 前置状态，
#    返回 package_id（32 位十六进制）与内容摘要 digest；把整个响应存到
#    package.json 后即可带离网络离线填写
curl -X POST $B/documents/1/review-packages -H 'Content-Type: application/json' -d '{
  "annotation_ids": [3], "note": "离线评审一轮"
}' -o package.json
python3 -c "import json; d=json.load(open('package.json')); print(d['package_id']); print(d['digest'])"
# → b83bab5613fa431ca162120f9d7d9685
# → <64 位十六进制 SHA-256>
python3 -c "import json; d=json.load(open('package.json'))['package']; \
print([(i['annotation_id'], i['roles'], i['latest_location']['status']) for i in d['items']])"
# → [(1, ['prerequisite'], 'ambiguous'),
#    (2, ['prerequisite'], 'unresolved'),
#    (3, ['requested'],   'resolved')]
python3 -c "import json; print(json.load(open('package.json'))['package']['prerequisite_states'])"
# → {'1': {'completed': False, 'included': True},
#    '2': {'completed': False, 'included': True}}

# 3. 【离线】摘要可完全不经过本服务复算（与归档同一套规范化 JSON 算法）
python3 - <<'PY'
import hashlib, json
d = json.load(open("package.json"))
canon = json.dumps(d["package"], ensure_ascii=False,
                   sort_keys=True, separators=(",", ":"))
assert hashlib.sha256(canon.encode("utf-8")).hexdigest() == d["digest"]
print("离线复算摘要一致")
PY

# 4. 【离线填写】审阅人只决定每条批注 confirm 还是 complete（决定顺序无关）。
#    本例：1、2 完成后解锁 3，故三条都给 complete；歧义位置稍后用第二个包确认。
cat > decisions.json <<'JSON'
{
  "package_id": "b83bab5613fa431ca162120f9d7d9685",
  "digest": "……把 package.json 中的 digest 原样填入……",
  "decisions": [
    {"annotation_id": 3, "action": "complete", "note": "丙：终审"},
    {"annotation_id": 1, "action": "complete", "note": "甲：通过（歧义位置另开确认包）"},
    {"annotation_id": 2, "action": "complete", "note": "乙：通过"}
  ]
}
JSON

# 5. 离线自校验：若只 complete 3 而漏掉前置 1、2，回传第一关 422
#    （missing_prerequisite_decisions，定位 items[2]，不写任何数据）
curl -X POST $B/review-packages/submit -H 'Content-Type: application/json' -d '{
  "package_id": "b83bab5613fa431ca162120f9d7d9685",
  "digest": "……同上……",
  "decisions": [{"annotation_id": 3, "action": "complete"}]
}'
# → 422 {"detail":{"message":"审阅包回传决定校验失败，整包未处理……",
#    "errors":[{"error":"missing_prerequisite_decisions",
#      "message":"批注 3 的 complete 决定无法在同包内解锁：传递前置 [1, 2] 既未在冻结前完成，也未在本包决定中一并完成",
#      "path":"items[2]","annotation_id":3}]}}

# 6. 【回传】决定齐全：同一写事务内按依赖拓扑顺序 1→2→3 追加完成
#    （与提交顺序无关），任一非法则整批回滚
curl -X POST $B/review-packages/submit -H 'Content-Type: application/json' \
  --data @decisions.json
# → 201
# {"package_id":"b83bab...","applied_at":"2026-09-30T21:09:41.756Z",
#  "confirmation_count":0,"completion_count":3,
#  "completions":[{"annotation_id":1,"completion_id":1,"note":"甲：通过……"},
#                 {"annotation_id":2,"completion_id":2,"note":"乙：通过"},
#                 {"annotation_id":3,"completion_id":3,"note":"丙：终审"}],
#  "unlocked_annotation_ids":[],"already_applied":false}

# 7. 【幂等重放】同一 package_id 再提交（哪怕决定列表为空 / 不同）：
#    原样返回首次结果，200 + already_applied=true，不新增任何行
curl -X POST $B/review-packages/submit -H 'Content-Type: application/json' -d '{
  "package_id": "b83bab5613fa431ca162120f9d7d9685",
  "digest": "……同上……", "decisions": []
}'
# → 200 {"completion_count":3, ..., "already_applied":true}

# 8. 【确认包】批注 1 已完成，但它在第 2 版的歧义仍可确认 → 单独再开一个包；
#    从冻结候选中选定 [0,1)。注意：confirm 不等于 complete，它不会解锁后续，
#    解锁完成依赖链必须用 complete（这也是第 5 步漏前置会被拒绝的原因）
curl -X POST $B/documents/1/review-packages -H 'Content-Type: application/json' -d \
  '{"annotation_ids":[1]}'
# → items[0]: {"annotation_id":1,"completed":true,
#              "latest_location":{"status":"ambiguous",
#                "candidates":[{"start":0,"end":1,...},{"start":2,"end":3,...}]}}
curl -X POST $B/review-packages/submit -H 'Content-Type: application/json' -d '{
  "package_id": "……确认包的 package_id……", "digest": "……确认包摘要……",
  "decisions": [
    {"annotation_id": 1, "action": "confirm", "start": 0, "end": 1, "note": "取首处"}
  ]
}'
# → 201 {"confirmation_count":1,
#        "confirmations":[{"annotation_id":1,"location_id":4,"start":0,"end":1}],
#        "completion_count":0,"already_applied":false}

# 9. 【冲突整包不写入】冻结之后若有人提交了新版本（最新迁移记录改变）、
#    抢先确认了目标，或抢先完成了批注，回传 409 并逐项定位，不写入：
#    冻结批注 4 → 外部提交第 2 版 → 再回传 complete
curl -X POST $B/review-packages/submit -H 'Content-Type: application/json' -d '{
  "package_id": "……过期包的 package_id……", "digest": "……过期包摘要……",
  "decisions": [{"annotation_id": 4, "action": "complete"}]
}'
# → 409 {"detail":{"message":"审阅包冻结后目标状态已变化，整包未处理……",
#    "conflicts":[{"error":"latest_location_changed",
#      "message":"批注 4 的最新位置已变化：最新版本由第 1 版变为第 2 版（新增版本改变了最新位置）；范围由 [0, 1) 变为 [1, 2)；迁移记录行由 7 变为 8，……",
#      "path":"items[0]","annotation_id":4}]}}
# 其他冲突 error：already_confirmed（冻结后被他人确认）、
# completion_state_changed（完成状态变化）、external_blocker（新增依赖引入包外未完成前置）
```

### 发布候选：创建、确认与冲突

```bash
B=http://localhost:8000

# 前置：文档 1（第 1 版）上有 3 条批注，均已 resolved。提交一把门禁策略：
# 无 unresolved 且无 ambiguous（v1 上三条 resolved → 通过）
curl -X POST $B/policies -H 'Content-Type: application/json' -d '{
  "name": "发布条件",
  "strategy": {"all": [
    {"location_count": {"status": "unresolved", "op": "eq", "value": 0}},
    {"location_count": {"status": "ambiguous",  "op": "eq", "value": 0}}
  ]}
}'
# → 201 {"name":"发布条件","revision":1,...}

# 1. 【创建候选】同一读事务固定版本、策略修订、门禁解释树、审阅状态与
#    该文档六类事件水位；通过后生成不可变 candidate_id 与规范摘要
curl -X POST $B/documents/1/release-candidates -H 'Content-Type: application/json' -d '{
  "policy": "发布条件"
}'
# → 201
# {"candidate_id":"7b1f6a3e9c2d4e0f8a5b1c6d0e2f9a34",
#  "document_id":1,"format":"document-release-candidate/1",
#  "digest":"9f2c…（64 位十六进制 SHA-256）",
#  "status":"pending","released_at":null,
#  "policy_name":"发布条件","policy_revision":1,"version_number":1,
#  "watermark":{"versions":1,"annotations":3,"locations":3,
#               "confirmations":0,"dependencies":0,"completions":0},
#  "frozen":{"version":{...},"policy":{...},
#            "gate":{"passed":true,"explanation":{"all":[...],"result":true}},
#            "review_state":{"actionable":[1,2,3],"blocked":[],"completed":[]},...}}

# 也可锁定历史修订 / 指定版本：
#   {"policy":"发布条件","policy_revision":1,"version_number":1,"note":"RC1"}

# 2. 【初始门禁未通过 → 拒绝创建且不落候选】
#    换一把「3 条全部 completed 才放行」的策略，当前 completed=0
curl -X POST $B/policies -H 'Content-Type: application/json' -d '{
  "name":"全部完成",
  "strategy":{"review_count":{"status":"completed","op":"eq","value":3}}
}'
curl -X POST $B/documents/1/release-candidates -H 'Content-Type: application/json' -d '{
  "policy": "全部完成"
}'
# → 409 {"detail":{"message":"门禁未通过……发布候选未创建（不落候选）",
#    "passed":false,"policy_revision":1,"version_number":1,
#    "explanation":{"review_count":{"status":"completed","op":"eq","value":3,
#                                  "actual":0,"annotation_ids":[]},
#                   "result":false},
#    "errors":[{"error":"gate_not_passed","message":"门禁未通过……"}]}}
# （该请求不产生 candidate_id，release_candidates 表也没有新行）

# 3. 【确认发布】水位未变且门禁仍通过 → 同一写事务追加一次 released
curl -X POST $B/release-candidates/7b1f6a3e9c2d4e0f8a5b1c6d0e2f9a34/confirm
# → 201
# {"candidate_id":"7b1f6a3e…","document_id":1,"outcome":"released",
#  "released_at":"2026-10-02T08:00:00.000Z",
#  "policy_name":"发布条件","policy_revision":1,"version_number":1,
#  "digest":"9f2c…","already_released":false}

# 4. 【重复确认 → 原样返回首次结果，不重复写入】
curl -X POST $B/release-candidates/7b1f6a3e9c2d4e0f8a5b1c6d0e2f9a34/confirm
# → 200 {"outcome":"released","released_at":"2026-10-02T08:00:00.000Z",
#        "already_released":true, ...（与首次逐字段一致）}

# 5. 【冲突：候选创建后该文档出现六类事件之一】
#    再开一个候选 → 有人提交了新版本 → 确认时 409，冲突可定位、候选状态不变
CID=$(curl -s -X POST $B/documents/1/release-candidates -H 'Content-Type: application/json' \
  -d '{"policy":"发布条件"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["candidate_id"])')
curl -X POST $B/documents/1/versions -H 'Content-Type: application/json' \
  -d '{"content":"……第 2 版全文……"}'
curl -X POST $B/release-candidates/$CID/confirm
# → 409 {"detail":{"message":"发布候选创建后该文档状态已变化或门禁不再通过……",
#    "candidate_id":"…","conflicts":[
#      {"error":"watermark_changed","path":"watermark.versions",
#       "event_type":"version","frozen_watermark":1,"current_watermark":2,...},
#      {"error":"watermark_changed","path":"watermark.locations",
#       "event_type":"location","frozen_watermark":3,"current_watermark":6,...}
#    ]}}
# 再确认仍是同样的 409；候选保持 pending、不写 released 结果。
# 其余四类 path：watermark.annotations / watermark.confirmations /
#   watermark.dependencies / watermark.completions（新批注 / 新确认 /
#   新依赖 / 新完成记录）；其他文档的写入不会触发本候选冲突。
# 若六类水位未变但门禁已不通过（例如直接改库使某条迁移变 unresolved），
# 则返回 path="gate" 的 gate_not_passed 冲突并附当前解释树。
```

### 版本范围检索：重叠命中、emoji 与并发写入

```bash
# 准备一篇含重叠词与 emoji 的文档，并在其上挂一条 resolved 批注、
# 一条后续版本歧义（再人工确认）的批注
curl -X POST $B/documents -H 'Content-Type: application/json' -d '{
  "title": "发布说明",
  "content": "哈哈😀哈哈，状态稳定，稳定。"
}'
# → {"document_id": 1}
# 第 1 版码点序列：0 哈 1 哈 2 😀 3 哈 4 哈 5 ， 6 状 7 态 8 稳 9 定 10 ， 11 稳 12 定 13 。
curl -X POST $B/documents/1/annotations -H 'Content-Type: application/json' -d '{
  "body": "复核稳定性", "selection": "稳定",
  "left_context": "状态", "right_context": "，",
  "start": 8, "end": 10
}'
# → {"annotation_id": 1, "location": {"status": "resolved", "start": 8, "end": 10}}

# 1. 【重叠命中】“哈哈”在第 1 版有两处重叠起点 [0,2) 与 [3,5)，
#    emoji 占一个码点把它们隔开；命中按 (版本号, 起点, 终点) 排序
curl -X POST $B/documents/1/search -H 'Content-Type: application/json' -d '{
  "query": "哈哈", "from_version": 1
}'
# → {"hit_count": 2, "total_in_snapshot": 2, "has_more": false,
#    "hits": [
#      {"version_number": 1, "start": 0, "end": 2,
#       "context": {"before": "", "match": "哈哈", "after": "😀哈哈，状态稳定，稳定。",
#         "snippet": "哈哈😀哈哈，状态稳定，稳定。",
#         "snippet_start": 0, "snippet_end": 14,
#         "match_start_in_snippet": 0, "match_end_in_snippet": 2,
#         "truncated_left": false, "truncated_right": false},
#       "annotations": []},
#      {"version_number": 1, "start": 3, "end": 5,
#       "context": {"match": "哈哈", ...}, "annotations": []}]}

# 2. 【emoji 按码点计】检索 emoji 本身：偏移不被多字节字符错位
curl -X POST $B/documents/1/search -H 'Content-Type: application/json' -d '{
  "query": "😀", "from_version": 1, "context_radius": 2
}'
# → {"hits": [{"version_number": 1, "start": 2, "end": 3,
#    "context": {"before": "哈哈", "match": "😀", "after": "哈哈",
#      "snippet": "哈哈😀哈哈", "match_start_in_snippet": 2, ...},
#    "annotations": []}]}

# 3. 【命中相交批注状态】“稳定”在第 1 版出现两次；批注 1 的生效范围
#    [8,10) 与第一个命中相交（第二个命中 [11,13) 不相交），状态 resolved
curl -X POST $B/documents/1/search -H 'Content-Type: application/json' -d '{
  "query": "稳定", "from_version": 1
}'
# → {"hits": [
#    {"version_number": 1, "start": 8, "end": 10,
#     "annotations": [{"annotation_id": 1, "status": "resolved",
#                      "start": 8, "end": 10}]},
#    {"version_number": 1, "start": 11, "end": 13, "annotations": []}]}

# 4. 【确认范围优先于迁移候选】第 2 版制造两处相同落点 → ambiguous，
#    人工确认其一后，检索命中以确认范围标 confirmed（另一候选不再报告）
curl -X POST $B/documents/1/versions -H 'Content-Type: application/json' -d '{
  "content": "稳定，稳定。"
}'
# → results[0].status = "ambiguous"
#   candidates = [{"start":0,"end":2,...}, {"start":3,"end":5,...}]
curl -X POST $B/documents/1/annotations/1/confirmations \
  -H 'Content-Type: application/json' -d '{"version_number": 2, "start": 3, "end": 5}'
curl -X POST $B/documents/1/search -H 'Content-Type: application/json' -d '{
  "query": "稳定", "from_version": 2, "to_version": 2
}'
# → {"hits": [
#    {"version_number": 2, "start": 0, "end": 2, "annotations": []},
#    {"version_number": 2, "start": 3, "end": 5,
#     "annotations": [{"annotation_id": 1, "status": "confirmed",
#                      "start": 3, "end": 5}]}]}

# 5. 【并发写入留到下一轮】命中超过 100 时按 next_cursor 续页；
#    续页严格沿用首轮冻结的版本上界与批注快照（示例用大量重叠命中）：
curl -X POST $B/documents -H 'Content-Type: application/json' \
  -d '{"title": "长文", "content": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
#                                                                              （71 个 a → 70 个 “aa” 重叠命中）
curl -X POST $B/documents/2/versions -H 'Content-Type: application/json' \
  -d '{"content": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
PAGE1=$(curl -s -X POST $B/documents/2/search -H 'Content-Type: application/json' \
  -d '{"query": "aa", "from_version": 1}')
echo "$PAGE1" | python3 -c 'import sys,json;j=json.load(sys.stdin);print(j["hit_count"], j["total_in_snapshot"], j["snapshot"]["latest_version"])'
# → 100 140 2
CUR=$(echo "$PAGE1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["next_cursor"])')

# 分页期间另一个客户端并发提交了第 3 版（又是 70 个命中）
curl -s -X POST $B/documents/2/versions -H 'Content-Type: application/json' \
  -d '{"content": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}' >/dev/null

# 续页仍返回冻结快照的剩余 40 条，不含第 3 版，水位与首轮一致
curl -s -X POST $B/documents/2/search -H 'Content-Type: application/json' \
  -d "{\"query\": \"aa\", \"from_version\": 1, \"cursor\": \"$CUR\"}" \
  | python3 -c 'import sys,json;j=json.load(sys.stdin);print(j["hit_count"], j["total_in_snapshot"], j["snapshot"]["latest_version"], bool(j["resume_cursor"]))'
# → 40 140 2 True
RESUME=$(curl -s -X POST $B/documents/2/search -H 'Content-Type: application/json' \
  -d "{\"query\": \"aa\", \"from_version\": 1, \"cursor\": \"$CUR\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["resume_cursor"])')

# 用 resume_cursor 续拉开新快照：第 3 版进入结果（共 210 条），
# 再沿 next_cursor 取尽新快照即可；没有新数据的空轮也返回新的 resume_cursor
curl -s -X POST $B/documents/2/search -H 'Content-Type: application/json' \
  -d "{\"query\": \"aa\", \"from_version\": 1, \"cursor\": \"$RESUME\"}" \
  | python3 -c 'import sys,json;j=json.load(sys.stdin);print(j["total_in_snapshot"], j["snapshot"]["latest_version"], j["has_more"])'
# → 210 3 True

# 6. 【游标安全】篡改载荷、跨文档使用或改动筛选条件 → 422
curl -s -X POST $B/documents/2/search -H 'Content-Type: application/json' \
  -d "{\"query\": \"a\", \"from_version\": 1, \"cursor\": \"$CUR\"}"
# → 422 {"detail": {"error": "filter_mismatch",
#    "message": "请求的筛选条件与游标冻结值不一致：query……", "fields": ["query"]}}
```

## 项目结构

```
app/
  main.py       # FastAPI 路由与迁移流程（含改动影响分析、审阅依赖编排、归档导出/核验、发布门禁、脱敏、覆盖图、增量事件、批量偏移换算与离线审阅包接口）
  matching.py   # 锚点匹配核心（枚举落点、上下文打分、排序判定）
  diffing.py    # 两版全文的码点级编辑片段、范围投影与分类（纯函数）
  redacting.py  # 脱敏副本：生效范围合并为最大区间、按起点替换与源/结果码点区间映射（纯函数）
  coverage.py   # 批注覆盖图：候选合并与扫描线切片为 covered/uncertain/uncovered 最大片段（纯函数）
  review.py     # 审阅依赖图算法：环检测、传递前置、拓扑分层（纯函数）
  gate.py       # 发布门禁策略树：语法校验（深度/节点数/枚举域）与快照评估、同形解释树（纯函数）
  events.py     # 文档增量事件流：六表水位向量、(created_at, type_rank, id) 稳定合并、续页/续拉游标编解码
  coordinates.py  # 批量偏移换算：全文一次扫描构建 codepoint/utf16/utf8_byte 边界映射，端点 O(1) 查表与整批校验（纯函数）
  archive.py    # 迁移归档：同一读事务导出、规范化 JSON + SHA-256、离线一致性核验（不查库）
  review_packages.py # 离线审阅包：同读事务冻结快照/自动收录传递前置、离线决定整批校验、回传冲突检测与同写事务按依赖顺序应用、首次结果幂等重放
  release_candidates.py # 发布候选：同读事务固定版本/策略修订/门禁解释树/审阅状态/六类事件水位，初始门禁不过不落候选；确认时同写事务重读水位与门禁，冲突可定位且不改候选，水位未变且门禁通过才追加一次 released、重复确认幂等重放
  search.py     # 版本范围字面子串检索：码点字面匹配（保留重叠）、前后文片段、相交批注生效状态（确认范围优先）、四表快照水位与 HMAC 签名续页/续拉游标（纯函数 + 游标编解码）
  db.py         # SQLite 连接（WAL/checkpoint）、读写事务、init_db 启动入口
  migrations.py # 版本化可审计迁移：非破坏性预检、单写事务顺序迁移、schema_migrations 只追加审计、迁移后结构校验
  schemas.py    # 请求体模型（含门禁策略、归档结构、核验请求、批量偏移换算与审阅包模型）
tests/
  test_matching.py  # 匹配算法单元测试
  test_diffing.py   # 编辑片段/投影/分类单元测试
  test_api.py       # 端到端流程测试（迁移/歧义/顺延/确认/对比）
  test_migrations.py # 版本化迁移测试（新库/旧库/缺表缺索引/重复拒绝/单步回滚重试/校验回滚/审计只追加/服务门禁）
  test_impact.py    # 改动影响分析接口端到端测试
  test_review.py    # 审阅依赖编排测试（依赖校验/环检测/审阅计划/完成记录）
  test_gate.py      # 发布门禁测试（策略语法拒绝/修订只追加/同事务快照评估/不短路解释树/稳定性）
  test_redacting.py # 批注驱动脱敏测试（范围合并/首尾相接/码点映射/整批可定位错误/字节稳定性/只读）
  test_coverage.py  # 批注覆盖图测试（候选合并/扫描线最大片段/definite优先/空区间/无缝铺满/顺延与确认/字节稳定性/只读）
  test_events.py    # 增量事件流测试（首轮六类合并/续页分页不漏不重/并发新增留到下一轮/续拉增量/空轮推进/游标422）
  test_coordinates.py # 批量偏移换算测试（emoji/组合字符/中文边界、代理对与多字节内部422、越界/start>end、200上限、只读与字节稳定）
  test_archive.py   # 归档导出与离线核验测试（摘要篡改/缺行/串文档/顺延/确认非法）
  test_review_packages.py # 离线审阅包测试（同事务冻结/自动收录前置/离线整批校验/四类冲突不写入/依赖顺序应用/回滚/幂等重放/并发）
  test_release_candidates.py # 发布候选测试（同读事务冻结/初始门禁不过不落候选/六类水位与门禁复评冲突可定位/冲突不改候选/released 只写一次/幂等重放/跨文档隔离）
  test_search.py # 版本范围检索测试（重叠命中/emoji 码点偏移/四类相交状态/确认优先/100 分页稳定/并发版本与确认留到下一轮/续拉开新快照/游标篡改跨文档筛选不一致 422）
Dockerfile
docker-compose.yml
requirements.txt        # 运行依赖
requirements-dev.txt    # 测试依赖
```
