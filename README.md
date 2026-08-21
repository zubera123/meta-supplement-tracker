# Meta Supplement Tracker

Initial production-oriented foundation for discovering supplement brands advertising on Meta. Apify's `solidcode/meta-ads-library-scraper` Actor is the primary commercial discovery provider. Meta's official Ad Library API provider remains available as an alternative for its documented UK/EU coverage.

No external-data integration is simulated. The Apify Actor supplies linked Instagram metadata where Meta exposes it, qualifying advertisers can be synchronized to one Google Sheets tab, and optional Trustpilot public Business Unit data can enrich candidates that have a genuine destination domain. Spend is stored as a conservative range or `Unknown`, never as a provider-reported exact commercial spend.

## Current capabilities

- FastAPI service with `GET /` and `GET /health`
- Environment-based settings with no embedded credentials
- Typed domain models for brands, ads, social data, reviews, and candidates
- Apify Actor discovery for active commercial ads in the UK, supported EU countries, USA, and Canada
- Optional advertiser-page enrichment with linked Instagram username and follower count
- Conservative keyword relevance filtering from real advertiser and ad text
- PostgreSQL scan history with advertiser/ad upserts and follower observations
- Idempotent Google Sheets candidate output backed by PostgreSQL advertiser identity
- Railway Cron Job entry point with PostgreSQL-backed overlap protection
- Alembic-managed database schema using SQLAlchemy 2.x and psycopg 3
- Official Meta Ad Library API discovery retained as a UK/EU alternative
- Cursor pagination, bounded transient retries, ad deduplication, and advertiser aggregation
- Inclusive qualification filters for estimated monthly spend of $5,000–$30,000 and Instagram audiences of 10,000–100,000
- Optional 10-point review bonus at 300 reviews; reviews never determine qualification
- Retry-aware scan orchestration that writes all qualifying brands
- Railway-compatible Python 3.12 container
- Unit tests for filters, scoring, and HTTP endpoints

## Local setup

Python 3.12 is required.

```bash
python -m venv .venv
```

Activate the environment on macOS/Linux:

```bash
source .venv/bin/activate
```

Or on PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Install dependencies and create local configuration:

```bash
python -m pip install -r requirements.txt
cp .env.example .env
```

On PowerShell, use `Copy-Item .env.example .env` instead of `cp`.

Start the API:

```bash
uvicorn app.main:app --reload --port 8000
```

Open `http://localhost:8000/`, `http://localhost:8000/health`, or `http://localhost:8000/docs`.

## Configuration

Settings are loaded from environment variables and, for local development, `.env`.

| Variable | Default / purpose |
| --- | --- |
| `APP_ENV` | `development` |
| `LOG_LEVEL` | `INFO` |
| `PORT` | `8000`; Railway supplies this in production |
| `SCAN_REGIONS` | `UK,Europe,USA,Canada` |
| `SUPPLEMENT_CATEGORIES` | Comma-separated broad supplement categories |
| `SUPPLEMENT_RELEVANCE_INCLUDE_KEYWORDS` | Comma-separated supplement signals; defaults cover supplements, sports nutrition, wellness, and pet supplements |
| `SUPPLEMENT_RELEVANCE_EXCLUDE_KEYWORDS` | Comma-separated obvious non-supplement signals such as produce, personal care, mineral specimens, apparel, and equipment |
| `TARGET_MIN_MONTHLY_SPEND_USD` | `5000` |
| `TARGET_MAX_MONTHLY_SPEND_USD` | `30000` |
| `SPEND_ESTIMATION_ENABLED` | `true`; calculates ranges locally from observed data |
| `SPEND_TARGET_MIN_USD` / `SPEND_TARGET_MAX_USD` | `5000` / `30000`; meaningful-overlap target |
| `SPEND_CPM_UK_LOW_USD` / `SPEND_CPM_UK_HIGH_USD` | `8` / `18`; directional assumption |
| `SPEND_CPM_EUROPE_LOW_USD` / `SPEND_CPM_EUROPE_HIGH_USD` | `5` / `18`; directional assumption |
| `SPEND_CPM_USA_LOW_USD` / `SPEND_CPM_USA_HIGH_USD` | `10` / `25`; directional assumption |
| `SPEND_CPM_CANADA_LOW_USD` / `SPEND_CPM_CANADA_HIGH_USD` | `8` / `20`; directional assumption |
| `SPEND_REACH_FREQUENCY_LOW` / `SPEND_REACH_FREQUENCY_HIGH` | `1` / `3`; reach-to-impression assumption |
| `SPEND_ACTIVITY_DAILY_LOW_USD` / `SPEND_ACTIVITY_DAILY_HIGH_USD` | `10` / `50`; fallback per-active-ad assumption |
| `SPEND_MIN_OBSERVATION_DAYS` | `7`; minimum age for monthlyizing audience disclosures |
| `TARGET_MIN_INSTAGRAM_FOLLOWERS` | `10000` |
| `TARGET_MAX_INSTAGRAM_FOLLOWERS` | `100000` |
| `DESIRABLE_TRUSTPILOT_REVIEW_COUNT` | `300`; legacy scorer setting |
| `SCAN_INTERVAL_HOURS` | `12`; descriptive application setting—the Railway Cron expression controls production timing |
| `SCAN_MAX_RUNTIME_SECONDS` | `2700`; 45-minute deadline for the complete `--run-once` pipeline |
| `CANDIDATE_DISQUALIFY_SCANS` | `3`; consecutive successful complete observations required before removing an explicitly disqualified company from Sheets |
| `CANDIDATE_ABSENT_DAYS` | `30`; successful complete scan-equivalent absence window before Sheet removal |
| `PROVIDER_RETRY_ATTEMPTS` | `3` |
| `DATABASE_URL` | Required only when persistence is enabled; reference Railway PostgreSQL's `DATABASE_URL` |
| `PERSIST_SCAN_RESULTS` | `false`; must be `true` for the complete `--run-once` pipeline |
| `DATABASE_CONNECT_TIMEOUT_SECONDS` | `10`; fail-closed connection timeout before paid discovery starts |
| `META_ACCESS_TOKEN` | Required only when `META_AD_PROVIDER=meta_ad_library` |
| `META_AD_PROVIDER` | `apify` for the primary provider; `meta_ad_library` remains available |
| `META_API_VERSION` | `v26.0`; update only after reviewing Meta's versioned documentation |
| `META_REQUEST_TIMEOUT_SECONDS` | `30` |
| `META_MAX_PAGES_PER_QUERY` | `100`; local safety ceiling, not a Meta rate limit |
| `APIFY_API_TOKEN` | Required when `META_AD_PROVIDER=apify` or `REVIEWS_PROVIDER=apify_trustpilot`; keep it in Railway or local `.env` only |
| `APIFY_ACTOR_ID` | `solidcode/meta-ads-library-scraper` |
| `APIFY_META_ACTOR_BUILD` | `1.0.7`; exact known-good Actor build number sent through Apify's documented `build` run option |
| `APIFY_MAX_RESULTS_PER_QUERY` | `15`; maximum enriched results for each country Actor run, shared across all search terms |
| `APIFY_MAX_TOTAL_CHARGE_USD_PER_RUN` | `0.019`; server-side ceiling for each Actor run |
| `APIFY_INCLUDE_ADVERTISER_DETAILS` | `true`; enables documented page and linked Instagram enrichment |
| `APIFY_MONTHLY_BUDGET_GBP` | `30`; aborts before starting paid runs when projected monthly usage exceeds this guard |
| `APIFY_BUDGET_GBP_PER_USD` | `1.0`; conservative conversion applied to Apify's USD usage figures |
| `APIFY_REQUEST_TIMEOUT_SECONDS` | `120`; overall wait limit for each Actor run |
| `APIFY_TRUSTPILOT_ACTOR_ID` | `automation-lab/trustpilot-scraper` |
| `APIFY_TRUSTPILOT_MAX_TOTAL_CHARGE_USD_PER_RUN` | `0.01`; strict server-side ceiling for each review lookup |
| `INSTAGRAM_PROVIDER`, `INSTAGRAM_API_KEY` | Instagram provider placeholders |
| `REVIEWS_ENABLED` | `false`; enables optional candidate review enrichment |
| `REVIEWS_PROVIDER` | `apify_trustpilot` (practical default) or `trustpilot`; ignored when reviews are disabled |
| `TRUSTPILOT_API_KEY` | Trustpilot API-module key; secret, never log or commit it |
| `TRUSTPILOT_MIN_DESIRABLE_REVIEWS` | `300`; positive signal only, never a rejection rule |
| `TRUSTPILOT_REFRESH_HOURS` | `24`; Trustpilot's display cache-refresh requirement |
| `TRUSTPILOT_REQUEST_TIMEOUT_SECONDS` | `30` |
| `TRUSTPILOT_MIN_REQUEST_INTERVAL_SECONDS` | `0.4`; keeps request throughput below documented limits |
| `REVIEWS_API_KEY` | Reserved legacy placeholder; not used by Trustpilot |
| `GOOGLE_SHEETS_ENABLED` | `false`; enable candidate synchronization after PostgreSQL and Sheets are configured |
| `GOOGLE_SHEET_ID` | Target spreadsheet ID; the supplied example points to the intended workbook |
| `GOOGLE_SHEET_TAB` | `Candidates`; created when absent if the service account has Editor access |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Complete Google Cloud service-account JSON credential; secret and required when Sheets is enabled |

