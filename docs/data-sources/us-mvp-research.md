# US MVP data-source research for issue #2

Date: 2026-07-23
Owner context: `@kazming666` / issue #2 research support
Status: research note, not the frozen inventory file. Do not treat this as approval to fetch production data or store restricted content.

This note inventories high-trust, first-party or official API sources for a US MVP covering instruments, daily bars, rates/FX, macro observations/releases, SEC filing metadata, and daily news. It intentionally avoids tokens, cookies, accounts, credentials, and long copied terms text.

## Executive recommendation

| Scope | Recommended MVP source | Use mode | Main reason / constraint |
|---|---|---|---|
| US listed instruments | Nasdaq Trader Symbol Directory + SEC `company_tickers.json` | No-key scheduled snapshots | Nasdaq gives exchange symbol directories; SEC gives CIK/ticker/title crosswalk. Treat Nasdaq redistribution rights as unapproved until legal/licensing review. |
| SEC filing metadata | SEC EDGAR data APIs | No-key production candidate | Official metadata source, real-time dissemination notes, explicit fair-access limit. |
| Treasury rates | U.S. Treasury XML feeds | No-key production candidate | First-party Treasury daily rates with official history ranges. |
| FX / Fed statistical rates | Federal Reserve Data Download Program, especially H.10 | No-key production candidate | First-party Federal Reserve statistical releases. |
| Macro observations and release/vintage data | BLS/BEA direct APIs; FRED/ALFRED only for discovery/manual cross-check unless approved | Direct agency production candidate; FRED non-ingest by default | BLS and BEA give direct official agency APIs and release schedules. FRED gives standardized API/vintages, but its current terms require series-owner review and include storage/AI-use restrictions that conflict with this platform’s default ingest/LLM path. |
| Daily equity bars | Massive/Polygon only if plan/license permits; Alpha Vantage only as fixture/dev fallback unless commercial permission exists | Contracted/fixture-only | Free or personal-market-data terms are not enough for production, LLM, embeddings, or redistribution. |
| Daily news | SEC RSS for official regulatory news; GDELT metadata discovery; NewsAPI or Massive News only with paid/legal approval | Regulatory production + commercial/metadata-only for general news | General publisher content has copyright/licensing risk. Do not ingest full article bodies without explicit rights. |

## Cross-source implementation rules

- No secrets in docs, fixtures, logs, issue comments, or committed configs.
- Keep all persisted datetimes timezone-aware UTC. Preserve source-local timestamps separately when useful.
- Use `first_seen_at` as the conservative `available_at` whenever the source does not provide a precise official dissemination/acceptance timestamp.
- Do not use scheduled release dates alone as `available_at`. Store them as `scheduled_at` or `release_date` unless an official release timestamp or observed successful fetch confirms availability.
- For historical backfills, if the source only exposes observation dates or calendar release dates, mark the rows as not intraday point-in-time safe.
- Parse prices/rates with `Decimal` from raw text/JSON, not through binary floats.
- For daily equity bars, treat the trading calendar and sessions as `America/New_York`; DST must be handled by IANA timezone rules rather than fixed UTC offsets.
- Do not store general-news article bodies, restricted vendor content, or publisher pages unless legal has approved the specific provider and plan.
- API runtime code should not call external providers directly; use provider ingestion jobs, snapshots, or fixtures behind the project’s provider boundary.

## Conservative `available_at` policy

| Source type | Conservative basis |
|---|---|
| SEC filings | Use official EDGAR acceptance/dissemination timestamp when present in the API record. Otherwise use `first_seen_at`. |
| FRED/ALFRED | Default MVP does not persist FRED-derived observations. If later approved, store `realtime_start` / vintage date, but use `first_seen_at` as intraday `available_at` unless an official agency timestamp is captured. FRED release dates are not the same as FRED availability. |
| BLS / BEA releases | Store official scheduled release time where available. Use `first_seen_at` for actual `available_at` unless the exact release page/API timestamp is captured. |
| Treasury / Fed statistical rows | Store observation date and source release date if provided. Use `first_seen_at` when no precise publish timestamp exists. |
| Equity daily bars | Store bar date/session and vendor response time. Use `first_seen_at` unless the vendor exposes a documented dissemination timestamp. |
| News | Store publisher `published_at` separately from provider availability. Use `first_seen_at` unless the provider gives a documented first-seen timestamp that legal and product accept for PIT use. |

## Source candidates

### 1. Nasdaq Trader Symbol Directory

