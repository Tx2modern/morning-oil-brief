#!/usr/bin/env python3
"""
Refresh news_data.json from Google News RSS feeds.

Fetches energy/oil/petroleum headlines from the past 48 hours,
deduplicates, categorizes, and writes news_data.json.

No API key required.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, 'news_data.json')

CUTOFF_HOURS = 72  # keep articles from last 72 hours
MAX_ITEMS = 100

# Google News RSS search queries mapped to category labels
FEEDS = [
    ('crude oil prices OPEC',             'Crude Oil & OPEC'),
    ('gasoline diesel fuel prices',        'Refined Products'),
    ('EIA petroleum inventory report',     'EIA & Inventory'),
    ('refinery operations capacity',       'Refining'),
    ('oil natural gas energy market',      'Energy Markets'),
    ('Middle East geopolitics oil supply', 'Geopolitics & Risk'),
    ('US shale production drilling',       'US Production'),
    ('LNG natural gas export import',      'LNG & Gas'),
]

GNEWS_BASE = 'https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en'


def fetch_feed(query, category):
    url = GNEWS_BASE.format(query=urllib.parse.quote(query))
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0 (compatible; morning-oil-brief/1.0)'}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            xml_bytes = r.read()
    except Exception as e:
        print(f'  FAIL {category}: {e}', file=sys.stderr)
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f'  XML parse error {category}: {e}', file=sys.stderr)
        return []

    items = []
    ns = ''
    for item in root.findall('.//item'):
        title_el   = item.find('title')
        link_el    = item.find('link')
        pubdate_el = item.find('pubDate')
        source_el  = item.find('source')
        desc_el    = item.find('description')

        if title_el is None or link_el is None:
            continue

        title = (title_el.text or '').strip()
        link  = (link_el.text or '').strip()

        # Google News embeds "Source - Title"; strip the source prefix
        source = ''
        if source_el is not None and source_el.text:
            source = source_el.text.strip()
        elif ' - ' in title:
            # fallback: last " - Source" segment
            parts = title.rsplit(' - ', 1)
            if len(parts) == 2:
                title = parts[0].strip()
                source = parts[1].strip()

        dt = None
        pub_str = ''
        if pubdate_el is not None and pubdate_el.text:
            try:
                dt = parsedate_to_datetime(pubdate_el.text.strip())
                pub_str = dt.isoformat()
            except Exception:
                pass

        description = ''
        if desc_el is not None and desc_el.text:
            # Strip HTML tags from description
            description = re.sub(r'<[^>]+>', '', desc_el.text).strip()

        items.append({
            'title':       title,
            'source':      source,
            'date':        pubdate_el.text.strip() if pubdate_el is not None else '',
            'datetime':    pub_str,
            'epoch':       dt.timestamp() if dt else 0.0,
            'link':        link,
            'description': description,
            'category':    category,
            '_dt':         dt,
        })
    return items


def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=CUTOFF_HOURS)

    all_items = []
    seen_titles = set()

    for query, category in FEEDS:
        print(f'  Fetching: {category}')
        items = fetch_feed(query, category)
        added = 0
        for item in items:
            dt = item.get('_dt')
            if dt and dt < cutoff:
                continue
            # Deduplicate by normalized title
            key = re.sub(r'\s+', ' ', item['title'].lower().strip())[:80]
            if key in seen_titles:
                continue
            seen_titles.add(key)
            all_items.append(item)
            added += 1
        print(f'    → {added} new items ({len(items)} fetched)')
        time.sleep(0.5)  # be polite

    # Sort by date descending, cap at MAX_ITEMS
    all_items.sort(key=lambda x: x.get('epoch', 0), reverse=True)
    all_items = all_items[:MAX_ITEMS]

    # Strip internal _dt field
    for item in all_items:
        item.pop('_dt', None)

    payload = {
        'generated_at': now.isoformat(),
        'cutoff_hours': CUTOFF_HOURS,
        'items': all_items,
    }

    with open(OUT_PATH, 'w') as f:
        json.dump(payload, f, indent=2)
    print(f'\nWrote {OUT_PATH} ({os.path.getsize(OUT_PATH):,} bytes), {len(all_items)} articles')


if __name__ == '__main__':
    main()
