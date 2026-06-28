"""
Build a production index.html for Netlify by injecting real EIA bulk data
into the polished `eia-dashboard-shareable.html` template.

Run after refresh_data.py — reads eia_data.json and writes index.html.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, date, timezone


def _utcnow():
    """Timezone-aware UTC 'now' returned as a naive datetime, so existing
    .strftime() / .isoformat()+'Z' call sites behave identically to the old
    datetime.utcnow() (which is deprecated in modern Python)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

HERE = os.path.dirname(os.path.abspath(__file__))

# NYMEX/NYSE full-day closures (extend as needed each year). When the next
# calendar weekday lands on one of these, skip it for the "next trade date".
_MARKET_HOLIDAYS = {
    # 2026
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents' Day
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),   # Independence Day (observed)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas
    # 2027
    date(2027, 1, 1),   # New Year's Day
    date(2027, 1, 18),  # MLK Day
    date(2027, 2, 15),  # Presidents' Day
    date(2027, 3, 26),  # Good Friday
    date(2027, 5, 31),  # Memorial Day
    date(2027, 6, 18),  # Juneteenth (observed)
    date(2027, 7, 5),   # Independence Day (observed)
    date(2027, 9, 6),   # Labor Day
    date(2027, 11, 25), # Thanksgiving
    date(2027, 12, 24), # Christmas (observed)
}


def _next_trading_day(d):
    """Return the next NYMEX trading day strictly after `d` (a date)."""
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5 or nxt in _MARKET_HOLIDAYS:
        nxt += timedelta(days=1)
    return nxt


def _next_refresh_date(trade_through):
    """Day after the next trade date, given the latest trade-data date."""
    if isinstance(trade_through, datetime):
        trade_through = trade_through.date()
    return _next_trading_day(trade_through) + timedelta(days=1)


# ─── EIA Weekly Petroleum Status Report — holiday release schedule ──────
# Source: https://www.eia.gov/petroleum/supply/weekly/schedule.php
#
# The WPSR is normally released Wednesdays at 10:30 a.m. ET. When a federal
# holiday lands on the Mon/Tue of release week, EIA shifts the release to
# Thursday (or, around Christmas, the following Monday). This table maps
# the WPSR data-week-ending-date (always a Friday) → the actual release
# datetime ET when it differs from the standard slot.
#
# Keep this in sync with EIA's published calendar each year.
_WPSR_HOLIDAY_RELEASES = {
    # 2024-2025 season
    date(2024, 12, 27): (date(2025, 1, 2),  'Thursday', '11:00 AM'),  # New Year's Day
    date(2025, 1, 17):  (date(2025, 1, 23), 'Thursday', '12:00 PM'),  # MLK / Inauguration
    date(2025, 2, 14):  (date(2025, 2, 20), 'Thursday', '12:00 PM'),  # Presidents' Day
    date(2025, 5, 23):  (date(2025, 5, 29), 'Thursday', '12:00 PM'),  # Memorial Day
    date(2025, 8, 29):  (date(2025, 9, 4),  'Thursday', '12:00 PM'),  # Labor Day
    date(2025, 10, 10): (date(2025, 10, 16),'Thursday', '12:00 PM'),  # Columbus Day
    date(2025, 11, 7):  (date(2025, 11, 13),'Thursday', '12:00 PM'),  # Veterans Day
    date(2025, 12, 19): (date(2025, 12, 29),'Monday',   '5:00 PM'),   # Christmas
    # 2026 season
    date(2026, 1, 16):  (date(2026, 1, 22), 'Thursday', '12:00 PM'),  # MLK Day
    date(2026, 2, 13):  (date(2026, 2, 19), 'Thursday', '12:00 PM'),  # Presidents' Day
    date(2026, 5, 22):  (date(2026, 5, 28), 'Thursday', '12:00 PM'),  # Memorial Day
    date(2026, 9, 4):   (date(2026, 9, 10), 'Thursday', '12:00 PM'),  # Labor Day
    date(2026, 10, 9):  (date(2026, 10, 15),'Thursday', '12:00 PM'),  # Columbus Day
    date(2026, 11, 6):  (date(2026, 11, 12),'Thursday', '12:00 PM'),  # Veterans Day
}


def _wpsr_release_for(week_ending):
    """Return ``(release_date, weekday_label, release_time_str)`` for the WPSR
    covering ``week_ending`` (a Friday date).

    Looks up the holiday table first; otherwise falls back to the standard
    slot — Wednesday following the week-ending Friday, at 10:30 AM ET.
    """
    if isinstance(week_ending, datetime):
        week_ending = week_ending.date()
    if week_ending in _WPSR_HOLIDAY_RELEASES:
        return _WPSR_HOLIDAY_RELEASES[week_ending]
    # Default: the Wednesday after the Friday week-ending = Friday + 5 days
    standard_wed = week_ending + timedelta(days=5)
    return (standard_wed, 'Wednesday', '10:30 AM')


def _next_wpsr_release_str(latest_data_week_ending):
    """Display string for the NEXT WPSR release given the latest data we have.

    ``latest_data_week_ending`` is the Friday-week-ending date of the most
    recent WPSR already loaded. The next release covers the *following*
    Friday, so we look that one up in the holiday table.

    Returns e.g. ``'Thu May 28, 2026 · 12:00 PM ET'`` or
    ``'Wed Jun 3, 2026 · 10:30 AM ET'``.
    """
    if isinstance(latest_data_week_ending, datetime):
        latest_data_week_ending = latest_data_week_ending.date()
    next_week_ending = latest_data_week_ending + timedelta(days=7)
    release_date, weekday, time_str = _wpsr_release_for(next_week_ending)
    return release_date.strftime(f'{weekday[:3]} %b %-d, %Y · {time_str} ET')


def _compute_freshness(prices_as_of_str, latest_date):
    """Decide whether the page is in "synced" or "post_eia" state.

    The dashboard has two independent data clocks:
      • EIA inventories  — refresh on WPSR release (Wed/Thu ~10:30 AM ET)
      • NYMEX prices     — refresh next morning, reflecting the post-EIA settle

    Between those two events (Wed ~10:30 AM → Thu ~5:00 AM) the inventory
    feed is ahead of the price feed. We call that window ``post_eia``: the
    fundamentals story is fresh, but no settle has yet priced it in.

    Once tomorrow's settle prints and prices refresh, both sides advance and
    we return to ``synced`` — prices now reflect the EIA print and commentary
    can speak retrospectively about the reaction.

    Returns a dict:
      state           — 'synced' | 'post_eia'
      prices_through  — display string for the settle date  ("May 27, 2026")
      inv_through     — display string for the EIA week ending ("May 22, 2026")
      eia_release_str — display string for the WPSR release  ("Wed May 27, 2026 · 10:30 AM ET")
      banner_html     — non-empty when state is 'post_eia'; ready to inject
                        directly into the page (already styled, no extra CSS
                        required — uses inline tokens from the dashboard palette).
    """
    if isinstance(latest_date, datetime):
        eia_week_end = latest_date.date()
    else:
        eia_week_end = latest_date

    release_date, release_weekday, release_time = _wpsr_release_for(eia_week_end)

    # Parse prices_as_of (may be empty / malformed — treat as "very stale").
    prices_dt = None
    if prices_as_of_str:
        try:
            prices_dt = datetime.strptime(prices_as_of_str, '%Y-%m-%d').date()
        except Exception:
            prices_dt = None

    # state = post_eia when the latest EIA report has been released but the
    # prior-session settle we're displaying predates that release.
    state = 'synced'
    if prices_dt is None or prices_dt < release_date:
        state = 'post_eia'

    prices_through = (
        prices_dt.strftime('%B %-d, %Y') if prices_dt
        else eia_week_end.strftime('%B %-d, %Y')
    )
    inv_through = eia_week_end.strftime('%B %-d, %Y')
    eia_release_str = release_date.strftime(
        f'{release_weekday[:3]} %b %-d, %Y · {release_time} ET'
    )

    banner_html = ''
    if state == 'post_eia':
        banner_html = (
            '<div class="eia-fresh-banner" style="'
            'margin:14px 0;padding:16px 20px;'
            'background:linear-gradient(180deg, rgba(240,176,86,0.10), rgba(240,176,86,0.04));'
            'border:1px solid rgba(240,176,86,0.55);border-left:3px solid #f0b056;'
            'border-radius:10px;color:var(--text,#e6e6e6);'
            'font-size:13px;line-height:1.6;">'
            '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;'
            'font-size:11px;font-weight:700;letter-spacing:0.8px;text-transform:uppercase;'
            'color:#f0b056;margin-bottom:8px;">'
            '<span>● EIA WPSR — Just Released</span>'
            f'<span style="color:var(--muted-2,#9aa0a6);font-weight:500;letter-spacing:0.4px;">'
            f'Week ending {inv_through} · Released {eia_release_str}'
            '</span></div>'
            '<div style="margin-bottom:8px;">'
            'Today\'s WPSR has refreshed on the <strong>Inventories</strong> tab. '
            'Prices and charts below are <strong>last session\'s NYMEX settle</strong> '
            'and do <em>not</em> yet incorporate today\'s report.'
            '</div>'
            '<div style="color:var(--muted-2,#9aa0a6);">'
            '<strong style="color:var(--text,#e6e6e6);">AI commentary is paused for this page.</strong> '
            'It will return with tomorrow morning\'s refresh, once the post-EIA settle '
            'prints and we can tie the market\'s reaction back to today\'s data.'
            '</div></div>'
        )

    return {
        'state': state,
        'prices_through': prices_through,
        'inv_through': inv_through,
        'eia_release_str': eia_release_str,
        'banner_html': banner_html,
    }


def _freshness_instruction(freshness_state, inv_through, eia_release_str):
    """Return the prompt-time instruction block that tells the model how to
    frame commentary given the current data-clock state.

    Two slots, swapped by state:
      • post_eia  — EIA refreshed today, prices still on yesterday's settle.
                    Lead with the fundamental print; explicitly note prices
                    have not yet reacted; do NOT pretend the price action
                    confirms the inventory move.
      • synced    — Prices already reflect the most recent EIA report.
                    Tie the price/curve move *back* to that report's print.
    """
    if freshness_state == 'post_eia':
        return (
            "DATA-CLOCK STATE: POST_EIA — Today's WPSR for the week ending "
            f"{inv_through} was just released ({eia_release_str}). The price, "
            "crack, and curve data below are from LAST session's NYMEX settle "
            "and DO NOT YET REFLECT today's report. Your commentary MUST:\n"
            f"  1. LEAD with the EIA print for week ending {inv_through}. The "
            "FIRST sentence of your Headline paragraph must name today's "
            "draws/builds (e.g. 'EIA reported a 3.3 MMbbl crude draw…'). The "
            "EIA release IS the headline — geopolitical news is secondary "
            "context, not the lead.\n"
            f"  2. PHRASING OVERRIDE: refer to this WPSR as 'today's report', "
            f"'this morning's WPSR', or 'the just-released EIA print for week "
            f"ending {inv_through}'. DO NOT call it 'last week's WPSR' — that "
            "phrasing is for SYNCED-state commentary only. The data covers "
            f"week ending {inv_through} but was RELEASED TODAY — treat it as "
            "breaking news.\n"
            "  3. State explicitly that prices below predate the report and "
            "that the market's reaction will print in tonight's settle.\n"
            "  4. Frame implications forward-looking: 'expect bearish/bullish "
            "pressure into tonight's settle' — NOT 'prices fell on the print' "
            "(they haven't moved yet in this dataset).\n"
            "  5. Do not claim cracks or curves 'confirmed' or 'reacted to' "
            "today's inventory data — that reaction is not in this dataset."
        )
    return (
        "DATA-CLOCK STATE: SYNCED — Prices below already reflect the market "
        f"reaction to the most recent WPSR (week ending {inv_through}, "
        f"released {eia_release_str}). Tie the price/crack/curve move "
        "RETROSPECTIVELY back to that report: 'yesterday's draw of X mb drove "
        "WTI +$Y in today's settle, narrowing the 321 crack by…'. This is the "
        "normal state — the daily morning refresh."
    )


def _refreshed_stamp_et():
    """Return ``(time_str, date_str)`` for the current build's refresh stamp,
    expressed in US Eastern time.

    ``time_str`` is like ``'8:47 AM ET'`` and ``date_str`` is like
    ``'MAY 25, 2026'`` (uppercased) — matching the home-page hero stamp
    layout that the margins and curves AI Brief headers also use.

    Prefers ``zoneinfo`` (stdlib, py3.9+). Falls back to a hand-rolled DST
    calculation so we never crash on a stripped-down Python build: US
    Eastern observes DST from the second Sunday of March through the first
    Sunday of November.
    """
    now_utc = _utcnow()
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo('America/New_York'))
    except Exception:
        year = now_utc.year
        mar1 = date(year, 3, 1)
        # Second Sunday of March = first Sunday + 7 days.
        dst_start = mar1 + timedelta(days=((6 - mar1.weekday()) % 7) + 7)
        nov1 = date(year, 11, 1)
        # First Sunday of November.
        dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
        in_dst = dst_start <= now_utc.date() < dst_end
        offset_hours = 4 if in_dst else 5
        now_et = now_utc - timedelta(hours=offset_hours)
    time_str = now_et.strftime('%-I:%M %p ET')
    date_str = now_et.strftime('%b %-d, %Y').upper()
    return time_str, date_str


# ─── Inventory-page overrides — re-skin the existing .market-read-static and
# .chart-insight containers to match the new "Today's Read" box look and drop
# the yellow left accent line that came with the original template.
INVENTORY_BRIEF_OVERRIDES_CSS = """
/* Re-skin the AI Inventory Brief container to match the home-page hero box */
.market-read-static {
  position: relative; overflow: hidden;
  background: var(--panel) !important;
  border: 1px solid var(--border) !important;
  border-left: 1px solid var(--border) !important;
  border-radius: 14px !important;
  padding: 24px 28px 26px !important;
  font-size: 15px !important;
  line-height: 1.65 !important;
  margin-bottom: 18px !important;
}
.market-read-static::before {
  content: ''; position: absolute; left: 0; right: 0; top: 0; height: 1px;
  background: linear-gradient(90deg, transparent 0%, rgba(245,165,36,0.55) 30%, rgba(94,234,212,0.5) 70%, transparent 100%);
}
.market-read-static p { margin: 0 0 12px !important; }
.market-read-static p:last-child { margin-bottom: 0 !important; }
/* AI Inference box (per-chart): drop the yellow left accent */
.chart-insight {
  border-left: 1px solid var(--border) !important;
}
"""


# ─── Shared CSS for the "AI Brief" box (matches the home page's "Today's Read"
# hero look — borderless rounded card with a subtle gradient hairline on top,
# JetBrains Mono eyebrow + mint AI pill, and roomy typography).  Injected on
# every page that renders an AI commentary block.
AI_BRIEF_BOX_CSS = """
/* AI Brief box — mirrors the home-page "Today's Read" hero styling */
.ai-brief-box {
  position: relative; overflow: hidden;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 14px; padding: 24px 28px 26px;
  margin-bottom: 18px;
}
.ai-brief-box::before {
  content: ''; position: absolute; left: 0; right: 0; top: 0; height: 1px;
  background: linear-gradient(90deg, transparent 0%, rgba(245,165,36,0.55) 30%, rgba(94,234,212,0.5) 70%, transparent 100%);
}
.ai-brief-box-head {
  display: flex; justify-content: space-between; align-items: center;
  gap: 16px; flex-wrap: wrap; margin-bottom: 14px;
}
.ai-brief-box-left { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.ai-brief-box-eyebrow {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 11px; font-weight: 600;
  letter-spacing: 0.22em; text-transform: uppercase;
  color: var(--accent);
}
.ai-brief-box-pill {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 10.5px; font-weight: 600;
  letter-spacing: 0.18em; text-transform: uppercase;
  padding: 4px 10px; border-radius: 4px;
  color: #5eead4;
  background: rgba(94,234,212,0.08);
  border: 1px solid rgba(94,234,212,0.35);
}
.ai-brief-box .market-read { font-size: 15px; line-height: 1.65; color: var(--muted-2); }
.ai-brief-box .market-read strong { color: var(--text); font-weight: 600; }
.ai-brief-box .market-read p { margin: 0 0 12px; }
.ai-brief-box .market-read p:last-child { margin-bottom: 0; }
"""


TEMPLATE = os.path.join(HERE, 'eia-dashboard-shareable.html')
DATA_FILE = os.path.join(HERE, 'eia_data.json')
OUT_LANDING = os.path.join(HERE, 'index.html')      # summary / home page
OUT_INVENTORY = os.path.join(HERE, 'inventory.html')  # detail dashboard
OUT_MARGINS = os.path.join(HERE, 'margins.html')      # margins & cracks
OUT_CURVES = os.path.join(HERE, 'curves.html')        # forward curves
OUT_NEWS   = os.path.join(HERE, 'news.html')          # daily news page
PRICES_FILE = os.path.join(HERE, 'prices_data.json')
SITE_BASE_URL = os.environ.get('SITE_BASE_URL', 'https://www.morningoilbrief.com')
CHAT_ENDPOINT_URL = os.environ.get('CHAT_ENDPOINT_URL', '/.netlify/functions/chat')

# ─── Shared top navigation (rendered on every page) ───
NAV_PAGES = [
    ('index.html',     'Home'),
    ('margins.html',   'Cracks · Prices'),
    ('curves.html',    'Forward Curves'),
    ('inventory.html', 'Inventories'),
    ('trading.html',   'Trading Calls'),
    ('news.html',      'News'),
    ('x_feed.html',    'X Feed'),
]


_BARREL_SVG = (
    '<svg width="28" height="32" viewBox="0 0 28 32" '
    'xmlns="http://www.w3.org/2000/svg" aria-hidden="true" '
    'style="display:block;overflow:visible">'
    # Body fill (amber oil)
    '<path d="M 3.2 4.6 Q 2 16 3.2 27.4 Q 14 29.6 24.8 27.4 '
    'Q 26 16 24.8 4.6 Q 14 2.4 3.2 4.6 Z" fill="#f0b056" opacity="0.95"/>'
    # Body outline
    '<path d="M 3.2 4.6 Q 2 16 3.2 27.4 Q 14 29.6 24.8 27.4 '
    'Q 26 16 24.8 4.6 Q 14 2.4 3.2 4.6 Z" fill="none" '
    'stroke="#f0b056" stroke-width="1.2"/>'
    # Top rim ellipse
    '<ellipse cx="14" cy="4.6" rx="10.8" ry="2.2" '
    'fill="#07090d" stroke="#f0b056" stroke-width="1.1"/>'
    # Hoop bands (carved into the fill)
    '<path d="M 2.5 10.6 Q 14 12.4 25.5 10.6" fill="none" '
    'stroke="#07090d" stroke-width="1.1"/>'
    '<path d="M 2.5 21.4 Q 14 23.2 25.5 21.4" fill="none" '
    'stroke="#07090d" stroke-width="1.1"/>'
    # OIL stencil
    '<text x="14" y="18.2" text-anchor="middle" fill="#07090d" '
    'font-family="\'IBM Plex Mono\',\'JetBrains Mono\',ui-monospace,monospace" '
    'font-size="6" font-weight="700" letter-spacing="0.4">OIL</text>'
    '</svg>'
)

_MOB_BRAND_HTML = (
    '<div class="mob-brand">'
    f'<span class="mob-mark">{_BARREL_SVG}</span>'
    '<span class="mob-name">MOB</span>'
    '<span class="mob-sep">·</span>'
    '<span class="mob-tagline">Morning Oil Brief</span>'
    '</div>'
)


# Favicon — inline SVG data URI (oil barrel emoji, no external file needed)
_FAVICON = '<link rel="icon" href="data:image/svg+xml,<svg xmlns=\'http://www.w3.org/2000/svg\' viewBox=\'0 0 32 32\'><text y=\'26\' font-size=\'28\'>🛢️</text></svg>">'

# Open Graph base tag (URL and image filled per page below)
def _og_tags(title, description, path=''):
    url = f'{SITE_BASE_URL}/{path}'
    return (
        f'<meta property="og:type" content="website">\n'
        f'<meta property="og:site_name" content="MOB · Morning Oil Brief">\n'
        f'<meta property="og:title" content="{title}">\n'
        f'<meta property="og:description" content="{description}">\n'
        f'<meta property="og:url" content="{url}">\n'
        f'<meta name="twitter:card" content="summary">\n'
        f'<meta name="twitter:title" content="{title}">\n'
        f'<meta name="twitter:description" content="{description}">'
    )


_SIGNOUT_JS = """
  function signOut() {
    // Clear Supabase session from localStorage then redirect — no CDN needed
    Object.keys(localStorage).forEach(function(k) {
      if (k.startsWith('sb-')) localStorage.removeItem(k);
    });
    window.location.href = 'login.html';
  }
"""


def _render_nav(active_href):
    items = []
    for href, label in NAV_PAGES:
        cls = 'nav-link active' if href == active_href else 'nav-link'
        items.append(f'<a class="{cls}" href="{href}">{label}</a>')
    return (
        '<nav class="top-nav" aria-label="Main navigation">\n'
        '  <div class="nav-inner">\n'
        f'    {_MOB_BRAND_HTML}\n'
        '    <div class="nav-links">' + ''.join(items) + '</div>\n'
        '    <button class="nav-signout" onclick="signOut()">Sign Out</button>\n'
        '  </div>\n'
        '</nav>'
    )


NAV_CSS = """
/* ────────────────────────────────────────────────────────────────────
   MOB · 2026 redesign — Inter + JetBrains Mono, deep dark, mono accents
   ──────────────────────────────────────────────────────────────────── */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

:root {
  --bg: #07090d;
  --panel: #0e1117;
  --panel-2: #141821;
  --border: #1d222d;
  --border-soft: #161a23;
  --text: #e6e8ec;
  --muted: #7c8593;
  --muted-2: #9aa3b1;
  --accent: #f5a524;
  --mob-ai: #5eead4;
  --mob-text-dim: #c1c5cf;
  --mob-border-strong: #2a3041;
}
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif !important;
  background:
    linear-gradient(rgba(255,255,255,0.06) 1px, transparent 1px) 0 0 / 64px 64px,
    linear-gradient(90deg, rgba(255,255,255,0.06) 1px, transparent 1px) 0 0 / 64px 64px,
    var(--bg) !important;
  font-size: 14px !important;
  -webkit-font-smoothing: antialiased;
}
.container { max-width: 1320px !important; padding: 22px 32px 60px !important; }
@media (max-width: 720px) { .container { padding: 18px 18px 40px !important; } }

/* ─── Top bar ─────────────────────────────────────────────────────── */
.top-nav {
  position: static !important;
  background: transparent !important;
  -webkit-backdrop-filter: none !important;
  backdrop-filter: none !important;
  border-bottom: none !important;
  margin: 0 0 18px !important;
}
.top-nav .nav-inner {
  max-width: 1320px !important;
  margin: 0 auto !important;
  padding: 18px 32px 0 !important;
  display: flex !important;
  justify-content: space-between !important;
  align-items: center !important;
  gap: 24px;
  flex-wrap: wrap;
}
@media (max-width: 720px) {
  .top-nav .nav-inner { padding: 18px 18px 0 !important; }
}
.top-nav .nav-links { display: flex; gap: 4px !important; }
.top-nav .nav-link {
  font-family: 'JetBrains Mono', 'SF Mono', ui-monospace, Menlo, monospace !important;
  font-size: 11.5px !important;
  font-weight: 600 !important;
  letter-spacing: 0.18em !important;
  text-transform: uppercase !important;
  padding: 8px 14px !important;
  color: var(--muted-2) !important;
  background: transparent !important;
  border-radius: 6px !important;
  text-decoration: none;
  transition: color 0.15s, background 0.15s;
}
.top-nav .nav-link:hover { color: var(--text) !important; background: var(--panel-2) !important; }
.top-nav .nav-link.active { color: var(--accent) !important; background: transparent !important; }
.top-nav .nav-link.nav-disabled { opacity: 0.35 !important; pointer-events: none; }
.nav-signout {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 11.5px; font-weight: 600; letter-spacing: 0.18em; text-transform: uppercase;
  padding: 8px 14px; color: var(--muted); background: transparent;
  border: 1px solid var(--border-soft, var(--border)); border-radius: 6px; cursor: pointer;
  transition: color 0.15s, border-color 0.15s; margin-left: 8px;
}
.nav-signout:hover { color: #f87171; border-color: rgba(248,113,113,0.45); background: rgba(248,113,113,0.06); }

/* MOB brand (left of top bar) — flat orange barrel + wordmark */
.mob-brand {
  display: flex; align-items: center; gap: 12px;
  font-family: 'JetBrains Mono', 'SF Mono', ui-monospace, Menlo, monospace;
  font-size: 11.5px; font-weight: 600;
  letter-spacing: 0.18em; text-transform: uppercase;
  color: var(--text);
}
.mob-brand .mob-mark {
  width: 32px; height: 32px;
  display: inline-flex; align-items: center; justify-content: center;
}
.mob-brand .mob-mark svg { width: 100%; height: 100%; display: block; }
.mob-brand .mob-name { color: var(--text); }
.mob-brand .mob-sep { color: var(--muted); margin: 0 2px; }
.mob-brand .mob-tagline { color: var(--muted-2); }

/* ─── Page title block (.header) — works for all pages ───────────── */
.header {
  flex-direction: row !important;
  align-items: flex-end !important;
  justify-content: space-between !important;
  border-bottom: 1px solid var(--border-soft) !important;
  padding: 18px 0 26px !important;
  margin-bottom: 26px !important;
  gap: 24px !important;
}
.header > div:first-child { min-width: 0; }
.header h1 {
  font-family: 'Inter', system-ui, sans-serif !important;
  font-size: 56px !important;
  font-weight: 600 !important;
  letter-spacing: -0.03em !important;
  line-height: 1 !important;
  color: var(--text) !important;
  margin: 0 !important;
}
@media (max-width: 900px) { .header h1 { font-size: 40px !important; } }
@media (max-width: 560px) { .header h1 { font-size: 32px !important; } }
.header .subtitle {
  font-family: 'JetBrains Mono', 'SF Mono', ui-monospace, monospace !important;
  font-size: 12px !important;
  letter-spacing: 0.12em !important;
  text-transform: uppercase !important;
  color: var(--muted) !important;
  margin-top: 10px !important;
}
.header .update-info {
  /* margin-left: auto consumes leftover flex-row space so the block stays
     pinned to the right edge of the header. Critical when the 56px h1
     wraps the flex layout to a second row — without this the update-info
     would land left-aligned on its own row. */
  margin-left: auto !important;
  flex-shrink: 0 !important;
  text-align: right !important;
  font-family: 'JetBrains Mono', 'SF Mono', ui-monospace, monospace !important;
  font-size: 11px !important;
  letter-spacing: 0.14em !important;
  text-transform: uppercase !important;
  color: var(--muted) !important;
  line-height: 1.7 !important;
}
.header .update-info strong { color: var(--mob-text-dim) !important; font-weight: 500 !important; }
.header .update-info .pill {
  background: rgba(94,234,212,0.08) !important;
  color: var(--mob-ai) !important;
  border: 1px solid rgba(94,234,212,0.35) !important;
  border-radius: 4px !important;
  padding: 4px 10px !important;
  font-family: 'JetBrains Mono', ui-monospace, monospace !important;
  font-size: 10.5px !important;
  letter-spacing: 0.18em !important;
  text-transform: uppercase !important;
}

/* ─── Section panels — softer borders, larger radius ──────────────── */
.section, .panel, .news-panel, .brief-panel, .insight, .ai-section {
  background: var(--panel) !important;
  border: 1px solid var(--border) !important;
  border-radius: 12px !important;
}

/* ─── Footer ──────────────────────────────────────────────────────── */
footer {
  margin-top: 56px !important;
  padding-top: 18px !important;
  border-top: 1px solid var(--border-soft) !important;
  font-family: 'JetBrains Mono', 'SF Mono', ui-monospace, monospace !important;
  font-size: 11px !important;
  letter-spacing: 0.16em !important;
  text-transform: uppercase !important;
  color: var(--muted) !important;
  text-align: center !important;
}

/* ─── Interior KPI cards — adopt the MOB landing-card look ──────────
   Black panel background with a 2px colored top hairline (red for
   draw, green for build) instead of the full green/red panel fill +
   left-side accent strip. Text colors adjusted for the dark bg.    */
.kpi {
  background: var(--panel) !important;
  border: 1px solid var(--border) !important;
  border-radius: 12px !important;
  padding: 14px 16px !important;
  position: relative;
  overflow: hidden;
  transition: transform 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease !important;
}
.kpi.build { --kpi-status-color: #22c55e; background: var(--panel) !important; border-color: var(--border) !important; }
.kpi.draw  { --kpi-status-color: #ef4444; background: var(--panel) !important; border-color: var(--border) !important; }
.kpi.neutral { --kpi-status-color: var(--muted); background: var(--panel) !important; }

/* Replace the gradient ::before tint with a 2px colored top hairline */
.kpi::before {
  content: '' !important;
  position: absolute !important;
  inset: 0 0 auto 0 !important;
  height: 2px !important;
  width: auto !important;
  background: var(--kpi-status-color, transparent) !important;
  opacity: 0.9 !important;
  z-index: 1 !important;
  pointer-events: none !important;
}
/* Hide the left-side accent strip */
.kpi::after { display: none !important; }

.kpi:hover {
  transform: translateY(-2px);
  border-color: var(--kpi-status-color, var(--mob-border-strong)) !important;
  box-shadow: 0 12px 32px rgba(0,0,0,0.45) !important;
}

/* Text inside KPI cards on dark bg */
.kpi-label {
  color: var(--muted-2) !important;
  font-family: 'JetBrains Mono', 'SF Mono', ui-monospace, monospace !important;
  font-size: 11px !important;
  letter-spacing: 0.14em !important;
  text-transform: uppercase !important;
  font-weight: 600 !important;
}
.kpi-value { color: var(--text) !important; }
.kpi-value-units {
  color: var(--muted) !important;
  font-family: 'JetBrains Mono', ui-monospace, monospace !important;
  letter-spacing: 0.12em !important;
}
.kpi-change-sub { color: var(--muted) !important; }

/* DRAW / BUILD / UP / DOWN pill — now an outlined pill matching status */
.kpi-tag {
  background: transparent !important;
  color: var(--kpi-status-color, var(--muted)) !important;
  border: 1px solid var(--kpi-status-color, var(--muted)) !important;
  font-family: 'JetBrains Mono', 'SF Mono', ui-monospace, monospace !important;
  font-size: 10.5px !important;
  letter-spacing: 0.16em !important;
  font-weight: 600 !important;
  border-radius: 4px !important;
  padding: 3px 9px !important;
}

/* Change indicator inherits the status color */
.kpi-change { color: var(--kpi-status-color, var(--muted)) !important; }

/* Position pill (e.g., "Near 5Y high") — keep its own coloring but on dark bg */
.kpi-position {
  font-family: 'JetBrains Mono', ui-monospace, monospace !important;
  letter-spacing: 0.12em !important;
}
"""


# ─── MOB landing — hero + 3-card layout CSS ──────────────────────────
MOB_LANDING_CSS = """
.title-block { padding: 14px 0 30px; }
.title-block h1 {
  margin: 0; font-size: 84px; font-weight: 600;
  letter-spacing: -0.035em; line-height: 0.98; color: var(--text);
  font-family: 'Inter', system-ui, sans-serif;
}
@media (max-width: 900px)  { .title-block h1 { font-size: 56px; } }
@media (max-width: 560px)  { .title-block h1 { font-size: 40px; } }
.title-block .tagline {
  margin-top: 18px;
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 12px; letter-spacing: 0.12em; text-transform: uppercase;
  color: var(--muted);
}
.title-block .tagline strong { color: var(--mob-text-dim); font-weight: 500; }

.mob-hero {
  position: relative; overflow: hidden;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 14px; padding: 24px 28px 26px;
  margin-bottom: 44px;
}
.mob-hero::before {
  content: ''; position: absolute; left: 0; right: 0; top: 0; height: 1px;
  background: linear-gradient(90deg, transparent 0%, rgba(245,165,36,0.55) 30%, rgba(94,234,212,0.5) 70%, transparent 100%);
}
.mob-hero-head {
  display: flex; justify-content: space-between; align-items: center;
  gap: 16px; flex-wrap: wrap; margin-bottom: 14px;
}
.mob-hero-left { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.mob-eyebrow {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 11px; font-weight: 600;
  letter-spacing: 0.22em; text-transform: uppercase;
  color: var(--accent);
}
.mob-pill-ai {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 10.5px; font-weight: 600;
  letter-spacing: 0.18em; text-transform: uppercase;
  padding: 4px 10px; border-radius: 4px;
  color: var(--mob-ai);
  background: rgba(94,234,212,0.08);
  border: 1px solid rgba(94,234,212,0.35);
}
.mob-hero-stamp {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 11px; letter-spacing: 0.16em; text-transform: uppercase;
  color: var(--muted);
}
.mob-hero-stamp .sep { color: var(--mob-border-strong); margin: 0 8px; }
.mob-hero-stamp strong { color: var(--mob-text-dim); font-weight: 500; }
.mob-hero-body { font-size: 15px; line-height: 1.65; color: var(--mob-text-dim); }
.mob-hero-body strong { color: var(--text); font-weight: 600; }
.mob-hero-body p { margin: 0 0 12px; }
.mob-hero-body p:last-child { margin-bottom: 0; }

.mob-section-h {
  font-size: 22px; font-weight: 600;
  letter-spacing: -0.012em; color: var(--text);
  margin: 0 0 18px;
  font-family: 'Inter', system-ui, sans-serif;
}

.mob-cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 18px; }
@media (max-width: 1080px) { .mob-cards { grid-template-columns: 1fr; } }
.mob-card {
  position: relative; overflow: hidden;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 12px; padding: 20px 22px 18px;
  text-decoration: none; color: inherit;
  display: flex; flex-direction: column; gap: 14px;
  transition: transform 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease;
}
.mob-card::before {
  content: ''; position: absolute; left: 0; right: 0; top: 0; height: 2px;
  background: var(--mob-card-accent, var(--muted));
  opacity: 0.85;
}
.mob-card:hover {
  transform: translateY(-2px);
  border-color: var(--mob-card-accent, var(--mob-border-strong));
  box-shadow: 0 12px 32px rgba(0,0,0,0.45);
}
.mob-card--draw  { --mob-card-accent: #ef4444; }
.mob-card--build { --mob-card-accent: #22c55e; }
.mob-card-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.mob-card-eyebrow {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 11px; font-weight: 600;
  letter-spacing: 0.18em; text-transform: uppercase;
  color: var(--accent);
}
.mob-card-eyebrow .num { opacity: 0.55; margin-right: 6px; }
.mob-card-pill {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 10.5px; font-weight: 600;
  letter-spacing: 0.16em; text-transform: uppercase;
  padding: 4px 10px; border-radius: 4px;
  border: 1px solid var(--mob-card-accent, var(--muted));
  color: var(--mob-card-accent, var(--muted));
  background: rgba(255,255,255,0.01);
  white-space: nowrap;
}
.mob-card-sub {
  font-size: 14px; color: var(--muted-2); line-height: 1.4;
}
.mob-card-value-row {
  display: flex; align-items: baseline; justify-content: space-between;
  gap: 12px; margin-top: 2px;
}
.mob-card-value {
  font-size: 44px; font-weight: 600; letter-spacing: -0.025em;
  color: var(--text); line-height: 1;
  font-variant-numeric: tabular-nums;
  display: flex; align-items: baseline; gap: 8px;
  font-family: 'Inter', system-ui, sans-serif;
}
.mob-card-value .unit {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 11px; font-weight: 500; color: var(--muted);
  letter-spacing: 0.14em; text-transform: uppercase;
}
.mob-card-change {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 12px; font-weight: 600; letter-spacing: 0.04em;
  color: var(--mob-card-accent, var(--muted));
  font-variant-numeric: tabular-nums; white-space: nowrap;
}
.mob-card-change--neutral { color: var(--mob-text-dim) !important; }
.mob-card-spark {
  height: 64px; width: 100%;
  background: rgba(0,0,0,0.25);
  border-radius: 6px;
  padding: 6px 4px 4px;
}
.mob-card-spark svg { width: 100%; height: 100%; display: block; }
.mob-card-stats {
  display: grid; grid-template-columns: 1fr 1fr; gap: 8px 18px;
  padding-top: 12px; border-top: 1px solid var(--border-soft);
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.mob-stat {
  display: flex; justify-content: space-between; align-items: baseline; gap: 8px;
}
.mob-stat .lbl { color: var(--muted); letter-spacing: 0.04em; }
.mob-stat .val { color: var(--mob-text-dim); font-weight: 500; }
.mob-stat .val.up   { color: #22c55e; }
.mob-stat .val.down { color: #ef4444; }
"""


def _mob_spark_svg(values, color='#7c8593', w=320, h=56):
    """Produce a lightweight inline SVG sparkline (area + line) from a numeric series."""
    if not values or len(values) < 2:
        return ''
    mn = min(values); mx = max(values); rng = (mx - mn) or 1.0
    step = w / (len(values) - 1)
    pts = []
    for i, v in enumerate(values):
        x = i * step
        y = h - ((v - mn) / rng) * h * 0.9 - h * 0.05
        pts.append((x, y))
    path = 'M' + ' L'.join(f'{x:.1f},{y:.1f}' for x, y in pts)
    fill_path = path + f' L {w:.1f},{h:.1f} L 0,{h:.1f} Z'
    fid = 'g' + hex(abs(hash(tuple(round(v, 4) for v in values))))[2:8]
    return (
        f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none">'
        f'<defs><linearGradient id="{fid}" x1="0" x2="0" y1="0" y2="1">'
        f'<stop offset="0%" stop-color="{color}" stop-opacity="0.32"/>'
        f'<stop offset="100%" stop-color="{color}" stop-opacity="0"/>'
        f'</linearGradient></defs>'
        f'<path d="{fill_path}" fill="url(#{fid})"/>'
        f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.6" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'</svg>'
    )


# Anthropic API key for LLM-generated commentary.
#
# Resolution order:
#   1. ANTHROPIC_API_KEY environment variable (set in
#      com.tx2modern.eia-watch.plist EnvironmentVariables, or in the shell
#      when running manually).
#   2. A single-line file at HERE/.env.local containing the key, or a
#      KEY=value line of the form ANTHROPIC_API_KEY=sk-ant-... (also
#      supports EIA_API_KEY=... on its own line).
#
# The key is intentionally NOT hardcoded — a literal in this file was
# auto-revoked by Anthropic's secret scanner after the repo was pushed to
# GitHub, which is exactly what caused the May 2026 commentary stall.
# .env.local is excluded from eia-push.sh's FILES list, so it never leaves
# this machine.