- Dataset fit: US listed instruments.
- Official documentation:
  - <https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs>
  - Trading calendar reference: <https://www.nasdaqtrader.com/trader.aspx?id=Calendar>
- API / endpoint shape:
  - `ftp://ftp.nasdaqtrader.com/symboldirectory/nasdaqlisted.txt`
  - `ftp://ftp.nasdaqtrader.com/symboldirectory/otherlisted.txt`
  - The documentation describes pipe-delimited files and a final row carrying `File Creation Time` in `mmddyyyyhh:mm` format.
- Auth / key needs:
  - No API key documented on the symbol-directory page.
- Pagination / rate-limit notes:
  - No pagination. Fetch whole files.
  - No official numeric rate limit found on the symbol-directory documentation page. Use low-frequency scheduled snapshots and conditional fetches where possible.
- Timezone / DST / update-time hints:
  - Official docs say files are updated periodically throughout each day.
  - The footer timestamp format is documented, but the source page does not clearly state timezone. Store raw footer value; convert only after timezone is confirmed.
  - For instrument listing status tied to equity sessions, normalize downstream calendar logic with `America/New_York`.
- Historical depth:
  - Official page describes current directory files, not an official historical archive. Treat history as project-retained snapshots from first ingestion.
- `available_at` basis:
  - If footer timezone is confirmed, use footer file-creation timestamp.
  - Until then, use ingestion `first_seen_at` and store the raw footer timestamp.
- Conservative rights assumptions:

| Right | Assumption |
|---|---:|
| storage_allowed | conditional |
| internal_analysis_allowed | conditional |
| external_llm_allowed | no |
| embedding_allowed | no |
| redistribution_allowed | no |

Basis: use as internal reference data only until Nasdaq data terms/licensing are reviewed. Do not redistribute directory snapshots or derived exchange reference datasets externally.

### 2. SEC EDGAR APIs, company tickers, and SEC RSS feeds

- Dataset fit: SEC filing metadata, CIK/ticker crosswalk, official regulatory news.
- Official documentation:
  - EDGAR APIs: <https://www.sec.gov/search-filings/edgar-application-programming-interfaces>
  - Privacy / automated access policy: <https://www.sec.gov/about/privacy-information>
  - RSS feeds: <https://www.sec.gov/about/rss-feeds>
  - Company tickers JSON: <https://www.sec.gov/files/company_tickers.json>
- API / endpoint shape:
  - Submissions metadata: `GET https://data.sec.gov/submissions/CIK##########.json`
  - Bulk submissions ZIP: `GET https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip`
  - Company tickers: `GET https://www.sec.gov/files/company_tickers.json`
  - RSS feeds are available for SEC materials, press releases, speeches/statements, litigation releases, trading suspensions, administrative proceedings, and EDGAR search results.
- Auth / key needs:
  - SEC describes these as RESTful JSON data APIs. No API key is documented.
  - Automated clients must identify themselves with an appropriate User-Agent/contact and follow SEC fair-access policy.
- Pagination / rate-limit notes:
  - SEC policy states a current automated-request guideline of no more than 10 requests per second overall.
  - A company submissions JSON includes recent filing history and references additional paginated JSON files when the filer has more history.
  - Bulk ZIPs are republished nightly around 3:00 a.m. ET according to the SEC API page.
- Timezone / DST / update-time hints:
  - SEC says submissions are updated in real time as they are disseminated, with typical processing delay under one second for submissions metadata.
  - Use any timezone-bearing API timestamp as authoritative. If a field is timezone-less, preserve raw value and do not infer more precision than documented.
- Historical depth:
  - Current submissions JSON includes at least one year or 1,000 most recent filings, plus references to older files by date range when available.
  - Bulk submissions ZIP provides a full bulk route for filing metadata snapshots.
- `available_at` basis:
  - Filing metadata: official `acceptanceDateTime` / dissemination timestamp when present.
  - Company-ticker crosswalk: SEC file `first_seen_at` unless HTTP headers or file metadata give a trustworthy timestamp.
  - SEC RSS: item publication timestamp if provided, otherwise `first_seen_at`.
- Conservative rights assumptions:

| Right | Assumption |
|---|---:|
| storage_allowed | yes |
| internal_analysis_allowed | yes |
| external_llm_allowed | yes, for metadata/headlines |
| embedding_allowed | yes, for metadata/headlines |
| redistribution_allowed | conditional |

