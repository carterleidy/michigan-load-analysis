"""
Build the dashboard dataset: raw EIA load CSVs + hourly temperatures -> one clean Parquet file.

    python src/eia.py              # 1) update raw load in data/raw/
    python src/build_dataset.py    # 2) clean + add temperatures -> data/processed/load_hourly.parquet
    streamlit run app.py           # 3) open the dashboard
"""

import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
OUT = ROOT / "data" / "processed" / "load_hourly.parquet"
WEATHER_URL = "https://archive-api.open-meteo.com/v1/archive"   # free, no API key

# Display name + weather stations for each subregion code.
# Station weights are rough shares of the zone's load -- an approximation you can refine.
# Subregions not listed here still load, just without temperatures.
SUBREGIONS = {
    "0027": {
        "label": "MISO Zones 2+7",       # Lower Michigan + eastern Wisconsin / Upper Peninsula
        "stations": {                    # name: (lat, lon, weight)
            "Detroit":      (42.33, -83.05, 0.35),
            "Grand Rapids": (42.96, -85.67, 0.15),
            "Lansing":      (42.73, -84.55, 0.10),
            "Milwaukee":    (43.04, -87.91, 0.25),
            "Green Bay":    (44.51, -88.01, 0.15),
        },
    },
}

# EIA-930 has occasional bad hours. Two conservative checks so real heat-wave peaks are never dropped:
#  1) one-hour blips: an hour 30%+ above BOTH neighbors (or 30%+ below both). Real load ramps
#     smoothly hour to hour, so a genuine peak always has a high neighbor.
#  2) wild values: more than 2x, or under 40% of, the surrounding week's median.
BLIP = 0.30
WEEK_HIGH, WEEK_LOW = 2.0, 0.4


def load_raw(path: Path) -> pd.DataFrame:
    """Read one data/raw/miso_<code>_load.csv and clean it."""
    code = path.stem.removeprefix("miso_").removesuffix("_load")
    raw = pd.read_csv(path, dtype={"period": str, "subba": str})
    label = SUBREGIONS.get(code, {}).get("label")
    if not label:
        label = str(raw["subba-name"].dropna().iloc[0]) if "subba-name" in raw.columns else code

    df = pd.DataFrame({
        "timestamp_utc": pd.to_datetime(raw["period"], format="%Y-%m-%dT%H", utc=True),
        "subregion_code": code,
        "subregion": label,
        "load_mw": pd.to_numeric(raw["value"], errors="coerce"),
    })

    n_raw = len(df)
    df = df.drop_duplicates("timestamp_utc", keep="last").sort_values("timestamp_utc")
    df = df[df["load_mw"] > 0]                       # drops blanks, zeros and negatives
    n_clean = len(df)

    load = df["load_mw"]
    prev, nxt = load.shift(1), load.shift(-1)
    blip = (load > (1 + BLIP) * np.fmax(prev, nxt)) | (load < (1 - BLIP) * np.fmin(prev, nxt))
    week_ratio = load / load.rolling(169, center=True, min_periods=24).median()
    wild = (week_ratio > WEEK_HIGH) | (week_ratio < WEEK_LOW)
    spikes = blip | wild
    df = df[~spikes].reset_index(drop=True)

    print(f"[{code}] {label}: kept {len(df):,} hours | dropped {n_raw - n_clean:,} blank/duplicate/"
          f"non-positive and {int(spikes.sum()):,} spike hours")
    return df


def fetch_temps(lat: float, lon: float, start: str, end: str, tries: int = 3) -> pd.Series:
    """Hourly temperature in °F (UTC index) from the Open-Meteo historical archive."""
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": start, "end_date": end,
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "timezone": "GMT",
    }
    for attempt in range(tries):
        try:
            r = requests.get(WEATHER_URL, params=params, timeout=120)
            r.raise_for_status()
            h = r.json()["hourly"]
            return pd.Series(pd.to_numeric(pd.Series(h["temperature_2m"]), errors="coerce").to_numpy(),
                             index=pd.to_datetime(h["time"], utc=True))
        except requests.RequestException:
            if attempt == tries - 1:
                raise
            time.sleep(2 ** attempt)


def zone_temperature(stations: dict, start: str, end: str) -> pd.Series:
    """Load-weighted average temperature across a zone's weather stations."""
    temps, weights = {}, {}
    for name, (lat, lon, w) in stations.items():
        print(f"    weather: {name}")
        temps[name] = fetch_temps(lat, lon, start, end)
        weights[name] = w
    t, w = pd.DataFrame(temps), pd.Series(weights)
    return t.mul(w).sum(axis=1, min_count=1) / t.notna().mul(w).sum(axis=1)


def main() -> None:
    files = sorted(RAW_DIR.glob("miso_*_load.csv"))
    if not files:
        raise SystemExit("No raw files in data/raw/. Run: python src/eia.py")

    frames = []
    for path in files:
        df = load_raw(path)
        df["temp_f"] = np.nan
        stations = SUBREGIONS.get(df["subregion_code"].iloc[0], {}).get("stations")
        if stations:
            start = df["timestamp_utc"].min().date().isoformat()
            end = min(df["timestamp_utc"].max().date(), date.today() - timedelta(days=6)).isoformat()
            try:
                temp = zone_temperature(stations, start, end)
                idx = pd.DatetimeIndex(df["timestamp_utc"]).as_unit("ns")
                df["temp_f"] = temp.set_axis(temp.index.as_unit("ns")).reindex(idx).to_numpy()
            except requests.RequestException as e:
                print(f"    weather download failed ({e}); continuing without temperatures")
        frames.append(df)

    out = pd.concat(frames, ignore_index=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT, index=False)

    summary = out.groupby(["subregion_code", "subregion"]).agg(
        first=("timestamp_utc", "min"),
        last=("timestamp_utc", "max"),
        hours=("load_mw", "size"),
        peak_mw=("load_mw", "max"),
        temp_coverage=("temp_f", lambda s: f"{s.notna().mean():.0%}"),
    )
    print("\n" + summary.to_string())
    print(f"\nSaved {len(out):,} rows to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
