"""Conservative monthly Meta spend estimation from observed provider data."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import Settings
from pydantic import JsonValue

from app.models import AdRecord, Region, SpendEstimate, SpendHistory


_NUMBER = re.compile(r"(?P<number>[\d,.]+)\s*(?P<suffix>[KMB])?", re.I)


@dataclass(frozen=True)
class _MetricRange:
    low: float
    high: float
    source: str
    active_days: int


class SpendEstimator:
    """Estimate ranges without converting assumptions into fake observations."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def estimate(
        self, record: AdRecord, history: SpendHistory | None = None
    ) -> SpendEstimate:
        if not self.settings.spend_estimation_enabled:
            return self._unknown("disabled")
        history = history or SpendHistory()
        cpm_low, cpm_high = self._cpm_range(record)
        region_values = [region.value for region in (record.regions or [record.region])]

        impressions = self._metrics(record, "impressions")
        if impressions:
            low, high = self._monthly_metric(impressions)
            return self._result(
                low * cpm_low / 1000,
                high * cpm_high / 1000,
                method="impressions_cpm",
                source="Impressions × CPM",
                confidence="medium",
                observed={
                    "regions": region_values,
                    "monthly_impressions_low": low,
                    "monthly_impressions_high": high,
                },
                assumptions={"cpm_low_usd": cpm_low, "cpm_high_usd": cpm_high},
            )

        reach = self._metrics(record, "reach")
        if reach:
            low, high = self._monthly_metric(reach)
            frequency_low = self.settings.spend_reach_frequency_low
            frequency_high = self.settings.spend_reach_frequency_high
            return self._result(
                low * frequency_low * cpm_low / 1000,
                high * frequency_high * cpm_high / 1000,
                method="reach_cpm",
                source="Reach × CPM",
                confidence="low",
                observed={
                    "regions": region_values,
                    "monthly_reach_low": low,
                    "monthly_reach_high": high,
                },
                assumptions={
                    "frequency_low": frequency_low,
                    "frequency_high": frequency_high,
                    "cpm_low_usd": cpm_low,
                    "cpm_high_usd": cpm_high,
                },
            )

        active_ads = record.active_ad_count if record.active_ad_count is not None else len(record.ads)
        active_days = [
            max(1, (_aware(record.observed_at) - _aware(ad.ad_delivery_start_time)).days + 1)
            for ad in record.ads
            if ad.ad_delivery_start_time is not None
        ]
        known_start_dates = len(active_days)
        longest_active_days = max(active_days, default=0)
        has_activity_evidence = (
            longest_active_days >= self.settings.spend_min_observation_days
            or history.observation_count >= 1
        )
        if active_ads > 0 and has_activity_evidence:
            return self._result(
                active_ads * self.settings.spend_activity_daily_low_usd * 30,
                active_ads * self.settings.spend_activity_daily_high_usd * 30,
                method="activity_model",
                source="Activity model - very rough",
                confidence="very_low",
                observed={
                    "regions": region_values,
                    "active_ad_count": active_ads,
                    "unique_creatives": len(record.ads),
                    "ads_with_start_dates": known_start_dates,
                    "longest_active_ad_days": longest_active_days,
                    "prior_observation_count": history.observation_count,
                    "prior_active_ad_counts": history.active_ad_counts,
                },
                assumptions={
                    "daily_spend_per_active_ad_low_usd": self.settings.spend_activity_daily_low_usd,
                    "daily_spend_per_active_ad_high_usd": self.settings.spend_activity_daily_high_usd,
                    "days_per_month": 30,
                },
                eligible_for_target=False,
            )
        return self._unknown("insufficient observed reach, impressions, longevity, or repeated activity")

    def _metrics(self, record: AdRecord, kind: str) -> list[_MetricRange]:
        metrics: list[_MetricRange] = []
        observed_at = _aware(record.observed_at)
        for ad in record.ads:
            start = ad.ad_delivery_start_time
            if start is None:
                continue
            active_days = max(1, (observed_at - _aware(start)).days + 1)
            if active_days < self.settings.spend_min_observation_days:
                continue
            if kind == "impressions":
                parsed = _parse_finite_range(ad.impressions)
                source = "impressions"
            else:
                value: JsonValue | None = ad.eu_total_reach
                if value is None:
                    value = ad.reach_estimate
                parsed = _parse_finite_range(value)
                source = "eu_total_reach" if ad.eu_total_reach is not None else "reach_estimate"
            if parsed is not None:
                metrics.append(_MetricRange(parsed[0], parsed[1], source, active_days))
        return metrics

    @staticmethod
    def _monthly_metric(metrics: list[_MetricRange]) -> tuple[float, float]:
        return (
            sum(item.low * 30 / item.active_days for item in metrics),
            sum(item.high * 30 / item.active_days for item in metrics),
        )

    def _cpm_range(self, record: AdRecord) -> tuple[float, float]:
        regions = record.regions or [record.region]
        configured = {
            Region.UK: (self.settings.spend_cpm_uk_low_usd, self.settings.spend_cpm_uk_high_usd),
            Region.EUROPE: (self.settings.spend_cpm_europe_low_usd, self.settings.spend_cpm_europe_high_usd),
            Region.USA: (self.settings.spend_cpm_usa_low_usd, self.settings.spend_cpm_usa_high_usd),
            Region.CANADA: (self.settings.spend_cpm_canada_low_usd, self.settings.spend_cpm_canada_high_usd),
        }
        values = [configured[region] for region in regions]
        return min(value[0] for value in values), max(value[1] for value in values)

    def _result(
        self,
        low: float,
        high: float,
        *,
        method: str,
        source: str,
        confidence: str,
        observed: dict[str, JsonValue],
        assumptions: dict[str, JsonValue],
        eligible_for_target: bool = True,
    ) -> SpendEstimate:
        rounded_low = max(0.0, math.floor(low / 100) * 100.0)
        rounded_high = max(rounded_low, math.ceil(high / 100) * 100.0)
        return SpendEstimate(
            low_usd=rounded_low,
            high_usd=rounded_high,
            method=method,
            source=source,
            confidence=confidence,
            observed_inputs=observed,
            assumptions=assumptions,
            target_match=(
                meaningful_overlap(
                    rounded_low,
                    rounded_high,
                    self.settings.spend_target_min_usd,
                    self.settings.spend_target_max_usd,
                )
                if eligible_for_target
                else None
            ),
        )

    @staticmethod
    def _unknown(reason: str) -> SpendEstimate:
        return SpendEstimate(
            method="unknown",
            source="Unknown",
            confidence="unknown",
            observed_inputs={"reason": reason},
            target_match=None,
        )


