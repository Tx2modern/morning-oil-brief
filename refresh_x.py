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
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, 'x_feed_data.json')

BEARER_TOKEN = os.environ.get('X_BEARER_TOKEN')
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY')

if not BEARER_TOKEN:
    print('ERROR: X_BEARER_TOKEN environment variable not set.', file=sys.stderr)
    sys.exit(1)

# Accounts to track — username: display name
TRACKED_ACCOUNTS = {
    'JavierBlas':    'Javier Blas',
    'Reuters':       'Reuters',
    'WoodMackenzie': 'Wood Mackenzie',
    'BakerHughes':   
    'EIAgov':        'EIA',
    'OPECnews':      'OPEC',
}

MAX_POSTS_PER_ACCOUNT = 10
MAX_TOTAL_POSTS = 30


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


def get_recent_tweets(user_id, max_results=10):
    params = {
        'max_results': min(max_results, 100),
        'tweet.fields': 'created_at,public_metrics,text',
        'exclude': 'retweets,replies',
    }
    data = x_get(f'/users/{user_id}/tweets', params)
    return data.get('data', [])


def summarize(text, display_name):
    """Generate a 1-2 sentence market-focused summary via Anthropic."""
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


def main():
    now = datetime.now(timezone.utc)
    posts = []
    errors = []

    for username, display_name in TRACKED_ACCOUNTS.items():
        print(f'  Fetching @{username}...')
        try:
            uid = get_user_id(username)
            tweets = get_recent_tweets(uid, MAX_POSTS_PER_ACCOUNT)
            print(f'    {len(tweets)} tweets fetched')
            for tweet in tweets:
                metrics = tweet.get('public_metrics', {})
                summary = summarize(tweet['text'], display_name)
                posts.append({
                    'id': tweet['id'],
                    'author': {'name': display_name, 'userName': username},
                    'text': tweet['text'],
                    'summary': summary,
                    'createdAt': tweet.get('created_at', ''),
                    'likeCount': metrics.get('like_count', 0),
                    'retweetCount': metrics.get('retweet_count', 0),
                    'replyCount': metrics.get('reply_count', 0),
                })
                time.sleep(0.3)  # rate limit buffer between summaries
        except Exception as e:
            print(f'    FAIL @{username}: {e}', file=sys.stderr)
            errors.append(username)
        time.sleep(1)  # rate limit buffer between accounts

    # Sort by date descending, cap total
    posts.sort(key=lambda x: x.get('createdAt', ''), reverse=True)
    posts = posts[:MAX_TOTAL_POSTS]

    payload = {
        'lastUpdated': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'posts': posts,
    }

        with open(OUT_PATH, 'w') as f:
        json.dump(payload, f, indent=2)
    print(f'\nWrote {OUT_PATH} ({os.path.getsize(OUT_PATH):,} bytes), {len(posts)} posts')

    # Patch x_feed.html — replace the baked-in FEED_DATA constant
    import re as _re
    x_feed_html = os.path.join(HERE, 'x_feed.html')
    if os.path.exists(x_feed_html):
        with open(x_feed_html, 'r', encoding='utf-8') as f:
            html = f.read()
        new_const = 'const FEED_DATA = ' + json.dumps(payload, separators=(',', ':')) + ';'
        patched, count = _re.subn(r'const FEED_DATA = \{.*?\};', new_const, html, count=1, flags=_re.DOTALL)
        if count:
            with open(x_feed_html, 'w', encoding='utf-8') as f:
                f.write(patched)
            print(f'Patched {x_feed_html}')
        else:
            print('WARNING: FEED_DATA pattern not found in x_feed.html', file=sys.stderr)

    if errors:
        print(f'Failed accounts: {errors}', file=sys.stderr)


if __name__ == '__main__':
    main()