Basis: SEC metadata and official SEC RSS items are public-government materials, but keep fair-access, attribution, and no-endorsement controls. Filing bodies and exhibits are out of this issue’s scope.

### 3. FRED / ALFRED

- Dataset fit: macro discovery, manual cross-checking, and possible future allowlisted ingest only after legal/project approval.
- Official documentation:
  - FRED API docs: <https://fred.stlouisfed.org/docs/api/fred/>
  - API key docs: <https://fred.stlouisfed.org/docs/api/api_key.html>
  - Series observations: <https://fred.stlouisfed.org/docs/api/fred/series_observations.html>
  - Releases dates: <https://fred.stlouisfed.org/docs/api/fred/releases_dates.html>
  - Real-time periods: <https://fred.stlouisfed.org/docs/api/fred/realtime_period.html>
  - API terms: <https://fred.stlouisfed.org/docs/api/terms_of_use.html>
- API / endpoint shape:
  - Observations: `GET https://api.stlouisfed.org/fred/series/observations?series_id={id}&api_key={key}&file_type=json`
  - Release dates: `GET https://api.stlouisfed.org/fred/releases/dates?api_key={key}&file_type=json`
  - Common params include `realtime_start`, `realtime_end`, `observation_start`, `observation_end`, `limit`, `offset`, `sort_order`, `frequency`, `aggregation_method`, `output_type`, and `vintage_dates`.
- Auth / key needs:
  - API key required for web service requests.
- Pagination / rate-limit notes:
  - Observations responses expose `count`, `offset`, and `limit`; official docs show `limit` support up to 100,000 for observations.
  - Release-date endpoint uses `limit` and `offset`; official docs describe `limit` from 1 to 1,000, default 1,000.
  - Official error docs describe HTTP 429 for rate limiting, but no stable public numeric request quota was confirmed in the consulted official docs.
- Timezone / DST / update-time hints:
  - Observation dates are date-level.
  - FRED release-date docs state release dates are published by data sources and do not necessarily represent when data is available on FRED/ALFRED.
  - Treat FRED vintage periods as date-granularity unless another official timestamp is captured.
- Historical depth:
  - Series-specific. Use FRED series metadata and response ranges per series.
  - ALFRED-style real-time periods support retrieving what was known for past periods.
- `available_at` basis:
  - For the default MVP, do not persist FRED-derived observations.
  - If a future approval allows specific series, store `realtime_start` / `realtime_end` as vintage metadata, use `first_seen_at` for intraday point-in-time availability unless a source agency timestamp is captured, and never treat observation date or release calendar date as exact availability by itself.
- Conservative rights assumptions:

| Right | Assumption |
|---|---:|
| storage_allowed | no by default |
| internal_analysis_allowed | no by default |
| external_llm_allowed | no |
| embedding_allowed | no |
| redistribution_allowed | no |

Basis: FRED terms put responsibility on the user for third-party or copyrighted series, require attention to series notes, and include restrictions around storage/caching/archive and AI/LLM-related use. Treat FRED as non-ingest for this MVP unless the project lead explicitly approves a narrow allowlist.

### 4. U.S. Treasury daily interest-rate XML feeds

- Dataset fit: Treasury yield curve, Treasury bill rates, long-term rates, real yield curve.
- Official documentation:
  - <https://home.treasury.gov/treasury-daily-interest-rate-xml-feed>
- API / endpoint shape:
  - Base: `https://home.treasury.gov`
  - Path: `/resource-center/data-chart-center/interest-rates/pages/xml`
  - Example params:
    - `data=daily_treasury_yield_curve`
    - `data=daily_treasury_bill_rates`
    - `data=daily_treasury_long_term_rate`
    - `data=daily_treasury_real_yield_curve`
    - `data=daily_treasury_real_long_term`
    - `field_tdr_date_value=YYYY`
    - `field_tdr_date_value=all&page=N`
    - `field_tdr_date_value_month=YYYYMM`
- Auth / key needs:
  - No API key documented.
- Pagination / rate-limit notes:
  - Year and month queries are direct.
  - `all` queries are paginated with zero-based `page`; the official page describes 300 rows on page 0 and incrementing until no `<entry>` rows.
  - No official numeric rate limit found in the consulted docs.
- Timezone / DST / update-time hints:
  - Rows are daily observations.
  - The consulted official page does not provide a precise publish timestamp per row. Store fetch time and any HTTP metadata.
