# Medium 数据查询服务运行手册

## 1. 服务定位

`medium` 是面向 Bit-Crew 的自然语言数据查询服务。Bit-Crew 发送原始问题和会话标识，服务在内部完成查询规划、元数据检索、LLM 生成 SQLite SQL、安全校验、只读执行和审计。

```text
BitAgent
-> Bit-Crew HTTP 节点
-> Medium POST /api/v1/query-energy-data
-> LLM 生成 QueryPlan
-> LLM 生成 SQL
-> SQL 白名单校验
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
| `config/examples.json` | 已验证问答-SQL 示例，仅用于上下文增强 |
| `.env` | 本机模型配置，不纳入 Git |
| `runtime/query_audit.jsonl` | 查询审计日志，不记录 SQL |
| `docs/bitcrew-workflow.md` | Bit-Crew 节点配置细节 |

当前 SQLite 默认路径为：

```text
../data/数据入库_v1.0.1_2026.07.17/data_1_all/zhangbei_energy_data_data1.sqlite3
```

同一数据包的 DDL 目录为：

```text
../data/数据入库_v1.0.1_2026.07.17/data_1_all/vanna_table_ddls
```

该版本在 `2026-07-17` 导入了 16 张业务表。服务运行时从 SQLite 读取实际字段；目录中的 DDL 用于同步表别名、字段说明和受控发布范围。

服务只允许访问 `catalog.json` 发布的数据表和字段，SQLite 以只读方式打开。

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
SQLITE_DB_PATH=../data/数据入库_v1.0.1_2026.07.17/data_1_all/zhangbei_energy_data_data1.sqlite3
QUERY_TIMEOUT_SECONDS=10
MAX_RESULT_ROWS=100
AUDIT_LOG_PATH=./runtime/query_audit.jsonl
```

配置说明：

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `OPENAI_BASE_URL` | 是 | OpenAI 兼容接口的版本根路径，例如以 `/v1` 结束；不要填写 `/chat/completions`，服务会自动追加该路径 |
| `OPENAI_API_KEY` | 是 | 模型密钥，仅保存于本机 `.env` |
| `OPENAI_MODEL` | 是 | 模型名称 |
| `SQLITE_DB_PATH` | 是 | SQLite 数据库路径，可使用相对路径 |
| `QUERY_TIMEOUT_SECONDS` | 否 | SQLite 单次查询超时，默认 `10` 秒 |
| `MAX_RESULT_ROWS` | 否 | 最多返回结果行数，默认 `100`，上限 `1000` |
| `AUDIT_LOG_PATH` | 否 | 审计日志路径 |

不要把 `.env`、API Key、Cloudflare Access 密钥或用户完整问题提交到 Git、工作流导出文件和日志。

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
  user_id = 'u_local_test'
  session_id = 's_local_test'
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
  user_id = 'u_public_test'
  session_id = 's_public_test'
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
  "question": "{{question}}",
  "user_id": "{{user_id}}",
  "session_id": "{{session_id}}"
}
```

字段约束：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `question` | string | 去除首尾空格后 1～2000 字符 |
| `user_id` | string | 去除首尾空格后 1～128 字符，来自平台认证上下文 |
| `session_id` | string | 去除首尾空格后 1～128 字符，来自平台会话上下文 |

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
  "question": "张北县已运行风电项目装机容量合计是多少？",
  "user_id": "u_10086",
  "session_id": "s_20260717_001"
}
```

服务不接收 `operation`、`metric`、`filters`、`group_by`、`limit`、SQL、表名或字段名。它们属于旧 `min` 服务的查询计划协议。

### 8.2 成功响应

```json
{
  "success": true,
  "data": {
    "rows": [{"total_capacity_mw": 6000.0}],
    "summary": {"total_capacity_mw": 6000.0},
    "schema": [{"name": "total_capacity_mw", "type": "number"}],
    "data_as_of": "2026-05-31"
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
| `LLM_NOT_CONFIGURED` | `.env` 模型配置不完整 | 检查本机 `.env` |
| `SQL_GENERATION_FAILED` | 模型规划或 SQL 生成失败 | 最多重试一次 |
| `SQL_VALIDATION_FAILED` | 生成 SQL 未通过安全校验 | 不展示 SQL，不重试 |
| `QUERY_TIMEOUT` | SQLite 查询超时 | 最多重试一次或缩小问题范围 |
| `INTERNAL_ERROR` | 服务内部错误 | 展示 `request_id` 以便排查 |

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

测试使用假模型和临时 SQLite，不调用外部模型，不修改原始数据库。
