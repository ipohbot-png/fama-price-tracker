# FAMA Price Tracker

Daily Malaysian farm produce prices, tracked at three market levels — **Ladang**
(farm-gate), **Borong** (wholesale), **Runcit** (retail) — with trend charts.

**Dashboard: https://ipohbot-png.github.io/fama-price-tracker/**

## Why

FAMA publishes daily prices at
[fama.gov.my/harga-pasaran-terkini](https://www.fama.gov.my/harga-pasaran-terkini),
but only as an embedded Power BI report showing a single day's averages, and the
source dataset keeps just a **rolling ~30-day window**. This project scrapes the
report's public data API twice daily, archives every row in git, and serves a
fast static dashboard with price history that grows beyond FAMA's window.

## How it works

```
FAMA Power BI public API ──> scraper/scrape.py ──> data/daily/YYYY-MM-DD.csv (archive)
                                                        │
                                        scraper/aggregate.py
                                                        │
                                              site/data/*.json ──> static dashboard (GitHub Pages)
```

- **Scrape**: `python -m scraper.scrape` — fetches the full rolling window
  (~3,500 rows/day: every product × grade × market × level), verifies each
  date's row count against a server-side control query, and merges into the
  archive idempotently (missed days self-heal for up to ~30 days).
- **Aggregate**: `python -m scraper.aggregate` — builds the dashboard JSONs
  (product catalog, per-product time series national + per-state, latest
  prices with disclosed comparison dates, per-state latest files, update-time
  analysis).
- **Automation**: GitHub Actions ([scrape.yml](.github/workflows/scrape.yml))
  runs at 12:30 and 23:00 MYT. FAMA's median price-entry time is ~10:34 MYT
  (measured from `systemdate`), so the midday run captures the day's data and
  the evening run picks up stragglers. If a run fails, GitHub emails the repo
  owner by default.

Everything is Python stdlib — no dependencies to install (pytest only for tests).

## Data notes

- Source: FAMA "Paparan Harian Harga AMI Seluruh Malaysia" public Power BI
  dataset. Prices are FAMA's survey figures, not offers to buy/sell.
- Archive starts **2026-06-27** (earliest date still in FAMA's window when this
  project began). History accumulates from there.
- Coverage varies by weekday: Thursdays ~6,000 rows, Sundays sometimes near
  zero. Products are surveyed against rotating subsets of states, so the
  dashboard flags day-over-day comparisons where the underlying basket of
  markets changed materially (excluded from "Top movers").
- National average = mean over all reported rows for that product/level/date.

## Development

```
python -m pytest tests/     # 108 tests, no network
python -m scraper.scrape    # exit 0 ok / 1 verification mismatch / 2 transport failure
python -m scraper.aggregate
cd site && python -m http.server 8000   # local dashboard
```

Power BI coordinates (resource key, model, dataset, entity) live in
`scraper/powerbi.py` and can be overridden via `FAMA_RESOURCE_KEY`,
`FAMA_MODEL_ID`, `FAMA_DATASET_ID`, `FAMA_ENTITY` if FAMA republishes the report.

See [DESIGN.md](DESIGN.md) for the full architecture and data contracts.

## Known limitations

- Series JSONs are dense and grow with archive length; fine for years at
  current rates (~72 KB/day), but a sparse format is the eventual fix.
- The `reliable` flag covers the day-over-day comparison only, not week-over-week.
- Grade-level product splits (e.g. TELUR AYAM A/B/C) are stable via
  `site/data/slugs.json`; do not delete that file.
