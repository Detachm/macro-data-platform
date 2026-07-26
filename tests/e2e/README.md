# End-to-end tests

本目录使用 CN、HK、US 合成 fixture 跑通：

`fixture provider → 公共 contracts → fixture repository → REST API → EditorContext`

覆盖三地区统一路由、PIT、coverage、provenance、rights 脱敏、稳定 fingerprint 和无区域私有 OpenAPI。worker 的 PostgreSQL 事务幂等、原始时区审计和 watermark 恢复由 `tests/integration/test_ingestion_checkpoint.py` 覆盖。

```bash
uv run pytest tests/e2e/test_three_region_api_smoke.py -q
```
