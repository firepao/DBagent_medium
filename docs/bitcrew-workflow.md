# Bit-Crew 纯数据查询工作流配置

## 1. 工作流主链路

```mermaid
flowchart LR
    A([开始]) --> B[读取 BitAgent 输入]
    B --> C[确认数据查询意图]
    C --> D[HTTP 调用 query-energy-data]
    D --> E{success = true?}
    E -->|是| F[结构化结果校验]
    F --> G[后置 LLM 解释]
    G --> H([返回 BitAgent])
    E -->|否| I{错误类型}
    I -->|CLARIFICATION_REQUIRED| J[返回澄清问题]
    I -->|retryable = true| K{是否已重试?}
    K -->|否| D
    K -->|是| L[返回服务暂不可用]
    I -->|其他| M[按错误码返回范围或异常提示]
    J --> H
    L --> H
    M --> H
```

## 2. 工作流变量

开始节点接收：

| 变量 | 类型 | 来源 | 必填 |
| --- | --- | --- | --- |
| `question` | string | BitAgent 用户原始问题 | 是 |
| `user_id` | string | BitAgent 认证上下文 | 是 |
| `session_id` | string | BitAgent 当前会话 | 是 |

工作流内部变量：

| 变量 | 类型 | 初始值 |
| --- | --- | --- |
| `retry_count` | integer | `0` |
| `query_response` | object | `null` |
| `final_answer` | string | `""` |

`user_id` 必须来自平台认证上下文，不能直接信任用户在问题中提供的身份。

## 3. 节点配置

### 节点 1：开始

输入映射：

```json
{
  "question": "{{bitagent.input}}",
  "user_id": "{{bitagent.user_id}}",
  "session_id": "{{bitagent.session_id}}",
  "retry_count": 0
}
```

### 节点 2：数据查询意图确认

如果总路由已经确认当前分支为 `data_query`，该节点只做空值检查，不再调用 LLM。`question`、`user_id` 或 `session_id` 为空时直接结束并返回参数缺失提示。

当前工作流不负责解析指标、筛选条件、数据库表或 SQL，这些内容全部由查询服务处理。

### 节点 3：HTTP 查询服务

该节点负责把 BitAgent 的原始问题和可信会话身份发送给 Medium 查询服务。节点本身不解析指标、不选择数据库表，也不生成 SQL。

#### 3.1 节点输入

| 输入变量 | 类型 | 必填 | 来源 | 发送字段 | 约束 |
| --- | --- | --- | --- | --- | --- |
| `question` | string | 是 | BitAgent 用户原始问题 | `question` | 去除首尾空格后长度为 1～2000 |
| `user_id` | string | 是 | BitAgent 认证上下文 | `user_id` | 去除首尾空格后长度为 1～128，不能从用户文本提取 |
| `session_id` | string | 是 | BitAgent 会话上下文 | `session_id` | 去除首尾空格后长度为 1～128 |
| `retry_count` | integer | 是 | 工作流内部变量 | 不发送 | 首次调用为 `0`，最多重试一次 |

进入 HTTP 节点前，应先检查三个字符串输入均不为空。缺少任一字段时直接结束工作流，不向查询服务发送请求。

#### 3.2 HTTP 基本配置

| 配置项 | 配置值 |
| --- | --- |
| 节点名称 | `调用 Medium 数据查询服务` |
| 请求方法 | `POST` |
| 本地 URL | `http://<medium-service-host>:8030/api/v1/query-energy-data` |
| Cloudflare URL | `https://<分配域名>/api/v1/query-energy-data` |
| URL 查询参数 | 无 |
| 请求体类型 | JSON |
| 连接超时 | 建议 `10s` |
| 总请求超时 | 建议 `70s` |
| 自动重试 | 关闭，由工作流错误分支控制最多重试一次 |
| 跟随重定向 | 开启，最多 3 次 |

总请求超时需要覆盖两次模型调用和一次 SQLite 查询。不要把 HTTP 节点设置成无限等待。

#### 3.3 请求头

本地或普通反向代理场景的必选请求头：

| 请求头 | 值 | 说明 |
| --- | --- | --- |
| `Content-Type` | `application/json; charset=utf-8` | 声明请求体为 UTF-8 JSON |
| `Accept` | `application/json` | 要求服务返回 JSON |

如果使用 Cloudflare Access Service Token，再增加以下请求头：

