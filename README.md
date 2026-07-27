# Medium 数据查询服务运行手册

## 1. 服务定位

`medium` 是面向 Bit-Crew 的自然语言数据查询服务。Bit-Crew 发送原始问题，服务在内部完成查询规划、渐进元数据加载、LLM 生成 SQLite SQL、语义审核、安全校验、只读执行和审计。

```text
BitAgent
-> Bit-Crew HTTP 节点
-> Medium POST /api/v1/query-energy-data
-> LLM 基于全部增强表卡生成 QueryPlan
-> 按选表加载已发布字段 DDL、完整字段语义、业务规则和验证示例
-> LLM 生成 SQL
-> SQL 白名单校验
   -> 可修正的字段、表覆盖或只读查询结构问题：反馈模型修复一次，再次校验
   -> 危险操作、多语句、注释：直接拒绝，不回转
-> LLM SQL 语义审核（通过 / 重写一次 / 澄清 / 不支持）
-> 重写 SQL 再次安全校验与语义审核
-> SQLite 只读查询
-> Bit-Crew 解析结果并生成用户回答
```

服务对外不会返回 SQL、DDL、数据库路径、模型提示词或内部异常堆栈。

> 注意：`min` 目录中的旧验证服务使用 `operation + metric + filters` 请求体和固定 SQL 模板；它与本服务是两个独立接口。不要把旧接口的请求体发送到本服务。

## 2. 目录与数据

| 路径 | 作用 |
| --- | --- |
| `app/` | FastAPI 服务、LLM 客户端、SQL 校验和 SQLite 执行器 |
| `config/catalog.json` | 已发布表、字段别名、敏感字段排除和来源信息 |
| `config/table_cards.json` | 全量增强表卡，包含业务覆盖范围、适合的问题类型、关键字段与数据限制，供规划模型选表 |
| `config/ddl_registry.json` | 发布表到源 DDL 文件、SHA-256 的受控映射 |
| `config/query_knowledge.json` | 前置路由、业务口径、计算规则的发布状态、待确认辅助规则及后置回答模板 |
| `config/validation_cases.json` | 客户验证题 Q1-Q40 的意图 ID、支持状态、范围、必选表和数据缺口；仅 `routing_enabled=true` 的精确命中题作为前置路由规则 |
| `config/administrative_regions.json` | 张家口完整行政区名称、各表地址字段映射和县区派生 SQL 模板 |
| `config/examples.json` | 已验证问答-SQL 示例，仅用于上下文增强 |
| `config/prompts.json` | 规划、SQL 生成、SQL 语义审核的版本化 System Prompt |
| `.env` | 本机模型配置，不纳入 Git |
| `runtime/query_audit.jsonl` | 查询审计日志，不记录 SQL |
| `docs/bitcrew-workflow.md` | Bit-Crew 节点配置细节 |

当前 SQLite 默认路径为：

```text
../data/数据入库v_1.1_0722/query_ready_v2/zhangbei_energy_query_ready_v2.sqlite3
```

同一数据包的 DDL 目录为：

```text
../data/数据入库v_1.1_0722/query_ready_v2/ddl
```

该版本在 `2026-07-17` 导入了 16 张业务表。服务运行时先校验 DDL 文件的 SHA-256、`CREATE TABLE` 名称和 SQLite 实际字段集；规划阶段加载全部表的业务覆盖范围、适合的问题类型和关键字段语义，SQL 阶段仅注入模型已选表的已发布字段 DDL、字段中文别名、类型、说明、数据限制、规则和示例。敏感或内部字段不会进入提示词。

服务只允许访问 `catalog.json` 发布的数据表和字段，SQLite 以只读方式打开。

### 2.1 渐进加载与规则发布

