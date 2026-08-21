"""Repository operations for scan runs, advertisers, ads, and observations."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Ad,
    Advertiser,
    AdvertiserObservation,
    GoogleSheetRow,
    ScanRun,
    utc_now,
)
from app.models import AdRecord, MetaAdDetails, RelevanceResult, SheetRowState


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

    def upsert_advertiser(self, record: AdRecord) -> Advertiser:
        page_id = record.brand.source_id
        advertiser = self.find_advertiser(record)

        social = record.social_stats
        followers = social.instagram_followers if social else None
        username = (
            social.instagram_handle if social and social.instagram_handle
            else record.brand.instagram_handle
        )
        if advertiser is None:
            advertiser = Advertiser(
                meta_page_id=page_id,
                page_name=record.brand.name,
                instagram_username=username,
                latest_instagram_followers=followers,
                first_seen_at=record.observed_at,
                last_seen_at=record.observed_at,
            )
            self.session.add(advertiser)
            self.session.flush()
            return advertiser

        advertiser.page_name = record.brand.name
        if username is not None:
            advertiser.instagram_username = username
        advertiser.latest_instagram_followers = followers
        advertiser.last_seen_at = record.observed_at
        return advertiser

    def find_advertiser(self, record: AdRecord) -> Advertiser | None:
        page_id = record.brand.source_id
        if page_id:
            return self.session.scalar(
                select(Advertiser).where(Advertiser.meta_page_id == page_id)
            )
        return self.session.scalar(
            select(Advertiser)
            .where(Advertiser.meta_page_id.is_(None))
            .where(Advertiser.page_name == record.brand.name)
            .limit(1)
        )

    def sheet_row_states(
        self, advertiser_ids: list[int]
    ) -> dict[int, SheetRowState]:
        if not advertiser_ids:
            return {}
        rows = self.session.scalars(
            select(GoogleSheetRow).where(
                GoogleSheetRow.advertiser_id.in_(advertiser_ids)
            )
        ).all()
        return {
            row.advertiser_id: SheetRowState(
                advertiser_id=row.advertiser_id,
                spreadsheet_id=row.spreadsheet_id,
                sheet_tab=row.sheet_tab,
                row_number=row.row_number,
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
                GoogleSheetRow.advertiser_id == state.advertiser_id
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
                first_seen_at=seen_at,
                last_seen_at=seen_at,
            )
            self.session.add(ad)
            return ad

        ad.advertiser_id = advertiser.id
        ad.ad_start_date = ad_details.ad_delivery_start_time
        ad.ad_text = ad_text
        ad.snapshot_url = ad_details.ad_snapshot_url
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
            observed_at=record.observed_at,
        )
        self.session.add(observation)
        return observation

    def _scan_run(self, scan_run_id: int) -> ScanRun:
        scan_run = self.session.get(ScanRun, scan_run_id)
        if scan_run is None:
            raise ValueError(f"Scan run {scan_run_id} does not exist")
        return scan_run
