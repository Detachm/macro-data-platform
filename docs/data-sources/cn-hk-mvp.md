# CN/HK 两日 MVP 数据源、端点与授权矩阵

Owner: @Nouzee
Issue: #1 P0
状态: review，冻结草案，已进入实习生 B 交叉评审
区域: CN / HK
Provider roles: `cn.instruments.primary`, `cn.bars.primary`, `cn.macro.primary`, `cn.news.primary`, `hk.instruments.primary`, `hk.bars.primary`, `hk.macro.primary`, `hk.news.primary`
数据集: instruments / bars / macro_series / macro_observations / macro_releases / news
采购/合同负责人: 项目负责人
账号负责人: 项目负责人；两日 MVP 仅允许无凭据公开 API 或合成/脱敏 fixture
首次批准日期: 待项目负责人批准
复核日期: 2026-07-23 review
允许服务器运行: 仅 `live-ready` 来源允许；`fixture-only` 和 `gap` 来源禁止生产调度
保留期限: fixture 随仓库生命周期；open-data 标准化事实随数据库保留策略；未采购市场数据不保留
要求署名: API/报告层保留 `source.source_name` 和 `source.source_url`

本文冻结中国内地和香港两日 MVP 的来源范围、端点、授权边界、公共 contract 映射、稳定 ID 和 checksum 规则。adapter 必须只输出 `src/macro_platform/contracts/` 中已有公共模型，不得新增区域 DTO，不得改变 `available_at`、UTC `Z` 和左闭右开区间语义。

## 决策摘要

| 区域 | 数据类 | 两日 MVP 结论 | 主来源 | fallback | 两日内凭据 |
|---|---|---|---|---|---|
| CN | instruments | fixture-only；可用官方网页建立脱敏/合成 fixture，暂不承诺自动 live 抓取 | SSE/SZSE 股票列表 | CNINFO 公司要览，仅校验 alias | 无 token；无自动抓取授权审批 |
| CN | daily bars | `live-ready`，但只覆盖 3 个批准的核心指数；个股、行业和其他指数仍为 gap | BaoStock `query_history_k_data_plus` | SSE/SZSE 授权 EOD 产品用于扩展覆盖 | BaoStock 无需注册；无 API token |
| CN | macro/releases | release calendar live-ready；macro observation 先 fixture-only | NBS 发布日程、NBS 数据发布库 | NBS 新闻稿页面 | 无 token；EasyQuery 无官方外部 API 文档，observation 不 live-ready |
| CN | news/announcements | NBS 数据发布标题 metadata live-ready；交易所/公司公告仍 fixture-only；禁止保存正文 | NBS 数据发布 | SSE/SZSE/CNINFO 公告检索页 | NBS 无 token；只读取标题、链接和发布日期 |
| HK | instruments | fixture-only；HKEX 公共证券列表可人工 fixture，付费 master file 未采购 | HKEX Securities Lists | HKEX Securities Master File | 无 Data Marketplace/Historical Data 订阅 |
| HK | daily bars | `live-ready`，仅覆盖 10 个批准的核心港股；其他标的仍为 gap | 项目托管 XtQuant data-centre（迅投）`1d` | HKEX Data Marketplace | macro worker 不持有 token；只连接已运行的 XtQuant 服务 |
| HK | macro/releases | allowlisted C&SD adapter live-ready；release-calendar coverage requires a separately approved source | DATA.GOV.HK/C&SD API、HKMA Open API | HKMA Economic & Financial Data page | 无需申请、注册或认证 |
| HK | news/announcements | HKMA press releases live-ready；HKEX issuer announcements fixture-only | HKMA Press Releases API；HKEXnews title search | HKEX IIS paid feed | HKMA 无需凭据；HKEX IIS 无凭据 |

`live-ready` 只表示两日内可在不写入 secret 的前提下实现在线 smoke。`fixture-only` 表示只能用合成或脱敏 fixture 验证 parser/normalizer，不得让 worker 在生产调度中调用该来源。

## 官方依据

- SSE 股票列表: https://www.sse.com.cn/assortment/stock/list/share/
- SSE 数据产品与历史数据: https://english.sse.com.cn/markets/dataservice/products/
- SSE 法律声明: https://www.sse.com.cn/home/legal/
- SSE InfoNet Level-1 行情: https://www.sseinfo.com/services/assortment/level1/
- SZSE 数据服务: https://investor.szse.cn/English/services/dataServices/index.html
- SZSE 技术服务/接口规范: https://www.szse.cn/marketServices/technicalservice/
- SZSE 法律声明: https://www.szse.cn/application/laws/
- CNINFO: https://www.cninfo.com.cn/new/index.jsp
- NBS 数据发布日程: https://www.stats.gov.cn/sj/fbrc/bnxxfb/
- NBS 数据发布库: https://data.stats.gov.cn/
- NBS 最新数据发布: https://www.stats.gov.cn/sj/zxfb/
- NBS 服务条款: https://www.stats.gov.cn/wzgl/202302/t20230217_1912857.html
- HKEX Securities Lists: https://www.hkex.com.hk/services/trading/securities/securities-lists?sc_lang=en
- HKEX Historical Data Products: https://www.hkex.com.hk/eng/ods/historicalData.aspx
- HKEX Data Licensing: https://www.hkex.com.hk/Services/Market-Data-Services/Real-Time-Data-Services/Data-Licensing?sc_lang=en
- HKEX Getting Market Data FAQ: https://www.hkex.com.hk/Global/Exchange/FAQ/Market-Data/Getting-Market-Data?sc_lang=en
- HKEX Terms of Use: https://www.hkex.com.hk/global/exchange/terms-of-use?sc_lang=en
- HKEXnews About: https://www2.hkexnews.hk/Global/Exchange/About-Us?sc_lang=en
- HKEXnews title search: https://www1.hkexnews.hk/search/titlesearch.xhtml
- HKEX IIS: https://www.hkex.com.hk/Services/Market-Data-Services/Infrastructure/Issuer-Information-feed-Service-%28IIS%29?sc_lang=en
- DATA.GOV.HK API specification: https://data.gov.hk/en/help/api-spec
- DATA.GOV.HK Terms: https://data.gov.hk/en/terms-and-conditions
- C&SD CPI API resource example: https://www.censtatd.gov.hk/api/get.php?id=510-60004&lang=en&full_series=1
- HKMA API documentation: https://apidocs.hkma.gov.hk/documentation/
- HKMA API overview: https://apidocs.hkma.gov.hk/abouthkmasapi/
- HKMA Press Releases API: https://apidocs.hkma.gov.hk/documentation/press-releases/
- HKMA daily monetary statistics example: https://apidocs.hkma.gov.hk/documentation/market-data-and-statistics/daily-monetary-statistics/efbn-closing/