Never commit `.env`, credentials, or service-account files. The supplied `.gitignore` excludes them.

## PostgreSQL persistence

Persistence uses SQLAlchemy 2.x with the synchronous psycopg 3 dialect and Alembic migrations. Railway PostgreSQL exposes `DATABASE_URL` on the **database service**. Railway does not automatically copy that variable to the application service; add a reference variable to the application so it remains synchronized with the database credentials.

The app accepts Railway's `postgres://` or `postgresql://` URL and changes only the SQLAlchemy driver scheme to `postgresql+psycopg://`. It does not parse, reconstruct, log, or store the credentials separately. SQLite is used only in isolated unit tests for behavior whose SQL semantics are compatible; runtime persistence rejects SQLite URLs.

### Add PostgreSQL to the existing Railway project

1. Open the existing Railway project and production environment.
2. On the project canvas, click **+ New** (or use `Ctrl/Cmd + K`).
3. Select **Database → PostgreSQL** and wait for the PostgreSQL service to become healthy.
4. Open the `meta-supplement-tracker` application service, then open **Variables**.
5. Click **Add Reference Variable** and select `DATABASE_URL` from the PostgreSQL service. If the service is named `Postgres`, Railway represents this as `DATABASE_URL=${{Postgres.DATABASE_URL}}`; use the actual service name shown in the project.
6. Add `PERSIST_SCAN_RESULTS=true` to the application service.
7. Deploy the resulting application variable changes. Do not expose the PostgreSQL service publicly; the referenced URL uses Railway private networking.

