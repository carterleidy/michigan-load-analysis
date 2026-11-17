"""
Phase 1 calculations for the Michigan–Wisconsin transmission-need dashboard.

Plain pandas, no Streamlit, so every number on the dashboard can be recomputed and explained
on its own (src/verify_dashboard.py does exactly that).

Input: data/processed/load_hourly.parquet, written by src/build_dataset.py
       (hourly EIA-930 demand for MISO Zones 2+7, already de-duplicated and spike-filtered).
"""

import calendar
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed" / "load_hourly.parquet"

# EIA timestamps are UTC. Hour-of-day and calendar years use Eastern time: most of the
# zone's load is in Michigan. Wisconsin (Zone 2) runs one hour behind on Central time.
LOCAL_TZ = "America/Detroit"
SUMMER = {6, 7, 8}
BASE_TEMP_F = 65.0          # degree-day base temperature


def load_hourly(path: Path = PROCESSED, subregion_code: str = "0027") -> pd.DataFrame:
    """Read the cleaned hourly file for one subregion and add local-time columns."""
    df = pd.read_parquet(path)
    df = df[df["subregion_code"].astype(str) == subregion_code].copy()
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    df["ts_local"] = df["timestamp_utc"].dt.tz_convert(LOCAL_TZ)
    df["year"] = df["ts_local"].dt.year
    return df.sort_values("timestamp_utc").reset_index(drop=True)


def missing_hours(df: pd.DataFrame) -> pd.DatetimeIndex:
    """Every hour between the first and last timestamp that has no data (UTC)."""
    have = pd.DatetimeIndex(df["timestamp_utc"]).as_unit("ns")
    full = pd.date_range(have.min(), have.max(), freq="h").as_unit("ns")
    return full.difference(have)


def annual_peaks(df: pd.DataFrame) -> pd.DataFrame:
    """One row per local calendar year.

    peak_mw              highest hourly load that year
    peak_time            when it happened (local time)
    full_year            True if every hour of the year is either present or a known gap
                         (False for the year the data starts in, and for the current year)
    missing_summer_days  June-August days with 12+ missing hours. A peak on one of those days
                         isn't in the data, so that year's peak is a lower bound.
    """
    gaps_local = missing_hours(df).tz_convert(LOCAL_TZ)
    rows = []
    for year, g in df.groupby("year"):
        i = g["load_mw"].idxmax()
        gaps_y = gaps_local[gaps_local.year == year]
        per_day = pd.Series(1, index=gaps_y).groupby(gaps_y.date).sum()
        summer_days = [d for d, n in per_day.items() if n >= 12 and d.month in SUMMER]
        hours_in_year = 8784 if calendar.isleap(year) else 8760
        rows.append({
            "year": int(year),
            "peak_mw": float(g.loc[i, "load_mw"]),
            "peak_time": g.loc[i, "ts_local"],
            "peak_temp_f": float(g.loc[i, "temp_f"]) if pd.notna(g.loc[i, "temp_f"]) else np.nan,
            "hours": len(g),
            "missing_hours": len(gaps_y),
            "full_year": len(g) + len(gaps_y) == hours_in_year,
            "missing_summer_days": summer_days,
            "lower_bound": bool(summer_days),
        })
    return pd.DataFrame(rows)


def growth_rate(peaks: pd.DataFrame, start: int = 2019, end: int = 2025) -> float:
    """Compound annual growth of the annual peak: (Peak_end / Peak_start)^(1/(end-start)) - 1."""
    p = peaks.set_index("year")["peak_mw"]
    return (p[end] / p[start]) ** (1 / (end - start)) - 1


def load_factor(df: pd.DataFrame, year: int) -> float:
    """Average hourly load / peak hourly load for one year (hours present in the data)."""
    g = df.loc[df["year"] == year, "load_mw"]
    return g.mean() / g.max()


def weather_adjusted_growth(df: pd.DataFrame, start: int = 2019, end: int = 2025) -> dict | None:
    """Growth with weather held constant (a cross-check on the endpoint growth rate).

    Fits  daily peak = b0 + b1*CDD + b2*HDD + b3*years + b4*weekend  on every day from start to end,
    where CDD/HDD are degrees of daily-mean temperature above/below 65°F. b3 is the yearly trend in MW;
    divided by the average daily peak it becomes %/yr.
    """
    g = df[(df["year"] >= start) & (df["year"] <= end)]
    if g["temp_f"].notna().sum() < 24 * 365:
        return None
    d = (g.assign(date=g["ts_local"].dt.date)
           .groupby("date")
           .agg(peak=("load_mw", "max"), temp=("temp_f", "mean"), n=("load_mw", "size"))
           .query("n >= 20")
           .dropna())
    dates = pd.to_datetime(pd.Series(d.index))
    t_years = ((dates - dates.iloc[0]).dt.days / 365.25).to_numpy()
    X = np.column_stack([
        np.ones(len(d)),
        np.clip(d["temp"].to_numpy() - BASE_TEMP_F, 0, None),
        np.clip(BASE_TEMP_F - d["temp"].to_numpy(), 0, None),
        t_years,
        (dates.dt.dayofweek >= 5).astype(float).to_numpy(),
    ])
    beta, *_ = np.linalg.lstsq(X, d["peak"].to_numpy(), rcond=None)
    return {"growth": beta[3] / d["peak"].mean(), "mw_per_cdd": beta[1], "days": len(d)}


def summary(df: pd.DataFrame, year: int = 2025, start: int = 2019) -> dict:
    """Everything the summary row needs, in one place."""
    peaks = annual_peaks(df)
    row = peaks.set_index("year").loc[year]
    return {
        "year": year,
        "peak_mw": row["peak_mw"],
        "peak_time": row["peak_time"],
        "peak_temp_f": row["peak_temp_f"],
        "peak_lower_bound": row["lower_bound"],
        "missing_summer_days": row["missing_summer_days"],
        "growth": growth_rate(peaks, start, year),
        "weather_adjusted": weather_adjusted_growth(df, start, year),
        "load_factor": load_factor(df, year),
        "peaks": peaks,
    }


def fmt_time(t: pd.Timestamp) -> str:
    """'Jul 14, 2026, 6 PM' (works on Windows, unlike %-I)."""
    return f"{t:%b} {t.day}, {t.year}, {t.hour % 12 or 12} {'AM' if t.hour < 12 else 'PM'}"


def fmt_day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"