## 全局接入规则

- 不写 token、Cookie、账号、验证码绕过逻辑或受限正文到代码、fixture、日志、文档。
- 外部来源原始时间保留本地时区解释，入库/API 统一 UTC `Z`。
- `available_at` 只使用可证明的上游发布时间、交易所发布时间、供应商分发时间或平台首次看到时间；无法证明时必须 `availability_basis=first_seen`。
- 所有公开网页类来源默认 `pagesize=source default`，adapter 内部分页游标必须使用公共 `ProviderPage.next_cursor`，cursor 绑定 query hash、as_of、snapshot_at 和最后排序键。
- 429/403/登录页伪 200/schema drift 必须阻断对应 provider，不得批量写空字段。
- 未获正文权限的新闻/公告 `body=null`，`content_mode=headline` 或 `snippet`。

### 状态定义

| 状态 | ProviderHealth | ProviderCapabilities | 调度规则 |
|---|---|---|---|
| `live-ready` | `ok` 或 `degraded` | 可登记对应 dataset；`supports_point_in_time` 只在 `available_at` 可证明时为 true | 可在线 smoke，可进生产调度 |
| `fixture-only` | `not_configured` | 不登记 live capability；只能用于 fixture provider | 禁止 worker 在线调用 |
| `gap` | `not_configured` | 不登记 capability | 禁止实现 live adapter，直到补采购/授权 ADR |

### 必填公共字段补齐规则

以下字段不依赖区域私有 DTO，adapter 必须按本节补齐。

| Contract | 公共字段 | 统一映射 |
|---|---|---|
| `Instrument` | `region` | 来源属地固定为 `CN` 或 `HK` |
| `Instrument` | `timezone` | CN=`Asia/Shanghai`; HK=`Asia/Hong_Kong` |
| `Instrument` | `status` | 明确上市/交易中=`active`; 明确暂停=`suspended`; 明确退市=`delisted`; 无法判定=`unknown` |
| `Instrument` | `source.provider_id` | 本文件 provider role 去掉 dataset role 后的具体来源 ID，例如 `sse_public_list`, `hkex_securities_lists` |
| `Instrument` | `source.provider_record_id` | `<provider_id>:<canonical_symbol>:<valid_from>`；无 `valid_from` 的 instrument 记录拒绝 |
| `Instrument` | `source.checksum_sha256` | 按“稳定 ID 和 checksum 规则”计算 |
| `MarketBar` | `region`, `interval`, `currency`, `availability_basis`, `source` | region/货币来自 instrument 或交易柜台；两日 MVP 仅 `interval=1d`, `adjustment=raw`; availability/source 按来源表 |
| `MacroSeries` | `authority`, `frequency`, `transformation`, `seasonal_adjustment`, `source` | CN=`NBS`; HK=`CENSTATD` 或 `HKMA`; 频率/变换/季调必须在 series registry 中人工登记，未登记拒绝 |
| `MacroObservation` | `availability_basis`, `transformation`, `source`, `vintage_id`, `revision_no`, `value_status` | transformation 来自 series registry；首发 `revision_no=0`; 无修订证据时 `value_status=preliminary` 或按官方状态 |
| `MacroRelease` | `source`, `unit`, `status` | unit 来自 series registry；状态按官方发布/日程文本 |
| `NewsEvent` | `language`, `regions`, `availability_basis`, `content_hash_sha256`, `usage_rights` | 语言来自端点参数/页面语言；regions 固定包含来源区域；hash/rights 按本文规则 |

## CN instruments

### 来源与接口

| 项 | primary | fallback |
|---|---|---|
| Provider role | `cn.instruments.primary` | `cn.instruments.fallback` |
| 来源 | SSE 股票列表、SZSE 股票列表/产品目录 | CNINFO 公司要览或公告页，只做 alias/名称交叉校验 |
| 状态 | fixture-only | fixture-only |
| 官方文档 | SSE 股票列表、SZSE 官网产品目录和技术服务页 | CNINFO 首页关于说明 |
| Base URL/端点 | `https://www.sse.com.cn/assortment/stock/list/share/`; `https://www.szse.cn/market/product/stock/list/index.html` | `https://www.cninfo.com.cn/new/index.jsp` |
| 请求参数 | 市场/板块、证券代码、地区等网页查询条件；adapter 不得依赖未登记的隐藏 API | 证券代码、简称、关键词 |
| 分页 | 网页表格分页；fixture 固定单页和重复页场景 | 网页表格分页；仅 fixture |
| 限流 | 未公布；fixture-only 时不在线调用。若后续批准 live，默认 1 rps、串行、指数退避 | 同 primary |
| 历史深度 | 官网当前列表为主；历史 alias 需从公告/历史快照重建，MVP 不承诺完整历史 | 当前和公告线索 |
| 时区 | `Asia/Shanghai` | `Asia/Shanghai` |
| 更新时间 | 当前网页更新，不提供稳定 SLA | 当前网页更新 |
| available_at basis | fixture 使用 `first_seen`；后续 live 若能证明网页更新时间，用 `exchange_published`，否则 `first_seen` | `first_seen` |
| 凭据 | 无 token；未取得自动化抓取审批 | 无 token；未取得自动化抓取审批 |

### 上游字段到公共 contracts 映射

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| 证券代码 / code | `Instrument.local_symbol` | 6 位代码保留前导 0 | 是 | 缺失拒绝记录 |
| 交易所 | `venue_mic` | SSE=`XSHG`; SZSE=`XSHE` | 是 | 缺失拒绝记录 |
| 证券代码 + 交易所 | `canonical_symbol` | `<MIC>:<LOCAL_SYMBOL>`，如 `XSHG:600519` | 是 | 缺失拒绝记录 |
| 证券简称 | `name` | 原文保留；去首尾空白 | 是 | 缺失拒绝记录 |
| 英文简称 | `name_en` | 有则填，无则 `null` | 否 | `null` |
| 板块/证券类别 | `asset_class` | A 股股票=`equity`; ETF=`etf`; 指数不进 instruments MVP | 是 | 未识别拒绝记录 |
| 币种 | `currency` | A 股=`CNY`; B 股若纳入按交易币种 | 是 | 缺失拒绝记录 |
| 上市日期 | `listed_on`, `valid_from` | 日期精度，不伪造时间戳 | 是 | 缺失拒绝记录；不得用抓取日期代替 |
| 终止上市日期 | `delisted_on`, `valid_to`, `status` | 有退市日期则 `delisted`; 否则 `active/unknown` | 否 | `null`/`unknown` |
| 原始 URL/页面 | `source.source_url` | 记录官方页面 URL | 是 | 缺失拒绝记录 |

