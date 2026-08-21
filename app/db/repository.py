"""Repository operations for scan runs, advertisers, ads, and observations."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Ad,
    Advertiser,
    AdvertiserObservation,
    GoogleSheetRow,
    ScanRun,
    Company,
    AdvertiserCompanyMapping,
    utc_now,
)
from app.services.company_identity import resolve_verified_company_domain
from app.models import (
    AdRecord,
    MetaAdDetails,
    RelevanceResult,
    ReviewCache,
    ReviewStats,
    SheetRowState,
)


class ScanRepository:
    """Perform persistence operations within a caller-owned transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create_scan_run(
        self, regions: list[str], *, started_at: datetime | None = None
    ) -> ScanRun:
        scan_run = ScanRun(
            started_at=started_at or utc_now(),
            status="running",
            regions=regions,
            ads_found=0,
            advertisers_found=0,
        )
        self.session.add(scan_run)
        self.session.flush()
        return scan_run

    def complete_scan_run(
        self,
        scan_run_id: int,
        *,
        ads_found: int,
        advertisers_found: int,
        finished_at: datetime | None = None,
    ) -> ScanRun:
        scan_run = self._scan_run(scan_run_id)
        scan_run.status = "succeeded"
        scan_run.finished_at = finished_at or utc_now()
        scan_run.ads_found = ads_found
        scan_run.advertisers_found = advertisers_found
        scan_run.error_message = None
        return scan_run

    def fail_scan_run(
        self,
        scan_run_id: int,
        error_message: str,
        *,
        finished_at: datetime | None = None,
    ) -> ScanRun:
        scan_run = self._scan_run(scan_run_id)
        scan_run.status = "failed"
        scan_run.finished_at = finished_at or utc_now()
        scan_run.error_message = error_message[:4000]
        return scan_run

    def upsert_advertiser(
        self, record: AdRecord, *, scan_run_id: int | None = None
    ) -> Advertiser:
        page_id = record.brand.source_id
        advertiser = self.find_advertiser(record)

        social = record.social_stats
        followers = social.instagram_followers if social else None
        username = (
            social.instagram_handle if social and social.instagram_handle
            else record.brand.instagram_handle
        )
        resolution = resolve_verified_company_domain(record)
        if advertiser is None:
            company = None
            if resolution.domain:
                company = self.session.scalar(
                    select(Company).where(Company.canonical_domain == resolution.domain)
                )
            if company is None:
                company = Company(
                    canonical_domain=resolution.domain,
                    display_name=record.brand.name,
                    regions=[r.value for r in (record.regions or [record.region])],
                    first_seen_at=record.observed_at,
                    last_seen_at=record.observed_at,
                )
                self.session.add(company)
                self.session.flush()
            advertiser = Advertiser(
                company_id=company.id,
                verified_landing_domain=resolution.domain,
                company_mapping_reason=resolution.reason,
                meta_page_id=page_id,
                page_name=record.brand.name,
                instagram_username=username,
                latest_instagram_followers=followers,
                first_seen_at=record.observed_at,
                last_seen_at=record.observed_at,
            )
            self.session.add(advertiser)
            self.session.flush()
            self.session.add(AdvertiserCompanyMapping(
                advertiser_id=advertiser.id, company_id=company.id,
                scan_run_id=scan_run_id, verified_domain=resolution.domain,
                reason=resolution.reason, started_at=record.observed_at,
            ))
            self._update_review_cache(advertiser, record)
            return advertiser

        if resolution.domain:
            target = self.session.scalar(
                select(Company).where(Company.canonical_domain == resolution.domain)
            )
            current = self.session.get(Company, advertiser.company_id)
            if (
                current is not None
                and current.canonical_domain is not None
                and current.canonical_domain != resolution.domain
            ):
                advertiser.company_mapping_reason = (
                    "verified destination changed; existing Page identity preserved"
                )
                target = current
            if target is None and current is not None and current.canonical_domain is None:
                current.canonical_domain = resolution.domain
                target = current
            if target is not None and target.id != advertiser.company_id:
                prior = self.session.scalar(
                    select(AdvertiserCompanyMapping)
                    .where(AdvertiserCompanyMapping.advertiser_id == advertiser.id)
                    .where(AdvertiserCompanyMapping.ended_at.is_(None))
                )
                if prior is not None:
                    prior.ended_at = record.observed_at
                current_count = self.session.scalar(
                    select(func.count()).select_from(Advertiser).where(
                        Advertiser.company_id == advertiser.company_id
                    )
                )
                if current is not None and current_count == 1:
                    current.merged_into_company_id = target.id
                advertiser.company_id = target.id
                self.session.add(AdvertiserCompanyMapping(
                    advertiser_id=advertiser.id, company_id=target.id,
                    scan_run_id=scan_run_id, verified_domain=resolution.domain,
                    reason=resolution.reason, started_at=record.observed_at,
                ))
            if target is not current or current is None or current.canonical_domain == resolution.domain:
                advertiser.verified_landing_domain = resolution.domain
                advertiser.company_mapping_reason = resolution.reason

        advertiser.page_name = record.brand.name
        if username is not None:
            advertiser.instagram_username = username
        advertiser.latest_instagram_followers = followers
        advertiser.last_seen_at = record.observed_at
        self._update_review_cache(advertiser, record)
        return advertiser

    def review_cache(self, record: AdRecord) -> ReviewCache:
        advertiser = self.find_advertiser(record)
        if advertiser is None:
            return ReviewCache()
        latest_stats = None
        if (
            advertiser.trustpilot_business_unit_id
            and advertiser.trustpilot_matched_domain
            and advertiser.latest_trustpilot_review_count is not None
        ):
            trust_score = (
                float(advertiser.latest_trustpilot_trust_score)
                if advertiser.latest_trustpilot_trust_score is not None
                else None
            )
            latest_stats = ReviewStats(
                source=advertiser.latest_trustpilot_review_source or "Trustpilot",
                review_count=advertiser.latest_trustpilot_review_count,
                rating=trust_score,
                trust_score=trust_score,
                star_score=(
                    float(advertiser.latest_trustpilot_stars)
                    if advertiser.latest_trustpilot_stars is not None
                    else None
                ),
                business_unit_id=advertiser.trustpilot_business_unit_id,
                matched_domain=advertiser.trustpilot_matched_domain,
                observed_at=(
                    advertiser.trustpilot_last_refreshed_at or advertiser.last_seen_at
                ),
            )
        return ReviewCache(
            business_unit_id=advertiser.trustpilot_business_unit_id,
            matched_domain=advertiser.trustpilot_matched_domain,
            last_refreshed_at=advertiser.trustpilot_last_refreshed_at,
            latest_stats=latest_stats,
        )

    def find_advertiser(self, record: AdRecord) -> Advertiser | None:
        page_id = record.brand.source_id
        if page_id:
            return self.session.scalar(
                select(Advertiser).where(Advertiser.meta_page_id == page_id)
            )
        domain = resolve_verified_company_domain(record).domain
        if domain:
            return self.session.scalar(
                select(Advertiser)
                .where(Advertiser.meta_page_id.is_(None))
                .where(Advertiser.verified_landing_domain == domain)
                .limit(1)
            )
        return None

    def sheet_row_states(
        self, company_ids: list[int]
    ) -> dict[int, SheetRowState]:
        if not company_ids:
            return {}
        rows = self.session.scalars(
            select(GoogleSheetRow).where(
                GoogleSheetRow.company_id.in_(company_ids)
            )
        ).all()
        return {
            row.company_id: SheetRowState(
                company_id=row.company_id,
                spreadsheet_id=row.spreadsheet_id,
                sheet_tab=row.sheet_tab,
                row_number=row.row_number,
                developer_metadata_id=row.developer_metadata_id,
                last_exported_first_seen=row.last_exported_first_seen,
                last_exported_brand=row.last_exported_brand,
                last_exported_region=row.last_exported_region,
                last_exported_instagram=row.last_exported_instagram,
            )
            for row in rows
        }

    def upsert_sheet_row_state(self, state: SheetRowState) -> GoogleSheetRow:
        row = self.session.scalar(
            select(GoogleSheetRow).where(
                GoogleSheetRow.company_id == state.company_id
            )
        )
        now = utc_now()
        if row is None:
            row = GoogleSheetRow(
                **state.model_dump(),
                created_at=now,
                updated_at=now,
            )
            self.session.add(row)
            return row
        row.spreadsheet_id = state.spreadsheet_id
        row.sheet_tab = state.sheet_tab
        row.row_number = state.row_number
        row.developer_metadata_id = state.developer_metadata_id
        row.last_exported_first_seen = state.last_exported_first_seen
        row.last_exported_brand = state.last_exported_brand
        row.last_exported_region = state.last_exported_region
        row.last_exported_instagram = state.last_exported_instagram
        row.updated_at = now
        return row

    def upsert_ad(
        self, advertiser: Advertiser, ad_details: MetaAdDetails, *, seen_at: datetime
    ) -> Ad:
        ad = self.session.scalar(
            select(Ad).where(Ad.meta_ad_id == ad_details.ad_id)
        )
        ad_text = next(
            (body for body in ad_details.creative_bodies if body.strip()), None
        )
        if ad is None:
            ad = Ad(
                meta_ad_id=ad_details.ad_id,
                advertiser_id=advertiser.id,
                ad_start_date=ad_details.ad_delivery_start_time,
                ad_text=ad_text,
                snapshot_url=ad_details.ad_snapshot_url,
                landing_page_url=ad_details.landing_page_url,
                landing_page_domain=ad_details.landing_page_domain,
                first_seen_at=seen_at,
                last_seen_at=seen_at,
            )
            self.session.add(ad)
            return ad

        ad.advertiser_id = advertiser.id
        ad.ad_start_date = ad_details.ad_delivery_start_time
        ad.ad_text = ad_text
        ad.snapshot_url = ad_details.ad_snapshot_url
        ad.landing_page_url = ad_details.landing_page_url
        ad.landing_page_domain = ad_details.landing_page_domain
        ad.last_seen_at = seen_at
        return ad

    def write_advertiser_observation(
        self,
        advertiser: Advertiser,
        scan_run_id: int,
        record: AdRecord,
        relevance: RelevanceResult | None = None,
    ) -> AdvertiserObservation:
        followers = (
            record.social_stats.instagram_followers
            if record.social_stats is not None
            else None
        )
        observation = AdvertiserObservation(
            advertiser_id=advertiser.id,
            scan_run_id=scan_run_id,
            instagram_followers=followers,
            active_ad_count=record.active_ad_count or 0,
            supplement_relevant=(
                relevance.is_relevant if relevance is not None else None
            ),
            relevance_reason=relevance.reason if relevance is not None else None,
            spend_estimate_low_usd=(
                record.spend_estimate.low_usd if record.spend_estimate else None
            ),
            spend_estimate_high_usd=(
                record.spend_estimate.high_usd if record.spend_estimate else None
            ),
            spend_estimation_method=(
                record.spend_estimate.method if record.spend_estimate else None
            ),
            spend_estimation_source=(
                record.spend_estimate.source if record.spend_estimate else None
            ),
            spend_estimation_confidence=(
                record.spend_estimate.confidence if record.spend_estimate else None
            ),
            spend_estimation_inputs=(
                record.spend_estimate.observed_inputs if record.spend_estimate else None
            ),
            spend_estimation_assumptions=(
                record.spend_estimate.assumptions if record.spend_estimate else None
            ),
            spend_target_match=(
                record.spend_estimate.target_match if record.spend_estimate else None
            ),
            review_source=(
                record.review_enrichment.stats.source
                if record.review_enrichment and record.review_enrichment.stats
                else None
            ),
            review_count=(
                record.review_enrichment.stats.review_count
                if record.review_enrichment and record.review_enrichment.stats
                else None
            ),
            review_trust_score=(
                record.review_enrichment.stats.trust_score
                if record.review_enrichment and record.review_enrichment.stats
                else None
            ),
            review_stars=(
                record.review_enrichment.stats.star_score
                if record.review_enrichment and record.review_enrichment.stats
                else None
            ),
            review_business_unit_id=(
                record.review_enrichment.stats.business_unit_id
                if record.review_enrichment and record.review_enrichment.stats
                else None
            ),
            review_matched_domain=(
                record.review_enrichment.stats.matched_domain
                if record.review_enrichment and record.review_enrichment.stats
                else record.review_enrichment.attempted_domain
                if record.review_enrichment
                else None
            ),
            review_desirable=(
                record.review_enrichment.stats.desirable
                if record.review_enrichment and record.review_enrichment.stats
                else None
            ),
            review_status=(
                record.review_enrichment.status if record.review_enrichment else None
            ),
            review_reason=(
                record.review_enrichment.reason if record.review_enrichment else None
            ),
            observed_at=record.observed_at,
        )
        self.session.add(observation)
        return observation

    @staticmethod
    def _update_review_cache(advertiser: Advertiser, record: AdRecord) -> None:
        result = record.review_enrichment
        if result is None or result.status == "error":
            return
        stats = result.stats
        if stats is not None:
            advertiser.trustpilot_business_unit_id = stats.business_unit_id
            advertiser.trustpilot_matched_domain = stats.matched_domain
            advertiser.trustpilot_last_refreshed_at = result.refreshed_at
            advertiser.latest_trustpilot_review_count = stats.review_count
            advertiser.latest_trustpilot_review_source = stats.source
            advertiser.latest_trustpilot_trust_score = stats.trust_score
            advertiser.latest_trustpilot_stars = stats.star_score
            return
        if result.refreshed_at is not None and result.attempted_domain is not None:
            if advertiser.trustpilot_matched_domain != result.attempted_domain:
                advertiser.trustpilot_business_unit_id = None
            advertiser.trustpilot_matched_domain = result.attempted_domain
            advertiser.trustpilot_last_refreshed_at = result.refreshed_at

    def _scan_run(self, scan_run_id: int) -> ScanRun:
        scan_run = self.session.get(ScanRun, scan_run_id)
        if scan_run is None:
            raise ValueError(f"Scan run {scan_run_id} does not exist")
        return scan_run
