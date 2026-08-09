#!/usr/bin/env python3
"""Currency Drift — weekly build.
Fetches ECB reference rates (frankfurter, H.10 fallback, or a local CSV),
optionally BIS REER and policy rates, scores ten currencies on trend, carry
and value from an SGD base, renders the Aurora dashboard, writes a
machine-readable snapshot, and sends the digest email plus WhatsApp alerts.
Stdlib only. Run modes:
  python build.py                      normal weekly run (network)
  python build.py --offline            no network; needs data/rates_daily.csv
  python build.py --no-send            build everything, send nothing
"""
import csv, io, json, math, os, re, smtplib, ssl, sys, urllib.parse, urllib.request
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

ROOT = os.path.dirname(os.path.abspath(__file__))
BUILD = "1.3"
OFFLINE = "--offline" in sys.argv
NO_SEND = "--no-send" in sys.argv or OFFLINE
CFG = json.load(open(os.path.join(ROOT, "config.json")))
CODES = [c["code"] for c in CFG["currencies"]]
BASE = CFG["base"]
NONBASE = [c for c in CODES if c != BASE]
PEGGED = {c["code"] for c in CFG["currencies"] if c.get("pegged")}
NAMES = {c["code"]: c["name"] for c in CFG["currencies"]}
BIS_AREA = {c["code"]: c["bis"] for c in CFG["currencies"]}

def log(msg):
    print(f"[drift] {msg}", flush=True)

