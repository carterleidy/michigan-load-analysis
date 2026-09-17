"""Fetch hourly demand from the EIA v2 API for a MISO subregion."""

import os
import time
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("EIA_API_KEY")
BASE = "https://api.eia.gov/v2/electricity/rto/region-sub-ba-data/data/"
PAGE_SIZE = 5000


def fetch_subregion_demand(subba, start, end, parent="MISO", verbose=True):
    """
    Pull hourly demand (MWh) for one BA subregion.

    Args:
        subba:  subregion code, e.g. "0027" for MISO Zones 2 and 7
        start:  "YYYY-MM-DDTHH" in UTC, e.g. "2018-07-01T00"
        end:    "YYYY-MM-DDTHH" in UTC
        parent: parent balancing authority code

    Returns:
        DataFrame with columns [period, subba, parent, value]
    """
    if not API_KEY:
        raise SystemExit("No EIA_API_KEY found. Check .env in the project root.")

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

        r = requests.get(BASE, params=params, timeout=60)
        r.raise_for_status()
        payload = r.json()["response"]

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


if __name__ == "__main__":
    import os
    from datetime import datetime, timezone

    out = "data/raw/miso_0027_load.csv"
    default_start = "2019-01-01T00"
    end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")

    if os.path.exists(out):
        existing = pd.read_csv(out)
        last = existing["period"].max()
        print(f"Existing data ends at {last}. Fetching from there.")
        new = fetch_subregion_demand(subba="0027", start=last, end=end)
        combined = pd.concat([existing, new], ignore_index=True)
        combined = combined.drop_duplicates(subset=["period"], keep="last")
        combined = combined.sort_values("period").reset_index(drop=True)
    else:
        print(f"No existing file. Fetching from {default_start}.")
        combined = fetch_subregion_demand(subba="0027", start=default_start, end=end)

    combined.to_csv(out, index=False)
    print(f"\nSaved {len(combined):,} rows to {out}")
    print(f"Range: {combined['period'].min()} to {combined['period'].max()}")