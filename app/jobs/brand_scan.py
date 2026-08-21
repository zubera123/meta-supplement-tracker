"""Brand scan orchestration and explicit one-run CLI commands."""

import argparse
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import Settings, get_settings
from app.db import (
    DatabaseConfigurationError,
    DatabasePersistenceError,
    ScanPersistenceService,
)
from app.logging_config import configure_logging
from app.models import AdRecord, Brand, BrandCandidate, ReviewStats, SocialStats
from app.services import ProviderConfigurationError, ProviderError, TransientProviderError
from app.services.google_docs import BrandOutputProvider
from app.services.google_sheets import GoogleSheetsProvider, SheetSyncResult
from app.services.instagram import InstagramProvider
from app.services.meta_ads import (
    ApifyMetaAdsProvider,
    MetaAdLibraryProvider,
    MetaAdsProvider,
)
from app.services.reviews import ReviewsProvider
from app.services.scoring import CandidateScorer
from app.services.scoring import instagram_follower_filter


logger = logging.getLogger(__name__)
T = TypeVar("T")


class BrandScanJob:
    """Coordinate retrieval, enrichment, evaluation, and output."""

    def __init__(
        self,
        *,
        settings: Settings,
        meta_ads: MetaAdsProvider,
        instagram: InstagramProvider,
        reviews: ReviewsProvider,
        scorer: CandidateScorer,
        output: BrandOutputProvider,
    ) -> None:
        self.settings = settings
        self.meta_ads = meta_ads
        self.instagram = instagram
        self.reviews = reviews
        self.scorer = scorer
        self.output = output

    async def _with_retry(self, operation: Callable[[], Awaitable[T]]) -> T:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self.settings.provider_retry_attempts),
            wait=wait_exponential(
                min=self.settings.provider_retry_min_wait_seconds,
                max=self.settings.provider_retry_max_wait_seconds,
            ),
            retry=retry_if_exception_type(TransientProviderError),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                return await operation()
        raise RuntimeError("Retry loop ended without a result")

    async def _enrich_social(self, brand: Brand) -> SocialStats | None:
        try:
            return await self._with_retry(lambda: self.instagram.get_social_stats(brand))
        except Exception:
            logger.exception("Instagram enrichment failed", extra={"brand": brand.name})
            return None

    async def _enrich_reviews(self, brand: Brand) -> ReviewStats | None:
        try:
            return await self._with_retry(lambda: self.reviews.get_review_stats(brand))
        except Exception:
            logger.exception("Review enrichment failed", extra={"brand": brand.name})
            return None

    async def run(self) -> Sequence[BrandCandidate]:
        """Run one scan and return every qualifying candidate written to output."""

        logger.info("Starting brand scan")
        ad_records = await self._with_retry(
            lambda: self.meta_ads.retrieve_advertisers(
                regions=self.settings.regions,
                categories=self.settings.categories,
            )
        )
        qualifying: list[BrandCandidate] = []

        for ad_record in ad_records:
            brand = ad_record.brand
            social_stats = ad_record.social_stats
            if social_stats is None:
                social_stats = await self._enrich_social(brand)
            review_stats = await self._enrich_reviews(brand)
            evaluated = self.scorer.evaluate(
                BrandCandidate(
                    brand=brand,
                    ad_record=ad_record,
                    social_stats=social_stats,
                    review_stats=review_stats,
                )
            )
            if evaluated.qualifies:
                qualifying.append(evaluated)

        await self._with_retry(lambda: self.output.write_candidates(qualifying))
        logger.info(
            "Brand scan completed",
            extra={"advertisers": len(ad_records), "qualifying": len(qualifying)},
        )
        return qualifying


@dataclass(frozen=True)
class CandidatePipelineResult:
    """Results from one discovery, persistence, and Sheet synchronization run."""

    records: Sequence[AdRecord]
    scan_run_id: int | None
    sheet_sync: SheetSyncResult | None