def _load_env_local():
    """Load KEY=value pairs from HERE/.env.local into os.environ if present.
    Silent no-op if the file is missing. Lines starting with # are comments.
    A bare key on its own (no '=') is treated as ANTHROPIC_API_KEY for
    backwards-compatible convenience."""
    path = os.path.join(HERE, '.env.local')
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    k, _, v = line.partition('=')
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    # Don't clobber explicit env (env > file).
                    os.environ.setdefault(k, v)
                elif line.startswith('sk-ant-'):
                    os.environ.setdefault('ANTHROPIC_API_KEY', line)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f'  → warning: could not parse .env.local ({e})')

_load_env_local()


def _ensure_ssl_certs():
    """Point Python at certifi's CA bundle if no SSL_CERT_FILE is set.

    macOS Python builds from python.org, Homebrew, and pyenv ship their own
    OpenSSL but do NOT trust the system keychain, so urllib HTTPS calls fail
    with 'unable to get local issuer certificate' until you either run
    Python's bundled Install Certificates.command or point SSL_CERT_FILE at
    a real bundle. We pick the latter — certifi is a tiny pure-Python
    package whose only job is to ship Mozilla's CA bundle — so future
    rotations don't require any cert plumbing.

    No-op if SSL_CERT_FILE is already set (env / .env.local override wins)
    or if certifi isn't installed (Apple's /usr/bin/python3, which uses the
    system keychain natively, doesn't need this).
    """
    if os.environ.get('SSL_CERT_FILE'):
        return
    try:
        import certifi
        os.environ['SSL_CERT_FILE'] = certifi.where()
    except ImportError:
        pass

_ensure_ssl_certs()


ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ANTHROPIC_MODEL = os.environ.get('EIA_CLAUDE_MODEL', 'claude-opus-4-8')

if not ANTHROPIC_API_KEY:
    # Loud, single-line warning at import time so a missing key is obvious
    # in eia-watch.log instead of producing a silently-stale brief.
    print('  → WARNING: ANTHROPIC_API_KEY not set — all LLM commentary will '
          'fall back to the last cached brief (no fresh generation).')


def load_data():
    with open(DATA_FILE) as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────────────────────
# Wiki context loader — injects institutional market intelligence into prompts
# ─────────────────────────────────────────────────────────────────────────────

WIKI_CONTEXT_PATH = os.path.join(HERE, 'wiki_context.json')


def _load_wiki_context():
    """Load the compact wiki context block for prompt injection.

    Returns the context text string, or an empty string if wiki_context.json
    is not present or is empty. Fails silently so a missing wiki never breaks
    the dashboard build.
    """
    if not os.path.exists(WIKI_CONTEXT_PATH):
        return ''
    try:
        with open(WIKI_CONTEXT_PATH, encoding='utf-8') as f:
            data = json.load(f)
        text = (data.get('context_text') or '').strip()
        if text:
            src = data.get('source_count', '?')
            gen = (data.get('generated_at') or '')[:10]
            print(f'  → wiki context loaded ({len(text):,} chars, {src} sources, generated {gen})')
        return text
    except Exception as e:
        print(f'  → wiki context load failed ({e}), skipping')
        return ''


# ─────────────────────────────────────────────────────────────────────────────
# Narrative generation: Anthropic API if key present, otherwise rich template
# ─────────────────────────────────────────────────────────────────────────────

NARRATIVE_PROMPT = """You are a senior crude oil & petroleum products analyst writing
the weekly market-read for an EIA Weekly Petroleum Status Report dashboard.
Voice: tight, data-anchored, specific. Use real numbers from the data. Cite
4-week averages and trajectory where meaningful. Reference WTI, Cushing tank
levels, NY Harbor RBOB, Colonial Pipeline, hurricane season risk, ULSD cracks,
PADD III exports — only where actually relevant to what the numbers show.

CUSHING FLOOR — GET THIS RIGHT:
- The Cushing operational floor is ~25 mb. If Cushing is BELOW 25 mb, say it is
  "already below the operational floor" — NEVER "approaching" or "dangerously close
  to" the floor. Below means below. Draws when sub-floor deepen the stress further.

NYH RBOB CASH STRUCTURE — GET THIS RIGHT:
- NYH RBOB backwardation (spot > forward) means the market is paying a premium for
  prompt barrels in New York — this PULLS barrels north via Colonial Pipeline,
  INCREASING supply into PADD I and ACCELERATING any inventory build.
- NYH RBOB contango (spot < forward) means no urgency for prompt barrels — Colonial
  nominations stay light, supply stays lean, and PADD I inventory STALLS or DRAWS.
- DO NOT WRITE: "backwardation would pull barrels north and stall the build" — that
  is backwards. More barrels arriving builds inventory, not stalls it.
- ARITHMETIC CHECK: backwardation → more supply → build accelerates.
  Contango → less supply → build stalls or reverses.

BUILD/DRAW vs DEMAND LOGIC — GET THIS RIGHT:
- Soft/weak demand CAUSES builds. A build alongside soft demand is EXPECTED, not
  surprising. NEVER write "built despite soft demand" — "despite" implies the build
  overcame a headwind, but low demand IS the tailwind for a build.
- Strong/firm demand CAUSES draws. A draw alongside firm demand is EXPECTED.
- Correct framing: "distillate built X mb on soft Y mb/d demand" (demand is the cause)
  or "distillate built X mb as weak Y mb/d demand failed to absorb supply."
- "Despite" is only correct when a build occurs in the face of a BULLISH factor
  (e.g., high demand, export pull, refinery cuts) — or when a draw occurs despite
  a BEARISH factor (e.g., weak demand, high imports). Match the word to the logic.
- ARITHMETIC CHECK: soft demand + normal supply → build. Firm demand + normal supply → draw.

SELF-CONTAINED TEXT — NO CROSS-REFERENCES:
- NEVER write "see below", "see above", "as shown below", "as noted above", "as discussed
  below", or any phrase that references another part of the page. This narrative block
  renders in isolation — there is no "below" or "above" visible to the reader.
- If you want to mention a topic (e.g. Russia supply disruption, export data), state the
  fact directly in the sentence rather than pointing the reader elsewhere.

{wiki_context_block}

Return STRICT JSON with this exact shape (no markdown, no commentary outside JSON):

{{
  "market_read": "...4 short paragraphs separated by \\\\n. Use <strong>Headline:</strong>, <strong>Driver:</strong>, <strong>Trade flows:</strong>, <strong>Watch next week:</strong> as paragraph leads...",
  "padd": {{
    "padd1": "...3-4 sentences (~55-85 words). Start with the most important driver in <strong>tags</strong>, cover the secondary product move and refinery utilization with specific numbers, then ALWAYS end with a separate sentence led by <strong>Watch:</strong> flagging the single most important thing to monitor next week. Reference NY Harbor RBOB structure and Colonial Pipeline nominations where relevant...",
    "padd2": "...same format, and ALWAYS end with a <strong>Watch:</strong> sentence. Cushing is in PADD II — the operational floor is ~25 mb. CRITICAL: if Cushing is BELOW 25 mb, say it is already below the operational floor — do NOT say 'approaching' or 'dangerously close to' the floor. Below means below. Reference WTI prompt backwardation and deliverability stress when Cushing is sub-floor...",
    "padd3": "...same format, and ALWAYS end with a <strong>Watch:</strong> sentence. Gulf Coast is the export hub & refining center; reference hurricane season (June 1 start), LOOP sour differentials and Asian crude buying pace when refinery util is near peak...",
    "padd4": "...same format, and ALWAYS end with a <strong>Watch:</strong> sentence. Rocky Mountain is small but volatile on turnarounds; reference Bakken/DJ basin when relevant...",
    "padd5": "...same format, and ALWAYS end with a <strong>Watch:</strong> sentence. West Coast is CARB-spec isolated; reference LA Basin margins, Phillips 66 Rodeo conversion when relevant..."
  }}
}}

DATA (week ending {report_date}):
{data}
"""

TRADING_CALLS_PROMPT = """You are a senior petroleum derivatives trader writing concise, actionable trading calls from a weekly EIA inventory report.

Rules:
- Identify 2–4 specific trade opportunities directly supported by the data.
- Each call must be a concrete, executable trade — not a general observation.
- Direction must be one of: "long", "short", or "spread".
- Instrument: the exact contract or spread to trade (e.g. "WTI M1–M2 Spread", "NYH RBOB Cash Basis", "Jet Crack vs HO").
- Setup: one tight sentence (≤15 words) naming the structural reason for the trade.
- Rationale: 2–3 sentences with specific numbers from the data. State what the data shows, why it creates the opportunity, and the key risk or catalyst to watch.
- Do NOT restate general market conditions as a trading call. Every call needs a specific entry thesis.
- Do NOT fabricate numbers. Use only figures present in the data.

Return STRICT JSON (no markdown, no commentary):

{{
  "calls": [
    {{
      "direction": "long" | "short" | "spread",
      "instrument": "...",
      "setup": "...",
      "rationale": "..."
    }}
  ]
}}

DATA (week ending {report_date}):
{data}
"""


def _generate_trading_calls_via_claude(ctx):
    prompt = TRADING_CALLS_PROMPT.format(
        report_date=ctx['reportDate'],
        data=json.dumps(ctx, indent=2),
    )
    raw = _call_claude(prompt, max_tokens=1500)
    start = raw.find('{')
    end = raw.rfind('}')
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f'no JSON in trading calls response: {raw[:200]}')
    parsed = json.loads(raw[start:end + 1])
    return parsed.get('calls', [])


def generate_trading_calls(ctx):
    try:
        return _generate_trading_calls_via_claude(ctx)
    except Exception as e:
        print(f'  ⚠ trading calls generation failed: {e}')
        return []


def _extract_first_json_object(raw):
    """Pull the first balanced top-level JSON object out of a Claude response.

    Haiku occasionally returns two JSON objects back-to-back (or wraps the
    real one in a thinking-style preamble), which makes a naive
    ``raw[raw.find('{'):raw.rfind('}')+1]`` slice fail with
    ``json.JSONDecodeError: Extra data``. This walks the string with a brace
    counter (respecting string literals + escapes) so we only return the
    first complete object. Returns the JSON substring or ``None``.
    """
    start = raw.find('{')
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return raw[start:i + 1]
    return None


def _call_claude(prompt, model=None, max_tokens=1500):
    """Call Anthropic's Messages API. Returns text content or raises."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError('no ANTHROPIC_API_KEY')
    body = json.dumps({
        'model': model or ANTHROPIC_MODEL,
        'max_tokens': max_tokens,
        'messages': [{'role': 'user', 'content': prompt}],
    }).encode('utf-8')
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=body,
        headers={
            'content-type': 'application/json',
            'x-api-key': ANTHROPIC_API_KEY,
            'anthropic-version': '2023-06-01',
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read())
    return resp['content'][0]['text']


def _generate_via_claude(ctx):
    wiki_context = _load_wiki_context()
    wiki_context_block = (
        "INSTITUTIONAL MARKET CONTEXT (multi-week knowledge base — use to add depth "
        "and continuity; do not contradict current session data):\n" + wiki_context
        if wiki_context else ''
    )
    prompt = NARRATIVE_PROMPT.format(
        report_date=ctx['reportDate'],
        data=json.dumps(ctx, indent=2),
        wiki_context_block=wiki_context_block,
    )
    raw = _call_claude(prompt, max_tokens=4000)
    # Extract the JSON object — match from first { to last }
    start = raw.find('{')
    end = raw.rfind('}')
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f'no JSON object found in response: {raw[:200]}')
    return json.loads(raw[start:end + 1])


_CROSS_REF_RE = re.compile(
    r'\b(see\s+(below|above|the\s+(chart|table|graph|section|figure|data)\s*(below|above)?)'
    r'|as\s+(shown|noted|discussed|detailed|described|mentioned)\s+(below|above)'
    r'|as\s+shown\s+in\s+the\s+(chart|table|graph|figure)\s*(below|above)?'
    r'|refer\s+to\s+the\s+(chart|table|graph|figure|section)\s*(below|above)?'
    r'|in\s+the\s+(chart|table|graph|figure)\s*(below|above))',
    re.IGNORECASE,
)

def _strip_cross_refs(text: str) -> str:
    """Remove phrases like 'see below', 'as shown above', etc. from LLM output."""
    if not text:
        return text
    return _CROSS_REF_RE.sub('', text)


def _fallback_market_read(ctx):
    us = ctx['us']
    trade = ctx['trade_4wk_mbd']
    demand = ctx['demand_4wk_mbd']
    cushing_traj = us['cushing']['trajectory_mb']
    cushing_5wk_chg = cushing_traj[-1][1] - cushing_traj[0][1]

    def sgn(v): return f'+{v:.2f}' if v >= 0 else f'{v:.2f}'

    headline = (
        f"<strong>Headline:</strong> Crude {sgn(us['crude']['chg'])} mb and gasoline {sgn(us['gas']['chg'])} mb "
        f"drew while distillate {('built' if us['dist']['chg']>=0 else 'drew')} {abs(us['dist']['chg']):.2f} mb "
        f"and jet {('built' if us['jet']['chg']>=0 else 'drew')} {abs(us['jet']['chg']):.2f} mb. "
        f"U.S. refinery utilization climbed to {us['util']['last']:.1f}% ({sgn(us['util']['chg'])} pp w/w)."
    )

    # Find dominant util mover
    padds = ctx['padds']
    biggest_util = max(padds.items(), key=lambda kv: abs(kv[1]['util_chg']))
    pid, pdata = biggest_util
    padd_label = {'padd1':'PADD I','padd2':'PADD II','padd3':'PADD III','padd4':'PADD IV','padd5':'PADD V'}[pid]
    driver = (
        f"<strong>Driver:</strong> {padd_label} refinery utilization {('snapped' if abs(pdata['util_chg'])>=5 else 'moved')} "
        f"{sgn(pdata['util_chg'])} pp to {pdata['util_last']:.1f}%, the headline supply-side mover this week. "
        f"<strong>Cushing fell to {us['cushing']['last']:.1f} mb</strong> ({sgn(us['cushing']['chg'])} mb w/w; "
        f"{sgn(cushing_5wk_chg)} mb over 5 weeks) — operational tank-bottom is ~25 mb. "
        f"Refiner crude inputs {('climbed' if ctx['refiner_inputs_chg_mbd']>=0 else 'eased')} "
        f"{abs(ctx['refiner_inputs_chg_mbd']):.2f} mb/d to {ctx.get('crude_inputs_mbd', 0):.2f} mb/d."
    )

    trade_swing = trade['crude_exp'] - trade['crude_exp_prev']
    trade_para = (
        f"<strong>Trade flows:</strong> Crude exports averaged {trade['crude_exp']:.2f} mb/d on 4-week basis "
        f"({sgn(trade_swing)} mb/d vs prior 4-week window — "
        f"{'multi-year high pace' if trade['crude_exp']>=5.0 else 'firming'}) while crude imports fell to "
        f"{trade['crude_imp']:.2f} mb/d ({sgn(trade['crude_imp']-trade['crude_imp_prev'])} mb/d). "
        f"Gasoline demand 4-wk avg {demand['gasoline']:.2f} mb/d ({sgn(demand['gasoline']-demand['gasoline_prev'])} mb/d w/w) "
        f"as driving season ramps."
    )

    watch = (
        f"<strong>Watch next week:</strong> Memorial Day gasoline pull, Cushing trajectory vs 25 mb operational floor "
        f"({us['cushing']['last']:.1f} mb currently — within {us['cushing']['last']-25:.1f} mb of the line), "
        f"June 1 Atlantic hurricane season start with PADD III running near {padds['padd3']['util_last']:.1f}% capacity, "
        f"and whether crude exports can sustain the {trade['crude_exp']:.1f} mb/d pace."
    )
    return '\n'.join([headline, driver, trade_para, watch])


def _fallback_padd_narrative(pid, ctx):
    p = ctx['padds'][pid]
    name = {'padd1':'East Coast','padd2':'Midwest','padd3':'Gulf Coast','padd4':'Rocky Mountain','padd5':'West Coast'}[pid]

    def sgn(v): return f'+{v:.2f}' if v >= 0 else f'{v:.2f}'
    def big(v, thresh=1.0): return abs(v) >= thresh

    # Identify the dominant move
    moves = [('Crude', p['crude_chg']), ('Gasoline', p['gas_chg']), ('Distillate', p['dist_chg']), ('Jet', p['jet_chg'])]
    moves.sort(key=lambda m: -abs(m[1]))
    dominant = moves[0]
    second = moves[1]

    lead = f"<strong>{dominant[0]} {('built' if dominant[1]>=0 else 'drew')} {abs(dominant[1]):.2f} mb</strong>"
    util_part = ''
    if abs(p['util_chg']) >= 0.5:
        util_part = f", refinery utilization {('climbed' if p['util_chg']>0 else 'eased')} {sgn(p['util_chg'])} pp to {p['util_last']:.1f}%"
    elif p['util_last']:
        util_part = f"; refinery utilization steady at {p['util_last']:.1f}%"

    second_part = ''
    if abs(second[1]) >= 0.3:
        second_part = f" {second[0]} also {('built' if second[1]>=0 else 'drew')} {abs(second[1]):.2f} mb."

    # PADD-specific colour
    extra = ''
    if pid == 'padd1':
        extra = ' Watch NY Harbor RBOB structure and Colonial Pipeline nominations into Memorial Day.'
    elif pid == 'padd2':
        cushing = ctx['us']['cushing']
        cl = cushing['last']; cc = cushing['chg']
        if cl < 25:
            extra = f" <strong>Cushing now at {cl:.1f} mb</strong> ({sgn(cc)} mb w/w) — already {25-cl:.1f} mb BELOW the ~25 mb operational floor; delivery-point stress is active, watch WTI prompt backwardation."
        else:
            extra = f" <strong>Cushing now at {cl:.1f} mb</strong> ({sgn(cc)} mb w/w) — {cl-25:.1f} mb above the ~25 mb operational floor; watch WTI prompt structure."
    elif pid == 'padd3':
        extra = ' Hurricane risk window opens June 1 with refineries near peak; watch LOOP sour differentials and Asian crude buying pace.'
    elif pid == 'padd4':
        extra = ' Bakken/DJ basin differentials should track PADD IV crude demand recovery.'
    elif pid == 'padd5':
        extra = ' CARB-spec isolation, ongoing Phillips 66 Rodeo conversion impact, and LA Basin margins remain the swing factors.'

    return f'{lead}{util_part}.{second_part}{extra}'


# ─────────────────────────────────────────────────────────────────────────────
# Mini-chart captions — generate brief commentary for each small chart
# ─────────────────────────────────────────────────────────────────────────────

MINI_CAPTION_PROMPT = """You are a senior crude oil & petroleum products analyst writing
weekly market-read commentary for {n} EIA seasonal charts. Each commentary should be
2–3 sentences (~45–70 words) — substantive enough to actually inform a trader, not just
restate the number. Use the analyst voice from a desk note: data-anchored, specific,
mention real market dynamics where they apply.

Note: most charts are weekly stock levels in million barrels (mb). Items where
commodity == 'util' are refinery utilization (% of operable capacity); write those
in pp w/w terms and reference turnaround season, peak summer running, hurricane
risk, regional refining configuration.

For each chart, weave in 2–3 of:
- The specific WoW change (mb) and what's driving it (refinery utilization, exports, demand pull, turnarounds)
- Position vs 5-year band and what that signals (above/below normal, near floor/ceiling)
- A relevant market hook tied to the region: NY Harbor RBOB / Colonial Pipeline (PADD I);
  Cushing tank-bottom risk (~25 mb operational floor) for crude PADD II;
  Gulf Coast exports, hurricane season (June 1), LOOP sour, Asian crude pulls (PADD III);
  Bakken/DJ basin or turnaround cycle (PADD IV);
  CARB-spec isolation, LA Basin margins, Phillips 66 Rodeo conversion (PADD V);
  WTI delivery hub structure, basis volatility (Cushing).
- A "watch" or "implication" hook where relevant.

Lead each commentary with <strong>tags</strong> around the headline number or driver phrase.
Use ONE <strong> per commentary. Plain text — no markdown, no headers, no bullets.

Return STRICT JSON mapping chart_id → commentary, no markdown wrappers around the JSON:

{{
  "crude_us": "<strong>Crude drew 4.3 mb to 452.9 mb</strong> as refinery utilization climbed to 91.7% (+1.6 pp) and exports surged to 5.37 mb/d 4-wk avg. Stocks now track just below the 5-yr average for week 19 (454 mb), with the export bid the dominant driver into Memorial Day. Watch whether export pace holds above 5 mb/d as PADD III runs near capacity.",
  ...
}}

CHARTS:
{data}
"""


def _fallback_mini_caption(c):
    chg = c['chg']; last = c['last']; band_lo = c['band_lo']; band_hi = c['band_hi']
    in_band = 'in line with' if band_lo <= last <= band_hi else ('above' if last > band_hi else 'below')
    is_util = c.get('commodity') == 'util'
    unit = '%' if is_util else ' mb'
    chg_unit = ' pp w/w' if is_util else ' mb w/w'
    direction = ('eased' if is_util else 'drew') if chg < 0 else (('climbed' if is_util else 'built') if chg > 0 else 'flat')
    chg_str = f'{abs(chg):.2f}{chg_unit}' if chg != 0 else 'flat w/w'
    return f'<strong>{last:.1f}{unit}</strong>, {direction} {chg_str}. Tracks {in_band} the 5-yr band ({band_lo:.0f}{unit}-{band_hi:.0f}{unit}).'


def _cache_path():
    return os.path.join(HERE, '.mini_captions_cache.json')


def _load_cache():
    try:
        with open(_cache_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache):
    with open(_cache_path(), 'w') as f:
        json.dump(cache, f, indent=2)


def _context_hash(c):
    """Stable hash of a chart's context — so cache invalidates when data changes."""
    import hashlib
    key = json.dumps({k: v for k, v in c.items() if k != 'region'}, sort_keys=True)
    return hashlib.md5(key.encode()).hexdigest()


def generate_mini_captions(mini_contexts):
    """LLM commentary for each mini-chart. Per-commodity batched, cached by context hash.
    Falls back to template per-chart on any failure."""
    cache = _load_cache()
    captions = {}
    needed = []
    # First, hit cache
    for c in mini_contexts:
        h = _context_hash(c)
        if cache.get(c['id'], {}).get('hash') == h and cache[c['id']].get('text'):
            captions[c['id']] = cache[c['id']]['text']
        else:
            needed.append(c)

    if not needed:
        print(f'  → mini-captions: all {len(mini_contexts)} from cache (no API calls)')
    elif ANTHROPIC_API_KEY and os.environ.get('MINI_CAPTION_LLM', '1') == '1':
        from collections import defaultdict
        groups = defaultdict(list)
        for c in needed:
            groups[c['commodity']].append(c)
        for commodity, items in groups.items():
            try:
                print(f'  → mini-captions: {commodity} ({len(items)} charts via API)...')
                prompt = MINI_CAPTION_PROMPT.format(
                    n=len(items), data=json.dumps(items, indent=2),
                )
                # Haiku is much faster than Sonnet and handles this analyst-style prompt fine
                raw = _call_claude(prompt, max_tokens=2500)
                start, end = raw.find('{'), raw.rfind('}')
                if start >= 0 and end > start:
                    parsed = json.loads(raw[start:end + 1])
                    for cid, text in parsed.items():
                        captions[cid] = text
                        for c in items:
                            if c['id'] == cid:
                                cache[cid] = {'hash': _context_hash(c), 'text': text}
                                break
                    _save_cache(cache)  # incremental save: durable across bash sessions
                    print(f'    {commodity}: saved {len(parsed)} captions to cache')
            except Exception as e:
                print(f'    {commodity} failed ({e}), template fallback for this group')

    # Fill in any missing via template
    for c in mini_contexts:
        if c['id'] not in captions or not captions[c['id']]:
            captions[c['id']] = _fallback_mini_caption(c)
    return captions


def generate_narratives(ctx):
    """Return {'market_read': str, 'padd': {pid: str, ...}}. Try Claude first, fall back to templates."""
    if ANTHROPIC_API_KEY:
        try:
            print('  → calling Anthropic API for narratives...')
            out = _generate_via_claude(ctx)
            if 'market_read' in out and 'padd' in out and len(out['padd']) >= 5:
                out['market_read'] = _strip_cross_refs(out['market_read'])
                print('  → AI-generated narratives received')
                return out
            print('  → API returned malformed JSON, falling back to template')
        except Exception as e:
            print(f'  → API call failed ({e}), falling back to template')
    # Fallback templates
    return {
        'market_read': _fallback_market_read(ctx),
        'padd': {pid: _fallback_padd_narrative(pid, ctx) for pid in ['padd1', 'padd2', 'padd3', 'padd4', 'padd5']},
    }



def iso_week(date_str):
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    iso = dt.isocalendar()
    return iso.year, iso.week


def group_by_year_week(points):
    """Return {year_int: {week_int: value}}"""
    out = {}
    for d, v in points:
        y, w = iso_week(d)
        out.setdefault(y, {})[w] = v
    return out


def fill_weeks(year_week_map, year, n_weeks=52):
    """Return a 52-length array for the given year, filling missing weeks via
    nearest non-null neighbor (forward-fill, then back-fill)."""
    weeks = year_week_map.get(year, {})
    arr = [weeks.get(w + 1) for w in range(n_weeks)]
    # Forward fill
    last = None
    for i in range(n_weeks):
        if arr[i] is not None:
            last = arr[i]
        elif last is not None:
            arr[i] = last
    # Back fill
    nxt = None
    for i in range(n_weeks - 1, -1, -1):
        if arr[i] is not None:
            nxt = arr[i]
        elif nxt is not None:
            arr[i] = nxt
    return arr


def partial_year(year_week_map, year, max_weeks=52):
    """Return an array of values from week 1 through the latest week with data,
    no null filling (just truncate at last valid)."""
    weeks = year_week_map.get(year, {})
    if not weeks:
        return []
    last_w = max(weeks.keys())
    return [weeks.get(w + 1) for w in range(min(last_w, max_weeks))]


def to_mb(v):
    """thousand barrels -> million barrels"""
    return None if v is None else round(v / 1000.0, 3)


def to_mbd(v):
    """thousand bbl/day stays thousand bbl/day but we display as mb/d"""
    return None if v is None else round(v / 1000.0, 3)


def fmt_num_js(v, decimals=3):
    if v is None:
        return 'null'
    return f'{v:.{decimals}f}'


def js_array(arr, decimals=3):
    return '[' + ','.join(fmt_num_js(v, decimals) for v in arr) + ']'


def build_hist_block(series, current_year, units='mb'):
    """Build a {year: [52 weekly values]} block from a series's data points.
    Years 2021-2025: full 52 weeks (filled).
    current_year: partial array up to last observed week."""
    converter = to_mb if units == 'mb' else (lambda v: v)
    pts = [(d, converter(v)) for d, v in series['data']]
    ywm = group_by_year_week(pts)
    block = {}
    for y in range(2021, current_year):
        block[str(y)] = fill_weeks(ywm, y)
    block[str(current_year)] = partial_year(ywm, current_year)
    return block


def compute_4wk_avg_kbd(series, n=4):
    """Last 4 weekly observations averaged (already in kbd)."""
    data = series['data']
    if len(data) < n:
        return None
    vals = [v for _, v in data[-n:]]
    return sum(vals) / len(vals)


def compute_4wk_avg_prev(series, n=4):
    """Previous 4-week window."""
    data = series['data']
    if len(data) < 2 * n:
        return None
    vals = [v for _, v in data[-2 * n:-n]]
    return sum(vals) / len(vals)


# Extract KPI CSS/JS from the legacy stub block below at module load.
# (Avoids duplicating the ~150 lines of CSS/JS in two places.)
try:
    _src = open(__file__).read()
    _m = re.search(r'_KPI_CSS_LEGACY\s*=\s*"""([\s\S]+?)"""', _src)
    _KPI_CSS_BODY = _m.group(1) if _m else ''
    _m = re.search(r'_KPI_JS_LEGACY\s*=\s*"""([\s\S]+?)"""', _src)
    _KPI_JS_BODY = _m.group(1) if _m else ''
except Exception:
    _KPI_CSS_BODY = ''
    _KPI_JS_BODY = ''


# ─── Narrative formatting helpers ───────────────────────────────────────────
# Convert literal '\n' (backslash + n, two chars) into real newlines and wrap
# known paragraph-lead labels in <strong>…</strong> when the LLM forgets to.
_NARRATIVE_LEADS = [
    # Morning brief
    'Headline', 'Crude & products', 'Crude &amp; products',
    'Refining margins', 'Curve structure', 'News & geopolitics',
    'News &amp; geopolitics',
    # Margins
    'Driver', 'Forward Curve',
    # Curves
    'Curve Shape', 'Calendar Spreads',
    # Inventories
    'Inventories', 'Margins & Cracks', 'Margins &amp; Cracks',
    # Shared
    'Watch next week',
]

def _normalize_narrative(s):
    """Make an LLM narrative paragraph-ready for the JS splitter.

    Robustly handles three failure modes from the LLM:
    1. Literal ``\\n`` (backslash + n) instead of real newline → converted.
    2. Plain ``Headline:`` without ``<strong>`` wrapping → wrapped.
    3. All paragraph labels jammed inline on a single line without breaks →
       a newline is inserted before each lead label (after the first one).

    Leads are matched case-insensitively. Existing ``<strong>…</strong>``
    wrapping is preserved (no double-wrapping).
    """
    if not s:
        return s
    s = s.replace('\\n', '\n').strip()

    # Build one combined regex that finds any lead label in the text.
    # We need to detect occurrences whether or not they're wrapped in <strong>.
    leads_alt = '|'.join(re.escape(L) for L in _NARRATIVE_LEADS)
    # Match either: a literal "<strong>LEAD:</strong>" OR a bare "LEAD:"
    lead_re = re.compile(
        r'(<strong>\s*(?:' + leads_alt + r')\s*:\s*</strong>'
        r'|(?<![A-Za-z])(?:' + leads_alt + r')\s*:)',
        flags=re.IGNORECASE,
    )

    # Find all lead positions
    matches = list(lead_re.finditer(s))
    if not matches:
        return s

    # Rebuild the string: ensure each lead (after the first) is preceded by a
    # blank-line break, and each lead is wrapped in <strong>…</strong>.
    out = []
    cursor = 0
    for i, m in enumerate(matches):
        # Text between previous match and this match
        between = s[cursor:m.start()]
        if i == 0:
            out.append(between)
        else:
            # Strip trailing whitespace from `between` and add a single newline
            # before the lead. This collapses runs of spaces or stray newlines.
            out.append(between.rstrip() + '\n')
        token = m.group(0)
        # Normalize this lead to "<strong>Lead:</strong>"
        # Extract the label text (between <strong> and :</strong> if wrapped,
        # else between start and :)
        inner = re.sub(r'</?strong>', '', token).rstrip(':').strip()
        out.append(f'<strong>{inner}:</strong>')
        cursor = m.end()
    out.append(s[cursor:])
    return ''.join(out).strip()


MORNING_BRIEF_PROMPT = """You are writing the MORNING OIL BRIEF — the lead read a trader,
refiner, or energy analyst reaches for with their first coffee. Treat the brief as a
substantial pre-market digest, not a one-liner. Weave together the PRICE/INVENTORY DATA
with REAL MARKET NEWS from the last 24-48 hours so the reader walks away with both the
numbers AND the story behind them.

Voice: tight, data-anchored, analyst-quality — like a Goldman commodities desk note
or S&P Platts daily. Direct, not academic. Confident, not hedgy. Reference real
geopolitics, OPEC moves, refinery outages, weather, demand surveys, etc. when the
headlines support it. Synthesize, don't summarize headline-by-headline.

CRITICAL — NUMERIC FIDELITY (this is non-negotiable):
- EVERY numeric figure you cite (inventory levels, draws/builds, prices, cracks,
  spreads, refinery utilization %, etc.) MUST come DIRECTLY from the JSON data
  blocks below. Do NOT round, do NOT estimate, do NOT recall similar figures
  from prior reads, do NOT invent plausible-looking placeholders.
- If a specific number is NOT present in the data, say so or omit that detail —
  never fabricate.
- Before writing any number, locate it in WEEKLY EIA INVENTORY, CRACK SPREADS,
  or CURVE SNAPSHOT JSON. The keys are explicit (e.g. `crude_chg_mb`,
  `wti_nymex_settlement`, `crack_321`). Use those values verbatim.
- Direction (build vs. draw, up vs. down) MUST come from the sign of the change
  field in the data — NOT from your guess about what news would imply.
- If your draft mentions "$68 WTI" or "458 mb crude" or "91% utilization" when
  the data says something different, you have failed. Re-read the data block
  and rewrite.

CUSHING FLOOR — GET THIS RIGHT:
- The Cushing operational floor is ~25 mb. Below this level the hub cannot
  function normally (tank bottoms, deliverability stress, basis volatility).
- If Cushing is BELOW 25 mb: say "already below the operational floor" or
  "operating below tank-bottom minimums." NEVER say "approaching," "nearing,"
  or "dangerously close to" the floor — that implies it hasn't been breached yet.
- If Cushing is ABOVE 25 mb: you may say "approaching the floor" only if it
  is within ~2 mb (i.e., 25–27 mb range).
- The direction matters: draws when already sub-floor deepen the stress;
  builds when sub-floor are relief but don't fix the problem until back above 25 mb.

BRENT–WTI SPREAD MECHANICS — GET THIS RIGHT:
- Brent–WTI = Brent price minus WTI price (positive in normal markets).
- WIDE spread (~$7+): WTI cheap vs. Brent → strong US export arb → high US exports.
- NARROW spread (~$4 or less): WTI close to Brent → weak export arb → US exports
  fall, barrels stay home, US crude builds.
- DO NOT WRITE: "narrow Brent–WTI keeps US exports competitive" — opposite.
- DO NOT WRITE: "narrow spread implies WTI upside to recapture barrels" — that
  flips the causal chain. Excess US crude from weak exports pressures WTI DOWN.
- To re-widen the spread and restore exports, WTI must FALL (or Brent must rise).

LOGICAL-CONSISTENCY CHECK:
- Every cause-and-effect claim must be economically valid. After drafting,
  re-read each "X therefore Y" claim and verify the mechanism actually flows
  that direction. If you cannot state the mechanism in one short sentence,
  rewrite or drop the claim.

{freshness_instruction}

IMPORTANT — DATA CADENCE & FRAMING:
- TODAY is {today_date}. PRICES & CRACKS reflect the prior session NYMEX settlement as of {prices_date}. Do NOT describe prices as "current" or "intraday" — they are yesterday's closing settlements.
- EIA INVENTORY DATA IS WEEKLY (Wednesday WPSR release). The figures below are for the
  week ending {inventory_date}. Always time-anchor inventory references — say
  "for the week ending {inventory_date}…" or "the latest weekly EIA report…".
  DO NOT write inventory changes as if they happened today or yesterday in the
  daily-price sense.
- NOTE: the DATA-CLOCK STATE block above tells you whether today's WPSR was JUST
  released (post_eia — call it "today's WPSR" / "this morning's report") or is a
  prior release that prices have already reacted to (synced — "last week's
  WPSR"). Follow that override.
- Use phrasing like "the report showed Cushing drew 1.7 mb" — not "Cushing
  drained another 1.7 mb" (which implies a fresh daily move).

Total length: ~350-500 words across 5 paragraphs. Each paragraph 3-5 sentences.
Use specific numbers, specific headlines, specific implications.

Return STRICT JSON, no markdown wrappers. Separate paragraphs with the two-character
escape \\\\n (backslash + n) inside the JSON string value:

{{
  "morning_brief": "5 paragraphs separated by \\\\n. Use <strong>Headline:</strong>, <strong>Crude &amp; products:</strong>, <strong>Refining margins:</strong>, <strong>Curve structure:</strong>, <strong>News &amp; geopolitics:</strong> as paragraph leads. The first paragraph (Headline) is the one-sentence-and-supporting-detail TLDR — what's the biggest story this morning. The Crude &amp; products paragraph MUST clearly attribute inventory figures to last week's WPSR (week ending {inventory_date}). The News &amp; geopolitics paragraph should weave in 4-6 of the most market-relevant headlines from the news data, citing them by paraphrasing — what happened, what it means for the market, why it matters today."
}}

WEEKLY EIA INVENTORY (WPSR — week ending {inventory_date}):
{inventory_data}

CRACK SPREADS (latest daily values, as of {prices_date}):
{margins_data}

CURVE SNAPSHOT (current shape, as of {prices_date}):
{curves_data}

RECENT MARKET HEADLINES (last 48 hours, newest first — use 4-6 of the most market-relevant):
{news_data}

{wiki_context_block}
"""


