# Medium 数据查询服务

该服务供 Bit-Crew 的数据查询工作流调用。Bit-Crew 只发送自然语言问题和会话标识；服务内部完成查询规划、受控元数据装配、LLM 生成 SQLite SQL、安全校验、只读执行、来源封装和审计。

服务不会向 Bit-Crew 返回 SQL、DDL、数据库路径、模型提示词或内部异常堆栈。

## 1. 运行链路

```text
Bit-Crew HTTP 节点
-> POST /api/v1/query-energy-data
-> LLM 解析 QueryPlan
-> 检索相关表、字段、业务别名和已验证示例
-> LLM 生成候选 SQLite SQL
-> AST、表、字段、函数和行数白名单校验
-> SQLite 只读查询
-> 结构化数据、来源、数据时点、request_id
```

## 2. 安装与配置

在 `medium` 目录执行：

```powershell
& 'C:\Users\nine\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  -m pip install `
  -r requirements.txt

Copy-Item .env.example .env
```

编辑 `.env`：

```dotenv
OPENAI_BASE_URL=https://your-openai-compatible-endpoint/v1
OPENAI_API_KEY=your-key
OPENAI_MODEL=your-model
SQLITE_DB_PATH=../data/数据库/zhangbei_energy_data.sqlite3
QUERY_TIMEOUT_SECONDS=10
MAX_RESULT_ROWS=100
AUDIT_LOG_PATH=./runtime/query_audit.jsonl
```

`OPENAI_BASE_URL` 填到兼容接口的版本路径即可，服务会调用 `${OPENAI_BASE_URL}/chat/completions`。

## 3. 启动

```powershell
& 'C:\Users\nine\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  -m uvicorn app.main:app `
  --host 127.0.0.1 `
  --port 8030
```

可用地址：

- 健康检查：`GET http://127.0.0.1:8030/health`
- OpenAPI：`http://127.0.0.1:8030/docs`
- 查询接口：`POST http://127.0.0.1:8030/api/v1/query-energy-data`

健康状态说明：

- `healthy`：SQLite 可读且模型配置完整；
- `degraded`：SQLite 或模型配置未就绪；
- 缺少模型配置时，查询接口返回 `LLM_NOT_CONFIGURED`。

## 4. 请求与响应

请求：

```json
{
  "question": "张北县已运行风电项目装机容量合计是多少？",
  "user_id": "u_10086",
  "session_id": "s_20260717_001"
}
```

成功响应：

```json
{
  "success": true,
  "data": {
    "rows": [{"total_capacity_mw": 250.0}],
    "summary": {"total_capacity_mw": 250.0},
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

错误响应：

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

## 5. 已发布数据范围

发布范围由 `config/catalog.json` 控制，当前包含：

- 已运行新能源集中式电站；
- 在建新能源与储能项目；
- 规上企业；
- 入库备案项目；
- 电网侧储能；
- 分布式光伏与风电；
- 工商业储能；
- 电网材料；
- 气象格点样本。

目录未发布的表、配置中排除的联系方式和内部导入字段，均不能被模型查询。

`config/examples.json` 保存的是少量已验证问答-SQL 示例，用于上下文增强，不是固定 SQL 模板。无论示例还是模型输出，都必须经过同一套安全校验才能执行。

## 6. 安全约束

- SQLite 以 `mode=ro` 和 `query_only=ON` 双重只读方式打开；
- 仅允许单条 `SELECT` 或 `WITH ... SELECT`；
- 校验表、字段、函数白名单；
- 拒绝写操作、多语句、注释、通配字段、PRAGMA、ATTACH、系统表和扩展加载；
- 自动限制返回行数并设置查询超时；
- 审计记录问题哈希、表、耗时、状态和错误码，不记录 SQL。

## 7. 测试

```powershell
& 'C:\Users\nine\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  -m pytest tests -v
```

测试使用假模型和临时 SQLite，不访问外部模型，也不会修改原数据库。