服务先执行前置路由：题集问题精确命中且 `routing_enabled=true` 时，按其意图 ID 决定必选候选表、数据不足或范围外；未精确命中或尚未启用的题集项，才按 `query_knowledge.json` 中已发布的轻量路由规则匹配。这样可以避免旧题集状态与新业务规则尚未核准一致时，错误阻断查询。允许进入查询后，第一次模型调用会得到全部增强表卡，选择 1 至 4 张已发布表；第二次调用才加载所选表的 DDL、全字段语义、已发布 SQL 规则和最相关的已验证示例。候选 SQL 先经过 AST 白名单校验：未发布字段、越出候选范围或只读查询结构等可修正问题只允许模型修复一次；写操作、多语句和注释等危险问题直接拒绝。SQL 可以使用候选表的子集，但由后续语义审核确认该子集足以回答问题。未确认的计算口径可标记为 `reference_enabled=true`：它会作为“不得执行”的辅助知识帮助模型识别数据缺口和拒答边界，但 `runtime_enabled=false` 时绝不会参与 SQL 生成。成功响应中的 `answer_guidance` 来自同一配置，供 Bit-Crew 后置 LLM 组织结论、口径、异常说明和来源；它不是 SQL 提示词，也不包含真实答案。

当前已发布的一期口径是：集中式新能源问题中的“装机容量”使用 `t01_operating_renewable_station_profile.grid_capacity_mw`（并网容量）统计。限电率、营收、变电站饱和等候选计算规则需要客户确认单位、阈值和适用范围后才能发布。

### 2.1.1 县区派生

表01、表02、表04、表07、表08、表09未单列 `county` 时，服务使用 `administrative_regions.json` 中的完整行政区名称，从项目或站点位置字段派生县区。对于“哪个县区”“按县区排名”等问题，模型不得追问是否接受按地址汇总；必须使用 SQL 上下文提供的 `CASE` 模板。未匹配或匹配多个行政区名称的记录统一标记为“待核实”，不得进入县区排名、占比或增速分母，并在最终回答中说明。

### 2.1.2 对象范围与字段筛选

TableCard 的 `object_scope` 用来区分业务对象和字段条件。例如，表02全表已经是“在建项目”，因此用户说“在建项目”只负责选中表02，不得自动生成 `current_progress = '在建'`。只有用户明确说出 `object_scope.status_filters.allowed_values` 中的实际枚举值，例如“前期”或“设备安装”，才允许筛选当前阶段字段。

该规则有三层约束：规划提示词区分对象和条件；SQL 上下文注入结构化 `object_scope`；服务端在 Guard 后、执行前确定性检查状态谓词。违反约束的候选 SQL 会交回原 SQL 生成器修复一次，不会进入数据库执行。

### 2.1.3 严格 DDL 与字段画像

`query_ready_v2/ddl/*.sql` 不只是可执行建表语句。每张 DDL 同时记录表的行粒度、当前行数、业务范围、关联口径、题集问题映射、已发布规则、待确认边界和验证问题示例；每个字段记录真实 SQLite 类型、中文含义、用途、非空/空值数、去重值数量，以及低基数字段的完整枚举、数值字段的当前范围或高基数字段的有限代表值。联系人、来源追溯列、原始解析列等排除字段不发布实际取值，也不会进入 LLM 的可查询字段上下文。

数据库或题集配置更新后，在 `medium` 目录重新生成：

```powershell
& 'C:\Users\nine\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  tools\generate_detailed_ddls.py
```

生成器依据当前 SQLite、`table_cards.json`、`validation_cases.json`、`query_knowledge.json` 和 `examples.json` 重建 16 张 DDL，并同步更新 `ddl_registry.json` 的 SHA-256。它可以重复执行，不会叠加旧画像。规划阶段只加载轻量字段业务含义；完整枚举、范围和题集口径只在选表后进入 SQL 生成与审核上下文。

### 2.2 源数据事实核查

每次更新 `data`、`data_1`、SQLite、DDL 或 `config/` 后，先运行只读核查：

```powershell
& 'C:\Users\nine\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  tools\audit_source_truth.py `
  --json-output config\source_truth_audit_2026-07-20.json `
  --markdown-output docs\source-truth-audit-2026-07-20.md
```

核查以原始 `data` 的业务语义、`data_1` 的运行源工作簿、SQLite 的运行字段为事实层级，逐表验证来源文件、处理后工作表、DDL 的列顺序/类型/主键属性、TableCard 和已发布规则字段。命令退出码非 `0` 时不得重启服务或发布配置。原始工作表经预处理拆分后如未登记显式映射，报告会标为 `not_mapped`，不会误判为缺失。