- Historical depth:
  - Daily Treasury par yield curve rates from 1990.
  - Daily Treasury bill rates from 2002.
  - Daily Treasury long-term rates from 2000.
  - Daily Treasury par real yield curve rates from 2003.
  - Daily Treasury real long-term rates from 2000.
- `available_at` basis:
  - Use `first_seen_at` unless an exact official publish timestamp is captured.
  - Store row date as observation date, not availability timestamp.
- Conservative rights assumptions:

| Right | Assumption |
|---|---:|
| storage_allowed | yes |
| internal_analysis_allowed | yes |
| external_llm_allowed | yes |
| embedding_allowed | yes |
| redistribution_allowed | conditional |

Basis: first-party U.S. government numeric data. Keep attribution/source URL and avoid implying Treasury endorsement.

### 5. Federal Reserve Data Download Program, H.10 / G.5

- Dataset fit: official FX rates and indexes; potentially other Federal Reserve statistical releases if later needed.
- Official documentation:
  - H.10 Data Download Program page: <https://www.federalreserve.gov/datadownload/Choose.aspx?rel=H10>
  - Federal Reserve website disclaimer: <https://www.federalreserve.gov/disclaimer.htm>
- API / endpoint shape:
  - The DDP page provides selectable downloads and preformatted packages for H.10 daily indexes, H.10 daily rates, G.5 monthly data, and an all-data XML download.
  - Treat DDP generated CSV/XML/ZIP URLs as source snapshots once selected and recorded in source config.
- Auth / key needs:
  - No API key documented on the DDP page.
- Pagination / rate-limit notes:
  - No official pagination or numeric rate limit found in the consulted H.10 DDP page.
  - Use low-frequency scheduled downloads and backoff.
- Timezone / DST / update-time hints:
  - DDP page shows release dates for the statistical release.
  - No exact per-row publish timestamp was confirmed in the consulted page.
- Historical depth:
  - Use the selected package/date-range metadata from DDP. The page supports all-data package download for G.5/H.10.
- `available_at` basis:
  - Use official release timestamp if present in downloaded metadata; otherwise use `first_seen_at`.
  - Store observation date separately.
- Conservative rights assumptions:

| Right | Assumption |
|---|---:|
| storage_allowed | yes |
| internal_analysis_allowed | yes |
| external_llm_allowed | yes |
| embedding_allowed | yes |
| redistribution_allowed | conditional |

Basis: Federal Reserve disclaimer says Board website information is generally public domain unless otherwise indicated. Preserve attribution and exclude non-Board copyrighted assets.

### 6. BLS Public Data API

- Dataset fit: source-of-record macro observations and release schedule for CPI, labor-market, employment, price, wage, and related series.
- Official documentation:
  - API signatures v2: <https://www.bls.gov/developers/api_signature_v2.htm>
  - API FAQs: <https://www.bls.gov/developers/api_faqs.htm>
  - Terms of service: <https://www.bls.gov/developers/termsOfService.htm>
  - Release calendar: <https://www.bls.gov/schedule/news_release/>
- API / endpoint shape:
  - Single series: `GET https://api.bls.gov/publicAPI/v2/timeseries/data/{series_id}`
  - Multiple series: `POST https://api.bls.gov/publicAPI/v2/timeseries/data/`
  - JSON payload can include `seriesid`, `startyear`, `endyear`, `catalog`, `calculations`, `annualaverage`, `aspects`, and `registrationkey`.
- Auth / key needs:
  - Unregistered access exists with smaller limits.
  - Registration key is required for v2 higher limits and optional features such as larger year windows.
- Pagination / rate-limit notes:
  - Official FAQ states:
    - Registered users: 500 daily queries, 50 series per query, 20 years per query.
    - Unregistered users: 25 daily queries, 25 series per query, 10 years per query.
    - Both: 50 requests per 10 seconds.
  - HTTP 429 indicates too many requests.
- Timezone / DST / update-time hints:
  - Official release calendar states release times in Eastern Time.
  - Individual API observations are period-based and date/period-level; they do not by themselves provide an exact release timestamp.
- Historical depth:
  - API window depth depends on registration status. Full series history may require chunking and/or alternate BLS files not covered in this note.
- `available_at` basis:
  - Store release-calendar time as `scheduled_at` where relevant.
  - Use `first_seen_at` for exact availability unless an official release timestamp/page is captured at fetch time.
- Conservative rights assumptions:

| Right | Assumption |
|---|---:|
| storage_allowed | yes |
| internal_analysis_allowed | yes |
| external_llm_allowed | yes |
| embedding_allowed | yes |
| redistribution_allowed | conditional |

