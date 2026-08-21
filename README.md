# Meta Supplement Tracker

Initial production-oriented foundation for discovering supplement brands advertising on Meta. Apify's `solidcode/meta-ads-library-scraper` Actor is the primary commercial discovery provider. Meta's official Ad Library API provider remains available as an alternative for its documented UK/EU coverage.

No external-data integration is simulated. The Apify Actor supplies linked Instagram metadata where Meta exposes it, and qualifying advertisers can be synchronized to one Google Sheets tab. Reviews and defensible spend estimation remain unimplemented until verified sources are selected.

## Current capabilities

- FastAPI service with `GET /` and `GET /health`
- Environment-based settings with no embedded credentials
- Typed domain models for brands, ads, social data, reviews, and candidates
- Apify Actor discovery for active commercial ads in the UK, supported EU countries, USA, and Canada
- Optional advertiser-page enrichment with linked Instagram username and follower count
- PostgreSQL scan history with advertiser/ad upserts and follower observations
- Idempotent Google Sheets candidate output backed by PostgreSQL advertiser identity
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
| `TARGET_MIN_MONTHLY_SPEND_USD` | `5000` |
| `TARGET_MAX_MONTHLY_SPEND_USD` | `30000` |
| `TARGET_MIN_INSTAGRAM_FOLLOWERS` | `10000` |
| `TARGET_MAX_INSTAGRAM_FOLLOWERS` | `100000` |
| `DESIRABLE_TRUSTPILOT_REVIEW_COUNT` | `300` |
| `SCAN_INTERVAL_HOURS` | `12` |
| `PROVIDER_RETRY_ATTEMPTS` | `3` |
| `DATABASE_URL` | Required only when persistence is enabled; reference Railway PostgreSQL's `DATABASE_URL` |
| `PERSIST_SCAN_RESULTS` | `false`; must be `true` for the complete `--run-once` pipeline |
| `DATABASE_CONNECT_TIMEOUT_SECONDS` | `10`; fail-closed connection timeout before paid discovery starts |
| `META_ACCESS_TOKEN` | Required only when `META_AD_PROVIDER=meta_ad_library` |
| `META_AD_PROVIDER` | `apify` for the primary provider; `meta_ad_library` remains available |
| `META_API_VERSION` | `v26.0`; update only after reviewing Meta's versioned documentation |
| `META_REQUEST_TIMEOUT_SECONDS` | `30` |
| `META_MAX_PAGES_PER_QUERY` | `100`; local safety ceiling, not a Meta rate limit |
| `APIFY_API_TOKEN` | Required when `META_AD_PROVIDER=apify`; keep it in Railway or local `.env` only |
| `APIFY_ACTOR_ID` | `solidcode/meta-ads-library-scraper` |
| `APIFY_MAX_RESULTS_PER_QUERY` | `500`; maximum results for each country Actor run, shared across all search terms |
| `APIFY_MAX_TOTAL_CHARGE_USD_PER_RUN` | `0.02`; server-side ceiling for each Actor run |
| `APIFY_INCLUDE_ADVERTISER_DETAILS` | `true`; enables documented page and linked Instagram enrichment |
| `APIFY_MONTHLY_BUDGET_GBP` | `30`; aborts before starting paid runs when projected monthly usage exceeds this guard |
| `APIFY_BUDGET_GBP_PER_USD` | `1.0`; conservative conversion applied to Apify's USD usage figures |
| `APIFY_REQUEST_TIMEOUT_SECONDS` | `120`; overall wait limit for each Actor run |
| `INSTAGRAM_PROVIDER`, `INSTAGRAM_API_KEY` | Instagram provider placeholders |
| `REVIEWS_PROVIDER`, `REVIEWS_API_KEY` | Reviews provider placeholders |
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

After this code is deployed and the Railway reference variable exists, apply and verify the migration inside the application container:

```bash
railway ssh -- alembic upgrade head
railway ssh -- alembic current
railway ssh -- python -m app.jobs.brand_scan --check-db
```

The connectivity command prints only `{"database": "reachable"}` and never prints the connection URL. Migrations are intentionally explicit rather than being run during every web-service startup.

With persistence enabled, scan commands verify the database before any paid Apify Actor start. A run creates a `scan_runs` row, upserts advertisers by Meta page ID and ads by Meta ad ID, writes one advertiser observation per scan, then records its counts. Provider, persistence, or output failures mark the scan failed when the database remains writable. An unavailable or missing database aborts clearly; results are never silently discarded. The JSON output includes the persisted scan-run ID.

