"""
Michigan–Wisconsin transmission-need dashboard.

Phase 1 (real EIA-930 data only): header, summary row, annual peak chart, seasonality, methods and limits.
All calculations live in src/metrics.py; this file only lays them out.

Run from the project root:   streamlit run app.py
(The earlier load screener / what-if dashboard is kept in app_screener.py.)
"""

import calendar

import plotly.graph_objects as go
import streamlit as st

from src.metrics import (BASE_TEMP_F, PROCESSED, SEASONS, fmt_day, fmt_hour, fmt_time, hour_month_avg,
                         load_hourly, monthly_peaks, seasonal_peaks, summary, temperature_response,
                         typical_day)

REPORT_YEAR, START_YEAR = 2025, 2019
BLUE, ORANGE, INK_2, GRID = "#2a78d6", "#eb6834", "#52514e", "rgba(128,128,128,0.18)"
SEASON_COLORS = {"Summer": "#2a78d6", "Fall": "#eb6834", "Winter": "#1baf7a", "Spring": "#eda100"}
SEQ_BLUE = [[0.0, "#cde2fb"], [0.25, "#86b6ef"], [0.5, "#3987e5"], [0.75, "#1c5cab"], [1.0, "#0d366b"]]
HOUR_TICKS = list(range(0, 24, 3))

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
    height=380, margin=dict(t=20, b=10, l=60, r=10), showlegend=False,
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

# ----------------------------------------------------------------------------- 3b. seasonality
st.subheader("Seasonality: when is the system most stressed?")
full_years = peaks.loc[peaks["full_year"], "year"].tolist()
yr = st.selectbox("Year for the seasonal views", full_years[::-1], key="season_year")


def end_labels(fig, x, values: dict, colors: dict, y_span: float):
    """Direct labels at the right end of each line, nudged apart so they don't overlap.
    y_span is the chart's full y-range in data units; labels stay at least 7% of it apart."""
    items = sorted(values.items(), key=lambda kv: kv[1])
    min_gap, placed = y_span * 0.07, []
    for name, y in items:
        y = max(y, placed[-1] + min_gap) if placed else y
        placed.append(y)
        fig.add_annotation(x=x, y=y, text=name, showarrow=False, xanchor="left", xshift=6,
                           font=dict(color=colors[name], size=12))


left, right = st.columns(2)

with left:
    hm = hour_month_avg(df, yr)
    fig_hm = go.Figure(go.Heatmap(
        z=hm.values, x=list(hm.columns), y=[calendar.month_abbr[m] for m in hm.index],
        colorscale=SEQ_BLUE, xgap=1, ygap=1, colorbar=dict(title="MW", thickness=12),
        customdata=[[fmt_hour(h) for h in hm.columns]] * len(hm.index),
        hovertemplate="%{y}, %{customdata}<br>Average load: %{z:,.0f} MW<extra></extra>",
    ))
    fig_hm.update_layout(
        title=f"Average load by hour and month, {yr}", height=400, margin=dict(t=50, b=60, l=50, r=10),
        xaxis=dict(title="Hour of day (Eastern)", tickmode="array", tickvals=HOUR_TICKS,
                   ticktext=[fmt_hour(h) for h in HOUR_TICKS], showgrid=False, automargin=True),
        yaxis=dict(autorange="reversed", showgrid=False, automargin=True),
    )
    st.plotly_chart(fig_hm)
    (m_hi, h_hi), (m_lo, h_lo) = hm.stack().idxmax(), hm.stack().idxmin()
    st.caption(f"Busiest: {calendar.month_name[m_hi]} around {fmt_hour(h_hi)}, averaging {hm.loc[m_hi, h_hi]:,.0f} MW. "
               f"Lightest: {calendar.month_name[m_lo]} around {fmt_hour(h_lo)}, averaging {hm.loc[m_lo, h_lo]:,.0f} MW.")

with right:
    td = typical_day(df, yr)
    fig_td = go.Figure()
    for sn in SEASONS:
        fig_td.add_scatter(x=list(td.index), y=td[sn], name=sn, mode="lines",
                           line=dict(width=2, color=SEASON_COLORS[sn]),
                           hovertemplate=f"{sn}: %{{y:,.0f}} MW<extra></extra>")
    end_labels(fig_td, td.index.max(), {sn: td[sn].iloc[-1] for sn in SEASONS}, SEASON_COLORS,
               y_span=float(td.max().max() - td.min().min()))
    fig_td.update_layout(
        title=f"Typical day in each MISO season, {yr}", height=400, hovermode="x unified",
        margin=dict(t=50, b=10, l=60, r=70), legend=dict(orientation="h", y=-0.22),
        xaxis=dict(title="Hour of day (Eastern)", tickvals=HOUR_TICKS, ticktext=[fmt_hour(h) for h in HOUR_TICKS]),
        yaxis=dict(title="Average MW", tickformat=",.0f", gridcolor=GRID),
    )
    st.plotly_chart(fig_td)
    st.caption(f"Summer days peak around {fmt_hour(td['Summer'].idxmax())} at {td['Summer'].max():,.0f} MW on average; "
               f"winter days peak around {fmt_hour(td['Winter'].idxmax())} at {td['Winter'].max():,.0f} MW. "
               f"A typical spring night bottoms out at {td['Spring'].min():,.0f} MW, "
               f"{1 - td['Spring'].min() / td['Summer'].max():.0%} below the typical summer afternoon.")

left2, right2 = st.columns(2)