Basis: BLS terms allow secondary use but require citation/access-date style care and prohibit misrepresentation or false endorsement. Keep transformed values clearly labeled.

### 7. BEA API

- Dataset fit: source-of-record macro observations and release schedule for GDP, PCE, income, industry, international, and regional data.
- Official documentation:
  - API signup / dataset overview: <https://apps.bea.gov/api/signup/>
  - API user guide PDF: <https://apps.bea.gov/api/_pdf/bea_web_service_api_user_guide.pdf>
  - API terms PDF: <https://apps.bea.gov/API/_pdf/bea_api_tos.pdf>
  - Release schedule: <https://www.bea.gov/news/schedule>
- API / endpoint shape:
  - Base endpoint: `GET https://apps.bea.gov/api/data`
  - Minimum params include `UserID` and `method`.
  - Metadata example method: `method=GETDATASETLIST`
  - Data calls specify `datasetname` plus dataset-specific params.
  - `ResultFormat` supports JSON/XML, with JSON as the normal integration target.
- Auth / key needs:
  - Requires registration and a BEA `UserID` key.
- Pagination / rate-limit notes:
  - Official user guide states limits of 100 requests/minute, 100 MB/minute, and 30 errors/minute.
  - Exceeding limits returns API errors and HTTP 429 with `Retry-After`.
  - Pagination depends on dataset and method response shape; record per dataset during adapter design.
- Timezone / DST / update-time hints:
  - BEA release schedule page provides dates and times for releases and offers machine-readable calendar formats.
  - Do not assume timezone from the HTML alone unless the specific machine-readable feed or page confirms it for the chosen release.
- Historical depth:
  - Dataset-specific. The signup/docs page lists supported datasets; each dataset must be probed via metadata methods before final adapter work.
- `available_at` basis:
  - Store official release schedule as `scheduled_at`.
  - Use `first_seen_at` for actual `available_at` unless an official release timestamp is captured.
- Conservative rights assumptions:

| Right | Assumption |
|---|---:|
| storage_allowed | yes |
| internal_analysis_allowed | yes |
| external_llm_allowed | yes |
| embedding_allowed | yes |
| redistribution_allowed | conditional |

Basis: BEA API terms allow using BEA public data through the API, with notice/no-endorsement and no misrepresentation controls.

### 8. Massive / Polygon stocks aggregates

- Dataset fit: daily equity bars, licensed market reference data, optional licensed stock news.
- Official documentation:
  - Stocks aggregates: <https://massive.com/docs/rest/stocks/aggregates/custom-bars>
  - Pricing: <https://massive.com/pricing>
  - Business terms: <https://massive.com/legal/businesses-terms-of-service>
  - Stock news endpoint if separately licensed: <https://massive.com/docs/rest/stocks/news>
- API / endpoint shape:
  - Aggregates: `GET /v2/aggs/ticker/{stocksTicker}/range/{multiplier}/{timespan}/{from}/{to}`
  - Typical daily-bars params: `multiplier=1`, `timespan=day`, `from=YYYY-MM-DD`, `to=YYYY-MM-DD`, `adjusted=true|false`, `sort=asc|desc`, `limit=N`.
  - Response includes `results` with OHLCV-style fields and millisecond timestamp `t`; response may include `next_url`.
  - News endpoint is a separate licensed endpoint under `/v2/reference/news`.
- Auth / key needs:
  - API key required.
  - Production use depends on plan, exchange/vendor licensing, and whether individual-use restrictions apply.
- Pagination / rate-limit notes:
  - Aggregates docs show `limit` up to 50,000 and `next_url` for pagination.
  - Pricing page lists plan-specific access. The free/basic stock plan has a small per-minute quota and limited history; paid plans expand history and remove that quota according to pricing.
- Timezone / DST / update-time hints:
  - Aggregates docs describe aggregate periods as based on Eastern Time.
  - Treat `t` as aggregate-window start. For daily bars, use `America/New_York` session rules and convert to UTC.
- Historical depth:
  - Plan-specific. Pricing page lists different historical-depth tiers, including limited free history and deeper paid-plan history.
- `available_at` basis:
  - Use vendor fetch `first_seen_at` unless the response contains a documented dissemination timestamp.
  - For delayed plans, record plan delay policy in source config and do not manufacture per-row availability.
- Conservative rights assumptions:

| Right | Assumption |
|---|---:|
| storage_allowed | conditional |
| internal_analysis_allowed | conditional |
| external_llm_allowed | no |
| embedding_allowed | no |
| redistribution_allowed | no |