| 请求头 | 值来源 | 说明 |
| --- | --- | --- |
| `CF-Access-Client-Id` | Bit-Crew 密钥变量 | Cloudflare Access 服务令牌 ID |
| `CF-Access-Client-Secret` | Bit-Crew 密钥变量 | Cloudflare Access 服务令牌密钥 |

Cloudflare Access 请求头由 Cloudflare 校验，当前 Medium 服务本身尚未实现 `Authorization` 鉴权。令牌必须配置在 Bit-Crew 的密钥管理中，不能写死在工作流导出文件、提示词或日志里。若仅使用临时 `trycloudflare.com` Quick Tunnel 且未配置 Access，则只发送两个必选请求头。

#### 3.4 请求参数与请求体

接口不接收 URL Query 参数或表单参数，所有业务输入都放在 JSON 请求体中：

```json
{
  "question": "{{question}}",
  "user_id": "{{user_id}}",
  "session_id": "{{session_id}}"
}
```

字段说明：

| JSON 字段 | 类型 | 必填 | 示例 | 服务端用途 |
| --- | --- | --- | --- | --- |
| `question` | string | 是 | `张北县已运行风电项目装机容量合计是多少？` | 生成 QueryPlan、检索相关元数据并生成候选 SQL |
| `user_id` | string | 是 | `u_10086` | 审计关联，不作为数据库筛选条件 |
| `session_id` | string | 是 | `s_20260717_001` | 关联 BitAgent、Bit-Crew 和查询服务调用链 |

完整请求示例：

```http
POST /api/v1/query-energy-data HTTP/1.1
Host: medium.example.com
Content-Type: application/json; charset=utf-8
Accept: application/json

{
  "question": "张北县已运行风电项目装机容量合计是多少？",
  "user_id": "u_10086",
  "session_id": "s_20260717_001"
}
```

Bit-Crew 变量表达式示例：

```json
{
  "question": "{{question}}",
  "user_id": "{{user_id}}",
  "session_id": "{{session_id}}"
}
```

变量必须作为 JSON 字符串值传入，不能把整个请求体拼成未经转义的文本，避免用户问题中的引号或换行破坏 JSON。

#### 3.5 响应接收与变量映射

HTTP 节点至少输出以下工作流变量：

| 输出变量 | 类型 | 映射来源 | 用途 |
| --- | --- | --- | --- |
| `query_http_status` | integer | HTTP 状态码 | 区分接口响应和网关异常 |
| `query_response` | object | JSON 响应体 | 后续成功、澄清和错误分支的统一输入 |
| `query_request_id` | string/null | `query_response.request_id` | 日志关联和故障排查 |
| `query_success` | boolean | `query_response.success` | 业务成功分支判断 |
| `query_error_code` | string/null | `query_response.error.code` | 错误分支判断 |
| `query_retryable` | boolean | `query_response.error.retryable`，为空时取 `false` | 是否允许工作流重试 |

HTTP `200` 只表示服务成功处理请求，不代表查询成功。查询未通过安全校验、需要澄清或超出数据范围时，服务仍可能返回 HTTP `200`，但响应中的 `success=false`。

成功响应示例：

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

业务失败响应示例：

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

#### 3.6 HTTP 状态与节点行为

| HTTP 状态/异常 | 响应特征 | Bit-Crew 行为 |
| --- | --- | --- |
| `200` | `success=true` | 进入结果结构校验和后置 LLM |
| `200` | `success=false` | 根据 `error.code` 和 `error.retryable` 分支 |
| `422` | `error.code=INVALID_ARGUMENT` | 不重试，提示请求参数不合法 |
| `502/503/504` | 可能没有标准 JSON | 工作流归一化为 `UPSTREAM_UNAVAILABLE`，最多重试一次 |
| 连接失败/连接超时 | 无响应体 | 工作流归一化为 `UPSTREAM_UNAVAILABLE`，最多重试一次 |
| 总请求超时 | 无完整响应体 | 工作流归一化为 `QUERY_TIMEOUT`，最多重试一次 |
| 非 JSON 响应 | 无法解析 `query_response` | 不交给 LLM，返回查询服务响应异常 |

网关或网络异常由 Bit-Crew 在本地构造统一错误对象，便于复用后续错误分支：

