"""
Check that the dashboard setup actually happened. Read-only: changes nothing, never prints secrets.

    python src/verify_dashboard.py

Paste the full output back into the chat to have it reviewed.
"""

import hashlib
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "miso_0027_load.csv"
PROCESSED = ROOT / "data" / "processed" / "load_hourly.parquet"

# Fingerprints of the files exactly as delivered (line endings normalized, so CRLF vs LF doesn't matter)
EXPECTED = {
    "src/eia.py": "86807549f1d5",
    "src/build_dataset.py": "ead094abf35d",
    "src/metrics.py": "bddca4365bb9",
    "app.py": "339f34aa3dd4",
    "app_screener.py": "e731a99c8c90",
    "requirements.txt": "2b2055663bda",
    "inputs/large_loads.csv": "6a232d69d3a2",
    "inputs/planned_transmission.csv": "720e15b029ea",
}

results = []


def check(label, ok, detail=""):
    ok = None if ok is None else bool(ok)
    results.append(ok)
    mark = "PASS" if ok is True else ("FAIL" if ok is False else "INFO")
    print(f"  [{mark}] {label}" + (f" -- {detail}" if detail else ""))


def section(title):
    print(f"\n== {title} " + "=" * max(0, 60 - len(title)))


def git(*args):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


print(f"verify_dashboard  |  {datetime.now():%Y-%m-%d %H:%M}  |  Python {sys.version.split()[0]}  |  {ROOT}")

# ---------------------------------------------------------------- 1. files
section("1. Files match what was delivered")
for rel, want in EXPECTED.items():
    p = ROOT / rel
    if not p.exists():
        check(rel, False, "missing")
        continue
    got = hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n").rstrip()).hexdigest()[:12]
    lines = p.read_text(encoding="utf-8", errors="replace").count("\n") + 1
    check(rel, got == want, f"{lines} lines, fingerprint {got}" + ("" if got == want else f" (expected {want}: file was changed)"))
check("fetch_eia.py not in project (old file, shouldn't be used)", not any(ROOT.rglob("fetch_eia.py")))

# ---------------------------------------------------------------- 2. packages
section("2. Packages installed in this Python")
check("running inside the project's .venv", ".venv" in sys.prefix, sys.prefix)
for mod in ("streamlit", "plotly", "pyarrow", "pandas", "requests", "dotenv"):
    try:
        m = __import__(mod)
        check(mod, True, getattr(m, "__version__", "installed"))
    except Exception as e:
        check(mod, False, f"not importable ({e.__class__.__name__})")

# ---------------------------------------------------------------- 3. API key (presence only)
section("3. API key (value never printed)")
env = ROOT / ".env"
check(".env exists", env.exists())
if env.exists():
    try:
        from dotenv import dotenv_values
        check("EIA_API_KEY present in .env", bool(dotenv_values(env).get("EIA_API_KEY")))
    except Exception as e:
        check("could not read .env", False, e.__class__.__name__)
    check(".env last modified", None, f"{datetime.fromtimestamp(env.stat().st_mtime):%Y-%m-%d %H:%M} (should NOT be today)")

# ---------------------------------------------------------------- 4. raw data
section("4. Raw EIA data updated (data/raw/miso_0027_load.csv)")
import pandas as pd
raw = None
try:
    raw = pd.read_csv(RAW, dtype={"period": str, "subba": str})
    last = pd.to_datetime(raw["period"].max(), format="%Y-%m-%dT%H", utc=True)
    age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    check("rows", None, f"{len(raw):,}  ({raw['period'].min()} to {raw['period'].max()} UTC)")
    check("data is current (last hour within 3 days)", age_h < 72, f"last hour is {age_h:.0f} h old")
    check("no duplicate hours", int(raw['period'].duplicated().sum()) == 0, f"{int(raw['period'].duplicated().sum())} duplicates")
    check("subba codes", None, ", ".join(sorted(raw['subba'].dropna().unique())))
    if "subba-name" in raw.columns:
        check("EIA's name for this subregion", None, ", ".join(sorted(raw["subba-name"].dropna().astype(str).unique())))
    check("file last modified", None, f"{datetime.fromtimestamp(RAW.stat().st_mtime):%Y-%m-%d %H:%M}")
except Exception as e:
    check("could not read raw CSV", False, f"{e.__class__.__name__}: {e}")

# ---------------------------------------------------------------- 5. processed data
section("5. Processed dataset (data/processed/load_hourly.parquet)")
try:
    df = pd.read_parquet(PROCESSED)
    need = {"timestamp_utc", "subregion", "load_mw", "temp_f"}
    check("expected columns", need.issubset(df.columns), ", ".join(df.columns))
    check("file last modified", None, f"{datetime.fromtimestamp(PROCESSED.stat().st_mtime):%Y-%m-%d %H:%M}")
    for sr, g in df.groupby("subregion"):
        cover = g["temp_f"].notna().mean()
        print(f"  [INFO] {sr}: {len(g):,} hours, {g['timestamp_utc'].min():%Y-%m-%d} to "
              f"{g['timestamp_utc'].max():%Y-%m-%d}, peak {g['load_mw'].max():,.0f} MW, "
              f"low {g['load_mw'].min():,.0f} MW, temperature coverage {cover:.1%}")
        check(f"{sr}: temperatures attached", cover > 0.95, f"{cover:.1%}")
        hrs = g.groupby(g["timestamp_utc"].dt.year).size()
        print("         hours per year: " + ", ".join(f"{y}: {n:,}" for y, n in hrs.items()))
    if raw is not None and "subregion_code" in df:
        dropped = len(raw) - int((df["subregion_code"] == "0027").sum())
        check("hours dropped as bad data (under 1%)", dropped < 0.01 * len(raw), f"{dropped:,} of {len(raw):,}")
