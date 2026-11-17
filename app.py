"""
MISO load screener + large-load what-if (Streamlit)

Reads data/processed/load_hourly.parquet (written by src/build_dataset.py), one row per subregion-hour:
    timestamp_utc  datetime in UTC
    subregion      str    display name, e.g. "MISO Zones 2+7"
    load_mw        float
    temp_f         float  optional; load-weighted hourly temperature for that subregion

Run locally (from the project root):
    python src/eia.py
    python src/build_dataset.py
    streamlit run app.py
"""

from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DATA_PATH = Path(__file__).parent / "data" / "processed" / "load_hourly.parquet"

# Local clock time drives "peak hour" and seasons. MISO spans Central and Eastern time.
DEFAULT_TZ = "America/Chicago"
SUBREGION_TZ: dict[str, str] = {
    "MISO Zones 2+7": "America/Detroit",   # most of this zone's load is in Lower Michigan
}

BASE_TEMP_F = 65.0                 # degree-day base temperature
SUMMER, WINTER = {6, 7, 8}, {12, 1, 2}
TOP_N = 100                        # "top hours" used for peak timing
MIN_HOURS_FULL_YEAR = 8_000        # a year with fewer hours than this is treated as partial

st.set_page_config(page_title="MISO Load Screener", layout="wide")


# --------------------------------------------------------------------------- data
@st.cache_data(show_spinner="Loading data...")
def load_data(path: str, modified: float) -> pd.DataFrame:
    """`modified` (file timestamp) makes the cache refresh whenever build_dataset.py rewrites the file."""
    df = pd.read_parquet(path)

    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    df["subregion"] = df["subregion"].astype(str)
    if "temp_f" not in df.columns:
        df["temp_f"] = np.nan
    df = (df.dropna(subset=["load_mw"])
            .drop_duplicates(["subregion", "timestamp_utc"]))

    parts = []
    for sr, g in df.groupby("subregion", sort=True):
        tz = SUBREGION_TZ.get(sr, DEFAULT_TZ)
        g = g.copy()
        g["ts_local"] = g["timestamp_utc"].dt.tz_convert(tz).dt.tz_localize(None)
        parts.append(g)
    df = pd.concat(parts, ignore_index=True)

    df["year"] = df["ts_local"].dt.year
    df["month"] = df["ts_local"].dt.month
    df["hour"] = df["ts_local"].dt.hour
    df["date"] = df["ts_local"].dt.normalize()
    return df.sort_values(["subregion", "ts_local"]).reset_index(drop=True)


# --------------------------------------------------------------------------- metrics
def full_years(g: pd.DataFrame) -> list[int]:
    counts = g.groupby("year").size()
    return sorted(counts[counts >= MIN_HOURS_FULL_YEAR].index.tolist())


def weather_fit(g: pd.DataFrame) -> dict | None:
    """Regress daily peak load on cooling/heating degrees, a linear time trend and a weekend flag.

    The trend coefficient is growth with weather held constant -- the
    "weather-normalized" number utilities report.
    """
    if g["temp_f"].notna().sum() < 24 * 365:
        return None
    d = (g.groupby("date")
           .agg(peak=("load_mw", "max"), temp=("temp_f", "mean"), n=("load_mw", "size"))
           .query("n >= 20")
           .dropna())
    if len(d) < 365:
        return None
    t_years = (d.index - d.index[0]).days.to_numpy() / 365.25
    X = np.column_stack([
        np.ones(len(d)),
        np.clip(d["temp"].to_numpy() - BASE_TEMP_F, 0, None),   # cooling degrees
        np.clip(BASE_TEMP_F - d["temp"].to_numpy(), 0, None),   # heating degrees
        t_years,
        (d.index.dayofweek >= 5).astype(float),
    ])
    beta, *_ = np.linalg.lstsq(X, d["peak"].to_numpy(), rcond=None)
    return {
        "growth_pct": 100 * beta[3] / d["peak"].mean(),
        "mw_per_cdd": beta[1],
        "mw_per_hdd": beta[2],
    }


