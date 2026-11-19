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


# ----------------------------------------------------------------------------- seasonality
# MISO's four capacity-auction seasons, so the charts line up with seasonal capacity prices.
SEASON_OF_MONTH = {12: "Winter", 1: "Winter", 2: "Winter", 3: "Spring", 4: "Spring", 5: "Spring",
                   6: "Summer", 7: "Summer", 8: "Summer", 9: "Fall", 10: "Fall", 11: "Fall"}
SEASONS = ["Summer", "Fall", "Winter", "Spring"]


def hour_month_avg(df: pd.DataFrame, year: int) -> pd.DataFrame:
    """Average load (MW) for each month (rows 1-12) and local hour of day (columns 0-23) in one year."""
    g = df[df["year"] == year]
    return g.pivot_table(index=g["ts_local"].dt.month, columns=g["ts_local"].dt.hour,
                         values="load_mw", aggfunc="mean")


def typical_day(df: pd.DataFrame, year: int) -> pd.DataFrame:
    """Average load by local hour of day (rows 0-23) for each MISO season (columns) in one year.
    Seasons use that calendar year's months, so 'Winter' is Jan, Feb and Dec of the same year."""
    g = df[df["year"] == year]
    season = g["ts_local"].dt.month.map(SEASON_OF_MONTH)
    hour = g["ts_local"].dt.hour
    return g.groupby([hour, season])["load_mw"].mean().unstack()[SEASONS]


def monthly_peaks(df: pd.DataFrame) -> pd.DataFrame:
    """Highest hourly load in each month: rows are months 1-12, columns are years."""
    return df.pivot_table(index=df["ts_local"].dt.month, columns="year", values="load_mw", aggfunc="max")


def seasonal_peaks(df: pd.DataFrame, year: int) -> pd.DataFrame:
    """Peak MW and when it happened, for each MISO season in one year."""
    g = df[df["year"] == year].assign(season=lambda x: x["ts_local"].dt.month.map(SEASON_OF_MONTH))
    idx = g.groupby("season")["load_mw"].idxmax()
    out = g.loc[idx, ["season", "load_mw", "ts_local", "temp_f"]].set_index("season")
    return out.reindex(SEASONS)


def temperature_response(df: pd.DataFrame, year: int) -> dict | None:
    """How daily peak load responds to temperature in one year.

    Fits  daily peak = a + b_cool*CDD + b_heat*HDD + c*weekend  using the day's average temperature
    (CDD/HDD = degrees above/below 65°F). b_cool and b_heat are MW added per degree of heat or cold.
    """
    g = df[df["year"] == year]
    d = (g.assign(date=g["ts_local"].dt.date)
           .groupby("date")
           .agg(peak=("load_mw", "max"), temp=("temp_f", "mean"), n=("load_mw", "size"))
           .query("n >= 20")
           .dropna())
    if len(d) < 300:
        return None
    dates = pd.to_datetime(pd.Series(d.index))
    weekend = (dates.dt.dayofweek >= 5).astype(float).to_numpy()
    cdd = np.clip(d["temp"].to_numpy() - BASE_TEMP_F, 0, None)
    hdd = np.clip(BASE_TEMP_F - d["temp"].to_numpy(), 0, None)
    X = np.column_stack([np.ones(len(d)), cdd, hdd, weekend])
    (a, b_cool, b_heat, c), *_ = np.linalg.lstsq(X, d["peak"].to_numpy(), rcond=None)
    grid = np.linspace(d["temp"].min(), d["temp"].max(), 120)
    fitted = a + b_cool * np.clip(grid - BASE_TEMP_F, 0, None) + b_heat * np.clip(BASE_TEMP_F - grid, 0, None)
    return {"daily": d.reset_index(), "mw_per_f_hot": b_cool, "mw_per_f_cold": b_heat,
            "weekend_mw": c, "curve_temp": grid, "curve_mw": fitted}


# ----------------------------------------------------------------------------- phase 2: large loads and scenario
INPUTS = ROOT / "inputs"
LARGE_LOADS = INPUTS / "large_loads.csv"
PLANNED_TX = INPUTS / "planned_transmission.csv"
PEAK_MONTH = 7      # this region's annual peaks fall in July, so loads are counted as of July 1 each year


