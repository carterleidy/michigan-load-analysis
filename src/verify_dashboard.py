"""
Check that the dashboard setup actually happened. Read-only: changes nothing, never prints secrets.

    python src/verify_dashboard.py

Paste the full output back into the chat to have it reviewed.
"""

import hashlib
import os
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
    "app.py": "e731a99c8c90",
    "requirements.txt": "2b2055663bda",
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

# ---------------------------------------------------------------- 6. dashboard runs
section("6. Dashboard runs on the real data (headless test)")
try:
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
    errs = [str(e.value)[:200] for e in at.exception] + [str(e.value)[:200] for e in at.error]
    check("app runs with no errors", not errs, "; ".join(errs))
    if at.dataframe:
        print("  Screener table:")
        print("    " + at.dataframe[0].value.T.to_string().replace("\n", "\n    "))
    if at.metric:
        print("  What-if defaults: " + " | ".join(f"{m.label}: {m.value}" for m in at.metric))
except Exception as e:
    check("headless app test", False, f"{e.__class__.__name__}: {e}")

try:
    import requests
    r = requests.get("http://localhost:8501/_stcore/health", timeout=3)
    check("dashboard server answering at http://localhost:8501", r.ok, r.text.strip())
except Exception:
    check("dashboard server at http://localhost:8501", None, "not running right now (fine if you stopped it)")

# ---------------------------------------------------------------- 7. git safety
section("7. Git: secrets protected")
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
