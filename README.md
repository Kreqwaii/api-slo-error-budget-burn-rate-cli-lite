# API SLO & Error Budget Burn-Rate CLI (Lite)

Zero-dependency Python 3.9+ CLI tool to compute **API SLA/SLO availability**, **exact p50/p90/p95/p99 latency percentiles**, **remaining error budget minutes**, and **Google SRE multi-window burn-rate alerts** directly from CSV or NDJSON request logs.

> **100% Pure Python Stdlib** — zero `pip` dependencies, fast execution, ready for CI/CD gates, cron jobs, and incident postmortems.

---

## Features

- **CSV & NDJSON Log Ingestion:** Auto-detects format and parses ISO-8601 timestamps (UTC/offset/naive) as well as Unix epoch seconds and milliseconds.
- **Per-Endpoint & Overall SLIs:** Computes total requests, good/error counts, availability %, error rate %, and exact linear-interpolated `min`, `avg`, `p50`, `p90`, `p95`, `p99`, and `max` latencies.
- **Error Budget Tracking:** Calculates total SLO budget minutes (e.g., `43.20 min` for `99.9%` over `30` days), consumed minutes, remaining minutes, and projected time-to-exhaustion.
- **Multi-Window Burn-Rate Alerting (Google SRE Standard):**
  - `CRITICAL` (Fast Burn / Page): `1h >= 14.4x` **and** `5m >= 14.4x`
  - `HIGH` (Fast Burn / Page): `6h >= 6.0x` **and** `30m >= 6.0x`
  - `WARNING` (Slow Burn / Ticket): `24h >= 3.0x` **and** `2h >= 3.0x` (or `72h >= 1.0x` **and** `6h >= 1.0x`)
  - `OK`: Within error budget
- **Multiple Output Formats:** Markdown tables (`--output markdown`), RFC-compliant JSON (`--output json`), and CSV (`--output csv`).
- **CI/CD Gate Mode:** `--fail-on-alert` exits with code `2` when `CRITICAL` or `HIGH` burn rate is detected.

---

## Quick Start

```bash
# Clone the repository
git clone https://github.com/Kreqwaii/api-slo-error-budget-burn-rate-cli-lite.git
cd api-slo-error-budget-burn-rate-cli-lite

# Run on included sample logs (Markdown output)
python slo_cli.py sample_logs.csv

# Export JSON report with 99.95% SLO and 4xx counted as errors
python slo_cli.py sample_logs.csv --slo 99.95 --include-4xx --output json --out-file report.json

# CI/CD gate: fail with exit code 2 on CRITICAL or HIGH burn-rate alerts
python slo_cli.py sample_logs.csv --slo 99.9 --fail-on-alert
```

### Sample SRE Report Output

Running `python slo_cli.py sample_logs.csv` produces:

| Endpoint | Reqs | Errs | Avail % | p50 (ms) | p95 (ms) | p99 (ms) | Rem Budget (m) | Burn Rate | 5m / 1h / 6h | Alert |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `ALL` | 130 | 6 | 95.385% | 82.0 | 757.9 | 868.3 | 39.12 | 46.15x | 500.00x / 65.22x / 46.15x | **CRITICAL** |
| `/api/v1/payments/charge` | 20 | 4 | 80.000% | 592.3 | 877.3 | 887.8 | 37.50 | 200.00x | 1000.00x / 200.00x / 200.00x | **CRITICAL** |
| `/api/v1/auth/login` | 25 | 1 | 96.000% | 125.8 | 180.9 | 186.9 | 40.32 | 40.00x | 0.00x / 62.50x / 40.00x | **HIGH** |
| `/api/v1/orders` | 25 | 1 | 96.000% | 243.4 | 416.8 | 419.0 | 40.32 | 40.00x | 0.00x / 62.50x / 40.00x | **HIGH** |
| `/api/v1/health` | 30 | 0 | 100.000% | 7.8 | 12.7 | 13.4 | 43.20 | 0.00x | 0.00x / 0.00x / 0.00x | **OK** |
| `/api/v1/users/profile` | 30 | 0 | 100.000% | 50.8 | 78.9 | 82.6 | 43.20 | 0.00x | 0.00x / 0.00x / 0.00x | **OK** |

---

## CLI Options

| Flag | Default | Description |
| --- | --- | --- |
| `input_file` | *(required)* | Path to `.csv` or `.ndjson`/`.jsonl` log file |
| `-f`, `--format` | `auto` | Input format: `auto`, `csv`, `ndjson` |
| `-o`, `--output` | `markdown` | Output format: `markdown`, `json`, `csv` |
| `--out-file` | `stdout` | Write report to file path |
| `--slo` | `99.9` | Target availability SLO percentage `(0, 100]` |
| `--window-days` | `30.0` | SLO compliance window in days |
| `--include-4xx` | `false` | Count HTTP 4xx as availability errors (default: 5xx only) |
| `--sort-by` | `burn_rate` | Sort endpoints by `burn_rate`, `error_rate`, `p99`, `requests`, or `endpoint` |
| `--fail-on-alert` | `false` | Exit with code `2` if `CRITICAL` or `HIGH` alert fires |

---

## Running Tests

```bash
python -m unittest discover -v
```

Includes **31 unit and end-to-end tests** covering timestamp parsing, exact percentile math, CSV/NDJSON ingestion, error budget math, multi-window burn-rate tiers, formatters, and CLI exit codes.

---

## Краткое описание (RU)

Лёгкая CLI-утилита на чистом Python (только стандартная библиотека, без `pip`-зависимостей) для расчёта **SLA/SLO доступности API**, точных перцентилей задержки (**p50/p90/p95/p99**), остатка **error budget** в минутах и многооконного **burn-rate** алертинга (по стандарту Google SRE) из CSV/NDJSON-логов. Поддерживает экспорт отчётов в Markdown, JSON и CSV, а также режим CI/CD-гейта (`--fail-on-alert`).

---

## Need Live 24/7 Monitoring, SQLite History, Web UI & Telegram Alerts?

This **Lite** CLI is ideal for ad-hoc log analysis and CI/CD checks. If you need a production-ready **24/7 API & Service Monitoring System** with:
- Continuous background health checks & latency tracking
- Persistent **SQLite** historical SLA/SLO storage
- Real-time **Web Dashboard** & incident timeline
- Instant **Telegram / Webhook** burn-rate & downtime alerts

Check out the **Pro Monitoring & Automation Solution** on FunPay:
- **[Live 24/7 API Monitoring & Alerting Pro (FunPay Offer)](https://funpay.com/lots/offer?id=82604437)**
- **[Full Python Automation & Monitoring Catalog](https://funpay.com/users/20096179/)**

---

## License

MIT License — see [LICENSE](LICENSE) for details.