def read_large_loads(path: Path = LARGE_LOADS) -> pd.DataFrame:
    """Hand-built table of announced large loads, one row per site (or per utility total).

    count_in_scenario = 'no' marks rows already inside another row (the We Energies total),
    so they're listed for context but never added twice.
    """
    ll = pd.read_csv(path, dtype=str, keep_default_na=False)
    ll["mw"] = pd.to_numeric(ll["mw"], errors="coerce")
    ll["online_dt"] = pd.to_datetime(ll["online_start"], format="%Y-%m", errors="coerce")
    ll["full_dt"] = pd.to_datetime(ll["full_load"], format="%Y-%m", errors="coerce")
    ll["counted"] = ll["count_in_scenario"].str.strip().str.lower().eq("yes")
    ll["dated"] = ll["online_dt"].notna()
    return ll


def read_planned_transmission(path: Path = PLANNED_TX) -> pd.DataFrame:
    """Hand-built table of planned transmission.

    kind = 'line' (one named line), 'portfolio' (a state's share or MISO's whole plan),
    or 'benchmark' (outside the region, used only for cost comparison).
    """
    tx = pd.read_csv(path, dtype=str, keep_default_na=False)
    for col in ("voltage_kv", "miles", "cost_usd_m"):
        tx[col] = pd.to_numeric(tx[col], errors="coerce")
    for col in ("selected", "in_service", "in_service_latest"):
        tx[f"{col}_dt"] = pd.to_datetime(tx[col], format="%Y-%m", errors="coerce")
    tx["cost_per_mile_m"] = tx["cost_usd_m"] / tx["miles"]
    return tx


def biggest_regional_line(tx: pd.DataFrame) -> pd.Series:
    """The highest-voltage named line in Michigan or Wisconsin with an in-service date
    (latest date breaks ties). The headline compares large-load timing against this line."""
    lines = tx[(tx["kind"] == "line") & tx["state"].isin(["MI", "WI"]) & tx["in_service_dt"].notna()]
    return lines.sort_values(["voltage_kv", "in_service_dt"], ascending=[False, False]).iloc[0]


STATE_RULES = INPUTS / "state_rules.csv"


def read_state_rules(path: Path = STATE_RULES) -> pd.DataFrame:
    """Hand-built table of state rules and risks: one row per state and topic, each with a source."""
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def share_online(online, full, year: int) -> float:
    """Share of a load's MW running at the summer peak (July 1) of `year`.

    Ramps in a straight line from the online date to the full-load date. With no full-load
    date it switches on all at once. A load that starts in December adds nothing to that
    year's summer peak.
    """
    if pd.isna(online):
        return 0.0
    t = pd.Timestamp(year=year, month=PEAK_MONTH, day=1)
    if pd.isna(full) or full <= online:
        return float(t >= online)
    return float(np.clip((t - online) / (full - online), 0.0, 1.0))


def scenario_table(peak_base: float, g: float, loads: pd.DataFrame, years, draw: float = 1.0,
                   undated_year: int | None = None, base_year: int = 2025) -> pd.DataFrame:
    """One row per year: trend-only peak, MW added by large loads, and trend + loads.

    trend_mw    = peak_base * (1 + g)^(year - base_year)
    added_mw    = draw * sum over counted loads of MW * share_online(...)
    Loads with no online date are added in full from `undated_year` on, or left out if it's None.
    """
    use = loads[loads["counted"]]
    rows = []
    for y in years:
        trend = peak_base * (1 + g) ** (y - base_year)
        per_load = {}
        for _, r in use.iterrows():
            if r["dated"]:
                share = share_online(r["online_dt"], r["full_dt"], y)
            else:
                share = float(undated_year is not None and y >= undated_year)
            per_load[r["site"]] = r["mw"] * share * draw
        added = sum(per_load.values())
        rows.append({"year": y, "trend_mw": trend, "added_mw": added, "scenario_mw": trend + added,
                     **{f"MW: {k}": v for k, v in per_load.items()}})
    return pd.DataFrame(rows)


def headline_share(sc: pd.DataFrame, year: int = 2030) -> float:
    """X in the headline: large-load MW at the summer peak of `year` as a share of the trend-only peak."""
    r = sc.set_index("year").loc[year]
    return r["added_mw"] / r["trend_mw"]


def fmt_time(t: pd.Timestamp) -> str:
    """'Jul 14, 2026, 6 PM' (works on Windows, unlike %-I)."""
    return f"{t:%b} {t.day}, {t.year}, {t.hour % 12 or 12} {'AM' if t.hour < 12 else 'PM'}"


def fmt_day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def fmt_hour(h: int) -> str:
    """0 -> '12 AM', 17 -> '5 PM'."""
    return f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}"