def _generate_morning_brief(inv_ctx, margins_ctx, curves_ctx, inventory_date, prices_date, news=None, freshness=None):
    """Generate the home-page "Today's Read".

    Sticky-fallback design (added 2026-05-25 after Memorial Day produced a
    near-empty brief): the underlying inputs fall into two buckets — data that
    only moves on trading days (inventory, prices, curves) and news that
    refreshes hourly. On a market holiday or weekend, news changes while
    everything else is frozen. Previously the cache key included news, so the
    cache missed every hour and the API was hammered; on any API failure we
    fell through to a worthless three-line template.

    Now we:
      1. Hit the cache on an EXACT match (data + news identical).
      2. Otherwise attempt a fresh API generation (so news can still update
         the brief mid-holiday when the API is healthy).
      3. On API failure, fall back to the most recent *good* brief that was
         ever generated — never the thin template — provided it exists.
      4. If data hasn't moved at all (same prices_date and inventory_date as
         the last good brief), we skip the API entirely and reuse the cached
         brief. The Latest Market News panel below the brief refreshes
         independently, so users still see today's headlines on the page.
    """
    import hashlib
    cache_path = os.path.join(HERE, '.morning_brief_cache.json')
    news = news or []
    # Compact news for the prompt — title + source + relative time
    news_compact = [{'h': n['title'], 's': n.get('source', ''), 'd': (n.get('datetime') or '')[:16]}
                    for n in news[:25]]
    freshness = freshness or {'state': 'synced', 'inv_through': inventory_date, 'eia_release_str': ''}

    # Post-EIA window: prices haven't reacted to today's release yet, so any
    # commentary written now is structurally incomplete (can't say "the market
    # reacted X" because the reaction hasn't printed). Suppress the brief; the
    # "Just Released" banner explains what's happening and when commentary
    # returns. The full brief regenerates after tomorrow morning's price refresh.
    if freshness.get('state') == 'post_eia':
        print('  → morning brief: suppressed (post_eia — banner carries the message)')
        # Invalidate any pre-release last_good so when state flips back to
        # synced tomorrow morning the data_unchanged shortcut can't reuse a
        # brief written BEFORE this EIA report was incorporated.
        try:
            cache_path_local = os.path.join(HERE, '.morning_brief_cache.json')
            with open(cache_path_local) as f:
                existing = json.load(f)
            existing.pop('last_good', None)
            existing.pop('last_good_prices_date', None)
            existing.pop('last_good_inventory_date', None)
            existing.pop('last_good_generated_at', None)
            existing.pop('key', None)
            existing.pop('value', None)
            with open(cache_path_local, 'w') as f:
                json.dump(existing, f)
        except Exception:
            pass
        return ''

    # Include a hash of the wiki context so updating the LLM-wiki (running
    # refresh_wiki_context.py) automatically busts the cache and forces a fresh
    # brief incorporating the new institutional knowledge.
    import hashlib as _hl
    _wiki_raw = _load_wiki_context()
    _wiki_hash = _hl.md5(_wiki_raw.encode()).hexdigest()[:8] if _wiki_raw else 'none'
    payload = {'inv': inv_ctx, 'margins': margins_ctx, 'curves': curves_ctx,
               'inv_date': inventory_date, 'prices_date': prices_date, 'news': news_compact,
               # Including freshness_state in the cache key forces a regen on the
               # synced ↔ post_eia transition, so commentary never lags reality.
               'fresh': freshness.get('state', 'synced'),
               # Bump when prompt wording changes materially — forces regen.
               'prompt_v': '2026-06-24-strip-cross-refs',
               # Wiki context hash — updating the wiki busts the brief cache.
               'wiki_hash': _wiki_hash}
    key = hashlib.md5(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    # Load existing cache so we can use it as a sticky fallback.
    cache = {}
    try:
        with open(cache_path) as f:
            cache = json.load(f)
    except Exception:
        cache = {}

    # 1. Exact-match cache hit — same data AND same news.
    if cache.get('key') == key and cache.get('value'):
        print('  → morning brief: from cache (exact match)')
        return cache['value']

    # Detect "no new market data" — prices_date and inventory_date are the
    # same as the last good brief. This is the holiday / weekend / no-WPSR
    # case. Skip the API call and reuse the cached brief.
    # Also gate on prompt_v: if the prompt wording changed materially, force
    # a regen even when data didn't move.
    last_good = cache.get('last_good')
    last_good_prices = cache.get('last_good_prices_date')
    last_good_inv = cache.get('last_good_inventory_date')
    last_good_prompt_v = cache.get('last_good_prompt_v')
    last_good_wiki_hash = cache.get('last_good_wiki_hash')
    data_unchanged = (
        last_good
        and last_good_prices == prices_date
        and last_good_inv == inventory_date
        and last_good_prompt_v == payload['prompt_v']
        and last_good_wiki_hash == _wiki_hash
    )
    if data_unchanged:
        print(
            f'  → morning brief: data unchanged since last good brief '
            f'(prices {prices_date}, inv {inventory_date}) — reusing it'
        )
        # Update the keyed cache so subsequent runs hit the exact-match path
        # without re-triggering the "data unchanged" branch.
        try:
            cache['key'] = key
            cache['value'] = last_good
            with open(cache_path, 'w') as f:
                json.dump(cache, f)
        except Exception:
            pass
        return last_good

    if ANTHROPIC_API_KEY:
        try:
            print('  → calling Anthropic API for morning brief...')
            # Include explicit weekday names so the AI never has to calculate
            # the day of week from a bare date (which it can get wrong).
            today_display = _utcnow().strftime('%A, %B %-d, %Y')
            try:
                prices_date_display = datetime.strptime(prices_date, '%Y-%m-%d').strftime('%A, %B %-d, %Y')
            except Exception:
                prices_date_display = prices_date
            wiki_ctx = _load_wiki_context()
            wiki_context_block = (
                "INSTITUTIONAL MARKET CONTEXT (multi-week knowledge base — use to "
                "add depth, continuity, and forward-looking context; do not contradict "
                "current session data):\n" + wiki_ctx
                if wiki_ctx else ''
            )
            prompt = MORNING_BRIEF_PROMPT.format(
                today_date=today_display,
                inventory_date=inventory_date,
                prices_date=prices_date_display,
                inventory_data=json.dumps(inv_ctx, indent=2),
                margins_data=json.dumps(margins_ctx, indent=2),
                curves_data=json.dumps(curves_ctx, indent=2),
                news_data=json.dumps(news_compact, indent=2),
                wiki_context_block=wiki_context_block,
                freshness_instruction=_freshness_instruction(
                    freshness.get('state', 'synced'),
                    freshness.get('inv_through', inventory_date),
                    freshness.get('eia_release_str', ''),
                ),
            )
            raw = _call_claude(prompt, max_tokens=2500)
            obj = _extract_first_json_object(raw)
            if obj:
                parsed = json.loads(obj)
                if 'morning_brief' in parsed and parsed['morning_brief'].strip():
                    value = _strip_cross_refs(parsed['morning_brief'])
                    print('  → morning brief received')
                    try:
                        # Persist both the keyed cache and the sticky
                        # last_good snapshot (with its data anchors) so any
                        # future failure falls back to real commentary.
                        with open(cache_path, 'w') as f:
                            json.dump({
                                'key': key,
                                'value': value,
                                'last_good': value,
                                'last_good_prices_date': prices_date,
                                'last_good_inventory_date': inventory_date,
                                'last_good_prompt_v': payload['prompt_v'],
                                'last_good_wiki_hash': _wiki_hash,
                                'last_good_generated_at': _utcnow().isoformat() + 'Z',
                            }, f)
                    except Exception:
                        pass
                    return value
        except Exception as e:
            print(f'  → morning brief API failed ({e}), using sticky fallback')

    # 3. API failed (or returned empty / malformed). Prefer the last good
    #    brief over the thin static template so commentary is never lost.
    if last_good:
        print(
            f'  → morning brief: reusing last good brief '
            f'(prices through {last_good_prices}, inventory through {last_good_inv})'
        )
        return last_good

    # 4. Cold start — no cache, no API. Use the thin template as last resort.
    return (
        f"<strong>Headline:</strong> Inventory week ending {inventory_date}; prices through {prices_date}.\n"
        f"<strong>Inventories:</strong> See Inventories tab for details.\n"
        f"<strong>Cracks &amp; Prices:</strong> See Cracks · Prices tab for cracks read.\n"
        f"<strong>Forward Curve:</strong> See Forward Curves tab for curve structure."
    )


def _build_landing_page(raw, kpi_data, narratives, latest_date, prices=None, news_items=None):
    """Produce the summary index.html — KPI strip + market read + section preview cards.
    All chrome (header, nav, footer) matches the inventory page."""
    report_date_str = latest_date.strftime('%B %-d, %Y')
    # Honour the EIA holiday schedule when computing the next-release stamp:
    # e.g. data through Fri May 15 → next WPSR covers week-ending Fri May 22,
    # which is holiday-shifted from Wed May 27 10:30 AM to Thu May 28 12:00 PM ET.
    next_release_str = _next_wpsr_release_str(latest_date)
    refreshed = _utcnow().strftime('%b %-d, %Y')
    # Live ET time + date for the AI Brief refresh stamp (used in the hero
    # block here and mirrored on the margins and curves AI Brief headers).
    refreshed_time, refreshed_date = _refreshed_stamp_et()

    # Latest trade-data date — use prices_as_of (prior trading day settlement) when
    # available; fall back to the last date in the 3-2-1 crack series.
    prices_latest_date = report_date_str
    trade_through_dt = latest_date  # fallback for next-refresh calc
    if prices:
        if prices.get('prices_as_of'):
            try:
                trade_through_dt = datetime.strptime(prices['prices_as_of'], '%Y-%m-%d')
                prices_latest_date = trade_through_dt.strftime('%B %-d, %Y')
            except Exception:
                prices_latest_date = prices['prices_as_of']
        else:
            crack_d = (prices.get('cracks', {}).get('crack_321', {}) or {}).get('data', [])
            if crack_d:
                try:
                    trade_through_dt = datetime.strptime(crack_d[-1][0], '%Y-%m-%d')
                    prices_latest_date = trade_through_dt.strftime('%B %-d, %Y')
                except Exception:
                    prices_latest_date = crack_d[-1][0]

    # Next refresh = day after the next NYMEX trade date following `trade_through_dt`
    try:
        next_refresh_str = _next_refresh_date(trade_through_dt).strftime('%B %-d, %Y')
    except Exception:
        next_refresh_str = ''

    # Freshness state — two-phase refresh design. When EIA inventories have
    # been refreshed but prices haven't yet caught up (Wed afternoon ~ Thu AM
    # window), we surface a "Just Released" banner so the page tells the truth
    # about its own data lag instead of letting the commentary equivocate.
    _freshness = _compute_freshness(prices.get('prices_as_of', '') if prices else '', latest_date)
    inv_latest_date = _freshness['inv_through']
    eia_banner_html = _freshness['banner_html']

    # In post_eia we suppress the AI Brief entirely — the banner carries the
    # message. Hide the "Today's Read · AI Generated" labels and the body slot.
    if _freshness['state'] == 'post_eia':
        mob_hero_eyebrow_html = ''
        mob_hero_body_html = ''
    else:
        mob_hero_eyebrow_html = (
            '<span class="mob-eyebrow">Today\'s Read</span>'
            '<span class="mob-pill-ai">AI Generated</span>'
        )
        mob_hero_body_html = '<div class="mob-hero-body" id="market-read"></div>'

    # Section preview cards — each describes one detail page with a headline metric
    crude_chg = kpi_data[1]['change']  # crude row
    gas_chg   = kpi_data[2]['change']
    dist_chg  = kpi_data[3]['change']
    jet_chg   = kpi_data[4]['change']
    util_last = kpi_data[5]['last']
    total_chg = kpi_data[0]['change']

    def sgn(v): return f'+{v:.1f}' if v >= 0 else f'{v:.1f}'
    def sgn_arr(v): return f'▲ +{v:.1f}' if v >= 0 else f'▼ {v:.1f}'

    inv_headline = (
        f'Total stocks {("drew" if total_chg < 0 else "built")} '
        f'{abs(total_chg):.1f} mb · refinery util {util_last:.1f}%'
    )

    # Margins preview — live headline from prices_data if available
    if prices and prices.get('cracks', {}).get('crack_321', {}).get('data'):
        c321 = prices['cracks']['crack_321']['data']
        last321 = c321[-1][1]; prev321 = c321[-2][1] if len(c321) >= 2 else last321
        chg321 = last321 - prev321
        cgas = prices['cracks']['crack_gasoline']['data'][-1][1]
        cdist = prices['cracks']['crack_distillate']['data'][-1][1]
        # Use NYMEX front-month settle (matches KPI strip + market read).
        # eia_spot.wti_spot lags by ~5 trading days and produced stale $ figures.
        wti_curve = (prices.get('futures', {}).get('wti', {}) or {}).get('curve') or []
        if wti_curve:
            wti = wti_curve[0]['price']
        else:
            wti = prices['eia_spot']['wti_spot']['data'][-1][1]
        margins_headline = f'3-2-1 crack ${last321:.1f}/bbl · WTI ${wti:.1f}'
        margins_sub = (
            f'Gasoline ${cgas:.1f} · Distillate ${cdist:.1f} · 3-2-1 {"▲ +" if chg321>=0 else "▼ "}{chg321:.2f} w/w'
        )
        margins_enabled = True
    else:
        margins_headline = 'Crack spreads, refining margins, Brent–WTI'
        margins_sub = '3-2-1 · gasoline · distillate · jet · Brent–WTI'
        margins_enabled = False

    margins_cls = 'preview-card preview-margins' if margins_enabled else 'preview-card preview-margins preview-disabled'
    margins_href = 'margins.html' if margins_enabled else '#'
    margins_title = '' if margins_enabled else ' title="Coming soon"'
    margins_corner = '<span class="preview-arrow">→</span>' if margins_enabled else '<span class="preview-soon">SOON</span>'
    margins_cta = 'Explore cracks &amp; curves →' if margins_enabled else 'Coming soon'

    # Curves preview — live headline from prices_data
    if prices and prices.get('futures', {}).get('wti', {}).get('curve'):
        wc = prices['futures']['wti']['curve']
        wc_front = wc[0]['price'] if wc else 0
        wc_back = wc[-1]['price'] if wc else 0
        wc_spread = wc_front - wc_back
        regime = 'backwardation' if wc_spread > 0 else 'contango'
        curves_headline = f'WTI in {regime} · M1–M12 ${wc_spread:+.1f}/bbl'
        curves_sub = f'Front ${wc_front:.1f} → 12-mo ${wc_back:.1f}'
        curves_enabled = True
    else:
        curves_headline = 'WTI / Brent / RBOB / ULSD forward curves'
        curves_sub = '12-month strips · contango/backwardation indicator'
        curves_enabled = False

    curves_cls = 'preview-card preview-curves' if curves_enabled else 'preview-card preview-curves preview-disabled'
    curves_href = 'curves.html' if curves_enabled else '#'
    curves_title = '' if curves_enabled else ' title="Coming soon"'
    curves_corner = '<span class="preview-arrow">→</span>' if curves_enabled else '<span class="preview-soon">SOON</span>'
    curves_cta = 'Explore forward strips →' if curves_enabled else 'Coming soon'

    preview_cards = f"""
    <a class="preview-card preview-crude" href="inventory.html">
      <div class="preview-head">
        <span class="preview-label">Inventories</span>
        <span class="preview-arrow">→</span>
      </div>
      <div class="preview-headline">{inv_headline}</div>
      <div class="preview-sub">
        Crude {sgn_arr(crude_chg)} · Gas {sgn_arr(gas_chg)} · Distillate {sgn_arr(dist_chg)} · Jet {sgn_arr(jet_chg)} mb w/w
      </div>
      <div class="preview-cta">Explore PADD breakdowns →</div>
    </a>
    <a class="{margins_cls}" href="{margins_href}"{margins_title}>
      <div class="preview-head">
        <span class="preview-label">Cracks · Prices</span>
        {margins_corner}
      </div>
      <div class="preview-headline">{margins_headline}</div>
      <div class="preview-sub">{margins_sub}</div>
      <div class="preview-cta">{margins_cta}</div>
    </a>
    <a class="{curves_cls}" href="{curves_href}"{curves_title}>
      <div class="preview-head">
        <span class="preview-label">Forward Curves</span>
        {curves_corner}
      </div>
      <div class="preview-headline">{curves_headline}</div>
      <div class="preview-sub">{curves_sub}</div>
      <div class="preview-cta">{curves_cta}</div>
    </a>
"""

    # Build the morning brief context — synthesize across inventory + margins + curves
    inv_summary = {
        'reportDate': report_date_str,
        'totalStocks_mb': kpi_data[0]['last'], 'totalStocks_chg_mb': kpi_data[0]['change'],
        'crude_mb': kpi_data[1]['last'], 'crude_chg_mb': kpi_data[1]['change'],
        'gas_mb':   kpi_data[2]['last'], 'gas_chg_mb':   kpi_data[2]['change'],
        'dist_mb':  kpi_data[3]['last'], 'dist_chg_mb':  kpi_data[3]['change'],
        'jet_mb':   kpi_data[4]['last'], 'jet_chg_mb':   kpi_data[4]['change'],
        'refUtil_pct': kpi_data[5]['last'], 'refUtil_chg_pp': kpi_data[5]['change'],
        'inventory_narrative_excerpt': narratives.get('market_read', '')[:600],
    }
    if prices:
        cracks = prices.get('cracks', {})
        spot = prices.get('eia_spot', {})
        futures = prices.get('futures', {})
        def last(s):
            d = (s or {}).get('data', [])
            return d[-1][1] if d else None
        def last_date(s):
            d = (s or {}).get('data', [])
            return d[-1][0] if d else None
        # Use NYMEX front-month settlement (curve[0].price) for WTI and Brent —
        # NOT the EIA daily spot, which lags 3-5 days and is a different price.
        # The AI must reference these as "prior session NYMEX settlement" figures.
        def front_settlement(commodity_key):
            curve = (futures.get(commodity_key, {}) or {}).get('curve', [])
            return curve[0]['price'] if curve else None
        def front_settlement_prev(commodity_key):
            """Prior day's settlement (price_1d on curve[0]) — used to compute direction."""
            curve = (futures.get(commodity_key, {}) or {}).get('curve', [])
            return curve[0].get('price_1d') if curve else None
        def settlement_change(commodity_key):
            cur = front_settlement(commodity_key)
            prev = front_settlement_prev(commodity_key)
            if cur is not None and prev is not None:
                return round(cur - prev, 2)
            return None
        wti_cur  = front_settlement('wti')
        wti_prev = front_settlement_prev('wti')
        wti_chg  = settlement_change('wti')
        margins_summary = {
            'crack_321':    last(cracks.get('crack_321')),
            'crack_gasoline': last(cracks.get('crack_gasoline')),
            'crack_distillate': last(cracks.get('crack_distillate')),
            'crack_jet': last(cracks.get('crack_jet')),
            'brent_wti_spread': last(cracks.get('brent_wti_spread')),
            # NYMEX prior-session settlements with explicit day-over-day change.
            # Use ONLY these for direction — do NOT infer up/down from news headlines.
            'wti_nymex_settlement': wti_cur,
            'wti_nymex_prev_day_settlement': wti_prev,
            'wti_nymex_change': wti_chg,   # negative = settled lower; positive = settled higher
            'brent_nymex_settlement': front_settlement('brent'),
            'brent_nymex_prev_day_settlement': front_settlement_prev('brent'),
            'brent_nymex_change': settlement_change('brent'),
            'rbob_nymex_settlement_per_gal': front_settlement('rbob'),
            'rbob_nymex_change_per_gal': settlement_change('rbob'),
            'ulsd_nymex_settlement_per_gal': front_settlement('ulsd'),
            'ulsd_nymex_change_per_gal': settlement_change('ulsd'),
            'note': 'All prices are prior-session NYMEX settlements. Use wti_nymex_change (not news) to determine if WTI settled higher or lower.',
        }
        # Curve regime context
        def m1m12(key):
            c = (futures.get(key, {}) or {}).get('curve', [])
            return (c[0]['price'] - c[-1]['price']) if len(c) >= 2 else None
        def m1m2(key):
            c = (futures.get(key, {}) or {}).get('curve', [])
            return (c[0]['price'] - c[1]['price']) if len(c) >= 2 else None
        # RBOB & ULSD: convert to $/bbl
        def m1m12_bbl(key):
            v = m1m12(key)
            return v * 42 if v is not None and key in ('rbob', 'ulsd') else v
        curves_summary = {
            'wti_M1_M12': m1m12('wti'),
            'brent_M1_M12': m1m12('brent'),
            'rbob_M1_M12_bbl': m1m12_bbl('rbob'),
            'ulsd_M1_M12_bbl': m1m12_bbl('ulsd'),
            'wti_M1_M2': m1m2('wti'),
            'brent_M1_M2': m1m2('brent'),
        }
        # Use prices_as_of (prior trading day settlement) if present; fall back
        # to the last date in the EIA WTI spot series.
        prices_date_str = prices.get('prices_as_of') or last_date(spot.get('wti_spot')) or 'recent'
    else:
        margins_summary = {}
        curves_summary = {}
        prices_date_str = '—'

    morning_brief = _generate_morning_brief(
        inv_summary, margins_summary, curves_summary,
        inventory_date=report_date_str, prices_date=prices_date_str,
        news=news_items, freshness=_freshness,
    )

    # ── Hero "headline numbers" — 7 stats above the fold ──
    # Crude w/w (inventory, weekly) — already from inventory KPI
    crude_chg = kpi_data[1]['change']
    crude_chg_sign = '+' if crude_chg >= 0 else ''
    crude_chg_color = '#16a34a' if crude_chg >= 0 else '#ef4444'

    # Daily price/spread deltas — use last two observations from prices_data
    def daily_last_prev(series_obj):
        d = (series_obj or {}).get('data', [])
        if len(d) < 2: return (None, None)
        return d[-1][1], d[-2][1]

    if prices:
        cracks = prices.get('cracks', {})
        spot = prices.get('eia_spot', {})
        futures = prices.get('futures', {})
        c321_last, c321_prev = daily_last_prev(cracks.get('crack_321'))
        cgas_last, cgas_prev = daily_last_prev(cracks.get('crack_gasoline'))
        cdist_last, cdist_prev = daily_last_prev(cracks.get('crack_distillate'))
        wti_last, wti_prev = daily_last_prev(spot.get('wti_spot'))
        brent_last, brent_prev = daily_last_prev(spot.get('brent_spot'))
        c = (futures.get('wti', {}) or {}).get('curve', [])
        wti_m1m12 = (c[0]['price'] - c[-1]['price']) if len(c) >= 2 else 0
    else:
        c321_last = c321_prev = 0
        cgas_last = cgas_prev = 0
        cdist_last = cdist_prev = 0
        wti_last = wti_prev = 0
        brent_last = brent_prev = 0
        wti_m1m12 = 0

    def chg(last, prev): return (last or 0) - (prev or 0)
    def chg_sign(v): return '+' if v >= 0 else ''
    def chg_color(v): return '#16a34a' if v >= 0 else '#ef4444'

    regime_label = 'Backwardation' if wti_m1m12 > 0 else 'Contango'

    # ── Build full KPI cards for landing (same gradient/sparkline/band format as inventory) ──
    def landing_kpi_daily(series_obj, label, color_id, units='$/bbl'):
        """Daily-change KPI card with 30-day sparkline only — no YoY / no band."""
        d = (series_obj or {}).get('data', [])
        if len(d) < 2:
            return None
        last = d[-1][1]; prev = d[-2][1]
        # Sparkline: last 30 trading days (≈6 weeks calendar)
        spark = [round(v, 2) for _, v in d[-30:]]
        yoy = []  # no YoY on landing minimal cards
        weekly = _weekly_from_daily(d)
        try:
            latest_dt = datetime.strptime(weekly[-1][0], '%Y-%m-%d')
            latest_week = latest_dt.isocalendar()[1]
            cur_year = latest_dt.year
            band_vals = []
            for date_str, v in weekly:
                dt2 = datetime.strptime(date_str, '%Y-%m-%d')
                if dt2.isocalendar()[1] == latest_week and cur_year - 5 <= dt2.year < cur_year:
                    band_vals.append(v)
            band_lo = round(min(band_vals), 2) if band_vals else last
            band_hi = round(max(band_vals), 2) if band_vals else last
        except Exception:
            band_lo = last; band_hi = last
        return {
            'id': color_id, 'label': label,
            'last': round(last, 2), 'change': round(last - prev, 2),
            'units': units, 'frequency': 'daily',
            'spark': spark, 'yoy': yoy,
            'band_lo': band_lo, 'band_hi': band_hi,
        }

    def landing_kpi_from_curve(commodity_key, label, color_id, units='$/bbl'):
        """Build a landing KPI card sourced from the *explicit* front-month
        contract (curve[0]) rather than yfinance's CL=F continuous, which can
        roll to the next contract a few days early. Sparkline still uses the
        continuous front_history for trajectory shape."""
        cdata = (futures_.get(commodity_key, {}) or {})
        curve = cdata.get('curve') or []
        front_hist = cdata.get('front_history') or []
        if not curve:
            # fall back to continuous front_history if curve missing
            return landing_kpi_daily({'data': front_hist}, label, color_id, units)
        c0 = curve[0]
        last = c0.get('price')
        prev = c0.get('price_1d')
        if last is None:
            return None
        if prev is None:
            prev = last  # no change if 1d data unavailable
        # Sparkline from continuous front_history (last 30 trading days)
        spark = [round(v, 2) for _, v in front_hist[-30:]] if front_hist else [round(last, 2)]
        # Anchor the last sparkline point to the explicit front-month price so
        # the bar matches the displayed value.
        if spark:
            spark[-1] = round(last, 2)
        # 5yr same-week band from the continuous history (approx, fine for context)
        weekly = _weekly_from_daily(front_hist) if front_hist else []
        try:
            latest_dt = datetime.strptime(weekly[-1][0], '%Y-%m-%d') if weekly else None
            band_vals = []
            if latest_dt:
                latest_week = latest_dt.isocalendar()[1]
                cur_year = latest_dt.year
                for date_str, v in weekly:
                    dt2 = datetime.strptime(date_str, '%Y-%m-%d')
                    if dt2.isocalendar()[1] == latest_week and cur_year - 5 <= dt2.year < cur_year:
                        band_vals.append(v)
            band_lo = round(min(band_vals), 2) if band_vals else round(last, 2)
            band_hi = round(max(band_vals), 2) if band_vals else round(last, 2)
        except Exception:
            band_lo = round(last, 2); band_hi = round(last, 2)
        return {
            'id': color_id, 'label': label,
            'last': round(last, 2), 'change': round(last - prev, 2),
            'units': units, 'frequency': 'daily',
            'spark': spark, 'yoy': [],
            'band_lo': band_lo, 'band_hi': band_hi,
            'contract': c0.get('contract'),  # e.g. "2026-06" for display/debug
        }

    landing_kpis = []
    if prices:
        cracks = prices.get('cracks', {})
        futures_ = prices.get('futures', {})
        # WTI & Brent first (front-month contract, not CL=F continuous which can
        # roll early), then crack spreads
        landing_kpis.append(landing_kpi_from_curve('wti',   'WTI',   'wti'))
        landing_kpis.append(landing_kpi_from_curve('brent', 'Brent', 'brent'))
        landing_kpis.append(landing_kpi_daily(cracks.get('crack_321'),        '3-2-1 Crack',    'crack321'))
        landing_kpis.append(landing_kpi_daily(cracks.get('crack_gasoline'),   'Gasoline Crack', 'crackgas'))
        landing_kpis.append(landing_kpi_daily(cracks.get('crack_distillate'), 'Diesel Crack',   'crackdist'))
        landing_kpis.append(landing_kpi_daily(cracks.get('crack_jet'),        'Jet Crack',      'crackjet'))
    landing_kpis = [k for k in landing_kpis if k is not None]

    # Normalize: convert literal '\n' to real newlines + wrap any missing
    # lead labels (Headline:, Crude & products:, etc.) in <strong>…</strong>.
    morning_brief = _normalize_narrative(morning_brief)
    # Shorter "Today's Read": keep first 3 paragraphs (Headline + Crude/products + Refining margins)
    _brief_paras = [p.strip() for p in morning_brief.split('\n') if p.strip()]
    short_brief = '\n'.join(_brief_paras[:3]) if _brief_paras else morning_brief
    snapshot_kpi_only = json.dumps({'kpi': landing_kpis, 'marketRead': morning_brief, 'shortBrief': short_brief})

    # ── MOB landing fields — M1-M2 spread (was M1-M12), stocks-history sparkline,
    #    3-2-1 crack sparkline, inline SVG sparkline strings baked into the card markup.
    futures_data = (prices.get('futures', {}) if prices else {})
    wti_curve = (futures_data.get('wti', {}) or {}).get('curve') or []
    if len(wti_curve) >= 2:
        m1, m2 = wti_curve[0], wti_curve[1]
        # Spread history series: 1y-ago, 1m-ago, 1w-ago, 1d-ago, now
        m1m2_hist = []
        for k in ('price_1y', 'price_1m', 'price_1w', 'price_1d', 'price'):
            if m1.get(k) is not None and m2.get(k) is not None:
                m1m2_hist.append(round(m1[k] - m2[k], 2))
        m1m2_now = m1m2_hist[-1] if m1m2_hist else 0.0
        # weekly change: now vs 1w ago (index -3 in the 5-pt history if present)
        m1m2_prev_w = m1m2_hist[-3] if len(m1m2_hist) >= 3 else (m1m2_hist[0] if m1m2_hist else m1m2_now)
        m1m2_ww = m1m2_now - m1m2_prev_w
        m1_px = m1.get('price') or 0.0
        m2_px = m2.get('price') or 0.0
    else:
        m1m2_hist = []
        m1m2_now = 0.0
        m1m2_ww = 0.0
        m1_px = 0.0
        m2_px = 0.0
    cur_regime = 'Backwardation' if m1m2_now > 0 else 'Contango'

    # Inventory card sparkline — total stocks weekly history (real data from kpi)
    total_stocks_spark_vals = kpi_data[0].get('spark') or []
    inv_color = '#ef4444' if total_chg < 0 else '#22c55e'
    inv_pill_label = '▼ Draw' if total_chg < 0 else '▲ Build'
    inv_pill_class = 'mob-card--draw' if total_chg < 0 else 'mob-card--build'
    inv_spark_svg = _mob_spark_svg(total_stocks_spark_vals, color=inv_color)

    # Cracks · Prices card sparkline — actual 3-2-1 crack daily history
    c321_data = ((prices or {}).get('cracks', {}).get('crack_321', {}) or {}).get('data', [])
    c321_spark_vals = [round(v, 2) for _, v in c321_data[-30:]] if c321_data else []
    c321_chg_ww = (c321_last - c321_prev) if (c321_last is not None and c321_prev is not None) else 0.0
    mar_color = '#22c55e' if c321_chg_ww >= 0 else '#ef4444'
    mar_pill_label = '▲ Higher' if c321_chg_ww >= 0 else '▼ Lower'
    mar_pill_class = 'mob-card--build' if c321_chg_ww >= 0 else 'mob-card--draw'
    mar_spark_svg = _mob_spark_svg(c321_spark_vals, color=mar_color)

    # Forward Curves card sparkline — M1-M2 spread history (real)
    cur_color = '#22c55e' if m1m2_ww >= 0 else '#ef4444'
    cur_pill_label = '▲ Higher' if m1m2_ww >= 0 else '▼ Lower'
    cur_pill_class = 'mob-card--build' if m1m2_ww >= 0 else 'mob-card--draw'
    cur_spark_svg = _mob_spark_svg(m1m2_hist, color=cur_color)

    # Card stat helpers — colored arrows for w/w direction
    def _arrow_mb(v):
        sign = '▼ ' if v < 0 else '▲ +'
        cls = 'down' if v < 0 else 'up'
        return f'<span class="val {cls}">{sign}{abs(v):.1f} mb</span>'

    inv_total_arrow = _arrow_mb(total_chg)
    inv_crude_arrow = _arrow_mb(crude_chg)
    inv_gas_arrow   = _arrow_mb(gas_chg)
    inv_dist_arrow  = _arrow_mb(dist_chg)
    inv_jet_arrow   = _arrow_mb(jet_chg)

    mar_sub_text = f'3-2-1 crack ${c321_last:.2f}/bbl · WTI ${(wti_cur or 0):.2f}'
    mar_change_str = f'{"▼ −" if c321_chg_ww < 0 else "▲ +"}{abs(c321_chg_ww):.2f} w/w'
    mar_ww_val = f'<span class="val {"down" if c321_chg_ww < 0 else "up"}">{"▼ −" if c321_chg_ww < 0 else "▲ +"}{abs(c321_chg_ww):.2f}</span>'

    cur_sub_text = f'WTI {cur_regime.lower()} · M1–M2 ${m1m2_now:+.2f}/bbl'
    cur_change_str = f'{"▲ +" if m1m2_ww >= 0 else "▼ −"}{abs(m1m2_ww):.2f} w/w'
    cur_spread_val = f'<span class="val {"up" if m1m2_ww >= 0 else "down"}">${m1m2_now:+.2f}</span>'

    # ── News list for landing — top 6 most-recent items, cleaned ──
    def _relative_time(iso_str):
        if not iso_str: return ''
        try:
            dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
            now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
            delta = now - dt
            mins = int(delta.total_seconds() / 60)
            if mins < 1: return 'just now'
            if mins < 60: return f'{mins}m ago'
            hrs = mins // 60
            if hrs < 24: return f'{hrs}h ago'
            days = hrs // 24
            if days == 1: return 'yesterday'
            return f'{days}d ago'
        except Exception:
            return ''

    def _clean_title(title, source):
        """Strip ' - Source Name' suffix that Google News appends."""
        if source and title.endswith(' - ' + source):
            return title[:-len(' - ' + source)].strip()
        # Generic case: remove any trailing " - <whatever>" tail beyond the last em-dash
        if ' - ' in title:
            # Heuristic: source name is usually short, after final " - "
            head, _, tail = title.rpartition(' - ')
            if 2 <= len(tail) <= 50:
                return head.strip()
        return title.strip()

    news_html = ''
    if news_items:
        items = []
        for n in news_items[:6]:
            title = _clean_title(n.get('title', ''), n.get('source', ''))
            source = (n.get('source') or '').strip()
            link = n.get('link', '#')
            when = _relative_time(n.get('datetime'))
            # HTML-escape title and source
            from html import escape
            items.append(
                f'<a class="news-item" href="{escape(link)}" target="_blank" rel="noopener">'
                f'<span class="news-title">{escape(title)}</span>'
                f'<span class="news-meta">{escape(source)}'
                + (f' · <span class="news-time">{escape(when)}</span>' if when else '')
                + '</span></a>'
            )
        news_html = (
            '<div class="section news-panel">'
            '<div class="section-header">'
            '<h2 class="section-title">Latest Market News '
            '<span class="section-subtitle">curated · last 48 hours · click to open</span></h2>'
            '</div>'
            '<div class="news-list">' + ''.join(items) + '</div>'
            '</div>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<script>
(function(){{{{
  var ok=false;
  try {{{{
    for(var i=0;i<localStorage.length;i++){{{{
      var k=localStorage.key(i);
      if(k&&k.startsWith('sb-')&&k.endsWith('-auth-token')){{{{
        var d=JSON.parse(localStorage.getItem(k));
        if(d&&d.access_token&&d.expires_at>Date.now()/1000){{{{ok=true;break;}}}}
      }}}}
    }}}}
  }}}}catch(e){{{{}}}}
  if(!ok)window.location.replace('login.html');
}})();
</script>
<meta name="description" content="Daily petroleum intelligence — WTI settlement, crack spreads, inventory draws, and AI market analysis. Updated each morning.">
<meta name="theme-color" content="#07090d">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self' https://mob-chat.brad-95b.workers.dev;">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
{_FAVICON}
{_og_tags('AI Morning Oil Brief · Petroleum Intelligence', 'Daily petroleum intelligence — WTI settlement, crack spreads, inventory draws, and AI market analysis. Updated each morning.')}
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Morning Oil Brief · Petroleum Intelligence</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.js" integrity="sha384-iU8HYtnGQ8Cy4zl7gbNMOhsDTTKX02BTXptVP/vqAWIaTfM7isw76iyZCsjL2eVi" crossorigin="anonymous"></script>
<style>
:root {{
  color-scheme: dark;
  --bg: #0f1117; --panel: #1a1d24; --panel-2: #232730;
  --border: #25272e; --border-soft: #1c1e24;
  --text: #e4e7ec; --muted: #9aa0ac; --muted-2: #c1c5cf;
  --build: #16a34a; --draw: #dc2626; --accent: #f59e0b;
}}
* {{ box-sizing: border-box; }}
html, body {{ background: var(--bg); margin: 0; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  color: var(--text); font-size: 15px; line-height: 1.5;
}}
table, .num {{ font-variant-numeric: tabular-nums; }}
.container {{ max-width: 1500px; margin: 0 auto; padding: 14px; }}
.header {{
  display: flex; justify-content: space-between; align-items: flex-start;
  flex-wrap: wrap; gap: 8px; margin-bottom: 18px;
  padding-bottom: 12px; border-bottom: 1px solid var(--border);
}}
.header h1 {{ margin: 0; font-size: 22px; font-weight: 600; letter-spacing: -0.3px; }}
.header .subtitle {{ font-size: 12px; color: var(--muted); margin-top: 3px; }}
.update-info {{ text-align: right; font-size: 11px; color: var(--muted); line-height: 1.6; }}
.update-info strong {{ color: var(--text); font-weight: 500; }}
.update-info .pill {{
  display: inline-block; padding: 2px 9px;
  background: rgba(22,163,74,0.15); color: #4ade80; border-radius: 10px;
  font-weight: 500; font-size: 11px; letter-spacing: 0.4px; text-transform: uppercase;
  margin-bottom: 4px; border: 1px solid rgba(22,163,74,0.35);
}}
.kpi-strip {{ display: grid; grid-template-columns: repeat(6, 1fr) !important; gap: 8px; margin-bottom: 18px; }}
@media (max-width: 1280px) {{ .kpi-strip {{ grid-template-columns: repeat(3, 1fr) !important; }} }}
@media (max-width: 900px)  {{ .kpi-strip {{ grid-template-columns: repeat(2, 1fr) !important; }} }}
@media (max-width: 600px)  {{ .kpi-strip {{ grid-template-columns: 1fr !important; }} }}

/* Landing minimal KPI cards — keep sparkline only, hide position pill + range bar */
.kpi-strip-minimal .kpi-position,
.kpi-strip-minimal .kpi-band {{ display: none !important; }}
.kpi-strip-minimal .kpi {{ padding: 16px 18px !important; gap: 10px !important; }}
.kpi-strip-minimal .kpi-value {{ font-size: 30px !important; }}
.kpi-strip-minimal .kpi-change-row {{ font-size: 14px !important; gap: 8px !important; }}
.kpi-strip-minimal .kpi-change {{ font-size: 15px !important; font-weight: 700 !important; }}
.kpi-strip-minimal .kpi-change-sub {{ font-size: 12px !important; }}
.kpi-strip-minimal .kpi-spark {{ height: 36px !important; margin-top: 4px; }}

/* Center KPI strips across all pages */
.kpi-strip {{ max-width: 1300px; margin-left: auto !important; margin-right: auto !important; }}
{kpi_redesign_css_for_landing()}
{NAV_CSS}
{MOB_LANDING_CSS}
/* Market Read block */
.section {{
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px 18px; margin-bottom: 14px;
}}
.section-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
.section-title {{ font-size: 14px; font-weight: 600; margin: 0; color: var(--text);
  letter-spacing: -0.1px; display: inline-flex; align-items: center; gap: 10px; }}
.ai-badge {{
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 9px; border-radius: 999px;
  font-size: 10px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
  color: #4ade80;
  background: rgba(22,163,74,0.15);
  border: 1px solid rgba(22,163,74,0.35);
  animation: ai-badge-glow 3.6s ease-in-out infinite;
  vertical-align: middle;
  white-space: nowrap;
}}
.ai-badge::before {{
  content: "✦"; font-size: 9px; line-height: 1; display: inline-block;
  filter: drop-shadow(0 0 1px rgba(74,222,128,0.5));
}}
@keyframes ai-badge-glow {{
  0%, 100% {{
    box-shadow: 0 0 2px rgba(74,222,128,0.25),
                0 0 4px rgba(22,163,74,0.15);
  }}
  50% {{
    box-shadow: 0 0 5px rgba(74,222,128,0.55),
                0 0 11px rgba(22,163,74,0.30);
  }}
}}
.section-subtitle {{ font-size: 11px; color: var(--muted); font-weight: 400; margin-left: 8px; text-transform: none; letter-spacing: 0; }}
.badge {{ display: inline-block; font-size: 11px; font-weight: 700;
  padding: 3px 8px; border-radius: 999px; vertical-align: 2px;
  letter-spacing: 0.7px; text-transform: uppercase; margin-left: 6px; }}
.badge-ai {{ background: linear-gradient(90deg, rgba(167,139,250,0.18), rgba(96,165,250,0.18));
  color: #c4b5fd; border: 1px solid rgba(167,139,250,0.4); }}
.market-read {{ font-size: 13.5px; line-height: 1.7; color: var(--muted-2); }}
.market-read strong {{ color: var(--text); }}
.market-read p {{ margin: 0 0 10px; }}
/* Section preview cards */
.preview-grid {{
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-bottom: 18px;
}}
@media (max-width: 1000px) {{ .preview-grid {{ grid-template-columns: 1fr; }} }}
.preview-card {{
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; padding: 18px 20px;
  text-decoration: none; color: var(--text);
  display: flex; flex-direction: column; gap: 10px;
  position: relative; overflow: hidden;
  transition: transform 0.18s, border-color 0.18s, box-shadow 0.18s;
}}
.preview-card::before {{
  content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 4px;
  background: var(--pv-color, var(--accent));
}}
.preview-crude  {{ --pv-color: #f59e0b; }}
.preview-margins {{ --pv-color: #16a34a; }}
.preview-curves  {{ --pv-color: #a78bfa; }}
.preview-card:hover {{
  transform: translateY(-3px);
  border-color: var(--pv-color);
  box-shadow: 0 8px 24px rgba(0,0,0,0.4);
}}
.preview-disabled {{ opacity: 0.45; cursor: not-allowed; pointer-events: none; }}
.preview-head {{ display: flex; justify-content: space-between; align-items: center; }}
.preview-label {{
  font-size: 12px; font-weight: 700; color: var(--pv-color);
  letter-spacing: -0.1px;
}}
.preview-arrow {{ font-size: 18px; color: var(--pv-color); }}
.preview-soon {{
  font-size: 11px; font-weight: 700;
  padding: 2px 7px; border-radius: 999px;
  background: rgba(255,255,255,0.08); color: var(--muted-2);
  letter-spacing: 0.6px;
}}
.preview-headline {{
  font-size: 18px; font-weight: 600; color: var(--text); line-height: 1.3;
}}
.preview-sub {{ font-size: 12px; color: var(--muted); line-height: 1.5; }}
.preview-cta {{
  margin-top: 4px; font-size: 12px; font-weight: 600;
  color: var(--pv-color); letter-spacing: -0.1px;
}}
footer {{
  margin-top: 24px; padding-top: 12px; border-top: 1px solid var(--border);
  font-size: 11px; color: var(--muted); text-align: center;
}}
footer a {{ color: var(--accent); text-decoration: none; }}

/* ─── Hero ──────────────────────────────────────────────────────────── */
.hero {{
  padding: 30px 4px 18px; text-align: center;
  background:
    radial-gradient(ellipse at 50% 0%, rgba(245,158,11,0.10) 0%, transparent 55%),
    radial-gradient(ellipse at 80% 100%, rgba(167,139,250,0.07) 0%, transparent 50%);
  border-bottom: 1px solid var(--border);
  margin-bottom: 22px;
}}
.hero-eyebrow {{
  font-size: 12px; letter-spacing: 0.3px;
  color: var(--muted); font-weight: 600; margin-bottom: 10px;
}}
.hero-title {{
  font-size: 44px; font-weight: 700; line-height: 1.05;
  letter-spacing: -1.2px; margin: 0;
  background: linear-gradient(180deg, #ffffff 0%, #d8dce4 100%);
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent;
}}
@media (max-width: 700px) {{
  .hero-title {{ font-size: 30px; letter-spacing: -0.6px; }}
  .hero {{ padding: 22px 4px 14px; }}
}}

/* ─── Hero numbers strip — 7 cards across, auto-wrap on small ─── */
.hero-numbers {{
  display: grid; grid-template-columns: repeat(7, 1fr); gap: 0;
  padding: 18px 16px; margin-bottom: 28px;
  background: linear-gradient(180deg, rgba(255,255,255,0.02) 0%, transparent 100%);
  border-top: 1px solid var(--border-soft);
  border-bottom: 1px solid var(--border-soft);
}}
.hero-stat {{
  text-align: center; padding: 4px 8px;
  border-right: 1px solid var(--border);
}}
.hero-stat:last-child {{ border-right: none; }}
.hero-stat-label {{
  font-size: 12px; letter-spacing: 0;
  color: var(--muted); font-weight: 600; margin-bottom: 6px;
}}
.hero-stat-value {{
  font-size: 26px; font-weight: 700; line-height: 1;
  font-variant-numeric: tabular-nums; letter-spacing: -0.4px;
  color: var(--text);
}}
.hero-stat-unit {{
  font-size: 11px; font-weight: 500; color: var(--muted-2);
  letter-spacing: 0; margin-left: 2px;
}}
.hero-stat-sub {{
  font-size: 10.5px; color: var(--muted); margin-top: 6px;
  font-variant-numeric: tabular-nums;
}}
@media (max-width: 1280px) {{ .hero-numbers {{ grid-template-columns: repeat(4, 1fr); }} .hero-stat {{ border-right: none; border-bottom: 1px solid var(--border-soft); padding: 10px 8px; }} }}
@media (max-width: 700px)  {{ .hero-numbers {{ grid-template-columns: repeat(2, 1fr); }} .hero-stat-value {{ font-size: 22px; }} }}

/* ─── Brief row (commentary + 3 stacked sparklines) ───────────────── */
.brief-row {{
  display: grid; grid-template-columns: 2fr 1fr; gap: 14px; margin-bottom: 26px;
}}
@media (max-width: 1100px) {{ .brief-row {{ grid-template-columns: 1fr; }} }}
.brief-panel {{ margin: 0; }}
.spark-stack {{ display: flex; flex-direction: column; gap: 10px; }}
.spark-card {{
  margin: 0; padding: 12px 14px;
  position: relative; overflow: hidden;
  transition: border-color 0.15s;
}}
.spark-card::before {{
  content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
  background: var(--sp-color, var(--accent));
}}
.spark-inventory {{ --sp-color: #f59e0b; }}
.spark-margin    {{ --sp-color: #16a34a; }}
.spark-curve     {{ --sp-color: #a78bfa; }}
.spark-label {{
  font-size: 12px; letter-spacing: 0;
  color: var(--muted-2); font-weight: 600; margin-bottom: 6px;
}}
.spark-canvas-wrap {{ position: relative; height: 72px; }}

/* ─── News list ───────────────────────────────────────────────────── */
.news-panel {{ padding: 16px 20px; }}
.news-list {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px 22px; }}
@media (max-width: 800px) {{ .news-list {{ grid-template-columns: 1fr; }} }}
.news-item {{
  display: block; padding: 10px 12px 11px;
  border-radius: 6px; text-decoration: none; color: inherit;
  border: 1px solid transparent; border-left: 3px solid var(--border);
  background: rgba(255,255,255,0.015);
  transition: background 0.15s, border-color 0.15s, transform 0.15s;
}}
.news-item:hover {{
  background: rgba(96,165,250,0.06);
  border-left-color: #60a5fa; transform: translateX(2px);
}}
.news-title {{
  display: block; font-size: 13px; font-weight: 500;
  color: var(--text); line-height: 1.45; margin-bottom: 4px;
}}
.news-meta {{
  font-size: 11px; color: var(--muted);
  letter-spacing: 0.2px;
}}
.news-time {{ color: var(--muted-2); }}

/* ─── Explore title ───────────────────────────────────────────────── */
.explore-title {{
  font-size: 15px; letter-spacing: -0.1px;
  color: var(--muted-2); font-weight: 700; margin: 26px 0 12px;
  padding-bottom: 8px; border-bottom: 1px solid var(--border-soft);
}}

/* ─── Why-this-exists block ───────────────────────────────────────── */
.why-block {{
  display: flex; align-items: center; gap: 18px;
  padding: 22px 24px; margin-top: 28px;
  background: linear-gradient(135deg, rgba(96,165,250,0.08) 0%, rgba(167,139,250,0.04) 100%);
  border: 1px solid rgba(96,165,250,0.18);
  border-radius: 12px;
}}
.why-icon {{
  font-size: 36px; line-height: 1; flex-shrink: 0;
  filter: drop-shadow(0 0 12px rgba(96,165,250,0.4));
}}
.why-text {{
  font-size: 13.5px; color: var(--muted-2); line-height: 1.6;
}}
.why-text strong {{ color: var(--text); font-weight: 600; }}

/* ─── Data sources ────────────────────────────────────────────────── */
.data-sources {{
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  margin-top: 22px; padding: 14px 4px;
  border-top: 1px solid var(--border-soft);
}}
.data-source-label {{
  font-size: 12px; letter-spacing: 0;
  color: var(--muted); font-weight: 600; margin-right: 6px;
}}
.data-source-pill {{
  font-size: 11px; padding: 4px 10px; border-radius: 999px;
  background: var(--panel-2); border: 1px solid var(--border);
  color: var(--muted-2); font-weight: 500;
}}
</style>
</head>
<body>
{_render_nav('index.html')}

<div class="container">

  <!-- ─── Title ──────────────────────────────────────────────── -->
  <!-- ─── Title ──────────────────────────────────────────────── -->
  <div class="header">
    <div>
      <h1>Morning Oil Brief</h1>
      <div class="subtitle">Petroleum intelligence · Yesterday's NYMEX settlement, decoded each morning</div>
    </div>
    <div class="update-info">
      <span class="pill">NYMEX SETTLEMENT</span><br>
      Prices thru: <strong>{prices_latest_date}</strong><br>
      Inventories thru: <strong>{inv_latest_date}</strong><br>
      Next refresh: <strong>{next_refresh_str}</strong> · <strong>~5:00 AM ET</strong>
    </div>
  </div>

  <!-- ─── Today's Read ───────────────────────────────────────── -->
  <section class="mob-hero">
    <div class="mob-hero-head">
      <div class="mob-hero-left">
        {mob_hero_eyebrow_html}
      </div>
    </div>
    {eia_banner_html}
    {mob_hero_body_html}
  </section>

  <!-- ─── Explore the data ───────────────────────────────────── -->
  <h2 class="mob-section-h">Explore the data</h2>
  <div class="mob-cards">

    <a class="mob-card {mar_pill_class}" href="margins.html">
      <div class="mob-card-head">
        <span class="mob-card-eyebrow"><span class="num">01</span> Cracks · Prices</span>
        <span class="mob-card-pill">{mar_pill_label}</span>
      </div>
      <div class="mob-card-sub">{mar_sub_text}</div>
      <div class="mob-card-value-row">
        <div class="mob-card-value">${c321_last:.2f}<span class="unit">3-2-1 / bbl</span></div>
        <div class="mob-card-change">{mar_change_str}</div>
      </div>
      <div class="mob-card-spark">{mar_spark_svg}</div>
      <div class="mob-card-stats">
        <div class="mob-stat"><span class="lbl">WTI</span><span class="val">${(wti_cur or 0):.2f}</span></div>
        <div class="mob-stat"><span class="lbl">Gasoline</span><span class="val">${(cgas_last or 0):.2f}</span></div>
        <div class="mob-stat"><span class="lbl">Distillate</span><span class="val">${(cdist_last or 0):.2f}</span></div>
        <div class="mob-stat"><span class="lbl">3-2-1 w/w</span>{mar_ww_val}</div>
      </div>
    </a>

    <a class="mob-card {cur_pill_class}" href="curves.html">
      <div class="mob-card-head">
        <span class="mob-card-eyebrow"><span class="num">02</span> Forward Curves</span>
        <span class="mob-card-pill">{cur_pill_label}</span>
      </div>
      <div class="mob-card-sub">{cur_sub_text}</div>
      <div class="mob-card-value-row">
        <div class="mob-card-value">${m1m2_now:+.2f}<span class="unit">M1–M2 / bbl</span></div>
        <div class="mob-card-change">{cur_change_str}</div>
      </div>
      <div class="mob-card-spark">{cur_spark_svg}</div>
      <div class="mob-card-stats">
        <div class="mob-stat"><span class="lbl">Front M1</span><span class="val">${m1_px:.2f}</span></div>
        <div class="mob-stat"><span class="lbl">M2</span><span class="val">${m2_px:.2f}</span></div>
        <div class="mob-stat"><span class="lbl">Spread</span>{cur_spread_val}</div>
        <div class="mob-stat"><span class="lbl">Shape</span><span class="val">{cur_regime}</span></div>
      </div>
    </a>

    <a class="mob-card {inv_pill_class}" href="inventory.html">
      <div class="mob-card-head">
        <span class="mob-card-eyebrow"><span class="num">03</span> Inventories</span>
        <span class="mob-card-pill">{inv_pill_label}</span>
      </div>
      <div class="mob-card-sub">{inv_headline}</div>
      <div class="mob-card-value-row">
        <div class="mob-card-value">{('−' if total_chg < 0 else '+')}{abs(total_chg):.1f}<span class="unit">mb Total {('Draw' if total_chg < 0 else 'Build')}</span></div>
        <div class="mob-card-change mob-card-change--neutral">{util_last:.1f}% Refinery Util</div>
      </div>
      <div class="mob-card-spark">{inv_spark_svg}</div>
      <div class="mob-card-stats">
        <div class="mob-stat"><span class="lbl">Crude</span>{inv_crude_arrow}</div>
        <div class="mob-stat"><span class="lbl">Gasoline</span>{inv_gas_arrow}</div>
        <div class="mob-stat"><span class="lbl">Distillate</span>{inv_dist_arrow}</div>
        <div class="mob-stat"><span class="lbl">Jet</span>{inv_jet_arrow}</div>
      </div>
    </a>

  </div>

  <footer>
    MOB · Morning Oil Brief · Refreshed {refreshed.upper()} · Trade data through {prices_latest_date}
  </footer>
</div>

<script>
{_SIGNOUT_JS}
const SNAPSHOT = {snapshot_kpi_only};
// Render the (trimmed) market read into the hero body — first 3 paragraphs.
(function() {{
  const el = document.getElementById('market-read');
  if (!el) return;
  const src = SNAPSHOT.shortBrief || SNAPSHOT.marketRead || '';
  const paras = src.split('\\n').map(s => s.trim()).filter(Boolean).slice(0, 3);
  el.innerHTML = paras.map(p => '<p>' + p + '</p>').join('');
}})();
</script>

<!-- AI Chat Widget -->
<style>
#mob-chat-btn{{position:fixed;bottom:24px;right:24px;z-index:9000;width:52px;height:52px;border-radius:50%;background:var(--accent);border:none;cursor:pointer;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 18px rgba(245,158,11,0.45);transition:transform 0.2s,box-shadow 0.2s;font-size:22px;}}
#mob-chat-btn:hover{{transform:scale(1.08);box-shadow:0 6px 24px rgba(245,158,11,0.6);}}
#mob-chat-panel{{position:fixed;bottom:88px;right:24px;z-index:9000;width:380px;max-width:calc(100vw - 32px);height:520px;max-height:calc(100vh - 120px);background:var(--panel);border:1px solid var(--border);border-radius:12px;display:flex;flex-direction:column;box-shadow:0 12px 40px rgba(0,0,0,0.55);transform:translateY(16px) scale(0.97);opacity:0;pointer-events:none;transition:opacity 0.18s ease,transform 0.18s ease;}}
#mob-chat-panel.open{{transform:translateY(0) scale(1);opacity:1;pointer-events:all;}}
.mob-chat-header{{padding:12px 14px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-shrink:0;}}
.mob-chat-header-title{{font-size:13px;font-weight:600;color:var(--text);display:flex;align-items:center;gap:7px;}}
.mob-chat-badge{{font-size:10px;font-weight:700;padding:2px 7px;border-radius:8px;background:rgba(245,158,11,0.18);color:var(--accent);border:1px solid rgba(245,158,11,0.3);letter-spacing:0.5px;text-transform:uppercase;}}
.mob-chat-close{{background:none;border:none;color:var(--muted);cursor:pointer;font-size:18px;line-height:1;padding:2px 4px;border-radius:4px;}}
.mob-chat-close:hover{{color:var(--text);background:var(--panel-2);}}
.mob-chat-messages{{flex:1;overflow-y:auto;padding:14px 14px 8px;display:flex;flex-direction:column;gap:10px;scrollbar-width:thin;scrollbar-color:var(--border) transparent;}}
.mob-chat-msg{{display:flex;flex-direction:column;gap:2px;max-width:88%;}}
.mob-chat-msg.user{{align-self:flex-end;align-items:flex-end;}}
.mob-chat-msg.assistant{{align-self:flex-start;align-items:flex-start;}}
.mob-chat-bubble{{padding:9px 12px;border-radius:10px;font-size:13px;line-height:1.5;word-break:break-word;}}
.mob-chat-msg.user .mob-chat-bubble{{background:var(--accent);color:#0f1117;border-radius:10px 10px 2px 10px;font-weight:500;}}
.mob-chat-msg.assistant .mob-chat-bubble{{background:var(--panel-2);color:var(--text);border:1px solid var(--border);border-radius:10px 10px 10px 2px;}}
.mob-chat-msg.assistant .mob-chat-bubble.thinking{{color:var(--muted);font-style:italic;}}
.mob-chat-suggestions{{display:flex;flex-direction:column;gap:5px;margin-top:2px;}}
.mob-chat-suggestion{{background:none;border:1px solid var(--border);border-radius:8px;color:var(--muted-2);font-size:12px;padding:6px 10px;cursor:pointer;text-align:left;transition:border-color 0.15s,color 0.15s,background 0.15s;}}
.mob-chat-suggestion:hover{{border-color:var(--accent);color:var(--accent);background:rgba(245,158,11,0.07);}}
.mob-chat-footer{{padding:10px 12px;border-top:1px solid var(--border);display:flex;gap:8px;flex-shrink:0;}}
#mob-chat-input{{flex:1;background:var(--panel-2);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;padding:8px 11px;outline:none;resize:none;font-family:inherit;line-height:1.4;max-height:100px;min-height:36px;transition:border-color 0.15s;}}
#mob-chat-input:focus{{border-color:rgba(245,158,11,0.5);}}
#mob-chat-input::placeholder{{color:var(--muted);}}
#mob-chat-send{{background:var(--accent);border:none;border-radius:8px;color:#0f1117;cursor:pointer;font-size:16px;padding:0 12px;flex-shrink:0;transition:opacity 0.15s;}}
#mob-chat-send:disabled{{opacity:0.4;cursor:default;}}
#mob-chat-send:not(:disabled):hover{{opacity:0.85;}}
</style>
<button id="mob-chat-btn" title="Ask AI about this data">&#x1F4AC;</button>
<div id="mob-chat-panel">
  <div class="mob-chat-header">
    <div class="mob-chat-header-title">&#x1F6E2;&#xFE0F; MOB Analyst <span class="mob-chat-badge">AI</span></div>
    <button class="mob-chat-close" id="mob-chat-close">&#x2715;</button>
  </div>
  <div class="mob-chat-messages" id="mob-chat-messages">
    <div class="mob-chat-msg assistant"><div class="mob-chat-bubble">Ask me anything about today's petroleum data &mdash; prices, cracks, inventory, or market outlook.</div></div>
    <div class="mob-chat-suggestions" id="mob-chat-suggestions">
      <button class="mob-chat-suggestion">What's driving WTI lower today?</button>
      <button class="mob-chat-suggestion">How do current crack spreads compare to normal?</button>
      <button class="mob-chat-suggestion">What does the inventory draw mean for prices?</button>
    </div>
  </div>
  <div class="mob-chat-footer">
    <textarea id="mob-chat-input" rows="1" placeholder="Ask about the data..." maxlength="800"></textarea>
    <button id="mob-chat-send">&#x27A4;</button>
  </div>
</div>
<script>
{_SIGNOUT_JS}
(function(){{
  var btn=document.getElementById("mob-chat-btn"),panel=document.getElementById("mob-chat-panel"),closeBtn=document.getElementById("mob-chat-close"),msgs=document.getElementById("mob-chat-messages"),input=document.getElementById("mob-chat-input"),send=document.getElementById("mob-chat-send"),suggestions=document.getElementById("mob-chat-suggestions"),open=false,busy=false,history=[];
  function toggle(){{open=!open;panel.classList.toggle("open",open);if(open)input.focus();}}
  btn.addEventListener("click",toggle);
  closeBtn.addEventListener("click",toggle);
  suggestions.addEventListener("click",function(e){{var c=e.target.closest(".mob-chat-suggestion");if(!c)return;input.value=c.textContent;suggestions.style.display="none";submit();}});
  input.addEventListener("keydown",function(e){{if(e.key==="Enter"&&!e.shiftKey){{e.preventDefault();submit();}}}});
  send.addEventListener("click",submit);
  function buildContext(){{
    var k=(SNAPSHOT.kpi||[]).map(function(x){{return x.label+": "+x.last+" "+x.units+" (change: "+(x.change>=0?"+":"")+x.change+")";}}).join("\\n");
    var s=(SNAPSHOT.marketRead||SNAPSHOT.shortBrief||"").replace(/<[^>]+>/g,"").replace(/\\s+/g," ").trim();
    return"=== KPI Snapshot ===\\n"+k+"\\n\\n=== Market Analysis ===\\n"+s;
  }}
  function appendMsg(role,text,thinking){{var w=document.createElement("div");w.className="mob-chat-msg "+role;var b=document.createElement("div");b.className="mob-chat-bubble"+(thinking?" thinking":"");b.textContent=text;w.appendChild(b);msgs.appendChild(w);msgs.scrollTop=msgs.scrollHeight;return b;}}
  async function submit(){{
    var text=input.value.trim();if(!text||busy)return;
    suggestions.style.display="none";input.value="";input.style.height="auto";
    busy=true;send.disabled=true;
    appendMsg("user",text);history.push({{role:"user",content:text}});
    var tb=appendMsg("assistant","Thinking...",true);
    try{{
      var r=await fetch("https://mob-chat.brad-95b.workers.dev",{{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{messages:history,context:buildContext()}})}});
      if(!r.ok){{var e=await r.json().catch(function(){{return{{error:"Unknown error"}};}});tb.textContent="Error: "+(e.error||r.statusText);}}
      else{{var d=await r.json();var rep=d.content||"(no response)";tb.textContent=rep;tb.classList.remove("thinking");history.push({{role:"assistant",content:rep}});}}
    }}catch(e){{tb.textContent="Network error.";}}
    busy=false;send.disabled=false;msgs.scrollTop=msgs.scrollHeight;input.focus();
  }}
  input.addEventListener("input",function(){{input.style.height="auto";input.style.height=Math.min(input.scrollHeight,100)+"px";}});
}})();
</script>

</body>
</html>
"""


def kpi_redesign_css_for_landing():
    return _KPI_CSS_BODY


def kpi_redesign_js_for_landing():
    return _KPI_JS_BODY


# ─────────────────────────────────────────────────────────────────────────────
# Margins page builder
# ─────────────────────────────────────────────────────────────────────────────

MARGINS_PROMPT = """You are a senior refining/petroleum analyst writing this week's
market read for refining margins and crude differentials. Voice: tight, data-anchored,
specific. Reference real refining economics — utilization, crack structure (gasoline-led
vs. distillate-led margin), Brent–WTI spread implications for export economics, seasonal
patterns (summer driving = gasoline cracks firm; winter = distillate cracks firm),
hurricane season risk for Gulf Coast crackers, ULSD/heating-oil cracks, and how cracks
relate to the inventory dynamics this week.

{freshness_instruction}

IMPORTANT — DATA CADENCE & FRAMING:
- ALL PRICES in this data are prior-session NYMEX settlements as of {prices_date}.
- Use phrasing like "WTI settled at $X" or "the prior session close was $X" — NEVER
  describe these as "current", "intraday", or "spot" prices.
- Crack spreads are computed from NYMEX futures settlements, not EIA spot prices.
- Do NOT confuse the NYMEX settlement with the EIA daily spot (which lags 3-5 days).

BRENT–WTI SPREAD MECHANICS — GET THIS RIGHT:
- Brent–WTI = Brent price minus WTI price (positive in normal markets).
- WIDE spread (~$7+): WTI cheap vs. Brent → strong US export arb → more US
  crude AND refined product (gasoline, ULSD) exports → domestic cracks softer.
- NARROW spread (~$4 or less): WTI close to Brent → weak export arb → US
  exports of crude AND products LESS competitive → barrels stay home → US
  crude builds → domestic cracks supported by retained product supply.
- DO NOT WRITE: "narrow Brent–WTI keeps US exports competitive" — opposite is true.
- DO NOT WRITE: "narrow spread implies WTI upside" — narrow spread already
  reflects WTI strength; continued weakness in exports pressures WTI DOWN, not up.
- To re-widen the spread and restore export economics, WTI would need to FALL
  (or Brent would need to rise).

WTI PRICE DIRECTION — GET THIS RIGHT:
- The data contains `wti_wow_chg` (WTI week-over-week price change in $/bbl) and
  `wti_wow_chg_label` (e.g. "-$9.27" or "+$1.50"). USE THESE EXPLICITLY.
- If wti_wow_chg is NEGATIVE, WTI fell on the week. Do NOT write that WTI was
  "driven higher" or "rose" — that is factually wrong.
- Crack spreads can widen even when WTI falls if PRODUCT prices fall FURTHER.
  In that case: "products weakened more than crude" or "product settlements
  lagged crude" is correct — but NEVER describe WTI itself as higher.
- NEVER conflate "crude outperformed products" with "WTI rose."

US EXPORT VOLUMES — GET THIS RIGHT:
- The data contains `crude_exp_us_mbd`, `gas_exp_us_mbd`, `dist_exp_us_mbd`
  (latest weekly EIA export volumes in mb/d).
  USE THESE EXPLICITLY before making any claim about export flows.
- If those numbers are at or near multi-year highs, DO NOT write that "exports
  are shut" or "barrels are staying home." That directly contradicts the data.
- Only use arb-shut / exports-diverted language if the export volumes are
  actually LOW relative to the recent range in the data.

LOGICAL-CONSISTENCY CHECK:
- Every cause-and-effect chain must be economically valid. After drafting,
  re-read each "X therefore Y" claim and verify the mechanism actually works
  that direction. If you can't state the mechanism in one short sentence,
  rewrite or drop the claim.

SELF-CONTAINED TEXT — NO CROSS-REFERENCES:
- NEVER write "see below", "see above", "as shown below", "as noted above", "as discussed
  below", or any phrase that references another part of the page. This narrative block
  renders in isolation — there is no "below" or "above" visible to the reader.
- State facts inline; do not point the reader elsewhere.

Return STRICT JSON with this shape (no markdown outside JSON):

{{
  "market_read": "5 sections separated by \\\\n. Each section covers one market: <strong>3-2-1 Crack:</strong>, <strong>Gasoline Crack:</strong>, <strong>Distillate Crack:</strong>, <strong>Jet Crack:</strong>, <strong>Brent–WTI Spread:</strong>. Within each section: (1) state the current settlement print with the w/w change; (2) explain the driver of that move in 1–2 sentences — every cause-and-effect claim must be economically valid; (3) end with <strong>Watch:</strong> followed by 1 sentence on what to monitor going forward. Keep each section to 3–4 sentences total. DO NOT include a Forward Curve section — that lives on its own page.",
  "chart_321":  "2–3 sentences analyzing the 3-2-1 crack seasonality chart — position vs 5-yr band, year-on-year vs 2025, what's driving the move",
  "chart_gas":  "2–3 sentences analyzing the gasoline crack — Memorial Day driving pull, gasoline-led margins, summer specs",
  "chart_dist": "2–3 sentences analyzing the distillate crack — diesel demand, trucking/freight, ULSD specs, weather context",
  "chart_jet":  "2–3 sentences analyzing the jet crack — air travel demand, refinery jet yield decisions, summer travel",
  "chart_bw":   "2–3 sentences analyzing the Brent–WTI spread — US export economics, OPEC supply dynamics, what the spread implies"
}}

DATA (prices as of prior session {prices_date}, inventory week ending {report_date}):
{data}

{wiki_context_block}
"""


def _generate_margins_narratives(ctx):
    """Margins/cracks narratives with sticky fallback — see
    `_generate_morning_brief` for the rationale. On holidays/weekends the
    crack-spread inputs don't change, so we either reuse the cached
    narratives outright or fall back to the last good ones if a fresh API
    call fails."""
    import hashlib
    cache_path = os.path.join(HERE, '.margins_cache.json')
    _wiki_raw_m = _load_wiki_context()
    _wiki_hash_m = hashlib.md5(_wiki_raw_m.encode()).hexdigest()[:8] if _wiki_raw_m else 'none'
    key = hashlib.md5((json.dumps(ctx, sort_keys=True) + _wiki_hash_m).encode()).hexdigest()
    prices_date = ctx.get('prices_date') or ctx.get('report_date')
    fresh_state = ctx.get('freshness_state', 'synced')

    # Post-EIA window — suppress commentary; banner carries the message.
    # See _generate_morning_brief() for the rationale.
    if fresh_state == 'post_eia':
        print('  → margins narratives: suppressed (post_eia — banner carries the message)')
        # Invalidate any pre-release last_good so when state flips back to
        # synced tomorrow morning the data_unchanged shortcut can't reuse a
        # margins brief written BEFORE this EIA report was incorporated.
        try:
            with open(cache_path) as f:
                existing = json.load(f)
            for k in ('last_good', 'last_good_prices_date',
                      'last_good_freshness_state', 'last_good_generated_at',
                      'key', 'value'):
                existing.pop(k, None)
            with open(cache_path, 'w') as f:
                json.dump(existing, f)
        except Exception:
            pass
        return {
            'market_read': '',
            'chart_321': '', 'chart_gas': '', 'chart_dist': '',
            'chart_jet': '', 'chart_bw': '',
        }

    cache = {}
    try:
        with open(cache_path) as f:
            cache = json.load(f)
    except Exception:
        cache = {}
    # 1. Exact-match cache hit
    if cache.get('key') == key and cache.get('value'):
        print('  → margins narratives: from cache')
        return cache['value']
    # 2. Data unchanged since last good run — reuse without an API call.
    #    Must also match freshness_state AND prompt_v: prices_date alone is
    #    identical across the synced→post_eia transition (Wed AM EIA release
    #    lands before any new settle), but the commentary needs to change.
    #    prompt_v gates regen when we update the prompt wording materially.
    last_good = cache.get('last_good')
    if (last_good
            and cache.get('last_good_prices_date') == prices_date
            and cache.get('last_good_freshness_state', 'synced') == fresh_state
            and cache.get('last_good_prompt_v') == ctx.get('prompt_v')
            and cache.get('last_good_wiki_hash') == _wiki_hash_m):
        print(f'  → margins narratives: data unchanged ({prices_date}, {fresh_state}) — reusing last good')
        try:
            cache['key'] = key
            cache['value'] = last_good
            with open(cache_path, 'w') as f:
                json.dump(cache, f)
        except Exception:
            pass
        return last_good
    if ANTHROPIC_API_KEY:
        try:
            print('  → calling Anthropic API (Opus) for margins narratives...')
            _wiki_m = _load_wiki_context()
            wiki_context_block = (
                "INSTITUTIONAL MARKET CONTEXT (multi-week knowledge base — use to "
                "add depth and continuity; do not contradict current session data):\n" + _wiki_m
                if _wiki_m else ''
            )
            prompt = MARGINS_PROMPT.format(
                report_date=ctx['report_date'],
                prices_date=ctx.get('prices_date', ctx['report_date']),
                data=json.dumps(ctx, indent=2),
                wiki_context_block=wiki_context_block,
                freshness_instruction=_freshness_instruction(
                    ctx.get('freshness_state', 'synced'),
                    ctx.get('freshness_inv_through', ctx['report_date']),
                    ctx.get('freshness_eia_release_str', ''),
                ),
            )
            raw = _call_claude(prompt, max_tokens=2500)
            start, end = raw.find('{'), raw.rfind('}')
            if start >= 0 and end > start:
                parsed = json.loads(raw[start:end + 1])
                if 'market_read' in parsed:
                    parsed['market_read'] = _strip_cross_refs(parsed['market_read'])
                    print('  → margins narratives received')
                    try:
                        with open(cache_path, 'w') as f:
                            json.dump({
                                'key': key,
                                'value': parsed,
                                'last_good': parsed,
                                'last_good_prices_date': prices_date,
                                'last_good_freshness_state': fresh_state,
                                'last_good_prompt_v': ctx.get('prompt_v'),
                                'last_good_wiki_hash': _wiki_hash_m,
                                'last_good_generated_at': _utcnow().isoformat() + 'Z',
                            }, f)
                    except Exception: pass
                    return parsed
        except Exception as e:
            print(f'  → margins API failed ({e}), using sticky fallback')
    # 3. API failed — prefer the last good narratives over the thin template
    if last_good:
        print(
            f'  → margins narratives: reusing last good '
            f'(prices through {cache.get("last_good_prices_date")})'
        )
        return last_good
    # 4. Cold start — thin template as last resort
    return {
        'market_read': (
            f'<strong>Headline:</strong> 3-2-1 crack at ${ctx["crack_321_last"]:.2f}/bbl ({ctx["crack_321_chg_label"]} w/w). '
            f'Gasoline crack ${ctx["crack_gas_last"]:.2f}, distillate ${ctx["crack_dist_last"]:.2f}, jet ${ctx["crack_jet_last"]:.2f}.\n'
            f'<strong>Driver:</strong> Refining economics are tracking {ctx["regime"]}.\n'
            f'<strong>Watch next week:</strong> Memorial Day demand pull, hurricane season risk.'
        ),
        'chart_321':  f'3-2-1 crack at <strong>${ctx["crack_321_last"]:.1f}/bbl</strong> ({ctx["crack_321_chg_label"]} w/w).',
        'chart_gas':  f'Gasoline crack <strong>${ctx["crack_gas_last"]:.1f}/bbl</strong>.',
        'chart_dist': f'Distillate crack <strong>${ctx["crack_dist_last"]:.1f}/bbl</strong>.',
        'chart_jet':  f'Jet crack <strong>${ctx["crack_jet_last"]:.1f}/bbl</strong>.',
        'chart_bw':   f'Brent–WTI <strong>${ctx["bw_spread_last"]:.2f}/bbl</strong>.',
    }


def _weekly_from_daily(daily_pairs):
    """Aggregate daily [date, value] pairs to weekly (last value of each ISO week)."""
    if not daily_pairs:
        return []
    by_week = {}
    for d, v in daily_pairs:
        dt = datetime.strptime(d, '%Y-%m-%d')
        iso = dt.isocalendar()
        by_week[(iso[0], iso[1])] = (d, v)  # last obs of week
    return sorted(by_week.values(), key=lambda x: x[0])


def _build_margins_page(prices, latest_date, raw=None):
    cracks = prices.get('cracks', {})
    spot = prices.get('eia_spot', {})
    futures = prices.get('futures', {})

    def last_two(series_obj):
        d = series_obj.get('data', [])
        if len(d) < 2: return (None, None, None)
        return d[-1][1], d[-2][1], d[-1][0]

    # Five 6-card-ready KPI specs: 4 cracks + WTI spot + Brent-WTI spread
    def kpi_for_crack(key, label, color_id):
        last, prev, dt = last_two(cracks.get(key, {}))
        if last is None: return None
        weekly = _weekly_from_daily(cracks[key]['data'])
        spark = [round(v, 2) for _, v in weekly[-12:]]
        latest_dt = datetime.strptime(weekly[-1][0], '%Y-%m-%d')
        latest_week = latest_dt.isocalendar()[1]
        cur_year = latest_dt.year
        band_vals = []
        for d, v in weekly:
            dt2 = datetime.strptime(d, '%Y-%m-%d')
            if dt2.isocalendar()[1] == latest_week and cur_year - 5 <= dt2.year < cur_year:
                band_vals.append(v)
        band_lo = round(min(band_vals), 2) if band_vals else last
        band_hi = round(max(band_vals), 2) if band_vals else last
        yoy = [round(v, 2) for _, v in weekly[-64:-52]] if len(weekly) >= 64 else []
        return {
            'id': color_id, 'label': label,
            'last': round(last, 2), 'change': round(last - prev, 2),
            'units': '$/bbl', 'frequency': 'daily',
            'spark': spark, 'yoy': yoy,
            'band_lo': band_lo, 'band_hi': band_hi,
        }

    def kpi_for_spot(key, label, color_id):
        last, prev, _ = last_two(spot.get(key, {}))
        if last is None: return None
        weekly = _weekly_from_daily(spot[key]['data'])
        spark = [round(v, 2) for _, v in weekly[-12:]]
        latest_dt = datetime.strptime(weekly[-1][0], '%Y-%m-%d')
        latest_week = latest_dt.isocalendar()[1]
        cur_year = latest_dt.year
        band_vals = []
        for d, v in weekly:
            dt2 = datetime.strptime(d, '%Y-%m-%d')
            if dt2.isocalendar()[1] == latest_week and cur_year - 5 <= dt2.year < cur_year:
                band_vals.append(v)
        band_lo = round(min(band_vals), 1) if band_vals else last
        band_hi = round(max(band_vals), 1) if band_vals else last
        yoy = [round(v, 2) for _, v in weekly[-64:-52]] if len(weekly) >= 64 else []
        units = spot[key].get('units', '')
        return {
            'id': color_id, 'label': label,
            'last': round(last, 2), 'change': round(last - prev, 2),
            'units': units,
            'spark': spark, 'yoy': yoy,
            'band_lo': band_lo, 'band_hi': band_hi,
        }

    def kpi_for_front_month(commodity_key, label, color_id, units='$/bbl', decimals=2):
        """Front-month NYMEX settlement card. Uses curve[0] (explicit prior-day
        settle for the front contract) for the value/change, and front_history
        for the sparkline and 5-yr same-week-of-year band — mirrors the home
        page's WTI KPI so the two pages agree on price."""
        cdata = futures.get(commodity_key, {}) or {}
        curve = cdata.get('curve') or []
        front_hist = cdata.get('front_history') or []
        if not curve:
            return None
        c0 = curve[0]
        last = c0.get('price')
        if last is None:
            return None
        prev = c0.get('price_1d')
        if prev is None:
            prev = last
        weekly = _weekly_from_daily(front_hist) if front_hist else []
        spark = [round(v, decimals) for _, v in weekly[-12:]] if weekly else [round(last, decimals)]
        if spark:
            spark[-1] = round(last, decimals)
        yoy = [round(v, decimals) for _, v in weekly[-64:-52]] if len(weekly) >= 64 else []
        try:
            latest_dt = datetime.strptime(weekly[-1][0], '%Y-%m-%d') if weekly else latest_date
            latest_week = latest_dt.isocalendar()[1]
            cur_year = latest_dt.year
            band_vals = []
            for d, v in weekly:
                dt2 = datetime.strptime(d, '%Y-%m-%d')
                if dt2.isocalendar()[1] == latest_week and cur_year - 5 <= dt2.year < cur_year:
                    band_vals.append(v)
            band_lo = round(min(band_vals), decimals) if band_vals else round(last, decimals)
            band_hi = round(max(band_vals), decimals) if band_vals else round(last, decimals)
        except Exception:
            band_lo = round(last, decimals); band_hi = round(last, decimals)
        return {
            'id': color_id, 'label': label,
            'last': round(last, decimals), 'change': round(last - prev, decimals),
            'units': units, 'frequency': 'daily',
            'spark': spark, 'yoy': yoy,
            'band_lo': band_lo, 'band_hi': band_hi,
            'contract': c0.get('contract'),
        }

    # Strip order: cracks first (the page's headline metrics), then crudes
    # (WTI, Brent, Brent–WTI spread grouped together), then refined products.
    margin_kpis = [
        kpi_for_crack('crack_321', '3-2-1 Crack', 'crack321'),
        kpi_for_crack('crack_gasoline', 'Gasoline Crack', 'crackgas'),
        kpi_for_crack('crack_distillate', 'Distillate Crack', 'crackdist'),
        kpi_for_crack('crack_jet', 'Jet Crack', 'crackjet'),
        kpi_for_front_month('wti',   'WTI',   'wti',   units='$/bbl', decimals=2),
        kpi_for_front_month('brent', 'Brent', 'brent', units='$/bbl', decimals=2),
        kpi_for_crack('brent_wti_spread', 'Brent–WTI', 'bwspread'),
        kpi_for_front_month('rbob',  'RBOB',  'rbob',  units='$/gal', decimals=4),
        kpi_for_front_month('ulsd',  'HO',    'ulsd',  units='$/gal', decimals=4),
    ]
    margin_kpis = [k for k in margin_kpis if k is not None]

    # Narrative context
    crack_321_last, crack_321_prev, _ = last_two(cracks.get('crack_321', {}))
    crack_gas_last, crack_gas_prev, _ = last_two(cracks.get('crack_gasoline', {}))
    crack_dist_last, _, _ = last_two(cracks.get('crack_distillate', {}))
    crack_jet_last, _, _ = last_two(cracks.get('crack_jet', {}))
    bw_last, _, _ = last_two(cracks.get('brent_wti_spread', {}))
    wti_front = (futures.get('wti', {}).get('front_history') or [[None, None]])[-1][1]
    wti_curve = futures.get('wti', {}).get('curve', [])
    regime = 'distillate-led' if (crack_dist_last or 0) > (crack_gas_last or 0) else 'gasoline-led'

    def chg_label(last, prev):
        if last is None or prev is None: return 'n/a'
        d = last - prev
        return f'{("+" if d >= 0 else "")}{d:.2f}'

    # Resolve prices_as_of (prior NYMEX settlement date)
    _prices_as_of = prices.get('prices_as_of', '')
    try:
        _prices_date_str = datetime.strptime(_prices_as_of, '%Y-%m-%d').strftime('%B %-d, %Y') if _prices_as_of else latest_date.strftime('%B %-d, %Y')
    except Exception:
        _prices_date_str = _prices_as_of or latest_date.strftime('%B %-d, %Y')

    # Freshness state — compute BEFORE narrative generation so the prompt can
    # frame commentary correctly (lead-with-EIA in post_eia, tie-prices-to-EIA
    # in synced). See _compute_freshness() docstring.
    _freshness = _compute_freshness(_prices_as_of, latest_date)
    inv_latest_date = _freshness['inv_through']
    eia_banner_html = _freshness['banner_html']

    # In post_eia we suppress the AI Brief entirely — the banner carries the
    # message. The section is hidden completely (header + body).
    if _freshness['state'] == 'post_eia':
        ai_margin_brief_section_html = ''
    else:
        # Mirrors the home-page "Today's Read" hero — see AI_BRIEF_BOX_CSS.
        ai_margin_brief_section_html = (
            '<section class="ai-brief-box">'
            '<div class="ai-brief-box-head">'
            '<div class="ai-brief-box-left">'
            '<span class="ai-brief-box-eyebrow">AI Margin Brief</span>'
            '<span class="ai-brief-box-pill">AI Generated</span>'
            '</div>'
            '</div>'
            '<div class="market-read" id="market-read"></div>'
            '</section>'
        )

    # Use explicit front-month contract price (curve[0]) for WTI settlement —
    # NOT front_history[-1] which is CL=F continuous and can differ on roll day.
    wti_settlement = (wti_curve[0]['price'] if wti_curve else wti_front) or 0

    # Pull actual EIA inventory figures so the AI doesn't have to infer build/draw
    # direction from price signals alone (contango ≠ build when stocks are drawing).
    _inv = {}
    if raw:
        try:
            s = raw['series']
            def _to_mb(kb): return round(kb / 1000.0, 2)
            def _chg_mb(key):
                d = s[key]['data']
                return round((d[-1][1] - d[-2][1]) / 1000.0, 2)
            _inv = {
                'crude_us_last_mb':    _to_mb(s['crude_us']['data'][-1][1]),
                'crude_us_chg_mb':     _chg_mb('crude_us'),
                'crude_us_date':       s['crude_us']['data'][-1][0],
                'cushing_last_mb':     _to_mb(s['crude_cushing']['data'][-1][1]),
                'cushing_chg_mb':      _chg_mb('crude_cushing'),
                'gas_us_chg_mb':       _chg_mb('gas_us'),
                'dist_us_chg_mb':      _chg_mb('dist_us'),
                'refutil_us_last_pct': round(s['refutil_us']['data'][-1][1], 1),
            }
        except Exception:
            pass

    nctx = {
        'report_date': latest_date.strftime('%B %-d, %Y'),
        'prices_date': _prices_date_str,   # prior NYMEX settlement date for AI framing
        'crack_321_last': crack_321_last or 0,
        'crack_gas_last': crack_gas_last or 0,
        'crack_dist_last': crack_dist_last or 0,
        'crack_jet_last': crack_jet_last or 0,
        'bw_spread_last': bw_last or 0,
        'wti_front_nymex_settlement': wti_settlement,  # prior session NYMEX settlement
        'crack_321_chg_label': chg_label(crack_321_last, crack_321_prev),
        'crack_gas_chg_label': chg_label(crack_gas_last, crack_gas_prev),
        'regime': regime,
        'wti_curve_12mo': [c['price'] for c in wti_curve[:12]],
        'recent_321_4wk': [round(v, 2) for _, v in cracks.get('crack_321', {}).get('data', [])[-20:]],
        'data_note': 'All prices are prior-session NYMEX settlements, not EIA spot. Reference them as settlements.',
        'inventory_actuals': _inv,  # EIA weekly stock change — use these, not price-inferred direction
        'inventory_note': (
            'inventory_actuals contains the ACTUAL EIA weekly stock changes. '
            'A negative crude_us_chg_mb means crude drew (stocks fell). '
            'Do NOT infer build/draw direction from the WTI forward curve shape — use these figures.'
        ),
        # Two-phase refresh design — included in ctx so it lands in the cache
        # key (forcing regen on the synced ↔ post_eia transition) AND so the
        # generator can swap the prompt's framing instruction.
        'freshness_state':           _freshness['state'],
        'freshness_inv_through':     _freshness['inv_through'],
        'freshness_eia_release_str': _freshness['eia_release_str'],
        # Bump when prompt wording changes materially — forces regen across
        # the synced cache too. Current bump: Brent-WTI mechanics guardrail
        # + logical-consistency check.
        'prompt_v':                  '2026-05-30-wti-direction-exports',
    }

    # WTI week-over-week price change — give the model the explicit direction
    # so it can't hallucinate WTI moving the wrong way.
    try:
        _wti_1w = wti_curve[0].get('price_1w') if wti_curve else None
        _wti_now = wti_curve[0].get('price') if wti_curve else None
        if _wti_1w and _wti_now:
            _wti_wow = round(_wti_now - _wti_1w, 2)
            nctx['wti_wow_chg'] = _wti_wow
            nctx['wti_wow_chg_label'] = f'{"+" if _wti_wow >= 0 else ""}{_wti_wow:.2f}'
    except Exception:
        pass

    # EIA export volumes (latest weekly observation, mb/d) —
    # prevent false "arb shut / barrels staying home" claims.
    try:
        s = raw['series']
        def _last_mbd(key):
            d = s[key]['data']
            return round(d[-1][1] / 1000.0, 2) if d else None
        nctx['crude_exp_us_mbd'] = _last_mbd('crude_exp_us')
        nctx['gas_exp_us_mbd']   = _last_mbd('gas_exp_us')
        nctx['dist_exp_us_mbd']  = _last_mbd('dist_exp_us')
        nctx['exports_note'] = (
            'These are the latest weekly EIA export volumes in mb/d. '
            'Only describe exports as "shut" or "staying home" if these '
            'numbers are materially below recent norms.'
        )
    except Exception:
        pass

    narratives = _generate_margins_narratives(nctx)

    # Build BOTH weekly and daily seasonal hist for each crack — toggled in UI
    def build_hist_weekly(crack_key):
        weekly = _weekly_from_daily(cracks.get(crack_key, {}).get('data', []))
        out = {}
        for d, v in weekly:
            dt = datetime.strptime(d, '%Y-%m-%d')
            out.setdefault(dt.year, {})[dt.isocalendar()[1]] = round(v, 2)
        return out

    def build_hist_daily(crack_key):
        data = cracks.get(crack_key, {}).get('data', [])
        out = {}
        for d, v in data:
            dt = datetime.strptime(d, '%Y-%m-%d')
            doy = dt.timetuple().tm_yday
            out.setdefault(dt.year, {})[doy] = round(v, 2)
        return out

    crack_keys = ['crack_321', 'crack_gasoline', 'crack_distillate', 'crack_jet', 'brent_wti_spread']
    crack_hists_weekly = {k: build_hist_weekly(k) for k in crack_keys}
    crack_hists_daily  = {k: build_hist_daily(k)  for k in crack_keys}

    # Build front-month price history seasonal hists for WTI, Brent, RBOB, ULSD
    def build_price_hist_weekly(commodity_key):
        hist = futures.get(commodity_key, {}).get('front_history', [])
        weekly = _weekly_from_daily(hist)
        out = {}
        for d, v in weekly:
            dt = datetime.strptime(d, '%Y-%m-%d')
            out.setdefault(dt.year, {})[dt.isocalendar()[1]] = round(v, 4)
        return out

    def build_price_hist_daily(commodity_key):
        hist = futures.get(commodity_key, {}).get('front_history', [])
        out = {}
        for d, v in hist:
            dt = datetime.strptime(d, '%Y-%m-%d')
            out.setdefault(dt.year, {})[dt.timetuple().tm_yday] = round(v, 4)
        return out

    price_keys = ['wti', 'brent', 'rbob', 'ulsd']
    price_hists_weekly = {k: build_price_hist_weekly(k) for k in price_keys}
    price_hists_daily  = {k: build_price_hist_daily(k)  for k in price_keys}

    current_year = latest_date.year

    refreshed = _utcnow().strftime('%b %-d, %Y')
    # Live ET time + date for the AI Margin Brief refresh stamp.
    refreshed_time, refreshed_date = _refreshed_stamp_et()
    report_date_str = latest_date.strftime('%B %-d, %Y')
    next_release_str = _next_wpsr_release_str(latest_date)
    # Latest trade-data date — use prices_as_of (prior settlement day) when present
    trade_through_dt = latest_date
    if prices.get('prices_as_of'):
        try:
            trade_through_dt = datetime.strptime(prices['prices_as_of'], '%Y-%m-%d')
            prices_latest_date = trade_through_dt.strftime('%B %-d, %Y')
        except Exception:
            prices_latest_date = prices['prices_as_of']
    else:
        crack_d = cracks.get('crack_321', {}).get('data', [])
        if crack_d:
            try:
                trade_through_dt = datetime.strptime(crack_d[-1][0], '%Y-%m-%d')
                prices_latest_date = trade_through_dt.strftime('%B %-d, %Y')
            except Exception:
                prices_latest_date = crack_d[-1][0]
        else:
            prices_latest_date = report_date_str

    # Next refresh = day after the next NYMEX trade date following the trade-through date
    try:
        next_refresh_str = _next_refresh_date(trade_through_dt).strftime('%B %-d, %Y')
    except Exception:
        next_refresh_str = ''

    # Normalize: real newlines + bold paragraph leads.
    market_read_norm = _normalize_narrative(narratives.get('market_read', ''))
    # Front-month price labels for price chart section headers
    def front_contract_label(commodity_key):
        curve = futures.get(commodity_key, {}).get('curve', [])
        return curve[0].get('contract', '') if curve else ''

    snapshot_js = json.dumps({
        'kpi': margin_kpis,
        'marketRead': market_read_norm,
        'chartCaptions': {
            'crack_321':        narratives.get('chart_321', ''),
            'crack_gasoline':   narratives.get('chart_gas', ''),
            'crack_distillate': narratives.get('chart_dist', ''),
            'crack_jet':        narratives.get('chart_jet', ''),
            'brent_wti_spread': narratives.get('chart_bw', ''),
        },
        'reportDate': report_date_str,
        'lastRefreshed': refreshed,
        'crackHistsWeekly': crack_hists_weekly,
        'crackHistsDaily':  crack_hists_daily,
        'priceHistsWeekly': price_hists_weekly,
        'priceHistsDaily':  price_hists_daily,
        'priceUnits': {'wti': '$/bbl', 'brent': '$/bbl', 'rbob': '$/gal', 'ulsd': '$/gal'},
        'priceContracts': {k: front_contract_label(k) for k in price_keys},
    })

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="description" content="Crack spread seasonality charts — 3-2-1, gasoline, distillate, jet cracks and Brent–WTI spread vs 5-year bands.">
<meta name="theme-color" content="#07090d">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self' https://mob-chat.brad-95b.workers.dev;">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
{_FAVICON}
{_og_tags('Cracks & Prices · MOB', 'Crack spread seasonality charts — 3-2-1, gasoline, distillate, jet cracks and Brent–WTI spread vs 5-year bands.', 'margins')}
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Petroleum Intelligence — Cracks & Prices</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.js" integrity="sha384-iU8HYtnGQ8Cy4zl7gbNMOhsDTTKX02BTXptVP/vqAWIaTfM7isw76iyZCsjL2eVi" crossorigin="anonymous"></script>
<style>
:root {{
  color-scheme: dark;
  --bg: #0f1117; --panel: #1a1d24; --panel-2: #232730;
  --border: #25272e; --border-soft: #1c1e24;
  --text: #e4e7ec; --muted: #9aa0ac; --muted-2: #c1c5cf;
  --build: #16a34a; --draw: #dc2626; --accent: #f59e0b;
}}
* {{ box-sizing: border-box; }}
html, body {{ background: var(--bg); margin: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  color: var(--text); font-size: 15px; line-height: 1.5; }}
.container {{ max-width: 1500px; margin: 0 auto; padding: 14px; }}
.header {{ display: flex; justify-content: space-between; align-items: flex-start;
  flex-wrap: wrap; gap: 8px; margin-bottom: 18px; padding-bottom: 12px; border-bottom: 1px solid var(--border); }}
.header h1 {{ margin: 0; font-size: 22px; font-weight: 600; letter-spacing: -0.3px; }}
.header .subtitle {{ font-size: 12px; color: var(--muted); margin-top: 3px; }}
.update-info {{ text-align: right; font-size: 11px; color: var(--muted); line-height: 1.6; }}
.update-info strong {{ color: var(--text); font-weight: 500; }}
.update-info .pill {{ display: inline-block; padding: 2px 9px; background: rgba(22,163,74,0.15);
  color: #4ade80; border-radius: 10px; font-weight: 500; font-size: 11px; letter-spacing: 0.4px;
  text-transform: uppercase; margin-bottom: 4px; border: 1px solid rgba(22,163,74,0.35); }}
.kpi-strip {{ display: grid; grid-template-columns: repeat(5, 1fr) !important; gap: 8px; margin-bottom: 18px; max-width: 1300px; margin-left: auto !important; margin-right: auto !important; }}
@media (max-width: 1280px) {{ .kpi-strip {{ grid-template-columns: repeat(3, 1fr) !important; }} }}
@media (max-width: 900px)  {{ .kpi-strip {{ grid-template-columns: repeat(2, 1fr) !important; }} }}
.section {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
  padding: 16px 18px; margin-bottom: 14px; }}
.section-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
.section-title {{ font-size: 14px; font-weight: 600; margin: 0; letter-spacing: -0.1px; }}
.section-subtitle {{ font-size: 11px; color: var(--muted); font-weight: 400; margin-left: 8px; text-transform: none; letter-spacing: 0; }}
.badge {{ display: inline-block; font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 999px;
  vertical-align: 2px; letter-spacing: 0.7px; text-transform: uppercase; margin-left: 6px; }}
.badge-ai {{ background: linear-gradient(90deg, rgba(167,139,250,0.18), rgba(96,165,250,0.18));
  color: #c4b5fd; border: 1px solid rgba(167,139,250,0.4); }}
.market-read {{ font-size: 13.5px; line-height: 1.7; color: var(--muted-2); }}
.market-read strong {{ color: var(--text); }}
.market-read p {{ margin: 0 0 10px; }}
.chart-container {{ height: 380px; position: relative; }}
.curve-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
@media (max-width: 1000px) {{ .curve-grid {{ grid-template-columns: 1fr; }} }}
.chart-caption {{
  margin-top: 12px; padding-top: 10px;
  border-top: 1px solid var(--border-soft);
  font-size: 12.5px; color: var(--muted-2); line-height: 1.55;
}}
.chart-caption strong {{ color: var(--text); font-weight: 600; }}
.chart-caption:empty {{ display: none; }}
/* Resolution toggle */
.resolution-toggle-bar {{
  display: flex; align-items: center; gap: 6px;
  padding: 8px 4px; margin-bottom: 6px;
}}
.resolution-label {{
  font-size: 12px; color: var(--muted);
  letter-spacing: 0;
  font-weight: 600; margin-right: 6px;
}}
.resolution-toggle {{
  padding: 6px 14px; border-radius: 14px;
  background: var(--panel-2); border: 1px solid var(--border);
  color: var(--muted-2); font-size: 11px; font-weight: 500;
  cursor: pointer; transition: background 0.15s, color 0.15s, border-color 0.15s;
  font-family: inherit;
}}
.resolution-toggle:hover {{ color: var(--text); border-color: #3a3d46; }}
.resolution-toggle.active {{
  background: var(--text); color: var(--bg);
  border-color: var(--text); font-weight: 600;
}}
.resolution-hint {{
  font-size: 11px; color: var(--muted);
  margin-left: auto; font-style: italic;
}}
{NAV_CSS}
{_KPI_CSS_BODY}
{AI_BRIEF_BOX_CSS}
.section-divider {{
  font-size: 13px; font-weight: 700; letter-spacing: -0.1px;
  color: var(--muted-2); margin: 28px 0 12px;
  padding-bottom: 8px; border-bottom: 1px solid var(--border-soft);
  text-transform: uppercase; letter-spacing: 0.5px;
}}
footer {{ margin-top: 24px; padding-top: 12px; border-top: 1px solid var(--border);
  font-size: 11px; color: var(--muted); text-align: center; }}
footer a {{ color: var(--accent); text-decoration: none; }}
</style>
</head>
<body>
{_render_nav('margins.html')}

<div class="container">
  <div class="header">
    <div>
      <h1>Cracks · Prices</h1>
      <div class="subtitle">3-2-1, gasoline, distillate, jet cracks · Brent–WTI spread</div>
    </div>
    <div class="update-info">
      <span class="pill">NYMEX SETTLEMENT</span><br>
      Prices thru: <strong>{prices_latest_date}</strong><br>
      Inventories thru: <strong>{inv_latest_date}</strong><br>
      Next refresh: <strong>{next_refresh_str}</strong> · <strong>~5:00 AM ET</strong>
    </div>
  </div>

  <div class="kpi-strip" id="kpi-strip"></div>

  {eia_banner_html}

  {ai_margin_brief_section_html}

  <div class="resolution-toggle-bar">
    <span class="resolution-label">Chart resolution:</span>
    <button class="resolution-toggle active" data-res="weekly" aria-pressed="true">Weekly</button>
    <button class="resolution-toggle" data-res="daily" aria-pressed="false">Daily</button>
    <span class="resolution-hint" id="resolution-hint">5-yr band + prior + current year</span>
  </div>

  <div class="section">
    <div class="section-header">
      <h2 class="section-title">3-2-1 Crack Seasonality
        <span class="section-subtitle" id="subtitle-321">5-yr band · prior year · current</span></h2>
    </div>
    <div class="chart-container"><canvas data-crack="crack_321" role="img" aria-label="3-2-1 crack spread seasonality chart"></canvas></div>
    <div class="chart-caption" data-caption="crack_321"></div>
  </div>

  <div class="curve-grid">
    <div class="section">
      <div class="section-header">
        <h2 class="section-title">Gasoline Crack
          <span class="section-subtitle">5-yr seasonality, $/bbl</span></h2>
      </div>
      <div class="chart-container"><canvas data-crack="crack_gasoline" role="img" aria-label="Gasoline crack spread seasonality chart"></canvas></div>
      <div class="chart-caption" data-caption="crack_gasoline"></div>
    </div>
    <div class="section">
      <div class="section-header">
        <h2 class="section-title">Distillate Crack
          <span class="section-subtitle">5-yr seasonality, $/bbl</span></h2>
      </div>
      <div class="chart-container"><canvas data-crack="crack_distillate" role="img" aria-label="Distillate crack spread seasonality chart"></canvas></div>
      <div class="chart-caption" data-caption="crack_distillate"></div>
    </div>
    <div class="section">
      <div class="section-header">
        <h2 class="section-title">Jet Crack
          <span class="section-subtitle">5-yr seasonality, $/bbl</span></h2>
      </div>
      <div class="chart-container"><canvas data-crack="crack_jet" role="img" aria-label="Jet crack spread seasonality chart"></canvas></div>
      <div class="chart-caption" data-caption="crack_jet"></div>
    </div>
    <div class="section">
      <div class="section-header">
        <h2 class="section-title">Brent–WTI Spread
          <span class="section-subtitle">5-yr seasonality, $/bbl</span></h2>
      </div>
      <div class="chart-container"><canvas data-crack="brent_wti_spread" role="img" aria-label="Brent–WTI spread seasonality chart"></canvas></div>
      <div class="chart-caption" data-caption="brent_wti_spread"></div>
    </div>
  </div>

  <!-- ─── Front-month price charts ─────────────────────────────────────────── -->
  <h2 class="section-divider">Front-Month Settlement Prices</h2>

  <div class="curve-grid">
    <div class="section">
      <div class="section-header">
        <h2 class="section-title">WTI Crude
          <span class="section-subtitle">NYMEX front-month · $/bbl · 5-yr seasonality</span></h2>
      </div>
      <div class="chart-container"><canvas data-price="wti"></canvas></div>
    </div>
    <div class="section">
      <div class="section-header">
        <h2 class="section-title">Brent Crude
          <span class="section-subtitle">ICE front-month · $/bbl · 5-yr seasonality</span></h2>
      </div>
      <div class="chart-container"><canvas data-price="brent"></canvas></div>
    </div>
    <div class="section">
      <div class="section-header">
        <h2 class="section-title">NYMEX RBOB Gasoline
          <span class="section-subtitle">Front-month · $/gal · 5-yr seasonality</span></h2>
      </div>
      <div class="chart-container"><canvas data-price="rbob"></canvas></div>
    </div>
    <div class="section">
      <div class="section-header">
        <h2 class="section-title">NYMEX NY Harbor ULSD
          <span class="section-subtitle">Front-month · $/gal · 5-yr seasonality</span></h2>
      </div>
      <div class="chart-container"><canvas data-price="ulsd"></canvas></div>
    </div>
  </div>

  <footer>
    Data through {prices_latest_date}
  </footer>
</div>

<script>
{_SIGNOUT_JS}
const SNAPSHOT = {snapshot_js};
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

// Render market read paragraphs (guard against absent element — in post_eia
// state the AI Brief section is omitted entirely, so this node won't exist).
(function() {{
  const el = document.getElementById('market-read');
  if (!el || !SNAPSHOT.marketRead) return;
  el.innerHTML = '<p>' + SNAPSHOT.marketRead.split('\\n').join('</p><p>') + '</p>';
}})();

{_KPI_JS_BODY}

// ─── Two render functions: weekly (with band) and daily (lines only) ───

function renderWeekly(canvas, key){{
  const ctx = canvas.getContext('2d');
  const hist = SNAPSHOT.crackHistsWeekly[key] || {{}};
  const years = Object.keys(hist).map(Number).sort();
  if (!years.length) return;
  const curYear = years[years.length - 1];
  const priorYear = curYear - 1;
  const bandYears = years.filter(y => y >= curYear - 5 && y < curYear);
  const weeks = Array.from({{length: 52}}, (_, i) => i + 1);
  const bandHi = weeks.map(w => {{
    const vs = bandYears.map(y => hist[y]?.[w]).filter(v => v != null);
    return vs.length ? Math.max(...vs) : null;
  }});
  const bandLo = weeks.map(w => {{
    const vs = bandYears.map(y => hist[y]?.[w]).filter(v => v != null);
    return vs.length ? Math.min(...vs) : null;
  }});
  const bandAvg = weeks.map(w => {{
    const vs = bandYears.map(y => hist[y]?.[w]).filter(v => v != null);
    return vs.length ? vs.reduce((a, b) => a + b, 0) / vs.length : null;
  }});
  const priorLine = weeks.map(w => hist[priorYear]?.[w] ?? null);
  const curLine   = weeks.map(w => hist[curYear]?.[w] ?? null);
  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: weeks.map(w => 'W' + w),
      datasets: [
        {{ label: '5Y High', data: bandHi, borderColor: 'rgba(140,150,170,0.45)', borderWidth: 1,
           borderDash: [4,4], backgroundColor: 'rgba(140,150,170,0.18)', fill: '+1',
           pointRadius: 0, tension: 0.25 }},
        {{ label: '5Y Low', data: bandLo, borderColor: 'rgba(140,150,170,0.45)', borderWidth: 1,
           borderDash: [4,4], fill: false, pointRadius: 0, tension: 0.25 }},
        {{ label: '5Y Avg', data: bandAvg, borderColor: 'rgba(180,190,210,0.55)', borderWidth: 1,
           borderDash: [2,3], fill: false, pointRadius: 0, tension: 0.3 }},
        {{ label: String(priorYear), data: priorLine, borderColor: '#fbbf24', borderWidth: 2,
           fill: false, pointRadius: 0, tension: 0.3 }},
        {{ label: String(curYear), data: curLine, borderColor: '#ffffff', borderWidth: 2.5,
           fill: false, pointRadius: 0, tension: 0.3 }},
      ],
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{ labels: {{ color: '#cbd5e1', boxWidth: 8 }}, position: 'top', align: 'end' }},
        tooltip: {{
          mode: 'index', intersect: false,
          backgroundColor: 'rgba(15,17,23,0.96)',
          titleColor: '#ffffff', bodyColor: '#d1d5db',
          borderColor: 'rgba(140,150,170,0.4)', borderWidth: 1,
          padding: 10, titleFont: {{ size: 11, weight: '600' }}, bodyFont: {{ size: 11 }},
          callbacks: {{
            title: items => 'Week ' + (items[0].dataIndex + 1),
            label: ctx => ctx.parsed.y == null ? null
                          : ' ' + ctx.dataset.label + ': $' + Number(ctx.parsed.y).toFixed(2) + '/bbl',
          }},
        }},
      }},
      scales: {{
        x: {{ ticks: {{ font: {{size: 10}}, color: '#9aa0ac', autoSkip: false,
              callback: function(_, i){{ return (i % 4 === 0) ? MONTHS[Math.floor(i/52*12)] : ''; }} }},
              grid: {{ color: 'rgba(255,255,255,0.04)' }} }},
        y: {{ ticks: {{ font: {{size: 10}}, color: '#9aa0ac', callback: v => '$' + v }},
              grid: {{ color: 'rgba(255,255,255,0.06)' }} }},
      }},
      elements: {{ point: {{ radius: 0, hoverRadius: 4, hitRadius: 12 }} }},
    }},
  }});
}}

function renderDaily(canvas, key){{
  const ctx = canvas.getContext('2d');
  const hist = SNAPSHOT.crackHistsDaily[key] || {{}};
  const years = Object.keys(hist).map(Number).sort();
  if (!years.length) return;
  const curYear = years[years.length - 1];
  const priorYear = curYear - 1;
  const days = Array.from({{length: 365}}, (_, i) => i + 1);
  const priorLine = days.map(d => hist[priorYear]?.[d] ?? null);
  const curLine   = days.map(d => hist[curYear]?.[d] ?? null);
  const firstOfMonth = [1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335];
  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: days,
      datasets: [
        {{ label: String(priorYear), data: priorLine, borderColor: '#fbbf24', borderWidth: 1.5,
           fill: false, pointRadius: 0, tension: 0, spanGaps: true }},
        {{ label: String(curYear), data: curLine, borderColor: '#ffffff', borderWidth: 2,
           fill: false, pointRadius: 0, tension: 0, spanGaps: true }},
      ],
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{ labels: {{ color: '#cbd5e1', boxWidth: 8 }}, position: 'top', align: 'end' }},
        tooltip: {{
          mode: 'index', intersect: false,
          backgroundColor: 'rgba(15,17,23,0.96)',
          titleColor: '#ffffff', bodyColor: '#d1d5db',
          borderColor: 'rgba(140,150,170,0.4)', borderWidth: 1,
          padding: 10, titleFont: {{ size: 11, weight: '600' }}, bodyFont: {{ size: 11 }},
          callbacks: {{
            title: items => 'Day ' + (items[0].dataIndex + 1),
            label: ctx => ctx.parsed.y == null ? null
                          : ' ' + ctx.dataset.label + ': $' + Number(ctx.parsed.y).toFixed(2) + '/bbl',
          }},
        }},
      }},
      scales: {{
        x: {{ ticks: {{ font: {{size: 10}}, color: '#9aa0ac', autoSkip: false, maxRotation: 0,
              callback: function(val, i){{
                const doy = i + 1;
                const idx = firstOfMonth.indexOf(doy);
                return idx >= 0 ? MONTHS[idx] : '';
              }} }},
              grid: {{ color: 'rgba(255,255,255,0.04)' }} }},
        y: {{ ticks: {{ font: {{size: 10}}, color: '#9aa0ac', callback: v => '$' + v }},
              grid: {{ color: 'rgba(255,255,255,0.06)' }} }},
      }},
      elements: {{ point: {{ radius: 0, hoverRadius: 4, hitRadius: 12 }} }},
    }},
  }});
}}

let _currentRes = 'weekly';
function renderAllCrackCharts(){{
  document.querySelectorAll('canvas[data-crack]').forEach(c => {{
    const existing = Chart.getChart(c);
    if (existing) existing.destroy();
    if (_currentRes === 'weekly') renderWeekly(c, c.dataset.crack);
    else renderDaily(c, c.dataset.crack);
  }});
}}

document.querySelectorAll('.resolution-toggle').forEach(btn => {{
  btn.addEventListener('click', () => {{
    _currentRes = btn.dataset.res;
    document.querySelectorAll('.resolution-toggle').forEach(b => b.classList.toggle('active', b === btn));
    const hint = document.getElementById('resolution-hint');
    if (hint) hint.textContent = _currentRes === 'weekly'
      ? '5-yr band + prior + current year'
      : 'daily resolution · 2025 + 2026 lines only';
    renderAllCrackCharts();
    renderAllPriceCharts();
  }});
}});

renderAllCrackCharts();
// Populate captions
Object.entries(SNAPSHOT.chartCaptions || {{}}).forEach(([key, caption]) => {{
  const el = document.querySelector(`[data-caption="${{key}}"]`);
  if (el && caption) el.innerHTML = caption;
}});

// ─── Front-month price charts ────────────────────────────────────────────────

function renderPriceWeekly(canvas, key) {{
  const ctx = canvas.getContext('2d');
  const hist = (SNAPSHOT.priceHistsWeekly || {{}})[key] || {{}};
  const years = Object.keys(hist).map(Number).sort();
  if (!years.length) return;
  const curYear = years[years.length - 1];
  const priorYear = curYear - 1;
  const bandYears = years.filter(y => y >= curYear - 5 && y < curYear);
  const weeks = Array.from({{length: 52}}, (_, i) => i + 1);
  const bandHi = weeks.map(w => {{
    const vs = bandYears.map(y => hist[y]?.[w]).filter(v => v != null);
    return vs.length ? Math.max(...vs) : null;
  }});
  const bandLo = weeks.map(w => {{
    const vs = bandYears.map(y => hist[y]?.[w]).filter(v => v != null);
    return vs.length ? Math.min(...vs) : null;
  }});
  const bandAvg = weeks.map(w => {{
    const vs = bandYears.map(y => hist[y]?.[w]).filter(v => v != null);
    return vs.length ? vs.reduce((a, b) => a + b, 0) / vs.length : null;
  }});
  const priorLine = weeks.map(w => hist[priorYear]?.[w] ?? null);
  const curLine   = weeks.map(w => hist[curYear]?.[w] ?? null);
  const units = (SNAPSHOT.priceUnits || {{}})[key] || '';
  const fmt = v => units === '$/gal' ? '$' + v.toFixed(4) : '$' + v.toFixed(2);
  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: weeks.map(w => 'W' + w),
      datasets: [
        {{ label: '5Y High', data: bandHi, borderColor: 'rgba(140,150,170,0.45)', borderWidth: 1,
           borderDash: [4,4], backgroundColor: 'rgba(140,150,170,0.18)', fill: '+1',
           pointRadius: 0, tension: 0.25 }},
        {{ label: '5Y Low', data: bandLo, borderColor: 'rgba(140,150,170,0.45)', borderWidth: 1,
           borderDash: [4,4], fill: false, pointRadius: 0, tension: 0.25 }},
        {{ label: '5Y Avg', data: bandAvg, borderColor: 'rgba(180,190,210,0.55)', borderWidth: 1,
           borderDash: [2,3], fill: false, pointRadius: 0, tension: 0.3 }},
        {{ label: String(priorYear), data: priorLine, borderColor: '#fbbf24', borderWidth: 2,
           fill: false, pointRadius: 0, tension: 0.3 }},
        {{ label: String(curYear), data: curLine, borderColor: '#ffffff', borderWidth: 2.5,
           fill: false, pointRadius: 0, tension: 0.3 }},
      ],
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{
        legend: {{ labels: {{ color: '#cbd5e1', boxWidth: 8 }}, position: 'top', align: 'end' }},
        tooltip: {{ mode: 'index', intersect: false,
          callbacks: {{ label: ctx => ctx.dataset.label + ': ' + fmt(ctx.parsed.y) }} }},
      }},
      scales: {{
        x: {{ ticks: {{ font: {{size: 10}}, color: '#9aa0ac', autoSkip: false,
              callback: function(_, i){{ return (i % 4 === 0) ? MONTHS[Math.floor(i/52*12)] : ''; }} }},
              grid: {{ color: 'rgba(255,255,255,0.04)' }} }},
        y: {{ ticks: {{ font: {{size: 10}}, color: '#9aa0ac',
              callback: v => units === '$/gal' ? '$' + v.toFixed(4) : '$' + v.toFixed(2) }},
              grid: {{ color: 'rgba(255,255,255,0.06)' }} }},
      }},
    }},
  }});
}}

