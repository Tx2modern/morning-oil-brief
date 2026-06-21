#!/usr/bin/env python3
"""
Refresh the CITGO_DATA block in x_feed.html using X API v2 recent-search.

Searches for recent tweets mentioning CITGO or Venezuela in an oil/energy context,
generates AI summaries via Anthropic, and patches x_feed.html in-place.

Requires:
  X_BEARER_TOKEN    - X API v2 Bearer Token
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
X_FEED_HTML = os.path.join(HERE, 'x_feed.html')

BEARER_TOKEN = os.environ.get('X_BEARER_TOKEN')
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY')

if not BEARER_TOKEN:
    print('ERROR: X_BEARER_TOKEN environment variable not set.', file=sys.stderr)
    sys.exit(1)

SEARCH_QUERY = (
    '(CITGO OR "Citgo Petroleum" OR "Venezuela oil" OR "Venezuelan crude" OR PDVSA) '
    '(oil OR refinery OR petroleum OR crude OR sanctions OR barrel OR energy) '
    '-is:retweet -is:reply lang:en'
)
MAX_RESULTS = 20


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


def search_recent(query, max_results=20):
    params = {
        'query': query,
        'max_results': min(max(10, max_results), 100),
        'tweet.fields': 'created_at,public_metrics,text,author_id',
        'expansions': 'author_id',
        'user.fields': 'username,name',
    }
    data = x_get('/tweets/search/recent', params)
    tweets = data.get('data', [])
    users = {u['id']: u for u in data.get('includes', {}).get('users', [])}
    return tweets, users


def summarize(text, display_name):
    if not ANTHROPIC_KEY:
        return text[:200] + ('...' if len(text) > 200 else '')

    prompt = (
        f'{display_name} posted on X about CITGO or Venezuela energy:\n\n"{text}"\n\n'
        'Write a 1-2 sentence summary focused on the implications for CITGO, '
        'Venezuelan oil, or US refining. Be factual and concise. '
        'Do not start with "This post" or use quotes.'
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
        print(f'ERROR: {X_FEED_HTML} not found', file=sys.stderr)
        return False

    with open(X_FEED_HTML, 'r', encoding='utf-8') as f:
        html = f.read()

    new_const = 'const CITGO_DATA = ' + json.dumps(payload, separators=(',', ':')) + ';'
    patched, count = re.subn(
        r'const CITGO_DATA = \{.*?\};',
        lambda m: new_const,
        html,
        count=1,
        flags=re.DOTALL,
    )
    if count:
        with open(X_FEED_HTML, 'w', encoding='utf-8') as f:
            f.write(patched)
        print(f'Patched {X_FEED_HTML} with {len(payload["posts"])} CITGO/Venezuela posts')
        return True
    else:
        print('WARNING: CITGO_DATA pattern not found in x_feed.html', file=sys.stderr)
        return False


def main():
    now = datetime.now(timezone.utc)
    print(f'Searching X for CITGO/Venezuela posts...')

    try:
        tweets, users = search_recent(SEARCH_QUERY, MAX_RESULTS)
    except Exception as e:
        print(f'ERROR fetching tweets: {e}', file=sys.stderr)
        sys.exit(1)

    print(f'  {len(tweets)} tweets found')

    posts = []
    for tweet in tweets:
        user = users.get(tweet.get('author_id', ''), {})
        username = user.get('username', 'unknown')
        display_name = user.get('name', username)
        metrics = tweet.get('public_metrics', {})
        print(f'  Summarizing @{username}...')
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
        time.sleep(0.3)

    posts.sort(key=lambda x: x.get('createdAt', ''), reverse=True)

    payload = {
        'lastUpdated': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'posts': posts,
    }

    patch_x_feed_html(payload)


if __name__ == '__main__':
    main()
