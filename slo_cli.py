#!/usr/bin/env python3
"""Zero-dependency CLI to compute API SLA/SLO, latency percentiles, and SRE error budget burn rate."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from burn_rate import ALERT_CRITICAL, ALERT_HIGH, BurnRateReport, analyze_logs
from metrics import load_logs


__version__ = "1.0.0"


def _safe_num(value: Optional[float], decimals: int = 4) -> Optional[float]:
    if value is None:
        return None
    if math.isnan(value):
        return None
    if math.isinf(value):
        return None
    return round(value, decimals)


def sort_reports(reports: List[BurnRateReport], sort_by: str) -> List[BurnRateReport]:
    """Sort endpoint reports by the selected metric."""
    key_map = {
        "burn_rate": lambda r: (r.overall_burn_rate, r.metrics.error_rate_pct),
        "error_rate": lambda r: (r.metrics.error_rate_pct, r.metrics.total_requests),
        "p99": lambda r: r.metrics.p99_ms,
        "requests": lambda r: r.metrics.total_requests,
        "endpoint": lambda r: r.metrics.endpoint,
    }
    if sort_by not in key_map:
        raise ValueError(f"Unsupported sort key: {sort_by!r}")
    reverse = sort_by != "endpoint"
    return sorted(reports, key=key_map[sort_by], reverse=reverse)


def report_to_dict(report: BurnRateReport) -> Dict[str, Any]:
    """Serialize a BurnRateReport into JSON-safe dictionary."""
    m = report.metrics
    return {
        "endpoint": m.endpoint,
        "total_requests": m.total_requests,
        "good_requests": m.good_requests,
        "error_requests": m.error_requests,
        "availability_pct": _safe_num(m.availability_pct, 4),
        "error_rate_pct": _safe_num(m.error_rate_pct, 4),
        "latency_ms": {
            "min": _safe_num(m.min_ms, 2),
            "avg": _safe_num(m.avg_ms, 2),
            "p50": _safe_num(m.p50_ms, 2),
            "p90": _safe_num(m.p90_ms, 2),
            "p95": _safe_num(m.p95_ms, 2),
            "p99": _safe_num(m.p99_ms, 2),
            "max": _safe_num(m.max_ms, 2),
        },
        "slo": {
            "target_pct": report.slo_target_pct,
            "window_days": report.window_days,
            "observed_span_minutes": _safe_num(report.observed_span_minutes, 2),
            "total_budget_minutes": _safe_num(report.total_budget_minutes, 4),
            "consumed_budget_minutes": _safe_num(report.consumed_budget_minutes, 4),
            "remaining_budget_minutes": _safe_num(report.remaining_budget_minutes, 4),
            "budget_consumed_pct": _safe_num(report.budget_consumed_pct, 4),
            "overall_burn_rate": (
                "inf" if math.isinf(report.overall_burn_rate) else _safe_num(report.overall_burn_rate, 3)
            ),
            "time_to_exhaustion_hours": _safe_num(report.time_to_exhaustion_hours, 2),
        },
        "window_burn_rates": {
            k: ("inf" if math.isinf(v) else _safe_num(v, 3))
            for k, v in report.window_burn_rates.items()
        },
        "alert_tier": report.alert_tier,
        "alert_reason": report.alert_reason,
    }


def format_json(overall: BurnRateReport, endpoints: Sequence[BurnRateReport]) -> str:
    """Format full SRE report as RFC-compliant JSON."""
    payload = {
        "schema_version": "1.0",
        "overall": report_to_dict(overall),
        "endpoints": [report_to_dict(r) for r in endpoints],
    }
    return json.dumps(payload, indent=2, allow_nan=False)


def format_csv(overall: BurnRateReport, endpoints: Sequence[BurnRateReport]) -> str:
    """Format SRE summary report as CSV."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        [
            "endpoint",
            "total_requests",
            "error_requests",
            "availability_pct",
            "error_rate_pct",
            "p50_ms",
            "p90_ms",
            "p95_ms",
            "p99_ms",
            "total_budget_min",
            "consumed_budget_min",
            "remaining_budget_min",
            "burn_rate",
            "burn_5m",
            "burn_1h",
            "burn_6h",
            "burn_24h",
            "alert_tier",
            "alert_reason",
        ]
    )
    for r in [overall, *endpoints]:
        m = r.metrics
        br_str = "inf" if math.isinf(r.overall_burn_rate) else f"{r.overall_burn_rate:.3f}"
        writer.writerow(
            [
                m.endpoint,
                m.total_requests,
                m.error_requests,
                f"{m.availability_pct:.4f}",
                f"{m.error_rate_pct:.4f}",
                f"{m.p50_ms:.2f}",
                f"{m.p90_ms:.2f}",
                f"{m.p95_ms:.2f}",
                f"{m.p99_ms:.2f}",
                f"{r.total_budget_minutes:.4f}",
                f"{r.consumed_budget_minutes:.4f}",
                f"{r.remaining_budget_minutes:.4f}",
                br_str,
                f"{r.window_burn_rates.get('5m', 0.0):.3f}",
                f"{r.window_burn_rates.get('1h', 0.0):.3f}",
                f"{r.window_burn_rates.get('6h', 0.0):.3f}",
                f"{r.window_burn_rates.get('24h', 0.0):.3f}",
                r.alert_tier,
                r.alert_reason,
            ]
        )
    return buf.getvalue().rstrip("\n")


