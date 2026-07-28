# ADR 0013：HK/US 官方宏观发布日历

- 状态：accepted
- 日期：2026-07-28
- 关联 Issue：[#51](https://github.com/Detachm/macro-data-platform/issues/51)

## 背景

冻结输入 `calendar.macro_releases_7d` 要求 CN、HK、US 三个区域都能证明未来七天的发布日历覆盖。
CN 已由 NBS 日历提供；此前 HK/US 没有获批 live provider，因此即使市场和新闻采集成功，日报仍会
按设计阻断。空窗口也必须由一次成功、可审计的官方源任务证明，不能把“没抓到”当作“没有事件”。

来源选择必须保留上游实际时间精度。香港政府统计处（C&SD）明确说明常规统计新闻稿通常在香港
时间 16:30 发布，并每年九月发布下一年度日程。美国 BLS 官方 ICS 在当前部署网络受到 Akamai 403
拦截，不能作为生产可用来源；OMB 的 Principal Federal Economic Indicators 年度 PDF 可稳定访问，
但只提供日期。BEA 官方 schedule 页面同时提供日期和美东发布时间。

## 决策

- 新增 `hk.censtatd.release-calendar.v1`，绑定 `hk.calendar.primary`：
  - 只读取官方年度 `Regular_Press_Releases_Schedule_<year>.xlsx`；
  - 标题保留官方 reference-period 文本，发布时间固定为该日 `16:30 Asia/Hong_Kong` 后转 UTC；
  - XLSX 只用标准库 ZIP/XML 解析，并限制压缩包大小、条目数、解压总量、行数和单元格长度；不执行
    宏、不解压到文件系统。
- 新增 `us.official.release-calendar.v1`，绑定 `us.calendar.primary`：
  - 先从 White House OMB 官方 PFEI landing page 发现对应年份 PDF，不猜测下载地址；
  - OMB PDF 只接入 BLS 的 `The Employment Situation`、`Producer Price Indexes`、
    `Consumer Price Index`，保持 `time_precision=date`；reference period 为发布日前一个完整月；
  - BEA 官方 schedule 的所有有确定日期和时间的行按 `America/New_York` 解析并转 UTC；`TBA` 行跳过；
  - PDF 限制字节数和页数，由 `pypdf` 读取；字段、月份行或机构分节漂移时 fail closed。
- 新增必需任务 `hk.macro-release-calendar` 和 `us.macro-release-calendar`。它们与
  `cn.macro-release-calendar` 分别落库、checkpoint 和生成 durable run evidence。
- 全球质量项只有在三个区域任务均成功且各自至少提交一个 provider page 时才为 `available`。合法
  空窗口会生成区域级零事件事实；任一区域失败、隔离、迟到或没有 durable page 均阻断。三个区域的
  facts/source references 最后合并成唯一的 `calendar.macro_releases_7d`。
- 上游不提供历史网页快照。`available_at` 使用本次抓取完成时间，历史 `as_of` 请求拒绝；每日保存的
  normalized facts、source checksum 和 revision rows 构成平台自己的 point-in-time 审计链。
- release/provider record identity 不包含 scheduled date。官方改期保持同一个身份、checksum 改变，
  从而写入修订而不是重复事件。HK 用完整官方标题，BEA 用完整官方标题，BLS 用指标加 reference month
  作为稳定业务身份。
- 日历不提供 actual、consensus、previous，统一保留为 `null`，不得从新闻或市场数据推断。

## 运行边界

- 报告日任务请求上海当地 00:00 起未来八天的半开窗口；报告事实显示未来七天。
- 年末跨年窗口同时读取涉及的两个年度文件。下一年度文件尚未出现在官方 landing/file path 时任务
  明确失败，不把不完整的旧年日历当作成功。
- US BLS 精确时刻是有意未填充的已知边界；只有 BEA 行能提供 `scheduled_at`。
- 这些 provider 只解决发布日历，不等于 BLS/BEA/C&SD 观测值 ingestion，也不解决 US 官方新闻。

## 验证与回滚

- contract tests 覆盖来源 provenance、时间精度、分页边界和 bounded page；parser tests 覆盖改期
  identity/checksum 语义与 malformed source fail-closed。
- live smoke 对 HK/US 各验证连续两个 report date；允许事件窗口为空，但要求完整解析官方全年来源、
  非空 watermark 和正确 region/provenance。
- 回滚时解除两个 calendar role 和任务注册。质量门禁会恢复为显式缺失；已落库事实、修订、run 和
  checkpoint 保留，不删除审计记录。

## 官方来源

- C&SD release calendar：<https://www.censtatd.gov.hk/en/press_release.html>
- C&SD 2026 XLSX：<https://www.censtatd.gov.hk/FileManager/EN/Common/Regular_Press_Releases_Schedule_2026.xlsx>
- OMB PFEI：<https://www.whitehouse.gov/omb/information-resources/guidance/us-principal-federal-economic-indicators/>
- BEA release schedule：<https://www.bea.gov/news/schedule>