### 2.3 提示词配置

所有 LLM System Prompt 统一位于 `config/prompts.json`，不再硬编码在 Python 代码中。该文件必须包含下列四个非空提示词；服务启动时会校验缺失或格式错误的配置并拒绝启动：

| ID | 调用阶段 | 业务职责 |
| --- | --- | --- |
| `planner` | 第一轮 LLM | 基于增强表卡选择完整且必要的表，识别缺参和范围限制 |
| `sql_generator` | 第二轮 LLM | 依据所选表的 DDL、字段语义、规则和示例生成候选 SQLite SQL |
| `pre_execution_reviewer` | 第三轮 LLM | 审核候选 SQL 的选表、指标、单位、范围和结果形态；仅输出结构化意见，不生成 SQL |
| `result_reviewer` | 执行后 LLM | 基于脱敏结果证据判断是否足以回答；可回答、要求一次补查、澄清或拒绝；不生成 SQL |

修改提示词时应保持输出契约不变：规划器返回 `QueryPlan` JSON；执行前审核器返回 `PreExecutionReview` JSON；结果审核器返回 `ResultReview` JSON；SQL 生成器在 `initial`、`guard_repair`、`semantic_revision`、`result_requery` 模式下均仅返回一条 SQL。审核器不得返回 SQL。修改后必须运行全量测试，并重启 Uvicorn 才会加载新版本。

### 2.4 双阶段审核与受控试执行

服务采用显式状态机：`规划 -> SQL 生成 -> Guard -> 执行前审核 -> 受控执行 -> 结果证据 -> 结果审核`。Guard 修复和执行前语义修改共享一次 SQL 修改预算；结果审核最多触发一次补查。每次修改都由 SQL 生成器完成并重新通过 Guard。结果审核只接收有限行、脱敏的结果摘要，不能直接执行 SQL 或修改结果数值。

成功响应保留原有 `data`，并新增可选的 `result_sets`、`coverage`、`limitations`。`result_sets` 中的 `primary` 是主查询，`supplemental` 仅在服务端完成一次补查后出现；两个结果集不会被服务端自动相加。

## 3. Python 环境与依赖

当前服务的依赖已安装在 Codex 随附的 Python 中。不要直接使用 `(base)` Conda 的 `D:\Anaconda\python.exe`，其中可能没有 `uvicorn`、`fastapi` 或 `sqlglot`。

建议统一使用：

```text
C:\Users\nine\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe
```

首次安装或重新安装依赖：

```powershell
cd D:\bitagent_workspace\resources_agent\medium

& 'C:\Users\nine\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  -m pip install `
  -r requirements.txt
```

验证运行环境：

```powershell
& 'C:\Users\nine\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  -c "import fastapi, uvicorn, httpx, sqlglot; print('dependencies ready')"
```

## 4. `.env` 配置

在 `medium` 目录创建本机配置：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`：

```dotenv
OPENAI_BASE_URL=https://你的OpenAI兼容接口/v1
OPENAI_API_KEY=你的密钥
OPENAI_MODEL=你的模型名称
LLM_TIMEOUT_SECONDS=120
ENABLE_LLM_TRACE=false
LLM_TRACE_LOG_PATH=./runtime/llm_trace.jsonl
SQLITE_DB_PATH=../data/数据入库v_1.1_0722/query_ready_v2/zhangbei_energy_query_ready_v2.sqlite3
DDL_DIRECTORY=../data/数据入库v_1.1_0722/query_ready_v2/ddl
QUERY_TIMEOUT_SECONDS=10
MAX_RESULT_ROWS=100
AUDIT_LOG_PATH=./runtime/query_audit.jsonl
ENABLE_QUERY_DIAGNOSTICS=false
```

