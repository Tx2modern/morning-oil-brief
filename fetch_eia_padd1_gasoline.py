#!/usr/bin/env python3
"""
Test routine: Pull PADD 1 gasoline inventories from EIA API v2.
Endpoint: petroleum/stoc/wstk (Weekly Petroleum Supply Report - Stocks)
"""

import json
import urllib.request
import urllib.parse
from datetime import datetime

EIA_API_KEY = "gqRJqWOOf5Gf7178xVHyjNMsComMl2yRcxlgdgwI"

# EIA API v2 base URL
BASE_URL = "https://api.eia.gov/v2/petroleum/stoc/wstk/data/"

params = {
    "api_key": EIA_API_KEY,
    "frequency": "weekly",
    "data[0]": "value",
    "facets[product][]": "EPM0",       # Total Gasoline
    "facets[duoarea][]": "R10",         # PADD 1
    "facets[process][]": "SAE",         # Ending Stocks
    "sort[0][column]": "period",
    "sort[0][direction]": "desc",
    "length": 10,                        # last 10 weeks
    "offset": 0,
}

url = BASE_URL + "?" + urllib.parse.urlencode(params, doseq=True)

print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC] Fetching PADD 1 gasoline inventories...")
print(f"URL: {url}\n")

try:
    with urllib.request.urlopen(url, timeout=15) as resp:
        raw = resp.read()
        data = json.loads(raw)

    records = data.get("response", {}).get("data", [])
    total = data.get("response", {}).get("total", 0)

    if not records:
        print("No data returned. Check facet codes or API key.")
    else:
        print(f"Success — {total} total records available, showing latest {len(records)}:\n")
        print(f"{'Period':<14} {'Area':<8} {'Product':<10} {'Value (Mbbl)':<15} {'Units'}")
        print("-" * 65)
        for r in records:
            period   = r.get("period", "")
            duoarea  = r.get("duoarea", "")
            product  = r.get("product", "")
            value    = r.get("value", "")
            units    = r.get("value-units", "")
            print(f"{period:<14} {duoarea:<8} {product:<10} {str(value):<15} {units}")

    print("\nRaw response snippet (first record):")
    print(json.dumps(records[0] if records else {}, indent=2))

except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"HTTP {e.code} error: {e.reason}")
    print(f"Response body: {body}")
except urllib.error.URLError as e:
    print(f"Network error: {e.reason}")
except Exception as e:
    print(f"Unexpected error: {e}")
