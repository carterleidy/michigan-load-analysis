"""Fetch hourly demand from the EIA v2 API for MISO subregions.

Run from the project root (paths are relative to the project, so any folder works):
    python src/eia.py                     # update data/raw/miso_0027_load.csv
    python src/eia.py --subba 0027 0001   # update several subregions (one CSV each)
    python src/eia.py --list              # show MISO subregion codes and names
"""

import os
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"

load_dotenv(ROOT / ".env")
API_KEY = os.getenv("EIA_API_KEY")
BASE = "https://api.eia.gov/v2/electricity/rto/region-sub-ba-data/data/"
PAGE_SIZE = 5000


def _get(params, tries=4):
    """GET one page from EIA, retrying on rate limits and temporary server errors."""
    if not API_KEY:
        raise SystemExit("No EIA_API_KEY found. Check .env in the project root.")
    for attempt in range(tries):
        r = requests.get(BASE, params=params, timeout=60)
        if r.status_code in (429, 500, 502, 503, 504) and attempt < tries - 1:
            wait = 2 ** attempt
            print(f"  EIA returned {r.status_code}; retrying in {wait}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()["response"]


def fetch_subregion_demand(subba, start, end, parent="MISO", verbose=True):
    """
    Pull hourly demand (MWh) for one BA subregion.

    Args:
        subba:  subregion code, e.g. "0027" for MISO Zones 2 and 7
        start:  "YYYY-MM-DDTHH" in UTC, e.g. "2018-07-01T00"
        end:    "YYYY-MM-DDTHH" in UTC
        parent: parent balancing authority code

    Returns:
        DataFrame with the columns EIA returns (period, subba, subba-name, parent, value, ...)
    """
    frames = []
    offset = 0

    while True:
        params = {
            "api_key": API_KEY,
            "frequency": "hourly",
            "data[0]": "value",
            "facets[parent][]": parent,
            "facets[subba][]": subba,
            "start": start,
            "end": end,
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "offset": offset,
            "length": PAGE_SIZE,
        }

        payload = _get(params)
        rows = payload.get("data", [])
        total = int(payload.get("total", 0))

        if not rows:
            break

        frames.append(pd.DataFrame(rows))
        offset += len(rows)

        if verbose:
            print(f"  fetched {offset:,} of {total:,} rows")

        if offset >= total:
            break

        time.sleep(0.3)

    if not frames:
        raise SystemExit("No rows returned. Check the subba code and date range.")

    return pd.concat(frames, ignore_index=True)


def list_subregions(parent="MISO"):
    """Codes and names of every subregion EIA reports under `parent` (checks the last 3 days)."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    payload = _get({
        "api_key": API_KEY,
        "frequency": "hourly",
        "data[0]": "value",
        "facets[parent][]": parent,
        "start": (now - timedelta(days=3)).strftime("%Y-%m-%dT%H"),
        "end": now.strftime("%Y-%m-%dT%H"),
        "length": PAGE_SIZE,
    })
    df = pd.DataFrame(payload.get("data", []))
    if df.empty:
        raise SystemExit(f"No recent rows for parent {parent}.")
    cols = [c for c in ("subba", "subba-name") if c in df.columns]
    return df[cols].drop_duplicates().sort_values("subba").reset_index(drop=True)


def update_subregion(subba, end, default_start="2019-01-01T00"):
    """Add new hours to data/raw/miso_<subba>_load.csv (or create it from default_start)."""
    out = RAW_DIR / f"miso_{subba}_load.csv"

    if out.exists():
        # dtype=str keeps "0027" from turning into the number 27
        existing = pd.read_csv(out, dtype={"subba": str, "period": str})
        last = existing["period"].max()
        print(f"[{subba}] Existing data ends at {last}. Fetching from there.")
        new = fetch_subregion_demand(subba=subba, start=last, end=end)
        combined = pd.concat([existing, new], ignore_index=True)
        combined = combined.drop_duplicates(subset=["period"], keep="last")
        combined = combined.sort_values("period").reset_index(drop=True)
    else:
        print(f"[{subba}] No existing file. Fetching from {default_start}.")
        combined = fetch_subregion_demand(subba=subba, start=default_start, end=end)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    combined.to_csv(out, index=False)
    print(f"[{subba}] Saved {len(combined):,} rows to {out.relative_to(ROOT)}")
    print(f"[{subba}] Range: {combined['period'].min()} to {combined['period'].max()}\n")


if __name__ == "__main__":
    import argparse
    from datetime import datetime, timezone

    ap = argparse.ArgumentParser(description="Update raw EIA load CSVs in data/raw/.")
    ap.add_argument("--subba", nargs="+", default=["0027"], help="subregion code(s), e.g. 0027 0001")
    ap.add_argument("--list", action="store_true", help="print MISO subregion codes and names, then exit")
    args = ap.parse_args()

    if args.list:
        print(list_subregions().to_string(index=False))
    else:
        end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        for code in args.subba:
            update_subregion(code, end)
