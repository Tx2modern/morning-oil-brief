#!/usr/bin/env python3
"""
Weekly EIA Petroleum Status Report data refresher.

Fetches all 52 series stored in eia_data.json from the EIA API v2,
merges new data points, and writes the updated file.

Run before build_index.py on Thursdays (after the 10:30 AM ET WPSR release).
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_KEY = os.environ.get('EIA_API_KEY')
if not API_KEY:
    print('ERROR: EIA_API_KEY environment variable not set.', file=sys.stderr)
    sys.exit(1)

BASE = 'https://api.eia.gov/v2/seriesid/'
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, 'eia_data.json')

# A handful of series occasionally hit a transient EIA API error (e.g. 502).
# Don't fail the whole build/deploy pipeline over a small number of those.
MAX_TOLERATED_ERRORS = 3
RETRIES_PER_SERIES = 3
RETRY_BACKOFF_SECONDS = 5


def fetch_series(series_id, length=370):
    """Fetch latest `length` weekly observations for a single EIA series.

    Retries transient errors (e.g. 502s) before giving up.
    """
    url = (
        BASE + urllib.parse.quote(series_id, safe='.')
        + f'?api_key={API_KEY}&frequency=weekly&data[]=value'
        + f'&sort[0][column]=period&sort[0][direction]=desc&length={length}'
    )
    req = urllib.request.Request(url, headers={'User-Agent': 'morning-oil-brief/1.0'})

    last_err = None
    for attempt in range(RETRIES_PER_SERIES):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                doc = json.loads(r.read())
            rows = doc.get('response', {}).get('data', [])
            cleaned = []
            for row in rows:
                try:
                    cleaned.append([row['period'], float(row['value'])])
                except (KeyError, TypeError, ValueError):
                    continue
            cleaned.sort(key=lambda x: x[0])
            return cleaned
        except Exception as e:
            last_err = e
            if attempt < RETRIES_PER_SERIES - 1:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_err


def main():
    # Load existing data
    try:
        with open(DATA_FILE) as f:
            existing = json.load(f)
    except FileNotFoundError:
        existing = {'last_updated': None, 'series': {}}

    series_map = existing.get('series', {})
    if not series_map:
        print('ERROR: eia_data.json has no series entries.', file=sys.stderr)
        sys.exit(1)

    print(f'Refreshing {len(series_map)} EIA weekly series...')
    updated_count = 0
    errors = []

    for key, meta in series_map.items():
        sid = meta.get('series_id')
        if not sid:
            continue
        try:
            new_data = fetch_series(sid)
            if not new_data:
                print(f'  {key:25} {sid} — no data returned', file=sys.stderr)
                errors.append(key)
                continue

            # Merge: build a dict from existing, update with new points
            existing_data = {row[0]: row[1] for row in meta.get('data', [])}
            for row in new_data:
                existing_data[row[0]] = row[1]

            # Keep last 365 weeks sorted
            merged = sorted(existing_data.items())
            merged = merged[-365:]
            meta['data'] = [[d, v] for d, v in merged]

            latest = new_data[-1]
            print(f'  {key:25} {sid:38} latest {latest[0]} = {latest[1]}')
            updated_count += 1
            time.sleep(0.15)  # be polite to EIA API
        except Exception as e:
            print(f'  {key:25} FAIL: {e}', file=sys.stderr)
            errors.append(key)

    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    existing['last_updated'] = now_str
    existing['series'] = series_map

    with open(DATA_FILE, 'w') as f:
        json.dump(existing, f, separators=(',', ':'))
    size = os.path.getsize(DATA_FILE)
    print(f'\nWrote {DATA_FILE} ({size:,} bytes)')
    print(f'Updated {updated_count}/{len(series_map)} series, {len(errors)} errors')

    if errors:
        print(f'Failed series: {errors}', file=sys.stderr)
        if len(errors) > MAX_TOLERATED_ERRORS:
            sys.exit(1)
        print(f'Tolerating {len(errors)} failed series (<= {MAX_TOLERATED_ERRORS}); continuing.')


if __name__ == '__main__':
    main()