Basis: market data and news are contract/licensing controlled. Use only fixtures or licensed internal storage until plan/order-form rights explicitly cover the project.

### 9. Alpha Vantage

- Dataset fit: daily equity bars as a low-volume development or fixture fallback; not recommended as production source without commercial permission.
- Official documentation:
  - API documentation: <https://www.alphavantage.co/documentation/>
  - Terms of service: <https://www.alphavantage.co/terms_of_service/>
  - Pricing / support: <https://www.alphavantage.co/premium/>
- API / endpoint shape:
  - Daily raw bars: `GET https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol={symbol}&outputsize=compact|full&datatype=json|csv&apikey={key}`
  - Daily adjusted bars: `GET https://www.alphavantage.co/query?function=TIME_SERIES_DAILY_ADJUSTED&symbol={symbol}&outputsize=compact|full&datatype=json|csv&apikey={key}`
  - Adjusted endpoint includes adjusted close and corporate-action fields.
- Auth / key needs:
  - API key required.
  - Freshness and endpoint availability depend on plan/entitlement.
- Pagination / rate-limit notes:
  - No cursor pagination for daily time-series endpoints; `compact` vs `full` controls history size.
  - Official pricing/support materials describe free and paid usage limits; verify the active quota for the selected key before any live smoke.
- Timezone / DST / update-time hints:
  - Daily series are exchange-date oriented. The API metadata may include last-refreshed date, but not a precise provider availability timestamp.
- Historical depth:
  - Official docs describe 20+ years of daily history for daily functions, with plan constraints for full history and premium/freshness features.
- `available_at` basis:
  - Use `first_seen_at`.
  - Store last-refreshed metadata separately and do not treat it as exact availability unless contract/docs say so.
- Conservative rights assumptions:

| Right | Assumption |
|---|---:|
| storage_allowed | conditional |
| internal_analysis_allowed | conditional |
| external_llm_allowed | no |
| embedding_allowed | no |
| redistribution_allowed | no |

Basis: official terms restrict use without Alpha Vantage permission beyond permitted personal/non-commercial contexts. Keep as fixture/dev fallback unless a commercial agreement approves the use case.

### 10. GDELT DOC API

- Dataset fit: daily general-news discovery metadata, not article-body ingestion.
- Official documentation:
  - DOC 2.0 API announcement/docs: <https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/>
  - GDELT project/about and terms posture: <https://www.gdeltproject.org/about.html>
  - Official rate-limiting note example: <https://blog.gdeltproject.org/ukraine-api-rate-limiting-web-ngrams-3-0/>
- API / endpoint shape:
  - `GET https://api.gdeltproject.org/api/v2/doc/doc?query={query}&mode=artlist&maxrecords={n}&timespan={window}&format=json`
  - Other modes and formats exist; MVP should restrict to article-list metadata discovery.
- Auth / key needs:
  - No API key documented for DOC examples.
- Pagination / rate-limit notes:
  - DOC examples use `maxrecords`, `timespan`, sorting, and query filters rather than cursor pagination.
  - Official GDELT blog posts state hosted APIs are rate limited, but no stable numeric global quota was confirmed in the consulted docs.
- Timezone / DST / update-time hints:
  - News metadata may include source publication or seen-date fields depending on response.
  - Preserve raw provider timestamps and normalize to UTC only when timezone is explicit.
- Historical depth:
  - DOC 2.0 documentation describes a large searchable news corpus and a rolling/dated search interface. Confirm exact archive boundary for the selected query mode during adapter implementation.
- `available_at` basis:
  - If a documented GDELT first-seen timestamp exists in the response, store it as provider-seen metadata.
  - Use project `first_seen_at` as `available_at` until PIT assumptions are validated.
- Conservative rights assumptions:

| Right | Assumption |
|---|---:|
| storage_allowed | yes, metadata only |
| internal_analysis_allowed | yes, metadata only |
| external_llm_allowed | no for publisher text |
| embedding_allowed | no for publisher text |
| redistribution_allowed | no for publisher text |

Basis: GDELT data is broadly usable, but linked publisher content is third-party copyrighted material. Store URLs, source metadata, and timestamps; do not scrape or persist article bodies.

### 11. NewsAPI

- Dataset fit: daily general-news search/discovery if procurement approves a production plan and content-use rules.
- Official documentation:
  - Everything endpoint: <https://newsapi.org/docs/endpoints/everything>
  - Pricing: <https://newsapi.org/pricing>
  - Terms: <https://newsapi.org/terms>
