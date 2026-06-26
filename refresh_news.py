#!/usr/bin/env python3
"""
Refresh news_data.json from OilPrice.com and Google News RSS.

Source priority:
  1. OilPrice.com RSS (always — high quality, energy-specific)
  2. OilPrice.com API (if OILPRICE_API_KEY set — adds more articles)
  3. Google News RSS (US-targeted queries with exclusions)

Filters out:
  - Indian/Asian retail fuel price articles (rupee pump prices)
  - Stock/equity analysis articles (simplywall, Motley Fool, etc.)
  - Crypto sites repurposing oil as a price hook
  - Low-quality aggregators and SEO spam
"""
import html
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

CUTOFF_HOURS = 72
MAX_ITEMS = 100

OILPRICE_API_KEY = os.environ.get('OILPRICE_API_KEY', '')

# ── OilPrice.com RSS feeds (no API key, always fetched) ──────────────────────
OILPRICE_RSS_FEEDS = [
    ('https://oilprice.com/rss/main',            'Crude Oil & OPEC'),
    ('https://oilprice.com/rss/geopolitics',     'Geopolitics & Risk'),
    ('https://oilprice.com/rss/energy_general',  'Energy Markets'),
    ('https://oilprice.com/rss/natural_gas',     'LNG & Gas'),
    ('https://oilprice.com/rss/oil_prices',      'Crude Oil & OPEC'),
    ('https://oilprice.com/rss/us_energy',       'US Production'),
]

# ── OilPrice.com API (paid, tried if key is set) ─────────────────────────────
OILPRICE_API_URLS = [
    'https://oilprice.com/api/v1/articles?api_key={key}&rows=50',
    'https://api.oilprice.com/v1/articles?api_key={key}&rows=50',
    'https://oilprice.com/api/v1/latest-news?api_key={key}&limit=50',
]

# ── Google News RSS — US-targeted with exclusions for junk ───────────────────
# Append -india -petrol to keep out Asian retail pump price floods
GNEWS_BASE = 'https://news.google.com/rss/search?q={query}+when:5d&hl=en-US&gl=US&ceid=US:en'
FEEDS = [
    # Broad queries so Google News returns enough results; allowlist handles quality filtering
    ('crude oil price Brent WTI OPEC',                       'Crude Oil & OPEC'),
    ('gasoline diesel fuel price refinery',                  'Refined Products'),
    ('EIA petroleum inventory oil stocks',                   'EIA & Inventory'),
    ('oil refinery capacity utilization',                    'Refining'),
    ('oil energy market supply demand',                      'Energy Markets'),
    ('Middle East Iran Saudi Arabia oil supply geopolitics', 'Geopolitics & Risk'),
    ('US oil shale production Permian drilling',             'US Production'),
    ('LNG natural gas export import',                        'LNG & Gas'),
]

# ── Source blocklist — known low-quality or off-topic ────────────────────────
BLOCKED_SOURCES = {
    # Indian retail pump price outlets
    'india.com', 'ndtv profit', 'news18', 'hindustan times', 'the economic times',
    'times of india', 'deccan herald', 'dt next', 'newsonair', 'the hindu',
    'financial express', 'livemint', 'business standard', 'moneycontrol',
    # Philippines / Taiwan / other Asian retail fuel
    'manila standard', 'gma network', 'philstar', 'businessmirror',
    'taiwan news', 'focus taiwan', 'cpc',
    # Crypto / fintech repurposing energy as price hook
    'cryptorank', 'bitget', 'coindesk', 'cointelegraph', 'decrypt',
    'beincrypto', 'u.today', 'ambcrypto',
    # Stock-picker / equity analysis sites
    'simplywall.st', 'simply wall st', 'the motley fool', 'motley fool',
    'discovery alert', 'seekingalpha', 'seeking alpha', 'zacks',
    'marketbeat', 'tipranks', 'gurufocus',
    # SEO aggregators / press release spam
    'globenewswire', 'prnewswire', 'businesswire', 'einpresswire',
    'accesswire', 'indexbox', 'ad hoc news',
    # Travel / tourism clickbait that references oil tangentially
    'travel and tour world', 'travelandtourworld', 'tourism review',
    'travel daily media', 'eturbonews', 'travelweekly',
    # Regional variants of otherwise-allowlisted outlets
    'cnbc tv18', 'cnbc awaaz', 'cnbc africa',
    'the guardian nigeria', 'guardian nigeria', 'the guardian ghana',
    # Low-signal aggregators and local TV
    'oilprice.net',
    'scanx.trade', 'stockanalysis.com',
    'fxempire', 'fx empire', 'fxstreet', 'investing.com nigeria',
    'quantum commodity intelligence',  # paywalled, summaries are thin
    '富途牛牛',  # Chinese retail brokerage
    # Misc low-signal
    'ipn.md', 'ایران اینترنشنال',
}

