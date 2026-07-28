# Golden files

保存经过评审的 OpenAPI snapshot、EditorContext、新闻聚类期望结果和日报产品契约样例。更新 golden 必须在 PR 中解释语义变化，不能用无审查的批量覆盖让测试转绿。

日报 v1 的规范化样例为：

- `daily_report_v1_success.json`：完整、可发布的 `DailyReport`。
- `daily_report_v1_incomplete.json`：必需输入缺失、明确阻断发布的 `DailyReport`。
- `daily_report_v1_feishu_card.json`：由已验证报告映射得到的 Feishu 交付卡片，包含摘要、
  CN/HK/US 高亮、日程、质量状态和来源链接。

样例的字段语义和调度规则见 [`docs/daily-report-v1.md`](../../docs/daily-report-v1.md)，契约测试入口为 `tests/contract/test_daily_report_product_contract.py`。