function renderPriceDaily(canvas, key) {{
  const ctx = canvas.getContext('2d');
  const hist = (SNAPSHOT.priceHistsDaily || {{}})[key] || {{}};
  const years = Object.keys(hist).map(Number).sort();
  if (!years.length) return;
  const curYear = years[years.length - 1];
  const priorYear = curYear - 1;
  const days = Array.from({{length: 365}}, (_, i) => i + 1);
  const priorLine = days.map(d => hist[priorYear]?.[d] ?? null);
  const curLine   = days.map(d => hist[curYear]?.[d] ?? null);
  const firstOfMonth = [1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335];
  const units = (SNAPSHOT.priceUnits || {{}})[key] || '';
  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: days,
      datasets: [
        {{ label: String(priorYear), data: priorLine, borderColor: '#fbbf24', borderWidth: 1.5,
           fill: false, pointRadius: 0, tension: 0, spanGaps: true }},
        {{ label: String(curYear), data: curLine, borderColor: '#ffffff', borderWidth: 2,
           fill: false, pointRadius: 0, tension: 0, spanGaps: true }},
      ],
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{
        legend: {{ labels: {{ color: '#cbd5e1', boxWidth: 8 }}, position: 'top', align: 'end' }},
        tooltip: {{ mode: 'index', intersect: false,
          callbacks: {{ label: ctx => ctx.dataset.label + ': ' + (units === '$/gal' ? '$' + ctx.parsed.y.toFixed(4) : '$' + ctx.parsed.y.toFixed(2)) }} }},
      }},
      scales: {{
        x: {{ ticks: {{ font: {{size: 10}}, color: '#9aa0ac', autoSkip: false, maxRotation: 0,
              callback: function(val, i) {{
                const doy = i + 1;
                const idx = firstOfMonth.indexOf(doy);
                return idx >= 0 ? MONTHS[idx] : '';
              }} }},
              grid: {{ color: 'rgba(255,255,255,0.04)' }} }},
        y: {{ ticks: {{ font: {{size: 10}}, color: '#9aa0ac',
              callback: v => units === '$/gal' ? '$' + v.toFixed(4) : '$' + v.toFixed(2) }},
              grid: {{ color: 'rgba(255,255,255,0.06)' }} }},
      }},
    }},
  }});
}}