配置说明：

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `OPENAI_BASE_URL` | 是 | OpenAI 兼容接口的版本根路径，例如以 `/v1` 结束；不要填写 `/chat/completions`，服务会自动追加该路径 |
| `OPENAI_API_KEY` | 是 | 模型密钥，仅保存于本机 `.env` |
| `OPENAI_MODEL` | 是 | 模型名称 |
| `LLM_TIMEOUT_SECONDS` | 否 | 单次 LLM 调用超时秒数，默认 `120`；规划、SQL 生成和语义审核分别适用 |
| `ENABLE_LLM_TRACE` | 否 | 是否写入仅服务端可读的原始 LLM 输出追踪日志，默认 `false` |
| `LLM_TRACE_LOG_PATH` | 否 | 原始 LLM 输出追踪日志路径，默认 `./runtime/llm_trace.jsonl` |
| `SQLITE_DB_PATH` | 是 | SQLite 数据库路径，可使用相对路径 |
| `QUERY_TIMEOUT_SECONDS` | 否 | SQLite 单次查询超时，默认 `10` 秒 |
| `MAX_RESULT_ROWS` | 否 | 最多返回结果行数，默认 `100`，上限 `1000` |
| `AUDIT_LOG_PATH` | 否 | 审计日志路径 |

不要把 `.env`、API Key、Cloudflare Access 密钥或用户完整问题提交到 Git、工作流导出文件和日志。`ENABLE_LLM_TRACE=true` 只用于受控联调：该日志会保存模型原始输出，必须限制服务器本机访问，并在问题定位后关闭。

### 4.1 多供应商自动降级

复制供应商池示例并填写各服务的地址、模型和密钥变量名：

```powershell
Copy-Item config\llm_providers.example.json config\llm_providers.json
```

在 `.env` 中启用并保存实际密钥：

```dotenv
LLM_PROVIDERS_PATH=config/llm_providers.json
LLM_PRIMARY_API_KEY=主服务密钥
LLM_FALLBACK_1_API_KEY=备用服务密钥
LLM_PROVIDER_RETRY_COUNT=1
LLM_CIRCUIT_FAILURE_THRESHOLD=3
LLM_CIRCUIT_COOLDOWN_SECONDS=60
LLM_MAX_PROVIDER_ATTEMPTS=3
```

`llm_providers.json` 只保存 `api_key_env`，不得保存实际密钥。各阶段按 `stage_routes` 顺序调用供应商。连接失败、超时、限流、上游 5xx、空响应、无效响应以及规划/审核结构错误会尝试备用服务；请求本身的 400/422 错误不会盲目切换。连续失败达到阈值后供应商进入冷却，避免每个请求反复等待已故障服务。

未配置 `LLM_PROVIDERS_PATH` 时，服务继续使用原有 `OPENAI_BASE_URL/OPENAI_API_KEY/OPENAI_MODEL`，保持向后兼容。供应商配置只在服务启动时加载，修改后必须重启。

## 5. 本地启动与检查

### 5.1 启动服务

在终端 1 执行并保持窗口运行：

```powershell
cd D:\bitagent_workspace\resources_agent\medium

& 'C:\Users\nine\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  -m uvicorn app.main:app `
  --host 127.0.0.1 `
  --port 8030 `
  --log-level debug
```

启动成功必须出现：

```text
Uvicorn running on http://127.0.0.1:8030
```

### 5.2 检查健康状态

在终端 2 执行：

```powershell
Invoke-RestMethod http://127.0.0.1:8030/health
```

期望结果：

```json
{
  "status": "healthy",
  "checks": {
    "database": "healthy",
    "llm": "configured"
  }
}
```

`database=healthy` 表示 SQLite 可读；`llm=configured` 仅表示模型配置存在，不表示模型接口已经可用。

本地接口地址：

| 地址 | 用途 |
| --- | --- |
| `GET http://127.0.0.1:8030/health` | 健康检查 |
| `http://127.0.0.1:8030/docs` | Swagger/OpenAPI 页面 |
| `POST http://127.0.0.1:8030/api/v1/query-energy-data` | 数据查询接口 |

### 5.3 本地真实查询

不要在 PowerShell 中优先使用 `curl.exe -d $body` 测试中文 JSON；其原生参数编码可能造成请求体解析异常。使用 `Invoke-RestMethod`：

