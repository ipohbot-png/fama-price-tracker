# FAMA Price Tracker — Design Document

Tracks daily Malaysian farm produce prices from FAMA (Lembaga Pemasaran Pertanian
Persekutuan) at three market levels — **Ladang** (farm-gate), **Borong** (wholesale),
**Runcit** (retail) — and presents trends in a static dashboard on GitHub Pages.

## 1. Data source (verified 2026-07-25)

The FAMA page https://www.fama.gov.my/harga-pasaran-terkini embeds a public
"Publish to web" Power BI report. Its data API is directly queryable — no HTML
scraping, no authentication.

- **Embed URL param `r`** (base64 JSON): `{"k":"b41dccd7-d9f7-4f56-80fe-127696493f53","t":"ba79c739-fdba-4c53-a51b-b0a614bae87f","c":10}`
- **Resource key**: `b41dccd7-d9f7-4f56-80fe-127696493f53` (send as header `X-PowerBI-ResourceKey`)
- **API host**: `https://wabi-south-east-asia-api.analysis.windows.net`
  - `POST /public/reports/querydata?synchronous=true` — run semantic queries
  - `GET  /public/reports/{resourceKey}/modelsAndExploration?preferReadOnlySession=true` — report metadata
  - `POST /public/reports/conceptualschema` — table/column schema (body `{"modelIds":[6546643]}`)
- **Model id**: `6546643`; **Dataset id**: `185b7047-f327-4ef7-897a-3168956a1850`
  (send in `ApplicationContext.DatasetId`). Model name: "Paparan Harian Harga AMI Seluruh Malaysia".
- **Data table (entity)**: `API Harga (30hari)` — **rolling ~30-day window** (~3,500 rows/day,
  ~103k rows total). Older data disappears from the source; our repo is the archive.
- Responses may be gzipped regardless of Accept-Encoding — sniff magic bytes `\x1f\x8b`.
- Working spike code: `scraper/reference_spike.py`.

### Columns we capture (entity `API Harga (30hari)`)

| Column           | Meaning                                            |
|------------------|----------------------------------------------------|
| `priceid`        | unique row id — **dedupe key**                     |
| `tarikh harga`   | price date (epoch ms in DSR responses)             |
| `systemdate`     | entry timestamp string `YYYY-MM-DD HH:MM:SS.ffffff` — powers update-time analysis |
| `negeri`         | state                                              |
| `daerah`         | district                                           |
| `lokasi`         | market / collection point name                     |
| `peringkat`      | price level: `Ladang` / `Borong` / `Runcit`        |
| `sublevel`       | sub-level detail                                   |
| `varieti`        | product/variety name (BM)                          |
| `kategori`, `kumpulan`, `jenis` | category hierarchy                  |
| `gred MID`       | grade (e.g. F.A.Q, A/B/C)                          |
| `unit`           | Kilogram / Biji / ...                              |
| `harga`          | price (RM)                                         |
| `average14`      | source-computed 14-day average                     |
| `supply`         | supply indicator                                   |

### Query mechanics (from spike)

