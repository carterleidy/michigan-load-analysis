import os
import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("EIA_API_KEY")

if not API_KEY:
    raise SystemExit("No EIA_API_KEY found. Check that .env is in the project root.")

BASE = "https://api.eia.gov/v2/electricity/rto/region-sub-ba-data"

# Which parent BAs report subregional demand?
r = requests.get(f"{BASE}/facet/parent", params={"api_key": API_KEY}, timeout=30)
r.raise_for_status()
print("Parent BAs reporting subregion demand:")
for f in r.json()["response"]["facets"]:
    print(f"   {f['id']:8} {f.get('name', '')}")

# What subregion codes exist?
r2 = requests.get(f"{BASE}/facet/subba", params={"api_key": API_KEY}, timeout=30)
r2.raise_for_status()
subs = r2.json()["response"]["facets"]

print(f"\n{len(subs)} subregions total. Likely MISO zones:")
for f in subs:
    label = f"{f['id']}: {f.get('name', '')}"
    if any(k in label.upper() for k in ["MISO", "ZONE", "000"]):
        print(f"   {label}")