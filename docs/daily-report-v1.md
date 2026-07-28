# 自动化宏观日报产品契约 v1

**状态：** 冻结草案（v1.0）
**Issue：** #25
**Owner：** 项目负责人
**适用时区：** `Asia/Shanghai`

本文是自动化宏观日报的产品和运行契约，不是 provider、LLM 或飞书 API 的实现说明。后续生成器、校验器和交付器必须以本文及规范化 JSON fixture 为输入，不得各自发明字段语义。

规范化样例：

- 完整且可发布：[daily_report_v1_success.json](../tests/golden/daily_report_v1_success.json)
- 必需数据不足、阻断发布：[daily_report_v1_incomplete.json](../tests/golden/daily_report_v1_incomplete.json)
- 飞书交付卡片示例：[daily_report_v1_feishu_card.json](../tests/golden/daily_report_v1_feishu_card.json)

## 1. 产品边界和版本

日报只陈述经过平台校验的市场、宏观、日历和新闻事实，不生成交易建议、目标价、仓位、预测或收益承诺。v1 的输出契约名称固定为 `DailyReport`，版本固定为 `1.0`。

契约版本规则：

- `contract_version` 必须随每份报告输出；同一版本不得改变字段含义。
- 增加可选字段可以留在 `1.x`；删除字段、改变类型、改变状态含义必须进入新主版本。
- `report_id`、`input_snapshot.snapshot_id` 和输入 fingerprint 标识一次不可变结果。重新生成必须产生新版本，不覆盖旧结果。
- provider 名称、原始记录 ID、来源 URL 和 checksum 是 provenance；它们不能替代报告事实 ID。

## 2. 时间、日期和调度

### 2.1 字段语义

`report_date` 是报告服务日期，不等于任何单一数据的业务日期。它是 `Asia/Shanghai` 当地的 ISO 日期；例如，`2026-07-23` 的报告可以引用 7 月 22 日的中港收盘、7 月 22 日美股收盘和 7 月 23 日前已发布的宏观新闻。

| 字段 | 语义 | 约束 |
| --- | --- | --- |
| `report_date` | 报告所属的上海本地日期 | `YYYY-MM-DD`，不可用 UTC 日期直接替代 |
| `timezone` | 报告调度与日期解释的时区 | v1 固定为 `Asia/Shanghai` |
| `observed_at` | 数值或行情实际代表的时点 | 由输入事实保留，不能改成生成时间 |
| `released_at` | 官方/供应商发布事实的时点 | 可以为空；只有日期时保留日期精度，不伪造分钟 |
| `available_at` | 平台可以使用该事实的最早时点 | 必须不晚于本次快照的 cutoff 才能进入报告 |
| `generated_at` | 报告内容生成完成的时点 | UTC、有时区；不等于发布时间 |
| `publication.scheduled_publish_at` | 根据本地发布时间计算出的计划发布时间 | UTC、有时区 |
| `publication.published_at` | 实际交付成功的时点 | 未发布时为 `null` |

所有带时间的 JSON 值使用 UTC `Z`。源事实仍须通过已有公共 contract 保留其原始业务时点、来源和可用时点。

### 2.2 默认调度配置

以下是 v1 默认值；部署可以通过同名产品配置覆盖非安全参数，但覆盖值必须在运行配置和审计记录中可见：

```yaml
daily_report:
  timezone: Asia/Shanghai
  publish_time_local: "08:30:00"
  late_data_cutoff_local: "08:15:00"
  run_policy: every_calendar_day
  holiday_policy: publish_with_notice
  calendar_id: cn_hk_report_calendar_v1
  calendar_lookahead_days: 7
  freshness:
    market_close_max_age_hours: 36
    official_news_max_age_hours: 24
    macro_observation_max_age_days: 45
```

调度规则：

1. 调度器以 `Asia/Shanghai` 的 `report_date` 和本地时间计算计划时间，再转换为 UTC；不得用固定 UTC 偏移替代 timezone database。
2. `run_policy=every_calendar_day` 是默认值。周末和节假日仍生成快照；不可用的市场数据必须在 `data_quality` 和对应 section 中显式说明。
3. `run_policy=business_days_only` 时，只在 `calendar_id` 标记为工作日时运行；未确认日历状态时不猜测，结果为 `incomplete`。
4. `holiday_policy=publish_with_notice` 是默认值：节假日照常运行并在 `calendar.holiday_notice` 与质量提示中说明。`skip` 表示不创建可发布报告；`next_business_day` 表示把运行移到下一个工作日，`report_date` 使用实际运行日，不制造假日期报告。
5. `late_data_cutoff_local` 是当前报告的事实截断点。`available_at` 晚于 cutoff 的数据不进入当前 input snapshot，记录在 `data_quality.late_inputs`，等待下一次报告或人工重新生成。

每份新 input snapshot 必须固化一个 `report_day_policy`：包括 policy ID、交易日历版本、三地 session
状态、上一交易日以及 required/optional 输入集合。周末三地 market input 均为 optional；工作日只允许
把交易所日历明确判定休市的地区 market input 降为 optional。CN/HK 官方新闻和三地发布日历始终
required。日历覆盖未知或 policy 不完整时 fail closed，不按星期或旧行情猜测。