class CandidatePipeline:
    """Run the real candidate pipeline once using configured provider services."""

    def __init__(
        self,
        *,
        settings: Settings,
        meta_ads: MetaAdsProvider,
        persistence: ScanPersistenceService | None,
        sheets: GoogleSheetsProvider | None,
    ) -> None:
        if sheets is not None and persistence is None:
            raise ProviderConfigurationError(
                "Google Sheets output requires PostgreSQL persistence"
            )
        self.settings = settings
        self.meta_ads = meta_ads
        self.persistence = persistence
        self.sheets = sheets

    async def run(self) -> CandidatePipelineResult:
        """Execute once; paid Meta retrieval is intentionally invoked only once."""

        scan_run_id: int | None = None
        if self.persistence is not None:
            # Fail before any paid provider call when persistence is unavailable.
            self.persistence.verify_connection()
        if self.sheets is not None:
            # Fail before any paid provider call when output cannot be reached.
            self.sheets.ensure_ready()

        try:
            if self.persistence is not None:
                scan_run_id = self.persistence.create_scan_run(self.settings.regions)
            records = await self.meta_ads.retrieve_advertisers(
                regions=self.settings.regions,
                categories=self.settings.categories,
            )
            if self.persistence is not None and scan_run_id is not None:
                # Every advertiser is persisted before the follower filter is applied.
                self.persistence.persist_success(scan_run_id, records)

            sheet_sync: SheetSyncResult | None = None
            if self.sheets is not None and self.persistence is not None:
                candidates, row_states = self.persistence.prepare_sheet_candidates(
                    records,
                    minimum_followers=self.settings.target_min_instagram_followers,
                    maximum_followers=self.settings.target_max_instagram_followers,
                )
                sheet_sync = self.sheets.sync_candidates(candidates, row_states)
                self.persistence.save_sheet_row_states(sheet_sync.row_states)
            return CandidatePipelineResult(records, scan_run_id, sheet_sync)
        except Exception as exc:
            if self.persistence is not None and scan_run_id is not None:
                self.persistence.record_failure(scan_run_id, exc)
            raise


async def _run_scan_command(settings: Settings, *, require_full_outputs: bool) -> int:
    persistence: ScanPersistenceService | None = None
    try:
        if require_full_outputs and not settings.persist_scan_results:
            raise ProviderConfigurationError(
                "PERSIST_SCAN_RESULTS=true is required for --run-once"
            )
        if require_full_outputs and not settings.google_sheets_enabled:
            raise ProviderConfigurationError(
                "GOOGLE_SHEETS_ENABLED=true is required for --run-once"
            )
        if settings.google_sheets_enabled and not settings.persist_scan_results:
            raise ProviderConfigurationError(
                "PERSIST_SCAN_RESULTS=true is required for Google Sheets output "
                "because stable identity and First seen come from PostgreSQL"
            )
        if settings.persist_scan_results:
            persistence = ScanPersistenceService.from_database_url(
                settings.database_url,
                connect_timeout_seconds=settings.database_connect_timeout_seconds,
            )
        sheets = (
            _build_sheets_provider(settings)
            if settings.google_sheets_enabled
            else None
        )
        provider = _build_meta_provider(settings)
        result = await CandidatePipeline(
            settings=settings,
            meta_ads=provider,
            persistence=persistence,
            sheets=sheets,
        ).run()
        output = _build_meta_only_output(result.records, settings)
        output["persistence"] = {
            "enabled": persistence is not None,
            "scan_run_id": result.scan_run_id,
            "status": "succeeded" if persistence is not None else "disabled",
        }
        output["google_sheets"] = {
            "enabled": sheets is not None,
            "appended": result.sheet_sync.appended if result.sheet_sync else 0,
            "updated": result.sheet_sync.updated if result.sheet_sync else 0,
        }
        print(json.dumps(output, indent=2))
        return 0
    finally:
        if persistence is not None:
            persistence.close()


async def _run_meta_only(settings: Settings) -> int:
    """Run discovery with persistence and Sheets when their flags are enabled."""

    return await _run_scan_command(settings, require_full_outputs=False)


async def _run_once(settings: Settings) -> int:
    """Run the complete production candidate pipeline exactly once."""

    return await _run_scan_command(settings, require_full_outputs=True)


