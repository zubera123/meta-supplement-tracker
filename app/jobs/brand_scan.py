"""Brand scan orchestration and explicit one-run CLI commands."""

import argparse
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import Settings, get_settings
from app.db import (
    DatabaseConfigurationError,
    DatabasePersistenceError,
    ScanLockError,
    ScanPersistenceService,
)
from app.logging_config import configure_logging
from app.models import (
    AdRecord,
    Brand,
    BrandCandidate,
    RelevanceResult,
    ReviewCache,
    ReviewEnrichmentResult,
    ReviewStats,
    SocialStats,
)
from app.services import ProviderConfigurationError, ProviderError, TransientProviderError
from app.services.google_docs import BrandOutputProvider
from app.services.google_sheets import GoogleSheetsProvider, SheetSyncResult
from app.services.instagram import InstagramProvider
from app.services.meta_ads import (
    ApifyMetaAdsProvider,
    MetaAdLibraryProvider,
    MetaAdsProvider,
)
from app.services.reviews import (
    ApifyTrustpilotReviewsProvider,
    ReviewsProvider,
    TrustpilotReviewsProvider,
    resolve_advertiser_domain,
    unavailable_review,
)
from app.services.relevance import SupplementRelevanceFilter
from app.services.scoring import CandidateScorer
from app.services.scoring import instagram_follower_filter
from app.services.spend_estimation import SpendEstimator, format_spend_range


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
    relevance_results: Sequence[RelevanceResult]
    scan_run_id: int | None
    sheet_sync: SheetSyncResult | None


class ScanRuntimeExceededError(ProviderError):
    """Raised when one complete production scan exceeds its global deadline."""


