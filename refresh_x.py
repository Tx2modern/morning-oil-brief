#!/usr/bin/env python3
"""
Refresh x_feed_data.json from the X (Twitter) API v2.

Fetches recent posts from tracked energy accounts, generates
AI summaries via Anthropic, and writes x_feed_data.json.

Requires:
  X_BEARER_TOKEN  - X API v2 Bearer Token
  ANTHROPIC_API_KEY - for generating post summaries (optional; falls back to truncated text)
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, 'x_feed_data.json')
X_FEED_HTML = os.path.join(HERE, 'x_feed.html')

BEARER_TOKEN = os.environ.get('X_BEARER_TOKEN')
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY')

if not BEARER_TOKEN:
    print('ERROR: X_BEARER_TOKEN environment variable not set.', file=sys.stderr)
    sys.exit(1)

# Crude, Refining & Products — full follow list
# Format: 'TwitterHandle': ('Display Name', 'Category')
TRACKED_ACCOUNTS = {
    # Price reporting agencies
    'argusmedia':       ('Argus Media',         'Price Reporting'),
    'opis':             ('OPIS',                 'Price Reporting'),
    'SPGEnergyOil':     ('S&P Global Energy',   'Price Reporting'),

    # Research, analytics & data
    'RBNEnergy':        ('RBN Energy',           'Research & Analytics'),
    'WoodMackenzie':    ('Wood Mackenzie',        'Research & Analytics'),
    'EnergyAspects':    ('Energy Aspects',        'Research & Analytics'),

    # Flows & vessel / cargo tracking
    'kpler':            ('Kpler',                'Flows & Tracking'),
    'Vortexa':          ('Vortexa',              'Flows & Tracking'),
    'tankertrackers':   ('Tanker Trackers',      'Flows & Tracking'),

    # News & journalists
    'ReutersVzla':      ('Reuters Venezuela',    'News'),
    'ArathySom':        ('Arathy Som',           'News'),
    'mariannaparraga':  ('Marianna Parraga',     'News'),
    'Rory_Johnston':    ('Rory Johnston',         'News'),
    'JavierBlas':       ('Javier Blas',          'News'),
    'jkempenergy':      ('JKem Energy',          'News'),
    'ftenergy':         ('FT Energy',            'News'),
    'OilandEnergy':     ('Oil and Energy',       'News'),
    'OilandGibbs':      ('Oil and Gibbs',        'News'),

    # Independent analysts & traders
    'CroftHelima':      ('Helima Croft',         'Analysts & Traders'),
    'staunovo':         ('Giovanni Staunovo',    'Analysts & Traders'),
    'IliaBouchouev':    ('Ilia Bouchouev',       'Analysts & Traders'),
    'chigrl':           ('Tracy Shuchart',       'Analysts & Traders'),
    'AndurandPierre':   ('Pierre Andurand',      'Analysts & Traders'),
    'energyphilflynn':  ('Phil Flynn',           'Analysts & Traders'),
    'Ole_S_Hansen':     ('Ole Hansen',           'Analysts & Traders'),

    # Retail fuel / pump prices
    'GasBuddyGuy':      ('Patrick De Haan',      'Retail Fuel'),
    'TomKloza':         ('Tom Kloza',            'Retail Fuel'),

    # Refiners & integrated majors
    'ValeroEnergy':     ('Valero Energy',        'Refiners & Majors'),
    'phillips66co':     ('Phillips 66',          'Refiners & Majors'),
    'MarathonPetroCo':  ('Marathon Petroleum',   'Refiners & Majors'),
    'Chevron':          ('Chevron',              'Refiners & Majors'),
    'exxonmobil':       ('ExxonMobil',           'Refiners & Majors'),
    'bp_America':       ('BP America',           'Refiners & Majors'),
    'IrvingOil':        ('Irving Oil',           'Refiners & Majors'),

    # Trading houses
    'trafigura':        ('Trafigura',            'Trading Houses'),
    'Gunvor':           ('Gunvor',               'Trading Houses'),
    'vitolnews':        ('Vitol',                'Trading Houses'),

    # Midstream, pipelines & distribution
    'Colpipe':          ('Colonial Pipeline',    'Midstream'),
    'EnergyTransfer':   ('Energy Transfer',      'Midstream'),
    'MansfieldEnergy':  ('Mansfield Energy',     'Midstream'),

    # Official / institutional
    'eiagov':           ('EIA',                  'Official'),
    'iea':              ('IEA',                  'Official'),
    'opecsecretariat':  ('OPEC',                 'Official'),
    'ENERGY':           ('US Dept of Energy',    'Official'),
}

MAX_POSTS_PER_ACCOUNT = 10  # fetch up to 10, select top 2 by likes
TOP_POSTS_PER_ACCOUNT = 2  # keep the 2 highest-liked posts per account
MAX_TOTAL_POSTS = 90       # hard cap on combined feed
LOOKBACK_HOURS = 24        # only posts from the past 24 hours

# ── Off-topic keyword filter ──────────────────────────────────────────────────
# Posts whose text matches one of these patterns AND lacks an oil/petroleum
# anchor are dropped before summarising.  Keeps the feed focused on crude,
# refined products, and freight — not the broader energy transition.
_OFFTOPIC_PATTERNS = [
    r'\bnuclear\s+(power|plant[s]?|reactor[s]?|energy|capacity|fuel|waste)\b',
    r'\b(nuclear|atomic)\s+power\b',
    r'\bnuclear\s+deal\b',
    r'\belectricity\s+(grid|price[s]?|market[s]?|generation|demand|supply|rate[s]?|network|sector)\b',
    r'\b(power\s+grid|electric\s+grid|grid\s+operator|grid\s+stability)\b',
    r'\bsolar\s+(panel[s]?|farm[s]?|power|energy|cell[s]?|capacity|install)\b',
    r'\bphotovoltaic\b',
    r'\bcoal\s+(mine[s]?|mining|plant[s]?|power|fired|price[s]?|production|sector|energy)\b',
    r'\bcoal-fired\b',
    r'\bbatter(y|ies)\b',
    r'\b(lithium.ion|sodium.ion|solid.state)\s+batter',
    r'\bwind\s+(farm[s]?|turbine[s]?|power|energy|capacity)\b',
    r'\brenewable\s+(energy|power|capacity|generation)\b',
]
_OFFTOPIC_RE = [re.compile(p, re.IGNORECASE) for p in _OFFTOPIC_PATTERNS]

# If any of these oil anchors appear, keep the post even if an off-topic
# pattern also matched (e.g. "crude oil vs solar cost comparison").
_OIL_ANCHORS = re.compile(
    r'\b(crude|brent|wti|opec|refin|gasoline|diesel|jet\s+fuel|naphtha|fuel\s+oil|'
    r'petroleum|barrel[s]?|bbl|tanker|pipeline|lng|natural\s+gas|upstream|downstream|'
    r'drilling|frack|shale|permian|pdvsa|citgo|venezuela\s+oil)\b',
    re.IGNORECASE,
)


def _is_offtopic(text):
    """Return True if the post is primarily about an off-topic energy sector."""
    if any(rx.search(text) for rx in _OFFTOPIC_RE):
        # Keep it if there is a clear oil/petroleum anchor in the text
        if _OIL_ANCHORS.search(text):
            return False
        return True
    return False


def x_get(path, params=None):
    url = 'https://api.twitter.com/2' + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            'Authorization': f'Bearer {BEARER_TOKEN}',
            'User-Agent': 'morning-oil-brief/1.0',
        }
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def get_user_id(username):
    data = x_get(f'/users/by/username/{username}')
    return data['data']['id']


def get_recent_tweets(user_id, max_results=10, since_hours=24):
    start_time = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
    params = {
        'max_results': min(max_results, 100),
        'tweet.fields': 'created_at,public_metrics,text',
        'exclude': 'retweets,replies',
        'start_time': start_time,
    }
    data = x_get(f'/users/{user_id}/tweets', params)
    return data.get('data', [])


def summarize(text, display_name):
    if not ANTHROPIC_KEY:
        return text[:200] + ('...' if len(text) > 200 else '')

    prompt = (
        f'{display_name} posted on X:\n\n"{text}"\n\n'
        'Write a 1-2 sentence summary focused on the oil market implications. '
        'Base your summary ONLY on the text above — do not reference any links or external content. '
        'If the text is too short or lacks substance, just restate the key point plainly. '
        'Be factual and concise. Do not start with "This post" or use quotes.'
    )
    payload = json.dumps({
        'model': 'claude-haiku-4-5-20251001',
        'max_tokens': 120,
        'messages': [{'role': 'user', 'content': prompt}],
    }).encode()

    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=payload,
        headers={
            'x-api-key': ANTHROPIC_KEY,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read())
        result = resp['content'][0]['text'].strip()
        # Fall back to raw text if the model refused or said it can't access content
        refusal_phrases = ("i don't have access", "i cannot", "i can't", "unable to provide", "no access")
        if any(result.lower().startswith(p) for p in refusal_phrases):
            return text[:200] + ('...' if len(text) > 200 else '')
        return result
    except Exception as e:
        print(f'    summary API error: {e}', file=sys.stderr)
        return text[:200]


def patch_x_feed_html(payload):
    if not os.path.exists(X_FEED_HTML):
        return
    with open(X_FEED_HTML, 'r', encoding='utf-8') as f:
        html = f.read()
    new_const = 'const FEED_DATA = ' + json.dumps(payload, separators=(',', ':')) + ';'
    patched, count = re.subn(
        r'const FEED_DATA = \{.*?\};',
        lambda m: new_const,
        html,
        count=1,
        flags=re.DOTALL,
    )
    if count:
        with open(X_FEED_HTML, 'w', encoding='utf-8') as f:
            f.write(patched)
        print(f'Patched {X_FEED_HTML}')
    else:
        print('WARNING: FEED_DATA pattern not found in x_feed.html', file=sys.stderr)


def main():
    now = datetime.now(timezone.utc)
    posts = []
    errors = []

    for username, (display_name, category) in TRACKED_ACCOUNTS.items():
        print(f'  Fetching @{username} ({category})...')
        try:
            uid = get_user_id(username)
            tweets = get_recent_tweets(uid, MAX_POSTS_PER_ACCOUNT, LOOKBACK_HOURS)
            print(f'    {len(tweets)} tweets fetched (last {LOOKBACK_HOURS}h)')

            # Filter off-topic posts (electricity, coal, nuclear, solar, etc.)
            filtered = [t for t in tweets if not _is_offtopic(t.get('text', ''))]
            if len(filtered) < len(tweets):
                print(f'    {len(tweets) - len(filtered)} off-topic posts filtered')

            # Select top 2 by likes
            filtered.sort(key=lambda t: t.get('public_metrics', {}).get('like_count', 0), reverse=True)
            top_tweets = filtered[:TOP_POSTS_PER_ACCOUNT]

            for tweet in top_tweets:
                metrics = tweet.get('public_metrics', {})
                summary = summarize(tweet['text'], display_name)
                posts.append({
                    'id': tweet['id'],
                    'author': {'name': display_name, 'userName': username, 'category': category},
                    'text': tweet['text'],
                    'summary': summary,
                    'createdAt': tweet.get('created_at', ''),
                    'likeCount': metrics.get('like_count', 0),
                    'retweetCount': metrics.get('retweet_count', 0),
                    'replyCount': metrics.get('reply_count', 0),
                })
                time.sleep(0.3)
        except Exception as e:
            print(f'    FAIL @{username}: {e}', file=sys.stderr)
            errors.append(username)
        time.sleep(1)

    # Sort by likes descending, then cap
    posts.sort(key=lambda x: x.get('likeCount', 0), reverse=True)
    posts = posts[:MAX_TOTAL_POSTS]

    payload = {
        'lastUpdated': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'posts': posts,
    }

    with open(OUT_PATH, 'w') as f:
        json.dump(payload, f, indent=2)
    print(f'\nWrote {OUT_PATH} ({os.path.getsize(OUT_PATH):,} bytes), {len(posts)} posts')

    patch_x_feed_html(payload)

    if errors:
        print(f'Failed accounts: {errors}', file=sys.stderr)


if __name__ == '__main__':
    main()