Railway also creates `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, and `PGDATABASE` on the PostgreSQL service, but this app deliberately uses only the canonical `DATABASE_URL`. `DATABASE_PUBLIC_URL` is created only if database Public Access is enabled and is not needed by the deployed application.

### Migrations and connectivity

For a local PostgreSQL database, set `DATABASE_URL` in the untracked `.env` file and run:

```bash
alembic upgrade head
python -m app.jobs.brand_scan --check-db
```

The authoritative web deployment runs `alembic upgrade head` through Railway's `preDeployCommand`. Railway executes it in a separate container with production variables and private-network access; a non-zero migration exit blocks the new web deployment. The Cron config deliberately has no pre-deploy command, avoiding concurrent Alembic runs against the same database. After deployment, verify the revision without reapplying it:

```bash
railway ssh -- alembic current
railway ssh -- python -m app.jobs.brand_scan --check-db
```

The connectivity command prints only `{"database": "reachable"}` and never prints the connection URL. See Railway's [pre-deploy command documentation](https://docs.railway.com/deployments/pre-deploy-command); this is intentionally a deployment step, not a web-process startup step.

With persistence enabled, scan commands verify the database before any paid Apify Actor start. A run creates a `scan_runs` row, upserts advertisers by Meta page ID and ads by Meta ad ID, writes one advertiser observation per scan—including its relevance decision and reason—then records its counts. Provider, persistence, or output failures mark the scan failed when the database remains writable. An unavailable or missing database aborts clearly; results are never silently discarded. The JSON output includes the persisted scan-run ID.

## Conservative spend estimation

`SPEND_ESTIMATION_ENABLED=true` calculates a monthly range after discovery and before the observation is stored. It does not call another API and does not alter the Apify cost controls. Every observation stores the low/high bounds, method, confidence, observed inputs, assumptions, and target-overlap decision. Historical observations are retained rather than overwritten.

Evidence is used in this order:

1. **Finite impressions × regional CPM (medium confidence):** each ad's cumulative impressions are monthlyized as `observed impressions × 30 / active days`; the low and high totals are then `monthly impressions / 1,000 × CPM`. Open-ended disclosures such as `>1M` are not converted into a fake upper bound.
2. **Finite reach × assumed frequency × regional CPM (low confidence):** reach is people rather than impressions, so the formula is `monthly reach × frequency / 1,000 × CPM`. Frequency defaults to a deliberately wide 1–3 range.
3. **Activity model (very-low confidence):** when commercial ads expose no finite audience metric, the fallback is `active ads × assumed daily spend per active ad × 30`. It runs only when at least one active ad has the configured minimum longevity (seven days by default), or a prior scan supplies repeated activity evidence. Defaults are $10–$50 per active ad per day. Active-ad count and longevity are real; the dollars-per-ad factor is an explicit configurable assumption, not observed spend. Its directional range is displayed with source `Activity model - very rough`, but `spend_target_match` is always `null`: it can neither qualify nor disqualify an advertiser.
4. **Unknown confidence:** insufficient evidence stays unknown.

All numeric estimates are rounded outward to $100. Regional CPM defaults are wide directional assumptions: UK $8–$18, Europe $5–$18, USA $10–$25, and Canada $8–$20. They are based on current third-party benchmark ranges, not Meta first-party price data. Meta defines CPM as spend divided by impressions times 1,000, which justifies the inverse calculation, and separately defines reach as people and impressions as screen entries. See [Meta's CPM definition](https://www.facebook.com/help/www/214576695231407), [Meta's reach/impressions definitions](https://www.facebook.com/help/274400362581037), [SolidCode Actor output documentation](https://apify.com/solidcode/meta-ads-library-scraper), and directional 2026 country benchmarks from [Adculator](https://adculator.com/benchmarks/facebook-cpm-by-country/) and [Adligator](https://adligator.com/blog/meta-ads-cpm-by-country-benchmarks).

Only genuine impressions-based and reach-based estimates may evaluate the target. For those methods, the target-match rule requires at least 50% of the estimated interval to overlap $5,000–$30,000. A zero-width estimate passes only when its value is inside the inclusive target. Boundary contact with no positive interval overlap does not pass. A reliable `spend_target_match=false` prevents a current Sheet write/update, while `true` remains eligible. Activity and unknown estimates return `null` and remain eligible for now; they cannot qualify or disqualify a candidate. Every advertiser and estimate is still persisted, and existing Sheet rows are not retroactively deleted.

Preview estimates from existing PostgreSQL ads and observations without calling Apify or writing data:

```bash
python -m app.jobs.brand_scan --estimate-spend-dry-run
```

Apply migration `20260821_0004` before enabling this version in production. The relevant variables and conservative defaults are listed in `.env.example`; every low value must be no greater than its matching high value.

## Optional Trustpilot review enrichment

Set `REVIEWS_ENABLED=true` to enrich only advertisers that already pass supplement relevance and have 10,000–100,000 Instagram followers. Review data never controls candidate qualification. At least 300 reviews sets an internal desirable flag; lower counts remain valid and unknown data receives no penalty. `REVIEWS_ENABLED=false` disables both providers.

### Apify provider (practical default)

`REVIEWS_PROVIDER=apify_trustpilot` uses the existing `APIFY_API_TOKEN` with [`automation-lab/trustpilot-scraper`](https://apify.com/automation-lab/trustpilot-scraper). The Actor's current documented input schema supports `mode`, `searchQueries`, and `maxResults`; its documented business result contains `businessId`, `domain`, `trustScore`, `stars`, and `numberOfReviews`. Each lookup submits exactly:

```json
{"mode":"search","searchQueries":["the-real-ad-domain.example"],"maxResults":1}
```

The query value comes only from an existing ad's `landing_page_domain`, or from the hostname of its genuine `landing_page_url` when no documented domain is present. It is never derived from the advertiser name. Returned data is accepted only when `type=business` and the normalized `domain` exactly matches the submitted domain. The dataset request selects only business metadata fields; no review text is requested. A missing or mismatched result stays unknown.

As documented on the Actor page on 21 August 2026, pricing is pay per event: $0.001 per run start plus $0.00345/Free, $0.00300/Bronze, $0.00234/Silver, $0.00180/Gold, $0.00120/Platinum, or $0.00084/Diamond per result. One business-result lookup therefore costs approximately $0.00445 on Free and $0.00400 on Bronze, before any included Apify credits. The live Pricing tab remains authoritative. The app sends both Apify's documented `maxItems=1` and `maxTotalChargeUsd=0.01`, with `restartOnError=false`.

Before every paid review start, the provider reads account-wide `current.monthlyUsageUsd` from Apify's [Get limits endpoint](https://docs.apify.com/api/v2/users-me-limits-get). Current usage, conservative in-process reservations, and the next $0.01 ceiling are converted with `APIFY_BUDGET_GBP_PER_USD` and rejected if they would exceed `APIFY_MONTHLY_BUDGET_GBP` (£30 by default). This budget includes both Meta and review Actor usage because Apify reports account-wide usage. The paid start request is never retried; only safe run-status and dataset reads use bounded transient retries. Actor failures, timeouts, malformed results, and budget rejections fail softly for that advertiser and preserve earlier valid Sheet review values.

Configure the same shared/reference values on the Railway web and Cron services:

```env
REVIEWS_ENABLED=true
REVIEWS_PROVIDER=apify_trustpilot
APIFY_API_TOKEN=
APIFY_TRUSTPILOT_ACTOR_ID=automation-lab/trustpilot-scraper
APIFY_TRUSTPILOT_MAX_TOTAL_CHARGE_USD_PER_RUN=0.01
TRUSTPILOT_MIN_DESIRABLE_REVIEWS=300
TRUSTPILOT_REFRESH_HOURS=24
```

`python -m app.jobs.brand_scan --check-reviews` validates the Apify token, account-limits endpoint, and Actor metadata with authenticated GET requests. It reports `actor_started=false` and never constructs the Meta provider or starts either paid Actor.

### Official Trustpilot API alternative

Set `REVIEWS_PROVIDER=trustpilot` to use the existing official Trustpilot Business Units provider instead. It requires `TRUSTPILOT_API_KEY` and remains unchanged as an alternative.

The implementation uses only Trustpilot's documented public endpoints:

- `GET https://api.trustpilot.com/v1/business-units/find?name={base-domain}` resolves a Business Unit by domain.
- `GET https://api.trustpilot.com/v1/business-units/{businessUnitId}` refreshes a cached match without resolving it again.
- Authentication is the `apikey` request header.
- The documented response fields normalized are `id`, `name.identifying`/`name.referring`, `numberOfReviews.total`, `score.trustScore`, and `score.stars`.

The source is always `Trustpilot`. PostgreSQL retains real landing-page URLs/domains, caches the matched Business Unit ID and domain, latest count/scores, and last successful resolution/refresh time. Every scan observation records its review status, values, desirable flag, match identity, and reason. A fresh cache suppresses API calls. Trustpilot says Business Unit IDs normally do not change and advises storing them instead of resolving each time. Because the Sheet displays the values, the default refresh interval is 24 hours rather than seven days, following Trustpilot's Content Refresh Guidelines.

