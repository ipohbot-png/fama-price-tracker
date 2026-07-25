"""Verified spike code (2026-07-25) — reference for the real scraper.

Demonstrates working calls against the FAMA Power BI public API:
- distinct peringkat values -> ["Ladang", "Borong", "Runcit"]
- min/max/count of `tarikh harga` -> rolling ~30-day window
- sample detail rows incl. systemdate, negeri, daerah, lokasi, peringkat,
  varieti, gred MID, unit, harga

Run: python -m scraper.reference_spike
"""
import gzip
import json
import urllib.request

RESOURCE_KEY = "b41dccd7-d9f7-4f56-80fe-127696493f53"
API = "https://wabi-south-east-asia-api.analysis.windows.net/public/reports/querydata?synchronous=true"
MODEL_ID = 6546643
DATASET_ID = "185b7047-f327-4ef7-897a-3168956a1850"
ENTITY = "API Harga (30hari)"


def run_query(q):
    body = {
        "version": "1.0.0",
        "queries": [{
            "Query": {"Commands": [{"SemanticQueryDataShapeCommand": q}]},
            "QueryId": "",
            "ApplicationContext": {"DatasetId": DATASET_ID},
        }],
        "cancelQueries": [],
        "modelId": MODEL_ID,
    }
    req = urllib.request.Request(
        API, data=json.dumps(body).encode(),
        headers={"X-PowerBI-ResourceKey": RESOURCE_KEY,
                 "Content-Type": "application/json",
                 "Accept-Encoding": "gzip"})
    r = urllib.request.urlopen(req, timeout=90)
    raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def col(src, prop, name=None):
    return {"Column": {"Expression": {"SourceRef": {"Source": src}},
                       "Property": prop}, "Name": name or prop}


if __name__ == "__main__":
    q = {
        "Query": {
            "Version": 2,
            "From": [{"Name": "t", "Entity": ENTITY, "Type": 0}],
            "Select": [
                col("t", "tarikh harga"), col("t", "systemdate"),
                col("t", "negeri"), col("t", "daerah"), col("t", "lokasi"),
                col("t", "peringkat"), col("t", "varieti"),
                col("t", "gred MID"), col("t", "unit"),
                {"Aggregation": {"Expression": {"Column": {
                    "Expression": {"SourceRef": {"Source": "t"}},
                    "Property": "harga"}}, "Function": 0}, "Name": "harga"},
            ],
        },
        "Binding": {
            "Primary": {"Groupings": [{"Projections": list(range(10))}]},
            "DataReduction": {"DataVolume": 3, "Primary": {"Top": {"Count": 5}}},
            "Version": 1,
        },
    }
    res = run_query(q)
    print(json.dumps(res["results"][0]["result"]["data"]["dsr"],
                     ensure_ascii=False, indent=1)[:4000])
