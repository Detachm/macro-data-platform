# ADR 0011：CN 官方新闻采用 NBS 数据发布标题 metadata

- 状态：accepted
- 日期：2026-07-28
- 关联 Issue：[#51](https://github.com/Detachm/macro-data-platform/issues/51)

## 背景

日报必需输入 `news.cn.official_headlines_24h` 原先没有获批 live provider，因此即使其他输入完整也会
被质量门禁阻断。第一阶段需要的是可审计的中国宏观官方信息，不是覆盖交易所公司公告或转载媒体全文。
国家统计局“数据发布”列表公开标题、发布日期和官方文章链接，且不需要 token。

## 决策

- `cn.news.primary` 绑定 `CnNbsNewsProvider`，唯一入口为
  `https://www.stats.gov.cn/sj/zxfb/`，allowlist 只接受 `https://www.stats.gov.cn`。
- provider 只解析列表中的完整标题、canonical article URL 和发布日期；`summary`、`body` 均为
  `null`，`content_mode=headline`，不会请求文章正文。
- 上游列表只提供日期时写入 `published_date` 和 `time_precision=date`。它不能证明历史可见时刻，
  因此 `first_seen_at=available_at=retrieved_at`、`availability_basis=first_seen`，并声明
  `supports_point_in_time=false`；历史 PIT 请求明确拒绝。
- 稳定 `provider_record_id` 由 canonical article URL 生成。响应快照 watermark 只覆盖解析出的
  provider record、标题和发布日期，公共 continuation cursor 绑定 query、snapshot 和上一记录。
- 页面没有可解析行、日期非法、文章 URL 越过 allowlist 或 continuation 期间快照变化时 fail closed，
  不把空列表当作“没有新闻”。
- 新增必需调度任务 `cn.official-headlines`，请求报告日前一天至报告日后一天的窗口。只有该任务已提交
  的 durable provider records 才能成为 `news.cn.official_headlines_24h` 证据；最近 24 小时无标题仍
  为 `missing` 并触发既有预警流程。

## 边界

- 本来源只覆盖 NBS 官方宏观数据发布，不宣称覆盖国务院、央行、证监会、交易所或公司公告。
- SSE/SZSE/CNINFO 自动抓取仍为 fixture-only，后续扩展必须独立审批来源、字段、频率和正文权利。
- 周末与节假日不降低 CN/HK 新闻必需性；报告日政策继续遵循 ADR 0010。

## 后果与回滚

- CN 新闻从“无 live provider”变为“官方宏观标题 metadata live-ready”，但没有最近 24 小时记录时报告
  仍安全阻断，不会用旧记录或 fixture 填充。
- 上游 HTML 结构变化会显式暴露为 schema drift。回滚时解除 `cn.news.primary` 和
  `cn.official-headlines` 注册即可；已持久化事实及运行审计不删除。

## 验证

- 录制的脱敏 HTML fixture 验证响应式重复 anchor 去重、相对链接规范化、日期精度和正文为空。
- provider contract 验证 provenance、分页、`available_at <= as_of`、headline-only 和 usage rights。
- 真实官网 smoke 验证当前列表可解析；非 live CI 不访问公网。
- PostgreSQL integration 验证 CN 任务的 committed run records 可生成 available 质量证据。
