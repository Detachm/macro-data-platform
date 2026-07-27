# Macro Data Platform

从零开发的宏观数据与新闻后端，为宏观总编 LLM 提供 A股、港股、美股、宏观发布和每日新闻的统一事实接口。

项目无图形界面。外部数据由 worker 采集，API 只查询已经标准化入库的数据。

## 快速开始

要求：Python 3.12+、`uv`、Docker。

```bash
cp .env.example .env
docker compose up -d postgres
uv sync --dev
uv run alembic upgrade head
uv run uvicorn macro_platform.api.app:create_app --factory --reload
```

健康检查：

```bash
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```

运行测试：

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy --strict src
uv run pytest -m "not live" --cov=macro_platform --cov-report=term-missing
```

完整接口、测试与分工要求见 [工程执行规范](docs/ENGINEERING_SPEC_ZH.md)。

> 当前目录是可独立建仓的项目根；放在其他仓库子目录中时，内部 `.github/` 工作流和 CODEOWNERS 不会生效。正式开发前请把本目录单独初始化为仓库，并将 CODEOWNERS 占位账号替换为真实 GitHub 账号或团队。

## 当前 MVP

- 严格 Pydantic contracts 与统一 API envelope。
- Market/Macro/News Provider Protocol 与注册表。
- FastAPI health、capabilities、数据查询和 EditorContext 路由。
- PostgreSQL/SQLAlchemy/Alembic 初始结构。
- worker/job 输入输出、事务幂等、原始时区审计和 watermark 恢复。
- CN、HK、US 三地区离线 fixture provider 与共享 contract suite。
- 三地区统一 REST API / EditorContext fixture smoke。
- Docker Compose、CI、CODEOWNERS、PR/ADR/数据源模板。

本轮完成的是无需真实凭据的 fixture-backed 数据底座；fixture provider 不会绑定生产 role，也不声明 live-ready。默认服务仍使用 `EmptyDataRepository` 并在 EditorContext 中明确标注 `unavailable`。真实供应商适配、完整 PostgreSQL 查询仓库和生产调度属于 Phase 2，API 请求不得临时直连上游。

复现三地区统一接口验收：

```bash
uv run pytest tests/e2e/test_three_region_api_smoke.py -q
```

## US fixture contract evidence

US fixture provider 的合同测试默认离线、无需真实凭据或数据库。在完成 `uv sync --dev` 后运行：

```bash
uv run pytest tests/unit tests/contract -m "not live" -q
```

如只复现 US provider 的共享合同证据，运行：

```bash
uv run pytest tests/contract/test_us_fixture_provider_contract.py -q
```

Fixture 文件和所覆盖的错误/时间场景记录在
`tests/fixtures/us/provider/manifest.json`。默认 CI 不运行任何 live smoke；如需联网，必须遵循
`docs/data-sources/us-mvp.md` 的 Phase 2 审批、凭据和限速要求。
生产来源与外部 LLM 的准入以
[`docs/data-sources/production-source-policy.md`](docs/data-sources/production-source-policy.md)
及其机器可读策略为准，缺失或未批准的条目默认拒绝。