# ── Source allowlist — Google News articles from unlisted sources are dropped ──
# Only articles from these outlets (or OilPrice.com which has its own feed)
# are accepted from Google News. This keeps the feed to real journalism.
ALLOWED_GNEWS_SOURCES = {
    # Wire services
    'reuters', 'associated press', 'ap news', 'bloomberg', 'bloomberg.com', 'bnn bloomberg',
    # Financial press
    'wall street journal', 'wsj', 'financial times', 'barron\'s',
    'the new york times', 'nytimes', 'new york times',
    'washington post', 'the economist',
    # Energy trade press
    'oil & gas journal', 'ogj', 'upstream online', 'energy intelligence',
    'hart energy', 'world oil', 'platts', 's&p global', 's&p global commodity insights',
    'argus media', 'argus', 'opis', 'rbn energy', 'icis',
    'natural gas intelligence', 'lng world news', 'naturalgasworld',
    # Mainstream business / finance
    'cnbc', 'cnn business', 'bbc', 'bbc news', 'the guardian',
    'axios', 'politico', 'the hill', 'npr', 'pbs',
    'fortune', 'time magazine', 'business insider',
    'yahoo finance',  # syndicates reuters/bloomberg articles
    'marketwatch', 'seeking alpha',  # keep for price/market context
    # Shipping / tanker / freight
    'gcaptain', 'tradewinds', 'seatrade maritime', 'lloyds list',
    'hellenic shipping news',
    # Government / official
    'eia', 'u.s. energy information administration', 'iea',
    'u.s. energy information administration (eia)',
    'u.s. energy information administration (eia) (.gov)',
    # Regional energy-specific
    'oilnow', 'rigzone', 'energy monitor', 'energymonitor', 'natural gas intelligence',
    'middle east eye', 'al monitor', 'al jazeera',
    'arab news', 'the national', 'gulf news',
    # Canada / Americas energy
    'financial post', 'globe and mail', 'calgary herald',
    'oil sands magazine', 'resources magazine',
    'mining.com', 'oilweek',
    # Other recognized outlets
    'foreign policy', 'foreign affairs', 'the intercept',
    'propublica', 'bloomberg law', 'law360', 'icis',
    'visual capitalist',  # good data journalism
    'isicds', 's&p capital iq',
    # Paywalled but recognizable headline value
    'bloomberg businessweek', 'wsj pro', 'ft energy',
}

# ── Title pattern blocklist ───────────────────────────────────────────────────
BLOCKED_TITLE_PATTERNS = [
    # Indian retail pump prices
    r'\bpetrol\b.*\bprice[s]?\b.*\b(india|delhi|mumbai|chennai|kolkata|bangalore|hyderabad)\b',
    r'\bdiesel\b.*\bprice[s]?\b.*\b(india|delhi|mumbai|chennai|kolkata|bangalore|hyderabad)\b',
    r'\bfuel rate[s]?\b.*\bjune\b',
    r'\bcity.wise\b',
    r'(?:rs\.?|rupee)\s*\d+',
    # Asian retail prices
    r'\b(taiwan|philippines|manila|cpc)\b.*\b(gasoline|diesel|fuel)\b.*\bprice\b',
    # Stock analysis
    r'\bstock[s]?\b.*\b(earnings|growth|risk|balance sheet|funding)\b',
    r'\b(buy|sell|hold|analyst rating|price target|eps|dividend)\b',
    r'\bshares?\b.*\b(rise|fall|climb|drop|surge|plunge)\b.*\b(oil|energy)\b',
    # Crypto
    r'\b(bitcoin|ethereum|crypto|blockchain|token|defi)\b',
    # Generic low-value
    r'\bcheck\s+(out\s+)?(petrol|diesel|fuel|gas)\s+price[s]?\b',
    r'\btoday[\'s]?\s+(petrol|diesel|fuel|gas)\s+price[s]?\b',
]
_BLOCKED_RE = [re.compile(p, re.IGNORECASE) for p in BLOCKED_TITLE_PATTERNS]

