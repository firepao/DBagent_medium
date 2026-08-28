# CI 部署门禁

## 门禁

`.github/workflows/quality.yml` 在每次 push 和 pull request 执行：

- Python 3.12 依赖安装；
- `pip-audit -r requirements.txt`；
- 全量 pytest；
- 使用隔离临时 SQLite/DDL fixture 检查 Compose 配置；
- Docker 构建运行镜像；
- Trivy 扫描镜像 HIGH/CRITICAL 漏洞（未修复项不阻断）。

该阶段落地时应用迁移到 schema v2 并创建持久化 `run_events` 表；当前平台已继续演进到 schema v5。CI 的全量测试覆盖迁移和 request ID 重放 API。

## 数据边界修复

此前 Dockerfile 依赖仓库父目录中的业务数据库，GitHub Actions checkout 后无法复现。现已改为：

- 镜像只包含服务代码、配置和页面；
- Compose 通过 `ENERGY_DB_PATH`、`ENERGY_DDL_PATH` 只读挂载业务数据；
- CI 使用最小隔离数据库验证镜像构建，不伪造生产数据；
- 生产部署可以将数据快照放在镜像外独立备份和更新。

当前本机没有 Docker CLI，CI 文件是对容器实际 build、healthcheck 和 Trivy 扫描的可复现门禁，但本地不能声称这些步骤已执行。