```powershell
$body = @{
  question = '张北县已运行风电项目装机容量合计是多少？'
} | ConvertTo-Json -Compress

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8030/api/v1/query-energy-data `
  -ContentType 'application/json; charset=utf-8' `
  -Headers @{ Accept = 'application/json' } `
  -Body $body
```

正常情况下会返回 `success=true`、`data.summary`、`sources` 和 `request_id`。该请求包含两次模型调用，实际耗时取决于模型接口；当前联调样例约为 8 秒。

## 6. Cloudflare Quick Tunnel 联调

Quick Tunnel 仅用于联调，不能替代生产部署。

### 6.1 启动隧道

保持终端 1 的 Uvicorn 运行，在终端 3 执行：

```powershell
cloudflared --version
cloudflared --loglevel info tunnel --url http://127.0.0.1:8030
```

如果本机没有 `cloudflared`：

```powershell
winget install --id Cloudflare.cloudflared
```

启动后记录终端输出的临时域名，例如：

```text
https://random-name.trycloudflare.com
```

关闭 `cloudflared` 后域名立即失效；再次启动时域名通常改变。

### 6.2 先验证公网健康检查

在终端 2 执行：

```powershell
$publicUrl = 'https://random-name.trycloudflare.com'
Invoke-RestMethod "$publicUrl/health"
```

必须先得到 `status=healthy`，才能配置 Bit-Crew。

### 6.3 再验证公网 POST

```powershell
$body = @{
  question = '张北县已运行风电项目装机容量合计是多少？'
} | ConvertTo-Json -Compress