# ── Preferred high-quality sources (score boost for ranking) ─────────────────
PREFERRED_SOURCES = {
    'reuters', 'bloomberg', 'wall street journal', 'wsj', 'financial times', 'ft',
    's&p global', 'platts', 'argus', 'opis', 'rbn energy',
    'oilprice.com', 'oil & gas journal', 'hart energy', 'world oil',
    'upstream online', 'energy intelligence', 'cnbc', 'the guardian',
    'associated press', 'ap', 'eia', 'iea', 'opec',
}


def _oilprice_category_from_url(link):
    """Infer OilPrice.com article category from URL path."""
    link_lower = link.lower()
    if '/energy/natural-gas/' in link_lower or '/energy/lng/' in link_lower:
        return 'LNG & Gas'
    if '/energy/crude-oil/' in link_lower or '/energy/oil-prices/' in link_lower:
        return 'Crude Oil & OPEC'
    if '/energy/gasoline/' in link_lower or '/energy/diesel/' in link_lower:
        return 'Refined Products'
    if '/geopolitics/' in link_lower:
        return 'Geopolitics & Risk'
    if '/us-energy/' in link_lower:
        return 'US Production'
    if '/energy/renewables/' in link_lower or '/alternative-energy/' in link_lower:
        return 'Energy Markets'
    return None  # fall back to feed-level default


def _is_blocked(title, source):
    """Return True if this article should be filtered out."""
    src_lower = (source or '').lower().strip()
    # Exact or partial source match against blocklist
    if any(b in src_lower for b in BLOCKED_SOURCES):
        return True
    # Title pattern match
    title_lower = (title or '').lower()
    if any(rx.search(title_lower) for rx in _BLOCKED_RE):
        return True
    return False


def _quality_score(item):
    """Higher = better. Used for final sort when epoch is equal."""
    src_lower = (item.get('source') or '').lower()
    bonus = 2 if any(p in src_lower for p in PREFERRED_SOURCES) else 0
    # OilPrice.com articles get a boost since they're curated energy content
    if 'oilprice' in src_lower:
        bonus += 3
    return item.get('epoch', 0) + bonus * 3600  # treat bonus as +hours


def _clean_description(description, title):
    """Strip HTML tags, decode entities, and discard Google News pseudo-descriptions.

    Google News RSS sets <description> to "ArticleTitle&nbsp;&nbsp;Source" — just a
    duplicate of the title with the source appended.  Detect this by comparing the
    cleaned text against the title and return '' in that case so we don't show noise.
    """
    text = html.unescape(re.sub(r'<[^>]+>', '', description or '')).strip()
    text = re.sub(r'\s+', ' ', text)            # collapse whitespace / non-breaking spaces
    # If the description is just the title (possibly + " Source"), discard it.
    title_clean = re.sub(r'\s+', ' ', title.strip())
    if text.startswith(title_clean):
        return ''
    return text[:300]


def _parse_item(title, link, source, pubdate_text, description):
    """Return a standardised item dict or None if blocked."""
    title = (title or '').strip()
    if not title or not link:
        return None
    if _is_blocked(title, source):
        return None
    dt = None
    pub_str = ''
    if pubdate_text:
        try:
            dt = parsedate_to_datetime(pubdate_text.strip())
            pub_str = dt.isoformat()
        except Exception:
            pass
    return {
        'title':       title,
        'source':      source or '',
        'date':        pubdate_text or '',
        'datetime':    pub_str,
        'epoch':       dt.timestamp() if dt else 0.0,
        'link':        link,
        'description': _clean_description(description, title),
        '_dt':         dt,
    }


# ── Fetchers ─────────────────────────────────────────────────────────────────