def meaningful_overlap(low: float, high: float, target_low: float, target_high: float) -> bool:
    """Require at least half of the estimated interval to lie in the target interval."""

    if high < low:
        raise ValueError("estimate high cannot be below estimate low")
    if high == low:
        return target_low <= low <= target_high
    intersection = max(0.0, min(high, target_high) - max(low, target_low))
    return intersection / (high - low) >= 0.5


def format_spend_range(estimate: SpendEstimate | None) -> str:
    if estimate is None or estimate.low_usd is None or estimate.high_usd is None:
        return "Unknown"
    return f"{_money(estimate.low_usd)}–{_money(estimate.high_usd)}/mo"


def _money(value: float) -> str:
    if value >= 1000:
        amount = value / 1000
        rendered = f"{amount:.1f}".rstrip("0").rstrip(".")
        return f"${rendered}k"
    return f"${value:,.0f}"


def _parse_finite_range(value: JsonValue | None) -> tuple[float, float] | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return (number, number) if number >= 0 else None
    if isinstance(value, dict):
        low = value.get("lower_bound")
        high = value.get("upper_bound")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)):
            return float(low), float(high)
        value = value.get("text")
    if not isinstance(value, str) or value.lstrip().startswith((">", "<")):
        return None
    matches = list(_NUMBER.finditer(value))
    if not matches:
        return None
    numbers = [_scaled(match) for match in matches[:2]]
    return (numbers[0], numbers[-1])


def _scaled(match: re.Match[str]) -> float:
    value = float(match.group("number").replace(",", ""))
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(
        (match.group("suffix") or "").upper(), 1
    )
    return value * multiplier


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