class CandidatePipeline:
    """Run the real candidate pipeline once using configured provider services."""

    def __init__(
        self,
        *,
        settings: Settings,
        meta_ads: MetaAdsProvider,
        persistence: ScanPersistenceService | None,
        sheets: GoogleSheetsProvider | None,
        relevance_filter: SupplementRelevanceFilter | None = None,
        spend_estimator: SpendEstimator | None = None,
        reviews: ReviewsProvider | None = None,
    ) -> None:
        if sheets is not None and persistence is None:
            raise ProviderConfigurationError(
                "Google Sheets output requires PostgreSQL persistence"
            )
        self.settings = settings
        self.meta_ads = meta_ads
        self.persistence = persistence
        self.sheets = sheets
        self.relevance_filter = relevance_filter or SupplementRelevanceFilter()
        self.spend_estimator = spend_estimator or SpendEstimator(settings)
        self.reviews = reviews

    async def run(self) -> CandidatePipelineResult:
        """Execute once; paid Meta retrieval is intentionally invoked only once."""

        scan_run_id: int | None = None
        if self.persistence is not None:
            # Fail before any paid provider call when persistence is unavailable.
            self.persistence.verify_connection()
        await asyncio.sleep(0)

        try:
            if self.persistence is not None:
                scan_run_id = self.persistence.create_scan_run(self.settings.regions)
                logger.info(
                    "Candidate scan created status=running scan_run_id=%s regions=%s",
                    scan_run_id,
                    ",".join(self.settings.regions),
                )
            await asyncio.sleep(0)
            if self.sheets is not None:
                # Fail before any paid provider call; because the scan-run row now
                # exists, a failed output preflight is recorded durably.
                self.sheets.ensure_ready()
            # Deliver a pending whole-scan cancellation before a paid Actor starts.
            await asyncio.sleep(0)
            records = await self.meta_ads.retrieve_advertisers(
                regions=self.settings.regions,
                categories=self.settings.categories,
            )
            await asyncio.sleep(0)
            histories = (
                self.persistence.load_spend_histories(records)
                if self.persistence is not None
                else [None] * len(records)
            )
            estimated_records: list[AdRecord] = []
            for record, history in zip(records, histories, strict=True):
                estimated_records.append(
                    record.model_copy(
                        update={
                            "spend_estimate": self.spend_estimator.estimate(record, history)
                        }
                    )
                )
                await asyncio.sleep(0)
            records = estimated_records
            relevance_results = tuple(
                self.relevance_filter.evaluate(record) for record in records
            )
            if self.reviews is not None:
                if self.persistence is None:
                    raise ProviderConfigurationError(
                        "Review enrichment requires PostgreSQL persistence"
                    )
                review_caches = self.persistence.load_review_caches(records)
                records = await self._enrich_reviews(
                    records, review_caches, relevance_results
                )
            for record, relevance in zip(
                records, relevance_results, strict=True
            ):
                if not relevance.is_relevant:
                    logger.info(
                        "Advertiser excluded from candidate output: %s",
                        relevance.reason,
                        extra={"advertiser": record.brand.name},
                    )
            if self.persistence is not None and scan_run_id is not None:
                # Every advertiser is persisted before output filters are applied.
                self.persistence.persist_success(
                    scan_run_id,
                    records,
                    relevance_results,
                    minimum_followers=self.settings.target_min_instagram_followers,
                    maximum_followers=self.settings.target_max_instagram_followers,
                    disqualify_scans=self.settings.candidate_disqualify_scans,
                    absent_days=self.settings.candidate_absent_days,
                    scan_interval_hours=self.settings.scan_interval_hours,
                    coverage_complete=getattr(
                        self.meta_ads, "last_scan_coverage_complete", True
                    ),
                )
            await asyncio.sleep(0)

            sheet_sync: SheetSyncResult | None = None
            if self.sheets is not None and self.persistence is not None:
                managed_states = self.persistence.load_all_sheet_row_states(
                    self.settings.google_sheet_id or "",
                    self.settings.google_sheet_tab,
                )
                reconciliation = self.sheets.reconcile_managed_rows(managed_states)
                self.persistence.save_sheet_row_states(reconciliation.row_states)
                candidates, row_states = self.persistence.prepare_sheet_candidates(
                    records,
                    minimum_followers=self.settings.target_min_instagram_followers,
                    maximum_followers=self.settings.target_max_instagram_followers,
                    relevance_results=relevance_results,
                )
                removals = self.persistence.sheet_rows_to_remove()
                sheet_sync = self.sheets.sync_candidates(
                    candidates, row_states, removals
                )
                self.persistence.save_sheet_row_states(sheet_sync.row_states)
                self.persistence.delete_sheet_row_states(
                    sheet_sync.removed_company_ids
                )
            await asyncio.sleep(0)
            ads_found = sum(len(record.ads) for record in records)
            candidates_written = (
                sheet_sync.appended + sheet_sync.updated if sheet_sync else 0
            )
            logger.info(
                "Candidate scan completed status=succeeded scan_run_id=%s "
                "ads_found=%s advertisers_found=%s candidates_written=%s",
                scan_run_id,
                ads_found,
                len(records),
                candidates_written,
            )
            return CandidatePipelineResult(
                records, relevance_results, scan_run_id, sheet_sync
            )
        except asyncio.CancelledError:
            timeout_error = ScanRuntimeExceededError(
                "Candidate scan exceeded "
                f"SCAN_MAX_RUNTIME_SECONDS={self.settings.scan_max_runtime_seconds:g}"
            )
            logger.error(
                "Candidate scan completed status=failed scan_run_id=%s "
                "failure_reason=%s",
                scan_run_id,
                timeout_error,
            )
            if self.persistence is not None and scan_run_id is not None:
                self.persistence.record_failure(scan_run_id, timeout_error)
            raise
        except Exception as exc:
            logger.exception(
                "Candidate scan completed status=failed scan_run_id=%s "
                "failure_reason=%s",
                scan_run_id,
                exc,
            )
            if self.persistence is not None and scan_run_id is not None:
                self.persistence.record_failure(scan_run_id, exc)
            raise

    async def _enrich_reviews(
        self,
        records: Sequence[AdRecord],
        caches: Sequence[ReviewCache],
        relevance_results: Sequence[RelevanceResult] | None = None,
    ) -> list[AdRecord]:
        """Enrich sequentially and convert provider outages into stored soft failures."""

        enriched: list[AdRecord] = []
        refresh_cutoff = datetime.now(UTC) - timedelta(
            hours=self.settings.trustpilot_refresh_hours
        )
        if relevance_results is not None and len(relevance_results) != len(records):
            raise ValueError("Review eligibility decisions do not match records")
        for index, (record, cache) in enumerate(zip(records, caches, strict=True)):
            followers = (
                record.social_stats.instagram_followers
                if record.social_stats is not None
                else None
            )
            review_eligible = (
                relevance_results is None
                or relevance_results[index].has_positive_evidence
            ) and (
                followers is not None
                and self.settings.target_min_instagram_followers
                <= followers
                <= self.settings.target_max_instagram_followers
            )
            if relevance_results is not None and not review_eligible:
                result = ReviewEnrichmentResult(
                    status="skipped",
                    reason=(
                        "Review lookup skipped because advertiser is not currently "
                        "eligible for candidate output"
                    ),
                )
                enriched.append(
                    record.model_copy(update={"review_enrichment": result})
                )
                continue
            domain, resolution_reason = resolve_advertiser_domain(
                record,
                include_brand_website=not isinstance(
                    self.reviews, ApifyTrustpilotReviewsProvider
                ),
            )
            if domain is None:
                result = unavailable_review(resolution_reason)
            elif _review_cache_is_fresh(cache, domain, refresh_cutoff):
                cached_stats = cache.latest_stats
                if cached_stats is not None:
                    cached_stats = cached_stats.model_copy(
                        update={
                            "desirable": cached_stats.review_count
                            >= self.settings.trustpilot_min_desirable_reviews
                        }
                    )
                result = ReviewEnrichmentResult(
                    status="cached",
                    stats=cached_stats,
                    reason="Trustpilot cache is inside the configured refresh interval",
                    attempted_domain=domain,
                    refreshed_at=cache.last_refreshed_at,
                )
            else:
                cached_id = (
                    cache.business_unit_id
                    if cache.matched_domain == domain
                    else None
                )
                try:
                    assert self.reviews is not None
                    result = await self.reviews.get_by_domain(
                        domain, business_unit_id=cached_id
                    )
                except ProviderError as exc:
                    logger.warning(
                        "Trustpilot review enrichment failed softly for advertiser=%s: %s",
                        record.brand.name,
                        exc,
                    )
                    result = ReviewEnrichmentResult(
                        status="error",
                        reason=f"Trustpilot enrichment failed: {type(exc).__name__}",
                        attempted_domain=domain,
                    )
            enriched.append(
                record.model_copy(update={"review_enrichment": result})
            )
        return enriched