function renderAllPriceCharts() {{
  document.querySelectorAll('canvas[data-price]').forEach(c => {{
    const existing = Chart.getChart(c);
    if (existing) existing.destroy();
    if (_currentRes === 'weekly') renderPriceWeekly(c, c.dataset.price);
    else renderPriceDaily(c, c.dataset.price);
  }});
}}
renderAllPriceCharts();
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Forward curves page builder
# ─────────────────────────────────────────────────────────────────────────────

CURVES_PROMPT = """You are a senior crude/products analyst writing this week's read on
the forward curve structure. Reference contango vs backwardation, what the M1–M12
spread implies (steeper backwardation = tighter prompt, market pricing supply tightness
near-term; contango = surplus/storage play), gasoline/distillate curve shape into and
out of summer, and cross-commodity comparisons (WTI vs Brent forward differential
implies US export economics).

{freshness_instruction}

IMPORTANT — DATA CADENCE & FRAMING:
- ALL PRICES are prior-session NYMEX settlements as of {prices_date}.
- Use phrasing like "WTI settled at $X" or "the prior session front month closed at $X".
- NEVER describe these as "current", "intraday", or "today's" prices.
- "wti_front" and similar fields are the explicit prior-day NYMEX settlement for the
  front-month contract — not spot, not intraday, not today's open.

BRENT–WTI SPREAD MECHANICS — GET THIS RIGHT:
- Brent–WTI spread = Brent price minus WTI price (positive in normal markets).
- WIDE spread (e.g., $7+): WTI trades at a deeper discount to Brent → strong US
  export arb → US producers ship more barrels overseas, US export volumes rise.
- NARROW spread (e.g., $4 or less): WTI priced close to Brent → weak/uneconomic
  export arb → US export volumes fall, barrels stay domestic, US crude builds.
- DIRECTIONAL: a narrowing spread typically means WTI is RISING relative to
  Brent (often driven by Cushing draws or domestic tightness). It does NOT mean
  WTI needs to rise FURTHER to fix export economics — that is backwards.
- To RE-WIDEN the spread and restore export competitiveness, WTI would need to
  FALL (or Brent would need to rise). Excess domestic crude from weak exports
  pressures WTI DOWN, not up.
- ARITHMETIC SANITY: Brent − WTI. If WTI rises (Brent constant), the spread
  NARROWS. If WTI falls (Brent constant), the spread WIDENS. Re-check every
  directional claim against this identity before writing it.
- DO NOT WRITE: "narrow spread implies WTI upside to recapture barrels" or any
  variant claiming WTI must rise to fix export economics — economically wrong.
- DO NOT WRITE: "narrow Brent–WTI spread keeps US exports competitive" — it's
  the opposite. Narrow spread = exports LESS competitive.

INVENTORY REFERENCES — STAY GENERIC:
- This prompt does NOT include detailed EIA inventory figures (those live on
  the Inventories page). DO NOT cite specific draw/build numbers, specific
  inventory levels, PADD breakdowns, or refinery utilization percentages —
  you do not have those numbers in this data block and inventing them is a
  hallucination. If you reference inventory dynamics, keep it qualitative:
  "the latest WPSR", "the reported draw", "tight Cushing balances" — never
  with a specific number unless that number is explicitly in the data block.

LOGICAL-CONSISTENCY CHECK:
- Every cause-and-effect chain you write must be economically valid. After
  drafting, re-read each claim of the form "X happened, therefore Y" and check
  the mechanism actually flows that way. If you can't articulate the mechanism
  in one short sentence, the claim is probably wrong — rewrite or drop it.

Return STRICT JSON, no markdown wrappers:

{{
  "market_read": "4 sections separated by \\\\n. Each section covers one market: <strong>WTI Curve:</strong>, <strong>Brent Curve:</strong>, <strong>RBOB Gasoline Curve:</strong>, <strong>ULSD Diesel Curve:</strong>. Within each section: (1) state the current front-month settlement print; (2) explain the driver of the move in 1–2 sentences — every cause-and-effect claim must be economically valid; (3) describe the curve shape (backwardation vs contango, steepness, what M1–M12 spread implies about prompt tightness or surplus); (4) note the key calendar spread (M1–M2 or M1–M12) and what it signals; (5) end with <strong>Watch:</strong> followed by 1 sentence on what to monitor going forward. Keep each section to 4–5 sentences total. DO NOT use Headline, Curve Shape, Calendar Spreads, or Watch next week as section headers.",
  "kpi_wti": "1 sentence on WTI front-month settlement context",
  "kpi_brent": "1 sentence on Brent front-month settlement context",
  "kpi_rbob": "1 sentence on RBOB front-month settlement context (gasoline summer pull)",
  "kpi_ulsd": "1 sentence on ULSD front-month settlement context (distillate / heating dynamics)",
  "kpi_bw": "1 sentence on Brent–WTI spread — US export economics implication",
  "kpi_curve_regime": "1 sentence on curve regime (steepness of backwardation/contango, what it implies)"
}}

DATA (prior-session NYMEX settlements as of {prices_date}):
{data}

{wiki_context_block}
"""


