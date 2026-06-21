#!/usr/bin/env python3
"""
Refresh the CITGO_DATA block in x_feed.html via Apify tweet-scraper.

Searches for recent posts (last 24h) mentioning "CITGO Venezuela",
fetches up to 20, keeps the top 6 by engagement (likes + retweets),
generates a 1-sentence analyst summary for each, then patches x_feed.html
between the sentinel comments:

    // @@CITGO_DATA_START@@
    ...
    // @@CITGO_DATA_END@@

Requires:
  APIFY_API_TOKEN   - Apify API token
  ANTHROPIC_API_KEY - for summaries (optional; falls back to truncated text)
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
X_FEED_HTML = os.path.join(HERE, 'x_feed.html')

APIFY_TOKEN = os.environ.get('APIFY_API_TOKEN')
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY')

ACTOR_ID = 'apidojo~tweet-scraper'
SEARCH_QUERY = 'CITGO Venezuela'
FETCH_LIMIT = 20
TOP_N = 6

SENTINEL_START = '// @@CITGO_DATA_START@@'
SENTINEL_END = '// @@CITGO_DATA_END@@'

if not APIFY_TOKEN:
    print('ERROR: APIFY_API_TOKEN environment variable not set.', file=sys.stderr)
    sys.exit(1)


def apify_post(path, payload):
    url = f'https://api.apify.com/v2{path}?token={APIFY_TOKEN}'
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={'Content-Type': 'application/json', 'User-Agent': 'morning-oil-brief/1.0'},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def apify_get(path):
    url = f'https://api.apify.com/v2{path}?token={APIFY_TOKEN}'
    req = urllib.request.Request(url, headers={'User-Agent': 'morning-oil-brief/1.0'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def run_actor_and_wait(actor_id, input_data, poll_interval=10, max_wait=300):
    print(f'  Starting Apify actor {actor_id}...')
    resp = apify_post(f'/acts/{actor_id}/runs', input_data)
    run = resp.get('data', {})
    run_id = run.get('id')
    dataset_id = run.get('defaultDatasetId')
    if not run_id:
        raise RuntimeError(f'No run ID returned: {resp}')
    print(f'  Run ID: {run_id}')

    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(poll_interval)
        status_resp = apify_get(f'/actor-runs/{run_id}')
        status = status_resp.get('data', {}).get('status', '')
        print(f'  Status: {status}')
        if status in ('SUCCEEDED', 'FAILED', 'ABORTED', 'TIMED-OUT'):
            break

    if status != 'SUCCEEDED':
        raise RuntimeError(f'Actor run ended with status: {status}')

    dataset_id = status_resp.get('data', {}).get('defaultDatasetId', dataset_id)
    return dataset_id


def fetch_dataset(dataset_id, limit=100):
    params = urllib.parse.urlencode({'limit': limit, 'token': APIFY_TOKEN})
    url = f'https://api.apify.com/v2/datasets/{dataset_id}/items?{params}'
    req = urllib.request.Request(url, headers={'User-Agent': 'morning-oil-brief/1.0'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def summarize(text, author_name):
    if not ANTHROPIC_KEY:
        return text[:200] + ('...' if len(text) > 200 else '')

    prompt = (
        f'This X post by {author_name} mentions CITGO or Venezuela energy:\n\n"{text}"\n\n'
        'Write exactly 1 sentence, analyst-style, summarizing the key implication '
        'for CITGO, Venezuelan oil, or US refining. Be factual. No quotes.'
    )
    payload = json.dumps({
        'model': 'claude-haiku-4-5-20251001',
        'max_tokens': 100,
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
        print(f'    summary error: {e}', file=sys.stderr)
        return text[:200]


def patch_html(payload):
    if not os.path.exists(X_FEED_HTML):
        print(f'ERROR: {X_FEED_HTML} not found', file=sys.stderr)
        return False

    with open(X_FEED_HTML, 'r', encoding='utf-8') as f:
        html = f.read()

    start_idx = html.find(SENTINEL_START)
    end_idx = html.find(SENTINEL_END)

    if start_idx == -1 or end_idx == -1:
        print('ERROR: sentinel comments not found in x_feed.html', file=sys.stderr)
        print(f'  Looking for: {SENTINEL_START!r}', file=sys.stderr)
        print(f'  and:         {SENTINEL_END!r}', file=sys.stderr)
        return False

    if end_idx <= start_idx:
        print('ERROR: END sentinel appears before START sentinel', file=sys.stderr)
        return False

    json_str = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)

    new_block = (
        f'{SENTINEL_START}\n'
        f'// ── CITGO/VENEZUELA DATA (rewritten by citgo-venezuela-x-feed-daily task) ──\n'
        f'const CITGO_DATA = {json_str};\n'
        f'{SENTINEL_END}'
    )

    patched = html[:start_idx] + new_block + html[end_idx + len(SENTINEL_END):]

    with open(X_FEED_HTML, 'w', encoding='utf-8') as f:
        f.write(patched)

    # Verify the written JSON is valid by parsing it back
    with open(X_FEED_HTML, 'r', encoding='utf-8') as f:
        written = f.read()
    verify_start = written.find(SENTINEL_START)
    verify_end = written.find(SENTINEL_END)
    block = written[verify_start:verify_end + len(SENTINEL_END)]
    const_prefix = 'const CITGO_DATA = '
    const_pos = block.find(const_prefix)
    if const_pos == -1:
        print('ERROR: verification failed — const CITGO_DATA not found after write', file=sys.stderr)
        return False
    json_start = const_pos + len(const_prefix)
    json_end = block.rfind(';', json_start)
    try:
        parsed = json.loads(block[json_start:json_end])
        print(f'  Verified: {len(parsed.get("posts", []))} posts in written JSON')
    except json.JSONDecodeError as e:
        print(f'ERROR: JSON verification failed: {e}', file=sys.stderr)
        return False

    print(f'Patched {X_FEED_HTML} with {len(payload["posts"])} CITGO/Venezuela posts')
    return True


def build_tweet_url(tweet, username):
    """Return the canonical X URL for a tweet.

    Prefer the url field returned by Apify directly; fall back to
    constructing from tweetId (the actual status ID) or id."""
    if tweet.get('url'):
        return tweet['url']
    # tweetId is the numeric status ID; 'id' may be an Apify internal ID
    status_id = tweet.get('tweetId') or tweet.get('id', '')
    return f'https://x.com/{username}/status/{status_id}'


def main():
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')

    actor_input = {
        'searchTerms': [SEARCH_QUERY],
        'maxItems': FETCH_LIMIT,
        'sort': 'Top',
        'tweetLanguage': 'en',
        'start': since,
    }

    try:
        dataset_id = run_actor_and_wait(ACTOR_ID, actor_input)
        items = fetch_dataset(dataset_id, limit=FETCH_LIMIT)
    except Exception as e:
        print(f'ERROR: {e}', file=sys.stderr)
        sys.exit(1)

    print(f'  {len(items)} tweets fetched')

    # Filter out retweets and posts that don't actually mention CITGO
    originals = [
        t for t in items
        if not t.get('isRetweet', False)
        and 'citgo' in (t.get('text', '') + t.get('fullText', '')).lower()
    ]
    print(f'  {len(originals)} original tweets after filtering retweets and non-CITGO posts')

    # Sort by engagement and take top 6
    originals.sort(
        key=lambda t: (t.get('likeCount', 0) + t.get('retweetCount', 0)),
        reverse=True,
    )
    top = originals[:TOP_N]

    posts = []
    for tweet in top:
        author = tweet.get('author', {})
        username = author.get('userName', 'unknown')
        display_name = author.get('name', username)
        text = tweet.get('text', tweet.get('fullText', ''))
        tweet_url = build_tweet_url(tweet, username)
        print(f'  Summarizing @{username} → {tweet_url}')
        summary = summarize(text, display_name)
        posts.append({
            'id': tweet.get('tweetId') or tweet.get('id', ''),
            'url': tweet_url,
            'author': {'name': display_name, 'userName': username},
            'text': text,
            'summary': summary,
            'createdAt': tweet.get('createdAt', ''),
            'likeCount': tweet.get('likeCount', 0),
            'retweetCount': tweet.get('retweetCount', 0),
            'replyCount': tweet.get('replyCount', 0),
        })
        time.sleep(0.3)

    payload = {
        'lastUpdated': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'posts': posts,
    }

    if not patch_html(payload):
        sys.exit(1)


if __name__ == '__main__':
    main()