def _fmt_br(val: float) -> str:
    if math.isinf(val):
        return "inf"
    return f"{val:.2f}x"


def format_markdown(overall: BurnRateReport, endpoints: Sequence[BurnRateReport]) -> str:
    """Format SRE summary report as a clean Markdown report."""
    om = overall.metrics
    lines: List[str] = [
        f"# API SLO & Error Budget Burn-Rate Report (SLO: {overall.slo_target_pct:.3f}% / {overall.window_days:.0f}d)",
        "",
        "## Overall Summary",
        f"- **Requests:** {om.total_requests} total | {om.good_requests} good | {om.error_requests} errors",
        f"- **Availability:** {om.availability_pct:.4f}% (Error Rate: {om.error_rate_pct:.4f}%)",
        (
            f"- **Latency (ms):** p50={om.p50_ms:.2f} | p90={om.p90_ms:.2f} | "
            f"p95={om.p95_ms:.2f} | p99={om.p99_ms:.2f} | max={om.max_ms:.2f}"
        ),
        (
            f"- **Error Budget:** {overall.remaining_budget_minutes:.2f} / "
            f"{overall.total_budget_minutes:.2f} min remaining "
            f"({overall.budget_consumed_pct:.2f}% consumed over {overall.observed_span_minutes:.1f}m)"
        ),
        f"- **Overall Burn Rate:** {_fmt_br(overall.overall_burn_rate)} | **Alert:** `{overall.alert_tier}` ({overall.alert_reason})",
        "",
        "## Per-Endpoint Breakdown",
        "",
        "| Endpoint | Reqs | Errs | Avail % | p50 (ms) | p95 (ms) | p99 (ms) | Rem Budget (m) | Burn Rate | 5m / 1h / 6h | Alert |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]

    for r in [overall, *endpoints]:
        m = r.metrics
        w = r.window_burn_rates
        windows_str = f"{_fmt_br(w.get('5m', 0.0))} / {_fmt_br(w.get('1h', 0.0))} / {_fmt_br(w.get('6h', 0.0))}"
        lines.append(
            f"| `{m.endpoint}` | {m.total_requests} | {m.error_requests} | "
            f"{m.availability_pct:.3f}% | {m.p50_ms:.1f} | {m.p95_ms:.1f} | {m.p99_ms:.1f} | "
            f"{r.remaining_budget_minutes:.2f} | {_fmt_br(r.overall_burn_rate)} | "
            f"{windows_str} | **{r.alert_tier}** |"
        )

    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slo_cli",
        description="Compute API SLA/SLO availability, latency percentiles, and SRE error budget burn rate from CSV/NDJSON logs.",
    )
    parser.add_argument("input_file", help="Path to CSV or NDJSON API request log file")
    parser.add_argument(
        "-f",
        "--format",
        choices=["auto", "csv", "ndjson"],
        default="auto",
        help="Input log format (default: auto)",
    )
    parser.add_argument(
        "-o",
        "--output",
        choices=["markdown", "json", "csv"],
        default="markdown",
        help="Output report format (default: markdown)",
    )
    parser.add_argument(
        "--out-file",
        help="Optional file path to write the rendered report instead of stdout",
    )
    parser.add_argument(
        "--slo",
        type=float,
        default=99.9,
        help="Target availability SLO percentage in (0, 100] (default: 99.9)",
    )
    parser.add_argument(
        "--window-days",
        type=float,
        default=30.0,
        help="SLO compliance window in days (default: 30.0)",
    )
    parser.add_argument(
        "--include-4xx",
        action="store_true",
        help="Treat HTTP 4xx responses as errors in addition to 5xx",
    )
    parser.add_argument(
        "--sort-by",
        choices=["burn_rate", "error_rate", "p99", "requests", "endpoint"],
        default="burn_rate",
        help="Sort per-endpoint table by metric (default: burn_rate)",
    )
    parser.add_argument(
        "--fail-on-alert",
        action="store_true",
        help="Exit with code 2 if any endpoint or overall traffic triggers CRITICAL or HIGH alert",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        records = load_logs(args.input_file, fmt=args.format)
        overall, endpoints = analyze_logs(
            records,
            slo_target_pct=args.slo,
            window_days=args.window_days,
            include_4xx=args.include_4xx,
        )
        sorted_endpoints = sort_reports(endpoints, args.sort_by)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.output == "json":
        rendered = format_json(overall, sorted_endpoints)
    elif args.output == "csv":
        rendered = format_csv(overall, sorted_endpoints)
    else:
        rendered = format_markdown(overall, sorted_endpoints)

    if args.out_file:
        out_path = Path(args.out_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)

    if args.fail_on_alert:
        has_severe = any(
            r.alert_tier in (ALERT_CRITICAL, ALERT_HIGH)
            for r in [overall, *sorted_endpoints]
        )
        if has_severe:
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