Semantic query JSON: `From` = `[{"Name":"t","Entity":"API Harga (30hari)","Type":0}]`,
`Select` = Column / Aggregation expressions, optional `Where` with
`Comparison`/`In` filters (used for per-date × per-state chunking),
`Binding.DataReduction.Primary.Top.Count` up to 30000.
Responses are DSR format: `ValueDicts` (string dictionaries `D0…Dn`), rows `C`
arrays with `R` repeat-bitmask (bit i set ⇒ column i repeats previous row's value)
and `Ø` null-bitmask. The decoder must handle both bitmasks.

## 2. Repository layout

```
fama-price-tracker/
├── DESIGN.md
├── README.md
├── requirements.txt            # stdlib-only preferred; pandas allowed if needed
├── scraper/
│   ├── __init__.py
│   ├── powerbi.py              # querydata client + DSR decoder (pure stdlib)
│   ├── scrape.py               # CLI: fetch window → data/daily/*.csv (idempotent merge)
│   ├── aggregate.py            # CLI: data/daily/*.csv → site/data/*.json
│   └── reference_spike.py      # verified spike code (reference only)
├── data/daily/YYYY-MM-DD.csv   # raw archive, one file per price date
├── site/                       # static dashboard (GitHub Pages artifact)
│   ├── index.html
│   ├── assets/…                # vendored JS/CSS (no CDN at runtime is OK on Pages, but vendor to be safe)
│   └── data/…                  # generated JSONs (gitignored? NO — committed, Pages serves them)
├── tests/
└── .github/workflows/scrape.yml
```

## 3. Contract A — daily CSV (`data/daily/YYYY-MM-DD.csv`)

UTF-8, header row, one file per **price date** (`tarikh harga`), sorted by `priceid`:

```
priceid,tarikh_harga,systemdate,negeri,daerah,lokasi,peringkat,sublevel,kategori,kumpulan,jenis,varieti,gred,unit,harga,average14,supply
```

- `tarikh_harga` as `YYYY-MM-DD`; `harga`/`average14` as decimal strings; empty for null.
- Scraper re-fetches the **entire available window** each run and merges by `priceid`
  (new rows added, existing rows overwritten with latest values) — idempotent,
  self-healing for missed days, captures late corrections.

## 4. Contract B — dashboard JSONs (`site/data/`)

- `catalog.json` — `{ "products": [{"id": "<slug>", "name": "...", "kategori": "...", "unit": "...", "grades": [...], "levels": [...], "latest": {"Ladang": x|null, "Borong": x|null, "Runcit": x|null}}], "states": [...], "dates": {"min": "...", "max": "..."} }`
- `series/<product-slug>.json` — `{ "name": ..., "unit": ..., "dates": ["YYYY-MM-DD",...], "national": {"Ladang": [x|null,...], "Borong": [...], "Runcit": [...]}, "by_state": {"PERAK": {"Ladang": [...], ...}, ...} }`
  Values = mean of `harga` across rows for that (date, level, [state], product), 2 dp.
- `latest.json` — latest date's per-product national averages per level + change vs previous
  available day and vs 7 days earlier: `{"date": ..., "rows": [{"id","name","unit","level","price","dod","wow"}]}`
- `meta.json` — `{ "generated_at_utc", "window": {...}, "row_count", "update_times": {"by_hour": {"00".."23": count}, "median_entry_local": "HH:MM", "note"}, "source": "..." }`
- Slug: lowercase varieti + grade when needed for uniqueness, non-alnum → `-`.
- Product identity = (`varieti`, `gred`) when a varieti has >1 grade (e.g. TELUR AYAM A/B/C),
  else `varieti` alone.

## 5. Dashboard (Phase 4)

Static, no build step. Vendored ECharts. Views:
1. **Overview**: latest-date table (product, level prices, DoD/WoW change), search box,
   top movers strip. Default scope = All Malaysia national average; a persistent
   scope toggle offers **Perak** one click away (user is Perak-based).
2. **Product detail**: line chart, x = date, three series (Ladang/Borong/Runcit),
   state selector (All + each state), grade selector where applicable.
3. **Compare**: 2–4 products on one chart at a chosen level.
4. **About/meta**: data source explanation, update-time histogram, last-scrape status.

Design: follow the `dataviz` skill system (loaded during Phase 4).
Mobile-friendly (user will check prices on phone).

## 6. Automation (Phase 5)

`.github/workflows/scrape.yml`:
- `schedule`: `30 4 * * *` (12:30 MYT) and `0 15 * * *` (23:00 MYT) + `workflow_dispatch`.
- Steps: checkout → setup-python → `python -m scraper.scrape` → `python -m scraper.aggregate`
  → commit & push if changed → deploy Pages (actions/deploy-pages from `site/`).
- Concurrency group to avoid overlapping runs; commit as github-actions bot.

## 7. Verification requirements (every phase)

- Scraper: row counts per (date × negeri) must match a control aggregation query;
  spot-check ≥5 (product, level, state, date) averages against the live report.
- Aggregator: unit tests with synthetic CSVs; national average must equal
  mean over rows (not mean of state means) — document the choice: **mean over rows**.
- Dashboard: renders from real generated JSONs; check a known value end-to-end
  (e.g. TELUR AYAM gred A ≈ RM0.47 on 2026-07-25, national).
- Honest reporting: any mismatch is surfaced, not smoothed over.