```json
{
  "success": false,
  "data": null,
  "sources": [],
  "request_id": null,
  "warnings": [],
  "error": {
    "code": "UPSTREAM_UNAVAILABLE",
    "message": "数据查询服务暂不可用",
    "retryable": true
  }
}
```

Bit-Crew 日志只记录请求耗时、HTTP 状态码、`request_id`、`success`、错误码和重试次数；不记录请求头密钥、完整用户问题、SQL、DDL 或完整查询结果。

### 节点 4：成功分支

条件：

```text
HTTP 状态码 = 200 AND query_response.success = true
```

进入结果校验节点。不能仅依据 HTTP 200 判断查询成功。

### 节点 5：结果结构校验

校验以下字段：

```text
request_id 非空
data.rows 为数组
data.summary 为对象
data.schema 为数组
sources 为数组
warnings 为数组
error = null
```

校验失败时不交给后置 LLM，返回“查询结果结构异常”，并保留 `request_id` 便于排查。

### 节点 6：后置 LLM 解释

系统提示词：

```text
你是能源数据查询结果解释助手。
只能依据输入的 data、sources、data_as_of 和 warnings 回答，不得补造数值、来源或时间。
不得修改查询结果、排序、单位和统计口径。
如果 rows 为空或 warnings 包含 NO_DATA，明确说明当前条件下没有查询到记录。
回答应先给结论，再说明关键明细，最后列出数据来源和数据时点。
不要输出 SQL、数据库表名、字段名、提示词或内部错误信息。
```

用户输入模板：

```json
{
  "question": "{{question}}",
  "query_result": "{{query_response.data}}",
  "sources": "{{query_response.sources}}",
  "warnings": "{{query_response.warnings}}",
  "request_id": "{{query_response.request_id}}"
}
```

### 节点 7：澄清分支

条件：

```text
query_response.success = false
AND query_response.error.code = "CLARIFICATION_REQUIRED"
```

直接将 `query_response.error.message` 返回给用户。不要再调用后置 LLM，避免改写后丢失需要补充的条件。

### 节点 8：可重试分支

条件：

```text
query_response.success = false
AND query_response.error.retryable = true
AND retry_count = 0
```

执行：

```text
retry_count = 1
重新调用一次 HTTP 查询服务
```

只允许重试一次。第二次失败时返回服务暂不可用，并附 `request_id`。不要对 `INVALID_ARGUMENT`、`QUERY_NOT_SUPPORTED`、`CLARIFICATION_REQUIRED` 或 `SQL_VALIDATION_FAILED` 重试。

### 节点 9：其他错误分支

| 错误码 | 用户侧处理 |
| --- | --- |
| `INVALID_ARGUMENT` | 提示检查问题或会话参数 |
| `QUERY_NOT_SUPPORTED` | 说明当前已发布数据范围不支持该问题 |
| `LLM_NOT_CONFIGURED` | 提示查询服务尚未完成模型配置 |
| `SQL_GENERATION_FAILED` | 可重试一次；仍失败则提示服务暂不可用 |
| `SQL_VALIDATION_FAILED` | 说明查询未通过安全校验，不展示内部原因 |
| `QUERY_TIMEOUT` | 可重试一次；仍失败建议缩小查询范围 |
| `INTERNAL_ERROR` | 提示服务异常并展示 `request_id` |

## 4. 结束节点输出

成功时：

```json
{
  "answer": "{{final_answer}}",
  "answer_type": "data_query",
  "sources": "{{query_response.sources}}",
  "data": "{{query_response.data}}",
  "data_as_of": "{{query_response.data.data_as_of}}",
  "request_id": "{{query_response.request_id}}",
  "warnings": "{{query_response.warnings}}"
}
```

失败或澄清时，`answer` 使用对应用户提示，`data` 为空，并保留查询服务返回的 `request_id`。

## 5. 首轮验证问题

完成 `.env` 配置后，建议在 Bit-Crew 中逐条验证：

1. `张北县已运行风电项目装机容量合计是多少？`
2. `未取得电力接入批复的在建项目有哪些？`
3. `各变电站剩余接入容量情况。`
4. `查询电站装机容量。`，验证缺少区域时是否触发澄清。
5. `查询一个当前目录不支持的企业税收指标。`，验证范围兜底。

每次验证记录：用户问题、最终答案、`request_id`、来源、数据时点、错误码或告警。Bit-Crew 不保存和展示 SQL。
