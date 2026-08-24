# Query-ready v2 数据包

- `zhangbei_energy_query_ready_v2.sqlite3`：从 `data_1` 处理后工作簿构建的查询库。
- `ddl/`：逐表完整 DDL，包含字段类型、语义、单位、限制、描述和关键词。
- `llm_catalog.json`：供 LLM 选表和 SQL 上下文注入的结构化表卡。
- `data_quality_report.json`：原文到规范化字段的解析状态与 Q33 验证结果。

规则：所有 `*_raw` 是原始文本；仅 `*_parse_status=valid` 的派生数值字段可参与数值计算。
Q33 近饱和条件：`remaining_bay_count <= 1 OR remaining_access_capacity_mw <= 10`。