def _generate_curves_narratives(ctx):
    """Forward-curve narratives with sticky fallback — see
    `_generate_morning_brief` for the rationale. No NYMEX trading on
    holidays/weekends means the curve inputs don't move; we reuse the
    cached commentary rather than dropping back to a thin template."""
    import hashlib
    cache_path = os.path.join(HERE, '.curves_cache.json')
    _wiki_raw_c = _load_wiki_context()
    _wiki_hash_c = hashlib.md5(_wiki_raw_c.encode()).hexdigest()[:8] if _wiki_raw_c else 'none'
    key = hashlib.md5((json.dumps(ctx, sort_keys=True) + _wiki_hash_c).encode()).hexdigest()
    prices_date = ctx.get('prices_date') or 'prior session'
    fresh_state = ctx.get('freshness_state', 'synced')

    # Post-EIA window — suppress commentary; banner carries the message.
    if fresh_state == 'post_eia':
        print('  → curves narratives: suppressed (post_eia — banner carries the message)')
        # Invalidate any pre-release last_good — see margins generator.
        try:
            with open(cache_path) as f:
                existing = json.load(f)
            for k in ('last_good', 'last_good_prices_date',
                      'last_good_freshness_state', 'last_good_generated_at',
                      'key', 'value'):
                existing.pop(k, None)
            with open(cache_path, 'w') as f:
                json.dump(existing, f)
        except Exception:
            pass
        return {
            'market_read': '',
            'kpi_wti': '', 'kpi_brent': '', 'kpi_rbob': '', 'kpi_ulsd': '',
            'kpi_bw': '', 'kpi_curve_regime': '',
        }

    cache = {}
    try:
        with open(cache_path) as f:
            cache = json.load(f)
    except Exception:
        cache = {}
    # 1. Exact-match cache hit
    if cache.get('key') == key and cache.get('value'):
        print('  → curves narratives: from cache')
        return cache['value']
    # 2. Data unchanged since last good run — reuse without an API call.
    #    Must also match freshness_state AND prompt_v — see margins generator.
    last_good = cache.get('last_good')
    if (last_good
            and cache.get('last_good_prices_date') == prices_date
            and cache.get('last_good_freshness_state', 'synced') == fresh_state
            and cache.get('last_good_prompt_v') == ctx.get('prompt_v')
            and cache.get('last_good_wiki_hash') == _wiki_hash_c):
        print(f'  → curves narratives: data unchanged ({prices_date}, {fresh_state}) — reusing last good')
        try:
            cache['key'] = key
            cache['value'] = last_good
            with open(cache_path, 'w') as f:
                json.dump(cache, f)
        except Exception:
            pass
        return last_good
    if ANTHROPIC_API_KEY:
        try:
            print('  → calling Anthropic API (Sonnet) for curves narratives...')
            _wiki_c = _load_wiki_context()
            wiki_context_block = (
                "INSTITUTIONAL MARKET CONTEXT (multi-week knowledge base — use to "
                "add depth and continuity; do not contradict current session data):\n" + _wiki_c
                if _wiki_c else ''
            )
            prompt = CURVES_PROMPT.format(
                prices_date=ctx.get('prices_date', 'prior session'),
                data=json.dumps(ctx, indent=2),
                wiki_context_block=wiki_context_block,
                freshness_instruction=_freshness_instruction(
                    ctx.get('freshness_state', 'synced'),
                    ctx.get('freshness_inv_through', ctx.get('prices_date', '')),
                    ctx.get('freshness_eia_release_str', ''),
                ),
            )
            # Upgraded from Haiku to Sonnet 2026-05-29: Haiku flipped the
            # direction of the Brent-WTI spread mechanics ("WTI rallies to
            # restore the arb" — wrong direction). Sonnet handles the
            # directional reasoning correctly, matching margins/morning brief.
            raw = _call_claude(prompt, max_tokens=2500)
            obj = _extract_first_json_object(raw)
            if obj:
                parsed = json.loads(obj)
                if 'market_read' in parsed:
                    parsed['market_read'] = _strip_cross_refs(parsed['market_read'])
                    print('  → curves narratives received')
                    try:
                        with open(cache_path, 'w') as f:
                            json.dump({
                                'key': key,
                                'value': parsed,
                                'last_good': parsed,
                                'last_good_prices_date': prices_date,
                                'last_good_freshness_state': fresh_state,
                                'last_good_prompt_v': ctx.get('prompt_v'),
                                'last_good_wiki_hash': _wiki_hash_c,
                                'last_good_generated_at': _utcnow().isoformat() + 'Z',
                            }, f)
                    except Exception:
                        pass
                    return parsed
        except Exception as e:
            print(f'  → curves API failed ({e}), using sticky fallback')
    # 3. API failed — prefer the last good narratives over the thin template
    if last_good:
        print(
            f'  → curves narratives: reusing last good '
            f'(prices through {cache.get("last_good_prices_date")})'
        )
        return last_good
    # 4. Cold start — thin template as last resort
    regime = 'backwardation' if ctx['wti_m1_m12'] > 0 else 'contango'
    return {
        'market_read': (
            f'<strong>Headline:</strong> WTI front ${ctx["wti_front"]:.2f}/bbl, curve in {regime}.\\n'
            f'<strong>Curve Shape:</strong> WTI M1-M12 differential ${ctx["wti_m1_m12"]:.2f}/bbl.\\n'
            f'<strong>Calendar Spreads:</strong> Brent ${ctx["brent_m1_m12"]:.2f}, RBOB ${ctx["rbob_m1_m12"]:.2f}.\\n'
            f'<strong>Watch next week:</strong> Calendar roll dynamics, prompt-month tightness.'
        ),
        'kpi_wti': f'WTI front ${ctx["wti_front"]:.2f}.',
        'kpi_brent': f'Brent front ${ctx["brent_front"]:.2f}.',
        'kpi_rbob': f'RBOB front ${ctx["rbob_front"]:.4f}/gal.',
        'kpi_ulsd': f'ULSD front ${ctx["ulsd_front"]:.4f}/gal.',
        'kpi_bw': f'Brent–WTI spread ${ctx["bw_spread"]:.2f}/bbl.',
        'kpi_curve_regime': f'WTI in {regime}, M1-M12 ${ctx["wti_m1_m12"]:.2f}/bbl.',
    }


def _build_curves_page(prices, latest_date):
    futures = prices.get('futures', {})
    spot = prices.get('eia_spot', {})

    # Forward curves shown in native trading units: WTI/Brent in $/bbl, RBOB/ULSD in $/gal.
    # (Crack-spread math elsewhere still converts to $/bbl internally — see compute_cracks.)
    UNITS_FOR = {'wti': '$/bbl', 'brent': '$/bbl', 'rbob': '$/gal', 'ulsd': '$/gal'}
    def conv(key, v):
        # No conversion — pass through whatever yfinance returned.
        return v

    def front_now(key):
        h = futures.get(key, {}).get('front_history', [])
        return conv(key, h[-1][1]) if h else None

    def front_prev_week(key):
        h = futures.get(key, {}).get('front_history', [])
        return conv(key, h[-6][1]) if len(h) >= 6 else None

    def curve_pts(key):
        """Curve points with prices + historical snapshots, all converted to $/bbl."""
        raw = futures.get(key, {}).get('curve', [])
        out = []
        for p in raw:
            out.append({
                'contract': p['contract'],
                'price':    conv(key, p.get('price')),
                'price_1d': conv(key, p.get('price_1d')) if p.get('price_1d') is not None else None,
                'price_1w': conv(key, p.get('price_1w')) if p.get('price_1w') is not None else None,
                'price_1m': conv(key, p.get('price_1m')) if p.get('price_1m') is not None else None,
                'price_1y': conv(key, p.get('price_1y')) if p.get('price_1y') is not None else None,
            })
        return out

    def m1_m12(key):
        c = curve_pts(key)
        if len(c) >= 12:
            return c[0]['price'] - c[11]['price']
        elif len(c) >= 2:
            return c[0]['price'] - c[-1]['price']
        return 0

    def m1_m12_prev(key):
        """Prior-day M1-M12 spread (uses each contract's price_1d)."""
        c = curve_pts(key)
        idx = 11 if len(c) >= 12 else (len(c) - 1 if len(c) >= 2 else None)
        if idx is None: return None
        a, b = c[0].get('price_1d'), c[idx].get('price_1d')
        if a is None or b is None: return None
        return a - b

    def m1_m2(key):
        c = curve_pts(key)
        if len(c) >= 2:
            return c[0]['price'] - c[1]['price']
        return 0

    def m1_m2_prev(key):
        c = curve_pts(key)
        if len(c) < 2: return None
        a, b = c[0].get('price_1d'), c[1].get('price_1d')
        if a is None or b is None: return None
        return a - b

    def weekly_front(key):
        """Daily front history → weekly, prices converted."""
        h = futures.get(key, {}).get('front_history', [])
        return [[d, conv(key, v)] for d, v in _weekly_from_daily(h)]

    def kpi_for_front(key, label, color_id, units, decimals=2):
        last = front_now(key); prev = front_prev_week(key)
        if last is None: return None
        weekly = weekly_front(key)
        spark = [round(v, decimals) for _, v in weekly[-12:]]
        yoy = [round(v, decimals) for _, v in weekly[-64:-52]] if len(weekly) >= 64 else []
        # 5-yr band at the latest week index
        latest_dt = datetime.strptime(weekly[-1][0], '%Y-%m-%d') if weekly else latest_date
        latest_week = latest_dt.isocalendar()[1]
        cur_year = latest_dt.year
        band_vals = []
        for d, v in weekly:
            dt2 = datetime.strptime(d, '%Y-%m-%d')
            if dt2.isocalendar()[1] == latest_week and cur_year - 5 <= dt2.year < cur_year:
                band_vals.append(v)
        band_lo = round(min(band_vals), decimals) if band_vals else last
        band_hi = round(max(band_vals), decimals) if band_vals else last
        return {
            'id': color_id, 'label': label,
            'last': round(last, decimals), 'change': round(last - (prev or last), decimals),
            'units': units, 'spark': spark, 'yoy': yoy,
            'band_lo': band_lo, 'band_hi': band_hi,
        }

    def kpi_for_bw_spread():
        """Brent–WTI spread from EIA daily spot (so we have rich historical sparkline)."""
        wti_data = spot.get('wti_spot', {}).get('data', [])
        brent_data = spot.get('brent_spot', {}).get('data', [])
        if not wti_data or not brent_data: return None
        wti = dict(wti_data); brent = dict(brent_data)
        common = sorted(set(wti) & set(brent))
        spread = [[d, brent[d] - wti[d]] for d in common]
        last = spread[-1][1]; prev = spread[-6][1] if len(spread) >= 6 else last
        weekly = _weekly_from_daily(spread)
        spark = [round(v, 2) for _, v in weekly[-12:]]
        yoy = [round(v, 2) for _, v in weekly[-64:-52]] if len(weekly) >= 64 else []
        latest_dt = datetime.strptime(weekly[-1][0], '%Y-%m-%d')
        latest_week = latest_dt.isocalendar()[1]
        cur_year = latest_dt.year
        band_vals = []
        for d, v in weekly:
            dt2 = datetime.strptime(d, '%Y-%m-%d')
            if dt2.isocalendar()[1] == latest_week and cur_year - 5 <= dt2.year < cur_year:
                band_vals.append(v)
        return {
            'id': 'bwspread', 'label': 'Brent–WTI Spread',
            'last': round(last, 2), 'change': round(last - prev, 2),
            'units': '$/bbl', 'spark': spark, 'yoy': yoy,
            'band_lo': round(min(band_vals), 2) if band_vals else last,
            'band_hi': round(max(band_vals), 2) if band_vals else last,
        }

    def kpi_for_regime():
        """Curve regime card — show WTI M1–M12 spread as the 'value', no sparkline."""
        spread = m1_m12('wti')
        # Compare to recent: use M1-M12 of Brent and RBOB as crude bounds
        return {
            'id': 'regime', 'label': 'WTI M1–M12',
            'last': round(spread, 2), 'change': 0,
            'units': '$/bbl', 'spark': [], 'yoy': [],
            'band_lo': round(spread - 5, 2), 'band_hi': round(spread + 5, 2),
        }

    def kpi_for_spread(label, color_id, value, prev_value=None, units='$/bbl', decimals=2):
        """Spread card — day-over-day change, no sparkline, no band (minimal style)."""
        chg = (value - prev_value) if prev_value is not None else 0
        return {
            'id': color_id, 'label': label,
            'last': round(value, decimals), 'change': round(chg, decimals),
            'units': units, 'frequency': 'daily',
            'spark': [], 'yoy': [],
            'band_lo': round(value, decimals), 'band_hi': round(value, decimals),
        }

    # Curves KPI strip: just the calendar-spread cards (day-over-day deltas)
    curve_kpis = [
        kpi_for_spread('WTI M1–M2',   'wti_m12s', m1_m2('wti'),   m1_m2_prev('wti')),
        kpi_for_spread('Brent M1–M2', 'brt_m12s', m1_m2('brent'), m1_m2_prev('brent')),
        kpi_for_spread('RBOB M1–M2',  'rb_m12s',  m1_m2('rbob'),  m1_m2_prev('rbob'),  units='$/gal', decimals=4),
        kpi_for_spread('ULSD M1–M2',  'ho_m12s',  m1_m2('ulsd'),  m1_m2_prev('ulsd'),  units='$/gal', decimals=4),
    ]
    curve_kpis += [
        kpi_for_spread('WTI M1–M12',   'wti_m112', m1_m12('wti'),   m1_m12_prev('wti')),
        kpi_for_spread('Brent M1–M12', 'brt_m112', m1_m12('brent'), m1_m12_prev('brent')),
        kpi_for_spread('RBOB M1–M12',  'rb_m112',  m1_m12('rbob'),  m1_m12_prev('rbob'),  units='$/gal', decimals=4),
        kpi_for_spread('ULSD M1–M12',  'ho_m112',  m1_m12('ulsd'),  m1_m12_prev('ulsd'),  units='$/gal', decimals=4),
    ]
    curve_kpis = [k for k in curve_kpis if k is not None]

    wti_curve = curve_pts('wti')
    brent_curve = curve_pts('brent')
    rbob_curve = curve_pts('rbob')
    ulsd_curve = curve_pts('ulsd')

    # Resolve prices_as_of date for prompt framing
    _prices_as_of_curves = prices.get('prices_as_of', '')
    try:
        _prices_date_curves = datetime.strptime(_prices_as_of_curves, '%Y-%m-%d').strftime('%B %-d, %Y') if _prices_as_of_curves else latest_date.strftime('%B %-d, %Y')
    except Exception:
        _prices_date_curves = _prices_as_of_curves or latest_date.strftime('%B %-d, %Y')

    # Freshness state — compute BEFORE narrative generation so the prompt can
    # frame commentary correctly. See _compute_freshness() docstring.
    _freshness = _compute_freshness(_prices_as_of_curves, latest_date)
    inv_latest_date = _freshness['inv_through']
    eia_banner_html = _freshness['banner_html']

    # In post_eia we suppress the AI Brief entirely — the banner carries the
    # message. The section is hidden completely (header + body).
    if _freshness['state'] == 'post_eia':
        ai_curve_brief_section_html = ''
    else:
        # Mirrors the home-page "Today's Read" hero — see AI_BRIEF_BOX_CSS.
        ai_curve_brief_section_html = (
            '<section class="ai-brief-box">'
            '<div class="ai-brief-box-head">'
            '<div class="ai-brief-box-left">'
            '<span class="ai-brief-box-eyebrow">AI Curve Brief</span>'
            '<span class="ai-brief-box-pill">AI Generated</span>'
            '</div>'
            '</div>'
            '<div class="market-read" id="market-read"></div>'
            '</section>'
        )

    nctx = {
        'prices_date': _prices_date_curves,          # prior NYMEX settlement date for AI framing
        'data_note': 'All prices are prior-session NYMEX settlements. Use "settled at" or "prior session close" — never "current" or "spot".',
        # Two-phase refresh design — lands in cache key + powers freshness_instruction.
        'freshness_state':           _freshness['state'],
        'freshness_inv_through':     _freshness['inv_through'],
        'freshness_eia_release_str': _freshness['eia_release_str'],
        # Bump when prompt wording changes materially — forces regen.
        'prompt_v':                  '2026-05-29-bw-sonnet-no-inv',
        'wti_front': front_now('wti') or 0,
        'brent_front': front_now('brent') or 0,
        'rbob_front': front_now('rbob') or 0,
        'ulsd_front': front_now('ulsd') or 0,
        'wti_m1_m12': round(m1_m12('wti'), 2),
        'brent_m1_m12': round(m1_m12('brent'), 2),
        'rbob_m1_m12': round(m1_m12('rbob'), 4),
        'ulsd_m1_m12': round(m1_m12('ulsd'), 4),
        'bw_spread': round((front_now('brent') or 0) - (front_now('wti') or 0), 2),
        'wti_curve': [p['price'] for p in wti_curve],
        'wti_curve_contracts': [p['contract'] for p in wti_curve],
        'brent_curve': [p['price'] for p in brent_curve],
        'rbob_curve': [p['price'] for p in rbob_curve],
        'ulsd_curve': [p['price'] for p in ulsd_curve],
    }
    narratives = _generate_curves_narratives(nctx)

    refreshed = _utcnow().strftime('%b %-d, %Y')
    # Live ET time + date for the AI Curve Brief refresh stamp.
    refreshed_time, refreshed_date = _refreshed_stamp_et()
    report_date_str = latest_date.strftime('%B %-d, %Y')
    # Latest curve data date — use prices_as_of (prior settlement day) when present
    curves_latest_date = report_date_str
    trade_through_dt = latest_date
    if prices.get('prices_as_of'):
        try:
            trade_through_dt = datetime.strptime(prices['prices_as_of'], '%Y-%m-%d')
            curves_latest_date = trade_through_dt.strftime('%B %-d, %Y')
        except Exception:
            curves_latest_date = prices['prices_as_of']
    else:
        for k in ('wti', 'brent', 'rbob', 'ulsd'):
            fh = (futures.get(k, {}) or {}).get('front_history', [])
            if fh:
                try:
                    trade_through_dt = datetime.strptime(fh[-1][0], '%Y-%m-%d')
                    curves_latest_date = trade_through_dt.strftime('%B %-d, %Y')
                    break
                except Exception:
                    pass

    # Next refresh = day after the next NYMEX trade date following the trade-through date
    try:
        next_refresh_str = _next_refresh_date(trade_through_dt).strftime('%B %-d, %Y')
    except Exception:
        next_refresh_str = ''

    # Normalize: real newlines + bold paragraph leads.
    curves_market_read = _normalize_narrative(narratives.get('market_read', ''))
    snapshot_js = json.dumps({
        'kpi': curve_kpis,
        'marketRead': curves_market_read,
        'wtiCurve':   wti_curve,
        'brentCurve': brent_curve,
        'rbobCurve':  rbob_curve,
        'ulsdCurve':  ulsd_curve,
    })

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="description" content="WTI and Brent forward curves, calendar spreads, and crude oil term structure updated daily from NYMEX settlement.">
<meta name="theme-color" content="#07090d">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self' https://mob-chat.brad-95b.workers.dev;">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
{_FAVICON}
{_og_tags('Forward Curves · MOB', 'WTI and Brent forward curves, calendar spreads, and crude oil term structure updated daily from NYMEX settlement.', 'curves')}
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Petroleum Intelligence — Forward Curves</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.js" integrity="sha384-iU8HYtnGQ8Cy4zl7gbNMOhsDTTKX02BTXptVP/vqAWIaTfM7isw76iyZCsjL2eVi" crossorigin="anonymous"></script>
<style>
:root {{
  color-scheme: dark;
  --bg: #0f1117; --panel: #1a1d24; --panel-2: #232730;
  --border: #25272e; --border-soft: #1c1e24;
  --text: #e4e7ec; --muted: #9aa0ac; --muted-2: #c1c5cf;
  --build: #16a34a; --draw: #dc2626; --accent: #f59e0b;
}}
* {{ box-sizing: border-box; }}
html, body {{ background: var(--bg); margin: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  color: var(--text); font-size: 15px; line-height: 1.5; }}
.container {{ max-width: 1500px; margin: 0 auto; padding: 14px; }}
.header {{ display: flex; justify-content: space-between; align-items: flex-start;
  flex-wrap: wrap; gap: 8px; margin-bottom: 18px; padding-bottom: 12px; border-bottom: 1px solid var(--border); }}
.header h1 {{ margin: 0; font-size: 22px; font-weight: 600; letter-spacing: -0.3px; }}
.header .subtitle {{ font-size: 12px; color: var(--muted); margin-top: 3px; }}
.update-info {{ text-align: right; font-size: 11px; color: var(--muted); line-height: 1.6; }}
.update-info strong {{ color: var(--text); font-weight: 500; }}
.update-info .pill {{ display: inline-block; padding: 2px 9px; background: rgba(22,163,74,0.15);
  color: #4ade80; border-radius: 10px; font-weight: 500; font-size: 11px; letter-spacing: 0.4px;
  text-transform: uppercase; margin-bottom: 4px; border: 1px solid rgba(22,163,74,0.35); }}
.kpi-strip {{ display: grid; grid-template-columns: repeat(4, 1fr) !important; gap: 8px; margin-bottom: 18px; max-width: 1300px; margin-left: auto !important; margin-right: auto !important; }}
@media (max-width: 1100px) {{ .kpi-strip {{ grid-template-columns: repeat(2, 1fr) !important; }} }}
@media (max-width: 600px)  {{ .kpi-strip {{ grid-template-columns: 1fr !important; }} }}
.kpi-row-label {{
  font-size: 13px; font-weight: 700; color: var(--muted-2);
  letter-spacing: -0.1px;
  padding: 12px 2px 6px; border-bottom: 1px solid var(--border-soft);
  margin-bottom: 8px;
}}
.kpi-row-label + .kpi-strip {{ margin-bottom: 8px; }}
/* Pair M1-M2 + M1-M12 strips side-by-side on one row */
.calspread-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-bottom: 18px; }}
.calspread-row .kpi-row-label {{
  padding-top: 8px;
  font-size: 12px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
.calspread-row .kpi-row-label > span {{ font-size: 11px; }}
.calspread-row .kpi-strip {{ max-width: none !important; margin: 0 !important; grid-template-columns: repeat(4, 1fr) !important; }}
@media (max-width: 1100px) {{
  .calspread-row {{ grid-template-columns: 1fr; gap: 0; }}
  .calspread-row .kpi-strip {{ grid-template-columns: repeat(2, 1fr) !important; margin-bottom: 8px !important; }}
}}
@media (max-width: 600px) {{
  .calspread-row .kpi-strip {{ grid-template-columns: 1fr !important; }}
}}
.section {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
  padding: 16px 18px; margin-bottom: 14px; }}
.section-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
.section-title {{ font-size: 14px; font-weight: 600; margin: 0; letter-spacing: -0.1px; }}
.section-subtitle {{ font-size: 11px; color: var(--muted); font-weight: 400; margin-left: 8px; text-transform: none; letter-spacing: 0; }}
.badge {{ display: inline-block; font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 999px;
  vertical-align: 2px; letter-spacing: 0.7px; text-transform: uppercase; margin-left: 6px; }}
.badge-ai {{ background: linear-gradient(90deg, rgba(167,139,250,0.18), rgba(96,165,250,0.18));
  color: #c4b5fd; border: 1px solid rgba(167,139,250,0.4); }}
.market-read {{ font-size: 13.5px; line-height: 1.7; color: var(--muted-2); }}
.market-read strong {{ color: var(--text); }}
.market-read p {{ margin: 0 0 10px; }}
.curve-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
@media (max-width: 1000px) {{ .curve-grid {{ grid-template-columns: 1fr; }} }}
.chart-container {{ height: 320px; position: relative; }}
.chart-container.tall {{ height: 420px; }}
/* Minimal-strip styling — used for spread cards (no spark/position/band) */
/* ─── Redesigned minimal KPI cards (M1-M2 / M1-M12 spread cards) ───
   Uniform layout: label on top, HIGHER/LOWER chip beneath, large value,
   then change. Everything nowrap, left-aligned, tabular nums for clean
   vertical alignment across all 8 cards. */
.kpi-strip-minimal .kpi-position,
.kpi-strip-minimal .kpi-spark,
.kpi-strip-minimal .kpi-band,
.kpi-strip-minimal .kpi-change-sub {{ display: none !important; }}
.kpi-strip-minimal .kpi {{
  padding: 12px 14px !important;
  gap: 6px !important;
  min-height: 124px;
  display: flex !important;
  flex-direction: column !important;
  align-items: stretch !important;
  justify-content: flex-start !important;
}}
.kpi-strip-minimal .kpi-row {{
  display: flex !important;
  flex-direction: column !important;
  align-items: flex-start !important;
  gap: 5px !important;
}}
.kpi-strip-minimal .kpi-label {{
  font-size: 13px !important;
  font-weight: 700 !important;
  white-space: nowrap !important;
  letter-spacing: 0.2px !important;
  line-height: 1.2 !important;
  color: var(--text) !important;
}}
.kpi-strip-minimal .kpi-tag {{
  font-size: 9px !important;
  padding: 2px 7px !important;
  letter-spacing: 0.5px !important;
  white-space: nowrap !important;
  align-self: flex-start !important;
}}
.kpi-strip-minimal .kpi-value {{
  font-size: 22px !important;
  font-weight: 700 !important;
  white-space: nowrap !important;
  font-variant-numeric: tabular-nums !important;
  line-height: 1.15 !important;
  margin-top: auto !important;
  display: flex !important;
  align-items: baseline !important;
  gap: 3px !important;
}}
.kpi-strip-minimal .kpi-value-units {{
  font-size: 10px !important;
  font-weight: 500 !important;
  color: var(--muted) !important;
}}
.kpi-strip-minimal .kpi-change-row {{
  font-size: 12px !important;
  gap: 6px !important;
  white-space: nowrap !important;
  display: flex !important;
  align-items: center !important;
}}
.kpi-strip-minimal .kpi-change {{
  font-size: 13px !important;
  font-weight: 700 !important;
  white-space: nowrap !important;
}}
.curve-table {{ width: 100%; margin-top: 12px; border-collapse: collapse; font-size: 11.5px; font-variant-numeric: tabular-nums; }}
.curve-table th, .curve-table td {{ padding: 5px 10px; border-bottom: 1px solid var(--border-soft); text-align: right; }}
.curve-table th:first-child, .curve-table td:first-child {{ text-align: left; }}
.curve-table th {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); font-weight: 600; }}
.curve-table td {{ color: var(--text); }}
.curve-table td.pos {{ color: #4ade80; font-weight: 500; }}
.curve-table td.neg {{ color: #f87171; font-weight: 500; }}
{NAV_CSS}
{_KPI_CSS_BODY}
{AI_BRIEF_BOX_CSS}
footer {{ margin-top: 24px; padding-top: 12px; border-top: 1px solid var(--border);
  font-size: 11px; color: var(--muted); text-align: center; }}
footer a {{ color: var(--accent); text-decoration: none; }}
</style>
</head>
<body>
{_render_nav('curves.html')}

<div class="container">
  <div class="header">
    <div>
      <h1>Forward Curves</h1>
      <div class="subtitle">12-month strips for WTI, Brent, RBOB, ULSD · contango / backwardation</div>
    </div>
    <div class="update-info">
      <span class="pill">NYMEX SETTLEMENT</span><br>
      Prices thru: <strong>{curves_latest_date}</strong><br>
      Inventories thru: <strong>{inv_latest_date}</strong><br>
      Next refresh: <strong>{next_refresh_str}</strong> · <strong>~5:00 AM ET</strong>
    </div>
  </div>

  {eia_banner_html}

  <div class="calspread-row">
    <div>
      <div class="kpi-row-label">M1–M2 Calendar Spreads <span style="font-weight:400;text-transform:none;color:var(--muted)"> · prompt-month tightness</span></div>
      <div class="kpi-strip kpi-strip-minimal" id="kpi-strip-m1m2"></div>
    </div>
    <div>
      <div class="kpi-row-label">M1–M12 Calendar Spreads <span style="font-weight:400;text-transform:none;color:var(--muted)"> · overall curve regime</span></div>
      <div class="kpi-strip kpi-strip-minimal" id="kpi-strip-m1m12"></div>
    </div>
  </div>

  {ai_curve_brief_section_html}

  <div class="curve-grid">
    <div class="section">
      <div class="section-header"><h2 class="section-title">WTI Curve <span class="section-subtitle">$/bbl absolute</span></h2></div>
      <div class="chart-container"><canvas id="wti-curve" role="img" aria-label="WTI crude oil forward curve chart"></canvas></div>
      <table class="curve-table" id="wti-table"></table>
    </div>
    <div class="section">
      <div class="section-header"><h2 class="section-title">Brent Curve <span class="section-subtitle">$/bbl absolute</span></h2></div>
      <div class="chart-container"><canvas id="brent-curve" role="img" aria-label="Brent crude oil forward curve chart"></canvas></div>
      <table class="curve-table" id="brent-table"></table>
    </div>
    <div class="section">
      <div class="section-header"><h2 class="section-title">RBOB Gasoline Curve <span class="section-subtitle">$/gal absolute</span></h2></div>
      <div class="chart-container"><canvas id="rbob-curve" role="img" aria-label="RBOB gasoline forward curve chart"></canvas></div>
      <table class="curve-table" id="rbob-table"></table>
    </div>
    <div class="section">
      <div class="section-header"><h2 class="section-title">ULSD Diesel Curve <span class="section-subtitle">$/gal absolute</span></h2></div>
      <div class="chart-container"><canvas id="ulsd-curve" role="img" aria-label="ULSD diesel forward curve chart"></canvas></div>
      <table class="curve-table" id="ulsd-table"></table>
    </div>
  </div>

  <footer>
    Data through {curves_latest_date}
  </footer>
</div>

<script>
{_SIGNOUT_JS}
const SNAPSHOT = {snapshot_js};
// Guard against absent element — in post_eia state the AI Brief section is
// omitted entirely, so this node won't exist.
(function() {{
  const el = document.getElementById('market-read');
  if (!el || !SNAPSHOT.marketRead) return;
  el.innerHTML = '<p>' + SNAPSHOT.marketRead.split('\\n').join('</p><p>') + '</p>';
}})();

// Split KPI strip: first 4 cards → M1-M2 strip, next 4 → M1-M12 strip
(function splitKpiStrip(){{
  const all = SNAPSHOT.kpi || [];
  const m1m2 = document.getElementById('kpi-strip-m1m2');
  const m1m12 = document.getElementById('kpi-strip-m1m12');
  // The KPI render JS below looks for `kpi-strip` id; we temporarily rename
  // the M1-M2 strip then run, append cards, then do the same for M1-M12.
  if (!m1m2 || !m1m12) return;
  const origGetById = document.getElementById.bind(document);
  // Render M1-M2 cards
  SNAPSHOT._kpiBackup = all;
  SNAPSHOT.kpi = all.slice(0, 4);
  m1m2.id = 'kpi-strip';
  redesignKpis();
  m1m2.id = 'kpi-strip-m1m2';
  // Render M1-M12 cards
  SNAPSHOT.kpi = all.slice(4);
  m1m12.id = 'kpi-strip';
  redesignKpis();
  m1m12.id = 'kpi-strip-m1m12';
  SNAPSHOT.kpi = all;
}});

// Define redesignKpis from the KPI JS body, then call our splitter
{_KPI_JS_BODY}

// Now actually split the strips (replaces the default redesignKpis call)
(function(){{
  const m1m2 = document.getElementById('kpi-strip-m1m2');
  const m1m12 = document.getElementById('kpi-strip-m1m12');
  if (!m1m2 || !m1m12) return;
  // The default redesignKpis already ran on document.load and looked for
  // 'kpi-strip' which doesn't exist on this page, so it did nothing.
  // We render manually here.
  const all = SNAPSHOT.kpi || [];
  function renderInto(strip, cards){{
    const orig = SNAPSHOT.kpi;
    SNAPSHOT.kpi = cards;
    const origId = strip.id;
    strip.id = 'kpi-strip';
    try {{ redesignKpis(); }} catch(e) {{}}
    strip.id = origId;
    SNAPSHOT.kpi = orig;
  }}
  renderInto(m1m2, all.slice(0, 4));
  renderInto(m1m12, all.slice(4));
}})();

// Helper: build a multi-line curve chart with historical overlays
function buildCurveChart(canvasId, data, color, units) {{
  const ctx = document.getElementById(canvasId).getContext('2d');
  const labels = data.map(p => p.contract);
  const today   = data.map(p => p.price);
  const wkAgo   = data.map(p => p.price_1w);
  const moAgo   = data.map(p => p.price_1m);
  const yrAgo   = data.map(p => p.price_1y);
  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels,
      datasets: [
        {{ label: '1 yr ago',  data: yrAgo, borderColor: 'rgba(160,170,190,0.55)',
           borderWidth: 1.2, pointRadius: 0, fill: false, tension: 0.25, borderDash: [4,4], spanGaps: true }},
        {{ label: '1 mo ago',  data: moAgo, borderColor: 'rgba(96,165,250,0.65)',
           borderWidth: 1.5, pointRadius: 0, fill: false, tension: 0.25, borderDash: [2,3], spanGaps: true }},
        {{ label: '1 wk ago',  data: wkAgo, borderColor: 'rgba(167,139,250,0.85)',
           borderWidth: 1.7, pointRadius: 0, fill: false, tension: 0.25, spanGaps: true }},
        {{ label: 'Today',     data: today, borderColor: color, backgroundColor: color + '15',
           borderWidth: 2.5, pointRadius: 3, fill: true, tension: 0.25 }},
      ],
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{
        legend: {{ position: 'top', align: 'end',
                   labels: {{ color: '#cbd5e1', boxWidth: 10, font: {{size: 10}} }} }},
        tooltip: {{ mode: 'index', intersect: false,
                    callbacks: {{ label: c => c.dataset.label + ': ' + units + (c.parsed.y != null ? c.parsed.y.toFixed(2) : '—') }} }},
      }},
      scales: {{
        x: {{ ticks: {{ font: {{size: 10}}, color: '#9aa0ac' }}, grid: {{ color: 'rgba(255,255,255,0.04)' }} }},
        y: {{ ticks: {{ font: {{size: 10}}, color: '#9aa0ac', callback: v => units + v }},
              grid: {{ color: 'rgba(255,255,255,0.06)' }} }},
      }},
    }},
  }});
}}

