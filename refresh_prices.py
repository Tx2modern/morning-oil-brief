#!/usr/bin/env python3
"""
Daily price + forward curve fetcher for the EIA Dashboard.

Pulls:
  - EIA daily spot prices: WTI, Brent, RBOB NY Harbor, ULSD NY Harbor, jet Gulf
  - NYMEX/ICE forward curves via yfinance: CL, BZ, RB, HO (12-month strip)
  - Computes 3-2-1 crack, gasoline crack, distillate crack, WTI–Brent spread

Writes to prices_data.json next to dashboard. Run daily after NYMEX close
(roughly 6:00 PM ET).
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, date

try:
    import yfinance as yf
except ImportError:
    print("yfinance missing. Install with: pip install yfinance --break-system-packages", file=sys.stderr)
    sys.exit(1)

API_KEY = os.environ.get('EIA_API_KEY', 'gqRJqWOOf5Gf7178xVHyjNMsComMl2yRcxlgdgwI')
BASE = 'https://api.eia.gov/v2/seriesid/'
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, 'prices_data.json')

# Daily EIA price series (all in $/bbl or $/gal as published)
EIA_SERIES = {
    'wti_spot':    ('PET.RWTC.D',                              'WTI Cushing Spot', '$/bbl'),
    'brent_spot':  ('PET.RBRTE.D',                             'Brent Europe Spot', '$/bbl'),
    'rbob_ny':     ('PET.EER_EPMRU_PF4_Y35NY_DPG.D',           'RBOB NY Harbor Wholesale', '$/gal'),
    'ulsd_ny':     ('PET.EER_EPD2DXL0_PF4_Y35NY_DPG.D',        'ULSD NY Harbor Wholesale', '$/gal'),
    'jet_gulf':    ('PET.EER_EPJK_PF4_RGC_DPG.D',              'Jet Fuel Gulf Coast Wholesale', '$/gal'),
    'propane_mb':  ('PET.EER_EPLLPA_PF4_Y44MB_DPG.D',          'Propane Mont Belvieu', '$/gal'),
}

# NYMEX/NYSE month codes for futures contracts
MONTH_CODES = {1:'F', 2:'G', 3:'H', 4:'J', 5:'K', 6:'M', 7:'N', 8:'Q', 9:'U', 10:'V', 11:'X', 12:'Z'}

# yfinance ticker prefixes — the front-month continuous contract uses =F
# Specific months use {prefix}{MonthCode}{YearLast2}.NYM, e.g. CLM26.NYM
FUTURES = {
    'wti':   {'prefix': 'CL', 'name': 'WTI Crude (NYMEX)',  'units': '$/bbl', 'months': 12},
    'brent': {'prefix': 'BZ', 'name': 'Brent Crude (ICE)',  'units': '$/bbl', 'months': 12},
    'rbob':  {'prefix': 'RB', 'name': 'RBOB Gasoline',      'units': '$/gal', 'months': 12},
    'ulsd':  {'prefix': 'HO', 'name': 'NY Harbor ULSD',     'units': '$/gal', 'months': 12},
}

# Calendar-based expiry: each commodity's contract for delivery month M expires
# on approximately the last business day of month (M - EXPIRY_MONTHS_BEFORE).
# We use the 1st of that month as a conservative expiry trigger — if today is
# on or after that date, the contract has rolled and should be trimmed.
#   ICE Brent (BZ): expires last business day of M-2  → offset = 2
#   NYMEX CL/RB/HO: expires around the 20th of M-1   → offset = 1
EXPIRY_MONTHS_BEFORE = {
    'brent': 2,
    'wti':   1,
    'rbob':  1,
    'ulsd':  1,
}


def fetch_eia_series(series_id, length=1825):
    """Pull last `length` daily observations for a single EIA series."""
    url = (
        BASE + urllib.parse.quote(series_id, safe='.')
        + f'?api_key={API_KEY}&frequency=daily&data[]=value'
        + f'&sort[0][column]=period&sort[0][direction]=desc&length={length}'
    )
    req = urllib.request.Request(url, headers={'User-Agent': 'eia-dashboard-prices/1.0'})
    with urllib.request.urlopen(req, timeout=15) as r:
        doc = json.loads(r.read())
    rows = doc.get('response', {}).get('data', [])
    cleaned = []
    for row in rows:
        try:
            cleaned.append([row['period'], float(row['value'])])
        except (KeyError, TypeError, ValueError):
            continue
    cleaned.sort(key=lambda x: x[0])
    return cleaned


def fetch_eia_prices():
    """Pull all configured daily price series."""
    out = {}
    for key, (sid, name, units) in EIA_SERIES.items():
        try:
            data = fetch_eia_series(sid)
            out[key] = {'series_id': sid, 'name': name, 'units': units, 'data': data}
            latest = data[-1] if data else ('—', None)
            print(f'  EIA  {key:14} {name[:36]:36} latest {latest[0]} = {latest[1]}')
        except Exception as e:
            print(f'  EIA  {key:14} FAIL: {e}', file=sys.stderr)
    return out


def contract_ticker(prefix, month, year):
    """e.g. CL + 7 + 2026 → CLN26.NYM (NYMEX July 2026 WTI)."""
    code = MONTH_CODES[month]
    yr2 = str(year)[-2:]
    return f'{prefix}{code}{yr2}.NYM'


def fetch_futures_curve():
    """Pull front-month + N forward contracts for each commodity via yfinance."""
    today = datetime.utcnow()
    # Build the contract calendar starting from next month (current month usually expiring/expired)
    contracts = []
    y, m = today.year, today.month + 1
    if m > 12: m -= 12; y += 1
    for i in range(12):
        contracts.append((y, m))
        m += 1
        if m > 12: m = 1; y += 1

    # Sanity-check thresholds — surface as warnings so the build still proceeds.
    MIN_CURVE_POINTS = 10        # alert if fewer than 10 of 12 contracts returned data
    MAX_FRONT_STALE_DAYS = 3     # alert if continuous front-month is older than this many calendar days
    warnings = []

    out = {}
    for key, info in FUTURES.items():
        print(f'  Futures {key:6} curve via yfinance...')
        # Continuous front-month (rolls automatically) — easier baseline
        try:
            front = yf.Ticker(f"{info['prefix']}=F")
            hist = front.history(period='5y', auto_adjust=False)
            history = [
                [d.strftime('%Y-%m-%d'), round(float(row['Close']), 4)]
                for d, row in hist.iterrows() if row['Close'] == row['Close']  # NaN guard
            ]
        except Exception as e:
            print(f'    front month FAIL: {e}', file=sys.stderr)
            history = []

        # Forward curve: query each specific contract with a year of history
        curve = []
        for y, m in contracts[: info['months']]:
            tkr = contract_ticker(info['prefix'], m, y)
            try:
                t = yf.Ticker(tkr)
                h = t.history(period='1y', auto_adjust=False)
                closes = h['Close'].dropna() if len(h) > 0 else None
                if closes is None or len(closes) == 0:
                    continue
                today = float(closes.iloc[-1])
                day_ago = float(closes.iloc[-2])  if len(closes) >=  2 else None
                wk_ago  = float(closes.iloc[-6])  if len(closes) >=  6 else None
                mo_ago  = float(closes.iloc[-22]) if len(closes) >= 22 else None
                yr_ago  = float(closes.iloc[0])   if len(closes) >= 200 else None
                curve.append({
                    'contract': f'{y}-{m:02d}',
                    'ticker':   tkr,
                    'price':    round(today, 4),
                    'price_1d': round(day_ago, 4) if day_ago is not None else None,
                    'price_1w': round(wk_ago, 4)  if wk_ago  is not None else None,
                    'price_1m': round(mo_ago, 4)  if mo_ago  is not None else None,
                    'price_1y': round(yr_ago, 4)  if yr_ago  is not None else None,
                })
            except Exception as e:
                print(f'    {tkr} FAIL: {e}', file=sys.stderr)

        latest_front = history[-1] if history else None

        # Trim expired contracts from the front of the curve.
        offset = EXPIRY_MONTHS_BEFORE.get(key, 1)
        today_date = datetime.utcnow().date()
        n_trim = 0
        while len(curve) >= 2:
            cy, cm = map(int, curve[0]['contract'].split('-'))
            em, ey = cm - offset + 1, cy
            while em <= 0:
                em += 12; ey -= 1
            expiry_trigger = date(ey, em, 1)
            if today_date >= expiry_trigger:
                n_trim += 1
                curve = curve[1:]
            else:
                break
        # Secondary: price-based check in case the calendar is borderline
        if not n_trim and history and len(curve) >= 2:
            continuous_price = history[-1][1]
            best_idx = 0
            best_diff = abs(curve[0]['price'] - continuous_price)
            for i in range(1, min(3, len(curve))):
                diff = abs(curve[i]['price'] - continuous_price)
                if diff < best_diff:
                    best_diff = diff
                    best_idx = i
            if best_idx > 0:
                n_trim = best_idx
                curve = curve[best_idx:]
        if n_trim:
            print(f'    {key}: trimmed {n_trim} expired contract(s) — M1 now {curve[0]["contract"]}')

        print(f'    {key}: front={latest_front}, curve_pts={len(curve)}, M1={curve[0]["contract"] if curve else "n/a"}')

        # Sanity check 1: curve point count
        if len(curve) < MIN_CURVE_POINTS:
            msg = f'{key.upper()}: only {len(curve)}/{info["months"]} curve points returned (threshold {MIN_CURVE_POINTS}) — likely Yahoo delisted a contract'
            warnings.append(msg)
            print(f'    WARN  {msg}', file=sys.stderr)

        # Sanity check 2: continuous front-month staleness
        if latest_front:
            try:
                front_dt = datetime.strptime(latest_front[0], '%Y-%m-%d')
                stale_days = (today - front_dt).days
                if stale_days > MAX_FRONT_STALE_DAYS:
                    msg = f'{key.upper()}: continuous front-month last close is {stale_days} days old ({latest_front[0]}) — yfinance may be stale'
                    warnings.append(msg)
                    print(f'    WARN  {msg}', file=sys.stderr)
            except (ValueError, TypeError):
                pass
        else:
            msg = f'{key.upper()}: NO continuous front-month history returned — crack spreads will break'
            warnings.append(msg)
            print(f'    WARN  {msg}', file=sys.stderr)

        out[key] = {
            'name': info['name'],
            'units': info['units'],
            'front_history': history,  # full 5-yr daily history — needed for seasonal bands
            'curve': curve,
        }

    return out, warnings


def compute_cracks(eia, futures):
    """Build daily crack-spread series from NYMEX/ICE futures front-month data
    so the dashboard tracks the previous trading day (no 2-3 day EIA spot lag).
    Jet crack still uses EIA spot since there's no liquid jet fuel futures.
    All output in $/bbl, aligned on common trading days."""

    def front_dict(commodity):
        """Front-month history as {date: price}. RBOB/ULSD converted gal→bbl (× 42)."""
        hist = (futures.get(commodity, {}) or {}).get('front_history', [])
        mult = 42.0 if commodity in ('rbob', 'ulsd') else 1.0
        return {d: v * mult for d, v in hist}

    wti   = front_dict('wti')
    brent = front_dict('brent')
    rbob  = front_dict('rbob')
    ulsd  = front_dict('ulsd')

    # 3-2-1 crack = (2 × RBOB + 1 × ULSD) / 3 − WTI, all in $/bbl
    common_321 = sorted(set(wti) & set(rbob) & set(ulsd))
    crack_321 = [[d, round((2 * rbob[d] + ulsd[d]) / 3 - wti[d], 2)] for d in common_321]

    common_gas = sorted(set(wti) & set(rbob))
    crack_gasoline = [[d, round(rbob[d] - wti[d], 2)] for d in common_gas]

    common_dist = sorted(set(wti) & set(ulsd))
    crack_distillate = [[d, round(ulsd[d] - wti[d], 2)] for d in common_dist]

    # Jet crack — fallback to EIA spot (jet futures aren't liquid enough to use)
    eia_wti = {d: v for d, v in eia.get('wti_spot', {}).get('data', [])}
    eia_jet = {d: v * 42 for d, v in eia.get('jet_gulf', {}).get('data', [])}
    common_jet = sorted(set(eia_wti) & set(eia_jet))
    crack_jet = [[d, round(eia_jet[d] - eia_wti[d], 2)] for d in common_jet]

    common_bw = sorted(set(brent) & set(wti))
    brent_wti_spread = [[d, round(brent[d] - wti[d], 2)] for d in common_bw]

    return {
        'crack_321':         {'name': '3-2-1 Crack (NYMEX futures)',   'units': '$/bbl', 'data': crack_321},
        'crack_gasoline':    {'name': 'Gasoline Crack (RBOB futures)', 'units': '$/bbl', 'data': crack_gasoline},
        'crack_distillate':  {'name': 'Distillate Crack (ULSD)',       'units': '$/bbl', 'data': crack_distillate},
        'crack_jet':         {'name': 'Jet Crack (EIA spot, lagged)',  'units': '$/bbl', 'data': crack_jet},
        'brent_wti_spread':  {'name': 'Brent–WTI Spread (futures)',    'units': '$/bbl', 'data': brent_wti_spread},
    }


def main():
    print('[1/3] Fetching EIA daily prices...')
    eia = fetch_eia_prices()

    print('[2/3] Fetching forward curves (yfinance)...')
    curves, curve_warnings = fetch_futures_curve()

    print('[3/3] Computing crack spreads (NYMEX/ICE futures-based)...')
    cracks = compute_cracks(eia, curves)

    payload = {
        'generated_at': datetime.utcnow().isoformat() + 'Z',
        'eia_spot': eia,
        'futures': curves,
        'cracks': cracks,
        'warnings': curve_warnings,
    }

    with open(OUT_PATH, 'w') as f:
        json.dump(payload, f, separators=(',', ':'))
    size = os.path.getsize(OUT_PATH)
    print(f'\nWrote {OUT_PATH} ({size:,} bytes)')

    # Quick summary of what's available
    print('\n=== SUMMARY ===')
    for k, v in eia.items():
        if v.get('data'):
            print(f"  EIA spot   {v['name']:36} {v['data'][-1]} {v['units']}")
    for k, v in curves.items():
        front = v['front_history'][-1] if v['front_history'] else None
        n_curve = len(v['curve'])
        print(f"  Futures    {v['name']:36} front {front}, curve points {n_curve}")
    for k, v in cracks.items():
        if v['data']:
            print(f"  Crack      {v['name']:36} latest {v['data'][-1]} {v['units']}")

    if curve_warnings:
        print('\n=== WARNINGS ===')
        for w in curve_warnings:
            print(f'  ! {w}')
    else:
        print('\nAll curves passed sanity checks.')


if __name__ == '__main__':
    main()