## Google Sheets candidate output

Google Sheets output requires `PERSIST_SCAN_RESULTS=true`: PostgreSQL remains the source of stable advertiser identity, the original first-seen date, and the row mapping. The spreadsheet receives no hidden identity column, metadata tab, or second application-created tab. The complete `--run-once` command fails closed unless both persistence and Sheets output are enabled.

The configured tab contains exactly these visible columns:

| First seen | Brand | Region | Instagram | Followers | Active ads | Spend est. | Spend source | Reviews | Review source |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

Only advertisers with a known Instagram follower count from 10,000 through 100,000 inclusive are written. Unknown, lower, and higher counts are excluded rather than guessed. `First seen` is the advertiser's original PostgreSQL date in `YYYY-MM-DD` format. A new advertiser receives one row with blank spend and review fields. A repeated advertiser updates columns A–F in its mapped row; columns G–J are never overwritten, preserving future spend/review values or formulas. Writes are batched, duplicate input advertisers are collapsed by PostgreSQL ID, and transient HTTP 429/5xx responses use bounded exponential retries.

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

The implementation uses the documented Sheets API v4 [spreadsheet structure updates](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate) and [values batch updates](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/batchUpdate). Its retry policy follows Google's [Sheets API quota guidance](https://developers.google.com/workspace/sheets/api/limits): rate limits and transient server responses use bounded exponential backoff.

### Run the complete pipeline once

After PostgreSQL, Apify, and Google Sheets are configured, run:

```bash
python -m app.jobs.brand_scan --run-once
```

This command performs exactly one discovery run: Apify aggregates and deduplicates ads by advertiser, all returned advertisers/ads/observations are persisted, known Instagram follower counts are filtered inclusively from 10,000 through 100,000, and qualifying advertisers are synchronized to their PostgreSQL-mapped Sheet rows. Unknown and out-of-range follower counts remain in PostgreSQL but are not written to the Sheet. Spend and review cells remain blank for new rows, while repeat updates touch only columns A–F and preserve existing values in G–J.

Database and Sheets connectivity are checked before paid discovery. The pipeline invokes the Meta provider once; Apify's paid Actor start retains its no-automatic-retry behavior, while the existing monthly budget and `maxTotalChargeUsd` guards remain active. Any persistence or Sheets failure is surfaced and the scan run is marked failed when PostgreSQL remains writable.

For a future safe production validation, use this exact UK-only, 20-result command after reviewing the current Actor pricing and account usage:

```bash
railway ssh -- env SCAN_REGIONS=UK APIFY_MAX_RESULTS_PER_QUERY=20 APIFY_INCLUDE_ADVERTISER_DETAILS=true APIFY_MAX_TOTAL_CHARGE_USD_PER_RUN=0.03 python -m app.jobs.brand_scan --run-once
```

The command performs one country run with at most 20 enriched results and a server-side `$0.03` run ceiling. Do not run it until a paid live validation is explicitly approved. `--meta-only` remains available for discovery diagnostics and uses persistence or Sheets only when their respective flags are enabled.

## Apify Meta ads provider

### Setup and documented API contract

Create an Apify account and copy its API token from **Apify Console → Settings → API & Integrations**. Configure it only in `.env` locally or in the Railway service's Variables tab:

```env
META_AD_PROVIDER=apify
APIFY_API_TOKEN=<secret Apify API token>
APIFY_ACTOR_ID=solidcode/meta-ads-library-scraper
APIFY_MAX_RESULTS_PER_QUERY=500
APIFY_MAX_TOTAL_CHARGE_USD_PER_RUN=0.02
APIFY_INCLUDE_ADVERTISER_DETAILS=true
APIFY_MONTHLY_BUDGET_GBP=30
APIFY_BUDGET_GBP_PER_USD=1.0
APIFY_REQUEST_TIMEOUT_SECONDS=120
```