- API / endpoint shape:
  - `GET https://newsapi.org/v2/everything`
  - Params include `q`, `searchIn`, `sources`, `domains`, `excludeDomains`, `from`, `to`, `language`, `sortBy`, `pageSize`, and `page`.
  - `pageSize` default/max is 100 in the official endpoint docs.
  - Response includes `articles[]` with source metadata, title, description, URL, image URL, `publishedAt`, and truncated content.
- Auth / key needs:
  - API key required, via `apiKey` query param or `X-Api-Key` header.
  - Developer/free plan is for development/testing, not production or internal production workflows.
- Pagination / rate-limit notes:
  - Uses `page` and `pageSize`.
  - Pricing page lists plan-specific request quotas, freshness delay, and history depth. Developer plan is limited and delayed.
- Timezone / DST / update-time hints:
  - Endpoint docs describe `publishedAt` as UTC timestamp.
  - Provider discovery timestamp is not documented in the endpoint response.
- Historical depth:
  - Plan-specific. Pricing page lists one-month history for Developer and deeper history for paid plans.
- `available_at` basis:
  - Store `publishedAt` as publisher/source publication time.
  - Use project `first_seen_at` as `available_at`.
- Conservative rights assumptions:

| Right | Assumption |
|---|---:|
| storage_allowed | conditional |
| internal_analysis_allowed | conditional |
| external_llm_allowed | no |
| embedding_allowed | no |
| redistribution_allowed | no |

Basis: NewsAPI terms and pricing constrain plan usage and third-party copyrighted content. Do not use Developer plan for production/internal workflows. Do not place article content, descriptions, or snippets into external LLMs or embeddings without explicit rights.

## Suggested MVP source map

| Provider role | Source | Dataset | Production readiness |
|---|---|---|---|
| `us.instruments.exchange_directory` | Nasdaq Trader Symbol Directory | instruments | Needs rights review; technically simple. |
| `us.instruments.sec_cik_crosswalk` | SEC company tickers JSON | instruments / issuer crosswalk | Ready with fair-access controls. |
| `us.filings.sec_metadata` | SEC EDGAR submissions API | SEC filing metadata | Ready with 10 rps global cap and User-Agent. |
| `us.rates.treasury` | U.S. Treasury XML | rates | Ready; no exact availability timestamp. |
| `us.fx.fed_h10` | Federal Reserve DDP H.10 | FX | Ready; exact update timing needs source metadata check. |
| `us.macro.fred_alfred` | FRED/ALFRED | macro observations/releases | Non-ingest by default; use only for discovery/manual cross-check unless approved. |
| `us.macro.bls` | BLS Public Data API | macro observations/releases | Ready with registration key for full MVP depth. |
| `us.macro.bea` | BEA API | macro observations/releases | Ready with BEA key; per-dataset adapter metadata still needed. |
| `us.bars.equity_daily` | Massive / Polygon | daily bars | Contracted only; otherwise fixtures. |
| `us.bars.equity_daily_dev` | Alpha Vantage | daily bars | Fixture/dev only unless commercial rights approved. |
| `us.news.sec_official` | SEC RSS | daily official regulatory news | Ready for metadata/headline use. |
| `us.news.discovery` | GDELT DOC API | general-news metadata | Metadata-only; no publisher body. |
| `us.news.commercial` | NewsAPI or Massive News | general-news search/news | Contracted only; no body/LLM/embedding until approved. |

## Rights matrix summary

| Source | Storage | Internal analysis | External LLM | Embedding | Redistribution |
|---|---:|---:|---:|---:|---:|
| Nasdaq Trader Symbol Directory | conditional | conditional | no | no | no |
| SEC EDGAR APIs / SEC RSS metadata | yes | yes | yes, metadata only | yes, metadata only | conditional |
| FRED/ALFRED | no by default | no by default | no | no | no |
| U.S. Treasury XML rates | yes | yes | yes | yes | conditional |
| Federal Reserve DDP H.10 | yes | yes | yes | yes | conditional |
| BLS API | yes | yes | yes | yes | conditional |
| BEA API | yes | yes | yes | yes | conditional |
| Massive / Polygon | conditional | conditional | no | no | no |
| Alpha Vantage | conditional | conditional | no | no | no |
| GDELT DOC API | yes, metadata only | yes, metadata only | no for publisher text | no for publisher text | no for publisher text |
| NewsAPI | conditional | conditional | no | no | no |