@st.cache_data(show_spinner="Building screener...")
def build_screener(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for sr, g in df.groupby("subregion"):
        yrs = full_years(g)
        if not yrs:
            continue
        last = g[g["year"] == yrs[-1]]
        peak = last["load_mw"].max()
        top = last.nlargest(TOP_N, "load_mw")
        summer_pk = last.loc[last["month"].isin(SUMMER), "load_mw"].max()
        winter_pk = last.loc[last["month"].isin(WINTER), "load_mw"].max()

        annual_peaks = g[g["year"].isin(yrs)].groupby("year")["load_mw"].max()
        span = yrs[-1] - yrs[0]
        raw_growth = (100 * ((annual_peaks.iloc[-1] / annual_peaks.iloc[0]) ** (1 / span) - 1)
                      if span > 0 else np.nan)
        fit = weather_fit(g)

        rows.append({
            "Subregion": sr,
            "Year": yrs[-1],
            "Peak (MW)": peak,
            "Load factor": last["load_mw"].mean() / peak,
            "Typical peak hour": int(top["hour"].mode().iloc[0]),
            f"% of top {TOP_N} hrs in summer": 100 * top["month"].isin(SUMMER).mean(),
            "Summer/winter peak": summer_pk / winter_pk,
            "Raw peak growth (%/yr)": raw_growth,
            "Weather-adj. growth (%/yr)": fit["growth_pct"] if fit else np.nan,
            "MW per °F (hot days)": fit["mw_per_cdd"] if fit else np.nan,
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values("Weather-adj. growth (%/yr)", ascending=False, na_position="last")


# --------------------------------------------------------------------------- layout
if not DATA_PATH.exists():
    st.title("MISO Load Screener")
    st.error("No processed data yet. In the VS Code terminal (project root), run "
             "`python src/eia.py` and then `python src/build_dataset.py`, then refresh this page.")
    st.stop()

df = load_data(str(DATA_PATH), DATA_PATH.stat().st_mtime)

with st.sidebar:
    st.header("Data")
    st.caption("Source: EIA Form 930, hourly subregion demand")
    temps = df.groupby("subregion")["temp_f"].apply(lambda s: s.notna().any())
    if not temps.any():
        st.info("No temperatures loaded, so weather-adjusted columns are blank. "
                "Check the weather stations in src/build_dataset.py and rerun it.")
    st.caption(f"{df['subregion'].nunique()} subregions · "
               f"{df['ts_local'].min():%b %Y} – {df['ts_local'].max():%b %Y} · "
               f"{len(df):,} rows")

st.title("MISO Load Screener")
screener = build_screener(df)
if screener.empty:
    st.error(f"No subregion has a full year of data (≥ {MIN_HOURS_FULL_YEAR:,} hours).")
    st.stop()

tab_screen, tab_whatif = st.tabs(["Screener", "Large-load what-if"])

# ----- Screener ------------------------------------------------------------
with tab_screen:
    st.subheader("Where is load growing, and when does it peak?")
    st.caption("Click a column header to sort. Metrics use the latest full calendar year "
               "unless marked as growth.")
    st.dataframe(
        screener,
        hide_index=True,
        column_config={
            "Year": st.column_config.NumberColumn(format="%d"),
            "Peak (MW)": st.column_config.NumberColumn(format="%.0f"),
            "Load factor": st.column_config.NumberColumn(format="%.2f"),
            "Typical peak hour": st.column_config.NumberColumn(format="%d:00"),
            f"% of top {TOP_N} hrs in summer": st.column_config.NumberColumn(format="%.0f%%"),
            "Summer/winter peak": st.column_config.NumberColumn(format="%.2f"),
            "Raw peak growth (%/yr)": st.column_config.NumberColumn(format="%.2f"),
            "Weather-adj. growth (%/yr)": st.column_config.NumberColumn(format="%.2f"),
            "MW per °F (hot days)": st.column_config.NumberColumn(format="%.0f"),
        },
    )

    fig = go.Figure()
    fig.add_bar(x=screener["Subregion"], y=screener["Raw peak growth (%/yr)"], name="Raw annual peak")
    fig.add_bar(x=screener["Subregion"], y=screener["Weather-adj. growth (%/yr)"], name="Weather-adjusted")
    fig.update_layout(barmode="group", height=340, margin=dict(t=40, b=10),
                      title="Raw vs. weather-adjusted growth (%/yr)", yaxis_title="%/yr")
    st.plotly_chart(fig)
    st.caption("A gap between the two bars means weather, not underlying demand, "
               "is driving the raw number.")

    with st.expander("How these are calculated"):
        st.markdown(f"""
- **Load factor**: average load ÷ peak load. Higher means a flatter, more 'baseload' profile.
- **Typical peak hour**: most common local hour among the year's top {TOP_N} load hours.
- **Summer/winter peak**: highest summer-month hour ÷ highest winter-month hour. Above 1 means summer-peaking.
- **Raw peak growth**: compound annual change in the annual peak hour from first to last full year. Weather can make this swing a lot.
- **Weather-adj. growth**: daily peak load regressed on cooling degrees, heating degrees (base {BASE_TEMP_F:.0f}°F),
  a weekend flag and a time trend. The trend, as a % of the average daily peak, is growth with weather held constant.
- **MW per °F (hot days)**: how much daily peak rises per degree above {BASE_TEMP_F:.0f}°F.
""")

# ----- Large-load what-if --------------------------------------------------
with tab_whatif:
    st.subheader("What happens if a data center shows up?")
    left, right = st.columns([1, 3])

    with left:
        sr = st.selectbox("Subregion", screener["Subregion"].tolist())
        g = df[df["subregion"] == sr]
        yr = st.selectbox("Base year", full_years(g)[::-1])
        add_mw = st.slider("New load, contracted (MW)", 0, 5_000, 1_000, step=100)
        util = st.slider("Average draw (% of contracted)", 50, 100, 85, step=5) / 100
        has_temp = bool(g["temp_f"].notna().any())
        cool = st.slider(
            "Share that tracks temperature (%)", 0, 40, 0, step=5, disabled=not has_temp,
            help="Data center cooling load rises on hot days. This share of the new load "
                 "follows cooling degrees instead of staying flat, so more of it lands on the peak.",
        ) / 100
        st.caption("For scale: DTE's 1.4 GW data center contract raises its electric load "
                   "about 25% (Utility Dive).")

    yr_df = g[g["year"] == yr]
    base = yr_df.set_index("ts_local")["load_mw"]
    avg_add = add_mw * util

    if cool > 0 and has_temp:
        cdd = np.nan_to_num(np.clip(yr_df["temp_f"].to_numpy() - BASE_TEMP_F, 0, None))
        weight = cdd / cdd.mean() if cdd.mean() > 0 else np.ones(len(base))
    else:
        weight = np.ones(len(base))
    added = avg_add * (1 - cool) + avg_add * cool * weight
    added = np.minimum(added, add_mw)          # can't draw more than contracted capacity
    new = base + added

    old_peak, new_peak = base.max(), new.max()
    hrs_above = int((new > old_peak).sum())

    with right:
        m = st.columns(5)
        m[0].metric("Base peak", f"{old_peak:,.0f} MW")
        m[1].metric("New peak", f"{new_peak:,.0f} MW", f"{100 * (new_peak / old_peak - 1):+.1f}%")
        m[2].metric("Annual energy", f"{new.sum() / 1e6:,.1f} TWh",
                    f"{100 * (new.sum() / base.sum() - 1):+.1f}%")
        m[3].metric("Hours above old record", f"{hrs_above:,}")
        m[4].metric("Load factor", f"{new.mean() / new_peak:.2f}",
                    f"{new.mean() / new_peak - base.mean() / old_peak:+.2f}")

        pct = np.arange(1, len(base) + 1) / len(base) * 100
        ldc = go.Figure()
        ldc.add_scatter(x=pct, y=np.sort(base.to_numpy())[::-1], name=f"{yr} actual", mode="lines")
        ldc.add_scatter(x=pct, y=np.sort(new.to_numpy())[::-1], name="With new load", mode="lines")
        ldc.add_hline(y=old_peak, line_dash="dot", annotation_text="Old record peak")
        ldc.update_layout(title="Load duration curve: every hour of the year, highest to lowest",
                          xaxis_title="% of hours in year", yaxis_title="MW",
                          height=360, margin=dict(t=50, b=10))
        st.plotly_chart(ldc)

        pk = base.idxmax()
        win = (base.index >= pk - pd.Timedelta(days=3)) & (base.index <= pk + pd.Timedelta(days=3))
        wk = go.Figure()
        wk.add_scatter(x=base.index[win], y=base[win], name=f"{yr} actual", mode="lines")
        wk.add_scatter(x=new.index[win], y=new[win], name="With new load", mode="lines")
        wk.update_layout(title=f"Week around the {yr} peak ({pk:%b %d, %H:00})",
                         yaxis_title="MW", height=320, margin=dict(t=50, b=10))
        st.plotly_chart(wk)

        st.caption(
            f"Adding about {avg_add:,.0f} MW of average draw pushes **{hrs_above:,} hours** "
            f"above the {yr} record of {old_peak:,.0f} MW. Every one of those hours needs new "
            f"capacity (generation, storage or imports), not just the single peak hour."
        )