The implementation follows the Actor's current [input/output reference and pricing](https://apify.com/solidcode/meta-ads-library-scraper) and [generated OpenAPI definition](https://apify.com/solidcode/meta-ads-library-scraper/api/openapi). It starts runs asynchronously through Apify's documented [Run Actor endpoint](https://docs.apify.com/api/v2/actors-runs-post), polls the [Get run endpoint](https://docs.apify.com/api/v2/actor-run-get), and pages through the [default dataset items endpoint](https://docs.apify.com/api/v2/actor-run-dataset-items-get). Tokens are sent in the recommended `Authorization: Bearer` header, never in URLs or logs.

Each country run sends only documented Actor input fields: `searchTerms`, `country`, `adActiveStatus="ACTIVE"`, `adType="ALL"`, `scrapeAdDetails=true`, `includeAboutPage`, `onlyTotalCount=false`, and `maxResults`. Creative-detail enrichment is enabled because it is the documented source of CTA landing URLs and snapshot URLs. `includeAboutPage` follows `APIFY_INCLUDE_ADVERTISER_DETAILS`; when enabled, the Actor documents page category, likes, verification, About text, linked Instagram username, and Instagram follower count.

The provider retains documented ad IDs, page IDs and names, status, platforms, dates, body copy, Ad Library and snapshot URLs, genuine CTA landing URLs, advertiser details, and real audience fields if present. Linked Instagram username and integer follower count are normalized into `SocialStats` and the brand handle. Missing, malformed, or conflicting values remain unknown. The Actor does not currently document an Instagram profile-URL output field, so the project does not construct or guess one. Commercial spend is not estimated or inferred, so `estimated_monthly_spend_usd` remains `null`. The Actor documents spend, impressions, and several audience disclosures as political/issue-ad-only; if such a real declared range is returned it is preserved separately as `declared_spend`, but ordinary supplement searches must not be assumed to contain it.

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

The provider calculates the documented event-cost estimate using the configured result limit and enrichment flag. The per-run ceiling defaults to a conservative `$0.02` and is independent of the result-count limit. A ceiling below the calculated estimate is logged and can intentionally stop a large requested result set before all rows are collected. Non-positive ceilings and a single-run ceiling above the converted monthly budget are rejected during configuration; the full multi-country conflict reserves the entire configured ceiling for every planned run and checks it against live monthly usage before any paid run starts. `APIFY_MAX_RESULTS_PER_QUERY` is always positive, so unlimited Actor runs are not exposed. Read-only API calls retry bounded transient network, HTTP 429, and server failures. A paid Actor start is not automatically retried after a network error because an ambiguous retry could create a second billable run. Runs exceeding the configured timeout are aborted. The monthly pre-check is deliberately account-wide and fail-closed, but it cannot make independent concurrent processes atomic; use Apify's account spending controls as an additional account-level backstop.

### Manual and Railway live test

With local credentials, run discovery only:

```bash
python -m app.jobs.brand_scan --meta-only
```

If the token exists only in Railway, do not copy it locally. After deploying this code, install and authenticate the current Railway CLI, link it to the project/service, then execute a capped test inside the running container:

```bash
railway ssh -- env SCAN_REGIONS=UK APIFY_MAX_RESULTS_PER_QUERY=20 APIFY_INCLUDE_ADVERTISER_DETAILS=true APIFY_MAX_TOTAL_CHARGE_USD_PER_RUN=0.03 python -m app.jobs.brand_scan --meta-only
```

Railway's current [`railway ssh` documentation](https://docs.railway.com/cli/ssh) supports running a command in the deployed service. This override performs one UK country run with at most 20 fully enriched rows. Current documented event pricing projects $0.023: $0.005 to start plus 20 × $0.0009, protected by a hard $0.03 ceiling. It does not expose the token. The command invokes Meta discovery and, if enabled, persistence and candidate-sheet synchronization; it does not run a separate Instagram scraper, reviews, or scheduling.

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

The 12-hour interval is configuration only. No in-process scheduler is started. Once all providers are implemented, invoke the scan from a Railway cron service or add a dedicated worker/scheduler; avoid running a scheduler in every web replica.

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

## Unimplemented integrations

- Defensible commercial Meta spend estimation; the official Ad Library API does not return commercial spend
- EU countries absent from the current SolidCode Actor country schema
- Instagram profile URL, because the current Actor does not document one
- Trustpilot or other reviews enrichment; the corresponding Sheet columns intentionally remain blank
- Scheduled invocation of the scan job

Each is intentionally left behind an interface rather than returning fabricated data. Provider choice must be validated against official documentation, access requirements, terms, and available fields before implementation.
