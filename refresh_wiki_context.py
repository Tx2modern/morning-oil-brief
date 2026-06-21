#!/usr/bin/env python3
"""
Extract compact market intelligence context from the LLM-wiki knowledge base
and save it to wiki_context.json for injection into dashboard AI prompts.

Run this after ingesting new sources into the LLM-wiki, or as part of your
morning refresh sequence (before build_index.py).

Usage:
    # Local filesystem (Obsidian vault):
    python3 refresh_wiki_context.py [--wiki-path /path/to/LLM-wiki]

    # GitHub Actions / remote (fetches from GitHub API):
    python3 refresh_wiki_context.py --github-repo tx2modern/llm-wiki

    WIKI_GITHUB_TOKEN env var (or GITHUB_TOKEN) is used for authenticated requests.

Outputs:
    wiki_context.json — compact context block for prompt injection
"""

import base64
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, 'wiki_context.json')

# Default wiki path — adjust if your LLM-wiki is elsewhere
DEFAULT_WIKI_PATH = os.path.expanduser(
    '~/Library/CloudStorage/Dropbox/LLM-wiki'
)

# Pages to extract, in priority order. (path relative to wiki/)
WIKI_PAGES = [
    'overview.md',
    'commodities/crude/WTI.md',
    'concepts/Cushing.md',
    'commodities/products/propane.md',
    'commodities/products/gasoline.md',
    'commodities/products/ULSD.md',
    'commodities/products/jet-fuel.md',
    'concepts/sour-sweet-differentials.md',
    'concepts/regrade.md',
    'concepts/strategic-petroleum-reserve.md',
    'concepts/jones-act.md',
    'concepts/Hormuz.md',
    'entities/Saudi-Arabia.md',
    'entities/Enbridge.md',
    'entities/Russia.md',
]

# For commodity/concept/entity pages, extract these sections
PAGE_SECTIONS = [
    'Current price context',
    'Recent weekly history',
    'Recent weekly inventory history',
    'Key drivers',
    'Current state',
    'Current rate context',
    'Why it matters',
    'Current context',
    'Background',
]

# Token budget: target ~600 words for the full context block
MAX_CHARS = 4000


# ---------------------------------------------------------------------------
# Text processing helpers (shared between local and GitHub paths)
# ---------------------------------------------------------------------------

def strip_frontmatter(text):
    """Remove YAML frontmatter block from markdown."""
    if text.startswith('---'):
        end = text.find('\n---', 3)
        if end != -1:
            return text[end + 4:].lstrip('\n')
    return text


def extract_sections(text, section_names):
    """Extract specific H2/H3 sections from markdown by heading name."""
    stripped = strip_frontmatter(text)
    results = []

    for name in section_names:
        pattern = re.compile(
            r'(#{2,3}\s+' + re.escape(name) + r'.*?)\n(.*?)(?=\n#{1,3}\s|\Z)',
            re.IGNORECASE | re.DOTALL,
        )
        m = pattern.search(stripped)
        if m:
            content = m.group(2).strip()
            if len(content) > 500:
                content = content[:500].rsplit('\n', 1)[0] + '\n...'
            results.append(f"### {name}\n{content}")

    return '\n\n'.join(results) if results else ''