buildCurveChart('wti-curve',   SNAPSHOT.wtiCurve,   '#fbbf24', '$');
buildCurveChart('brent-curve', SNAPSHOT.brentCurve, '#60a5fa', '$');
buildCurveChart('rbob-curve',  SNAPSHOT.rbobCurve,  '#ef4444', '$');
buildCurveChart('ulsd-curve',  SNAPSHOT.ulsdCurve,  '#facc15', '$');

// All Four Curves overlay removed by request

// ─── Populate curve data tables (contract | price | Δ vs prior month) ───
function fillCurveTable(tableId, curve, units){{
  const t = document.getElementById(tableId);
  if (!t || !curve.length) return;
  const dec = (units === '$/gal') ? 4 : 2;
  const rows = ['<thead><tr><th>Contract</th><th>Price (' + units + ')</th><th>Δ vs prior</th></tr></thead><tbody>'];
  curve.forEach((pt, i) => {{
    const prev = i === 0 ? null : curve[i - 1].price;
    const diff = prev === null ? null : pt.price - prev;
    const diffStr = diff === null ? '—'
      : (diff >= 0 ? '+' : '') + '$' + diff.toFixed(dec);
    const diffCls = diff === null ? '' : (diff >= 0 ? 'pos' : 'neg');
    rows.push(
      '<tr>' +
      '<td>' + pt.contract + '</td>' +
      '<td>$' + pt.price.toFixed(dec) + '</td>' +
      '<td class="' + diffCls + '">' + diffStr + '</td>' +
      '</tr>'
    );
  }});
  rows.push('</tbody>');
  t.innerHTML = rows.join('');
}}
fillCurveTable('wti-table',   SNAPSHOT.wtiCurve,   '$/bbl');
fillCurveTable('brent-table', SNAPSHOT.brentCurve, '$/bbl');
fillCurveTable('rbob-table',  SNAPSHOT.rbobCurve,  '$/gal');
fillCurveTable('ulsd-table',  SNAPSHOT.ulsdCurve,  '$/gal');

// (All Four Curves overlay removed by request)
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# News page builder
# ─────────────────────────────────────────────────────────────────────────────

NEWS_CATEGORIES_ORDER = [
    'Crude Oil & OPEC',
    'Geopolitics & Risk',
    'Energy Markets',
    'EIA & Inventory',
    'Refined Products',
    'Refining',
    'US Production',
    'LNG & Gas',
]


# ─── Inline SVG glyphs for the news category pills.  Each one is a 13x13
# stroke-based icon using currentColor so it inherits the pill's text color.
NEWS_CATEGORY_ICONS = {
    'Top Stories': (
        '<svg viewBox="0 0 16 16" width="13" height="13" fill="currentColor" '
        'aria-hidden="true" style="flex:none">'
        '<polygon points="8,1.5 10.1,5.8 14.8,6.5 11.4,9.8 12.2,14.5 8,12.3 '
        '3.8,14.5 4.6,9.8 1.2,6.5 5.9,5.8"/></svg>'
    ),
    'Crude Oil & OPEC': (
        # Oil drop
        '<svg viewBox="0 0 16 16" width="13" height="13" fill="currentColor" '
        'aria-hidden="true" style="flex:none">'
        '<path d="M8 1.5C5.5 5 4 7.5 4 10a4 4 0 0 0 8 0c0-2.5-1.5-5-4-8.5z"/>'
        '</svg>'
    ),
    'Geopolitics & Risk': (
        # Globe
        '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" '
        'stroke="currentColor" stroke-width="1.4" stroke-linecap="round" '
        'stroke-linejoin="round" aria-hidden="true" style="flex:none">'
        '<circle cx="8" cy="8" r="6"/>'
        '<path d="M2 8h12"/>'
        '<ellipse cx="8" cy="8" rx="3" ry="6"/></svg>'
    ),
    'Energy Markets': (
        # Trending line chart
        '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" '
        'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" '
        'stroke-linejoin="round" aria-hidden="true" style="flex:none">'
        '<polyline points="2,12 5.5,8.5 8,10.5 13.5,4"/>'
        '<polyline points="10,4 13.5,4 13.5,7.5"/></svg>'
    ),
    'EIA & Inventory': (
        # Storage tank
        '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" '
        'stroke="currentColor" stroke-width="1.4" stroke-linecap="round" '
        'stroke-linejoin="round" aria-hidden="true" style="flex:none">'
        '<ellipse cx="8" cy="3.5" rx="5" ry="1.5"/>'
        '<path d="M3 3.5v9c0 .8 2.2 1.5 5 1.5s5-.7 5-1.5v-9"/>'
        '<path d="M3 7.5c0 .8 2.2 1.5 5 1.5s5-.7 5-1.5"/></svg>'
    ),
    'Refined Products': (
        # Distillation columns
        '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" '
        'stroke="currentColor" stroke-width="1.4" stroke-linecap="round" '
        'stroke-linejoin="round" aria-hidden="true" style="flex:none">'
        '<rect x="2" y="7" width="2.8" height="7"/>'
        '<rect x="6.6" y="3.5" width="2.8" height="10.5"/>'
        '<rect x="11.2" y="6" width="2.8" height="8"/>'
        '<line x1="3.4" y1="5.5" x2="3.4" y2="6.8"/>'
        '<line x1="8" y1="2" x2="8" y2="3.3"/>'
        '<line x1="12.6" y1="4.5" x2="12.6" y2="5.8"/></svg>'
    ),
    'Refining': (
        # Distillation columns (same icon)
        '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" '
        'stroke="currentColor" stroke-width="1.4" stroke-linecap="round" '
        'stroke-linejoin="round" aria-hidden="true" style="flex:none">'
        '<rect x="2" y="7" width="2.8" height="7"/>'
        '<rect x="6.6" y="3.5" width="2.8" height="10.5"/>'
        '<rect x="11.2" y="6" width="2.8" height="8"/>'
        '<line x1="3.4" y1="5.5" x2="3.4" y2="6.8"/>'
        '<line x1="8" y1="2" x2="8" y2="3.3"/>'
        '<line x1="12.6" y1="4.5" x2="12.6" y2="5.8"/></svg>'
    ),
    'US Production': (
        # Briefcase / rig silhouette
        '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" '
        'stroke="currentColor" stroke-width="1.4" stroke-linecap="round" '
        'stroke-linejoin="round" aria-hidden="true" style="flex:none">'
        '<rect x="2" y="5" width="12" height="9" rx="0.8"/>'
        '<path d="M6 5V3.5h4V5"/>'
        '<line x1="2" y1="9" x2="14" y2="9"/></svg>'
    ),
    'LNG & Gas': (
        # Leaf / flame
        '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" '
        'stroke="currentColor" stroke-width="1.4" stroke-linecap="round" '
        'stroke-linejoin="round" aria-hidden="true" style="flex:none">'
        '<path d="M14 2c0 6-4 11-12 12 1-8 6-12 12-12z"/>'
        '<path d="M3 13c2-2 5-4 8-6"/></svg>'
    ),
}


def _build_news_page(news_items, refreshed_str):
    from html import escape
    from collections import defaultdict

    def relative_time(iso_str):
        if not iso_str: return ''
        try:
            dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
            now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
            delta = now - dt
            mins = int(delta.total_seconds() / 60)
            if mins < 1: return 'just now'
            if mins < 60: return f'{mins}m ago'
            hrs = mins // 60
            if hrs < 24: return f'{hrs}h ago'
            days = hrs // 24
            if days == 1: return 'yesterday'
            return f'{days}d ago'
        except Exception:
            return ''

    def clean_title(title, source):
        if not title:
            return ''
        if source and title.endswith(' - ' + source):
            return title[:-len(' - ' + source)].strip()
        if ' - ' in title:
            head, _, tail = title.rpartition(' - ')
            if 2 <= len(tail) <= 50:
                return head.strip()
        return title.strip()

    def article_html(it):
        title = escape(clean_title(it.get('title', ''), it.get('source', '')))
        source = escape((it.get('source') or '').strip())
        link = escape(it.get('link', '#'))
        when = escape(relative_time(it.get('datetime')))
        desc = escape((it.get('description') or '').strip())
        desc_html = f'<div class="news-desc">{desc}</div>' if desc else ''
        time_html = f'<span class="news-time">{when}</span>' if when else ''
        return (
            f'<a class="news-item" href="{link}" target="_blank" rel="noopener">'
            f'<div class="news-title">{title}</div>'
            f'<div class="news-meta">{source}'
            + (f' · {time_html}' if when else '')
            + '</div>'
            + desc_html
            + '</a>'
        )

    # Group by category and sort within each by recency
    by_cat = defaultdict(list)
    for it in news_items:
        cat = it.get('category') or 'Markets & Prices'
        by_cat[cat].append(it)
    for cat in by_cat:
        by_cat[cat].sort(key=lambda x: x.get('epoch', 0), reverse=True)

    # Top stories — overall 6 most recent
    top_stories = sorted(news_items, key=lambda x: x.get('epoch', 0), reverse=True)[:6]

    sections_html = []
    if top_stories:
        items_html = '\n'.join(article_html(it) for it in top_stories)
        top_icon = NEWS_CATEGORY_ICONS.get('Top Stories', '')
        sections_html.append(
            f'<div class="news-section">'
            f'<h2 class="news-section-title">'
            f'<span class="news-cat-pill cat-top">{top_icon}<span class="cat-label">Top Stories</span></span>'
            f'<span class="news-count">{len(top_stories)}</span></h2>'
            f'<div class="news-grid">{items_html}</div>'
            f'</div>'
        )
    for cat in NEWS_CATEGORIES_ORDER:
        items = by_cat.get(cat, [])[:12]
        if not items:
            continue
        items_html = '\n'.join(article_html(it) for it in items)
        cls = 'cat-' + cat.lower().replace(' & ', '-').replace(' ', '-').replace('&', '').strip('-')
        icon = NEWS_CATEGORY_ICONS.get(cat, '')
        sections_html.append(
            f'<div class="news-section">'
            f'<h2 class="news-section-title">'
            f'<span class="news-cat-pill {cls}">{icon}<span class="cat-label">{escape(cat)}</span></span>'
            f'<span class="news-count">{len(items)}</span></h2>'
            f'<div class="news-grid">{items_html}</div>'
            f'</div>'
        )

    total = len(news_items)
    today_str = datetime.now().strftime('%A, %B %-d, %Y')
    sections_joined = '\n  '.join(sections_html)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="description" content="Oil market headlines grouped by topic — geopolitics, OPEC, refining margins, inventory, and prices. Updated daily.">
