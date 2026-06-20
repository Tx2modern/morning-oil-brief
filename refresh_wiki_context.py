#!/usr/bin/env python3
"""
Extract compact market intelligence context from the LLM-wiki knowledge base
and save it to wiki_context.json for injection into dashboard AI prompts.

Run this after ingesting new sources into the LLM-wiki, or as part of your
morning refresh sequence (before build_index.py).

Usage:
    python3 refresh_wiki_context.py [--wiki-path /path/to/LLM-wiki]

Outputs:
    wiki_context.json — compact context block for prompt injection
"""

import json
import os
import re
import sys
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

# Sections to extract from overview.md (by heading text)
OVERVIEW_SECTIONS = [
    'Current state',
    'Key themes',
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
        # Match ## or ### headings that contain the section name (case-insensitive)
        pattern = re.compile(
            r'(#{2,3}\s+' + re.escape(name) + r'.*?)\n(.*?)(?=\n#{1,3}\s|\Z)',
            re.IGNORECASE | re.DOTALL,
        )
        m = pattern.search(stripped)
        if m:
            content = m.group(2).strip()
            # Trim very long sections to ~500 chars
            if len(content) > 500:
                content = content[:500].rsplit('\n', 1)[0] + '\n...'
            results.append(f"### {name}\n{content}")

    return '\n\n'.join(results) if results else ''


def read_page_summary(wiki_dir, rel_path, max_chars=600):
    """Read a wiki page and extract the most useful context."""
    path = os.path.join(wiki_dir, rel_path)
    if not os.path.exists(path):
        return None

    with open(path, encoding='utf-8') as f:
        raw = f.read()

    stripped = strip_frontmatter(raw)

    # Extract title from H1
    title_m = re.search(r'^#\s+(.+)$', stripped, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else os.path.basename(rel_path)

    # Try to extract known sections
    extracted = extract_sections(stripped, PAGE_SECTIONS)

    # If no specific sections found, take the first substantive paragraph
    if not extracted:
        paras = [p.strip() for p in stripped.split('\n\n') if p.strip() and not p.startswith('#')]
        extracted = '\n\n'.join(paras[:3])

    # Trim to budget
    if len(extracted) > max_chars:
        extracted = extracted[:max_chars].rsplit('\n', 1)[0] + '\n...'

    return f"## {title}\n{extracted}" if extracted else None


def read_overview(wiki_dir, max_chars=1800):
    """Extract the key themes and current state from overview.md."""
    path = os.path.join(wiki_dir, 'overview.md')
    if not os.path.exists(path):
        return ''

    with open(path, encoding='utf-8') as f:
        raw = f.read()

    stripped = strip_frontmatter(raw)

    # Extract "Current state" paragraph + "Key themes" section
    result_parts = []

    # Current state paragraph
    m = re.search(
        r'## Current state.*?\n(.*?)(?=\n## |\Z)',
        stripped, re.DOTALL
    )
    if m:
        content = m.group(1).strip()
        if len(content) > 600:
            content = content[:600].rsplit('\n', 1)[0] + '\n...'
        result_parts.append(f"### Current state\n{content}")

    # Key themes — grab heading + first paragraph of each numbered theme
    themes_m = re.search(
        r'## Key themes(.*?)(?=\n## |\Z)',
        stripped, re.DOTALL
    )
    if themes_m:
        themes_text = themes_m.group(1)
        # Extract each ### theme heading + its first substantive paragraph
        theme_blocks = re.findall(
            r'###\s+\d+\.\s+(.+?)\n(.*?)(?=\n###|\Z)',
            themes_text,
            re.DOTALL,
        )
        theme_summaries = []
        for theme_title, theme_body in theme_blocks:
            # Take first 2 sentences / ~200 chars
            body = theme_body.strip()
            # Remove sub-bullets and warning notes
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


def build_context(wiki_path):
    """Build the full wiki context text block."""
    wiki_dir = os.path.join(wiki_path, 'wiki')
    if not os.path.isdir(wiki_dir):
        print(f'  → wiki directory not found: {wiki_dir}')
        return '', 0

    # Read log.md to get latest activity date
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

    parts = [
        f"MARKET INTELLIGENCE CONTEXT (from integrated knowledge base; {source_count} sources ingested through {latest_date})",
        "Use this background to add depth and continuity to your commentary. These are established themes — integrate naturally, do not contradict with current session data.",
        "---",
    ]

    # Overview first
    overview = read_overview(wiki_dir)
    if overview:
        parts.append("# MARKET OVERVIEW\n" + overview)

    # Key commodity and concept pages
    remaining_budget = MAX_CHARS - sum(len(p) for p in parts)
    per_page_budget = max(200, remaining_budget // max(1, len(WIKI_PAGES) - 1))

    page_parts = []
    for rel_path in WIKI_PAGES[1:]:  # skip overview.md — already handled
        summary = read_page_summary(wiki_dir, rel_path, max_chars=per_page_budget)
        if summary:
            page_parts.append(summary)

    if page_parts:
        parts.append("# COMMODITY & CONCEPT CONTEXT\n" + '\n\n'.join(page_parts))

    full_text = '\n\n'.join(parts)

    # Final trim
    if len(full_text) > MAX_CHARS:
        full_text = full_text[:MAX_CHARS].rsplit('\n', 1)[0] + '\n...'

    return full_text, source_count


def main():
    wiki_path = DEFAULT_WIKI_PATH
    for i, arg in enumerate(sys.argv[1:]):
        if arg == '--wiki-path' and i + 1 < len(sys.argv[1:]):
            wiki_path = sys.argv[i + 2]
        elif not arg.startswith('--'):
            wiki_path = arg

    print(f'refresh_wiki_context: reading from {wiki_path}')

    context_text, source_count = build_context(wiki_path)

    if not context_text:
        print('  → no wiki content found; wiki_context.json will be empty')

    result = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'wiki_path': wiki_path,
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