except Exception as e:
    check("could not read processed file", False, f"{e.__class__.__name__}: {e}")

# ---------------------------------------------------------------- 6. phase 1 numbers, two ways
section("6. Phase 1 numbers, recomputed without src/metrics.py")
try:
    d = df[df["subregion_code"].astype(str) == "0027"].copy()
    d["local"] = pd.to_datetime(d["timestamp_utc"], utc=True).dt.tz_convert("America/Detroit")
    d["yr"] = d["local"].dt.year
    ann = d.loc[d.groupby("yr")["load_mw"].idxmax(), ["yr", "load_mw", "local", "temp_f"]].set_index("yr")
    counts = d.groupby("yr").size()
    print("  Annual peak hour (Eastern time):")
    for y, r in ann.iterrows():
        print(f"    {y}: {r.load_mw:>9,.0f} MW  at {r.local:%Y-%m-%d %H:00}  {r.temp_f:5.1f}°F  ({counts[y]:,} hours)")
    g = (ann.loc[2025, "load_mw"] / ann.loc[2019, "load_mw"]) ** (1 / 6) - 1
    lf = d.loc[d["yr"] == 2025, "load_mw"].mean() / d.loc[d["yr"] == 2025, "load_mw"].max()
    check("peak growth 2019-2025, endpoint formula", None, f"{g:+.3%} per year")
    check("load factor 2025", None, f"{lf:.3f}")

    sys.path.insert(0, str(ROOT))
    import src.metrics as m
    s = m.summary(m.load_hourly(PROCESSED), 2025, 2019)
    check("metrics.py 2025 peak matches", abs(s["peak_mw"] - ann.loc[2025, "load_mw"]) < 0.5,
          f"{s['peak_mw']:,.0f} MW at {m.fmt_time(s['peak_time'])}")
    check("metrics.py growth rate matches", abs(s["growth"] - g) < 1e-9, f"{s['growth']:+.3%}")
    check("metrics.py load factor matches", abs(s["load_factor"] - lf) < 1e-9, f"{s['load_factor']:.3f}")
    wa = s["weather_adjusted"]
    check("weather-adjusted growth (cross-check)", None,
          f"{wa['growth']:+.3%} per year over {wa['days']:,} days" if wa else "not available (no temperatures)")
    # seasonality, recomputed with plain groupbys and compared with metrics.py
    dfm = m.load_hourly(PROCESSED)
    d25 = d[d["yr"] == 2025]
    mon, hr = d25["local"].dt.month, d25["local"].dt.hour
    summer = d25.loc[mon.isin([6, 7, 8]), "load_mw"].max()
    winter = d25.loc[mon.isin([12, 1, 2]), "load_mw"].max()
    sp = m.seasonal_peaks(dfm, 2025)
    check("metrics.py seasonal peaks match (2025)",
          abs(sp.loc["Summer", "load_mw"] - summer) < 0.5 and abs(sp.loc["Winter", "load_mw"] - winter) < 0.5,
          f"summer {summer:,.0f} MW, winter {winter:,.0f} MW, ratio {summer / winter:.2f}")
    hm_indep = d25.groupby([mon, hr])["load_mw"].mean().unstack()
    hm_diff = float((m.hour_month_avg(dfm, 2025).to_numpy() - hm_indep.to_numpy()).__abs__().max())
    check("metrics.py hour-by-month averages match (2025)", hm_diff < 1e-6, f"largest difference {hm_diff:.2e} MW")
    tr = m.temperature_response(dfm, 2025)
    check("temperature response (2025)", None,
          f"+{tr['mw_per_f_hot']:,.0f} MW per °F above 65, +{tr['mw_per_f_cold']:,.0f} MW per °F below 65"
          if tr else "not available (no temperatures)")

    flagged = s["peaks"].loc[s["peaks"]["lower_bound"], ["year", "missing_summer_days"]]
    for _, r in flagged.iterrows():
        check(f"{r['year']} peak is a lower bound", None,
              "missing summer days: " + ", ".join(m.fmt_day(x) for x in r["missing_summer_days"]))
except Exception as e:
    check("recompute failed", False, f"{e.__class__.__name__}: {e}")