Domain resolution never derives a website from the advertiser name. The Apify review provider accepts only Meta-ad `ctaDomain` normalized as `landing_page_domain`, or the hostname of a genuine `ctaUrl` when no documented domain is present. The official provider retains its existing support for a genuine `Brand.website` value as well. Multiple conflicting domains, missing destinations, IP addresses, and Meta/Instagram destinations remain unknown with a persisted reason. The current SolidCode Actor documents and can return `ctaDomain`/`ctaUrl` when ad-detail scraping is enabled, but individual commercial results may omit both, so Trustpilot coverage is necessarily partial.

HTTP 429 and transient 5xx/network failures use bounded retries. A numeric `Retry-After` is honored when it fits inside the configured scan retry window; a longer rate-limit wait fails softly and is deferred to a later scheduled run. Trustpilot currently recommends no more than 833 calls per five minutes or 10,000 calls/hour. Requests are serialized and spaced by at least 0.4 seconds by default, capping steady throughput at 750 calls per five minutes and 9,000 calls/hour before network latency. Persistent ID reuse and the 24-hour refresh gate reduce it further. Trustpilot outages and malformed responses are logged without credentials and stored as an error outcome; they do not cancel the paid Meta result, PostgreSQL persistence, or Sheet sync. Existing valid review cells are preserved if a later lookup is unavailable.

Trustpilot requires a Trustpilot for Business account with access to its API module. Configure these variables in both the Railway web and Cron services, preferably through shared/reference variables:

```env
REVIEWS_ENABLED=true
REVIEWS_PROVIDER=trustpilot
TRUSTPILOT_API_KEY=
TRUSTPILOT_MIN_DESIRABLE_REVIEWS=300
TRUSTPILOT_REFRESH_HOURS=24
TRUSTPILOT_REQUEST_TIMEOUT_SECONDS=30
TRUSTPILOT_MIN_REQUEST_INTERVAL_SECONDS=0.4
```

Verify the selected provider without starting Meta discovery or a paid Actor:

```bash
python -m app.jobs.brand_scan --check-reviews
```

For the official provider, the check resolves Trustpilot's own documented domain. For the Apify provider, it uses only free metadata/limits GET requests. Both print only provider/connectivity status. Apply migrations through `20260821_0006` before enabling the Apify provider; `0006` adds only a cached review-source column so `Trustpilot via Apify` survives refresh-cache reuse. The existing review observation/history tables are reused.

## Google Sheets candidate output

Google Sheets output requires `PERSIST_SCAN_RESULTS=true`: PostgreSQL remains the source of canonical company identity, original first-seen date, lifecycle history, and a cached row location. The spreadsheet receives no visible identity column, metadata tab, or second application-created tab. Instead, every managed row receives project-visible Google Sheets developer metadata containing only its internal canonical-company integer ID. Google documents that row metadata follows its row through sorting, inserted rows, and moves, and is deleted with the row. The app searches that metadata on every synchronization; it never blindly trusts the cached physical row number.

The configured tab contains exactly these visible columns:

| First seen | Brand | Region | Instagram | Followers | Active ads | Spend est. | Spend source | Reviews | Review source |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

Only companies with a known Instagram follower count from 10,000 through 100,000 inclusive are written. Unknown, lower, and higher counts are excluded rather than guessed. `First seen` is the company's earliest original advertiser date in PostgreSQL. Spend estimates update columns G–H. A successful review match writes its numeric count and either `Trustpilot via Apify` or `Trustpilot` to I–J. Missing or failed review enrichment leaves those cells unchanged, preserving an earlier valid value. No visible columns are added. Unknown spend is written explicitly as `Unknown`. Writes are batched, duplicate input companies are collapsed by canonical ID, and transient HTTP 429/5xx responses use bounded exponential retries.

### Company identity and stale-row policy

Meta Page ID remains the primary advertiser identity and every original page, ad, and observation is preserved. Separate pages group into one canonical output company only when their real ad destinations resolve to the exact same normalized registrable domain. Normalization lowercases IDNA hostnames, removes scheme, `www`, ports, path, query, and fragment, and uses an offline bundled Public Suffix List snapshot so domains such as `shop.example.co.uk` become `example.co.uk` without a runtime network fetch. Conflicting destinations, missing destinations, IPs, Meta/Instagram links, and known redirect/link-shortener domains do not merge. Names and Instagram similarity are never company keys. PostgreSQL stores the current verified mapping, canonical domain, and mapping history; a later conflicting destination does not silently move an established Page identity.

Grouped output uses the earliest first-seen date, unique ad IDs across the pages, their real region union, and the most recent current valid Instagram identity. It selects the strongest existing spend evidence without summing page estimates, which avoids double-counting overlapping audiences, and uses the latest real review result. All underlying page-level records remain queryable.

An existing Sheet row is removed only after one of these conservative rules:

- `CANDIDATE_DISQUALIFY_SCANS` consecutive successful, uncapped observations explicitly show irrelevance, followers outside 10,000–100,000, or a reliable impressions/reach spend result outside the target.
- `CANDIDATE_ABSENT_DAYS` is converted to successful complete scan equivalents using `SCAN_INTERVAL_HOURS`; at the defaults, 30 days requires 60 relevant-region scans in which the company is absent.

Unknown follower/spend data and mere absence in one scan are not explicit failures. Activity-model spend never disqualifies. Failed, aborted, capped, and unrelated-region scans do not advance either counter. Removal affects only the visible Sheet row; advertisers, companies, ads, observations, mappings, and qualification/removal events remain in PostgreSQL. A later qualifying observation recreates the row with the original `First seen`. The first successful post-migration sync reconciles existing visible rows through their stored visible identity, attaches developer metadata in place, and does not append duplicates.

### Google Cloud and spreadsheet setup