Measure-Command {
  Invoke-RestMethod `
    -Method Post `
    -Uri "$publicUrl/api/v1/query-energy-data" `
    -ContentType 'application/json; charset=utf-8' `
    -Headers @{ Accept = 'application/json' } `
    -Body $body
}
```

只有公网 POST 成功后，才进入 Bit-Crew 工作流配置。

### 6.4 Cloudflare DNS 异常

若 `cloudflared` 输出以下日志：

```text
ERR Failed to refresh DNS local resolver error="lookup region1.v2.argotunnel.com: i/o timeout"
```

表示 Cloudflare 客户端本机的 DNS 解析刷新失败。它不一定立即断开已建立的隧道，但必须以公网 `/health` 和公网 POST 结果为准。

先诊断 DNS：

```powershell
ipconfig /flushdns
Resolve-DnsName region1.v2.argotunnel.com -Server 1.1.1.1
Resolve-DnsName region1.v2.argotunnel.com -Server 8.8.8.8
```

处理原则：

1. 两个 DNS 查询都失败：当前网络、代理或防火墙阻断了 DNS/Cloudflare 连接，需由网络环境处理。
2. DNS 查询成功但 `cloudflared` 持续报错：停止当前隧道后重新启动；确认本机代理软件的 DNS 设置没有劫持或阻断连接。
3. 公网 `/health` 或 POST 失败：不要配置 Bit-Crew，先恢复稳定的公网访问。
4. 联调稳定后应改用命名 Tunnel、固定域名和 Cloudflare Access，不要长期依赖 `trycloudflare.com`。

本手册不自动修改系统 DNS。若需要更换 DNS 服务器，应先按本机网络和组织安全要求确认。

## 7. Bit-Crew HTTP 节点配置

详细节点图和错误分支见 [docs/bitcrew-workflow.md](docs/bitcrew-workflow.md)。HTTP 节点的最小配置如下：

| 配置项 | 填写内容 |
| --- | --- |
| 方法 | `POST` |
| URL | `https://<trycloudflare域名>/api/v1/query-energy-data` |
| 请求体类型 | `json` |
| `Content-Type` | `application/json; charset=utf-8` |
| `Accept` | `application/json` |
| 鉴权 | 未启用 Cloudflare Access 时关闭 |
| 超时时间 | `150.0` 秒 |
| 失败重试 | 关闭，由后续错误分支最多重试一次 |
| 异常处理方式 | 调试阶段使用“继续流程”或输出异常变量；不要直接中断且不保留错误信息 |

请求体只允许以下三个字段：

```json
{
  "question": "{{question}}"
}
```

字段约束：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `question` | string | 去除首尾空格后 1～2000 字符 |

截图所示的 HTTP 节点输出中，`body` 是 `String`，不是对象。HTTP 节点后必须先通过代码/变量处理节点解析：

```javascript
const query_response = JSON.parse(body);
```

后续分支读取解析后的字段：

```text
query_response.success
query_response.request_id
query_response.data
query_response.sources
query_response.warnings
query_response.error.code
query_response.error.retryable
```

HTTP `200` 不代表业务查询成功。`200 + success=false` 是服务已正常返回业务错误；只有网络错误、网关错误、非 JSON 响应或节点超时才属于 HTTP 节点异常。

## 8. API 契约

### 8.1 请求

```json
{
  "question": "张北县已运行风电项目装机容量合计是多少？"
}
```

{
  "inputParameters": {
    "question": "张北县已运行风电项目装机容量合计是多少？"
  }
}{"output":{"output":"抱歉，我无法根据您提供的信息回答这个问题。\n\n请求编号：qry_cd89c56c11d74767979cec25df50db65","question":"在建项目中，建设用地批复尚未办结的项目有哪些？列出项目名称，统计数量和总装机容量（MW）"},"processInstanceId":"2079846670824247296","traceId":"hyuwGuDayHejeInN"}
/api/v1/query-energy-data
https://shoes-greensboro-module-alexander.trycloudflare.com/api/v1/query-energy-data
https://inspired-dayton-technological-cedar.trycloudflare.com/api/v1/query-energy-data
https://viruses-everywhere-fighter-arab.trycloudflare.com/api/v1/query-energy-data
https://achievement-release-ref-academic.trycloudflare.com/api/v1/query-energy-data
https://compounds-convicted-aruba-referenced.trycloudflare.com/api/v1/query-energy-data
https://strip-moore-casual-seemed.trycloudflare.com/api/v1/query-energy-data
https://advances-genius-interventions-low.trycloudflare.com/api/v1/query-energy-data
https://enforcement-fuzzy-controller-replaced.trycloudflare.com/api/v1/query-energy-data
https://moment-senate-astrology-offset.trycloudflare.com/api/v1/query-energy-data
https://engineer-accordingly-freeze-end.trycloudflare.com/api/v1/query-energy-data
https://choose-dennis-affiliated-travels.trycloudflare.com/api/v1/query-energy-data
https://frog-pixel-mistakes-computing.trycloudflare.com/api/v1/query-energy-data
https://fixed-matched-soldier-greatly.trycloudflare.com/api/v1/query-energy-data
https://disabilities-passage-colour-edwards.trycloudflare.com/api/v1/query-energy-data
https://yeast-isolated-entries-receptor.trycloudflare.com/api/v1/query-energy-data
https://travel-lease-save-pills.trycloudflare.com/api/v1/query-energy-data
https://reserve-submissions-extensions-software.trycloudflare.com/api/v1/query-energy-data
https://tel-smart-appliance-handling.trycloudflare.com/api/v1/query-energy-data
https://isle-treaty-groundwater-floating.trycloudflare.com/api/v1/query-energy-data
https://mention-wines-addresses-extent.trycloudflare.com/api/v1/query-energy-data
https://advertising-estimated-machines-sustained.trycloudflare.com/api/v1/query-energy-data
https://chains-modular-colin-reservoir.trycloudflare.com/api/v1/query-energy-data
https://oecd-memories-sat-vsnet.trycloudflare.com/api/v1/query-energy-data
https://robertson-tubes-carrying-sending.trycloudflare.com/api/v1/query-energy-data
https://tramadol-processes-detect-attending.trycloudflare.com/api/v1/query-energy-data
https://reed-facts-jar-closer.trycloudflare.com/api/v1/query-energy-data
https://external-excellent-tribunal-feels.trycloudflare.com/api/v1/query-energy-data
https://bibliography-cant-protecting-ecology.trycloudflare.com/api/v1/query-energy-data
https://strike-tissue-thumbnail-purse.trycloudflare.com/api/v1/query-energy-data
https://scotland-minimize-pit-executive.trycloudflare.com/api/v1/query-energy-data
https://regards-sig-relay-metallic.trycloudflare.com/api/v1/query-energy-data
https://situations-intention-dispatch-lip.trycloudflare.com/api/v1/query-energy-data
https://works-invest-teach-bit.trycloudflare.com/api/v1/query-energy-data
https://captain-staffing-rugs-lovely.trycloudflare.com/api/v1/query-energy-data
https://exposed-charger-fits-cocktail.trycloudflare.com/api/v1/query-energy-data
服务不接收 `operation`、`metric`、`filters`、`group_by`、`limit`、SQL、表名、字段名、DDL 或 RAG 原始 chunk。它们属于旧 `min` 服务的查询计划协议，或应在 Medium 服务内部受控处理。

### 8.2 成功响应

```json
{
  "success": true,
  "data": {
    "rows": [{"total_capacity_mw": 6000.0}],
    "summary": {"total_capacity_mw": 6000.0},
    "schema": [{"name": "total_capacity_mw", "type": "number"}],
    "data_as_of": "2026-05-31",
    "result_status": "data_found"
  },
  "sources": [
    {
      "dataset": "已运行新能源集中式电站",
      "version": "local-sqlite-2026-07",
      "data_as_of": "2026-05-31"
    }
  ],
  "request_id": "qry_xxx",
  "warnings": [],
  "error": null
}
```

### 8.3 错误响应

```json
{
  "success": false,
  "data": null,
  "sources": [],
  "request_id": "qry_xxx",
  "warnings": [],
  "error": {
    "code": "CLARIFICATION_REQUIRED",
    "message": "请明确需要查询的区县。",
    "retryable": false
  }
}
```

常见错误码：

| 错误码 | 含义 | Bit-Crew 行为 |
| --- | --- | --- |
| `INVALID_ARGUMENT` | 请求体不符合契约 | 不重试，修正输入 |
| `CLARIFICATION_REQUIRED` | 缺少必要条件 | 直接向用户追问 |
| `QUERY_NOT_SUPPORTED` | 已发布数据不支持该问题 | 说明范围限制 |
| `CAPABILITY_NOT_SUPPORTED` | SQL 语义审核确认当前数据或已发布规则不支持该问题 | 说明数据或口径限制，不重试 |
| `LLM_NOT_CONFIGURED` | `.env` 模型配置不完整 | 检查本机 `.env` |
| `LLM_UPSTREAM_UNAVAILABLE` | 模型接口连接或 HTTP 调用失败 | 最多重试一次，检查模型服务 |
| `LLM_TIMEOUT` | 模型接口调用超时 | 最多重试一次 |
| `LLM_INVALID_JSON` | 模型未返回可解析内容 | 最多重试一次 |
| `LLM_PLAN_SCHEMA_INVALID` | 模型返回的查询规划不符合 JSON 契约 | 不重试，检查模型输出与提示词 |
| `SEMANTIC_VALIDATION_FAILED` | SQL 语义审核结果不符合 JSON 契约或缺少必要重写内容 | 不执行 SQL，不重试 |
| `SQL_REWRITE_EXHAUSTED` | SQL 经一次语义重写后仍未通过审核 | 不执行 SQL，说明当前问题需要澄清或规则补充 |
| `SQL_GENERATION_FAILED` | SQL 生成失败 | 最多重试一次 |
| `SQL_VALIDATION_FAILED` | 生成 SQL 未通过安全校验 | 不展示 SQL，不重试 |
| `NO_DATA_AFTER_VALID_FILTER` | 合法筛选后无匹配记录 | 返回 `success=true`、`result_status=no_match`，由后置 LLM 如实说明 |
| `QUERY_TIMEOUT` | SQLite 查询超时 | 最多重试一次或缩小问题范围 |
| `INTERNAL_ERROR` | 服务内部错误 | 展示 `request_id` 以便排查 |

对于 `config/examples.json` 中精确匹配的已验证问题，若模型规划或 SQL 生成失败，服务会回退执行对应示例 SQL。示例 SQL 仍需经过 SQL 白名单校验和只读执行；响应保持 `success=true`，并在 `warnings` 中标记 `LLM_FALLBACK_EXAMPLE`。近似问法、未收录问题和未通过校验的示例不会触发回退。

## 9. 常见故障排查

| 现象 | 首先检查 | 处理 |
| --- | --- | --- |
| `No module named uvicorn` | 是否使用了 Conda Python | 使用本手册指定的 Codex Python，或在当前环境安装 `requirements.txt` |
| 本地 `/health` 无法访问 | Uvicorn 是否仍在终端 1 运行 | 查看启动日志、确认端口 `8030` 未被占用 |
| `/health` 为 `degraded` | `database` 和 `llm` 两个字段 | 修正 SQLite 路径或 `.env` 三个 OpenAI 配置 |
| PowerShell `curl.exe` 返回 `422` | 请求体传递与编码 | 使用 `Invoke-RestMethod` 和 `ConvertTo-Json` |
| Bit-Crew `SocketTimeoutException` | 节点超时是否仍为 `10s` | 设为 `150s`，再依次验证本地 POST、Cloudflare POST、Bit-Crew GET `/health` |
| Bit-Crew GET `/health` 成功而 POST 超时 | POST 节点 URL、Body 类型、超时 | 使用固定 JSON 请求体测试；确认路径含 `/api/v1/query-energy-data` |
| HTTP `200` 但下游变量为空 | HTTP 节点 `body` 是字符串 | 先 `JSON.parse(body)`，再进入条件分支 |
| Cloudflare DNS 刷新超时 | `Resolve-DnsName region1.v2.argotunnel.com` | 修复 DNS/代理网络后重启 `cloudflared` |

## 10. 安全与生产要求

- SQLite 使用 `mode=ro` 和 `query_only=ON` 双重只读；
- 只允许单条 `SELECT` 或 `WITH ... SELECT`，并校验表、字段、函数和行数上限；
- 拒绝写操作、多语句、注释、通配字段、PRAGMA、ATTACH、系统表和扩展加载；
- 审计仅保留问题哈希、表、耗时、状态和错误码，不记录 SQL；
- Quick Tunnel 仅供短期联调；生产应使用命名 Tunnel、固定域名、Cloudflare Access/API 网关和来源访问控制；
- Bit-Crew 密钥、OpenAI Key 和 Cloudflare Access Service Token 必须使用平台密钥变量，不得写入请求体、提示词、日志和 Git。

## 11. 自动验证

```powershell
cd D:\bitagent_workspace\resources_agent\medium

& 'C:\Users\nine\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  -m pytest tests -v
```

## 12. 联调诊断

联调期间可在 `.env` 中设置：

```dotenv
ENABLE_QUERY_DIAGNOSTICS=true
```

重启 Uvicorn 后，`POST /api/v1/query-energy-data` 的响应会额外包含 `diagnostics`：查询规划状态、候选 SQL、SQL Guard 校验结果、SQL 语义审核决策以及示例回退状态。此开关默认关闭；正式环境必须保持 `false`，并且 Bit-Crew 的最终回答节点不得向最终用户展示 `diagnostics`。

测试使用假模型和临时 SQLite，不调用外部模型，不修改原始数据库。

### 12.1 服务端 LLM 输出追踪

设置以下配置并重启服务后，`runtime/llm_trace.jsonl` 会按同一 `request_id` 记录 `planning`、`sql_generation`、`semantic_review` 三个阶段的原始模型输出或调用错误类型：

```dotenv
LLM_TIMEOUT_SECONDS=120
ENABLE_LLM_TRACE=true
LLM_TRACE_LOG_PATH=./runtime/llm_trace.jsonl
```

PowerShell 查看最新追踪记录：

```powershell
Get-Content .\runtime\llm_trace.jsonl -Tail 20
```

该日志不会经 HTTP 响应返回给 Bit-Crew；不要将其中内容复制到最终用户回答。