# ---------------------------------------------------------------- 6b. phase 2 inputs and headline
section("6b. Phase 2: hand-built tables and the headline, recomputed")
try:
    import csv
    for name in ("inputs/large_loads.csv", "inputs/planned_transmission.csv"):
        with open(ROOT / name, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        no_src = [r.get("site") or r.get("project") for r in rows if not r["source_url"].startswith("http")]
        check(f"{name}: every row has a source link", not no_src, f"{len(rows)} rows" + (f"; missing: {no_src}" if no_src else ""))
        bad_dates = [v for r in rows for k, v in r.items()
                     if k in ("online_start", "full_load", "selected", "in_service") and v and not re.fullmatch(r"\d{4}-\d{2}", v)]
        check(f"{name}: dates are blank or YYYY-MM", not bad_dates, ", ".join(bad_dates))

    with open(ROOT / "inputs/large_loads.csv", newline="", encoding="utf-8") as f:
        ll = list(csv.DictReader(f))
    not_counted = [r for r in ll if r["count_in_scenario"].strip().lower() != "yes"]
    check("rows left out of the scenario say what total covers them",
          all("We Energies" in r["note"] for r in not_counted), ", ".join(r["site"] for r in not_counted))

    # headline X with default settings, computed by hand (month counting, no metrics.py)
    def months(ym):
        y_, m_ = map(int, ym.split("-"))
        return y_ * 12 + (m_ - 1)

    peak25 = ann.loc[2025, "load_mw"]
    g_end = (ann.loc[2025, "load_mw"] / ann.loc[2019, "load_mw"]) ** (1 / 6) - 1
    totals = {}
    for year in range(2025, 2033):
        july = year * 12 + 6
        added = 0.0
        for r in ll:
            if r["count_in_scenario"].strip().lower() != "yes" or not r["online_start"]:
                continue
            on = months(r["online_start"])
            full = months(r["full_load"]) if r["full_load"] else on
            share = (1.0 if july >= on else 0.0) if full <= on else min(1.0, max(0.0, (july - on) / (full - on)))
            added += float(r["mw"]) * share
        totals[year] = (peak25 * (1 + g_end) ** (year - 2025), added)
    x_hand = totals[2030][1] / totals[2030][0]
    print("  Large-load MW at each July peak (default settings): "
          + ", ".join(f"{y}: {a:,.0f}" for y, (_, a) in totals.items()))

    loads_m = m.read_large_loads()
    sc_m = m.scenario_table(s["peak_mw"], s["growth"], loads_m, range(2025, 2033))
    x_m = m.headline_share(sc_m, 2030)
    check("headline X matches metrics.py (default settings)", abs(x_hand - x_m) < 0.002,
          f"by hand {x_hand:.1%}, metrics.py {x_m:.1%}; {totals[2030][1]:,.0f} MW on a "
          f"{totals[2030][0]:,.0f} MW trend-only 2030 peak")
    tx_m = m.read_planned_transmission()
    big = tx_m.sort_values(["voltage_kv", "in_service_dt"], ascending=[False, False]).iloc[0]
    check("biggest planned line used in the headline", None,
          f"{big['project']}: {big['voltage_kv']:.0f} kV, in service {big['in_service']}")
except Exception as e:
    check("phase 2 checks failed", False, f"{e.__class__.__name__}: {e}")

# ---------------------------------------------------------------- 7. dashboards run
section("7. Dashboards run on the real data (headless test)")
try:
    from streamlit.testing.v1 import AppTest
    for name in ("app.py", "app_screener.py"):
        if not (ROOT / name).exists():
            check(f"{name} exists", False)
            continue
        at = AppTest.from_file(str(ROOT / name), default_timeout=300).run()
        errs = [str(e.value)[:200] for e in at.exception] + [str(e.value)[:200] for e in at.error]
        check(f"{name} runs with no errors", not errs, "; ".join(errs))
        if name == "app.py" and at.metric:
            print("  Summary row: " + " | ".join(f"{x.label}: {x.value}" for x in at.metric))
except Exception as e:
    check("headless app test", False, f"{e.__class__.__name__}: {e}")

try:
    import requests
    r = requests.get("http://localhost:8501/_stcore/health", timeout=3)
    check("dashboard server answering at http://localhost:8501", r.ok, r.text.strip())
except Exception:
    check("dashboard server at http://localhost:8501", None, "not running right now (fine if you stopped it)")

# ---------------------------------------------------------------- 7. git safety
section("8. Git: secrets protected")
code, _ = git("ls-files", "--error-unmatch", ".env")
check(".env is NOT tracked by git", code != 0)
code, out = git("check-ignore", "-v", ".env")
check(".env is ignored", code == 0, out or "no ignore rule matches .env")
code, out = git("check-ignore", "-v", "data/processed/load_hourly.parquet")
check("processed parquet ignore status", None, out if code == 0 else "NOT ignored (can be committed for deployment)")
_, out = git("log", "-1", "--format=%h  %cr  %s")
check("latest commit", None, out)
_, out = git("status", "--short", "--branch")
print("  git status:\n    " + (out.replace("\n", "\n    ") if out else "(clean)"))

# ---------------------------------------------------------------- summary
fails = sum(1 for r in results if r is False)
passes = sum(1 for r in results if r is True)
print(f"\nSUMMARY: {passes} passed, {fails} failed")