`conditional` means the adapter must bind rights to a reviewed source config, plan, contract, source notes, or legal approval before enabling the capability.

## Open implementation gaps

1. Daily equity bars require a licensed market-data provider. Massive/Polygon is a viable API shape, but rights must be confirmed before production storage or any LLM/embedding use.
2. General daily news requires a procurement/legal decision. SEC RSS is safe but narrow; GDELT is discovery metadata; NewsAPI/Massive News require plan-specific review.
3. Nasdaq symbol-directory rights need explicit review before redistributing snapshots or using them in external LLM/embedding workflows.
4. BEA release schedule timezone should be confirmed from its machine-readable feed or explicit page text before using release times as exact `scheduled_at`.
5. FRED must remain non-ingest by default. If the project later wants FRED data, create a separate approval issue with legal/project-owner review, series allowlist, series-note checks, and explicit storage/LLM/embedding decisions.
6. For all sources without precise official availability timestamps, adapters must mark rows as `first_seen_at` based and not intraday point-in-time safe for periods before the first project capture.

## Primary sources consulted

- GitHub issue #2: <https://github.com/Detachm/macro-data-platform/issues/2>
- Nasdaq Trader Symbol Directory definitions: <https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs>
- Nasdaq Trader calendar: <https://www.nasdaqtrader.com/trader.aspx?id=Calendar>
- SEC EDGAR APIs: <https://www.sec.gov/search-filings/edgar-application-programming-interfaces>
- SEC privacy / automated-access policy: <https://www.sec.gov/about/privacy-information>
- SEC RSS feeds: <https://www.sec.gov/about/rss-feeds>
- SEC company tickers JSON: <https://www.sec.gov/files/company_tickers.json>
- FRED API docs: <https://fred.stlouisfed.org/docs/api/fred/>
- FRED API key docs: <https://fred.stlouisfed.org/docs/api/api_key.html>
- FRED series observations: <https://fred.stlouisfed.org/docs/api/fred/series_observations.html>
- FRED release dates: <https://fred.stlouisfed.org/docs/api/fred/releases_dates.html>
- FRED real-time periods: <https://fred.stlouisfed.org/docs/api/fred/realtime_period.html>
- FRED API terms: <https://fred.stlouisfed.org/docs/api/terms_of_use.html>
- U.S. Treasury daily interest-rate XML feed: <https://home.treasury.gov/treasury-daily-interest-rate-xml-feed>
- Federal Reserve H.10 DDP: <https://www.federalreserve.gov/datadownload/Choose.aspx?rel=H10>
- Federal Reserve disclaimer: <https://www.federalreserve.gov/disclaimer.htm>
- BLS API signatures v2: <https://www.bls.gov/developers/api_signature_v2.htm>
- BLS API FAQs: <https://www.bls.gov/developers/api_faqs.htm>
- BLS API terms: <https://www.bls.gov/developers/termsOfService.htm>
- BLS release calendar: <https://www.bls.gov/schedule/news_release/>
- BEA API signup / overview: <https://apps.bea.gov/api/signup/>
- BEA API user guide PDF: <https://apps.bea.gov/api/_pdf/bea_web_service_api_user_guide.pdf>
- BEA API terms PDF: <https://apps.bea.gov/API/_pdf/bea_api_tos.pdf>
- BEA release schedule: <https://www.bea.gov/news/schedule>
- Massive stocks aggregates docs: <https://massive.com/docs/rest/stocks/aggregates/custom-bars>
- Massive stock news docs: <https://massive.com/docs/rest/stocks/news>
- Massive pricing: <https://massive.com/pricing>
- Massive business terms: <https://massive.com/legal/businesses-terms-of-service>
- Alpha Vantage API docs: <https://www.alphavantage.co/documentation/>
- Alpha Vantage terms: <https://www.alphavantage.co/terms_of_service/>
- Alpha Vantage premium/pricing: <https://www.alphavantage.co/premium/>
- GDELT DOC 2.0 API docs: <https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/>
- GDELT about: <https://www.gdeltproject.org/about.html>
- GDELT hosted API rate-limiting note: <https://blog.gdeltproject.org/ukraine-api-rate-limiting-web-ngrams-3-0/>
- NewsAPI Everything endpoint: <https://newsapi.org/docs/endpoints/everything>
- NewsAPI pricing: <https://newsapi.org/pricing>
- NewsAPI terms: <https://newsapi.org/terms>
