"""SRE error budget and multi-window burn-rate alerting calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

from metrics import (
    EndpointMetrics,
    LogRecord,
    compute_endpoint_metrics,
    group_records_by_endpoint,
    is_error_status,
    time_span_minutes,
)


ALERT_CRITICAL = "CRITICAL"
ALERT_HIGH = "HIGH"
ALERT_WARNING = "WARNING"
ALERT_OK = "OK"

# Standard Google SRE multi-window burn-rate thresholds (for 30-day SLO window)
# Critical (page): 1h >= 14.4x AND 5m >= 14.4x  (2% budget in 1h)
# High (page):     6h >= 6.0x  AND 30m >= 6.0x  (5% budget in 6h)
# Warning (ticket):24h >= 3.0x AND 2h >= 3.0x   (10% budget in 24h)
#               OR 72h >= 1.0x AND 6h >= 1.0x
WINDOW_SPECS_MINUTES: Dict[str, int] = {
    "5m": 5,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "6h": 360,
    "24h": 1440,
    "72h": 4320,
}


@dataclass(frozen=True)
class BurnRateReport:
    """Complete SLO, error budget, and burn-rate report for an endpoint or overall traffic."""

    metrics: EndpointMetrics
    slo_target_pct: float
    window_days: float
    observed_span_minutes: float
    total_budget_minutes: float
    consumed_budget_minutes: float
    remaining_budget_minutes: float
    budget_consumed_pct: float
    overall_burn_rate: float
    time_to_exhaustion_hours: Optional[float]
    window_burn_rates: Dict[str, float]
    alert_tier: str
    alert_reason: str


def compute_burn_rate(error_rate: float, error_budget_ratio: float) -> float:
    """Compute burn rate = error_rate / error_budget_ratio."""
    if error_rate <= 0.0:
        return 0.0
    if error_budget_ratio <= 0.0:
        return float("inf")
    return error_rate / error_budget_ratio


def _window_burn_rate(
    records: Sequence[LogRecord],
    anchor_ts: datetime,
    window_minutes: int,
    error_budget_ratio: float,
    include_4xx: bool,
) -> float:
    cutoff = anchor_ts - timedelta(minutes=window_minutes)
    in_window = [r for r in records if r.timestamp >= cutoff]
    if not in_window:
        return 0.0
    errors = sum(1 for r in in_window if is_error_status(r.status_code, include_4xx=include_4xx))
    err_rate = errors / len(in_window)
    return compute_burn_rate(err_rate, error_budget_ratio)


def evaluate_alert_tier(window_burn_rates: Dict[str, float]) -> tuple[str, str]:
    """Evaluate Google SRE multi-window burn-rate alert tier."""
    br_5m = window_burn_rates.get("5m", 0.0)
    br_30m = window_burn_rates.get("30m", 0.0)
    br_1h = window_burn_rates.get("1h", 0.0)
    br_2h = window_burn_rates.get("2h", 0.0)
    br_6h = window_burn_rates.get("6h", 0.0)
    br_24h = window_burn_rates.get("24h", 0.0)
    br_72h = window_burn_rates.get("72h", 0.0)

    if br_1h >= 14.4 and br_5m >= 14.4:
        return (
            ALERT_CRITICAL,
            f"Fast burn (page): 1h={br_1h:.2f}x & 5m={br_5m:.2f}x >= 14.4x",
        )
    if br_6h >= 6.0 and br_30m >= 6.0:
        return (
            ALERT_HIGH,
            f"Fast burn (page): 6h={br_6h:.2f}x & 30m={br_30m:.2f}x >= 6.0x",
        )
    if br_24h >= 3.0 and br_2h >= 3.0:
        return (
            ALERT_WARNING,
            f"Slow burn (ticket): 24h={br_24h:.2f}x & 2h={br_2h:.2f}x >= 3.0x",
        )
    if br_72h >= 1.0 and br_6h >= 1.0:
        return (
            ALERT_WARNING,
            f"Slow burn (ticket): 72h={br_72h:.2f}x & 6h={br_6h:.2f}x >= 1.0x",
        )
    return (ALERT_OK, "Within error budget")


def analyze_slice(
    endpoint: str,
    records: Sequence[LogRecord],
    slo_target_pct: float = 99.9,
    window_days: float = 30.0,
    include_4xx: bool = False,
    anchor_ts: Optional[datetime] = None,
) -> BurnRateReport:
    """Compute full SLO, error budget, and multi-window burn-rate report for a record slice."""
    if not (0.0 < slo_target_pct <= 100.0):
        raise ValueError(f"SLO target must be in (0, 100], got {slo_target_pct}")
    if window_days <= 0.0:
        raise ValueError(f"Window days must be > 0, got {window_days}")

    ep_metrics = compute_endpoint_metrics(endpoint, records, include_4xx=include_4xx)
    error_budget_ratio = max(0.0, 1.0 - (slo_target_pct / 100.0))
    total_budget_minutes = window_days * 1440.0 * error_budget_ratio

    span_minutes = time_span_minutes(records)
    err_rate = ep_metrics.error_rate_pct / 100.0
    overall_br = compute_burn_rate(err_rate, error_budget_ratio)

    consumed_minutes = err_rate * span_minutes
    remaining_minutes = max(0.0, total_budget_minutes - consumed_minutes)
    if total_budget_minutes > 0.0:
        budget_consumed_pct = (consumed_minutes / total_budget_minutes) * 100.0
    else:
        budget_consumed_pct = 100.0 if ep_metrics.error_requests > 0 else 0.0

    if err_rate > 0.0:
        tte_hours: Optional[float] = (remaining_minutes / err_rate) / 60.0
    else:
        tte_hours = None

    effective_anchor = anchor_ts or (max(r.timestamp for r in records) if records else None)
    window_burn_rates: Dict[str, float] = {}
    if effective_anchor is not None and records:
        for label, mins in WINDOW_SPECS_MINUTES.items():
            window_burn_rates[label] = _window_burn_rate(
                records=records,
                anchor_ts=effective_anchor,
                window_minutes=mins,
                error_budget_ratio=error_budget_ratio,
                include_4xx=include_4xx,
            )
    else:
        for label in WINDOW_SPECS_MINUTES:
            window_burn_rates[label] = 0.0

    alert_tier, alert_reason = evaluate_alert_tier(window_burn_rates)

    return BurnRateReport(
        metrics=ep_metrics,
        slo_target_pct=slo_target_pct,
        window_days=window_days,
        observed_span_minutes=span_minutes,
        total_budget_minutes=total_budget_minutes,
        consumed_budget_minutes=consumed_minutes,
        remaining_budget_minutes=remaining_minutes,
        budget_consumed_pct=budget_consumed_pct,
        overall_burn_rate=overall_br,
        time_to_exhaustion_hours=tte_hours,
        window_burn_rates=window_burn_rates,
        alert_tier=alert_tier,
        alert_reason=alert_reason,
    )


def analyze_logs(
    records: Sequence[LogRecord],
    slo_target_pct: float = 99.9,
    window_days: float = 30.0,
    include_4xx: bool = False,
) -> tuple[BurnRateReport, List[BurnRateReport]]:
    """Analyze all logs and return (overall_report, per_endpoint_reports)."""
    anchor_ts = max((r.timestamp for r in records), default=None)
    overall = analyze_slice(
        endpoint="ALL",
        records=records,
        slo_target_pct=slo_target_pct,
        window_days=window_days,
        include_4xx=include_4xx,
        anchor_ts=anchor_ts,
    )

    grouped = group_records_by_endpoint(records)
    endpoint_reports = [
        analyze_slice(
            endpoint=ep,
            records=ep_records,
            slo_target_pct=slo_target_pct,
            window_days=window_days,
            include_4xx=include_4xx,
            anchor_ts=anchor_ts,
        )
        for ep, ep_records in grouped.items()
    ]
    return overall, endpoint_reports