def _summarize_page(raw, rel_path, max_chars=600):
    """Extract the most useful context from a wiki page's raw markdown text."""
    stripped = strip_frontmatter(raw)

    title_m = re.search(r'^#\s+(.+)$', stripped, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else os.path.basename(rel_path)

    extracted = extract_sections(stripped, PAGE_SECTIONS)

    if not extracted:
        paras = [p.strip() for p in stripped.split('\n\n') if p.strip() and not p.startswith('#')]
        extracted = '\n\n'.join(paras[:3])

    if len(extracted) > max_chars:
        extracted = extracted[:max_chars].rsplit('\n', 1)[0] + '\n...'

    return f"## {title}\n{extracted}" if extracted else None


def _summarize_overview(raw, max_chars=1800):
    """Extract current state and key themes from overview.md text."""
    stripped = strip_frontmatter(raw)
    result_parts = []

    m = re.search(r'## Current state.*?\n(.*?)(?=\n## |\Z)', stripped, re.DOTALL)
    if m:
        content = m.group(1).strip()
        if len(content) > 600:
            content = content[:600].rsplit('\n', 1)[0] + '\n...'
        result_parts.append(f"### Current state\n{content}")

    themes_m = re.search(r'## Key themes(.*?)(?=\n## |\Z)', stripped, re.DOTALL)
    if themes_m:
        themes_text = themes_m.group(1)
        theme_blocks = re.findall(
            r'###\s+\d+\.\s+(.+?)\n(.*?)(?=\n###|\Z)',
            themes_text,
            re.DOTALL,
        )
        theme_summaries = []
        for theme_title, theme_body in theme_blocks:
            body = theme_body.strip()
            body = re.sub(r'\n>.*', '', body)
            body = re.sub(r'\n\*\*.*?\*\*:.*', '', body)
            sentences = re.split(r'(?<=[.!?])\s+', body)
            short_body = ' '.join(sentences[:2])[:250]
            theme_summaries.append(f"- **{theme_title.strip()}**: {short_body}")
        result_parts.append("### Key themes\n" + '\n'.join(theme_summaries))

    result = '\n\n'.join(result_parts)
    if len(result) > max_chars:
        result = result[:max_chars].rsplit('\n', 1)[0] + '\n...'
    return result


def _assemble_context(overview_text, page_texts, source_count, latest_date):
    """Build the final context text block from extracted pieces."""
    parts = [
        f"MARKET INTELLIGENCE CONTEXT (from integrated knowledge base; {source_count} sources ingested through {latest_date})",
        "Use this background to add depth and continuity to your commentary. These are established themes — integrate naturally, do not contradict with current session data.",
        "---",
    ]

    if overview_text:
        parts.append("# MARKET OVERVIEW\n" + overview_text)

    if page_texts:
        parts.append("# COMMODITY & CONCEPT CONTEXT\n" + '\n\n'.join(page_texts))

    full_text = '\n\n'.join(parts)
    if len(full_text) > MAX_CHARS:
        full_text = full_text[:MAX_CHARS].rsplit('\n', 1)[0] + '\n...'

    return full_text


# ---------------------------------------------------------------------------
# Local filesystem path
# ---------------------------------------------------------------------------

def read_page_summary(wiki_dir, rel_path, max_chars=600):
    path = os.path.join(wiki_dir, rel_path)
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        raw = f.read()
    return _summarize_page(raw, rel_path, max_chars)


def read_overview(wiki_dir, max_chars=1800):
    path = os.path.join(wiki_dir, 'overview.md')
    if not os.path.exists(path):
        return ''
    with open(path, encoding='utf-8') as f:
        raw = f.read()
    return _summarize_overview(raw, max_chars)


def build_context(wiki_path):
    """Build the full wiki context text block from a local filesystem path."""
    wiki_dir = os.path.join(wiki_path, 'wiki')
    if not os.path.isdir(wiki_dir):
        print(f'  → wiki directory not found: {wiki_dir}')
        return '', 0

    log_path = os.path.join(wiki_dir, 'log.md')
    latest_date = 'unknown'
    source_count = 0
    if os.path.exists(log_path):
        with open(log_path) as f:
            log_text = f.read()
        dates = re.findall(r'## \[(\d{4}-\d{2}-\d{2})\]', log_text)
        if dates:
            latest_date = sorted(dates)[-1]
        source_count = log_text.count('] ingest |')

    overview_text = read_overview(wiki_dir)

    remaining_budget = MAX_CHARS - len(overview_text) - 300
    per_page_budget = max(200, remaining_budget // max(1, len(WIKI_PAGES) - 1))

    page_texts = []
    for rel_path in WIKI_PAGES[1:]:
        summary = read_page_summary(wiki_dir, rel_path, max_chars=per_page_budget)
        if summary:
            page_texts.append(summary)

    return _assemble_context(overview_text, page_texts, source_count, latest_date), source_count


# ---------------------------------------------------------------------------
# GitHub API path (used in CI / GitHub Actions)
# ---------------------------------------------------------------------------

def _github_get(owner_repo, path, token=None):
    """Fetch a file from GitHub Contents API; returns decoded text or None."""
    url = f'https://api.github.com/repos/{owner_repo}/contents/wiki/{path}'
    req = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'morning-oil-brief/refresh-wiki',
    })
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        if data.get('encoding') == 'base64':
            return base64.b64decode(data['content']).decode('utf-8')
        return None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # page doesn't exist yet — skip silently
        print(f'  → GitHub API error {e.code} for {path}: {e.reason}')
        return None
    except Exception as e:
        print(f'  → GitHub fetch failed for {path}: {e}')
        return None