1. In [Google Cloud Console](https://console.cloud.google.com/), select or create the project that will own this integration.
2. Follow Google's [Sheets API Python setup](https://developers.google.com/workspace/sheets/api/quickstart/python) to open **APIs & Services → Library**, find **Google Sheets API**, and click **Enable**.
3. Open **IAM & Admin → Service Accounts** and follow Google's [service-account creation guide](https://docs.cloud.google.com/iam/docs/service-accounts-create). No project-wide role is needed merely to edit a spreadsheet that is shared directly with the account.
4. Open that service account and follow Google's [service-account key guide](https://docs.cloud.google.com/iam/docs/keys-create-delete) to select **Keys → Add key → Create new key → JSON → Create**. Google downloads the private key once. Store it securely; never commit it or place it in `.env.example`.
5. Copy the service account's `client_email` value. Open the target spreadsheet in Google Sheets and follow Google's [Drive sharing instructions](https://support.google.com/drive/answer/2494822?hl=en) to add that email with **Editor** access.
6. In the Railway application service's **Variables** tab, add the complete downloaded JSON object as the secret `GOOGLE_SERVICE_ACCOUNT_JSON`. Railway accepts the JSON value directly; do not split its fields into separate variables or print it in logs.
7. Add the remaining non-secret variables:

   ```env
   GOOGLE_SHEETS_ENABLED=true
   GOOGLE_SHEET_ID=1m6DRz8GzhW_Xn297WDfa-amZHMFn9v8qqhvRLzlIy84
   GOOGLE_SHEET_TAB=Candidates
   ```

8. Redeploy the application, then apply the new PostgreSQL row-mapping migration:

   ```bash
   railway ssh -- alembic upgrade head
   railway ssh -- alembic current
   ```

9. Verify authentication, spreadsheet access, the `Candidates` tab, exact headers, and Editor permission without running Apify or adding a candidate:

   ```bash
   railway ssh -- python -m app.jobs.brand_scan --check-sheets
   ```

The check creates only the configured `Candidates` tab and its exact header when missing. Otherwise, it performs an idempotent header write to verify Editor permission. It never starts a scan, calls Apify, or adds fake candidates. A successful response contains `"spreadsheet": "reachable"`, `"headers": "ready"`, and `"write_access": "verified"` without exposing credentials.

For local use, put the same four variables in the untracked `.env` file and run `python -m app.jobs.brand_scan --check-sheets`. If access is denied, confirm that the Sheets API is enabled in the credential's project and that the spreadsheet—not merely a similarly named Drive file—was shared with the exact service-account email as Editor.

The implementation uses the documented Sheets API v4 [developer metadata behavior](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.developerMetadata), [metadata search guide](https://developers.google.com/workspace/sheets/api/guides/metadata), [spreadsheet structure updates](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate), and [values batch updates](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/batchUpdate). Its retry policy follows Google's [Sheets API quota guidance](https://developers.google.com/workspace/sheets/api/limits): rate limits and transient server responses use bounded exponential backoff.

### Run the complete pipeline once

After PostgreSQL, Apify, and Google Sheets are configured, run:

```bash
python -m app.jobs.brand_scan --run-once
```

This command performs exactly one discovery run: Apify aggregates and deduplicates ads by advertiser, the deterministic supplement relevance filter evaluates real provider-returned text, all returned advertisers/ads/observations and relevance reasons are persisted, known Instagram follower counts are filtered inclusively from 10,000 through 100,000, and qualifying relevant advertisers are synchronized to their PostgreSQL-mapped Sheet rows. Reliable impressions/reach estimates outside the spend target suppress the current Sheet write; activity/unknown spend remains eligible. Optional Trustpilot enrichment runs only for current candidate-output advertisers and never changes qualification. Irrelevant, unknown-follower, out-of-range, and reliable spend-disqualified advertisers remain in PostgreSQL.

Database and Sheets connectivity are checked before paid discovery. The pipeline invokes the Meta provider once; Apify's paid Actor start retains its no-automatic-retry behavior, while the existing monthly budget and `maxTotalChargeUsd` guards remain active. `SCAN_MAX_RUNTIME_SECONDS=2700` bounds the complete `--run-once` lifecycle. On expiry the current Actor is aborted when one has started, the scan is marked failed when its row exists, resources and the advisory lock are released, and the process exits non-zero.

For a future safe production validation, use this exact UK-only, 15-result command after reviewing the current Actor pricing and account usage:

```bash
railway ssh -- env SCAN_REGIONS=UK APIFY_MAX_RESULTS_PER_QUERY=15 APIFY_INCLUDE_ADVERTISER_DETAILS=true APIFY_MAX_TOTAL_CHARGE_USD_PER_RUN=0.019 python -m app.jobs.brand_scan --run-once
```

The command performs one country run with at most 15 enriched results and a server-side `$0.019` run ceiling. Current event pricing projects `$0.0185`. Do not run it until a paid live validation is explicitly approved. `--meta-only` remains available for discovery diagnostics and uses persistence or Sheets only when their respective flags are enabled.

## Automated production scans on Railway

Railway Cron Jobs are the scheduling layer; the FastAPI service remains a separate, continuously running web service. The scanner does not contain an in-process timer or loop. Railway's current [Cron Jobs documentation](https://docs.railway.com/cron-jobs) says scheduled services run their configured start command, must exit when complete, use five-field UTC cron expressions, and skip a scheduled launch while the previous Railway execution is still active. Railway also notes that execution can vary by a few minutes, so the schedule is not an absolute-to-the-minute guarantee.

Use a second Railway service in the existing production environment with this configuration:

| Setting | Value |
| --- | --- |
| Service name | `meta-supplement-tracker-scan` |
| Source | The same private GitHub repository and `main` branch |
| Config File | `/railway.cron.json` |
| Start command | `python -m app.jobs.brand_scan --run-once` (supplied by the config file) |
| Cron Schedule | `0 0,12 * * *` |
| Restart policy | `NEVER` (supplied by the config file) |
| Public domain | None |

The expression runs every day at **00:00 UTC** and **12:00 UTC**. Railway evaluates the schedule in UTC, including when the UK changes between GMT and BST. The dedicated config file uses the existing Dockerfile but deliberately has no HTTP health check because a Cron Job runs to completion and exits; it does not replace or modify the web service's `railway.json` or `/health` behavior. `NEVER` prevents Railway process-level restarts from accidentally repeating a paid job after a non-zero exit.

### Create the Cron Job in the Railway UI

1. Open the existing project and select the `production` environment.
2. Select **+ New → GitHub Repo**, choose the same private `meta-supplement-tracker` repository, and name the new service `meta-supplement-tracker-scan`.
3. In the new service's **Settings**, confirm the source branch is `main`, then set **Config File** to `/railway.cron.json`. This custom file is supported by Railway's [Config as Code documentation](https://docs.railway.com/config-as-code).
4. In **Settings → Cron Schedule**, enter `0 0,12 * * *`.
5. In **Variables**, use **Add Reference Variable** to reference the existing production app service's values. Reference variables are documented by Railway and avoid copying secrets. The Cron service needs the same values for:

   - `APP_ENV`, `LOG_LEVEL`, `SCAN_REGIONS`, `SCAN_MAX_RUNTIME_SECONDS`, `SUPPLEMENT_CATEGORIES`
   - `SUPPLEMENT_RELEVANCE_INCLUDE_KEYWORDS`, `SUPPLEMENT_RELEVANCE_EXCLUDE_KEYWORDS`
   - `TARGET_MIN_INSTAGRAM_FOLLOWERS`, `TARGET_MAX_INSTAGRAM_FOLLOWERS`
   - `PROVIDER_RETRY_ATTEMPTS`, `PROVIDER_RETRY_MIN_WAIT_SECONDS`, `PROVIDER_RETRY_MAX_WAIT_SECONDS`
   - `DATABASE_URL`, `PERSIST_SCAN_RESULTS`, `DATABASE_CONNECT_TIMEOUT_SECONDS`
   - `META_AD_PROVIDER`, `APIFY_API_TOKEN`, `APIFY_ACTOR_ID`, `APIFY_META_ACTOR_BUILD`, `APIFY_MAX_RESULTS_PER_QUERY`
   - `APIFY_MAX_TOTAL_CHARGE_USD_PER_RUN`, `APIFY_INCLUDE_ADVERTISER_DETAILS`, `APIFY_MONTHLY_BUDGET_GBP`, `APIFY_BUDGET_GBP_PER_USD`, `APIFY_REQUEST_TIMEOUT_SECONDS`
   - `APIFY_TRUSTPILOT_ACTOR_ID`, `APIFY_TRUSTPILOT_MAX_TOTAL_CHARGE_USD_PER_RUN`
   - `GOOGLE_SHEETS_ENABLED`, `GOOGLE_SHEET_ID`, `GOOGLE_SHEET_TAB`, `GOOGLE_SERVICE_ACCOUNT_JSON`
   - `REVIEWS_ENABLED`, `REVIEWS_PROVIDER`, `TRUSTPILOT_API_KEY`, `TRUSTPILOT_MIN_DESIRABLE_REVIEWS`, `TRUSTPILOT_REFRESH_HOURS`, `TRUSTPILOT_REQUEST_TIMEOUT_SECONDS`, `TRUSTPILOT_MIN_REQUEST_INTERVAL_SECONDS`

   The production values must still make `PERSIST_SCAN_RESULTS=true` and `GOOGLE_SHEETS_ENABLED=true`; `--run-once` fails closed otherwise. Do not give the Cron service a public domain.
6. Review Railway's staged service and variable changes, then deploy the Cron service. No paid scan runs at deploy time; the command runs only at the next scheduled execution or an explicitly requested manual execution.

The application adds a second overlap guard beyond Railway's own active-execution skip. Before building the provider or starting Apify, every `--run-once` process calls PostgreSQL `pg_try_advisory_lock` with one application-owned key. If another scheduled or manual full scan holds the lock, the invocation logs `status=overlap`, exits successfully, and makes no paid provider call. The session lock is explicitly released after both success and failure. PostgreSQL also cleans it up when the database session ends, including an ungraceful disconnect, so there is no persistent stale-lock row to expire.

Every acquired run creates its `scan_runs` row before the Sheets preflight. Logs identify the actual UTC invocation time, scan-run ID, success/failure status, ads found, advertisers found, candidates written, and failure reason. Provider errors are logged without credential values. A database outage cannot be recorded in that unavailable database, but it fails before Apify starts.

To run one protected scan manually, use:

```bash
railway ssh -- python -m app.jobs.brand_scan --run-once
```

To disable automation without changing application code, clear **Cron Schedule** in the `meta-supplement-tracker-scan` service's Settings. Manual `--run-once` remains available and still uses the same PostgreSQL lock. To inspect history without exposing connection credentials, use Railway's PostgreSQL query interface with this read-only query:

```sql
SELECT id, started_at, finished_at, status, regions,
       ads_found, advertisers_found, error_message
FROM scan_runs
ORDER BY started_at DESC
LIMIT 50;
```

Runtime logs are available from the Cron service's deployment/execution history. Do not run `railway variables` when collecting diagnostics because it may display unsealed values.

### Supplement relevance rules

The relevance filter uses no LLM and makes no external calls. It normalizes and searches only the advertiser/page name, Facebook page category, page About text, and real creative body, caption, description, title, and CTA text returned by the configured Meta provider.

The rules deliberately favor recall:

1. If an obvious exclusion keyword appears in the advertiser name or page category and no supplement include keyword appears in that same identity text, exclude the advertiser. This prevents a produce advertiser from passing merely because its ad mentions a nutrient.
2. Otherwise, if any include keyword appears in the available identity, About, or creative text, include it.
3. Otherwise, if an exclusion keyword appears elsewhere in the available text, exclude it.
4. If neither kind of signal appears, keep the ambiguous advertiser rather than guessing that it is irrelevant.

Default include terms cover supplements, vitamins, multivitamins, mineral supplements, protein, whey, creatine, pre-workout, collagen, gummies, electrolytes, greens powders, probiotics, omega 3, magnesium, wellness supplements, pet/dog/cat supplements, sports/gym nutrition, amino acids, BCAA, hydration powder, meal replacements, nutrition shakes, and fish oil. Default exclusions cover explicit produce businesses, restaurants/food delivery, hair/skin care and cosmetics, mineral specimens/gemstones/jewellery, clothing/apparel, and gym equipment. Both lists are environment-configurable. Matching uses normalized whole words or phrases, not substrings.

Excluded advertisers are still upserted with all ads and follower observations. PostgreSQL stores `supplement_relevant` and `relevance_reason` on each observation. Apply migration `20260821_0003` before the first filtered production run:

```bash
railway ssh -- alembic upgrade head
railway ssh -- alembic current
```

## Apify Meta ads provider

### Setup and documented API contract

Create an Apify account and copy its API token from **Apify Console → Settings → API & Integrations**. Configure it only in `.env` locally or in the Railway service's Variables tab:

```env
META_AD_PROVIDER=apify
APIFY_API_TOKEN=<secret Apify API token>
APIFY_ACTOR_ID=solidcode/meta-ads-library-scraper
APIFY_META_ACTOR_BUILD=1.0.7
APIFY_MAX_RESULTS_PER_QUERY=15
APIFY_MAX_TOTAL_CHARGE_USD_PER_RUN=0.019
APIFY_INCLUDE_ADVERTISER_DETAILS=true
APIFY_MONTHLY_BUDGET_GBP=30
APIFY_BUDGET_GBP_PER_USD=1.0
APIFY_REQUEST_TIMEOUT_SECONDS=120
```

The implementation follows the Actor's current [input/output reference and pricing](https://apify.com/solidcode/meta-ads-library-scraper) and [generated OpenAPI definition](https://apify.com/solidcode/meta-ads-library-scraper/api/openapi). It starts runs asynchronously through Apify's documented [Run Actor endpoint](https://docs.apify.com/api/v2/actors-runs-post), using its `build` query parameter with exact build number `1.0.7`, polls the [Get run endpoint](https://docs.apify.com/api/v2/actor-run-get), and pages through the [default dataset items endpoint](https://docs.apify.com/api/v2/actor-run-dataset-items-get). Upgrade the build setting only after deliberate contract regression testing. Tokens are sent in the recommended `Authorization: Bearer` header, never in URLs or logs.

Each country run sends only documented Actor input fields: `searchTerms`, `country`, `adActiveStatus="ACTIVE"`, `adType="ALL"`, `scrapeAdDetails=true`, `includeAboutPage`, `onlyTotalCount=false`, and `maxResults`. Creative-detail enrichment is enabled because it is the documented source of CTA landing URLs and snapshot URLs. `includeAboutPage` follows `APIFY_INCLUDE_ADVERTISER_DETAILS`; when enabled, the Actor documents page category, likes, verification, About text, linked Instagram username, and Instagram follower count.

The provider retains documented ad IDs, page IDs and names, status, platforms, dates, body copy, Ad Library and snapshot URLs, genuine CTA landing URLs, advertiser details, and real audience fields if present. Linked Instagram username and integer follower count are normalized into `SocialStats` and the brand handle. Missing, malformed, or conflicting values remain unknown. The Actor does not currently document an Instagram profile-URL output field, so the project does not construct or guess one. Provider-reported commercial spend remains absent and `estimated_monthly_spend_usd` remains `null`; the separate estimator stores a range with evidence and assumptions. The Actor documents spend, impressions, and several audience disclosures as political/issue-ad-only; if such a real declared range is returned it is preserved separately as `declared_spend`, but ordinary supplement searches must not be assumed to contain it.

The Instagram follower filter is inclusive: 10,000 and 100,000 both pass; values below or above fail; unknown remains unknown. Meta-only discovery reports the filter status but does not discard records, making missing enrichment visible. The future full pipeline can use the same filter when qualifying candidates.

### Region handling

The Actor accepts one country code per keyword-search run. This project maps regions as follows:

| Configured region | Actor queries |
| --- | --- |
| `UK` | `GB` |
| `USA` | `US` |
| `Canada` | `CA` |
| `Europe` or `EU` | `AT, BE, CZ, DK, FI, FR, DE, GR, HU, IE, IT, NL, PL, PT, RO, ES, SE` |

The current Actor schema does not list `BG, HR, CY, EE, LV, LT, LU, MT, SK, SI`. Those EU countries are logged and skipped rather than sent as undocumented inputs. Results are deduplicated by Meta ad archive ID and then grouped/deduplicated by advertiser page ID across keywords, countries, and regions.

### Cost controls and retries

The Actor's current public pricing metadata lists a $0.005 Actor-start event, $0.40 per 1,000 ad rows, $0.10 per 1,000 rows for creative details, and $0.40 per 1,000 rows for advertiser details. With both enrichments enabled, 1,000 results cost $0.90 in row events plus $0.005 to start the run, or $0.905 per country run. Without advertiser enrichment, the corresponding amount is $0.505. Apify bills in USD; these figures exclude taxes or external currency-conversion effects.

Before any paid run starts, the provider reads `current.monthlyUsageUsd` from Apify's documented [account limits endpoint](https://docs.apify.com/api/v2/users-me-limits-get). It reserves `APIFY_MAX_TOTAL_CHARGE_USD_PER_RUN` for every planned country run, converts the projected account usage using `APIFY_BUDGET_GBP_PER_USD`, and aborts if the total exceeds `APIFY_MONTHLY_BUDGET_GBP`. The default `1.0` deliberately treats each reported USD as £1, which is conservative relative to a lower GBP-per-USD rate; update the setting only if you intentionally want another budgeting rate. Every run receives the configured ceiling through Apify's documented `maxTotalChargeUsd` server-side parameter.

The provider calculates the documented event-cost estimate using the configured result limit and enrichment flag. The default 15 enriched rows project `$0.0185` per country, protected by a `$0.019` ceiling. Across the configured 20 countries twice daily, the event estimate is about `$22.20` per 30 days and the ceiling-based maximum is `$22.80`, using the deliberately conservative `$1 = £1` guard conversion. A ceiling below the calculated estimate is logged and can intentionally stop a requested result set before all rows are collected. Non-positive ceilings and a single-run ceiling above the converted monthly budget are rejected during configuration; the full multi-country conflict reserves the entire configured ceiling for every planned run and checks it against live monthly usage before any paid run starts. `APIFY_MAX_RESULTS_PER_QUERY` is always positive, so unlimited Actor runs are not exposed. Read-only API calls retry bounded transient network, HTTP 429, and server failures. A paid Actor start is not automatically retried after a network error because an ambiguous retry could create a second billable run. Runs exceeding either the per-country provider timeout or the whole-scan deadline are aborted. The monthly pre-check is deliberately account-wide and fail-closed, but it cannot make independent concurrent processes atomic; use Apify's account spending controls as an additional account-level backstop.

### Manual and Railway live test

With local credentials, run discovery only:

```bash
python -m app.jobs.brand_scan --meta-only
```

If the token exists only in Railway, do not copy it locally. After deploying this code, install and authenticate the current Railway CLI, link it to the project/service, then execute a capped test inside the running container:

```bash
railway ssh -- env SCAN_REGIONS=UK APIFY_MAX_RESULTS_PER_QUERY=15 APIFY_INCLUDE_ADVERTISER_DETAILS=true APIFY_MAX_TOTAL_CHARGE_USD_PER_RUN=0.019 python -m app.jobs.brand_scan --meta-only
```

Railway's current [`railway ssh` documentation](https://docs.railway.com/cli/ssh) supports running a command in the deployed service. This override performs one UK country run with at most 15 fully enriched rows. Current documented event pricing projects $0.0185: $0.005 to start plus 15 × $0.0009, protected by a hard $0.019 ceiling. It does not expose the token. The command invokes Meta discovery and, if enabled, persistence and candidate-sheet synchronization; it does not run a separate Instagram scraper, reviews, or scheduling.

A sanitised output shape is:

```json
{
  "unique_advertisers": 1,
  "unique_ads": 2,
  "follower_filter": {"minimum": 10000, "maximum": 100000},
  "persistence": {
    "enabled": true,
    "scan_run_id": 42,
    "status": "succeeded"
  },
  "advertisers": [
    {
      "facebook_page_name": "<page name>",
      "facebook_page_id": "<page id>",
      "active_ad_count": 2,
      "instagram_username": "<username or null>",
      "instagram_profile_url": null,
      "instagram_followers": 25000,
      "passes_instagram_follower_filter": true,
      "instagram_follower_filter_status": "pass"
    }
  ]
}
```

## Meta Ad Library API

### Official access and credentials

Meta's current [Ad Library API access page](https://www.facebook.com/ads/library/api/) documents this authorisation flow:

1. Use a Facebook account.
2. Confirm your identity and location through [facebook.com/ID](https://www.facebook.com/ID). Meta says this process can take a few days.
3. Create a [Meta for Developers](https://developers.facebook.com/) account and accept the Platform Policy.
4. From the Ad Library API access page, select **Access the API**, then create an app through **My Apps → Create App**.
5. Generate the Facebook Developer access token for that authorised app.

The current access page does not name an additional Graph permission to request, so this project does not claim that an undocumented permission is required. Store the resulting token only in `.env` locally or Railway service variables:

```env
META_AD_PROVIDER=meta_ad_library
META_ACCESS_TOKEN=<your authorised token>
META_API_VERSION=v26.0
```

### Documented request contract

The provider calls Meta's versioned Graph endpoint:

```text
GET https://graph.facebook.com/v26.0/ads_archive
```

For each configured keyword and supported region it sends the documented parameters `access_token`, `fields`, `search_terms`, `search_type=KEYWORD_EXACT_PHRASE`, `ad_type=ALL`, `ad_active_status=ACTIVE`, and `ad_reached_countries`. Keyword searches are separate because Meta does not translate search terms and documents a 100-character limit.

The requested Archived Ad fields are:

```text
id,page_id,page_name,ad_creation_time,ad_delivery_start_time,
ad_delivery_stop_time,ad_snapshot_url,ad_creative_bodies,
ad_creative_link_captions,ad_creative_link_descriptions,
ad_creative_link_titles,publisher_platforms,languages,eu_total_reach,
total_reach_by_location,age_country_gender_reach_breakdown,target_ages,
target_gender,target_locations,beneficiary_payers
```

These fields and their location restrictions are defined in Meta's current [Ads Archive endpoint](https://developers.facebook.com/docs/graph-api/reference/ads_archive/) and [Archived Ad object](https://developers.facebook.com/docs/graph-api/reference/archived-ad/) references.

Pagination follows the documented `paging.next` signal and `paging.cursors.after` cursor. Repeated Library IDs are deduplicated across keywords and regions. Results are grouped by `page_id`, with active-ad count and the oldest/newest documented delivery start times. Snapshot URLs are retained but any embedded `access_token` query parameter is removed before storage or output.

### Region support

| Configured region | Commercial discovery | Implementation |
| --- | --- | --- |
| `UK` | Supported | Queries `GB` |
| `Europe` | Supported only for the European Union | Queries the EU-27 country codes |
| `USA` | Unsupported | Logged and skipped |
| `Canada` | Unsupported | Logged and skipped |

Meta documents ads of any type delivered to the UK or EU during the past year. It documents worldwide API access over seven years only for social-issue, election, or political ads. Consequently, this project does not treat the availability of `US` and `CA` request codes as commercial-ad access. `Europe` means the 27 EU member states, not geographic Europe; non-EU countries such as Norway and Switzerland are not queried.

### Reach, impressions, spend, and URLs

For commercial UK/EU ads, the provider retains documented reach and targeting fields when Meta returns them, including `total_reach_by_location`, `age_country_gender_reach_breakdown`, `target_ages`, `target_gender`, and `target_locations`; `eu_total_reach` and `beneficiary_payers` are EU-specific.

Meta's Archived Ad reference marks `spend` and `impressions` as available only for `POLITICAL_AND_ISSUE_ADS`. This provider queries ordinary supplement ads with `ad_type=ALL`, does not request those political-only fields, and always leaves `estimated_monthly_spend_usd` as `null`. The official response model does not expose an advertiser landing-page URL; `ad_snapshot_url` is an Ad Library snapshot, not a landing page.

### Pagination, rate limits, and retries

Meta's endpoint reference documents error `613` when calls exceed the rate limit but does not publish an Ad Library-specific numeric quota. Meta's [Graph API rate-limit documentation](https://developers.facebook.com/docs/graph-api/overview/rate-limiting/) says user-token call-count values are not disclosed and instructs clients to stop making calls after throttling. The provider therefore fails visibly on documented throttle codes rather than looping indefinitely. Network failures, server errors, and responses explicitly marked transient receive bounded exponential retries. `META_MAX_PAGES_PER_QUERY` prevents an unexpectedly large or malformed pagination chain from running forever.

### Manual Meta-only discovery

After configuring the authorised token, run:

```bash
python -m app.jobs.brand_scan --meta-only
```

This performs only Meta discovery. The official provider has no linked Instagram enrichment, so its follower status is unknown. It does not invoke a separate Instagram provider, reviews, scheduling, or fake data. Results use the same advertiser-summary JSON shape documented above. When `PERSIST_SCAN_RESULTS=true`, the same normalized output is stored through the persistence service; Sheets output additionally requires `GOOGLE_SHEETS_ENABLED=true` and excludes unknown followers.


## Tests

```bash
python -m pytest
```

## Provider architecture and scan job

Provider contracts live under `app/services/`. Implementations normalize verified provider responses into the models in `app/models.py`. `ApifyMetaAdsProvider` is selected by `META_AD_PROVIDER=apify`; `MetaAdLibraryProvider` remains selectable with `META_AD_PROVIDER=meta_ad_library`.

`BrandScanJob` accepts provider instances through its constructor. A run retrieves advertisers for every configured region and category, enriches each brand with Instagram and optional review data, evaluates it, and writes the complete qualifying set to the output provider. Provider calls that raise `TransientProviderError` use bounded exponential retries. Configuration and programming errors fail immediately. A failed optional enrichment is logged and treated as unavailable; retrieval and output failures abort the run so failures remain visible.

Production scheduling is provided by the separate Railway Cron Job described above. The FastAPI process never starts a scheduler, and horizontal web replicas therefore cannot multiply scheduled scans.

## Railway deployment preparation

The repository includes a `Dockerfile` and `railway.json`. The container runs as a non-root user and listens on Railway's `$PORT` using:

```bash
/bin/sh -c "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"
```

After the private GitHub repository has been pushed:

1. In the existing Railway project, create a service from the private GitHub repository.
2. Grant Railway access to that private repository if prompted.
3. Add environment variables in Railway rather than committing a `.env` file.
4. Deploy and verify `/health` returns `status: ok`.
5. Add real provider credentials only after the corresponding provider implementation exists.

No Railway deployment is performed by this project setup.

## Current limitations

- EU countries absent from the current SolidCode Actor country schema
- Instagram profile URL, because the current Actor does not document one
- Trustpilot matching is unavailable when Apify omits a reliable CTA destination domain or returns conflicting domains
- Trustpilot access requires a Business account with the API module; this project does not scrape public profile pages

Each is intentionally left behind an interface rather than returning fabricated data. Provider choice must be validated against official documentation, access requirements, terms, and available fields before implementation.