## CN daily bars

### 来源与接口

| 项 | primary | fallback |
|---|---|---|
| Provider role | `cn.bars.primary` | `cn.bars.fallback` |
| 来源 | BaoStock Python client 的 `query_history_k_data_plus` | SSE/SZSE 授权 EOD 产品，用于扩大个股和指数覆盖 |
| 状态 | live-ready；仅 `sh.000001`、`sh.000300`、`sz.399001` 三个静态 allowlist 映射 | gap |
| 官方文档 | [BaoStock](https://www.baostock.com/)；项目内 adapter 版本固定为 `baostock 0.9.x` | 采购后登记产品说明和端点 |
| Base URL/端点 | BaoStock stateful Python client；adapter 不直接拼接未登记 HTTP URL | 按合同 |
| 请求参数 | `code`, `start_date`, `end_date`, `frequency=d`, `adjustflag=3`；只接受 raw 日线 | 按合同 |
| 分页 | 上游单次请求按代码和日期窗口；adapter 以签名 cursor、结果 checksum 和前驱 `bar_id` 做本地 continuation | 按合同 |
| 限流 | 上游未公布 SLA；每个 client session 串行，30 秒 deadline，错误后由 job retry 策略处理 | 按合同 |
| 历史深度 | adapter 单个日期窗口最多 5 个日历年；实际可用历史范围以 BaoStock 返回为准 | 按合同 |
| 时区 | `Asia/Shanghai` | 按合同 |
| 更新时间 | EOD/日终；不声称上游分发时刻 | 按合同 |
| available_at basis | `retrieved_at`，`availability_basis=first_seen`；不支持历史 PIT snapshot | 按合同 |
| 凭据 | 无账号、token 或 secret；登录/登出仅为 BaoStock client session | 按合同 |

### 上游字段到公共 contracts 映射

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| `code` | `MarketBar.canonical_symbol`, `instrument_id` | 仅静态 mapping: `sh.000001`→`XSHG:000001`、`sh.000300`→`XSHG:000300`、`sz.399001`→`XSHE:399001` | 是 | 未在 allowlist 或返回 symbol 不匹配时拒绝记录 |
| 交易日期 | `trading_date`, `bar_start`, `bar_end` | 日期精度；`bar_start` 为交易日开盘时刻 UTC，`bar_end` 为收盘时刻 UTC | 是 | 缺失拒绝记录 |
| 开盘价/最高价/最低价/收盘价 | `open/high/low/close` | Decimal 字符串；不使用 float | 是 | 缺失拒绝记录 |
| 成交量 | `volume` | 股/份，非负 Decimal | 否 | `null`，加 flag |
| 成交金额 | `turnover` | CNY，非负 Decimal | 否 | `null`，加 flag |
| 均价 | `vwap` | 若上游无字段则不自行计算 | 否 | `null` |
| 复权标识 | `adjustment` | 首期只允许 `raw` | 是 | 非 raw 且无 `adjustment_as_of` 拒绝 |
| 本次抓取时间 | `available_at`, `source.retrieved_at` | 转 UTC，`availability_basis=first_seen` | 是 | 无时间则拒绝记录 |

## CN macro/releases

### 来源与接口

| 项 | primary | fallback |
|---|---|---|
| Provider role | `cn.macro.primary` | `cn.macro.fallback` |
| 来源 | NBS 发布日程、NBS 数据发布库 | NBS 新闻稿/最新发布页面 |
| 状态 | release calendar live-ready；macro observations fixture-only | fixture-only for observation backfill |
| 官方文档 | NBS 发布日程、NBS 数据发布库、NBS 服务条款 | NBS 最新发布页面 |
| Base URL/端点 | release calendar: `https://www.stats.gov.cn/sj/fbrc/bnxxfb/`; data portal: `https://data.stats.gov.cn/` | `https://www.stats.gov.cn/` 最新发布/新闻稿页面 |
| 请求参数 | calendar 无 API 参数；EasyQuery 仅作为未正式登记的候选: `m`, `dbcode`, `rowcode`, `colcode`, `wds`, `dfwds` | 年/月/栏目/关键词网页条件 |
| 分页 | calendar 页面按年份；数据发布库/新闻页按页面分页 | 网页分页 |
| 限流 | 无公开 API SLA；calendar live 默认 0.2 rps、每日一次；EasyQuery 不进 live | 0.2 rps、每日一次 |
| 历史深度 | 发布日程公开多个年份；宏观序列深度以 NBS 数据发布库可见为准 | 新闻稿历史以站内页面为准 |
| 时区 | `Asia/Shanghai` | `Asia/Shanghai` |
| 更新时间 | NBS 日程列出具体发布日期和北京时间；数据发布库通常滞后更新，按 NBS 注释 | 页面发布时间 |
| available_at basis | release: 有 NBS 页面/数据发布库发布时间用 `provider_disseminated`; 仅抓到页面时用 `first_seen`; actual observation 无证明时 `first_seen` | `first_seen` |
| 凭据 | 无需 token；但 EasyQuery 无官方外部 API 文档，observation 不 live-ready | 无需 token；不保存正文 |

### 上游字段到公共 contracts 映射

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| 指标名称 | `MacroSeries.name`, `MacroRelease.release_name` | 原文保留，必要时映射到固定 `series_id` | 是 | 缺失拒绝 |
| 指标代码/表号 | `MacroSeries.code`, `series_id` | `macro:CN:NBS:<CODE>`；无官方代码时用人工登记代码 | 是 | 未登记不得入库 |
| 发布日期/发布时间 | `scheduled_at` / `scheduled_date`, `released_at`, `available_at` | 北京时间转 UTC；只有日期时填 `scheduled_date`、`time_precision=date`，不伪造 midnight timestamp | 是 | 无 release 时间则 `released_at=null`, `available_at=first_seen_at` |
| 统计期 | `period_start`, `period_end` | 月/季/年按自然统计期 | 是 | 缺失拒绝 |
| 数值 | `actual`, `MacroObservation.value` | Decimal；百分比单位为 `percent` 中的数值，如 5.2 表示 5.2% | 否 | 未发布时 `actual=null`, status=`scheduled` |
| 单位 | `unit` | NBS 原单位规范化: `percent`, `index`, `CNY`, `person` 等 | 是 | 未登记拒绝 |
| 修订状态 | `revision_no`, `value_status`, `vintage_id` | C&SD live adapter 仅稳定标识当前值，不声称可获取修订序列；后续同统计期新值需由已批准的历史快照来源补齐 | 是 | 无法判定则 `quality_flags` |

## CN news/announcements

### 来源与接口

| 项 | primary | fallback |
|---|---|---|
| Provider role | `cn.news.primary` | `cn.news.fallback` |
| 来源 | NBS 数据发布 | SSE/SZSE/CNINFO 公告检索页 |
| 状态 | headline metadata live-ready | fixture-only |
| 官方文档 | NBS 最新数据发布、NBS 服务条款 | SSE/SZSE 法律声明、CNINFO 关于说明 |
| Base URL/端点 | `https://www.stats.gov.cn/sj/zxfb/` | SSE/SZSE 公告页、`https://www.cninfo.com.cn/new/index.jsp` |
| 请求参数 | 无 API 参数；每日读取最新发布列表首页 | 证券代码、公告类型、开始/结束日期、关键词；不得保存 Cookie |
| 分页 | 上游按 `index_N.html` 归档；日报 24 小时窗口只读取最新列表快照，公共 cursor 仅对该快照切页 | 网页分页；必须检测空页循环和重复页 |
| 限流 | 每个调度任务一次 GET；超时 30 秒 | fixture-only 不在线调用 |
| 历史深度 | 当前列表页；不承诺历史 PIT，回填请求拒绝 | 以网站搜索可查范围为准，不承诺完整 |
| 时区 | `Asia/Shanghai` | `Asia/Shanghai` |
| 更新时间 | 页面发布日期只有日期精度 | 公告披露时间/页面发布时间 |
| available_at basis | `first_seen`；`published_date` 不冒充可见时刻 | 有披露时间用 `exchange_published`; 否则 `first_seen` |
| 凭据 | 无 token；只保存标题、官方链接、发布日期和首次发现时间；正文与摘要均为 `null` | 无 token；不保存正文 |

### 上游字段到公共 contracts 映射

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| 发布标题 | `NewsEvent.title` | 优先使用桌面版 anchor 的完整 `title` 属性；原文保留 | 是 | 缺失拒绝并标记 schema drift |
| 公告摘要 | `summary` | 仅有授权 snippet 时填；否则 `null` | 否 | `null` |
| PDF/正文 | `body` | MVP 一律 `null` | 否 | 不保存 |
| 发布日期 | `published_date`, `first_seen_at`, `available_at` | ISO 日期写入 `published_date`、`time_precision=date`；`available_at=first_seen_at` | 是 | 日期非法时拒绝整个快照 |
| 官方文章链接 | `canonical_url`, `source.source_url`, `provider_record_id` | 相对链接基于列表页解析，只允许 `https://www.stats.gov.cn`；稳定 ID 由 canonical URL 生成 | 是 | 越过 host allowlist 时阻断快照 |
| 股票代码 | `entities[]` | 解析为 instrument entity，confidence=`1` | 否 | 未解析则仅保留 mention |
| 来源名称 | `source_name`, `source_tier` | SSE/SZSE/CNINFO=`official` | 是 | 缺失拒绝 |
| URL | `canonical_url`, `source.source_url` | canonicalize URL，去 tracking 参数 | 是 | 缺失拒绝 |
| 分类 | `topics`, `vendor_annotations` | 官方公告分类进 `topics`；供应商标签只能进 `vendor_annotations` | 否 | 空列表 |

## HK instruments

### 来源与接口

| 项 | primary | fallback |
|---|---|---|
| Provider role | `hk.instruments.primary` | `hk.instruments.fallback` |
| 来源 | HKEX Securities Lists / Full List of Securities | HKEX Securities Master File |
| 状态 | fixture-only | gap |
| 官方文档 | HKEX Securities Lists | HKEX Historical Data Products / Data Marketplace |
| Base URL/端点 | `https://www.hkex.com.hk/services/trading/securities/securities-lists?sc_lang=en` | 采购后由 HKEX Historical Data/Data Marketplace 提供下载/API |
| 请求参数 | 公共网页/下载链接；语言 `sc_lang=en|zh-HK|zh-CN` | 合同提供产品、日期、文件名/API 参数 |
| 分页 | 文件下载无分页；网页列表按链接 | 文件按交易日分片 |
| 限流 | 未公布；fixture-only 不在线调用。后续批准 live 默认每日一次 | 按合同 |
| 历史深度 | 公共列表为当前；Master File 为订阅期内每日文件 | 按订阅 |
| 时区 | `Asia/Hong_Kong` | `Asia/Hong_Kong` |
| 更新时间 | 公共网页不保证 SLA；Master File 通常交易日约 23:30，最迟次日 03:00 | 按产品说明 |
| available_at basis | 公共网页 fixture 使用 `first_seen`; Master File 用 `provider_disseminated` | `provider_disseminated` |
| 凭据 | 无 token；未取得自动化抓取审批 | 无 HKEX 订阅账号 |

### 上游字段到公共 contracts 映射

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| Stock Code | `local_symbol` | 5 位港股代码保留前导 0，如 `00700` | 是 | 缺失拒绝 |
| Market | `venue_mic` | SEHK securities=`XHKG` | 是 | 缺失拒绝 |
| Stock Code + MIC | `canonical_symbol` | `XHKG:<LOCAL_SYMBOL>` | 是 | 缺失拒绝 |
| English/Chinese Short Name | `name`, `name_en` | 英文优先填 `name_en`，中文/英文可按 source 语言填 `name` | 是 | 缺失拒绝 |
| Security Type | `asset_class` | Equity=`equity`; ETF=`etf`; REIT=`equity` + topic/flag，后续可扩 | 是 | 未识别拒绝或 flag |
| Trading Currency | `currency` | HKD/RMB/USD | 是 | 缺失拒绝 |
| Board/listing markers | `status` | 停牌不等于 delisted；仅标 `status` | 否 | `unknown` |
| Lot size | `lot_size` | Decimal | 否 | `null` |
| Listing Date | `listed_on`, `valid_from` | 日期精度 | 是 | 缺失拒绝记录；不得用抓取日期代替 |

## HK daily bars

### 来源与接口

| 项 | primary | fallback |
|---|---|---|
| Provider role | `hk.bars.primary` | `hk.bars.fallback` |
| 来源 | 项目托管 XtQuant data-centre（迅投 xtquant Python SDK） | HKEX Data Marketplace，用于扩大覆盖或替换内部运行时 |
| 状态 | live-ready；仅 `00700.HK`、`09988.HK`、`03690.HK`、`01810.HK`、`00941.HK`、`00005.HK`、`00388.HK`、`01299.HK`、`02318.HK`、`09618.HK` | gap |
| 官方文档 | 项目内 Beast XtQuant 实现及随 SDK 发布的 `xtdata` 文档 | 按合同 |
| Base URL/端点 | 本机或私有网络 XtQuant data-centre；worker 只调用 `connect(host, port)`，不启动、停止或清理端口 | 按合同 |
| 请求参数 | `stock_list`, `period=1d`, `start_time`, `end_time`, `dividend_type=none`, `fill_data=false` | 按合同 |
| 分页 | 上游按代码/日期窗口批量下载；adapter 以签名 cursor、结果 checksum 和前驱 `bar_id` 做本地 continuation | 按合同 |
| 限流 | 由内部数据中心和上游账户配额管理；adapter 串行访问同一 SDK client，30 秒 deadline | 按合同 |
| 历史深度 | adapter 单个日期窗口最多 5 个日历年；实际范围由 XtQuant 服务和本地缓存决定 | 按合同 |
| 时区 | `Asia/Hong_Kong` | 按合同 |
| 更新时间 | 日线缓存抓取完成时间；不声称上游分发时刻 | 按合同 |
| available_at basis | `retrieved_at`，`availability_basis=first_seen`；不支持历史 PIT snapshot | 按合同 |
| 凭据 | data-centre 进程持有 `XTQUANT_TOKEN`；macro worker 不读取或记录 token | 按合同 |

### 上游字段到公共 contracts 映射

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| `00700.HK` 等 XtQuant code | `canonical_symbol`, `instrument_id` | 10 条静态 mapping，例如 `00700.HK`→`XHKG:00700` | 是 | 未在 allowlist 或响应 symbol 不匹配时拒绝 |
| `index` / `time` | `trading_date`, `bar_start`, `bar_end` | `index` 为 `YYYYMMDD`；epoch milliseconds 与香港日期必须一致；标准交易日按 09:30–16:00 HK 转 UTC | 是 | 缺失、冲突或无法转换时拒绝 |
| Open/High/Low/Close | `open/high/low/close` | Decimal；币种为交易柜台币种 | 是 | 缺失拒绝 |
| Volume | `volume` | 股/单位，非负 Decimal | 否 | `null` + flag |
| Turnover | `turnover` | 交易币种金额，非负 Decimal | 否 | `null` + flag |
| VWAP | `vwap` | 上游无则不自行计算 | 否 | `null` |
| Adjustment | `adjustment` | 首期只允许 `raw` | 是 | 非 raw 拒绝 |
| 本次抓取时间 | `available_at`, `source.retrieved_at` | 转 UTC，`availability_basis=first_seen` | 是 | 无时间则拒绝 |

## HK macro/releases

### 来源与接口

| 项 | primary | fallback |
|---|---|---|
| Provider role | `hk.macro.primary` | `hk.macro.fallback` |
| 来源 | C&SD open data via DATA.GOV.HK/C&SD API；HKMA Open API | HKMA Economic & Financial Data page |
| 状态 | allowlisted C&SD adapter live-ready；release-calendar coverage requires a separately approved source | fixture-only for manual validation |
| 官方文档 | DATA.GOV.HK API spec、C&SD resource pages、HKMA API docs | HKMA data page |
| Base URL/端点 | C&SD: `https://www.censtatd.gov.hk/api/get.php`; HKMA: `https://api.hkma.gov.hk/public/...` | `https://www.hkma.gov.hk/eng/data-publications-and-research/data-and-statistics/economic-financial-data-for-hong-kong/` |
| 请求参数 | C&SD: `id`, `lang`, `full_series`; HKMA: dataset parameters plus `pagesize`, `offset`, `fields`, `choose/from/to`, `sortby/sortorder` | HTML table |
| 分页 | C&SD resource returns full series; HKMA uses `pagesize` max 1000 and `offset` | 页面无 API 分页 |
| 限流 | 未列硬配额；默认 1 rps、并发 1、30s timeout、指数退避 | 每日一次 |
| 历史深度 | C&SD full series by table; HKMA dataset-specific，有些仅最新工作日 | 页面当前值和 DSBB 链接 |
| 时区 | `Asia/Hong_Kong` | `Asia/Hong_Kong` |
| 更新时间 | C&SD resource `Update Frequency`；HKMA API 自动更新，具体按 dataset | 页面声明按发布日更新 |
| available_at basis | API response 无明确发布时间时用 `first_seen`; 有 release/update date 可用 `provider_disseminated` | `first_seen` |
| 凭据 | 无需申请、注册或认证 | 无需凭据 |

### 上游字段到公共 contracts 映射

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| dataset/table id | `series_id`, `MacroSeries.code` | `macro:HK:CENSTATD:<TABLE_ID>` 或 `macro:HK:HKMA:<DATASET>` | 是 | 未登记拒绝 |
| title/name | `MacroSeries.name`, `MacroRelease.release_name` | 原文保留，语言按 `lang` | 是 | 缺失拒绝 |
| period/date fields | `period_start`, `period_end`, `scheduled_at` / `scheduled_date` | 月/季/年按统计期；HKMA daily 用 `end_of_date`，仅日期不伪造 timestamp | 是 | 缺失拒绝 |
| numeric value | `actual`, `MacroObservation.value` | Decimal；单位规范化 | 否 | 未发布 `actual=null` |
| unit | `unit` | `%` -> `percent`; `HK$ million` -> `HKD` with value in original unit unless mapping指定换算 | 是 | 未登记拒绝 |
| release/update date | `released_at`, `available_at` | 有精确时间转 UTC；仅日期则 date precision notes + `first_seen` | 否 | `released_at=null`, `available_at=first_seen_at` |
| status | `MacroRelease.status` | 未来日程=`scheduled`; 有数值=`released`; 延迟/取消需官方文本 | 是 | 不明状态隔离 |

## HK news/announcements

### 来源与接口

| 项 | primary | fallback |
|---|---|---|
| Provider role | `hk.news.primary` | `hk.news.fallback` |
| 来源 | HKMA Press Releases API for official policy news; HKEXnews title search for listed issuer announcements | HKEX IIS paid feed |
| 状态 | HKMA press-release adapter live-ready；HKEXnews fixture-only | gap |
| 官方文档 | HKMA Press Releases API、HKEXnews About/title search、HKEX Terms | HKEX IIS technical docs |
| Base URL/端点 | HKMA: `https://api.hkma.gov.hk/public/press-releases`; HKEXnews: `https://www1.hkexnews.hk/search/titlesearch.xhtml` | IIS feed endpoint after contract/certification |
| 请求参数 | HKMA: `lang`, `offset`, optional common API params; HKEXnews: `category`, `market`, `stockId`, date/title filters | feed subscription params |
| 分页 | HKMA uses `offset`/common API pagination; HKEXnews page pagination/sort | feed sequence |
| 限流 | HKMA default 1 rps；HKEXnews fixture-only 不在线 | 按合同 |
| 历史深度 | HKMA API returned history by offset; HKEXnews title search historical pages | 按 IIS retention/contract |
| 时区 | `Asia/Hong_Kong` | `Asia/Hong_Kong` |
| 更新时间 | HKMA API auto-updates; HKEX announcements follow ESS publication windows | IIS real-time feed |
| available_at basis | HKMA: `provider_disseminated` if API date/time proves; otherwise `first_seen`; HKEXnews: release time -> `exchange_published` for title metadata only | `provider_disseminated` |
| 凭据 | HKMA 无需凭据；HKEXnews 无自动抓取授权；IIS 无凭据 | 无 IIS 客户资格 |

### 上游字段到公共 contracts 映射

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| title / Document | `NewsEvent.title` | 原文保留 | 是 | 缺失拒绝 |
| link / document URL | `canonical_url`, `source.source_url` | canonicalize URL，去 tracking 参数 | 是 | 缺失拒绝 |
| date / Release Time | `published_at` / `published_date`, `first_seen_at`, `available_at` | 香港时间转 UTC；仅日期时填 `published_date`、`time_precision=date`，`available_at=first_seen_at` | 是 | 无时间拒绝或隔离 |
| stock code/name | `entities[]` | `XHKG:<code>` -> instrument entity | 否 | 未解析则 mention |
| press release body | `body` | HKMA press release body 默认不保存；只保存标题和 permitted summary | 否 | `null` |
| source | `source_name`, `source_tier` | HKMA/HKEXnews=`official` | 是 | 缺失拒绝 |
| category/headline | `topics`, `vendor_annotations` | 官方类别进 `topics`; feed vendor 标签进 `vendor_annotations` | 否 | 空列表 |

## 权利矩阵

| 来源/产品 | storage | internal analysis | external LLM | embedding | redistribution | 依据/到期 |
|---|---:|---:|---:|---:|---:|---|
| SSE/SZSE public instruments pages | Yes, metadata fixture only | Yes, internal non-commercial review | No for raw page/content | No | No | 法律声明仅允许非商业浏览/下载；商业、出售牟利、传播需书面许可。到期: 每次使用前复核 |
| SSE/SZSE/CNINFO announcements | Yes, headline/url fixture only | Yes, internal review | No | No | No | SSE/SZSE 法律声明和 CNINFO 页面性质；公告正文不入库。到期: 每次使用前复核 |
| SSE/SZSE licensed daily bars | No until contract | No until contract | No | No | No | 未取得 CIIS/SSE InfoNet/SSIC 行情合同和凭据 |
| BaoStock core-index daily bars | Yes, normalized internal facts for the three allowlisted indices | Yes, internal analysis | No | No | No | 项目负责人于 2026-07-27 选择该公开、无需注册来源；adapter 不保存凭据或原始响应，且不将公开访问视为再分发授权。每次来源条款变化前复核 |
| NBS release calendar/news pages | Yes, source-attributed metadata | Yes | Yes for facts/metadata only, no long verbatim text | Yes for numeric metadata only | No raw redistribution beyond attributed excerpts | NBS 服务条款允许转载/引用需注明来源，排除禁止转载/需授权内容 |
| NBS data portal observations | Yes for fixtures only | Yes for fixtures only | No | No | No | 官方数据发布库；EasyQuery 未作为正式外部 API 文档，当前不冻结 live observation 权利 |
| HKEX public securities lists | Yes, metadata fixture only | Yes, internal review | No raw page/content | No | No | HKEX Terms 禁止未经许可使用 marks/information；市场数据另需许可 |
| HKEX Historical Data / Data Marketplace | No until contract | No until contract | No | No | No | 未订阅；市场数据 vendor/end-user/redistribution 权利按合同 |
| 项目托管 XtQuant HK daily bars | Yes, normalized internal facts for the ten allowlisted symbols | Yes, internal analysis | No | No | No | 内部 XtQuant data-centre 账户和 token 由运行环境管理；adapter 不保存 token、SDK cache 或原始响应，且不将内部访问视为再分发授权 |
| HKEXnews title search | Yes, title/url fixture only | Yes, internal review | No | No | No | HKEXnews/HKEX Terms；IIS 才是授权 feed |
| HKEX IIS | No until contract | No until contract | No | No | No | 未取得 IIS 客户资格、传输规格和许可 |
| DATA.GOV.HK/C&SD API | Yes | Yes | Yes, with attribution | Yes, with attribution | Yes, with attribution | DATA.GOV.HK Terms 允许免费浏览、下载、分发、复制、打印，含商业和非商业用途，需注明来源和遵守条款 |
| HKMA Open API | Yes | Yes | Pending owner/legal confirmation; No until approved | Pending owner/legal confirmation; No until approved | No | HKMA API 无需申请/注册/认证且免费；使用受 HKMA Terms and Conditions；署名、终端用户遵守条款、停止使用和销毁副本义务均需纳入撤销控制；external LLM/embedding 未在本 issue 冻结为允许 |
| HKMA Press Releases API | Yes, title/link/summary only | Yes | Yes for title/link/permitted summary; no full body in MVP | Yes for metadata only | No | HKMA API docs/terms；MVP 不保存全文；redistribution 未在本 issue 冻结为允许 |

`UsageRights` 落库规则:

- 对 fixture-only 或 gap 来源，若产生 fixture 记录，`usage_rights.storage_allowed=true` 仅限合成/脱敏 fixture；真实上游正文不得保存。
- 对 open-data numeric/metadata 来源，`storage_allowed=true`, `internal_analysis_allowed=true`; `external_llm_allowed` 和 `embedding_allowed` 只有在上表明确为 Yes 且有可验证授权依据时才为 true。HKMA Open API 在负责人/法务确认前均为 false；`redistribution_allowed` 只能在上表明确为 Yes 时为 true。
- 对未采购市场数据，全部 false；不得生成 live provider capabilities。BaoStock 的三条批准核心指数映射是本文明确列出的例外，只能按本表范围运行。
- 新闻 `body` 只有在书面合同明确允许 storage 时才能非空；external LLM 和 embedding
  权限分别只限制外发和向量化，不得在公共 `NewsEvent` validation 中解释为禁止内部保存。

## 稳定 ID 和 checksum 规则

### 通用 canonicalization

- 所有 checksum 使用 SHA-256，小写 hex。
- checksum 输入为 canonical JSON: UTF-8、对象 key 排序、去除 token/Cookie/session id、Decimal 用规范字符串、时间转 UTC `Z`、空值显式 `null`。
- `source.checksum_sha256` 使用上游原始业务字段 canonical JSON，不包含 `retrieved_at`、HTTP header、下载路径临时签名或本地批次 ID。
- `content_hash_sha256` 使用允许保存的 `title|summary|body` canonical JSON；未获正文权限时 body 固定为 `null`。

### Instruments

- `canonical_symbol = <MIC>:<LOCAL_SYMBOL>`，CN 使用 `XSHG`/`XSHE`，HK 使用 `XHKG`。
- `instrument_id` 由平台 registry 首次创建并持久化。MVP fixture 可用 deterministic seed: `ins_` + first 26 chars of base32(SHA-256(`region|venue_mic|local_symbol|listed_on`))。缺少 `listed_on`/`valid_from` 的 instrument 记录拒绝，后续正式 registry 不得因名称、简称、抓取日期或供应商 alias 变化改 ID。
- alias 唯一键: `provider_id`, `source_symbol`, `valid_from`。

### Bars

- `bar_id = bar_` + first 32 hex SHA-256(`instrument_id|interval|bar_start_utc|bar_end_utc|adjustment|source.provider_id|source.provider_record_id`).
- storage 去重键遵守 migration: `instrument_id`, `interval`, `bar_start`, `adjustment`, `source.provider_id`。
- `provider_record_id = <provider_id>:<canonical_symbol>:<trading_date>:<interval>:<adjustment>:<file_issue_or_first_seen_date>`.

### Macro

- `series_id = macro:<REGION>:<AUTHORITY>:<CODE>`，例如 `macro:CN:NBS:CPI_YOY`, `macro:HK:CENSTATD:510-60004:SCC_CM`, `macro:HK:HKMA:EFBNCLOSING_BILLS`.
- `release_id = rel_` + first 32 hex SHA-256(`series_id|scheduled_at_utc_or_scheduled_date|period_start|period_end|release_name`).
- `observation_id = obs_` + first 32 hex SHA-256(`series_id|period_start|period_end|vintage_id|revision_no|source.provider_record_id`).
- `vintage_id = <series_id>:<available_at_utc>` for point-in-time revisions.

### News/announcements

- `news_id = news_` + first 32 hex SHA-256(`source.provider_id|provider_record_id|canonical_url_or_title|published_at_utc_or_published_date`).
- `provider_record_id` uses official document id if present; otherwise canonical URL; otherwise normalized title + published instant/date.
- `cluster_id = cluster_` + first 32 hex SHA-256(`region_set|normalized_title|published_date|sorted_entity_ids`) and may be recomputed only by shared news dedup code, not by regional provider-specific DTOs.
- `supersedes_news_id` is set only for explicit corrections/retractions or same provider record replacement.

## Provider capabilities freeze

### #28 live adapter boundary

Issue #28 的 live 选择通过 `PROVIDER_MODE` 显式控制：`live` 才会由应用工厂注册 live roles；`fixture` 只绑定 `*.contract_fixture` roles，生产环境拒绝该模式。当前实现的 allowlist 与公共 contract 映射如下：

| adapter | allowlisted endpoint | live datasets | 数据边界 |
|---|---|---|---|
| `CnNbsReleaseProvider` | NBS release calendar | `macro_releases` | 只输出发布日程 metadata，不保存正文 |
| `CnNbsNewsProvider` | NBS `数据发布` listing | `news` | 只输出标题、官方链接、发布日期和 `first_seen`；摘要/正文请求明确拒绝 |
| `BaoStockDailyBarsProvider` | BaoStock `query_history_k_data_plus` | `bars` | 仅 `sh.000001`、`sh.000300`、`sz.399001`；`1d raw`，`available_at=first_seen` |
| `HkXtQuantDailyBarsProvider` | 项目托管 XtQuant data-centre | `bars` | 十个批准 HK equity mappings；`1d raw`，`available_at=first_seen`，不启动或配置 data-centre |
| `HkCsdProvider` | C&SD `510-60004` API | `macro_series`, `macro_observations` | 以 API 首次可见时间作为 `available_at`，不声称 PIT 或 revision history |
| `HkmaPressReleaseProvider` | HKMA press-releases API | `news` | 只输出标题/链接；正文请求明确拒绝 |

C&SD `510-60004` 当前批准 `sv=SCC_CM`、`SA_CM`、`SB_CM`、`SC_CM`，它们分别映射为
Composite CPI、CPI(A)、CPI(B)、CPI(C) 的季调后三个月平均环比变化，且各自拥有独立的
canonical series ID。frequency、unit、transformation 和 seasonal adjustment 来自代码内
registry；未登记的 `sv` 或 metadata 不匹配时阻断 adapter，不按上游字段启发式推断。

这三个 live adapter 均不提供上游历史快照；当 `as_of` 早于本次抓取时间时抛出
`UnsupportedCapabilityError`，不能把空页当作“确实没有数据”。分页 continuation 会绑定
源数据 watermark，并校验前一条排序记录；源数据变化时返回 `ProviderCursorError`。

未在 allowlist 中的 host、C&SD dataset、BaoStock symbol、XtQuant symbol、市场行情和 HKEX/CN 公告来源不会被 live factory 注册。HK 非批准标的、CN 个股及未获行情授权的数据仍必须走后续已批准来源；不能用 fixture adapter 冒充 live 完成日报。

Adapter authors must register capabilities according to this matrix:

| Provider role | Dataset | Region | Capability status |
|---|---|---|---|
| `cn.instruments.primary` | instruments | CN | fixture-only, no live capability |
| `cn.bars.primary` | bars | CN | live-ready for three BaoStock core-index mappings only; no historical PIT |
| `cn.macro.primary` | macro_releases | CN | live-ready for release calendar only |
| `cn.macro.primary` | macro_observations | CN | fixture-only |
| `cn.news.primary` | news | CN | live-ready for NBS data-release headline metadata; body prohibited |
| `hk.instruments.primary` | instruments | HK | fixture-only, no live capability |
| `hk.bars.primary` | bars | HK | live-ready for ten XtQuant HK equity mappings only; no historical PIT |
| `hk.macro.primary` | macro_series/macro_observations | HK | live-ready for the allowlisted C&SD table API; release-calendar coverage requires a separately approved source |
| `hk.news.primary` | news | HK | live-ready for HKMA press-release metadata; HKEXnews fixture-only |

## 失败与降级

| 场景 | 错误映射 | 重试 | fallback/降级 |
|---|---|---|---|
| 401/认证失败 | `ProviderAuthenticationError` | 不重试 | 标 `not_configured`; 禁止使用 fixture 冒充 live |
| 403/授权不足 | `ProviderAuthorizationError` | 不重试 | 标 `not_configured`; 需要采购/授权 ADR |
| 429/限流 | `ProviderRateLimitError` + `retry_after_seconds` | 按 Retry-After 或指数退避 | 当前页不入库，保留 cursor |
| 超时/网络错误 | `ProviderTimeoutError` | 最多 2 次退避重试 | 仍失败则 job `retry_wait` |
| 登录页伪 200/验证码/风控页 | `ProviderAuthorizationError` | 不重试 | 按 PRV-019 立即阻断 provider，禁止解析 HTML 内容，不能当空数据 |
| schema drift/必填字段改名 | `ProviderSchemaError` | 不重试 | 阻断该 adapter，生成 `schema_drift_status` |
| 空页循环/重复页 | `ProviderCursorError` | 不重试当前 cursor | 停止分页并报告 duplicate cursor/page checksum |
| cursor 过期 | `ProviderCursorError` | 以原 query 从第一页重建一次 | 仍失败则 job `retry_wait` |
| 未采购或 fixture-only 被生产调度调用 | `UnsupportedCapabilityError` | 不重试 | ProviderHealth=`not_configured` |

禁止重试: 401、403、登录页伪 200、schema drift、未采购授权、受限正文访问。

## Fixtures 与测试

| 区域 | Fixture 目录 | 最小文件 | 测试 ID | 脱敏/正文规则 |
|---|---|---|---|---|
| CN | `tests/fixtures/cn/synthetic/` | `success.json`, `empty.json`, `missing_fields.json`, `auth_failure.json`, `rate_limited.json`, `timeout.json`, `schema_changed.json`, `duplicate_page.json` | `PRV-001`, `PRV-002`, `PRV-009`, `PIT-009`, `API-013` | 只用合成或脱敏数据；公告 `body=null`; 不含 token/Cookie/个人信息 |
| HK | `tests/fixtures/hk/synthetic/` | 同 CN | 同 CN | 只用合成或脱敏数据；HKMA/HKEXnews 正文不入 fixture |

在线 smoke 最小请求和成本:

- CN release calendar: 每日一次 GET NBS 发布日程页面；无 token；成本为公共网页请求。
- CN official headlines: 每日一次 GET NBS 数据发布列表首页；无 token；只存 metadata，不请求文章正文。
- CN core-index bars: BaoStock client 查询 `sh.000300` 最近 10 个日历日；无 token；成本为公共客户端请求。
- HK daily bars: 已配置的 XtQuant data-centre 查询 `00700.HK` 最近 10 个日历日；worker 无 token；成本计入内部 XtQuant 账户/缓存。
- HK macro: 每日一次 C&SD table API 或 HKMA Open API，`pagesize<=100`; 无 token；成本为公共 API 请求。
- HKMA press releases: 每日一次 `lang=en&offset=0`; 无 token；成本为公共 API 请求。
- 所有未列为 `live-ready` 的 `fixture-only` 和 `gap` 来源在线 smoke 禁止运行。NBS 新闻 smoke 与其他公共来源一样必须显式设置 `RUN_LIVE_SMOKE=1`；XtQuant smoke 还必须设置 `RUN_XTQUANT_LIVE_SMOKE=1`，避免没有 vendor runtime 或共享数据中心时误跑。

## 运行指标与退出方案

| 指标 | 阈值 | 告警接收人 |
|---|---:|---|
| freshness | live-ready 来源最新成功抓取滞后不超过 1 个调度周期 | @Nouzee + 项目负责人 |
| completeness | fixture provider contract suite 100% 通过；live-ready 单页解析拒绝率 < 1% | @Nouzee |
| rejection | schema/必填字段拒绝率 > 1% 告警；> 5% 阻断入库 | @Nouzee + 实习生 B |
| latency | 单请求 p95 < 5s；超时 30s | @Nouzee |
| rights_violation | 任何正文、token、Cookie、未授权市场数据入库为 0 容忍 | 项目负责人 |

停用/撤销:

- 将 provider role 解绑或标记 `not_configured`，停止调度。
- 删除内存/Secret Manager 中凭据引用；本仓库不得有 secret 可撤销。
- 对未授权真实正文或市场数据，执行隔离表审计并删除 raw payload；保留合规 metadata、checksum、run_id 和删除审计记录。
- 对 open-data 来源停用，保留已标准化事实和 source attribution，除非来源条款或负责人要求删除。

## Review gate

- 实习生 A 可在本文件基础上实现 CN/HK fixture/provider skeleton。
- review 中需由实习生 B 交叉检查本文件中的 contract 映射、`available_at` basis、cursor/分页和 `UsageRights`，并运行公共 provider contract suite。
- 后续 Issue 如需新增公共字段、改变 ID 或改变 `available_at` 语义，必须先提交 ADR 和负责人批准；`UsageRights` 兼容元数据不构成内部运行时 gate。
