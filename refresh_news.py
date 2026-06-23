#!/usr/bin/env python3
"""
Refresh news_data.json from Google News RSS and OilPrice.com feeds.

Fetches energy/oil/petroleum headlines from the past 72 hours,
deduplicates, categorizes, and writes news_data.json.

Optional: set OILPRICE_API_KEY to pull from OilPrice.com API.
          Falls back to their public RSS feeds if key is not set.
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

OILPRICE_API_KEY = os.environ.get('OILPRICE_API_KEY', '')

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

# OilPrice.com RSS category feeds (no API key required)
OILPRICE_RSS_FEEDS = [
    ('https://oilprice.com/rss/main',                          'Crude Oil & OPEC'),
    ('https://oilprice.com/rss/geopolitics',                   'Geopolitics & Risk'),
    ('https://oilprice.com/rss/energy_general',                'Energy Markets'),
    ('https://oilprice.com/rss/natural_gas',                   'LNG & Gas'),
]

# OilPrice.com API endpoint (paid, requires OILPRICE_API_KEY)
# Their v1 endpoint; rows/limit param may vary by plan
OILPRICE_API_URLS = [
    'https://oilprice.com/api/v1/articles?api_key={key}&rows=50',
    'https://api.oilprice.com/v1/articles?api_key={key}&rows=50',
    'https://oilprice.com/api/v1/latest-news?api_key={key}&limit=50',
]


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


def fetch_oilprice_rss(url, category):
    """Fetch a single OilPrice.com RSS feed."""
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0 (compatible; morning-oil-brief/1.0)'}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            xml_bytes = r.read()
    except Exception as e:
        print(f'  FAIL OilPrice RSS {category}: {e}', file=sys.stderr)
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f'  XML parse error OilPrice RSS {category}: {e}', file=sys.stderr)
        return []

    items = []
    for item in root.findall('.//item'):
        title_el   = item.find('title')
        link_el    = item.find('link')
        pubdate_el = item.find('pubDate')
        desc_el    = item.find('description')

        if title_el is None or link_el is None:
            continue

        title = (title_el.text or '').strip()
        link  = (link_el.text or '').strip()
        if not link:
            # RSS 2.0 sometimes puts link as text node between tags
            link = item.findtext('link') or ''

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
            description = re.sub(r'<[^>]+>', '', desc_el.text).strip()[:300]

        items.append({
            'title':       title,
            'source':      'OilPrice.com',
            'date':        pubdate_el.text.strip() if pubdate_el is not None else '',
            'datetime':    pub_str,
            'epoch':       dt.timestamp() if dt else 0.0,
            'link':        link,
            'description': description,
            'category':    category,
            '_dt':         dt,
        })
    return items


def _parse_oilprice_date(art):
    """Try every known date field name OilPrice API uses across versions."""
    for field in ('pubDate', 'published_at', 'publishedAt', 'date', 'created_at', 'pub_date'):
        raw = art.get(field, '')
        if not raw:
            continue
        try:
            # RFC 2822 (Sat, 21 Jun 2026 12:00:00 +0000)
            return parsedate_to_datetime(raw), raw
        except Exception:
            pass
        try:
            # ISO 8601 (2026-06-21T12:00:00Z or 2026-06-21 12:00:00)
            normalized = raw.replace(' ', 'T').rstrip('Z') + '+00:00'
            return datetime.fromisoformat(normalized), raw
        except Exception:
            pass
    return None, ''


def fetch_oilprice_api(api_key, category='Energy Markets'):
    """Fetch articles from OilPrice.com paid API, trying multiple known endpoint patterns."""
    data = None
    last_err = None
    for url_template in OILPRICE_API_URLS:
        url = url_template.format(key=api_key)
        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'morning-oil-brief/1.0', 'Accept': 'application/json'}
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw_bytes = r.read()
            data = json.loads(raw_bytes)
            print(f'  OilPrice API: got response from {url.split("?")[0]}')
            break
        except Exception as e:
            last_err = e
            print(f'  OilPrice API: {url.split("?")[0]} → {e}', file=sys.stderr)

    if data is None:
        print(f'  FAIL OilPrice API (all endpoints exhausted): {last_err}', file=sys.stderr)
        return []

    # Normalise response — API returns various shapes across versions:
    #   {"data": [...]}
    #   {"status":"ok", "data": [...]}
    #   {"articles": [...]}
    #   {"data": {"articles": [...]}}
    #   [...]   (bare array)
    if isinstance(data, list):
        articles = data
    elif isinstance(data, dict):
        inner = data.get('data', data.get('articles', data.get('items', [])))
        if isinstance(inner, list):
            articles = inner
        elif isinstance(inner, dict):
            articles = inner.get('articles', inner.get('items', []))
        else:
            articles = []
    else:
        articles = []

    print(f'  OilPrice API: {len(articles)} articles parsed')

    items = []
    for art in articles:
        dt, raw_date = _parse_oilprice_date(art)
        pub_str = dt.isoformat() if dt else ''

        # Link field may be 'link', 'url', or 'article_url'
        link = art.get('link') or art.get('url') or art.get('article_url') or ''
        # Category may come from the article itself
        cat = art.get('category') or art.get('type') or category
        # Summary/description field
        desc_raw = art.get('description') or art.get('summary') or art.get('excerpt') or ''

        title = (art.get('title') or '').strip()
        if not title:
            continue

        items.append({
            'title':       title,
            'source':      'OilPrice.com',
            'date':        raw_date,
            'datetime':    pub_str,
            'epoch':       dt.timestamp() if dt else 0.0,
            'link':        link,
            'description': re.sub(r'<[^>]+>', '', desc_raw).strip()[:300],
            'category':    cat,
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

    # OilPrice.com — API (preferred) or RSS fallback
    if OILPRICE_API_KEY:
        print('  Fetching: OilPrice.com API')
        op_items = fetch_oilprice_api(OILPRICE_API_KEY)
        print(f'    → {len(op_items)} articles from OilPrice API')
    else:
        print('  Fetching: OilPrice.com RSS feeds')
        op_items = []
        for rss_url, rss_cat in OILPRICE_RSS_FEEDS:
            op_items += fetch_oilprice_rss(rss_url, rss_cat)
            time.sleep(0.3)
        print(f'    → {len(op_items)} articles from OilPrice RSS')

    added_op = 0
    for item in op_items:
        dt = item.get('_dt')
        if dt and dt < cutoff:
            continue
        key = re.sub(r'\s+', ' ', item['title'].lower().strip())[:80]
        if key in seen_titles:
            continue
        seen_titles.add(key)
        all_items.append(item)
        added_op += 1
    print(f'    → {added_op} new OilPrice items after dedup/cutoff')

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
