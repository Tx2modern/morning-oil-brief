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
from datetime import datetime, timezone

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

MAX_POSTS_PER_ACCOUNT = 5
MAX_TOTAL_POSTS = 60


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


def get_recent_tweets(user_id, max_results=5):
    params = {
        'max_results': min(max_results, 100),
        'tweet.fields': 'created_at,public_metrics,text',
        'exclude': 'retweets,replies',
    }
    data = x_get(f'/users/{user_id}/tweets', params)
    return data.get('data', [])


def summarize(text, display_name):
    if not ANTHROPIC_KEY:
        return text[:200] + ('...' if len(text) > 200 else '')

    prompt = (
        f'{display_name} posted on X:\n\n"{text}"\n\n'
        'Write a 1-2 sentence summary focused on the oil market implications. '
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
        return resp['content'][0]['text'].strip()
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
            tweets = get_recent_tweets(uid, MAX_POSTS_PER_ACCOUNT)
            print(f'    {len(tweets)} tweets fetched')
            for tweet in tweets:
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

    posts.sort(key=lambda x: x.get('createdAt', ''), reverse=True)
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
