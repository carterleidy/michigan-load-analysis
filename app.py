"""
Michigan–Wisconsin transmission-need dashboard.

Phase 1 (real EIA-930 data only): header, summary row, annual peak chart, methods and limits.
All calculations live in src/metrics.py; this file only lays them out.

Run from the project root:   streamlit run app.py
(The earlier load screener / what-if dashboard is kept in app_screener.py.)
"""

import plotly.graph_objects as go
import streamlit as st

from src.metrics import BASE_TEMP_F, PROCESSED, fmt_day, fmt_time, load_hourly, summary

REPORT_YEAR, START_YEAR = 2025, 2019
BLUE, INK_2, GRID = "#2a78d6", "#52514e", "rgba(128,128,128,0.18)"

st.set_page_config(page_title="MI–WI Transmission Need", layout="wide")

if not PROCESSED.exists():
    st.error("No processed data yet. In the VS Code terminal (project root) run "
             "`python src/eia.py` and then `python src/build_dataset.py`, then refresh this page.")
    st.stop()


@st.cache_data(show_spinner="Loading EIA data...")
def get_data(path: str, modified: float):
    """`modified` (the file's timestamp) refreshes the cache whenever build_dataset.py rewrites the file."""
    df = load_hourly(path)
    return df, summary(df, REPORT_YEAR, START_YEAR)


df, s = get_data(str(PROCESSED), PROCESSED.stat().st_mtime)
peaks = s["peaks"]

# ----------------------------------------------------------------------------- 1. header
st.title("Is the Michigan–Wisconsin region heading toward a transmission need?")
st.markdown(
    "**Region:** MISO Zones 2 + 7, covering most of Michigan and Wisconsin. EIA reports the two zones as one number.  \n"
    f"**Data:** EIA Form 930 hourly demand, {fmt_day(df['timestamp_utc'].min())} – "
    f"{fmt_day(df['timestamp_utc'].max())}. Times are shown in Eastern time.  \n"
    "**Real vs. sample:** every number on this page is real EIA data. Panels added later that use "
    "hand-built tables or sample values will be labeled."
)

# ----------------------------------------------------------------------------- 2. summary row
c1, c2, c3, c4 = st.columns(4)

with c1:
    st.metric(f"{REPORT_YEAR} peak", f"{s['peak_mw']:,.0f} MW")
    st.caption(f"{fmt_time(s['peak_time'])} Eastern · {s['peak_temp_f']:.0f}°F (5-city weighted avg.)")
    if s["peak_lower_bound"]:
        days = ", ".join(fmt_day(d) for d in s["missing_summer_days"])
        st.caption(f"**Lower bound:** EIA has no data for {days}. A higher peak could have happened then.")

with c2:
    st.metric(f"Peak growth {START_YEAR}–{REPORT_YEAR}", f"{s['growth']:+.2%} / yr")
    wa = s["weather_adjusted"]
    st.caption(f"Compares two single hours, so weather moves it a lot. Weather-adjusted: "
               f"{wa['growth']:+.2%} / yr." if wa else "Compares two single hours, so weather moves it a lot.")

with c3:
    st.metric(f"Load factor ({REPORT_YEAR})", f"{s['load_factor']:.0%}")
    st.caption("Average hour ÷ peak hour. Lower means a peakier load.")

with c4:
    st.metric("Tracked large loads", "Phase 2")
    st.caption("Comes from large_loads.csv (hand-built and sourced) in Phase 2.")

# ----------------------------------------------------------------------------- 3. annual peak chart
st.subheader("Annual peak hour")

latest = peaks["year"].max()
plot = peaks[peaks["full_year"] | (peaks["year"] == latest)].copy()     # drops the 5-hour stub of 2018
plot["label"] = [f"{y} (to date)" if not full else str(y) for y, full in zip(plot["year"], plot["full_year"])]
plot["text"] = [f"{mw / 1000:.1f} GW" + (" *" if lb else "")
                for mw, lb in zip(plot["peak_mw"], plot["lower_bound"])]
plot["hover"] = [
    f"<b>{r.label}</b><br>Peak: {r.peak_mw:,.0f} MW<br>{fmt_time(r.peak_time)} Eastern"
    f"<br>Temperature: {r.peak_temp_f:.0f}°F"
    + (f"<br>Lower bound: no EIA data for {', '.join(fmt_day(d) for d in r.missing_summer_days)}"
       if r.lower_bound else "")
    for r in plot.itertuples()
]