<meta name="theme-color" content="#07090d">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self' https://mob-chat.brad-95b.workers.dev;">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
{_FAVICON}
{_og_tags('News · MOB', 'Oil market headlines grouped by topic — geopolitics, OPEC, refining margins, inventory, and prices. Updated daily.', 'news')}
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>News · Morning Oil Brief</title>
<style>
:root {{
  color-scheme: dark;
  --bg: #0f1117; --panel: #1a1d24; --panel-2: #232730;
  --border: #25272e; --border-soft: #1c1e24;
  --text: #e4e7ec; --muted: #9aa0ac; --muted-2: #c1c5cf;
  --accent: #f59e0b;
}}
* {{ box-sizing: border-box; }}
html, body {{ background: var(--bg); margin: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  color: var(--text); font-size: 15px; line-height: 1.55; }}
.container {{ max-width: 1500px; margin: 0 auto; padding: 14px; }}
.header {{
  padding: 26px 4px 18px; text-align: center;
  background: radial-gradient(ellipse at 50% 0%, rgba(245,158,11,0.10) 0%, transparent 55%);
  border-bottom: 1px solid var(--border); margin-bottom: 22px;
}}
.header-eyebrow {{
  font-size: 12px; letter-spacing: 0.3px;
  color: var(--muted); font-weight: 600; margin-bottom: 10px;
}}
.header h1 {{
  font-size: 40px; font-weight: 700; line-height: 1.05;
  letter-spacing: -1.0px; margin: 0;
  background: linear-gradient(180deg, #ffffff 0%, #d8dce4 100%);
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent;
}}
.header-sub {{
  font-size: 13px; color: var(--muted-2); margin-top: 8px;
}}
{NAV_CSS}
.news-section {{ margin-bottom: 28px; }}
.news-section-title {{
  display: flex; align-items: center; gap: 10px;
  margin: 0 0 12px; padding-bottom: 6px;
  border-bottom: 1px solid var(--border-soft);
}}
.news-cat-pill {{
  display: inline-flex; align-items: center; gap: 7px;
  font-size: 12px; font-weight: 700; letter-spacing: 1px;
  text-transform: uppercase; padding: 5px 12px 5px 10px; border-radius: 999px;
  background: var(--cat-color, rgba(96,165,250,0.18));
  color: var(--cat-text, #cbd5e1);
  border: 1px solid var(--cat-border, rgba(96,165,250,0.4));
  line-height: 1;
}}
.news-cat-pill svg {{ display: block; opacity: 0.9; }}
.news-cat-pill .cat-label {{ letter-spacing: 1px; }}
.cat-top {{ --cat-color: rgba(251,191,36,0.18); --cat-text: #fde68a; --cat-border: rgba(251,191,36,0.45); }}
.cat-geopolitics-risk {{ --cat-color: rgba(239,68,68,0.18); --cat-text: #fca5a5; --cat-border: rgba(239,68,68,0.45); }}
.cat-opec-production {{ --cat-color: rgba(245,158,11,0.18); --cat-text: #fcd34d; --cat-border: rgba(245,158,11,0.45); }}
.cat-refining-margins {{ --cat-color: rgba(22,163,74,0.18); --cat-text: #86efac; --cat-border: rgba(22,163,74,0.45); }}
.cat-inventory-demand {{ --cat-color: rgba(167,139,250,0.18); --cat-text: #c4b5fd; --cat-border: rgba(167,139,250,0.45); }}
.cat-markets-prices {{ --cat-color: rgba(96,165,250,0.18); --cat-text: #93c5fd; --cat-border: rgba(96,165,250,0.45); }}
.cat-companies-deals {{ --cat-color: rgba(34,211,238,0.18); --cat-text: #67e8f9; --cat-border: rgba(34,211,238,0.45); }}
.cat-transition-macro {{ --cat-color: rgba(132,204,22,0.18); --cat-text: #bef264; --cat-border: rgba(132,204,22,0.45); }}
.news-count {{
  font-size: 11px; color: var(--muted); font-weight: 500;
}}
.news-grid {{
  display: grid; grid-template-columns: 1fr 1fr; gap: 12px 18px;
}}
@media (max-width: 900px) {{ .news-grid {{ grid-template-columns: 1fr; }} }}
.news-item {{
  display: block; padding: 12px 14px;
  text-decoration: none; color: inherit;
  background: var(--panel); border: 1px solid var(--border-soft);
  border-left: 3px solid var(--border);
  border-radius: 6px;
  transition: background 0.15s, border-color 0.15s, transform 0.15s;
}}
.news-item:hover {{
  background: rgba(96,165,250,0.05);
  border-left-color: #60a5fa;
  transform: translateX(2px);
}}
.news-title {{
  font-size: 14px; font-weight: 500; color: var(--text);
  line-height: 1.45; margin-bottom: 5px;
}}
.news-meta {{
  font-size: 11px; color: var(--muted);
}}
.news-time {{ color: var(--muted-2); }}
.news-desc {{
  font-size: 12.5px; color: var(--muted-2);
  margin-top: 8px; line-height: 1.55;
}}
footer {{
  margin-top: 28px; padding-top: 14px;
  border-top: 1px solid var(--border);
  font-size: 11px; color: var(--muted); text-align: center;
}}
</style>
</head>
<body>
{_render_nav('news.html')}

<div class="container">
  <div class="header">
    <h1>News</h1>
    <div class="header-sub">{total} headlines from the last 48 hours · grouped by topic</div>
  </div>

  {sections_joined}

  <footer>
    Last refresh: {refreshed_str} · Sources: OilPrice.com, Google News (Reuters, Bloomberg, CNBC, FT &amp; trade press)
  </footer>
</div>
</body>
</html>
"""


def main():
    raw = load_data()
    series = raw['series']

    # ── Determine the latest report date from crude_us
    latest_date_str = series['crude_us']['data'][-1][0]
    prior_date_str = series['crude_us']['data'][-2][0]
    latest_date = datetime.strptime(latest_date_str, '%Y-%m-%d')
    current_year = latest_date.year

    # KPIs: stocks (US level) in mb + refinery util in %
    def last_prev(key):
        d = series[key]['data']
        return d[-1][1], d[-2][1]

    def compute_kpi(cid, label, keys, units):
        """Aggregate one or more series for a KPI tile. `keys` is a list (single for individual tiles,
        multiple for the combined-stocks tile)."""
        # Latest and prior sums
        last_kb = sum(series[k]['data'][-1][1] for k in keys)
        prev_kb = sum(series[k]['data'][-2][1] for k in keys)
        if units == 'mil bbl':
            last_v = round(last_kb / 1000.0, 1)
            prev_v = round(prev_kb / 1000.0, 1)
        else:
            last_v = round(last_kb, 1); prev_v = round(prev_kb, 1)
        # 12-week trajectory (current year)
        recent_summed = []
        for i in range(12, 0, -1):
            try:
                s = sum(series[k]['data'][-i][1] for k in keys)
                recent_summed.append(round(s / 1000.0, 2) if units == 'mil bbl' else round(s, 1))
            except IndexError:
                recent_summed.append(None)
        # YoY same 12 weeks (~52 weeks back)
        recent_yoy = []
        for i in range(12, 0, -1):
            idx = -i - 52
            try:
                s = sum(series[k]['data'][idx][1] for k in keys)
                recent_yoy.append(round(s / 1000.0, 2) if units == 'mil bbl' else round(s, 1))
            except (IndexError, KeyError):
                recent_yoy.append(None)
        # 5-year band at the latest week's index
        latest_dt = datetime.strptime(series[keys[0]]['data'][-1][0], '%Y-%m-%d')
        latest_week = latest_dt.isocalendar().week
        band_vals = []
        # Build map year→week→sum across the key set
        year_week_sum = {}
        # Need to iterate each series and accumulate
        max_len = max(len(series[k]['data']) for k in keys)
        # Easier: walk through the first series's points, sum across keys at each point
        for i in range(max_len):
            try:
                d = series[keys[0]]['data'][-(i + 1)][0]
            except IndexError:
                continue
            dt = datetime.strptime(d, '%Y-%m-%d')
            wk = dt.isocalendar().week
            yr = dt.year
            try:
                s = sum(series[k]['data'][-(i + 1)][1] for k in keys)
            except IndexError:
                continue
            year_week_sum.setdefault(yr, {})[wk] = s
        for y in range(current_year - 5, current_year):
            if latest_week in year_week_sum.get(y, {}):
                v = year_week_sum[y][latest_week]
                band_vals.append(v / 1000.0 if units == 'mil bbl' else v)
        band_lo = round(min(band_vals), 1) if band_vals else last_v
        band_hi = round(max(band_vals), 1) if band_vals else last_v
        band_avg = round(sum(band_vals) / len(band_vals), 1) if band_vals else last_v
        return {
            'id': cid, 'label': label,
            'last': last_v, 'change': round(last_v - prev_v, 1),
            'units': units,
            'spark': recent_summed,
            'yoy': recent_yoy,
            'band_lo': band_lo, 'band_hi': band_hi, 'band_avg': band_avg,
        }

    kpi_data = [
        compute_kpi('total',    'Total Stocks',  ['crude_us', 'gas_us', 'dist_us', 'jet_us'], 'mil bbl'),
        compute_kpi('crude',    'Crude Oil',     ['crude_us'],   'mil bbl'),
        compute_kpi('gasoline', 'Gasoline',      ['gas_us'],     'mil bbl'),
        compute_kpi('diesel',   'Distillate',    ['dist_us'],    'mil bbl'),
        compute_kpi('jet',      'Jet Fuel',      ['jet_us'],     'mil bbl'),
        compute_kpi('util',     'Refinery Util.', ['refutil_us'], '%'),
    ]

    # Trade flows: 4-wk avgs in mb/d (raw kbd; convert to mbd in display)
    trade_keys = {
        'crude':    ('crude_imp_us', 'crude_exp_us'),
        'gasoline': ('gas_imp_us',   'gas_exp_us'),
        'diesel':   ('dist_imp_us',  'dist_exp_us'),
        'jet':      ('jet_imp_us',   'jet_exp_us'),
    }
    trade_snapshot = {}
    for c, (imp_k, exp_k) in trade_keys.items():
        if imp_k is None:
            # Hard-zero so the block exists; the page hides product trade for non-crude
            trade_snapshot[c] = {'imports': 0.0, 'exports': 0.0, 'importsPrev': 0.0, 'exportsPrev': 0.0}
            continue
        imp = compute_4wk_avg_kbd(series[imp_k]) / 1000.0
        exp = compute_4wk_avg_kbd(series[exp_k]) / 1000.0
        imp_prev = compute_4wk_avg_prev(series[imp_k]) / 1000.0
        exp_prev = compute_4wk_avg_prev(series[exp_k]) / 1000.0
        trade_snapshot[c] = {
            'imports': round(imp, 2), 'exports': round(exp, 2),
            'importsPrev': round(imp_prev, 2), 'exportsPrev': round(exp_prev, 2),
        }

    # Regional/PADD breakdown
    padd_names = {
        'p1': 'East Coast (PADD I)',
        'p2': 'Midwest (PADD II)',
        'p3': 'Gulf Coast (PADD III)',
        'p4': 'Rocky Mountain (PADD IV)',
        'p5': 'West Coast (PADD V)',
    }
    regional = []

    # Crude includes Cushing
    crude_rows = [{'name': 'Cushing, OK (within PADD II)',
                   'last': to_mb(series['crude_cushing']['data'][-1][1]),
                   'prev': to_mb(series['crude_cushing']['data'][-2][1]),
                   'excludeFromTotal': True}]
    for pk, pn in padd_names.items():
        s = series[f'crude_{pk}']
        crude_rows.append({'name': pn, 'last': to_mb(s['data'][-1][1]),
                           'prev': to_mb(s['data'][-2][1])})
    regional.append({'group': 'Crude Oil', 'commodity': 'crude', 'rows': crude_rows})

    gas_rows = []
    for pk, pn in padd_names.items():
        s = series[f'gas_{pk}']
        gas_rows.append({'name': pn, 'last': to_mb(s['data'][-1][1]),
                         'prev': to_mb(s['data'][-2][1])})
    regional.append({'group': 'Gasoline', 'commodity': 'gasoline', 'rows': gas_rows})

    dist_rows = []
    for pk, pn in padd_names.items():
        s = series[f'dist_{pk}']
        dist_rows.append({'name': pn, 'last': to_mb(s['data'][-1][1]),
                          'prev': to_mb(s['data'][-2][1])})
    regional.append({'group': 'Distillate', 'commodity': 'diesel', 'rows': dist_rows})

    jet_rows = []
    for pk, pn in padd_names.items():
        s = series[f'jet_{pk}']
        jet_rows.append({'name': pn, 'last': to_mb(s['data'][-1][1]),
                         'prev': to_mb(s['data'][-2][1])})
    regional.append({'group': 'Jet Fuel', 'commodity': 'jet', 'rows': jet_rows})

    # Refinery utilization table
    refinery = [{'name': 'U.S. Total', 'last': round(series['refutil_us']['data'][-1][1], 1),
                 'prev': round(series['refutil_us']['data'][-2][1], 1)}]
    for pk, pn in padd_names.items():
        s = series[f'refutil_{pk}']
        refinery.append({'name': pn, 'last': round(s['data'][-1][1], 1),
                         'prev': round(s['data'][-2][1], 1)})

    # PADD-level narrative (factual, derived from real numbers)
    def wow_mb(prefix, padd_k):
        last_kb, prev_kb = last_prev(f'{prefix}_{padd_k}')
        return round((last_kb - prev_kb) / 1000.0, 3)

    def wow_pct(padd_k):
        s = series[f'refutil_{padd_k}']
        return round(s['data'][-1][1] - s['data'][-2][1], 1)

    padd_meta = [
        ('padd1', 'PADD I · East Coast', 'p1'),
        ('padd2', 'PADD II · Midwest',   'p2'),
        ('padd3', 'PADD III · Gulf Coast', 'p3'),
        ('padd4', 'PADD IV · Rocky Mountain', 'p4'),
        ('padd5', 'PADD V · West Coast', 'p5'),
    ]

    # Compute WoW changes (needed for narrative context AND padd_read)
    def headline(k):
        last_kb, prev_kb = last_prev(k)
        return (last_kb - prev_kb) / 1000.0  # mb change

    crude_chg = headline('crude_us'); gas_chg = headline('gas_us')
    dist_chg = headline('dist_us'); jet_chg = headline('jet_us')
    util_chg = round(series['refutil_us']['data'][-1][1] - series['refutil_us']['data'][-2][1], 1)
    cushing_last = to_mb(series['crude_cushing']['data'][-1][1])
    cushing_chg = (series['crude_cushing']['data'][-1][1] - series['crude_cushing']['data'][-2][1]) / 1000.0
    crude_exp_4wk = compute_4wk_avg_kbd(series['crude_exp_us']) / 1000.0
    crude_imp_4wk = compute_4wk_avg_kbd(series['crude_imp_us']) / 1000.0

    def sign_mb(v): return f'{("+" if v >= 0 else "")}{v:.1f} mb'
    def sign_pp(v): return f'{("+" if v >= 0 else "")}{v:.1f} pp'

    # Build a context bundle for narrative generation
    util_us_last = series['refutil_us']['data'][-1][1]
    crude_imp_prev_4wk = compute_4wk_avg_prev(series['crude_imp_us']) / 1000.0
    crude_exp_prev_4wk = compute_4wk_avg_prev(series['crude_exp_us']) / 1000.0
    gas_dem_4wk = compute_4wk_avg_kbd(series['gas_demand']) / 1000.0
    gas_dem_prev_4wk = compute_4wk_avg_prev(series['gas_demand']) / 1000.0
    dist_dem_4wk = compute_4wk_avg_kbd(series['dist_demand']) / 1000.0
    jet_dem_4wk = compute_4wk_avg_kbd(series['jet_demand']) / 1000.0
    crude_input_chg = (series['crude_input']['data'][-1][1] - series['crude_input']['data'][-2][1]) / 1000.0
    crude_input_last_mbd = series['crude_input']['data'][-1][1] / 1000.0

    # Cushing 5-wk trajectory for context
    cushing_5wk = [(d, v / 1000.0) for d, v in series['crude_cushing']['data'][-5:]]

    nctx = {
        'reportDate': latest_date.strftime('%B %-d, %Y'),
        'us': {
            'crude': {'last': to_mb(series['crude_us']['data'][-1][1]), 'chg': round(crude_chg, 2)},
            'gas':   {'last': to_mb(series['gas_us']['data'][-1][1]),   'chg': round(gas_chg, 2)},
            'dist':  {'last': to_mb(series['dist_us']['data'][-1][1]),  'chg': round(dist_chg, 2)},
            'jet':   {'last': to_mb(series['jet_us']['data'][-1][1]),   'chg': round(jet_chg, 2)},
            'util':  {'last': round(util_us_last, 1), 'chg': util_chg},
            'cushing': {'last': cushing_last, 'chg': round(cushing_chg, 2),
                        'trajectory_mb': [(d, round(v, 2)) for d, v in cushing_5wk]},
        },
        'trade_4wk_mbd': {
            'crude_imp': round(crude_imp_4wk, 2), 'crude_imp_prev': round(crude_imp_prev_4wk, 2),
            'crude_exp': round(crude_exp_4wk, 2), 'crude_exp_prev': round(crude_exp_prev_4wk, 2),
        },
        'demand_4wk_mbd': {
            'gasoline': round(gas_dem_4wk, 2), 'gasoline_prev': round(gas_dem_prev_4wk, 2),
            'distillate': round(dist_dem_4wk, 2),
            'jet': round(jet_dem_4wk, 2),
        },
        'refiner_inputs_chg_mbd': round(crude_input_chg, 2),
        'crude_inputs_mbd': round(crude_input_last_mbd, 2),
        'padds': {
            f'padd{p}': {
                'crude_chg':  round(wow_mb('crude', f'p{p}'), 2),
                'gas_chg':    round(wow_mb('gas', f'p{p}'), 2),
                'dist_chg':   round(wow_mb('dist', f'p{p}'), 2),
                'jet_chg':    round(wow_mb('jet', f'p{p}'), 2),
                'util_last':  round(series[f'refutil_p{p}']['data'][-1][1], 1),
                'util_chg':   round(series[f'refutil_p{p}']['data'][-1][1] - series[f'refutil_p{p}']['data'][-2][1], 1),
            } for p in range(1, 6)
        },
    }

    # Cache main narratives too (cheap optimization for re-builds on same data)
    nar_cache_path = os.path.join(HERE, '.narratives_cache.json')
    import hashlib
    _wiki_raw_n = _load_wiki_context()
    _wiki_hash_n = hashlib.md5(_wiki_raw_n.encode()).hexdigest()[:8] if _wiki_raw_n else 'none'
    nar_key = hashlib.md5((json.dumps(nctx, sort_keys=True) + _wiki_hash_n).encode()).hexdigest()
    try:
        with open(nar_cache_path) as f:
            nar_cache = json.load(f)
    except Exception:
        nar_cache = {}
    if nar_cache.get('key') == nar_key and nar_cache.get('value'):
        print('  → narratives: from cache')
        narratives = nar_cache['value']
    else:
        narratives = generate_narratives(nctx)
        try:
            with open(nar_cache_path, 'w') as f:
                json.dump({'key': nar_key, 'value': narratives}, f, indent=2)
        except Exception:
            pass
    # Normalize: real newlines + bold paragraph leads.
    market_read = _normalize_narrative(narratives.get('market_read', ''))

    # Generate trading calls (separate Claude call, cached alongside narratives)
    tc_cache_path = os.path.join(HERE, '.trading_calls_cache.json')
    tc_key = hashlib.md5(json.dumps(nctx, sort_keys=True).encode()).hexdigest()
    try:
        with open(tc_cache_path) as f:
            tc_cache = json.load(f)
    except Exception:
        tc_cache = {}
    if tc_cache.get('key') == tc_key and tc_cache.get('value') is not None:
        print('  → trading calls: from cache')
        trading_calls = tc_cache['value']
    else:
        trading_calls = generate_trading_calls(nctx)
        try:
            with open(tc_cache_path, 'w') as f:
                json.dump({'key': tc_key, 'value': trading_calls}, f, indent=2)
        except Exception:
            pass

    # NOW build padd_read using the generated narratives
    padd_read = []
    for pid, pname, pk in padd_meta:
        crude_d = wow_mb('crude', pk); gas_d = wow_mb('gas', pk)
        dist_d = wow_mb('dist', pk); util_d = wow_pct(pk)
        draws = sum(1 for x in [crude_d, gas_d, dist_d] if x < 0)
        trend = 'bull' if draws >= 2 and util_d >= 0 else ('bear' if draws == 0 else 'neutral')
        padd_read.append({
            'id': pid, 'name': pname, 'trend': trend,
            'crudeWoW': crude_d, 'gasWoW': gas_d, 'distWoW': dist_d, 'utilWoW': util_d,
            'narrative': narratives['padd'].get(pid, ''),
        })

    # SNAP_LAST_US (latest US-level for chart anchoring)
    snap_last_us = {
        'crude':    kpi_data[0]['last'],
        'gasoline': kpi_data[1]['last'],
        'diesel':   kpi_data[2]['last'],
        'jet':      kpi_data[3]['last'],
        'util':     kpi_data[4]['last'],
    }
    snap_last_reg = {
        'crude':    {f'padd{i+1}': to_mb(series[f'crude_p{i+1}']['data'][-1][1]) for i in range(5)},
        'gasoline': {f'padd{i+1}': to_mb(series[f'gas_p{i+1}']['data'][-1][1]) for i in range(5)},
        'diesel':   {f'padd{i+1}': to_mb(series[f'dist_p{i+1}']['data'][-1][1]) for i in range(5)},
        'jet':      {f'padd{i+1}': to_mb(series[f'jet_p{i+1}']['data'][-1][1]) for i in range(5)},
        'util':     {f'padd{i+1}': round(series[f'refutil_p{i+1}']['data'][-1][1], 1) for i in range(5)},
    }
    snap_last_reg['crude']['cushing'] = to_mb(series['crude_cushing']['data'][-1][1])
    if 'crude_spr' in series:
        snap_last_reg['crude']['spr'] = to_mb(series['crude_spr']['data'][-1][1])
    # Sub-PADD anchors (only gasoline & distillate are published at this granularity)
    for sub in ('1a', '1b', '1c'):
        snap_last_reg['gasoline'][f'padd{sub}'] = to_mb(series[f'gas_p{sub}']['data'][-1][1])
        snap_last_reg['diesel'][f'padd{sub}']   = to_mb(series[f'dist_p{sub}']['data'][-1][1])

    # ── Build full HIST and TRADE blocks
    HIST = {'crude': {}, 'gasoline': {}, 'diesel': {}, 'jet': {}, 'util': {}}
    HIST['crude']['us'] = build_hist_block(series['crude_us'], current_year, 'mb')
    HIST['crude']['cushing'] = build_hist_block(series['crude_cushing'], current_year, 'mb')
    if 'crude_spr' in series:
        HIST['crude']['spr'] = build_hist_block(series['crude_spr'], current_year, 'mb')
    for i in range(5):
        HIST['crude'][f'padd{i+1}'] = build_hist_block(series[f'crude_p{i+1}'], current_year, 'mb')
        HIST['gasoline'][f'padd{i+1}'] = build_hist_block(series[f'gas_p{i+1}'], current_year, 'mb')
        HIST['diesel'][f'padd{i+1}'] = build_hist_block(series[f'dist_p{i+1}'], current_year, 'mb')
        HIST['jet'][f'padd{i+1}'] = build_hist_block(series[f'jet_p{i+1}'], current_year, 'mb')
        HIST['util'][f'padd{i+1}'] = build_hist_block(series[f'refutil_p{i+1}'], current_year, 'raw')
    HIST['gasoline']['us'] = build_hist_block(series['gas_us'], current_year, 'mb')
    HIST['diesel']['us']   = build_hist_block(series['dist_us'], current_year, 'mb')
    HIST['jet']['us']      = build_hist_block(series['jet_us'], current_year, 'mb')
    HIST['util']['us']     = build_hist_block(series['refutil_us'], current_year, 'raw')
    # Sub-PADD histories — gasoline & distillate only (jet/crude not published at sub-PADD)
    for sub in ('1a', '1b', '1c'):
        HIST['gasoline'][f'padd{sub}'] = build_hist_block(series[f'gas_p{sub}'], current_year, 'mb')
        HIST['diesel'][f'padd{sub}']   = build_hist_block(series[f'dist_p{sub}'], current_year, 'mb')

    # TRADE: only crude has dedicated import/export series — for products,
    # we'll build empty containers so the UI doesn't break
    TRADE = {'crude': {}, 'gasoline': {}, 'diesel': {}, 'jet': {}}
    for c, (imp_k, exp_k) in trade_keys.items():
        if imp_k is None:
            # Empty series so the UI gracefully shows nothing
            TRADE[c] = {'imports': {}, 'exports': {}, 'net': {}}
            for y in range(2021, current_year + 1):
                TRADE[c]['imports'][str(y)] = []
                TRADE[c]['exports'][str(y)] = []
                TRADE[c]['net'][str(y)]     = []
            continue
        imp_block = build_hist_block(series[imp_k], current_year, 'mb')  # kb/d -> mb/d (divide 1000)
        exp_block = build_hist_block(series[exp_k], current_year, 'mb')
        net_block = {}
        for y in imp_block:
            imp_v = imp_block[y]; exp_v = exp_block[y]
            net_block[y] = [round(i - e, 3) if i is not None and e is not None else None
                            for i, e in zip(imp_v, exp_v)]
        TRADE[c] = {'imports': imp_block, 'exports': exp_block, 'net': net_block}

    # ── Compose the replacement JavaScript block
    def js_year_obj(year_block):
        """JS dict literal: {"2021": [...], ...}"""
        parts = []
        for y in sorted(year_block.keys()):
            arr = year_block[y]
            parts.append(f'"{y}":' + js_array(arr, 3))
        return '{' + ','.join(parts) + '}'

    def js_hist_full():
        out = ['{']
        for c in HIST:
            inner = []
            for r in HIST[c]:
                inner.append(f'"{r}":' + js_year_obj(HIST[c][r]))
            out.append(f'"{c}":{{' + ','.join(inner) + '}')
            out.append(',')
        out.pop()
        out.append('}')
        return ''.join(out)

    def js_trade_full():
        out = ['{']
        for c in TRADE:
            inner = []
            for t in TRADE[c]:
                inner.append(f'"{t}":' + js_year_obj(TRADE[c][t]))
            out.append(f'"{c}":{{' + ','.join(inner) + '}')
            out.append(',')
        out.pop()
        out.append('}')
        return ''.join(out)

    def js_snapshot():
        # Released date = the WPSR release date for THIS week-ending Friday.
        # Holiday-aware: e.g. data for week ending Aug 29, 2025 was released
        # Thu Sep 4 (Labor Day shift), not Wed Sep 3. Format matches the
        # rest of the header: "Wed May 20, 2026" / "Thu May 28, 2026".
        _release_date, _release_weekday, _ = _wpsr_release_for(latest_date)
        released_str = _release_date.strftime(f'{_release_weekday[:3]} %b %-d, %Y')
        return json.dumps({
            'reportDate':   latest_date.strftime('%b %-d, %Y'),
            'releasedDate': released_str,
            'nextRelease':  _next_wpsr_release_str(latest_date),
            'lastRefreshed': _utcnow().strftime('%b %-d, %Y'),
            'marketRead':   market_read,
            'tradingCalls': trading_calls,
            'kpi':          kpi_data,
            'trade':        trade_snapshot,
            'regional':     regional,
            'refinery':     refinery,
            'paddRead':     padd_read,
        }, indent=2)

    snapshot_js = 'const SNAPSHOT = ' + js_snapshot() + ';'
    snap_last_us_js = 'const SNAP_LAST_US = ' + json.dumps(snap_last_us) + ';'
    snap_last_reg_js = 'const SNAP_LAST_REG = ' + json.dumps(snap_last_reg) + ';'
    hist_js = 'const HIST = ' + js_hist_full() + ';'
    trade_js = 'const TRADE = ' + js_trade_full() + ';'

    # Read template HTML
    with open(TEMPLATE) as f:
        html = f.read()

    # Replace `const SNAPSHOT = { ... };` block (multi-line until matching `};`)
    html = re.sub(
        r'const SNAPSHOT = \{[\s\S]*?\n\};',
        snapshot_js.replace('\\', '\\\\'),
        html, count=1
    )
    # Replace the whole PROFILES/TRADE_PROFILES/genYear/genYTD/HIST/TRADE construction
    # by SNAP_LAST_US + SNAP_LAST_REG + HIST + TRADE assignments.
    # We anchor on `// ============================================================\n// HISTORICAL DATA`
    # through the next `// ============================================================` comment.
    pattern = re.compile(
        r'// ===+\n// HISTORICAL DATA[\s\S]+?(?=// ===+\n// (?:[A-Z]|UI BIND))',
        re.MULTILINE
    )
    replacement = (
        '// ============================================================\n'
        '// HISTORICAL DATA - REAL EIA values, precomputed by build_index.py\n'
        '// ============================================================\n'
        + snap_last_us_js + '\n'
        + snap_last_reg_js + '\n'
        + 'const REGIONS = ["us","padd1","padd1a","padd1b","padd1c","padd2","padd3","padd4","padd5","cushing","spr"];\n'
        + 'const COMMODITIES = ["crude","gasoline","diesel","jet","util"];\n'
        + 'const YTD_WEEKS = ' + str(min(52, max(len(b.get(str(current_year), [])) for b in HIST["crude"].values()))) + ';\n'
        + hist_js + '\n'
        + trade_js + '\n\n'
    )
    new_html, count = pattern.subn(replacement, html, count=1)
    if count == 0:
        print('WARNING: HIST block pattern did not match — output unchanged from template')
        new_html = html

    # ─── Build the seasonal mini-charts section ──────────────────────────────
    # 7 crude (US + 5 PADDs + Cushing) + 6 gas + 6 diesel + 6 jet = 25 charts
    mini_layout = [
        ('crude',    'Crude Oil',  [
            ('us', 'U.S. Total'), ('padd1', 'PADD I · East Coast'),
            ('padd2', 'PADD II · Midwest'), ('padd3', 'PADD III · Gulf Coast'),
            ('padd4', 'PADD IV · Rocky Mtn'), ('padd5', 'PADD V · West Coast'),
            ('cushing', 'Cushing, OK'),
            ('spr', 'Strategic Petroleum Reserve'),
        ]),
        ('gasoline', 'Gasoline', [
            ('us', 'U.S. Total'), ('padd1', 'PADD I · East Coast'),
            ('padd2', 'PADD II · Midwest'), ('padd3', 'PADD III · Gulf Coast'),
            ('padd4', 'PADD IV · Rocky Mtn'), ('padd5', 'PADD V · West Coast'),
        ]),
        ('diesel',   'Distillate (Diesel)', [
            ('us', 'U.S. Total'), ('padd1', 'PADD I · East Coast'),
            ('padd2', 'PADD II · Midwest'), ('padd3', 'PADD III · Gulf Coast'),
            ('padd4', 'PADD IV · Rocky Mtn'), ('padd5', 'PADD V · West Coast'),
        ]),
        ('jet',      'Jet Fuel', [
            ('us', 'U.S. Total'), ('padd1', 'PADD I · East Coast'),
            ('padd2', 'PADD II · Midwest'), ('padd3', 'PADD III · Gulf Coast'),
            ('padd4', 'PADD IV · Rocky Mtn'), ('padd5', 'PADD V · West Coast'),
        ]),
        ('util',     'Refinery Utilization', [
            ('us', 'U.S. Total'), ('padd1', 'PADD I · East Coast'),
            ('padd2', 'PADD II · Midwest'), ('padd3', 'PADD III · Gulf Coast'),
            ('padd4', 'PADD IV · Rocky Mtn'), ('padd5', 'PADD V · West Coast'),
        ]),
    ]

    # Compute context for each chart (latest value, WoW change, 5-yr range)
    mini_contexts = []
    for commodity, _, regions in mini_layout:
        for region_key, region_label in regions:
            block = HIST[commodity][region_key]
            cur_year_arr = block.get(str(current_year), [])
            prev_year_arr = block.get(str(current_year - 1), [])
            if not cur_year_arr:
                continue
            last = cur_year_arr[-1]
            prev = cur_year_arr[-2] if len(cur_year_arr) >= 2 else None
            # 5-yr band at the same week index
            wk_idx = len(cur_year_arr) - 1
            band_vals = []
            for y in range(current_year - 5, current_year):
                arr = block.get(str(y), [])
                if wk_idx < len(arr) and arr[wk_idx] is not None:
                    band_vals.append(arr[wk_idx])
            band_lo = min(band_vals) if band_vals else last
            band_hi = max(band_vals) if band_vals else last
            band_avg = sum(band_vals) / len(band_vals) if band_vals else last
            prior_yr = prev_year_arr[wk_idx] if wk_idx < len(prev_year_arr) else None
            mini_contexts.append({
                'id': f'{commodity}_{region_key}',
                'commodity': commodity,
                'region': region_label,
                'last': round(last, 2),
                'chg': round(last - prev, 2) if prev is not None else 0,
                'prior_yr': round(prior_yr, 2) if prior_yr else None,
                'band_lo': round(band_lo, 1),
                'band_hi': round(band_hi, 1),
                'band_avg': round(band_avg, 1),
            })

    captions = generate_mini_captions(mini_contexts)

    # Build HTML for the section, with per-commodity color theming
    commodity_colors = {
        'crude':    '#f59e0b',  # amber — oil
        'gasoline': '#ef4444',  # red   — gas-pump handle
        'diesel':   '#facc15',  # yellow — diesel pump handle
        'jet':      '#22d3ee',  # cyan  — sky / aviation
        'util':     '#a78bfa',  # violet — refinery operations
    }
    mini_section_html = ['<div class="section">',
        '<div class="section-header"><h2 class="section-title">Seasonal Charts by Region '
        '<span class="section-subtitle">stock levels vs 5-yr band, by commodity and PADD</span></h2></div>']
    for commodity, commodity_label, regions in mini_layout:
        color = commodity_colors.get(commodity, '#9aa0ac')
        mini_section_html.append(
            f'<div class="mini-commodity-header mini-cm-{commodity}" '
            f'style="--cm-color:{color}">{commodity_label}</div>'
        )
        mini_section_html.append(f'<div class="mini-grid mini-grid-{commodity}">')
        for region_key, region_label in regions:
            cid = f'{commodity}_{region_key}'
            caption = captions.get(cid, '')
            mini_section_html.append(
                f'  <div class="mini-card" style="--cm-color:{color}">'
                f'<div class="mini-title">{region_label}</div>'
                f'<div class="mini-canvas-wrap"><canvas data-mini="{cid}"></canvas></div>'
                f'<div class="mini-narrative">{caption}</div>'
                f'</div>'
            )
        mini_section_html.append('</div>')
    mini_section_html.append('</div>')
    mini_section_block = '\n'.join(mini_section_html)

    # ─── KPI card redesign — overrides the original .kpi styles + render JS ───
    kpi_redesign_css = _KPI_CSS_BODY
    kpi_redesign_js = _KPI_JS_BODY
    if False:  # legacy stub kept solely so the long string literals below parse
        _KPI_CSS_LEGACY = """
/* KPI card redesign — gradient bg, sparkline, band indicator */
.kpi-strip { grid-template-columns: repeat(6, 1fr); gap: 8px; max-width: 1400px; margin-left: auto; margin-right: auto; }
@media (max-width: 1280px) { .kpi-strip { grid-template-columns: repeat(3, 1fr) !important; } }
@media (max-width: 900px)  { .kpi-strip { grid-template-columns: repeat(2, 1fr) !important; } }
.kpi {
  border-radius: 8px !important;
  padding: 12px 13px !important;
  position: relative; overflow: hidden;
  background: var(--panel-2) !important;
  border: 1px solid var(--border) !important;
  transition: transform 0.15s, box-shadow 0.15s, border-color 0.15s !important;
  display: flex; flex-direction: column; gap: 8px;
}
.kpi::before {
  content: ''; position: absolute; inset: 0;
  background: linear-gradient(135deg, var(--kpi-tint, transparent) 0%, transparent 55%);
  pointer-events: none; z-index: 0;
}
.kpi::after {
  content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
  background: var(--kpi-accent, transparent);
}
.kpi.build { --kpi-tint: rgba(22,163,74,0.22); --kpi-accent: #16a34a; }
.kpi.draw  { --kpi-tint: rgba(220,38,38,0.22);  --kpi-accent: #dc2626; }
.kpi.neutral { --kpi-tint: rgba(120,130,150,0.18); --kpi-accent: #6b7280; }
.kpi:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(0,0,0,0.35); border-color: var(--kpi-accent); }
.kpi > * { position: relative; z-index: 1; }
.kpi-row { display: flex; justify-content: space-between; align-items: center; }
.kpi-label {
  font-size: 12px; font-weight: 700; color: var(--muted-2);
  letter-spacing: -0.1px;
}
.kpi-tag {
  font-size: 11px; padding: 3px 9px; border-radius: 999px;
  background: var(--kpi-accent); color: #fff; font-weight: 700;
  letter-spacing: 0.8px; text-transform: uppercase;
}
.kpi-value {
  font-size: 24px; font-weight: 700; color: var(--text);
  letter-spacing: -0.5px; font-variant-numeric: tabular-nums;
  line-height: 1; display: flex; align-items: baseline; gap: 4px;
}
.kpi-value-units {
  font-size: 11px; font-weight: 500; color: var(--muted);
  letter-spacing: 0.3px;
}
.kpi-change-row {
  display: flex; align-items: center; gap: 6px; font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.kpi-change {
  font-weight: 600; color: var(--kpi-accent);
  display: inline-flex; align-items: center; gap: 3px;
}
.kpi-change-sub { color: var(--muted); font-size: 11px; }
.kpi-position {
  display: inline-block; font-size: 11px; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.6px;
  padding: 3px 9px; border-radius: 999px;
  background: var(--kpi-pos-bg, rgba(255,255,255,0.06));
  color: var(--kpi-pos-fg, var(--muted-2));
  border: 1px solid var(--kpi-pos-border, transparent);
  margin-top: 4px;
  align-self: flex-start;
}
.kpi-position.pos-below { --kpi-pos-bg: rgba(239,68,68,0.18); --kpi-pos-fg: #fca5a5; --kpi-pos-border: rgba(239,68,68,0.45); }
.kpi-position.pos-near-lo { --kpi-pos-bg: rgba(251,146,60,0.16); --kpi-pos-fg: #fdba74; }
.kpi-position.pos-mid { --kpi-pos-bg: rgba(255,255,255,0.07); --kpi-pos-fg: var(--muted-2); }
.kpi-position.pos-near-hi { --kpi-pos-bg: rgba(34,197,94,0.16); --kpi-pos-fg: #86efac; }
.kpi-position.pos-above { --kpi-pos-bg: rgba(34,197,94,0.18); --kpi-pos-fg: #86efac; --kpi-pos-border: rgba(34,197,94,0.45); }
.kpi-spark {
  height: 32px; width: 100%; opacity: 0.95; margin-top: 2px;
}
.kpi-band {
  display: grid; grid-template-columns: auto 1fr auto;
  align-items: center; gap: 5px;
  font-size: 11px; color: var(--muted);
  letter-spacing: 0.3px;
}
.kpi-band-bar {
  height: 8px; position: relative;
  display: flex; align-items: center;
}
.kpi-band-track {
  position: absolute; left: 0; right: 0; height: 6px; top: 1px;
  background: rgba(255,255,255,0.05); border-radius: 3px;
}
.kpi-band-fill {
  position: absolute; left: 0; right: 0; height: 6px; top: 1px;
  background: linear-gradient(90deg, rgba(252,165,165,0.2), rgba(255,255,255,0.25), rgba(134,239,172,0.2));
  border-radius: 3px;
}
.kpi-band-marker {
  position: absolute; width: 10px; height: 10px;
  background: #fbbf24; border-radius: 50%;
  border: 2px solid #232730; top: -1px;
  box-shadow: 0 0 8px rgba(251,191,36,0.6);
  transform: translateX(-50%);
}
.kpi-band-marker.out-low {
  background: #ef4444; left: -4px; transform: none;
  box-shadow: 0 0 8px rgba(239,68,68,0.7);
}
.kpi-band-marker.out-low::before {
  content: '◄'; position: absolute; left: -10px; top: -4px;
  color: #ef4444; font-size: 12px;
}
.kpi-band-marker.out-high {
  background: #22c55e; right: -4px; left: auto; transform: none;
  box-shadow: 0 0 8px rgba(34,197,94,0.7);
}
.kpi-band-marker.out-high::after {
  content: '►'; position: absolute; right: -10px; top: -4px;
  color: #22c55e; font-size: 12px;
}
"""
        _KPI_JS_LEGACY = """
// ─── KPI redesign — rebuild cards with sparkline + band indicator ───
function redesignKpis(){
  const strip = document.getElementById('kpi-strip');
  if (!strip || !SNAPSHOT.kpi) return;
  strip.innerHTML = '';
  const toRender = [];
  SNAPSHOT.kpi.forEach(k => {
    const isBuild = k.change >= 0;
    const cls = isBuild ? 'build' : 'draw';
    // Tag wording depends on what the card represents
    let tag;
    if (k.id === 'util')                tag = isBuild ? 'UP' : 'DOWN';
    else if (k.units === 'mil bbl')     tag = isBuild ? 'BUILD' : 'DRAW';
    else                                tag = isBuild ? 'HIGHER' : 'LOWER';
    const arrow = isBuild ? '▲' : '▼';
    // Unit handling: %, mil bbl (→MMbbl), $/bbl, $/gal
    let valPrefix = '', valSuffix = '', unitsTag = '', chgPrefix = '', chgSuffix = ' mb', valDecimals = 1, chgDecimals = 1;
    if (k.units === '%') { valSuffix = '%'; unitsTag = 'utilization'; chgSuffix = ' pp'; }
    else if (k.units === 'mil bbl') { unitsTag = 'MMbbl'; chgSuffix = ' mb'; }
    else if (k.units === '$/bbl') { valPrefix = '$'; unitsTag = '/bbl'; chgPrefix = '$'; chgSuffix = '/bbl'; valDecimals = 2; chgDecimals = 2; }
    else if (k.units === '$/gal') { valPrefix = '$'; unitsTag = '/gal'; chgPrefix = '$'; chgSuffix = '/gal'; valDecimals = 4; chgDecimals = 4; }
    else { unitsTag = k.units || ''; }
    const unitStr = valSuffix;
    // Position in 5-yr band: pct (clipped 0..1) plus out-of-band flag and label
    const range = k.band_hi - k.band_lo;
    const rawPct = range > 0 ? (k.last - k.band_lo) / range : 0.5;
    const pct = Math.max(0, Math.min(1, rawPct));
    let markerCls = '', posLabel = '', posCls = 'pos-mid';
    // Unit text used in band-position labels
    let bandUnit = ' mb';
    if (k.units === '%')      bandUnit = ' pp';
    else if (k.units === '$/bbl') bandUnit = '/bbl';
    else if (k.units === '$/gal') bandUnit = '/gal';
    const isMoney = (k.units === '$/bbl' || k.units === '$/gal');
    if (k.last < k.band_lo) {
      markerCls = 'out-low';
      const dVal = (k.band_lo - k.last);
      const dStr = (isMoney ? '$' : '') + dVal.toFixed(isMoney ? 2 : 1);
      posLabel = `${dStr}${bandUnit} below 5Y low`;
      posCls = 'pos-below';
    } else if (k.last > k.band_hi) {
      markerCls = 'out-high';
      const dVal = (k.last - k.band_hi);
      const dStr = (isMoney ? '$' : '') + dVal.toFixed(isMoney ? 2 : 1);
      posLabel = `+${dStr}${bandUnit} above 5Y high`;
      posCls = 'pos-above';
    } else if (rawPct < 0.25) {
      posLabel = 'Near 5Y low';
      posCls = 'pos-near-lo';
    } else if (rawPct > 0.75) {
      posLabel = 'Near 5Y high';
      posCls = 'pos-near-hi';
    } else if (k.band_avg && Math.abs(k.last - k.band_avg) < range * 0.08) {
      posLabel = 'At 5Y avg';
      posCls = 'pos-mid';
    } else {
      posLabel = 'Within 5Y band';
      posCls = 'pos-mid';
    }
    const card = document.createElement('div');
    card.className = `kpi ${cls}`;
    card.innerHTML = `
      <div class="kpi-row">
        <span class="kpi-label">${k.label}</span>
        <span class="kpi-tag">${tag}</span>
      </div>
      <div class="kpi-value">${valPrefix}${k.last.toFixed(valDecimals)}${unitStr}<span class="kpi-value-units">${unitsTag}</span></div>
      <div class="kpi-change-row">
        <span class="kpi-change">${arrow} ${chgPrefix}${Math.abs(k.change).toFixed(chgDecimals)}${chgSuffix}</span>
        <span class="kpi-change-sub">vs prior ${k.frequency === 'daily' ? 'day' : 'wk'}</span>
      </div>
      <canvas class="kpi-spark" data-kpi-spark="${k.id}"></canvas>
      <div class="kpi-band">
        <span>${k.band_lo.toFixed(0)}</span>
        <div class="kpi-band-bar">
          <div class="kpi-band-track"></div>
          <div class="kpi-band-fill"></div>
          <div class="kpi-band-marker ${markerCls}" style="${markerCls ? '' : 'left:'+(pct*100).toFixed(0)+'%'}"></div>
        </div>
        <span>${k.band_hi.toFixed(0)}</span>
      </div>
      <span class="kpi-position ${posCls}">${posLabel}</span>
    `;
    strip.appendChild(card);
    toRender.push({card, k, isBuild});
  });
  // Defer sparkline charts to next frame so layout is computed first
  requestAnimationFrame(() => {
    toRender.forEach(({card, k, isBuild}) => {
      const canvas = card.querySelector('canvas.kpi-spark');
      if (!canvas) return;
      // Set explicit pixel dimensions to bypass auto-sizing
      const rect = canvas.parentElement.getBoundingClientRect();
      canvas.width = Math.max(60, Math.floor(rect.width)) * 2;
      canvas.height = 64;
      canvas.style.width = '100%';
      canvas.style.height = '32px';
      try {
        const datasets = [];
        // YoY dotted comparison line (drawn first so it's behind the current line)
        if (k.yoy && k.yoy.some(v => v != null)) {
          datasets.push({
            data: k.yoy,
            borderColor: 'rgba(180,180,200,0.55)',
            borderWidth: 1, pointRadius: 0, fill: false, tension: 0.35,
            borderDash: [3, 3],
          });
        }
        datasets.push({
          data: k.spark,
          borderColor: isBuild ? '#16a34a' : '#dc2626',
          backgroundColor: isBuild ? 'rgba(22,163,74,0.18)' : 'rgba(220,38,38,0.18)',
          borderWidth: 1.6, pointRadius: 0, fill: true, tension: 0.35,
        });
        new Chart(canvas.getContext('2d'), {
          type: 'line',
          data: { labels: k.spark.map((_, i) => i), datasets },
          options: {
            responsive: false, maintainAspectRatio: false, animation: false,
            plugins: { legend: { display: false }, tooltip: { enabled: false } },
            scales: { x: { display: false }, y: { display: false } },
            elements: { line: { borderJoinStyle: 'round' } },
          },
        });
      } catch(e) { /* silently skip if Chart fails on this card */ }
    });
  });
}
// Run after window fully loaded so all dimensions are stable
if (document.readyState === 'complete') redesignKpis();
else window.addEventListener('load', redesignKpis);
"""
    # end of legacy if-False placeholder block

    # Extra CSS: badges + colored region-table headers
    extra_css = """
.badge {
  display: inline-block; font-size: 11px; font-weight: 700;
  padding: 3px 8px; border-radius: 999px; vertical-align: 2px;
  letter-spacing: 0.7px; text-transform: uppercase;
  margin-left: 6px; margin-right: 4px;
}
.badge-interactive {
  background: rgba(96,165,250,0.15); color: #93c5fd;
  border: 1px solid rgba(96,165,250,0.4);
  animation: pulse-badge 2.4s ease-in-out infinite;
}
.badge-ai {
  background: linear-gradient(90deg, rgba(167,139,250,0.18), rgba(96,165,250,0.18));
  color: #c4b5fd; border: 1px solid rgba(167,139,250,0.4);
}
@keyframes pulse-badge {
  0%, 100% { box-shadow: 0 0 0 0 rgba(96,165,250,0.4); }
  50%      { box-shadow: 0 0 0 6px rgba(96,165,250,0); }
}
/* Regional table category headers — color by commodity */
tr.region-header.region-crude    td { color: #f59e0b !important; border-left: 3px solid #f59e0b; }
tr.region-header.region-gasoline td { color: #ef4444 !important; border-left: 3px solid #ef4444; }
tr.region-header.region-diesel   td { color: #facc15 !important; border-left: 3px solid #facc15; }
tr.region-header.region-jet      td { color: #22d3ee !important; border-left: 3px solid #22d3ee; }
tr.region-header td {
  font-size: 13px !important; font-weight: 700 !important;
  letter-spacing: 0.6px !important; padding: 10px 12px !important;
}
/* Grand-total rows — color label matching the commodity */
tr.total-row.total-crude    td:first-child { color: #f59e0b !important; }
tr.total-row.total-gasoline td:first-child { color: #ef4444 !important; }
tr.total-row.total-diesel   td:first-child { color: #facc15 !important; }
tr.total-row.total-jet      td:first-child { color: #22d3ee !important; }
/* Subtle tinted background + border for each total row to match the commodity */
tr.total-row.total-crude    td { background: rgba(245,158,11,0.06) !important; border-top: 1px solid rgba(245,158,11,0.35) !important; border-bottom: 1px solid rgba(245,158,11,0.35) !important; }
tr.total-row.total-gasoline td { background: rgba(239,68,68,0.06)  !important; border-top: 1px solid rgba(239,68,68,0.35)  !important; border-bottom: 1px solid rgba(239,68,68,0.35)  !important; }
tr.total-row.total-diesel   td { background: rgba(250,204,21,0.06) !important; border-top: 1px solid rgba(250,204,21,0.35) !important; border-bottom: 1px solid rgba(250,204,21,0.35) !important; }
tr.total-row.total-jet      td { background: rgba(34,211,238,0.06) !important; border-top: 1px solid rgba(34,211,238,0.35) !important; border-bottom: 1px solid rgba(34,211,238,0.35) !important; }
/* Regional table layout — bring labels and values close together, cap table width */
.section table { table-layout: fixed; max-width: 960px; }
.section table th, .section table td { white-space: nowrap; padding: 6px 14px !important; }
.section table th:nth-child(1), .section table td:nth-child(1) { width: 42%; text-align: left; }
.section table th:nth-child(2), .section table td:nth-child(2) { width: 18%; text-align: right; }
.section table th:nth-child(3), .section table td:nth-child(3) { width: 18%; text-align: right; }
.section table th:nth-child(4), .section table td:nth-child(4) { width: 22%; text-align: right; }
"""

    # CSS additions
    mini_css = """
.mini-commodity-header {
  font-size: 20px; font-weight: 700; color: var(--cm-color);
  letter-spacing: -0.3px;
  margin: 22px 0 12px; padding: 6px 0 8px;
  border-bottom: 2px solid var(--cm-color);
  display: flex; align-items: center; gap: 10px;
}
.mini-commodity-header::before {
  content: ""; display: inline-block; width: 14px; height: 14px;
  border-radius: 3px; background: var(--cm-color);
  box-shadow: 0 0 12px color-mix(in srgb, var(--cm-color) 60%, transparent);
}
.mini-commodity-header:first-child { margin-top: 0; }
.mini-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(440px, 1fr));
  gap: 14px; margin-bottom: 18px;
}
.mini-card {
  background: var(--panel-2);
  border: 1px solid color-mix(in srgb, var(--cm-color) 30%, var(--border-soft));
  border-top: 3px solid var(--cm-color);
  border-radius: 8px; padding: 14px 16px;
  display: flex; flex-direction: column; gap: 10px;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.mini-card:hover {
  border-color: color-mix(in srgb, var(--cm-color) 55%, var(--border-soft));
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--cm-color) 30%, transparent),
              0 4px 14px color-mix(in srgb, var(--cm-color) 12%, transparent);
}
.mini-title {
  font-size: 14px; font-weight: 600; color: var(--text);
  letter-spacing: -0.1px;
}
.mini-canvas-wrap { position: relative; height: 220px; }
.mini-narrative {
  font-size: 12px; color: var(--muted-2); line-height: 1.5;
  border-top: 1px solid var(--border-soft); padding-top: 9px;
}
.mini-narrative strong { color: var(--text); font-weight: 600; }
@media (max-width: 1100px) { .mini-grid { grid-template-columns: 1fr 1fr; } }
@media (max-width: 700px)  { .mini-grid { grid-template-columns: 1fr; } }
"""

    # JS to render every mini-chart on page load
    mini_js = """
// ============================================================
// MINI SEASONAL CHARTS
// ============================================================
(function renderMiniCharts(){
  const canvases = document.querySelectorAll('canvas[data-mini]');
  canvases.forEach(canvas => {
    const id = canvas.dataset.mini;
    const [commodity, region] = [id.split('_')[0], id.split('_').slice(1).join('_')];
    if (!HIST[commodity] || !HIST[commodity][region]) return;
    const block = HIST[commodity][region];
    const weeks = WEEKS;
    const cur = block[String(new Date().getFullYear())] || block['2026'] || [];
    const prior = block[String(new Date().getFullYear() - 1)] || block['2025'] || [];
    const band = buildBand(block);

    new Chart(canvas.getContext('2d'), {
      type: 'line',
      data: {
        labels: weeks,
        datasets: [
          { label: '5Y High', data: band.high, borderColor: 'transparent', backgroundColor: 'rgba(140,150,170,0.18)', pointRadius: 0, fill: '+1', order: 4, tension: 0.3 },
          { label: '5Y Low',  data: band.low,  borderColor: 'transparent', backgroundColor: 'transparent', pointRadius: 0, fill: false, order: 3, tension: 0.3 },
          { label: 'Prior',   data: prior, borderColor: '#fbbf24', borderWidth: 1.3, pointRadius: 0, fill: false, order: 1, tension: 0.3 },
          { label: 'Current', data: cur, borderColor: '#ffffff', borderWidth: 1.8, pointRadius: 0, fill: false, order: 0, tension: 0.3 },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        animation: false, interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            enabled: true,
            backgroundColor: 'rgba(15,17,23,0.96)',
            titleColor: '#ffffff',
            bodyColor: '#d1d5db',
            borderColor: 'rgba(140,150,170,0.4)',
            borderWidth: 1,
            padding: 8,
            titleFont: { size: 10, weight: '600' },
            bodyFont: { size: 10 },
            displayColors: false,
            callbacks: {
              title: (items) => 'Week ' + (items[0].dataIndex + 1),
              label: (item) => {
                if (item.parsed.y == null) return null;
                const lbl = item.dataset.label;
                const val = Number(item.parsed.y).toFixed(1);
                return ' ' + lbl + ': ' + val + ' mb';
              },
            },
          },
        },
        scales: {
          x: { display: false },
          y: {
            display: true, ticks: { font: { size: 9 }, color: '#666', maxTicksLimit: 3 },
            grid: { color: 'rgba(255,255,255,0.04)' },
          },
        },
        elements: {
          line: { borderJoinStyle: 'round' },
          point: { radius: 0, hoverRadius: 4, hitRadius: 12 },
        },
      },
    });
  });
})();
"""

    # Inject HTML into output (after Market Read div, before "REGIONAL TABLE" comment)
    new_html = re.sub(
        r'(    <div class="padd-grid" id="padd-grid"></div>\s*\n  </div>)\s*\n\s*(<!-- REGIONAL TABLE)',
        r'\1\n\n  ' + mini_section_block.replace('\n', '\n  ') + '\n\n  \\2',
        new_html, count=1
    )

    # ── Remove the refinery utilization side-panel + make Regional Stocks full-width
    new_html = re.sub(
        r'<!-- REGIONAL TABLE \+ REFINERY UTIL -->\s*\n\s*<div class="row-2col">([\s\S]+?)</div>\s*\n\s*</div>\s*\n',
        r'<!-- REGIONAL TABLE -->\n  <div>\1</div>\n',
        new_html, count=1
    )
    # Patch the regional-render JS so each header row gets a commodity class for coloring
    new_html = new_html.replace(
        'hdr.className = "region-header";',
        'hdr.className = "region-header" + (group.commodity ? " region-" + group.commodity : "");',
        1
    )
    # Same for the grand-total row at the bottom of each commodity group
    new_html = new_html.replace(
        'totalRow.className = "total-row";',
        'totalRow.className = "total-row" + (group.commodity ? " total-" + group.commodity : "");',
        1
    )
    # Now that the refinery-util container is removed, guard the JS so it doesn't crash
    new_html = new_html.replace(
        'const refContainer = document.getElementById("refinery-rows");',
        'const refContainer = document.getElementById("refinery-rows"); if (refContainer) {',
        1
    )
    # Close the if-block — find the corresponding section and close brace
    new_html = re.sub(
        r'(const refContainer = document\.getElementById\("refinery-rows"\); if \(refContainer\) \{[\s\S]+?refContainer\.appendChild\(div\);\s*\}\);)',
        r'\1\n}',
        new_html, count=1
    )
    # Strip the entire Refinery Utilization sub-section
    new_html = re.sub(
        r'<div class="section">\s*<div class="section-header">\s*<h2 class="section-title">Refinery Utilization[\s\S]+?</div>\s*</div>',
        '',
        new_html, count=1
    )

    # ── Add SPR pill button to the main chart's region selector (right after Cushing)
    new_html = new_html.replace(
        '<button class="pill-btn" data-region="cushing" id="cushing-pill">Cushing, OK</button>',
        '<button class="pill-btn" data-region="cushing" id="cushing-pill">Cushing, OK</button>\n'
        '      <button class="pill-btn" data-region="spr" id="spr-pill">SPR</button>',
        1
    )
    # Add SPR to the REGION_NAMES JS map for AI inference + chart titles
    new_html = new_html.replace(
        'cushing: "Cushing, OK"',
        'cushing: "Cushing, OK",\n  spr: "Strategic Petroleum Reserve"',
        1
    )
    # Hide the SPR pill except for crude+stocks (mirroring the Cushing visibility rule)
    new_html = new_html.replace(
        'document.getElementById("cushing-pill").style.display =\n'
        '    (currentCommodity === "crude" && currentMetric === "stocks") ? "" : "none";',
        'document.getElementById("cushing-pill").style.display =\n'
        '    (currentCommodity === "crude" && currentMetric === "stocks") ? "" : "none";\n'
        '  document.getElementById("spr-pill").style.display =\n'
        '    (currentCommodity === "crude" && currentMetric === "stocks") ? "" : "none";',
        1
    )

    # ── Add INTERACTIVE badge to the Seasonal Bands header
    new_html = new_html.replace(
        '<h2 class="section-title">Seasonal Bands <span class="section-subtitle">5-year high/low envelope · 2025 + 2026 YTD overlay</span></h2>',
        '<h2 class="section-title">Seasonal Bands <span class="badge badge-interactive">▶ INTERACTIVE</span> '
        '<span class="section-subtitle">click any commodity, metric, or region pill to update the chart</span></h2>',
        1
    )

    # ── Simplify the Market Read header (no AI label / Claude reference)
    new_html = new_html.replace(
        '<h2 class="section-title">Market Read <span class="section-subtitle">summary + PADD-level trends, generated weekly with new EIA data</span></h2>',
        '<h2 class="section-title">AI Inventory Brief</h2>',
        1
    )
    # ── Strip the dead "Regenerate AI Summary" Cowork-only JS block (references Claude in dead code)
    new_html = re.sub(
        r'// =+\s*\n// REGENERATE AI SUMMARY\s*\n// =+\s*\n[\s\S]+?\}\);\s*\n',
        '',
        new_html, count=1
    )
    # Also hide the ↻ Regenerate button itself (it had no working backend on Netlify anyway)
    new_html = new_html.replace(
        '<button class="ai-button" id="ai-summary-btn">↻ Regenerate</button>',
        '',
        1
    )
    # Inject CSS just before </style>
    new_html = new_html.replace('</style>', kpi_redesign_css + extra_css + mini_css + INVENTORY_BRIEF_OVERRIDES_CSS + '\n</style>', 1)
    # Append JS just before the very last </script>
    last_script_close = new_html.rfind('</script>')
    if last_script_close > 0:
        new_html = new_html[:last_script_close] + kpi_redesign_js + mini_js + new_html[last_script_close:]

    # Replace the simplistic monthLabel() function with a calendar-accurate one
    # that places the month label at the first week of each month for `current_year`.
    new_month_fn = (
        'function monthLabel(i){\n'
        f'  const yearStart = new Date(Date.UTC({current_year}, 0, 1));\n'
        '  // Find Monday of ISO week 1\n'
        '  const dow = yearStart.getUTCDay() || 7;\n'
        '  const wk1Mon = new Date(yearStart.getTime() - (dow - 1) * 86400000);\n'
        '  if (dow > 4) wk1Mon.setUTCDate(wk1Mon.getUTCDate() + 7);\n'
        '  const monday = new Date(wk1Mon.getTime() + i * 7 * 86400000);\n'
        '  const month = monday.getUTCMonth();\n'
        '  if (i === 0) return MONTHS[month];\n'
        '  const prev = new Date(wk1Mon.getTime() + (i - 1) * 7 * 86400000);\n'
        '  return month !== prev.getUTCMonth() ? MONTHS[month] : "";\n'
        '}'
    )
    new_html = re.sub(
        r'function monthLabel\(i\)\{\s*\n\s*const idx = Math\.floor\(i / 52 \* 12\);\s*\n\s*return \(i % 4 === 0\) \? MONTHS\[idx\] : "";\s*\n\}',
        new_month_fn,
        new_html, count=1
    )

    # Inject head improvements (CSP, meta description, theme-color, font preconnect)
    new_html = new_html.replace(
        '<meta charset="UTF-8">',
        '<meta charset="UTF-8">\n'
        '<meta name="description" content="EIA Weekly Petroleum Status Report — crude, gasoline, distillate, and jet fuel stocks with PADD-level breakdowns and 5-year seasonal bands.">\n'
        '<meta name="theme-color" content="#07090d">\n'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'self\'; script-src \'self\' \'unsafe-inline\' https://cdn.jsdelivr.net; style-src \'self\' \'unsafe-inline\' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src \'self\' data:; connect-src \'self\';">\n'
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        + _FAVICON + '\n'
        + _og_tags('Inventories · MOB', 'EIA Weekly Petroleum Status Report — crude, gasoline, distillate, and jet fuel stocks with PADD-level breakdowns and 5-year seasonal bands.', 'inventory'),
        1
    )
    # Add SRI hash to Chart.js if present
    new_html = new_html.replace(
        'src="https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.js">',
        'src="https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.js" integrity="sha384-iU8HYtnGQ8Cy4zl7gbNMOhsDTTKX02BTXptVP/vqAWIaTfM7isw76iyZCsjL2eVi" crossorigin="anonymous">'
    )
    # Add ARIA to nav and table
    new_html = new_html.replace('<nav class="top-nav">', '<nav class="top-nav" aria-label="Main navigation">', 1)
    new_html = new_html.replace(
        '<th>Region</th><th>Last</th><th>Prev</th><th>Δ W/W</th>',
        '<th scope="col">Region</th><th scope="col">Last (mb)</th><th scope="col">Prev (mb)</th><th scope="col">Δ W/W (mb)</th>'
    )

    # Inject shared nav (active = inventory) into the inventory page just after <body>
    nav_html = _render_nav('inventory.html')
    new_html = new_html.replace('<body>', f'<body>\n{nav_html}', 1)
    # Inject nav CSS
    new_html = new_html.replace('</style>', NAV_CSS + '\n</style>', 1)
    # Inject signOut function before </body>
    new_html = new_html.replace('</body>', f'<script>{_SIGNOUT_JS}</script>\n</body>', 1)

    with open(OUT_INVENTORY, 'w') as f:
        f.write(new_html)
    print(f'Wrote {OUT_INVENTORY}  ({os.path.getsize(OUT_INVENTORY):,} bytes)')

    # ─── Load prices data (shared by landing + margins pages) ──────────────
    prices = None
    if os.path.exists(PRICES_FILE):
        try:
            with open(PRICES_FILE) as f:
                prices = json.load(f)
        except Exception as e:
            print(f'  could not load prices_data.json: {e}')

    # ─── Load news data (for morning brief) ───────────────────────────────
    news_items = []
    news_path = os.path.join(HERE, 'news_data.json')
    if os.path.exists(news_path):
        try:
            with open(news_path) as f:
                news_data = json.load(f)
            news_items = news_data.get('items', [])
            print(f'  loaded {len(news_items)} news headlines')
        except Exception as e:
            print(f'  could not load news_data.json: {e}')

    # ─── Build the landing page ────────────────────────────────────────────
    landing_html = _build_landing_page(raw, kpi_data, narratives, latest_date, prices, news_items)
    with open(OUT_LANDING, 'w') as f:
        f.write(landing_html)
    print(f'Wrote {OUT_LANDING}  ({os.path.getsize(OUT_LANDING):,} bytes)')

    # ─── Build the margins page ────────────────────────────────────────────
    if prices:
        try:
            margins_html = _build_margins_page(prices, latest_date, raw)
            with open(OUT_MARGINS, 'w') as f:
                f.write(margins_html)
            print(f'Wrote {OUT_MARGINS}  ({os.path.getsize(OUT_MARGINS):,} bytes)')
        except Exception as e:
            print(f'  margins page build failed: {e}')
    else:
        print(f'  (skipped margins page — run refresh_prices.py first)')

    # ─── Build the curves page ─────────────────────────────────────────────
    if prices:
        try:
            curves_html = _build_curves_page(prices, latest_date)
            with open(OUT_CURVES, 'w') as f:
                f.write(curves_html)
            print(f'Wrote {OUT_CURVES}  ({os.path.getsize(OUT_CURVES):,} bytes)')
        except Exception as e:
            print(f'  curves page build failed: {e}')
    else:
        print(f'  (skipped curves page — run refresh_prices.py first)')

    # ─── Build the news page ───────────────────────────────────────────────
    if news_items:
        try:
            refreshed_str = _utcnow().strftime('%b %-d, %Y %H:%M UTC')
            news_html = _build_news_page(news_items, refreshed_str)
            with open(OUT_NEWS, 'w') as f:
                f.write(news_html)
            print(f'Wrote {OUT_NEWS}  ({os.path.getsize(OUT_NEWS):,} bytes)')
        except Exception as e:
            print(f'  news page build failed: {e}')
    else:
        print(f'  (skipped news page — run refresh_news.py first)')

    print(f'  Latest data: {latest_date_str}')
    print(f'  KPIs:')
    for k in kpi_data:
        print(f'    {k["label"]:14} {k["last"]:>8.2f}  Δ {k["change"]:+.2f}')


if __name__ == '__main__':
    main()