### 2.3 Freshness 规则

freshness 是相对于本次快照 cutoff 的数据年龄，而不是抓取完成时间。以下阈值是 v1 默认上限；输入注册表可以为某个数据集设置更严格的阈值，取更严格者：

| 输入类别 | 默认最大年龄 | 超过阈值 | 是否允许作为必需事实 |
| --- | ---: | --- | --- |
| 核心指数/市场收盘 | 36 小时 | `stale`，不能支撑正常发布 | 是 |
| 官方新闻标题 | 24 小时 | `stale`，对应区域降级或阻断 | 是 |
| 宏观观测 | 45 个自然日 | `stale`；若该指标按更高频率发布，使用注册表更严阈值 | 是，按 section 声明 |
| 未来发布日历 | 计划窗口为 `report_date` 起 7 天 | 不以年龄判断；日历本身不可用才是 `unavailable` | 是 |

`stale`、`late`、`unavailable` 不能静默转成空字符串、零或“暂无变化”。`data_quality` 中的每项必须是包含 `input_id`、`reason_code` 和人类可读 `reason` 的对象。

## 3. v1 最低报告数据集

这些是 #28 接入 CN/HK provider 时必须覆盖的最小 CN/HK 输入。字段名是平台事实输入 ID，不是供应商私有字段：

| 输入 ID | 区域 | 用途 | 必需 |
| --- | --- | --- | --- |
| `market.cn.core_indices.previous_close` | CN | 中国 highlights、摘要、关键变动 | 是 |
| `news.cn.official_headlines_24h` | CN | 中国 highlights 与质量判断 | 是 |
| `market.hk.core_indices.previous_close` | HK | 香港 highlights、摘要、关键变动 | 是 |
| `news.hk.official_headlines_24h` | HK | 香港 highlights 与质量判断 | 是 |
| `calendar.macro_releases_7d` | CN/HK/US | upcoming calendar、摘要 | 是 |
| `market.cn.breadth.turnover` | CN | 关键变动补充 | 否 |
| `market.hk.southbound.net_flow` | HK | 关键变动补充 | 否 |
| `macro.*.latest_key_observations` | CN/HK/US | 宏观背景补充 | 否 |
| `market.us.core_indices.previous_close` | US | 美国 highlights、摘要、关键变动 | 是 |

必需输入必须在 cutoff 前存在、通过公共 contract 并满足 freshness 规则。可选输入缺失时可以发布 `degraded` 报告，但必须在 `data_quality_notice` 中披露。`calendar.macro_releases_7d` 可以是合法的空列表，但 CN NBS、HK C&SD、US OMB/BLS + BEA 任一区域任务抓取失败、没有 durable page 或无法证明完整窗口时均为 `unavailable`。CN news 只接受 NBS 官方数据发布任务已提交的最近 24 小时标题 metadata；任务缺失、无记录或失败时阻断。不得用 fixture、历史行或合成记录代替任一日历区域。

上表的 market input “必需”指普通交易报告日。经 `report_day_policy` 明确为周末或区域休市时，对应
market input 为 optional，缺失只使报告 `degraded`，且 section 必须显示休市原因；非休市日仍按必需
输入处理。该例外不适用于新闻或宏观日历。

## 4. 输出结构

每份 `DailyReport` 顶层至少包含：

| 字段 | 类型/取值 | 说明 |
| --- | --- | --- |
| `contract_name` | `"DailyReport"` | 固定值 |
| `contract_version` | `"1.0"` | 固定版本 |
| `report_id` | string | 不可变报告版本 ID |
| `report_date` | date | 上海本地报告日期 |
| `timezone` | `"Asia/Shanghai"` | 固定值 |
| `schedule` | object | 本次采用的调度配置快照 |
| `calendar` | object | 工作日/节假日判定和提示 |
| `generated_at` | UTC timestamp | 生成完成时间 |
| `input_snapshot` | object | `snapshot_id`、`snapshot_version`、`as_of`、`cutoff_at`、`fingerprint_sha256`、本快照实际可用的 `fact_ids` |
| `status` | `complete` / `degraded` / `incomplete` | 报告事实完整性 |
| `publication` | object | `decision`、原因、计划与实际发布时间 |
| `data_quality` | object | 缺失、过期、迟到、修订和不可用输入清单 |
| `sections` | object | 下表规定的 8 个 section，全部必须出现 |

每个 section 至少包含 `section_id`、`status`、`character_count` 和 `max_characters`。有数字、日期、方向或引用的内容必须同时提供 `fact_ids` 和 `source_ref_ids`；生成器不得只在自然语言中埋入无法追踪的事实。

`character_count` 按用户可见文本的 Unicode code point 计数：section 的 `text`，或 `items[]` 中的 `label`、`text`、`name` 字段串联后计数；来源引用的 ID、URL、checksum、时间和其他元数据不计入。实现可以额外设置更低上限，但不能超过本表上限。