def _build_meta_provider(settings: Settings) -> MetaAdsProvider:
    configured_provider = (settings.meta_ad_provider or "meta_ad_library").casefold()
    if configured_provider == "apify":
        return ApifyMetaAdsProvider(
            api_token=settings.apify_api_token,
            actor_id=settings.apify_actor_id,
            max_results_per_query=settings.apify_max_results_per_query,
            max_total_charge_usd_per_run=(
                settings.apify_max_total_charge_usd_per_run
            ),
            include_advertiser_details=settings.apify_include_advertiser_details,
            monthly_budget_gbp=settings.apify_monthly_budget_gbp,
            budget_gbp_per_usd=settings.apify_budget_gbp_per_usd,
            request_timeout_seconds=settings.apify_request_timeout_seconds,
            retry_attempts=settings.provider_retry_attempts,
            retry_min_wait_seconds=settings.provider_retry_min_wait_seconds,
            retry_max_wait_seconds=settings.provider_retry_max_wait_seconds,
        )
    if configured_provider == "meta_ad_library":
        return MetaAdLibraryProvider(
            access_token=settings.meta_access_token,
            api_version=settings.meta_api_version,
            request_timeout_seconds=settings.meta_request_timeout_seconds,
            max_pages_per_query=settings.meta_max_pages_per_query,
            retry_attempts=settings.provider_retry_attempts,
            retry_min_wait_seconds=settings.provider_retry_min_wait_seconds,
            retry_max_wait_seconds=settings.provider_retry_max_wait_seconds,
        )
    raise ProviderError(
        f"Unsupported META_AD_PROVIDER for scan command: {settings.meta_ad_provider}"
    )


def _check_database(settings: Settings) -> int:
    persistence = ScanPersistenceService.from_database_url(
        settings.database_url,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    try:
        persistence.verify_connection()
    finally:
        persistence.close()
    print(json.dumps({"database": "reachable"}))
    return 0


def _build_sheets_provider(settings: Settings) -> GoogleSheetsProvider:
    return GoogleSheetsProvider(
        spreadsheet_id=settings.google_sheet_id,
        sheet_tab=settings.google_sheet_tab,
        service_account_json=settings.google_service_account_json,
        retry_attempts=settings.provider_retry_attempts,
        retry_min_wait_seconds=settings.provider_retry_min_wait_seconds,
        retry_max_wait_seconds=settings.provider_retry_max_wait_seconds,
    )


def _check_sheets(settings: Settings) -> int:
    provider = _build_sheets_provider(settings)
    provider.ensure_ready(verify_write_access=True)
    print(
        json.dumps(
            {
                "spreadsheet": "reachable",
                "tab": settings.google_sheet_tab,
                "headers": "ready",
                "write_access": "verified",
            }
        )
    )
    return 0


def _build_meta_only_output(
    records: Sequence[AdRecord], settings: Settings
) -> dict[str, object]:
    advertisers: list[dict[str, object]] = []
    for record in records:
        social = record.social_stats
        followers = social.instagram_followers if social else None
        filter_result = instagram_follower_filter(
            followers,
            minimum=settings.target_min_instagram_followers,
            maximum=settings.target_max_instagram_followers,
        )
        advertisers.append(
            {
                "facebook_page_name": record.brand.name,
                "facebook_page_id": record.brand.source_id,
                "active_ad_count": record.active_ad_count,
                "instagram_username": (
                    social.instagram_handle if social else None
                ),
                "instagram_profile_url": (
                    str(social.instagram_profile_url)
                    if social and social.instagram_profile_url
                    else None
                ),
                "instagram_followers": followers,
                "passes_instagram_follower_filter": filter_result,
                "instagram_follower_filter_status": (
                    "unknown"
                    if filter_result is None
                    else "pass" if filter_result else "fail"
                ),
            }
        )
    return {
        "unique_advertisers": len(advertisers),
        "unique_ads": sum(len(record.ads) for record in records),
        "follower_filter": {
            "minimum": settings.target_min_instagram_followers,
            "maximum": settings.target_max_instagram_followers,
        },
        "advertisers": advertisers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Meta Supplement Tracker scan utilities")
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument(
        "--run-once",
        action="store_true",
        help="Run one full Meta, PostgreSQL, follower-filter, and Sheets pipeline",
    )
    commands.add_argument(
        "--meta-only",
        action="store_true",
        help="Run Meta discovery with only the outputs enabled by configuration",
    )
    commands.add_argument(
        "--check-db",
        action="store_true",
        help="Verify DATABASE_URL connectivity without running a scan",
    )
    commands.add_argument(
        "--check-sheets",
        action="store_true",
        help="Verify Google Sheets access, tab, headers, and Editor permission",
    )
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        if args.check_db:
            return _check_database(settings)
        if args.check_sheets:
            return _check_sheets(settings)
        if args.run_once:
            return asyncio.run(_run_once(settings))
        return asyncio.run(_run_meta_only(settings))
    except (
        ProviderError,
        DatabaseConfigurationError,
        DatabasePersistenceError,
    ) as exc:
        logger.error("Command failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