async def _run_scan_command(settings: Settings, *, require_full_outputs: bool) -> int:
    persistence: ScanPersistenceService | None = None
    scan_lock = None
    reviews: ReviewsProvider | None = None
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
        if settings.reviews_enabled and not settings.persist_scan_results:
            raise ProviderConfigurationError(
                "PERSIST_SCAN_RESULTS=true is required for cached review enrichment"
            )
        if settings.persist_scan_results:
            persistence = ScanPersistenceService.from_database_url(
                settings.database_url,
                connect_timeout_seconds=settings.database_connect_timeout_seconds,
            )
        if require_full_outputs:
            if persistence is None:
                raise ProviderConfigurationError(
                    "PostgreSQL persistence is required for scan overlap protection"
                )
            started_at = datetime.now(UTC).isoformat()
            logger.info(
                "Candidate scan invocation started scheduled_start_time_utc=%s",
                started_at,
            )
            scan_lock = persistence.try_acquire_scan_lock()
            if scan_lock is None:
                logger.warning(
                    "Candidate scan skipped status=overlap "
                    "scheduled_start_time_utc=%s",
                    started_at,
                )
                print(
                    json.dumps(
                        {
                            "status": "skipped",
                            "reason": "another candidate scan is already running",
                        }
                    )
                )
                return 0
        sheets = (
            _build_sheets_provider(settings)
            if settings.google_sheets_enabled
            else None
        )
        provider = _build_meta_provider(settings)
        reviews = _build_reviews_provider(settings) if settings.reviews_enabled else None
        result = await CandidatePipeline(
            settings=settings,
            meta_ads=provider,
            persistence=persistence,
            sheets=sheets,
            relevance_filter=SupplementRelevanceFilter(
                include_keywords=settings.relevance_include_keywords,
                exclude_keywords=settings.relevance_exclude_keywords,
            ),
            reviews=reviews,
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
        output["relevance_filter"] = {
            "relevant": sum(
                decision.is_relevant for decision in result.relevance_results
            ),
            "excluded": sum(
                not decision.is_relevant for decision in result.relevance_results
            ),
        }
        print(json.dumps(output, indent=2))
        return 0
    finally:
        try:
            if scan_lock is not None:
                scan_lock.release()
                logger.info("Released PostgreSQL candidate scan lock")
        finally:
            if persistence is not None:
                persistence.close()
            if reviews is not None:
                await reviews.close()


async def _run_meta_only(settings: Settings) -> int:
    """Run discovery with persistence and Sheets when their flags are enabled."""

    return await _run_scan_command(settings, require_full_outputs=False)


async def _run_once(settings: Settings) -> int:
    """Run the complete production candidate pipeline exactly once."""

    try:
        async with asyncio.timeout(settings.scan_max_runtime_seconds):
            return await _run_scan_command(settings, require_full_outputs=True)
    except TimeoutError as exc:
        raise ScanRuntimeExceededError(
            "Candidate scan exceeded "
            f"SCAN_MAX_RUNTIME_SECONDS={settings.scan_max_runtime_seconds:g}"
        ) from exc


def _build_meta_provider(settings: Settings) -> MetaAdsProvider:
    configured_provider = (settings.meta_ad_provider or "meta_ad_library").casefold()
    if configured_provider == "apify":
        return ApifyMetaAdsProvider(
            api_token=settings.apify_api_token,
            actor_id=settings.apify_actor_id,
            actor_build=settings.apify_meta_actor_build,
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


def _build_reviews_provider(settings: Settings) -> ReviewsProvider:
    configured_provider = (
        settings.reviews_provider or "apify_trustpilot"
    ).casefold()
    if configured_provider == "apify_trustpilot":
        return ApifyTrustpilotReviewsProvider(
            api_token=settings.apify_api_token,
            actor_id=settings.apify_trustpilot_actor_id,
            max_total_charge_usd_per_run=(
                settings.apify_trustpilot_max_total_charge_usd_per_run
            ),
            minimum_desirable_reviews=settings.trustpilot_min_desirable_reviews,
            monthly_budget_gbp=settings.apify_monthly_budget_gbp,
            budget_gbp_per_usd=settings.apify_budget_gbp_per_usd,
            request_timeout_seconds=settings.apify_request_timeout_seconds,
            retry_attempts=settings.provider_retry_attempts,
            retry_min_wait_seconds=settings.provider_retry_min_wait_seconds,
            retry_max_wait_seconds=settings.provider_retry_max_wait_seconds,
        )
    if configured_provider == "trustpilot":
        return TrustpilotReviewsProvider(
            api_key=settings.trustpilot_api_key,
            minimum_desirable_reviews=settings.trustpilot_min_desirable_reviews,
            request_timeout_seconds=settings.trustpilot_request_timeout_seconds,
            retry_attempts=settings.provider_retry_attempts,
            retry_min_wait_seconds=settings.provider_retry_min_wait_seconds,
            retry_max_wait_seconds=settings.provider_retry_max_wait_seconds,
            min_request_interval_seconds=(
                settings.trustpilot_min_request_interval_seconds
            ),
        )
    raise ProviderConfigurationError(
        "REVIEWS_PROVIDER must be trustpilot or apify_trustpilot when reviews are enabled"
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


def _reconcile_sheet(settings: Settings) -> int:
    """Reconcile every managed row and native table without invoking Apify."""

    if not settings.persist_scan_results:
        raise ProviderConfigurationError(
            "PERSIST_SCAN_RESULTS=true is required for Sheet reconciliation"
        )
    if not settings.google_sheets_enabled:
        raise ProviderConfigurationError(
            "GOOGLE_SHEETS_ENABLED=true is required for Sheet reconciliation"
        )
    persistence = ScanPersistenceService.from_database_url(
        settings.database_url,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    scan_lock = None
    try:
        persistence.verify_connection()
        scan_lock = persistence.try_acquire_scan_lock()
        if scan_lock is None:
            raise ScanLockError(
                "Another candidate scan holds the PostgreSQL advisory lock"
            )
        provider = _build_sheets_provider(settings)
        provider.ensure_ready()
        states = persistence.load_all_sheet_row_states(
            settings.google_sheet_id or "", settings.google_sheet_tab
        )
        result = provider.reconcile_managed_rows(states)
        persistence.save_sheet_row_states(result.row_states)
    finally:
        if scan_lock is not None:
            scan_lock.release()
        persistence.close()
    print(
        json.dumps(
            {
                "spreadsheet": "reachable",
                "tab": settings.google_sheet_tab,
                "managed_rows": len(result.row_states),
                "metadata_attached": result.metadata_attached,
                "metadata_existing": result.metadata_existing,
                "native_table_id": result.table_id,
                "native_table_created": result.table_created,
                "native_table_resized": result.table_resized,
                "visible_values_changed": False,
                "meta_provider_called": False,
                "actor_started": False,
            },
            indent=2,
        )
    )
    return 0


async def _check_reviews(settings: Settings) -> int:
    provider = _build_reviews_provider(settings)
    try:
        await provider.check_connection()
    finally:
        await provider.close()
    is_apify = isinstance(provider, ApifyTrustpilotReviewsProvider)
    print(
        json.dumps(
            {
                "provider": (
                    "Trustpilot via Apify" if is_apify else "Trustpilot"
                ),
                "api": "reachable",
                "authentication": "accepted",
                "actor_available": True if is_apify else None,
                "actor_started": False,
                "meta_provider_called": False,
            }
        )
    )
    return 0


def _estimate_spend_dry_run(settings: Settings) -> int:
    """Estimate from PostgreSQL only; never construct or invoke a Meta provider."""

    persistence = ScanPersistenceService.from_database_url(
        settings.database_url,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    try:
        persistence.verify_connection()
        records, histories = persistence.load_spend_dry_run_records()
        estimator = SpendEstimator(settings)
        estimates = []
        for record, history in zip(records, histories, strict=True):
            estimate = estimator.estimate(record, history)
            estimates.append(
                {
                    "advertiser": record.brand.name,
                    "estimate": format_spend_range(estimate),
                    "source": estimate.source,
                    "confidence": estimate.confidence,
                    "matches_target": estimate.target_match,
                    "observed_inputs": estimate.observed_inputs,
                }
            )
    finally:
        persistence.close()
    print(json.dumps({"provider_called": False, "advertisers": estimates}, indent=2))
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
                "spend_estimate": format_spend_range(record.spend_estimate),
                "spend_source": (
                    record.spend_estimate.source if record.spend_estimate else "Unknown"
                ),
                "spend_confidence": (
                    record.spend_estimate.confidence if record.spend_estimate else "unknown"
                ),
                "matches_spend_target": (
                    record.spend_estimate.target_match if record.spend_estimate else None
                ),
                "reviews": (
                    record.review_enrichment.stats.review_count
                    if record.review_enrichment and record.review_enrichment.stats
                    else None
                ),
                "review_source": (
                    record.review_enrichment.stats.source
                    if record.review_enrichment and record.review_enrichment.stats
                    else None
                ),
                "review_status": (
                    record.review_enrichment.status
                    if record.review_enrichment
                    else "disabled"
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


def _review_cache_is_fresh(
    cache: ReviewCache, domain: str, refresh_cutoff: datetime
) -> bool:
    if cache.matched_domain != domain or cache.last_refreshed_at is None:
        return False
    refreshed_at = cache.last_refreshed_at
    if refreshed_at.tzinfo is None:
        refreshed_at = refreshed_at.replace(tzinfo=UTC)
    return refreshed_at >= refresh_cutoff


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
    commands.add_argument(
        "--reconcile-sheet",
        action="store_true",
        help=(
            "Reconcile all PostgreSQL Sheet mappings and the native table without Apify"
        ),
    )
    commands.add_argument(
        "--estimate-spend-dry-run",
        action="store_true",
        help="Estimate spend from existing PostgreSQL data without calling Apify",
    )
    commands.add_argument(
        "--check-reviews",
        action="store_true",
        help="Verify review provider configuration without starting a paid Actor or Meta",
    )
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        if args.check_db:
            return _check_database(settings)
        if args.check_sheets:
            return _check_sheets(settings)
        if args.reconcile_sheet:
            return _reconcile_sheet(settings)
        if args.estimate_spend_dry_run:
            return _estimate_spend_dry_run(settings)
        if args.check_reviews:
            return asyncio.run(_check_reviews(settings))
        if args.run_once:
            return asyncio.run(_run_once(settings))
        return asyncio.run(_run_meta_only(settings))
    except (
        ProviderError,
        DatabaseConfigurationError,
        DatabasePersistenceError,
        ScanLockError,
    ) as exc:
        logger.error("Command failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