fig = go.Figure(go.Bar(
    x=plot["label"], y=plot["peak_mw"],
    text=plot["text"], textposition="outside", textfont=dict(color=INK_2),
    hovertext=plot["hover"], hoverinfo="text",
    marker=dict(
        color=BLUE,
        opacity=[1.0 if full else 0.45 for full in plot["full_year"]],
        pattern=dict(shape=["/" if lb else "" for lb in plot["lower_bound"]], fgcolor="white", size=6),
        cornerradius=4,
    ),
))
fig.update_layout(
    height=380, margin=dict(t=20, b=10, l=10, r=10), showlegend=False,
    yaxis=dict(title="MW", range=[0, plot["peak_mw"].max() * 1.15], gridcolor=GRID, tickformat=",.0f"),
    xaxis=dict(title=None),
)
st.plotly_chart(fig)
st.caption("Hatched bars with * are lower bounds: EIA is missing at least one summer day that year. "
           "The faded bar is the current year so far. Projections for 2026–2032 come in Phase 2.")

with st.expander("Annual peaks as a table"):
    table = plot.assign(
        peak_time=[fmt_time(t) for t in plot["peak_time"]],
        missing_summer_days=[", ".join(fmt_day(d) for d in days) for days in plot["missing_summer_days"]],
    )[["year", "peak_mw", "peak_time", "peak_temp_f", "hours", "missing_hours", "missing_summer_days"]]
    st.dataframe(table, hide_index=True, column_config={
        "year": st.column_config.NumberColumn("Year", format="%d"),
        "peak_mw": st.column_config.NumberColumn("Peak (MW)", format="%.0f"),
        "peak_time": "Peak hour (Eastern)",
        "peak_temp_f": st.column_config.NumberColumn("Temp at peak (°F)", format="%.0f"),
        "hours": st.column_config.NumberColumn("Hours with data", format="%d"),
        "missing_hours": st.column_config.NumberColumn("Missing hours", format="%d"),
        "missing_summer_days": "Missing summer days",
    })

# ----------------------------------------------------------------------------- 7. methods and limits
with st.expander("Methods and limits"):
    st.markdown("**Data and cleaning**")
    st.markdown(
        "- Source: EIA Form 930 hourly demand for MISO subregion 0027 (Zones 2 + 7), pulled from the EIA API v2.\n"
        "- EIA timestamps are UTC. They're converted to Eastern time before any hour-of-day or calendar-year step. "
        "Wisconsin is on Central time, so its local clock reads one hour earlier.\n"
        "- Removed before taking peaks: duplicate hours, blanks, zeros, one-hour spikes (30%+ above or below both "
        "neighboring hours) and values over 2× or under 40% of that week's median.\n"
        "- Hours EIA never reported are left missing, not filled in. A year missing a summer day is marked as a lower bound.\n"
        f"- Temperatures: Open-Meteo hourly archive, weighted across Detroit, Grand Rapids, Lansing, Milwaukee and "
        f"Green Bay. The weights are rough load shares, an approximation."
    )
    st.markdown("**Formulas**")
    st.latex(r"\text{Annual peak}_y = \max_{h \in y} \text{Load}_h")
    st.latex(rf"g = \left(\frac{{\text{{Peak}}_{{{REPORT_YEAR}}}}}{{\text{{Peak}}_{{{START_YEAR}}}}}\right)^{{1/{REPORT_YEAR - START_YEAR}}} - 1")
    st.latex(r"\text{Load factor} = \frac{\text{average hourly load}}{\text{peak hourly load}}")
    st.markdown(f"Weather-adjusted growth (cross-check), fit on every day {START_YEAR}–{REPORT_YEAR}:")
    st.latex(r"\text{DailyPeak}_d = \beta_0 + \beta_1\,\text{CDD}_d + \beta_2\,\text{HDD}_d + \beta_3\,t_d + \beta_4\,\text{Weekend}_d + \varepsilon_d")
    st.latex(r"g_{\text{weather-adj}} = \frac{\beta_3}{\overline{\text{DailyPeak}}}")
    st.markdown(f"CDD and HDD are degrees of the day's average temperature above or below {BASE_TEMP_F:.0f}°F; "
                "t is years since the start.")
    st.markdown("**Limits**")
    st.markdown(
        "- Zones 2 + 7 are reported as one number, so any Michigan vs. Wisconsin split comes only from the "
        "hand-built tables added later.\n"
        "- This is a demand-side screen, not a grid study. Real transmission studies use power-flow models "
        "and grid data that aren't public.\n"
        "- Endpoint growth compares two single hours, so a hot or mild summer in either year moves it a lot."
    )