def http_get(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": "currency-drift/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

# ---------------------------------------------------------------- rates
def rates_from_local():
    path = os.path.join(ROOT, "data", "rates_daily.csv")
    out = {}
    with open(path) as f:
        for row in csv.reader(f):
            d, ccy, v = row[0], row[1], float(row[2])
            out.setdefault(d, {})[ccy] = v
    log(f"rates: local csv, {len(out)} days")
    return out, "ECB (local snapshot)"

def rates_from_frankfurter(start_year):
    base_url = CFG["sources"]["frankfurter"]
    symbols = ",".join(NONBASE)
    out = {}
    this_year = date.today().year
    for y in range(start_year, this_year + 1):
        a, b = f"{y}-01-01", f"{y}-12-31"
        url = f"{base_url}/{a}..{b}?base={BASE}&symbols={symbols}"
        data = json.loads(http_get(url))
        for d, rr in data.get("rates", {}).items():
            # frankfurter returns CCY per 1 SGD; invert to SGD per 1 CCY
            out[d] = {c: 1.0 / v for c, v in rr.items() if v}
    log(f"rates: frankfurter, {len(out)} days")
    return out, "ECB"

def rates_from_h10():
    txt = http_get(CFG["sources"]["h10_fallback"], timeout=90)
    name_map = {"Singapore": "SGD", "Euro": "EUR", "United Kingdom": "GBP",
                "Japan": "JPY", "Switzerland": "CHF", "China": "CNY",
                "Hong Kong": "HKD", "Malaysia": "MYR", "South Africa": "ZAR"}
    usd = {}
    for row in csv.reader(io.StringIO(txt)):
        if len(row) < 3 or row[0] == "Date":
            continue
        ccy = name_map.get(row[1])
        if not ccy:
            continue
        try:
            v = float(row[2])
        except ValueError:
            continue
        # H.10 quotes: EUR and GBP are USD per unit; others units per USD
        usd_per_unit = v if ccy in ("EUR", "GBP") else (1.0 / v if v else None)
        if usd_per_unit:
            usd.setdefault(row[0], {})[ccy] = usd_per_unit
    out = {}
    for d, rr in usd.items():
        if "SGD" not in rr:
            continue
        sgd_per_usd = 1.0 / rr["SGD"]
        day = {"USD": sgd_per_usd}
        for c, upu in rr.items():
            if c != "SGD":
                day[c] = upu * sgd_per_usd
        out[d] = day
    log(f"rates: H.10 fallback, {len(out)} days")
    return out, "Fed H.10"

def load_daily_rates():
    need_from = date.today().year - 12
    if OFFLINE:
        return rates_from_local()
    try:
        return rates_from_frankfurter(need_from)
    except Exception as e:
        log(f"frankfurter failed ({e}); trying H.10")
    try:
        return rates_from_h10()
    except Exception as e:
        log(f"H.10 failed ({e}); trying local csv")
    return rates_from_local()

def weekly_closes(daily):
    """Return ordered list of (friday_date, {ccy: sgd_per_ccy}) using the last
    observation on or before each Friday. Weeks missing any currency are
    dropped."""
    days = sorted(daily)
    if not days:
        raise SystemExit("no rate data")
    first = datetime.strptime(days[0], "%Y-%m-%d").date()
    last = datetime.strptime(days[-1], "%Y-%m-%d").date()
    fri = first + timedelta(days=(4 - first.weekday()) % 7)
    lookup = {d: daily[d] for d in days}
    weeks = []
    while fri <= last + timedelta(days=6):
        obs = None
        for back in range(0, 5):
            d = (fri - timedelta(days=back)).isoformat()
            if d in lookup:
                obs = (d, lookup[d])
                break
        if obs and all(c in obs[1] for c in NONBASE) and fri <= last + timedelta(days=2):
            weeks.append((fri.isoformat(), obs[1], obs[0]))
        fri += timedelta(days=7)
    return weeks

# ---------------------------------------------------------------- lenses
def basket_logs(week_rates):
    """log basket-relative index per currency (SGD included at x=1)."""
    lx = {c: math.log(week_rates[c]) for c in NONBASE}
    lx[BASE] = 0.0
    mean = sum(lx.values()) / len(lx)
    return {c: lx[c] - mean for c in CODES}

def zscores(raw):
    vals = [v for v in raw.values() if v is not None]
    m = sum(vals) / len(vals)
    var = sum((v - m) ** 2 for v in vals) / len(vals)
    sd = math.sqrt(var) or 1e-9
    return {c: ((v - m) / sd if v is not None else None) for c, v in raw.items()}

def lens_score(z):
    return {c: (None if v is None else max(0.0, min(10.0, 5 + 2.5 * v))) for c, v in z.items()}

def parse_sdmx_csv(txt, code_field_hint):
    """Header-driven SDMX-CSV parser tolerant of v1 and v2 layouts.
    Returns {area_code: {period: value}}."""
    rdr = csv.reader(io.StringIO(txt))
    try:
        header = next(rdr)
    except StopIteration:
        return {}
    up = [h.strip().upper().replace('"', "") for h in header]
    def col(*names):
        for n in names:
            if n in up:
                return up.index(n)
        return None
    i_val = col("OBS_VALUE", "VALUE")
    i_per = col("TIME_PERIOD", "TIME", "PERIOD")
    i_area = col("REF_AREA", "REFERENCE_AREA", "AREA")
    i_key = col("KEY", "SERIES_KEY", "TIMESERIES", "STRUCTURE_ID")
    if i_val is None or i_per is None:
        return {}
    out = {}
    for row in rdr:
        if len(row) <= i_val:
            continue
        try:
            val = float(row[i_val])
        except ValueError:
            continue
        area = None
        if i_area is not None and len(row) > i_area and re.fullmatch(r"[A-Z0-9]{2}", row[i_area].strip()):
            area = row[i_area].strip()
        elif i_key is not None and len(row) > i_key:
            m = re.search(code_field_hint, row[i_key])
            if m:
                area = m.group(1)
        if area:
            out.setdefault(area, {})[row[i_per].strip()] = val
    return out

def bis_try_urls(urls, code_field_hint, min_areas):
    last_err = None
    for url in urls:
        try:
            txt = http_get(url, timeout=60)
            series = parse_sdmx_csv(txt, code_field_hint)
            if len(series) >= min_areas:
                log(f"bis: hit {url.split('/api/')[1][:60]}")
                return series
            last_err = f"parsed {len(series)} areas"
        except Exception as e:
            last_err = str(e)[:80]
    raise RuntimeError(last_err or "no url worked")

def fetch_bis_reer():
    codes = "+".join(BIS_AREA[c] for c in CODES)
    start = f"{date.today().year - 11}-01"
    urls = [
        f"https://stats.bis.org/api/v2/data/dataflow/BIS/WS_EER/1.0/M.R.B.{codes}?format=csv&startPeriod={start}",
        f"https://stats.bis.org/api/v2/data/dataflow/BIS/WS_EER_M/1.0/M.R.B.{codes}?format=csv&startPeriod={start}",
        f"https://stats.bis.org/api/v1/data/WS_EER_M/M.R.B.{codes}/all?format=csv&startPeriod={start}",
        f"https://stats.bis.org/api/v1/data/WS_EER/M.R.B.{codes}/all?format=csv&startPeriod={start}",
    ]
    series = bis_try_urls(urls, r"M\.R\.B\.([A-Z0-9]{2})", len(CODES) - 1)
    area_to_code = {v: k for k, v in BIS_AREA.items()}
    out = {}
    for area, obs in series.items():
        code = area_to_code.get(area)
        if not code or len(obs) < 60:
            continue
        periods = sorted(obs)
        latest = obs[periods[-1]]
        last10 = [obs[p] for p in periods[-120:]]
        out[code] = -math.log(latest / (sum(last10) / len(last10)))
    if len(out) < len(CODES) - 1:
        raise RuntimeError(f"REER incomplete ({len(out)}/{len(CODES)})")
    return out

def fetch_bis_cbpol():
    codes = "+".join(BIS_AREA[c] for c in CODES)
    start = f"{date.today().year - 1}-01"
    urls = [
        f"https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.{codes}?format=csv&startPeriod={start}",
        f"https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL_D/1.0/D.{codes}?format=csv&startPeriod={start}",
        f"https://stats.bis.org/api/v1/data/WS_CBPOL_D/D.{codes}/all?format=csv&startPeriod={start}",
        f"https://stats.bis.org/api/v1/data/WS_CBPOL/D.{codes}/all?format=csv&startPeriod={start}",
    ]
    series = bis_try_urls(urls, r"D\.([A-Z0-9]{2})", 3)
    area_to_code = {v: k for k, v in BIS_AREA.items()}
    out = {}
    for area, obs in series.items():
        code = area_to_code.get(area)
        if code:
            out[code] = obs[sorted(obs)[-1]]
    if not out:
        raise RuntimeError("CBPOL parsed 0 areas")
    return out

def value_proxy(weeks, w_index, lookback):
    """-(current basket log index minus its trailing mean)."""
    lo = max(0, w_index - lookback + 1)
    hist = [basket_logs(weeks[i][1]) for i in range(lo, w_index + 1)]
    cur = hist[-1]
    out = {}
    for c in CODES:
        mean = sum(h[c] for h in hist) / len(hist)
        out[c] = -(cur[c] - mean)
    return out

# ---------------------------------------------------------------- scoring
def score_week(weeks, w, carry_rates, value_raw_override=None):
    cfgw = CFG["weights"]
    tw, tb = CFG["trend_weeks"], CFG["trend_blend"]
    cur = basket_logs(weeks[w][1])
    trend_raw = {}
    for c in CODES:
        parts = []
        for k, wt in zip(tw, tb):
            if w - k >= 0:
                prev = basket_logs(weeks[w - k][1])
                parts.append(wt * (cur[c] - prev[c]))
        trend_raw[c] = sum(parts) if parts else 0.0
    value_raw = value_raw_override or value_proxy(weeks, w, CFG["value_lookback_weeks"])
    carry_raw = {c: carry_rates[c] - carry_rates[BASE] for c in CODES}
    zt, zv, zc = zscores(trend_raw), zscores(value_raw), zscores(carry_raw)
    st, sv, sc = lens_score(zt), lens_score(zv), lens_score(zc)
    scale = CFG["comp_display_scale"]
    rows = {}
    for c in CODES:
        if c in PEGGED:
            comp = zc[c] * scale
            rows[c] = {"trend": None, "carry": round(sc[c]), "value": None,
                       "comp": round(comp, 1)}
        else:
            comp = (cfgw["value"] * zv[c] + cfgw["trend"] * zt[c] + cfgw["carry"] * zc[c]) * scale
            rows[c] = {"trend": round(st[c]), "carry": round(sc[c]),
                       "value": round(sv[c]), "comp": round(comp, 1)}
    order = sorted(CODES, key=lambda c: -rows[c]["comp"])
    for i, c in enumerate(order, 1):
        rows[c]["rank"] = i
    prev_rates = weeks[w - 1][1] if w >= 1 else None
    for c in CODES:
        if c == BASE:
            rows[c]["wk"] = 0.0
        elif prev_rates:
            rows[c]["wk"] = round((weeks[w][1][c] / prev_rates[c] - 1) * 100, 2)
        else:
            rows[c]["wk"] = 0.0
    return rows

# ---------------------------------------------------------------- alerts
def build_alerts(cur, prev):
    if not prev:
        return []
    alerts = []
    band = CFG["alert_week_band_pct"]
    n = len(CODES)
    top_now = {c for c in CODES if cur[c]["rank"] <= 3}
    top_was = {c for c in CODES if prev[c]["rank"] <= 3}
    bot_now = {c for c in CODES if cur[c]["rank"] >= n - 2}
    bot_was = {c for c in CODES if prev[c]["rank"] >= n - 2}
    for c in sorted(top_now - top_was, key=lambda x: cur[x]["rank"]):
        alerts.append(f"{c} entered top 3")
    for c in sorted(top_was - top_now):
        alerts.append(f"{c} left top 3")
    for c in sorted(bot_now - bot_was, key=lambda x: -cur[x]["rank"]):
        alerts.append(f"{c} entered bottom 3")
    for c in sorted(bot_was - bot_now):
        alerts.append(f"{c} left bottom 3")
    for c in CODES:
        a, b = prev[c]["comp"], cur[c]["comp"]
        if a > 0 >= b:
            alerts.append(f"{c} flipped negative")
        elif a < 0 <= b:
            alerts.append(f"{c} flipped positive")
    for c in CODES:
        wk = cur[c]["wk"]
        if abs(wk) >= band:
            side = "-" if wk < 0 else "+"
            alerts.append(f"{c} broke {side}{band:.1f}% week band")
    return alerts

def churn(cur, prev):
    if not prev:
        return 0
    return sum(abs(cur[c]["rank"] - prev[c]["rank"]) for c in CODES)

def churn_band(v):
    b = CFG["churn_bands"]
    return "calm" if v <= b["calm"] else ("lively" if v <= b["lively"] else "volatile")

# ---------------------------------------------------------------- language
def tile_phrase(kind, c, row, carry_rates, alerts, dr):
    v, t = row.get("value"), row.get("trend")
    if kind == "increase":
        if v is not None and v >= 7 and t is not None and t >= 6:
            return "cheapest end of the basket, trend confirmed"
        if v is not None and v >= 7:
            return "cheap, waiting on the trend"
        if t is not None and t >= 7:
            return "momentum leader, no longer cheap"
        return "best blend of trend, carry and value"
    if kind == "reduce":
        if v is not None and v <= 3 and t is not None and t <= 4:
            return "rich and fading"
        if v is not None and v <= 3:
            return "expensive end of the basket"
        if t is not None and t <= 3:
            return "weakest trend in the basket"
        return "worst blend in the basket this week"
    band_hit = next((a for a in alerts if a.startswith(c) and "week band" in a), None)
    if band_hit:
        if "broke +" in band_hit:
            return f"jumped the week band \u2014 carry {carry_rates[c]:.0f}% on top"
        return f"paid {carry_rates[c]:.0f}% to wait, then broke the week band"
    if dr <= -2:
        return f"slid {abs(dr)} places this week"
    if dr >= 2:
        return f"climbed {dr} places this week"
    return "biggest mover of the week"

def wheel_phrase(kind, row):
    v, t = row.get("value"), row.get("trend")
    if kind == "top":
        if v is not None and v >= 7 and t is not None and t >= 6:
            return "cheap & climbing"
        if v is not None and v >= 7:
            return "cheap & stirring"
        return "leading on momentum"
    if v is not None and v <= 3 and t is not None and t <= 4:
        return "rich & fading"
    if v is not None and v <= 3:
        return "rich & wobbly"
    return "on the outs"

# ---------------------------------------------------------------- palettes
DARK = {"bg": "#0a0b0e", "panel": "#12151b", "panel2": "#171b22", "txt": "#eceef2",
        "muted": "#7d838f", "border": "rgba(255,255,255,0.09)", "line": "rgba(255,255,255,0.20)",
        "teal": "#33d6bd", "coral": "#ff6a5c", "amber": "#ffb84d", "grey": "#9aa0ab",
        "ghost": "#4c515b", "ring": "rgba(255,255,255,0.06)", "glow": "rgba(51,214,189,0.18)",
        "glow2": "rgba(255,106,92,.07)"}
LIGHT = {"bg": "#eeeae2", "panel": "#ffffff", "panel2": "#f5f2ec", "txt": "#191c21",
         "muted": "#6b7178", "border": "rgba(0,0,0,0.10)", "line": "rgba(0,0,0,0.20)",
         "teal": "#0e9a8a", "coral": "#df4536", "amber": "#b47614", "grey": "#8b909a",
         "ghost": "#c3bfb6", "ring": "rgba(0,0,0,0.06)", "glow": "rgba(14,154,138,0.16)",
         "glow2": "rgba(223,69,54,.06)"}

def val_color(v, pegged=False):
    if pegged or v is None:
        return "var(--ghost)"
    if v >= 7:
        return "var(--teal)"
    if v <= 3:
        return "var(--coral)"
    return "var(--grey)"

def sign(n, dec=1):
    if n > 0:
        return f"+{abs(n):.{dec}f}"
    if n < 0:
        return f"\u2212{abs(n):.{dec}f}"
    return f"{0:.{dec}f}"

# ---------------------------------------------------------------- svg bits
def polar(cx, cy, r, deg):
    a = math.radians(deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)

def arc_path(cx, cy, r, a0, a1):
    x0, y0 = polar(cx, cy, r, a0)
    x1, y1 = polar(cx, cy, r, a1)
    large = 1 if (a1 - a0) > 180 else 0
    return f"M {x0:.1f} {y0:.1f} A {r} {r} 0 {large} 1 {x1:.1f} {y1:.1f}"

def spark_svg(vals, week):
    W, H, pad = 66, 20, 3
    mn, mx = min(vals), max(vals)
    norm = [0.5 if mx == mn else (v - mn) / (mx - mn) for v in vals]
    pts = " ".join(f"{(i / (len(norm) - 1)) * (W - 2):.1f},{H - pad - v * (H - 2 * pad):.1f}"
                   for i, v in enumerate(norm))
    col = "var(--teal)" if week >= 0 else "var(--coral)"
    ly = H - pad - norm[-1] * (H - 2 * pad)
    return (f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" style="overflow:visible">'
            f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="1.5" '
            f'stroke-linejoin="round" stroke-linecap="round" opacity="0.9"/>'
            f'<circle cx="{W - 2}" cy="{ly:.1f}" r="1.9" fill="{col}"/></svg>')

def bar_svg(comp, comp_max):
    W, H, cx, half = 128, 15, 64, 56
    ln = min(abs(comp) / comp_max * half, half) if comp_max else 0
    x = cx if comp >= 0 else cx - ln
    col = "var(--teal)" if comp >= 0 else "var(--coral)"
    return (f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" style="display:block">'
            f'<line x1="{cx}" y1="1" x2="{cx}" y2="{H - 1}" stroke="var(--line)" stroke-width="1"/>'
            f'<rect x="{x:.1f}" y="3.5" width="{ln:.1f}" height="{H - 7}" rx="1.5" fill="{col}" opacity="0.92"/></svg>')

def wheel_svg(rows, order, churn_v, band, alert_ccys):
    S, cx, cy, rIn, rMax = 400, 200, 200, 82, 180
    comps = [rows[c]["comp"] for c in CODES]
    mn, mx = min(comps), max(comps)
    span = (mx - mn) or 1
    rad = lambda c: rIn + (c - mn) / span * (rMax - rIn)
    top = order[0]
    parts = ['<svg viewBox="0 0 400 400" width="100%" style="overflow:visible;display:block" '
             'role="img" aria-label="Currency wheel: spoke length is composite strength, dot size is carry, colour is valuation">']
    parts.append('<defs><radialGradient id="wglow"><stop offset="0%" stop-color="var(--glow)" stop-opacity="0.9"/>'
                 '<stop offset="68%" stop-color="var(--glow)" stop-opacity="0"/></radialGradient></defs>')
    parts.append(f'<circle cx="{cx}" cy="{cy}" r="172" fill="url(#wglow)" style="animation:glowPulse 6s ease-in-out infinite"/>')
    for t in (0.34, 0.67, 1.0):
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{rIn + t * (rMax - rIn):.1f}" fill="none" stroke="var(--ring)" stroke-width="1" stroke-dasharray="1 5"/>')
    if mn < 0 < mx:
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{rad(0):.1f}" fill="none" stroke="var(--muted)" stroke-width="1" stroke-opacity="0.32" stroke-dasharray="4 4"/>')
    labels = []
    for i, c in enumerate(order):
        row = rows[c]
        ang = -90 + i * (360 / len(order))
        r = rad(row["comp"])
        tx, ty = polar(cx, cy, r, ang)
        bx, by = polar(cx, cy, rIn, ang)
        pegged = c in PEGGED
        vc = val_color(row.get("value"), pegged)
        carry = row.get("carry")
        dotR = 3 + (4 if carry is None else carry) / 10 * 6.5
        if pegged:
            parts.append(f'<line x1="{bx:.1f}" y1="{by:.1f}" x2="{tx:.1f}" y2="{ty:.1f}" stroke="var(--ghost)" stroke-width="1.5" stroke-opacity="0.75" stroke-dasharray="3 4"/>')
            parts.append(f'<circle cx="{tx:.1f}" cy="{ty:.1f}" r="{dotR:.1f}" fill="none" stroke="var(--ghost)" stroke-width="1.5" stroke-dasharray="2 3"/>')
        else:
            sc_col = "var(--teal)" if row["comp"] >= 0 else "var(--coral)"
            parts.append(f'<line x1="{bx:.1f}" y1="{by:.1f}" x2="{tx:.1f}" y2="{ty:.1f}" stroke="{sc_col}" stroke-width="2.5" stroke-opacity="0.55" stroke-linecap="round"/>')
            parts.append(f'<circle cx="{tx:.1f}" cy="{ty:.1f}" r="{dotR + 3.5:.1f}" fill="none" stroke="{vc}" stroke-opacity="0.22" stroke-width="5"/>')
            pulse = ' style="animation:pulseDot 3s ease-in-out infinite"' if (c == top or c in alert_ccys) else ""
            parts.append(f'<circle cx="{tx:.1f}" cy="{ty:.1f}" r="{dotR:.1f}" fill="{vc}"{pulse}/>')
        lx, ly = polar(cx, cy, r + dotR + 14, ang)
        cosv = math.cos(math.radians(ang))
        anchor = "middle" if abs(cosv) <= 0.2 else ("start" if cosv > 0 else "end")
        labels.append({"c": c, "x": lx, "y": ly, "anchor": anchor, "rank": row["rank"]})
    for _ in range(4):  # collision nudge
        moved = False
        for i in range(len(labels)):
            for j in range(i + 1, len(labels)):
                a, b = labels[i], labels[j]
                if abs(a["x"] - b["x"]) < 40 and abs(a["y"] - b["y"]) < 22:
                    b["y"] += 18 if b["y"] >= a["y"] else -18
                    moved = True
        if not moved:
            break
    for L in labels:
        parts.append(f'<text x="{L["x"]:.1f}" y="{L["y"] - 1:.1f}" fill="var(--txt)" font-size="12.5" '
                     f'font-family="\'IBM Plex Mono\',monospace" font-weight="600" text-anchor="{L["anchor"]}" dominant-baseline="middle">{L["c"]}</text>')
        parts.append(f'<text x="{L["x"]:.1f}" y="{L["y"] + 11:.1f}" fill="var(--muted)" font-size="8.5" '
                     f'font-family="\'IBM Plex Mono\',monospace" text-anchor="{L["anchor"]}" dominant-baseline="middle">#{L["rank"]}</text>')
    # annotations: top and worst non-pegged
    worst = [c for c in reversed(order) if c not in PEGGED][0]
    for kind, c in (("top", top), ("bottom", worst)):
        row = rows[c]
        idx = order.index(c)
        ang = -90 + idx * (360 / len(order))
        px, py = polar(cx, cy, rad(row["comp"]), ang)
        col = "var(--teal)" if kind == "top" else "var(--coral)"
        dx, anchor = (14, "start") if math.cos(math.radians(ang)) >= 0 else (-14, "end")
        dy = -28 if math.sin(math.radians(ang)) < 0 else 26
        parts.append(f'<text x="{px + dx:.1f}" y="{py + dy:.1f}" fill="{col}" font-size="10.5" font-style="italic" '
                     f'font-family="\'Newsreader\',serif" text-anchor="{anchor}">{wheel_phrase(kind, row)}</text>')
    # drift meter core
    parts.append(f'<circle cx="{cx}" cy="{cy}" r="62" fill="var(--panel)"/>')
    parts.append(f'<path d="{arc_path(cx, cy, 54, -215, 35)}" stroke="var(--ring)" stroke-width="8" fill="none" stroke-linecap="round"/>')
    gv = max(0.0, min(1.0, churn_v / 30.0))
    gcol = {"calm": "var(--teal)", "lively": "var(--amber)", "volatile": "var(--coral)"}[band]
    if gv > 0.01:
        parts.append(f'<path d="{arc_path(cx, cy, 54, -215, -215 + gv * 250)}" stroke="{gcol}" stroke-width="8" fill="none" stroke-linecap="round"/>')
    parts.append(f'<text x="{cx}" y="{cy - 22}" fill="var(--muted)" font-size="8.5" letter-spacing="0.16em" '
                 f'font-family="\'IBM Plex Mono\',monospace" text-anchor="middle">DRIFT</text>')
    parts.append(f'<text x="{cx}" y="{cy + 10}" fill="var(--txt)" font-size="34" font-family="\'Newsreader\',serif" text-anchor="middle">{churn_v}</text>')
    parts.append(f'<text x="{cx}" y="{cy + 28}" fill="{gcol}" font-size="10" letter-spacing="0.1em" '
                 f'font-family="\'IBM Plex Mono\',monospace" text-anchor="middle">{band}</text>')
    parts.append(f'<text x="{cx}" y="{cy + 44}" fill="var(--muted)" font-size="8" '
                 f'font-family="\'IBM Plex Mono\',monospace" text-anchor="middle">base {BASE}</text>')
    parts.append("</svg>")
    return "".join(parts)

# ---------------------------------------------------------------- page
def render_page(state):
    rows, order = state["rows"], state["order"]
    d = state
    comp_max = max(0.1, max(abs(rows[c]["comp"]) for c in CODES))
    def cssvars(p):
        return ";".join(f"--{k}:{v}" for k, v in p.items())
    tiles = ""
    for kind, col, glyph in (("increase", "teal", "\u2191"), ("watch", "amber", "\u25c6"), ("reduce", "coral", "\u2193")):
        t = d["tiles"][kind]
        tiles += f'''<div class="tile" style="border-top:2px solid var(--{col})">
<div class="tile-head"><span style="color:var(--{col})">{kind}</span><span style="color:var(--{col})">{glyph}</span></div>
<div class="tile-ccy">{t["ccy"]}</div>
<div class="tile-why">{t["why"]}</div></div>'''
    trs = ""
    for c in order:
        r = rows[c]
        tag = "base" if c == BASE else ("peg" if c in PEGGED else "")
        dash = "\u2013"
        wkcol = "var(--teal)" if r["wk"] > 0 else ("var(--coral)" if r["wk"] < 0 else "var(--muted)")
        compcol = "var(--teal)" if r["comp"] > 0 else ("var(--coral)" if r["comp"] < 0 else "var(--muted)")
        vcol = val_color(r.get("value"), c in PEGGED)
        wkf = ("0.0%" if r["wk"] == 0 else sign(r["wk"]) + "%")
        trs += f'''<tr><td class="mut">{r["rank"]}</td>
<td><span class="ccy">{c}</span> <span class="tag">{tag}</span></td>
<td><span class="compcell">{bar_svg(r["comp"], comp_max)}<span style="color:{compcol};min-width:40px">{sign(r["comp"])}</span></span></td>
<td class="ctr">{dash if r["trend"] is None else r["trend"]}</td>
<td class="ctr">{dash if r["carry"] is None else r["carry"]}</td>
<td class="ctr" style="font-weight:600;color:{vcol}">{dash if r["value"] is None else r["value"]}</td>
<td class="rgt" style="color:{wkcol}">{wkf}</td>
<td class="rgt">{spark_svg(d["sparks"][c], r["wk"])}</td></tr>'''
    alert_html = ""
    if d["alerts"]:
        alert_html = f'''<div class="alert"><span class="alert-k">\u25b2 ALERT</span>
<span class="alert-t">{" \u00b7 ".join(d["alerts"])}</span></div>'''
    html = f'''<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>Currency Drift \u00b7 {d["week_label"]}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;1,6..72,400&family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{{{cssvars(DARK)}}}
:root[data-theme=light]{{{cssvars(LIGHT)}}}
*{{box-sizing:border-box;margin:0;padding:0}}
@keyframes pulseDot{{0%,100%{{opacity:.5}}50%{{opacity:1}}}}
@keyframes glowPulse{{0%,100%{{opacity:.45}}50%{{opacity:.8}}}}
::selection{{background:rgba(51,214,189,.28)}}
body{{background:var(--bg);min-height:100vh;display:flex;justify-content:center;padding:48px 16px;font-family:'IBM Plex Sans',sans-serif;transition:background .3s}}
.panel{{width:1140px;max-width:100%;padding:60px;border-radius:28px;background:var(--panel);color:var(--txt);border:1px solid var(--border);box-shadow:0 40px 100px -30px rgba(0,0,0,.65);position:relative;overflow:hidden;background-image:radial-gradient(70% 44% at 50% 34%,var(--glow),transparent 62%),radial-gradient(55% 40% at 74% 72%,var(--glow2),transparent 62%)}}
header{{display:flex;justify-content:space-between;align-items:flex-start;gap:24px}}
.eyebrow{{font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--teal);margin-bottom:14px}}
h1{{font-family:'Newsreader',serif;font-weight:400;font-size:66px;line-height:.9;letter-spacing:-.02em}}
.sub{{font-family:'IBM Plex Mono',monospace;font-size:13px;color:var(--muted);margin-top:16px;letter-spacing:.02em}}
.toggle{{display:inline-flex;align-items:center;gap:7px;font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:.05em;color:var(--txt);background:var(--panel2);border:1px solid var(--border);border-radius:999px;padding:8px 15px;cursor:pointer;flex:none}}
.tiles{{display:grid;grid-template-columns:repeat(3,1fr);gap:24px;margin-top:44px}}
.tile-head{{display:flex;justify-content:space-between;align-items:baseline;font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:.16em;text-transform:uppercase}}
.tile{{padding-top:16px}}
.tile-ccy{{font-family:'Newsreader',serif;font-size:52px;line-height:1;margin:14px 0 10px}}
.tile-why{{font-size:14px;color:var(--muted);line-height:1.45;font-family:'Newsreader',serif;font-style:italic}}
.wheelwrap{{display:flex;flex-direction:column;align-items:center;margin:52px 0 8px}}
.wheel{{width:574px;max-width:100%}}
.legend{{display:flex;flex-wrap:wrap;justify-content:center;gap:20px;margin-top:22px;font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--muted);letter-spacing:.02em}}
.legend span{{display:inline-flex;align-items:center;gap:6px}}
.dot{{width:9px;height:9px;border-radius:50%;display:inline-block}}
.dotp{{width:11px;height:11px;border:1.5px dashed var(--ghost);border-radius:50%;display:inline-block}}
.tablewrap{{margin-top:44px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-family:'IBM Plex Mono',monospace;min-width:760px}}
th{{text-align:left;padding:0 10px 13px;font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:500;border-bottom:1px solid var(--border)}}
th.ctr{{text-align:center}}th.rgt{{text-align:right}}
td{{padding:13px 10px;font-size:13px;border-bottom:1px solid var(--border)}}
td.ctr{{text-align:center}}td.rgt{{text-align:right}}
td.mut{{font-size:12px;color:var(--muted)}}
.ccy{{font-size:15px;font-weight:600}}
.tag{{font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}}
.compcell{{display:inline-flex;align-items:center;gap:10px}}
.alert{{margin-top:20px;display:flex;gap:12px;align-items:center;font-family:'IBM Plex Mono',monospace;font-size:12.5px;color:var(--amber);border:1px solid rgba(255,184,77,.28);border-radius:12px;padding:13px 17px;background:rgba(255,184,77,.05)}}
.alert-k{{font-weight:600;letter-spacing:.06em;white-space:nowrap}}
.alert-t{{color:var(--txt);opacity:.85}}
footer{{margin-top:32px;font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--muted);letter-spacing:.02em}}
@media(max-width:760px){{
 body{{padding:12px 6px}}
 .panel{{padding:28px 20px;border-radius:20px}}
 h1{{font-size:42px}}
 .tiles{{grid-template-columns:1fr;gap:18px}}
 .tile-ccy{{font-size:40px}}
 .wheel{{width:100%}}
}}
</style></head><body>
<div class="panel">
<header><div>
<div class="eyebrow">{d["week_label"]}</div>
<h1>Currency Drift</h1>
<div class="sub">measured vs basket \u00b7 shown from {BASE}</div>
</div>
<button class="toggle" id="tg" onclick="tgl()">\u25d0 <span id="tgt">Light</span></button>
</header>
<div class="tiles">{tiles}</div>
<div class="wheelwrap"><div class="wheel">{d["wheel"]}</div>
<div class="legend">
<span><span class="dot" style="background:var(--teal)"></span>cheap</span>
<span><span class="dot" style="background:var(--grey)"></span>fair</span>
<span><span class="dot" style="background:var(--coral)"></span>rich</span>
<span><span class="dotp"></span>pegged</span>
<span>spoke = strength (\u00b1) \u00b7 dot = carry, coloured by value \u00b7 core = drift</span>
</div></div>
<div class="tablewrap"><table>
<thead><tr><th>Rank</th><th>Ccy</th><th>Composite</th><th class="ctr">Trend</th><th class="ctr">Carry</th><th class="ctr">Value</th><th class="rgt">Wk%</th><th class="rgt">12w</th></tr></thead>
<tbody>{trs}</tbody></table></div>
{alert_html}
<footer>data: {d["data_line"]} \u00b7 updates Saturdays \u00b7 base {BASE} \u00b7 data through {d["data_through"]} \u00b7 v{BUILD}</footer>
</div>
<script>
function setT(t){{document.documentElement.dataset.theme=t;document.getElementById('tgt').textContent=t==='light'?'Dark':'Light';try{{localStorage.setItem('cd-theme',t)}}catch(e){{}}}}
function tgl(){{setT(document.documentElement.dataset.theme==='light'?'dark':'light')}}
try{{var s=localStorage.getItem('cd-theme');if(s)setT(s)}}catch(e){{}}
</script>
</body></html>'''
    return html

# ---------------------------------------------------------------- digest
def email_bar(comp, comp_max, P):
    """Diverging composite bar built from table cells (SVG is dead in Gmail)."""
    half = 54
    ln = int(min(abs(comp) / comp_max * half, half)) if comp_max else 0
    neg = ln if comp < 0 else 0
    pos = ln if comp > 0 else 0
    cells = [f'<td width="{half - neg}" style="font-size:1px;line-height:9px">&nbsp;</td>']
    if neg:
        cells.append(f'<td width="{neg}" height="9" bgcolor="{P["coral"]}" style="font-size:1px;line-height:9px">&nbsp;</td>')
    cells.append(f'<td width="2" height="13" bgcolor="{P["line"]}" style="font-size:1px;line-height:13px">&nbsp;</td>')
    if pos:
        cells.append(f'<td width="{pos}" height="9" bgcolor="{P["teal"]}" style="font-size:1px;line-height:9px">&nbsp;</td>')
    cells.append(f'<td width="{half - pos}" style="font-size:1px;line-height:9px">&nbsp;</td>')
    return ('<table cellpadding="0" cellspacing="0" border="0" style="display:inline-table;vertical-align:middle"><tr>'
            + "".join(cells) + "</tr></table>")

def email_spark(vals):
    blocks = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
    mn, mx = min(vals), max(vals)
    return "".join(blocks[3] if mx == mn else blocks[round((v - mn) / (mx - mn) * 7)] for v in vals)

def render_email(state):
    rows, order = state["rows"], state["order"]
    P = {"bg": "#0a0b0e", "panel": "#12151b", "txt": "#eceef2", "muted": "#7d838f",
         "border": "#262b33", "line": "#3a404b", "teal": "#33d6bd", "coral": "#ff6a5c",
         "amber": "#ffb84d", "grey": "#9aa0ab", "ghost": "#4c515b"}
    MONO = "'Courier New',Courier,monospace"
    SERIF = "Georgia,'Times New Roman',serif"

    def col(v):
        return P["teal"] if v > 0 else (P["coral"] if v < 0 else P["muted"])

    comp_max = max(0.1, max(abs(rows[c]["comp"]) for c in CODES))
    t = state["tiles"]
    tilecells = ""
    for kind, kcol, glyph in (("increase", P["teal"], "\u2191"),
                              ("watch", P["amber"], "\u25c6"),
                              ("reduce", P["coral"], "\u2193")):
        tt = t[kind]
        tilecells += (
            f'<td width="33%" valign="top" style="padding:14px 12px 4px;border-top:2px solid {kcol}">'
            f'<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>'
            f'<td style="font-family:{MONO};font-size:10px;letter-spacing:2px;text-transform:uppercase;color:{kcol}">{kind}</td>'
            f'<td align="right" style="color:{kcol};font-size:12px">{glyph}</td></tr></table>'
            f'<div style="font-family:{SERIF};font-size:34px;line-height:1;margin:10px 0 8px;color:{P["txt"]}">{tt["ccy"]}</div>'
            f'<div style="font-family:{SERIF};font-size:12.5px;font-style:italic;color:{P["muted"]};line-height:1.4">{tt["why"]}</div></td>')

    rowshtml = ""
    dash = "\u2013"
    for c in order:
        r = rows[c]
        tag = ""
        if c == BASE or c in PEGGED:
            word = "BASE" if c == BASE else "PEG"
            tag = f' <span style="font-size:8px;letter-spacing:1px;color:{P["muted"]}">{word}</span>'
        if r["value"] is None:
            vcol = P["ghost"]
        elif r["value"] >= 7:
            vcol = P["teal"]
        elif r["value"] <= 3:
            vcol = P["coral"]
        else:
            vcol = P["grey"]
        bd = f'border-bottom:1px solid {P["border"]}'
        rowshtml += (
            f'<tr><td style="padding:9px 8px;color:{P["muted"]};font-size:11px;font-family:{MONO};{bd}">{r["rank"]}</td>'
            f'<td style="padding:9px 8px;font-weight:bold;color:{P["txt"]};font-family:{MONO};font-size:13px;{bd}">{c}{tag}</td>'
            f'<td style="padding:9px 8px;{bd};white-space:nowrap">{email_bar(r["comp"], comp_max, P)}'
            f'<span style="font-family:{MONO};font-size:12px;color:{col(r["comp"])}">&nbsp;{sign(r["comp"])}</span></td>'
            f'<td align="center" style="padding:9px 6px;color:{P["txt"]};font-family:{MONO};font-size:12px;{bd}">{dash if r["trend"] is None else r["trend"]}</td>'
            f'<td align="center" style="padding:9px 6px;color:{P["txt"]};font-family:{MONO};font-size:12px;{bd}">{dash if r["carry"] is None else r["carry"]}</td>'
            f'<td align="center" style="padding:9px 6px;font-weight:bold;color:{vcol};font-family:{MONO};font-size:12px;{bd}">{dash if r["value"] is None else r["value"]}</td>'
            f'<td align="right" style="padding:9px 8px;color:{col(r["wk"])};font-family:{MONO};font-size:12px;{bd}">{sign(r["wk"])}%</td>'
            f'<td align="right" style="padding:9px 8px;color:{col(r["wk"])};font-family:{MONO};font-size:10px;letter-spacing:1px;{bd}">{email_spark(state["sparks"][c])}</td></tr>')

    alert_block = ""
    if state["alerts"]:
        inner = (f'<table width="100%" cellpadding="0" cellspacing="0"><tr>'
                 f'<td style="padding:12px 14px;border:1px solid #6b512a;border-radius:10px;background:#1c1710;'
                 f'font-family:{MONO};font-size:11.5px;color:{P["amber"]}">\u25b2 ALERT&nbsp;&nbsp;'
                 f'<span style="color:{P["txt"]}">{" \u00b7 ".join(state["alerts"])}</span></td></tr></table>')
        alert_block = f'<tr><td style="padding:16px 30px 0">{inner}</td></tr>'

    link = ""
    if CFG.get("page_url"):
        link = (f'<tr><td align="center" style="padding:20px 0 4px">'
                f'<a href="{CFG["page_url"]}?w={state["week"]}" style="font-family:{MONO};font-size:12px;letter-spacing:1px;'
                f'color:{P["bg"]};background:{P["teal"]};text-decoration:none;padding:10px 22px;'
                f'border-radius:999px;display:inline-block">OPEN THE WHEEL \u2192</a></td></tr>')

    return (
        f'<html><head><meta name="color-scheme" content="dark">'
        f'<meta name="supported-color-schemes" content="dark"></head>'
        f'<body style="margin:0;padding:0;background:{P["bg"]}">'
        f'<table width="100%" cellpadding="0" cellspacing="0" bgcolor="{P["bg"]}"><tr><td align="center" style="padding:26px 8px">'
        f'<table width="660" cellpadding="0" cellspacing="0" bgcolor="{P["panel"]}" '
        f'style="background:{P["panel"]};border-radius:16px;border:1px solid {P["border"]}">'
        f'<tr><td style="padding:30px 30px 0">'
        f'<div style="font-family:{MONO};font-size:10px;letter-spacing:3px;color:{P["teal"]};text-transform:uppercase">{state["week_label"]}</div>'
        f'<div style="font-family:{SERIF};font-size:42px;color:{P["txt"]};padding:8px 0 4px">Currency Drift</div>'
        f'<div style="font-family:{MONO};font-size:11px;color:{P["muted"]}">measured vs basket \u00b7 shown from {BASE} \u00b7 drift {state["churn"]} \u00b7 {state["band"]}</div>'
        f'</td></tr>'
        f'<tr><td style="padding:20px 30px 0"><table width="100%" cellpadding="0" cellspacing="0"><tr>{tilecells}</tr></table></td></tr>'
        f'<tr><td style="padding:18px 30px 0"><table width="100%" cellpadding="0" cellspacing="0">'
        f'<tr style="font-family:{MONO};font-size:9px;letter-spacing:2px;color:{P["muted"]};text-transform:uppercase">'
        f'<td style="padding:6px 8px;border-bottom:1px solid {P["line"]}">#</td>'
        f'<td style="padding:6px 8px;border-bottom:1px solid {P["line"]}">Ccy</td>'
        f'<td style="padding:6px 8px;border-bottom:1px solid {P["line"]}">Composite</td>'
        f'<td align="center" style="padding:6px;border-bottom:1px solid {P["line"]}">T</td>'
        f'<td align="center" style="padding:6px;border-bottom:1px solid {P["line"]}">C</td>'
        f'<td align="center" style="padding:6px;border-bottom:1px solid {P["line"]}">V</td>'
        f'<td align="right" style="padding:6px 8px;border-bottom:1px solid {P["line"]}">Wk%</td>'
        f'<td align="right" style="padding:6px 8px;border-bottom:1px solid {P["line"]}">12w</td></tr>'
        f'{rowshtml}</table></td></tr>'
        f'{alert_block}'
        f'{link}'
        f'<tr><td style="padding:16px 30px 26px;font-family:{MONO};font-size:9.5px;color:{P["muted"]};letter-spacing:1px">'
        f'data: {state["data_line"]} \u00b7 base {BASE} \u00b7 through {state["data_through"]} \u00b7 v{BUILD}</td></tr>'
        f'</table></td></tr></table></body></html>')

def send_email(state):
    user, pw = os.environ.get("EMAIL_USER"), os.environ.get("EMAIL_PASS")
    to = os.environ.get("EMAIL_TO", user)
    if not (user and pw):
        log("email: no credentials, skipping")
        return
    msg = MIMEMultipart("alternative")
    n_alert = len(state["alerts"])
    flag = f" \u00b7 {n_alert} alert{'s' if n_alert != 1 else ''}" if n_alert else ""
    msg["Subject"] = f"Currency Drift \u00b7 {state['week_label']} \u00b7 {state['tiles']['increase']['ccy']} up, {state['tiles']['reduce']['ccy']} down{flag}"
    msg["From"], msg["To"] = user, to
    msg.attach(MIMEText(render_email(state), "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pw)
        s.sendmail(user, [to], msg.as_string())
    log(f"email: sent to {to}")

def send_whatsapp(state):
    phone, key = os.environ.get("CALLMEBOT_PHONE"), os.environ.get("CALLMEBOT_APIKEY")
    if not (phone and key):
        log("whatsapp: no credentials, skipping")
        return
    if not state["alerts"]:
        log("whatsapp: no alerts, staying quiet")
        return
    t = state["tiles"]
    wk = state["week_label"].replace("Week ", "Wk ")
    lines = [f"\U0001f9ed *Currency Drift* \u00b7 {wk}",
             f"\U0001f300 drift {state['churn']} \u00b7 {state['band']}",
             (f"\U0001f7e2 *{t['increase']['ccy']}* \u2191  "
              f"\U0001f7e1 *{t['watch']['ccy']}* \u25c6  "
              f"\U0001f534 *{t['reduce']['ccy']}* \u2193")]
    for a in state["alerts"][:6]:
        if "entered top 3" in a or "left bottom 3" in a:
            g = "\u2b06\ufe0f"
        elif "left top 3" in a or "entered bottom 3" in a:
            g = "\u2b07\ufe0f"
        elif "week band" in a:
            g = "\u26a1"
        elif "flipped positive" in a:
            g = "\U0001f7e2"
        elif "flipped negative" in a:
            g = "\U0001f534"
        else:
            g = "\u2022"
        lines.append(f"{g} {a}")
    if CFG.get("page_url"):
        lines.append(f"\U0001f517 {CFG['page_url']}?w={state['week']}")
    text = "\n".join(lines)
    url = ("https://api.callmebot.com/whatsapp.php?phone=" + urllib.parse.quote(phone)
           + "&apikey=" + urllib.parse.quote(key) + "&text=" + urllib.parse.quote(text))
    try:
        http_get(url, timeout=30)
        log("whatsapp: alert sent")
    except Exception as e:
        log(f"whatsapp failed: {e}")

# ---------------------------------------------------------------- main
def main():
    daily, rates_src = load_daily_rates()
    weeks = weekly_closes(daily)
    lookback = CFG["value_lookback_weeks"]
    min_w = max(CFG["trend_weeks"]) + 1
    if len(weeks) < min_w + CFG["spark_weeks"]:
        raise SystemExit("not enough weekly history")

    carry = dict(CFG["carry_seed"]["rates"])
    carry_src = f"seed {CFG['carry_seed']['as_of']}"
    if not OFFLINE:
        try:
            live = fetch_bis_cbpol()
            for c, v in live.items():
                carry[c] = v
            if len(live) >= len(CODES):
                carry_src = "BIS CBPOL live"
            else:
                missing = ",".join(sorted(set(CODES) - set(live)))
                carry_src = f"BIS CBPOL {len(live)}/10 + seed ({missing})"
            log(f"carry: {carry_src}")
        except Exception as e:
            log(f"carry: BIS CBPOL failed ({e}); using seed")

    value_override, value_src = None, "nominal 10y proxy"
    if not OFFLINE:
        try:
            value_override = fetch_bis_reer()
            value_src = "BIS REER"
            log("value: BIS REER live")
        except Exception as e:
            log(f"value: BIS REER failed ({e}); using nominal proxy")

    W = len(weeks) - 1
    spark_n = CFG["spark_weeks"]
    all_rows = {}
    for w in range(W - spark_n, W + 1):
        # historic weeks always use the proxy value lens (REER history not kept);
        # the current week uses REER when available
        vo = value_override if w == W else None
        all_rows[w] = score_week(weeks, w, carry, vo)
    cur, prev = all_rows[W], all_rows[W - 1]
    order = sorted(CODES, key=lambda c: cur[c]["rank"])
    sparks = {c: [all_rows[w][c]["comp"] for w in range(W - spark_n + 1, W + 1)] for c in CODES}
    alerts = build_alerts(cur, prev)
    churn_v = churn(cur, prev)
    band = churn_band(churn_v)

    nonpeg = [c for c in order if c not in PEGGED]
    inc, red = nonpeg[0], nonpeg[-1]
    mid = [c for c in CODES if c not in (inc, red)]
    watch = max(mid, key=lambda c: (abs(cur[c]["wk"]), abs(cur[c]["rank"] - prev[c]["rank"])))
    dr_watch = prev[watch]["rank"] - cur[watch]["rank"]
    tiles = {
        "increase": {"ccy": inc, "why": tile_phrase("increase", inc, cur[inc], carry, alerts, 0)},
        "watch": {"ccy": watch, "why": tile_phrase("watch", watch, cur[watch], carry, alerts, dr_watch)},
        "reduce": {"ccy": red, "why": tile_phrase("reduce", red, cur[red], carry, alerts, 0)},
    }

    week_date = datetime.strptime(weeks[W][0], "%Y-%m-%d").date()
    iso_week = week_date.isocalendar()[1]
    state = {
        "rows": cur, "order": order, "sparks": sparks, "alerts": alerts,
        "churn": churn_v, "band": band, "tiles": tiles,
        "week": weeks[W][0],
        "week_label": f"Week {iso_week} \u00b7 {week_date.strftime('%a %d %b')}",
        "data_line": f"{rates_src} \u00b7 value: {value_src} \u00b7 carry: {carry_src}",
        "data_through": datetime.strptime(weeks[W][2], "%Y-%m-%d").date().strftime("%d %b %Y"),
    }
    state["wheel"] = wheel_svg(cur, order, churn_v, band, {a.split()[0] for a in alerts})

    # history (idempotent on week key)
    hist_path = os.path.join(ROOT, "data", "history.json")
    hist = []
    if os.path.exists(hist_path):
        hist = json.load(open(hist_path))
    hist = [h for h in hist if h["week"] != weeks[W][0]]
    if not hist:  # first run: backfill from computed weeks
        for w in range(W - spark_n + 1, W):
            wd = weeks[w][0]
            hist.append({"week": wd, "rows": all_rows[w],
                         "churn": churn(all_rows[w], all_rows.get(w - 1)),
                         "backfilled": True})
    hist.append({"week": weeks[W][0], "rows": cur, "churn": churn_v, "alerts": alerts})
    hist.sort(key=lambda h: h["week"])
    json.dump(hist, open(hist_path, "w"), indent=1)

    snap = {"as_of": weeks[W][2], "week": weeks[W][0], "base": BASE,
            "version": CFG["version"], "drift_churn": churn_v, "drift_band": band,
            "value_source": value_src, "carry_source": carry_src, "rates_source": rates_src,
            "alerts": alerts, "tiles": {k: v["ccy"] for k, v in tiles.items()},
            "currencies": {c: {**cur[c], "pegged": c in PEGGED,
                               "carry_rate_pct": carry[c],
                               "sgd_per_unit": weeks[W][1].get(c, 1.0)} for c in CODES}}
    json.dump(snap, open(os.path.join(ROOT, "fx_scores.json"), "w"), indent=1)

    open(os.path.join(ROOT, "index.html"), "w").write(render_page(state))
    log(f"page: {state['week_label']} \u00b7 drift {churn_v} ({band}) \u00b7 alerts {len(alerts)}")
    log(f"tiles: +{inc} \u00b7 watch {watch} \u00b7 \u2212{red}")

    if "--email-preview" in sys.argv:
        open(os.path.join(ROOT, "email_preview.html"), "w").write(render_email(state))
        log("email preview written")

    if not NO_SEND:
        try:
            send_email(state)
        except Exception as e:
            log(f"email failed: {e}")
        send_whatsapp(state)

if __name__ == "__main__":
    main()