def fetch_oilprice_rss(url, category):
    req = urllib.request.Request(
        url, headers={'User-Agent': 'Mozilla/5.0 (compatible; morning-oil-brief/1.0)'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            xml_bytes = r.read()
    except Exception as e:
        print(f'  FAIL OilPrice RSS {url}: {e}', file=sys.stderr)
        return []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f'  XML parse error OilPrice RSS: {e}', file=sys.stderr)
        return []

    items = []
    for item in root.findall('.//item'):
        title_el   = item.find('title')
        link_el    = item.find('link')
        pubdate_el = item.find('pubDate')
        desc_el    = item.find('description')
        link = (link_el.text or '').strip() if link_el is not None else ''
        if not link:
            link = item.findtext('link') or ''
        parsed = _parse_item(
            title_el.text if title_el is not None else '',
            link,
            'OilPrice.com',
            pubdate_el.text if pubdate_el is not None else '',
            desc_el.text if desc_el is not None else '',
        )
        if parsed:
            parsed['category'] = _oilprice_category_from_url(link) or category
            items.append(parsed)
    return items


def _parse_oilprice_date(art):
    for field in ('pubDate', 'published_at', 'publishedAt', 'date', 'created_at', 'pub_date'):
        raw = art.get(field, '')
        if not raw:
            continue
        try:
            return parsedate_to_datetime(raw), raw
        except Exception:
            pass
        try:
            normalized = raw.replace(' ', 'T').rstrip('Z') + '+00:00'
            return datetime.fromisoformat(normalized), raw
        except Exception:
            pass
    return None, ''


def fetch_oilprice_api(api_key):
    data = None
    for url_template in OILPRICE_API_URLS:
        url = url_template.format(key=api_key)
        req = urllib.request.Request(
            url, headers={'User-Agent': 'morning-oil-brief/1.0', 'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
            data = json.loads(raw)
            print(f'  OilPrice API: response from {url.split("?")[0]} — raw preview: {str(data)[:200]}')
            break
        except Exception as e:
            print(f'  OilPrice API {url.split("?")[0]}: {e}', file=sys.stderr)

    if data is None:
        return []

    # Normalise various response shapes
    if isinstance(data, list):
        articles = data
    elif isinstance(data, dict):
        inner = data.get('data', data.get('articles', data.get('items', [])))
        articles = inner if isinstance(inner, list) else (inner.get('articles', []) if isinstance(inner, dict) else [])
    else:
        articles = []

    print(f'  OilPrice API: {len(articles)} raw articles')
    items = []
    for art in articles:
        dt, raw_date = _parse_oilprice_date(art)
        link = art.get('link') or art.get('url') or art.get('article_url') or ''
        cat  = art.get('category') or art.get('type') or 'Energy Markets'
        desc = art.get('description') or art.get('summary') or art.get('excerpt') or ''
        parsed = _parse_item(
            art.get('title', ''), link, 'OilPrice.com', raw_date,
            re.sub(r'<[^>]+>', '', desc))
        if parsed:
            parsed['category'] = cat
            parsed['epoch'] = dt.timestamp() if dt else 0.0
            parsed['datetime'] = dt.isoformat() if dt else ''
            items.append(parsed)
    return items


def fetch_gnews(query, category):
    url = GNEWS_BASE.format(query=urllib.parse.quote(query))
    req = urllib.request.Request(
        url, headers={'User-Agent': 'Mozilla/5.0 (compatible; morning-oil-brief/1.0)'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            xml_bytes = r.read()
    except Exception as e:
        print(f'  FAIL Google News [{category}]: {e}', file=sys.stderr)
        return []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f'  XML parse error Google News [{category}]: {e}', file=sys.stderr)
        return []

    items = []
    for item in root.findall('.//item'):
        title_el   = item.find('title')
        link_el    = item.find('link')
        pubdate_el = item.find('pubDate')
        source_el  = item.find('source')
        desc_el    = item.find('description')

        title = (title_el.text or '').strip() if title_el is not None else ''
        link  = (link_el.text or '').strip() if link_el is not None else ''

        # Extract source name
        source = ''
        if source_el is not None and source_el.text:
            source = source_el.text.strip()
        elif ' - ' in title:
            parts = title.rsplit(' - ', 1)
            if len(parts) == 2 and len(parts[1]) <= 60:
                title = parts[0].strip()
                source = parts[1].strip()

        # Skip OilPrice.com articles — we fetch those directly from their RSS,
        # and Google News returns many syndicated copies (Yahoo Finance, MSN,
        # Nasdaq) with identical titles that waste the dedup budget.
        if 'oilprice' in link.lower() or 'oilprice' in source.lower():
            continue

        # Only accept articles from recognized quality outlets. Google News
        # search surfaces a long tail of travel blogs, local TV, and SEO spam
        # that happen to mention oil. Drop anything not on the allowlist.
        # Use word-boundary regex so 'time' doesn't match 'times kuwait', etc.
        src_lower = source.lower().strip()
        if src_lower and not any(
            re.search(r'\b' + re.escape(a) + r'\b', src_lower)
            for a in ALLOWED_GNEWS_SOURCES
        ):
            continue

        # Google News <description> is always "Title&nbsp;&nbsp;Source" — not real
        # article text. Pass empty string so we don't display that noise.
        parsed = _parse_item(
            title, link, source,
            pubdate_el.text if pubdate_el is not None else '',
            '',
        )
        if parsed:
            parsed['category'] = category
            items.append(parsed)
    return items


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=CUTOFF_HOURS)
    all_items = []
    seen_titles = set()

    def _add(items, label=''):
        added = 0
        skipped_old = 0
        skipped_dup = 0
        for item in items:
            dt = item.get('_dt')
            if dt and dt < cutoff:
                skipped_old += 1
                continue
            key = re.sub(r'\s+', ' ', item['title'].lower().strip())[:80]
            if key in seen_titles:
                skipped_dup += 1
                continue
            seen_titles.add(key)
            all_items.append(item)
            added += 1
        if added == 0 and (skipped_old or skipped_dup):
            print(f'    ({label}skipped: {skipped_old} too old, {skipped_dup} dedup)')
        return added

    # 1. Google News RSS — run FIRST so quality sources (Reuters, Bloomberg, WSJ)
    #    claim their titles in seen_titles before OilPrice syndicated copies arrive.
    print('[1/3] Google News RSS...')
    for query, category in FEEDS:
        items = fetch_gnews(query, category)
        n = _add(items)
        print(f'  {category:30} → {n} added ({len(items)} fetched)')
        time.sleep(0.5)

    # 2. OilPrice.com RSS — supplements Google News with energy-specific coverage
    print('[2/3] OilPrice.com RSS feeds...')
    op_rss_total = 0
    for rss_url, rss_cat in OILPRICE_RSS_FEEDS:
        items = fetch_oilprice_rss(rss_url, rss_cat)
        n = _add(items)
        op_rss_total += n
        print(f'  {rss_url.split("/")[-1]:20} → {n} added ({len(items)} fetched)')
        time.sleep(0.4)
    print(f'  OilPrice RSS total: {op_rss_total} new articles')

    # 3. OilPrice.com API — additional articles if key available
    if OILPRICE_API_KEY:
        print('[3/3] OilPrice.com API...')
        api_items = fetch_oilprice_api(OILPRICE_API_KEY)
        n = _add(api_items)
        print(f'  OilPrice API: {n} new articles added (after dedup)')
    else:
        print('[3/3] OilPrice.com API: skipped (no OILPRICE_API_KEY)')

    # Sort: quality-adjusted recency (OilPrice + preferred sources float up)
    all_items.sort(key=_quality_score, reverse=True)
    all_items = all_items[:MAX_ITEMS]

    # Strip internal fields
    for item in all_items:
        item.pop('_dt', None)

    # Summary stats
    sources = {}
    for it in all_items:
        s = it.get('source', '(none)')
        sources[s] = sources.get(s, 0) + 1
    print(f'\nTotal: {len(all_items)} articles')
    print('Top sources:')
    for s, c in sorted(sources.items(), key=lambda x: -x[1])[:15]:
        print(f'  {c:3d}  {s}')

    payload = {
        'generated_at': now.isoformat(),
        'cutoff_hours': CUTOFF_HOURS,
        'items': all_items,
    }
    with open(OUT_PATH, 'w') as f:
        json.dump(payload, f, indent=2)
    print(f'\nWrote {OUT_PATH} ({os.path.getsize(OUT_PATH):,} bytes)')


if __name__ == '__main__':
    main()