with left2:
    mp = monthly_peaks(df)
    others = [y for y in full_years if y != yr]
    months = [calendar.month_abbr[m] for m in mp.index]
    fig_mp = go.Figure()
    if others:
        fig_mp.add_scatter(x=months, y=mp[others].max(axis=1), mode="lines", line=dict(width=0),
                           hoverinfo="skip", showlegend=False)
        fig_mp.add_scatter(x=months, y=mp[others].min(axis=1), mode="lines", line=dict(width=0),
                           fill="tonexty", fillcolor="rgba(128,128,128,0.22)",
                           name=f"Range of other full years ({min(others)}–{max(others)})", hoverinfo="skip")
    fig_mp.add_scatter(x=months, y=mp[yr], name=str(yr), mode="lines+markers",
                       line=dict(width=2, color=BLUE), marker=dict(size=8),
                       hovertemplate=f"{yr} %{{x}}: %{{y:,.0f}} MW<extra></extra>")
    latest = peaks["year"].max()
    if latest not in full_years:
        fig_mp.add_scatter(x=months, y=mp[latest], name=f"{latest} to date", mode="lines+markers",
                           line=dict(width=2, color=ORANGE, dash="dot"), marker=dict(size=8),
                           hovertemplate=f"{latest} %{{x}}: %{{y:,.0f}} MW<extra></extra>")
    fig_mp.update_layout(
        title="Monthly peak hour", height=400, margin=dict(t=50, b=10, l=60, r=10),
        legend=dict(orientation="h", y=-0.15), yaxis=dict(title="MW", tickformat=",.0f", gridcolor=GRID),
    )
    st.plotly_chart(fig_mp)
    sp = seasonal_peaks(df, yr)
    st.caption(f"In {yr}, the summer peak ({sp.loc['Summer', 'load_mw']:,.0f} MW, {fmt_time(sp.loc['Summer', 'ts_local'])}) "
               f"was {sp.loc['Summer', 'load_mw'] / sp.loc['Winter', 'load_mw']:.2f}× the winter peak "
               f"({sp.loc['Winter', 'load_mw']:,.0f} MW, {fmt_time(sp.loc['Winter', 'ts_local'])}).")

with right2:
    tr = temperature_response(df, yr)
    if tr is None:
        st.info("Load vs. temperature needs temperatures for most of the year. Rerun src/build_dataset.py.")
    else:
        dd = tr["daily"]
        fig_sc = go.Figure()
        fig_sc.add_scatter(x=dd["temp"], y=dd["peak"], mode="markers", name="Each day",
                           marker=dict(size=7, color=BLUE, opacity=0.6, line=dict(width=1, color="white")),
                           customdata=[fmt_day(d) for d in dd["date"]],
                           hovertemplate="%{customdata}<br>Average temp: %{x:.0f}°F<br>Daily peak: %{y:,.0f} MW<extra></extra>")
        fig_sc.add_scatter(x=tr["curve_temp"], y=tr["curve_mw"], mode="lines", name="Fitted (weekdays)",
                           line=dict(width=2, color=ORANGE), hoverinfo="skip")
        fig_sc.update_layout(
            title=f"Daily peak vs. temperature, {yr}", height=400, margin=dict(t=50, b=10, l=60, r=10),
            legend=dict(orientation="h", y=-0.22),
            xaxis=dict(title="Day's average temperature (°F, 5-city weighted)", gridcolor=GRID),
            yaxis=dict(title="Daily peak (MW)", tickformat=",.0f", gridcolor=GRID),
        )
        st.plotly_chart(fig_sc)
        hot, cold = tr["mw_per_f_hot"], tr["mw_per_f_cold"]
        st.caption(f"Above {BASE_TEMP_F:.0f}°F, each extra degree adds about {hot:,.0f} MW to the daily peak. "
                   f"Below {BASE_TEMP_F:.0f}°F, each degree colder adds about {cold:,.0f} MW."
                   + (" Cooling drives the peaks, which is why they come in summer." if hot > cold else ""))

st.caption("**Why seasonality matters here:** planners study summer-peak and winter-peak cases separately, and MISO "
           "prices capacity by season. A data center draws about the same MW every hour, so it takes up the most "
           f"slack in the lightest cells of the heatmap ({calendar.month_name[m_lo]} around {fmt_hour(h_lo)} in {yr}), "
           "not just at the summer peak.")

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
    st.markdown("**Seasonality**")
    st.markdown(
        "- Seasons follow MISO's capacity auction: Summer (Jun–Aug), Fall (Sep–Nov), Winter (Dec–Feb), "
        "Spring (Mar–May), using the selected calendar year's months.\n"
        "- Heatmap and typical-day charts average every hour with data; the monthly chart shows each month's single highest hour.\n"
        "- Load vs. temperature fits one year of daily peaks:"
    )
    st.latex(r"\text{DailyPeak}_d = a + b_{\text{cool}}\,\text{CDD}_d + b_{\text{heat}}\,\text{HDD}_d + c\,\text{Weekend}_d")
    st.markdown(r"$b_{\text{cool}}$ and $b_{\text{heat}}$ are the MW added per degree above or below 65°F.")
    st.markdown("**Limits**")
    st.markdown(
        "- Zones 2 + 7 are reported as one number, so any Michigan vs. Wisconsin split comes only from the "
        "hand-built tables added later.\n"
        "- This is a demand-side screen, not a grid study. Real transmission studies use power-flow models "
        "and grid data that aren't public.\n"
        "- Endpoint growth compares two single hours, so a hot or mild summer in either year moves it a lot."
    )
