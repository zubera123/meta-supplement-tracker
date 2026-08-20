# Meta Supplement Tracker

Initial production-oriented foundation for discovering supplement brands advertising on Meta. The first real provider uses Meta's official Ad Library API for commercial ads delivered to the UK and European Union, the locations for which Meta currently documents commercial-ad API access.

No external-data integration is simulated. Instagram, reviews, and Google Docs remain explicit provider contracts until verified providers are selected and implemented.

## Current capabilities

- FastAPI service with `GET /` and `GET /health`
- Environment-based settings with no embedded credentials
- Typed domain models for brands, ads, social data, reviews, and candidates
- Official Meta Ad Library API discovery for active UK/EU commercial ads
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
| `META_ACCESS_TOKEN` | Required Facebook Developer access token authorised for the Ad Library API |
| `META_AD_PROVIDER` | `meta_ad_library` |
| `META_API_VERSION` | `v26.0`; update only after reviewing Meta's versioned documentation |
| `META_REQUEST_TIMEOUT_SECONDS` | `30` |
| `META_MAX_PAGES_PER_QUERY` | `100`; local safety ceiling, not a Meta rate limit |
| `INSTAGRAM_PROVIDER`, `INSTAGRAM_API_KEY` | Instagram provider placeholders |
| `REVIEWS_PROVIDER`, `REVIEWS_API_KEY` | Reviews provider placeholders |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service-account JSON value or provider-specific reference; pending implementation |
| `GOOGLE_DOC_ID` | Target Google document ID |

Never commit `.env`, credentials, or service-account files. The supplied `.gitignore` excludes them.

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

This performs only Meta discovery. It does not invoke Instagram, reviews, Google Docs, scoring output, scheduling, or fake data. Results are emitted as JSON. A shape-only, sanitised example is:

```json
[
  {
    "brand": {"name": "<page name>", "source_id": "<page id>"},
    "regions": ["UK"],
    "estimated_monthly_spend_usd": null,
    "active_ad_count": 2,
    "oldest_active_ad": "<UTC timestamp>",
    "newest_active_ad": "<UTC timestamp>",
    "ads": [
      {
        "ad_id": "<Meta Library ID>",
        "page_id": "<page id>",
        "page_name": "<page name>",
        "creative_bodies": ["<ad text>"],
        "platforms": ["facebook", "instagram"],
        "matched_regions": ["UK"]
      }
    ]
  }
]
```

## Tests

```bash
python -m pytest
```

## Provider architecture and scan job

Provider contracts live under `app/services/`. Implementations normalize verified provider responses into the models in `app/models.py`. `MetaAdLibraryProvider` is the first concrete implementation.

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
- Commercial Meta discovery outside the UK/EU official API coverage
- Instagram follower enrichment
- Trustpilot or other reviews enrichment
- Google Docs authentication and writes
- Scheduled invocation of the scan job

Each is intentionally left behind an interface rather than returning fabricated data. Provider choice must be validated against official documentation, access requirements, terms, and available fields before implementation.