def build_context_from_github(owner_repo, token=None):
    """Build wiki context by fetching pages from the GitHub API."""
    print(f'  → fetching wiki pages from github.com/{owner_repo}')

    # Parse source stats from log.md
    latest_date = 'unknown'
    source_count = 0
    log_text = _github_get(owner_repo, 'log.md', token)
    if log_text:
        dates = re.findall(r'## \[(\d{4}-\d{2}-\d{2})\]', log_text)
        if dates:
            latest_date = sorted(dates)[-1]
        source_count = log_text.count('] ingest |')
    else:
        print('  → log.md not found in wiki repo (repo may be empty or still initializing)')

    # Overview
    overview_raw = _github_get(owner_repo, 'overview.md', token)
    overview_text = _summarize_overview(overview_raw) if overview_raw else ''

    # Commodity / concept pages
    remaining_budget = MAX_CHARS - len(overview_text) - 300
    per_page_budget = max(200, remaining_budget // max(1, len(WIKI_PAGES) - 1))

    page_texts = []
    for rel_path in WIKI_PAGES[1:]:
        raw = _github_get(owner_repo, rel_path, token)
        if raw:
            summary = _summarize_page(raw, rel_path, max_chars=per_page_budget)
            if summary:
                page_texts.append(summary)

    fetched = len(page_texts)
    print(f'  → fetched {fetched} page(s) from GitHub wiki')

    if not overview_raw and fetched == 0:
        return '', 0

    return _assemble_context(overview_text, page_texts, source_count, latest_date), source_count


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    github_repo = None
    wiki_path = DEFAULT_WIKI_PATH

    i = 0
    while i < len(args):
        if args[i] == '--github-repo' and i + 1 < len(args):
            github_repo = args[i + 1]
            i += 2
        elif args[i] == '--wiki-path' and i + 1 < len(args):
            wiki_path = args[i + 1]
            i += 2
        elif not args[i].startswith('--'):
            wiki_path = args[i]
            i += 1
        else:
            i += 1

    if github_repo:
        token = os.environ.get('WIKI_GITHUB_TOKEN') or os.environ.get('GITHUB_TOKEN')
        print(f'refresh_wiki_context: GitHub mode → {github_repo}')
        context_text, source_count = build_context_from_github(github_repo, token)
        source_label = github_repo
    else:
        print(f'refresh_wiki_context: local mode → {wiki_path}')
        context_text, source_count = build_context(wiki_path)
        source_label = wiki_path

    if not context_text:
        print('  → no wiki content found; wiki_context.json will be empty')

    result = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'wiki_path': source_label,
        'source_count': source_count,
        'context_text': context_text,
        'char_count': len(context_text),
    }

    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f'  → wiki_context.json written ({len(context_text):,} chars, {source_count} sources)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
