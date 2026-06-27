#!/usr/bin/env python3
"""
Refresh the CITGO_DATA block in x_feed.html via Apify tweet-scraper.

Searches for recent posts (last 24h) mentioning "CITGO Venezuela",
fetches up to 20, keeps the top 6 by engagement (likes + retweets),
generates a 1-sentence analyst summary for each (translating Spanish
to English when needed), then patches x_feed.html between the sentinel
comments:

    // @@CITGO_DATA_START@@
    ...
    // @@CITGO_DATA_END@@

Requires:
  APIFY_API_TOKEN   - Apify API token
  ANTHROPIC_API_KEY - for summaries (optional; falls back to truncated text)
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
X_FEED_HTML = os.path.join(HERE, 'x_feed.html')

APIFY_TOKEN = os.environ.get('APIFY_API_TOKEN')
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY')

ACTOR_ID = 'apidojo~tweet-scraper'
SEARCH_QUERY_PRIMARY   = 'CITGO Venezuela'
 SEARCH_QUERY_FALLBACK  = 'Venezuela oil refinery'
FETCH_LIMIT = 30
TOP_N = 6
FALLBACK_N = 2
LOOKBACK_HOURS = 48

SENTINEL_START = '// @@CITGO_DATA_START@@'
SENTINEL_END = '// @@CITGO_DATA_END@@'

if not APIFY_TOKEN:
    print('ERROR: APIFY_API_TOKEN environment variable not set.', file=sys.stderr)
    sys.exit(1)

# Oil/energy anchor keywords — a tweet must contain at least one to be kept
_OIL_ANCHOR_RE = re.compile(
    r'\b(oil|crude|refin|gasoline|diesel|fuel|barrel[s]?|bbl|pdvsa|citgo|'
    r'petroleum|energy|lng|pipeline|tanker|cargo|export|import|sanction|'
    r'production|output|supply|downstream|upstream|petrole|combust)\b',
    re.IGNORECASE,
)

# AI refusal phrases — if summary starts with one of these, skip the post
_REFUSAL_STARTS = (
    "i cannot", "i can't", "i don't have", "i am unable", "i'm unable",
    "unable to provide", "no specific", "this post does not", "this post contains no",
    "the post does not", "the post contains no", "there is no", "there are no",
    "no direct", "no oil", "no energy", "no operational", "no implication",
)


def _is_oil_relevant(text):
    """Return True only if the tweet text contains an oil/energy anchor keyword."""
    return bool(_OIL_ANCHOR_RE.search(text))


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
    """Return an English analyst summary, or None if the post lacks oil content."""
    if not ANTHROPIC_KEY:
        return text[:200] + ('...' if len(text) > 200 else '')

    prompt = (
        f'This X post by {author_name} mentions CITGO or Venezuela energy:\n\n"{text}"\n\n'
        'If the post is in Spanish or another language, translate it to English first. '
        'Then write exactly 1 sentence, analyst-style, summarizing the key implication '
        'for CITGO, Venezuelan oil, or US refining. Be factual. No quotes. '
        'If the post contains no oil or energy market information, reply with exactly: SKIP'
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
        # Drop explicit SKIP signal
        if result.upper() == 'SKIP':
            return None
        # Drop any refusal-style response
        result_lower = result.lower()
        if any(result_lower.startswith(p) for p in _REFUSAL_STARTS):
            return None
        return result
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

    # Verify the written JSON is valid
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
    if tweet.get('url'):
        return tweet['url']
    status_id = tweet.get('tweetId') or tweet.get('id', '')
    return f'https://x.com/{username}/status/{status_id}'


def _run_search(query, since, limit):
    actor_input = {
        'searchTerms': [query],
        'maxItems': limit,
        'sort': 'Top',
        'start': since,
    }
    dataset_id = run_actor_and_wait(ACTOR_ID, actor_input)
    return fetch_dataset(dataset_id, limit=limit)


def _filter_candidates(items, require_both_keywords=False):
    """Filter to oil-relevant originals, optionally requiring both citgo AND venezuela."""
    def _relevant(t):
        body = (t.get('text', '') + t.get('fullText', '')).lower()
        keyword_match = (
            ('citgo' in body and 'venezuela' in body)
            if require_both_keywords
            else ('citgo' in body or 'venezuela' in body)
        )
        return keyword_match and _is_oil_relevant(t.get('text', '') + t.get('fullText', ''))

    originals = [t for t in items if not t.get('isRetweet', False) and _relevant(t)]
    if not originals:
        originals = [t for t in items if _relevant(t)]
    return originals


def main():
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=LOOKBACK_HOURS)).strftime('%Y-%m-%dT%H:%M:%SZ')
    is_fallback = False

    # --- Pass 1: CITGO Venezuela ---
    try:
        print(f'  Pass 1: searching "{SEARCH_QUERY_PRIMARY}"...')
        items = _run_search(SEARCH_QUERY_PRIMARY, since, FETCH_LIMIT)
        print(f'  {len(items)} tweets fetched')
    except Exception as e:
        print(f'ERROR (pass 1): {e}', file=sys.stderr)
        sys.exit(1)

    candidates = _filter_candidates(items)
    print(f'  {len(candidates)} oil-relevant CITGO/Venezuela candidates')

    # --- Pass 2: fallback if pass 1 found nothing oil-relevant ---
    if not candidates:
        is_fallback = True
        print(f'  No oil-relevant posts found — running fallback search "{SEARCH_QUERY_FALLBACK}"...')
        try:
            items2 = _run_search(SEARCH_QUERY_FALLBACK, since, FETCH_LIMIT)
            print(f'  {len(items2)} tweets fetched (fallback)')
        except Exception as e:
            print(f'ERROR (pass 2): {e}', file=sys.stderr)
            sys.exit(1)
        candidates = _filter_candidates(items2)
        print(f'  {len(candidates)} Venezuela oil candidates (fallback)')

    limit = FALLBACK_N if is_fallback else TOP_N

    candidates.sort(
        key=lambda t: (t.get('likeCount', 0) + t.get('retweetCount', 0)),
        reverse=True,
    )

    posts = []
    for tweet in candidates:
        if len(posts) >= limit:
            break
        author = tweet.get('author', {})
        username = author.get('userName', 'unknown')
        display_name = author.get('name', username)
        text = tweet.get('text', tweet.get('fullText', ''))
        tweet_url = build_tweet_url(tweet, username)
        print(f'  Summarizing @{username} → {tweet_url}')
        summary = summarize(text, display_name)
        if summary is None:
            print(f'    Skipped (no oil content in summary)')
            continue
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

    print(f'  {len(posts)} posts kept after summarization filter')

    payload = {
        'lastUpdated': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'posts': posts,
    }

    if not patch_html(payload):
        sys.exit(1)


if __name__ == '__main__':
    main()
