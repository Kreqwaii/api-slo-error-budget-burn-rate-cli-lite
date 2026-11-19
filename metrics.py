"""Log ingestion and SLI metrics computation using Python stdlib only."""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class LogRecord:
    """Single API request log entry."""

    timestamp: datetime
    endpoint: str
    status_code: int
    latency_ms: float


@dataclass(frozen=True)
class EndpointMetrics:
    """Aggregated SLI metrics for a single endpoint or overall traffic."""

    endpoint: str
    total_requests: int
    good_requests: int
    error_requests: int
    availability_pct: float
    error_rate_pct: float
    min_ms: float
    avg_ms: float
    p50_ms: float
    p90_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float


def parse_timestamp(raw: object) -> datetime:
    """Parse ISO-8601 string or Unix epoch (seconds/milliseconds) into UTC datetime."""
    if isinstance(raw, (int, float)):
        val = float(raw)
        if not math.isfinite(val):
            raise ValueError(f"Non-finite numeric timestamp: {raw!r}")
        if abs(val) > 1e11:
            val /= 1000.0
        return datetime.fromtimestamp(val, tz=timezone.utc)

    if not isinstance(raw, str):
        raise ValueError(f"Unsupported timestamp type: {type(raw).__name__}")

    text = raw.strip()
    if not text:
        raise ValueError("Empty timestamp")

    # Numeric string (epoch seconds or ms)
    try:
        num = float(text)
        if math.isfinite(num):
            if abs(num) > 1e11:
                num /= 1000.0
            return datetime.fromtimestamp(num, tz=timezone.utc)
    except ValueError:
        pass

    # ISO-8601
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def percentile(values: Sequence[float], pct: float) -> float:
    """Compute exact linear-interpolated percentile (0..100) without external libs."""
    if not 0.0 <= pct <= 100.0:
        raise ValueError(f"Percentile must be between 0 and 100, got {pct}")
    n = len(values)
    if n == 0:
        return 0.0
    if n == 1:
        return float(values[0])

    ordered = sorted(float(v) for v in values)
    idx = (pct / 100.0) * (n - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return ordered[lo]
    frac = idx - lo
    return ordered[lo] + frac * (ordered[hi] - ordered[lo])


def is_error_status(status_code: int, include_4xx: bool = False) -> bool:
    """Return True if HTTP status code counts as an availability error."""
    if include_4xx:
        return status_code >= 400
    return 500 <= status_code < 600


def _detect_format(path: Path, fmt: str) -> str:
    normalized = fmt.lower().strip()
    if normalized in ("csv", "ndjson"):
        return normalized
    if normalized != "auto":
        raise ValueError(f"Unsupported format: {fmt!r}. Expected auto, csv, or ndjson.")

    suffix = path.suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix in (".ndjson", ".jsonl", ".json"):
        return "ndjson"

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped:
                return "ndjson" if stripped.startswith("{") else "csv"
    return "csv"


def _build_record(
    ts_raw: object,
    endpoint_raw: object,
    status_raw: object,
    latency_raw: object,
    line_num: int,
) -> LogRecord:
    try:
        ts = parse_timestamp(ts_raw)
    except Exception as exc:
        raise ValueError(f"Line {line_num}: invalid timestamp {ts_raw!r}: {exc}") from exc

    endpoint = str(endpoint_raw).strip() if endpoint_raw is not None else ""
    if not endpoint:
        raise ValueError(f"Line {line_num}: missing endpoint")

    try:
        status_code = int(status_raw)  # type: ignore[arg-type]
    except Exception as exc:
        raise ValueError(f"Line {line_num}: invalid status_code {status_raw!r}") from exc

    try:
        latency_ms = float(latency_raw)  # type: ignore[arg-type]
    except Exception as exc:
        raise ValueError(f"Line {line_num}: invalid latency_ms {latency_raw!r}") from exc

    if not math.isfinite(latency_ms) or latency_ms < 0:
        raise ValueError(f"Line {line_num}: latency_ms must be non-negative finite, got {latency_raw!r}")

    return LogRecord(
        timestamp=ts,
        endpoint=endpoint,
        status_code=status_code,
        latency_ms=latency_ms,
    )


def load_logs(path: str | Path, fmt: str = "auto") -> List[LogRecord]:
    """Load API request logs from a CSV or NDJSON file."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"Log file not found: {file_path}")

    resolved_fmt = _detect_format(file_path, fmt)
    records: List[LogRecord] = []

    if resolved_fmt == "csv":
        with file_path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                return []
            field_map = {name.strip(): name for name in reader.fieldnames if name is not None}
            required = {"timestamp", "endpoint", "status_code", "latency_ms"}
            missing = required - set(field_map.keys())
            if missing:
                raise ValueError(f"CSV missing required columns: {', '.join(sorted(missing))}")

            for line_num, row in enumerate(reader, start=2):
                if not row or all(v is None or str(v).strip() == "" for v in row.values()):
                    continue
                records.append(
                    _build_record(
                        row[field_map["timestamp"]],
                        row[field_map["endpoint"]],
                        row[field_map["status_code"]],
                        row[field_map["latency_ms"]],
                        line_num,
                    )
                )
    else:
        with file_path.open("r", encoding="utf-8") as fh:
            for line_num, raw_line in enumerate(fh, start=1):
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Line {line_num}: invalid JSON: {exc}") from exc
                if not isinstance(obj, dict):
                    raise ValueError(f"Line {line_num}: expected JSON object")
                for key in ("timestamp", "endpoint", "status_code", "latency_ms"):
                    if key not in obj:
                        raise ValueError(f"Line {line_num}: missing required key {key!r}")
                records.append(
                    _build_record(
                        obj["timestamp"],
                        obj["endpoint"],
                        obj["status_code"],
                        obj["latency_ms"],
                        line_num,
                    )
                )

    records.sort(key=lambda r: r.timestamp)
    return records


def compute_endpoint_metrics(
    endpoint: str,
    records: Sequence[LogRecord],
    include_4xx: bool = False,
) -> EndpointMetrics:
    """Compute availability and latency percentiles for a collection of LogRecords."""
    total = len(records)
    if total == 0:
        return EndpointMetrics(
            endpoint=endpoint,
            total_requests=0,
            good_requests=0,
            error_requests=0,
            availability_pct=100.0,
            error_rate_pct=0.0,
            min_ms=0.0,
            avg_ms=0.0,
            p50_ms=0.0,
            p90_ms=0.0,
            p95_ms=0.0,
            p99_ms=0.0,
            max_ms=0.0,
        )

    errors = sum(1 for r in records if is_error_status(r.status_code, include_4xx=include_4xx))
    good = total - errors
    error_rate = errors / total
    availability = good / total

    latencies = sorted(r.latency_ms for r in records)
    avg_ms = sum(latencies) / total

    return EndpointMetrics(
        endpoint=endpoint,
        total_requests=total,
        good_requests=good,
        error_requests=errors,
        availability_pct=availability * 100.0,
        error_rate_pct=error_rate * 100.0,
        min_ms=latencies[0],
        avg_ms=avg_ms,
        p50_ms=percentile(latencies, 50.0),
        p90_ms=percentile(latencies, 90.0),
        p95_ms=percentile(latencies, 95.0),
        p99_ms=percentile(latencies, 99.0),
        max_ms=latencies[-1],
    )


def group_records_by_endpoint(records: Iterable[LogRecord]) -> Dict[str, List[LogRecord]]:
    """Group LogRecords by endpoint preserving timestamp order."""
    grouped: Dict[str, List[LogRecord]] = {}
    for rec in records:
        grouped.setdefault(rec.endpoint, []).append(rec)
    return grouped


def time_span_minutes(records: Sequence[LogRecord]) -> float:
    """Return observed time span in minutes between earliest and latest record."""
    if len(records) < 2:
        return 0.0
    min_ts = min(r.timestamp for r in records)
    max_ts = max(r.timestamp for r in records)
    return max(0.0, (max_ts - min_ts).total_seconds() / 60.0)
