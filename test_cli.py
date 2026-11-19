"""Comprehensive unittest suite for api-slo-error-budget-burn-rate-cli-lite."""

from __future__ import annotations

import csv
import io
import json
import math
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from burn_rate import (
    ALERT_CRITICAL,
    ALERT_HIGH,
    ALERT_OK,
    ALERT_WARNING,
    analyze_logs,
    analyze_slice,
    compute_burn_rate,
    evaluate_alert_tier,
)
from metrics import (
    LogRecord,
    compute_endpoint_metrics,
    is_error_status,
    load_logs,
    parse_timestamp,
    percentile,
    time_span_minutes,
)
from slo_cli import format_csv, format_json, format_markdown, main, sort_reports


def _make_record(minutes_offset: float, endpoint: str, status: int, latency: float) -> LogRecord:
    base = datetime(2025, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    return LogRecord(
        timestamp=base + timedelta(minutes=minutes_offset),
        endpoint=endpoint,
        status_code=status,
        latency_ms=latency,
    )


class TestTimestampAndPercentiles(unittest.TestCase):
    def test_parse_iso_z(self) -> None:
        dt = parse_timestamp("2025-05-01T12:30:00Z")
        self.assertEqual(dt, datetime(2025, 5, 1, 12, 30, 0, tzinfo=timezone.utc))

    def test_parse_iso_offset_converted_to_utc(self) -> None:
        dt = parse_timestamp("2025-05-01T14:30:00+02:00")
        self.assertEqual(dt, datetime(2025, 5, 1, 12, 30, 0, tzinfo=timezone.utc))

    def test_parse_iso_naive_assumed_utc(self) -> None:
        dt = parse_timestamp("2025-05-01 12:30:00")
        self.assertEqual(dt, datetime(2025, 5, 1, 12, 30, 0, tzinfo=timezone.utc))

    def test_parse_epoch_seconds_and_ms(self) -> None:
        dt_sec = parse_timestamp(1746100800)
        dt_ms = parse_timestamp("1746100800000")
        self.assertEqual(dt_sec, dt_ms)
        self.assertEqual(dt_sec.tzinfo, timezone.utc)

    def test_parse_invalid_timestamp_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_timestamp("")
        with self.assertRaises(ValueError):
            parse_timestamp("not-a-date")

    def test_percentile_empty_and_single(self) -> None:
        self.assertEqual(percentile([], 50), 0.0)
        self.assertEqual(percentile([42.5], 99), 42.5)

    def test_percentile_linear_interpolation(self) -> None:
        vals = [10.0, 20.0, 30.0, 40.0, 50.0]
        self.assertEqual(percentile(vals, 0), 10.0)
        self.assertEqual(percentile(vals, 50), 30.0)
        self.assertAlmostEqual(percentile(vals, 90), 46.0)
        self.assertAlmostEqual(percentile(vals, 95), 48.0)
        self.assertAlmostEqual(percentile(vals, 99), 49.6)
        self.assertEqual(percentile(vals, 100), 50.0)

    def test_percentile_out_of_bounds(self) -> None:
        with self.assertRaises(ValueError):
            percentile([1.0, 2.0], 101.0)
        with self.assertRaises(ValueError):
            percentile([1.0, 2.0], -1.0)


class TestIngestionAndMetrics(unittest.TestCase):
    def test_is_error_status_default_vs_include_4xx(self) -> None:
        self.assertFalse(is_error_status(200))
        self.assertFalse(is_error_status(404, include_4xx=False))
        self.assertTrue(is_error_status(404, include_4xx=True))
        self.assertTrue(is_error_status(503, include_4xx=False))

    def test_load_csv_auto_detect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "logs.csv"
            p.write_text(
                "timestamp,endpoint,status_code,latency_ms\n"
                "2025-05-01T12:00:00Z,/health,200,10.5\n"
                "2025-05-01T12:01:00Z,/pay,500,250.0\n",
                encoding="utf-8",
            )
            records = load_logs(p)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0].endpoint, "/health")
            self.assertEqual(records[1].status_code, 500)

    def test_load_ndjson_auto_detect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "logs.ndjson"
            p.write_text(
                '{"timestamp": "2025-05-01T12:05:00Z", "endpoint": "/a", "status_code": 200, "latency_ms": 12}\n'
                '{"timestamp": 1746100800, "endpoint": "/b", "status_code": 502, "latency_ms": 300}\n',
                encoding="utf-8",
            )
            records = load_logs(p)
            self.assertEqual(len(records), 2)
            # Sorted chronologically: 1746100800 is 2025-05-01T12:00:00Z
            self.assertEqual(records[0].endpoint, "/b")
            self.assertEqual(records[1].endpoint, "/a")

    def test_load_csv_missing_column_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.csv"
            p.write_text("timestamp,endpoint,status_code\n2025-05-01T12:00:00Z,/a,200\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_logs(p)

    def test_load_ndjson_malformed_json_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.ndjson"
            p.write_text("{broken json\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_logs(p)

    def test_load_missing_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_logs("nonexistent_file_xyz_123.csv")

    def test_compute_endpoint_metrics_empty(self) -> None:
        m = compute_endpoint_metrics("/empty", [])
        self.assertEqual(m.total_requests, 0)
        self.assertEqual(m.availability_pct, 100.0)
        self.assertEqual(m.p99_ms, 0.0)

    def test_compute_endpoint_metrics_values(self) -> None:
        recs = [
            _make_record(0, "/orders", 200, 10.0),
            _make_record(1, "/orders", 200, 20.0),
            _make_record(2, "/orders", 404, 30.0),
            _make_record(3, "/orders", 500, 100.0),
        ]
        m_default = compute_endpoint_metrics("/orders", recs, include_4xx=False)
        self.assertEqual(m_default.error_requests, 1)
        self.assertAlmostEqual(m_default.availability_pct, 75.0)
        self.assertAlmostEqual(m_default.avg_ms, 40.0)
        self.assertAlmostEqual(m_default.p50_ms, 25.0)

        m_4xx = compute_endpoint_metrics("/orders", recs, include_4xx=True)
        self.assertEqual(m_4xx.error_requests, 2)
        self.assertAlmostEqual(m_4xx.availability_pct, 50.0)

    def test_time_span_minutes(self) -> None:
        self.assertEqual(time_span_minutes([]), 0.0)
        self.assertEqual(time_span_minutes([_make_record(0, "/a", 200, 10)]), 0.0)
        recs = [_make_record(0, "/a", 200, 10), _make_record(90, "/a", 200, 10)]
        self.assertAlmostEqual(time_span_minutes(recs), 90.0)


class TestBurnRateAndAlerts(unittest.TestCase):
    def test_compute_burn_rate_normal_and_zero_budget(self) -> None:
        self.assertAlmostEqual(compute_burn_rate(0.001, 0.001), 1.0)
        self.assertAlmostEqual(compute_burn_rate(0.0144, 0.001), 14.4)
        self.assertEqual(compute_burn_rate(0.0, 0.0), 0.0)
        self.assertTrue(math.isinf(compute_burn_rate(0.01, 0.0)))

    def test_error_budget_math_30d_999(self) -> None:
        # 1000 requests over 60 minutes, 1 error => err_rate = 0.001, burn_rate = 1.0
        recs = [_make_record(i * (60.0 / 999.0), "/api", 200, 20.0) for i in range(999)]
        recs.append(_make_record(60.0, "/api", 500, 20.0))
        rep = analyze_slice("/api", recs, slo_target_pct=99.9, window_days=30.0)
        self.assertAlmostEqual(rep.total_budget_minutes, 43.2, places=4)
        self.assertAlmostEqual(rep.overall_burn_rate, 1.0, places=4)
        self.assertAlmostEqual(rep.consumed_budget_minutes, 0.06, places=4)
        self.assertAlmostEqual(rep.remaining_budget_minutes, 43.14, places=4)
        self.assertIsNotNone(rep.time_to_exhaustion_hours)
        self.assertAlmostEqual(rep.time_to_exhaustion_hours or 0.0, 719.0, places=1)

    def test_alert_tier_critical(self) -> None:
        tier, _ = evaluate_alert_tier({"5m": 15.0, "1h": 14.4, "30m": 10.0, "6h": 7.0, "2h": 4.0, "24h": 4.0, "72h": 2.0})
        self.assertEqual(tier, ALERT_CRITICAL)

    def test_alert_tier_high(self) -> None:
        tier, _ = evaluate_alert_tier({"5m": 2.0, "1h": 3.0, "30m": 6.5, "6h": 6.0, "2h": 4.0, "24h": 4.0, "72h": 2.0})
        self.assertEqual(tier, ALERT_HIGH)

    def test_alert_tier_warning(self) -> None:
        tier1, _ = evaluate_alert_tier({"5m": 0.5, "1h": 1.0, "30m": 1.0, "6h": 1.0, "2h": 3.5, "24h": 3.0, "72h": 1.0})
        tier2, _ = evaluate_alert_tier({"5m": 0.0, "1h": 0.5, "30m": 0.5, "6h": 1.2, "2h": 1.0, "24h": 1.2, "72h": 1.1})
        self.assertEqual(tier1, ALERT_WARNING)
        self.assertEqual(tier2, ALERT_WARNING)

    def test_alert_tier_ok(self) -> None:
        tier, _ = evaluate_alert_tier({"5m": 0.0, "1h": 0.5, "30m": 0.5, "6h": 0.5, "2h": 0.5, "24h": 0.5, "72h": 0.5})
        self.assertEqual(tier, ALERT_OK)

    def test_analyze_slice_invalid_params(self) -> None:
        with self.assertRaises(ValueError):
            analyze_slice("/a", [], slo_target_pct=0.0)
        with self.assertRaises(ValueError):
            analyze_slice("/a", [], slo_target_pct=101.0)
        with self.assertRaises(ValueError):
            analyze_slice("/a", [], window_days=0.0)

    def test_analyze_logs_groups_and_uses_shared_anchor(self) -> None:
        recs = [
            _make_record(0, "/a", 200, 10.0),
            _make_record(10, "/a", 500, 50.0),
            _make_record(120, "/b", 200, 15.0),
        ]
        overall, endpoints = analyze_logs(recs, slo_target_pct=99.9)
        self.assertEqual(overall.metrics.total_requests, 3)
        self.assertEqual(len(endpoints), 2)
        ep_a = next(r for r in endpoints if r.metrics.endpoint == "/a")
        # /a had no requests in the last 5m relative to global anchor (120m), so 5m burn rate is 0.0
        self.assertEqual(ep_a.window_burn_rates["5m"], 0.0)
        self.assertGreater(ep_a.window_burn_rates["6h"], 0.0)


class TestFormattingAndCLI(unittest.TestCase):
    def _sample_reports(self):
        recs = [
            _make_record(0, "/healthy", 200, 10.0),
            _make_record(5, "/healthy", 200, 20.0),
            _make_record(10, "/failing", 500, 200.0),
            _make_record(12, "/failing", 200, 50.0),
        ]
        return analyze_logs(recs, slo_target_pct=99.9)

    def test_format_json_valid_rfc8259(self) -> None:
        overall, endpoints = self._sample_reports()
        text = format_json(overall, endpoints)
        data = json.loads(text)
        self.assertEqual(data["schema_version"], "1.0")
        self.assertEqual(data["overall"]["total_requests"], 4)
        self.assertEqual(len(data["endpoints"]), 2)

    def test_format_csv_valid(self) -> None:
        overall, endpoints = self._sample_reports()
        text = format_csv(overall, endpoints)
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[0][0], "endpoint")
        self.assertEqual(len(rows), 4)  # header + ALL + 2 endpoints

    def test_format_markdown_contains_table(self) -> None:
        overall, endpoints = self._sample_reports()
        md = format_markdown(overall, endpoints)
        self.assertIn("# API SLO & Error Budget Burn-Rate Report", md)
        self.assertIn("| `/failing` |", md)
        self.assertIn("| `/healthy` |", md)

    def test_sort_reports(self) -> None:
        _, endpoints = self._sample_reports()
        by_ep = sort_reports(endpoints, "endpoint")
        self.assertEqual([r.metrics.endpoint for r in by_ep], ["/failing", "/healthy"])
        by_p99 = sort_reports(endpoints, "p99")
        self.assertEqual(by_p99[0].metrics.endpoint, "/failing")

    def test_cli_end_to_end_json_out_file_and_fail_on_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "api.csv"
            out_path = Path(tmp) / "report.json"
            log_path.write_text(
                "timestamp,endpoint,status_code,latency_ms\n"
                "2025-05-01T12:00:00Z,/checkout,500,320.0\n"
                "2025-05-01T12:01:00Z,/checkout,500,410.0\n",
                encoding="utf-8",
            )
            rc = main(
                [
                    str(log_path),
                    "--output",
                    "json",
                    "--out-file",
                    str(out_path),
                    "--fail-on-alert",
                ]
            )
            self.assertEqual(rc, 2)
            self.assertTrue(out_path.is_file())
            parsed = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["overall"]["alert_tier"], ALERT_CRITICAL)

    def test_cli_missing_file_returns_1(self) -> None:
        stderr_buf = io.StringIO()
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            rc = main(["does_not_exist_999.csv"])
        self.assertEqual(rc, 1)
        self.assertIn("Error:", stderr_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