| Section ID | 必需输入 | 可发布条件与降级 | 最大可见字符 |
| --- | --- | --- | ---: |
| `executive_summary` | 三地核心指数、未来 7 天发布日历 | 缺任何一项则 `incomplete`；可选数据缺失则 `degraded` | 800 |
| `cn_highlights` | CN 核心指数、CN 官方标题 | 两项均可用为 `complete`；标题迟到/过期为 `degraded`，核心指数不可用为 `unavailable` | 1,000 |
| `hk_highlights` | HK 核心指数、HK 官方标题 | 同上 | 1,000 |
| `us_highlights` | US 核心指数 | 核心指数不可用为 `unavailable`；新闻或补充指标缺失为 `degraded` | 1,000 |
| `key_movements` | 通过校验的数值、方向、单位和来源 | 无可验证变动则空 `items` 并标记原因；不得猜测方向 | 1,200 |
| `upcoming_calendar` | `calendar.macro_releases_7d` | 合法空列表仍为 `complete`；日历数据集不可用为 `unavailable` | 1,600 |
| `data_quality_notice` | 快照质量和 freshness 结果 | 始终输出；完整报告也必须明确“无问题” | 600 |
| `source_references` | 本报告实际使用的来源记录 | 每个引用必须能回到公共 `SourceRef` 和输入 snapshot | 4,000 |

### 4.1 来源引用规则

`sections.source_references.items[]` 使用已有 `SourceRef` 的语义，至少包括 `source_ref_id`、`provider_id`、`provider_record_id`、`source_name`、`source_url`（若来源没有 URL 则为 `null`）、`retrieved_at` 和 `checksum_sha256`。报告可以保留 legacy rights 元数据，但不得输出 token、Cookie、账号、密码或供应商凭据。

每个报告事实的 `fact_id` 必须出现在本次 `input_snapshot.fact_ids` 中；snapshot 不得以未声明的聚合 ID 代替实际事实。`source_ref_ids` 必须是 `sections.source_references.items[]` 中存在的 ID；没有来源 ID 的数字、日期、方向或引用不能发布。

## 5. 数据质量、修订和发布决策

### 5.1 状态定义

- `complete`：所有必需输入可用、未过期、通过契约校验，所有 section 满足要求；可以 `published`。
- `degraded`：必需输入满足要求，但可选输入缺失、过期、节假日无行情或存在已披露修订；可以发布，但必须列出原因。
- `incomplete`：任何必需输入为 `missing`、`stale`、`late`、`unavailable` 或 schema/质量校验失败；`publication.decision` 必须是 `not_published`。
- `unavailable`：没有任何可安全使用的事实。section 可以使用此状态，但不得伪造空白成功结果。

### 5.2 不完整、过期和迟到

1. 必需输入缺失或不可用时，记录精确的输入 ID 和原因代码 `REQUIRED_INPUT_UNAVAILABLE`，完整报告不发布。
2. 必需输入过期时，记录在 `stale_inputs`，不能用旧值冒充当前值。可选输入过期可以发布 `degraded`。
3. 输入在 cutoff 后才到达时，记录在 `late_inputs`，不进入本次 snapshot；“迟到”不能被标记为“无数据正常”。
4. 未发布结果仍然是可审计的 `DailyReport` 版本；Feishu 不发送正文，只可发送“不完整/未发布”的运维提示。

### 5.3 修订和重新生成

- 同一统计期有多个合法 vintage 时，选择 `available_at <= cutoff` 的最新版本，并将 `fact_id`、vintage 和 `revised_inputs` 保留在审计信息中。
- cutoff 之后才出现的修订不回写已经发布的报告；下一次报告或人工重新生成产生新 `report_id`。
- 人工重新生成必须引用新的 `input_snapshot`，不得覆盖已发布或已拒绝版本。
- 任何被拒绝的版本及其错误原因都必须保留，方便重放和运营排查。

## 6. Feishu 交付边界

`tests/golden/daily_report_v1_feishu_card.json` 是把已验证报告映射为 Feishu 卡片的
冻结示例。它展示报告日期、摘要、CN/HK/US 高亮、未来日程、数据质量和可点击的来源
链接；它不定义 Feishu API 调用、鉴权或重试策略。

交付器必须遵守：

- 只消费 `publication.decision=published` 的报告正文；`not_published` 只发送明确的阻断提示或不发送。
- 卡片文字必须来自已通过校验的 section，不能在交付层重新查询 provider 或补写事实。
- 卡片不得包含凭据、内部数据库连接信息或未经 `source_references` 证明的数字。

## 7. 明确不属于本任务

- provider adapter、实时数据源接入：由 #26、#28 等任务负责。
- raw/audit 持久化和报告版本存储：由 #27 负责。
- LLM client、prompt、结构化生成：由 #30 负责。
- 事实校验、fallback 和最终发布阻断：由 #31 负责。
- Feishu API 调用和发送凭据：不在本产品契约内。

本任务的后续实现必须通过 `tests/contract/test_daily_report_product_contract.py`，并保持两个 canonical JSON fixture 与本文同步。
