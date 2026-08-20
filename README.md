# Meta Supplement Tracker

Initial production-oriented foundation for discovering supplement brands advertising on Meta in the UK, Europe, USA, and Canada. The service normalizes provider data, enriches brands, applies qualification rules, and exposes health endpoints for Railway.

No external-data integration is simulated. Meta ads, Instagram, reviews, and Google Docs are represented by explicit provider contracts until verified providers are selected and implemented.

## Current capabilities

- FastAPI service with `GET /` and `GET /health`
- Environment-based settings with no embedded credentials
- Typed domain models for brands, ads, social data, reviews, and candidates
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
| `META_ACCESS_TOKEN` | Reserved for a verified Meta data provider |
| `META_AD_PROVIDER` | Provider selection placeholder |
| `INSTAGRAM_PROVIDER`, `INSTAGRAM_API_KEY` | Instagram provider placeholders |
| `REVIEWS_PROVIDER`, `REVIEWS_API_KEY` | Reviews provider placeholders |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service-account JSON value or provider-specific reference; pending implementation |
| `GOOGLE_DOC_ID` | Target Google document ID |

Never commit `.env`, credentials, or service-account files. The supplied `.gitignore` excludes them.

## Tests

```bash
python -m pytest
```

## Provider architecture and scan job

Provider contracts live under `app/services/`. Implementations must normalize verified provider responses into the models in `app/models.py`; no endpoint or response field is assumed here.

`BrandScanJob` accepts provider instances through its constructor. A run retrieves advertisers for every configured region and category, enriches each brand with Instagram and optional review data, evaluates it, and writes the complete qualifying set to the output provider. Provider calls that raise `TransientProviderError` use bounded exponential retries. Configuration and programming errors fail immediately. A failed optional enrichment is logged and treated as unavailable; retrieval and output failures abort the run so failures remain visible.

The 12-hour interval is configuration only in this initial version. No in-process scheduler is started because provider implementations do not exist yet. Once those are implemented, invoke the scan from a Railway cron service or add a dedicated worker/scheduler; avoid running a scheduler in every web replica.

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

- Meta advertiser discovery and defensible monthly-spend estimation
- Instagram follower enrichment
- Trustpilot or other reviews enrichment
- Google Docs authentication and writes
- Scheduled invocation of the scan job

Each is intentionally left behind an interface rather than returning fabricated data. Provider choice must be validated against official documentation, access requirements, terms, and available fields before implementation.
