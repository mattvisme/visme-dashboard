#!/usr/bin/env python3
"""
Visme GA4 Dashboard Builder
Pulls ~3 years of weekly data and renders a self-contained HTML dashboard.
"""

import json, sys, os
from datetime import date, timedelta, datetime
from collections import defaultdict

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange, Dimension, Metric, RunReportRequest,
    FilterExpression, Filter
)
InListFilter = Filter.InListFilter
from google.oauth2 import service_account

# ─── CONFIG ──────────────────────────────────────────────────────────────────
# Env vars override local defaults — used by GitHub Actions CI
CREDENTIALS_FILE = os.environ.get(
    "GA4_CREDENTIALS_FILE",
    r"C:\Users\mattj\Downloads\visme-marketing-491309-47059dacd5b9.json"
)
PROPERTY_ID  = os.environ.get("GA4_PROPERTY_ID", "368188880")
OUTPUT_FILE  = os.environ.get(
    "GA4_OUTPUT_FILE",
    r"C:\Users\mattj\Documents\Claude\Visme\visme-ga4-dashboard.html"
)
WEEKS_HISTORY = 156   # 3 years: 104 current + 52 prior-year buffer
CI = os.environ.get("CI", "false").lower() == "true"

# ─── DATE RANGE ───────────────────────────────────────────────────────────────
today         = date.today()
last_sunday   = today - timedelta(days=(today.weekday() + 1) % 7 or 7)
start_dt      = last_sunday - timedelta(weeks=WEEKS_HISTORY - 1)

END_DATE   = last_sunday.strftime("%Y-%m-%d")
START_DATE = start_dt.strftime("%Y-%m-%d")
AS_OF_DATE = last_sunday.strftime("%B %-d, %Y") if sys.platform != "win32" else last_sunday.strftime("%B %#d, %Y")

print(f"📅  Date range: {START_DATE} → {END_DATE}  ({WEEKS_HISTORY} weeks)")

# ─── GA4 CLIENT ───────────────────────────────────────────────────────────────
# Support inline JSON string (GitHub Actions secret) or file path
_creds_json = os.environ.get("GA4_CREDENTIALS_JSON")
if _creds_json:
    import tempfile
    _tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    _tmp.write(_creds_json); _tmp.close()
    CREDENTIALS_FILE = _tmp.name

creds  = service_account.Credentials.from_service_account_file(
    CREDENTIALS_FILE,
    scopes=["https://www.googleapis.com/auth/analytics.readonly"]
)
client = BetaAnalyticsDataClient(credentials=creds)
PROP   = f"properties/{PROPERTY_ID}"

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def yw_to_monday(yw: str) -> date:
    """Convert GA4 yearWeek (e.g. '202403') to the Monday of that ISO week."""
    year, wk = int(yw[:4]), int(yw[4:])
    return datetime.strptime(f"{year}-W{wk:02d}-1", "%G-W%V-%u").date()

def run(dimensions, metrics, row_limit=250_000, dim_filter=None):
    req = RunReportRequest(
        property=PROP,
        date_ranges=[DateRange(start_date=START_DATE, end_date=END_DATE)],
        dimensions=[Dimension(name=d) for d in dimensions],
        metrics=[Metric(name=m) for m in metrics],
        limit=row_limit,
    )
    if dim_filter:
        req.dimension_filter = dim_filter
    resp = client.run_report(req)
    rows = []
    for r in resp.rows:
        dims = [d.value for d in r.dimension_values]
        mets = [m.value for m in r.metric_values]
        rows.append(dims + mets)
    return rows

def int_(v):
    try: return int(float(v))
    except: return 0

# ─── 1. SESSIONS + NEW USERS BY WEEK ─────────────────────────────────────────
print("⏳  Pulling sessions + new users …")
rows1 = run(["yearWeek"], ["sessions", "newUsers"])
weekly_sessions  = {}
weekly_new_users = {}
for yw, sess, nu in rows1:
    weekly_sessions[yw]  = int_(sess)
    weekly_new_users[yw] = int_(nu)

# ─── 2. NEW vs RETURNING by WEEK ──────────────────────────────────────────────
print("⏳  Pulling new vs returning …")
rows2 = run(["yearWeek", "newVsReturning"], ["sessions"])
weekly_nvr = defaultdict(lambda: {"new": 0, "returning": 0})
for yw, nvr, sess in rows2:
    key = "new" if nvr.lower() == "new" else "returning"
    weekly_nvr[yw][key] += int_(sess)

# ─── 3. CHANNEL by WEEK ──────────────────────────────────────────────────────
print("⏳  Pulling channel sessions …")
rows3 = run(["yearWeek", "sessionDefaultChannelGroup"], ["sessions"])
weekly_channels = defaultdict(lambda: defaultdict(int))
all_channels    = set()
for yw, ch, sess in rows3:
    weekly_channels[yw][ch] += int_(sess)
    all_channels.add(ch)

# top-15 channels by total sessions
channel_totals = defaultdict(int)
for yw_data in weekly_channels.values():
    for ch, v in yw_data.items():
        channel_totals[ch] += v
top_channels = [c for c, _ in sorted(channel_totals.items(), key=lambda x: -x[1])[:15]]

# ─── 4. US vs NON-US by WEEK ─────────────────────────────────────────────────
print("⏳  Pulling geo sessions …")
rows4 = run(["yearWeek", "country"], ["sessions"])
weekly_geo = defaultdict(lambda: {"us": 0, "nonUs": 0})
for yw, country, sess in rows4:
    if country == "United States":
        weekly_geo[yw]["us"] += int_(sess)
    else:
        weekly_geo[yw]["nonUs"] += int_(sess)

# ─── 5. TOP LANDING PAGES (aggregate) ────────────────────────────────────────
print("⏳  Pulling landing pages …")
rows5 = run(["landingPagePlusQueryString"], ["sessions", "newUsers", "bounceRate"], row_limit=500)
landing_pages_raw = []
for row in rows5:
    page, sess, nu, br = row
    landing_pages_raw.append({
        "page": page[:80],
        "sessions": int_(sess),
        "newUsers": int_(nu),
        "bounceRate": round(float(br) * 100, 1)
    })
landing_pages_raw.sort(key=lambda x: -x["sessions"])
top_landing_pages = landing_pages_raw[:10]

# ─── 6. CONVERSION EVENTS by WEEK ────────────────────────────────────────────
print("⏳  Pulling conversion events …")
TARGET_EVENTS = ["create_an_account", "visit_payment_page", "purchase"]
event_filter = FilterExpression(
    filter=Filter(
        field_name="eventName",
        in_list_filter=InListFilter(values=TARGET_EVENTS)
    )
)
rows6 = run(["yearWeek", "eventName"], ["eventCount"], dim_filter=event_filter)
weekly_events = defaultdict(lambda: {e: 0 for e in TARGET_EVENTS})
for yw, evt, cnt in rows6:
    if evt in TARGET_EVENTS:
        weekly_events[yw][evt] += int_(cnt)

# ─── ASSEMBLE SORTED WEEK LIST ────────────────────────────────────────────────
all_weeks = sorted(set(
    list(weekly_sessions.keys()) +
    list(weekly_nvr.keys()) +
    list(weekly_channels.keys()) +
    list(weekly_geo.keys()) +
    list(weekly_events.keys())
))

# Build ordered labels (week-start Monday as readable date)
week_labels = {}
for yw in all_weeks:
    try:
        mon = yw_to_monday(yw)
        week_labels[yw] = mon.strftime("%b %-d '%y") if sys.platform != "win32" else mon.strftime("%b %#d '%y")
    except:
        week_labels[yw] = yw

# ─── SERIALIZE TO JSON ───────────────────────────────────────────────────────
payload = {
    "asOfDate"    : AS_OF_DATE,
    "weeks"       : all_weeks,
    "weekLabels"  : [week_labels.get(w, w) for w in all_weeks],
    "sessions"    : {w: weekly_sessions.get(w, 0)  for w in all_weeks},
    "newUsers"    : {w: weekly_new_users.get(w, 0) for w in all_weeks},
    "nvr"         : {w: weekly_nvr.get(w, {"new": 0, "returning": 0}) for w in all_weeks},
    "channels"    : {w: {ch: weekly_channels[w].get(ch, 0) for ch in top_channels} for w in all_weeks},
    "topChannels" : top_channels,
    "geo"         : {w: weekly_geo.get(w, {"us": 0, "nonUs": 0}) for w in all_weeks},
    "landingPages": top_landing_pages,
    "events"      : {w: weekly_events.get(w, {e: 0 for e in TARGET_EVENTS}) for w in all_weeks},
}

print(f"✅  Data collected — {len(all_weeks)} weeks, {len(top_channels)} channels")

# ─── HTML TEMPLATE ───────────────────────────────────────────────────────────
DATA_JSON = json.dumps(payload, separators=(',', ':'))

HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Visme Marketing Analytics</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=Fraunces:ital,opsz,wght@0,9..144,300;0,9..144,600;1,9..144,300&display=swap" rel="stylesheet"/>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js"></script>
<style>
/* ── RESET & BASE ── */
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#F7F6F2;
  --surface:#FFFFFF;
  --surface2:#F0EFE9;
  --sidebar:#0E1117;
  --sidebar-accent:#1C2333;
  --text:#1A1A1A;
  --text-muted:#6B7280;
  --text-dim:#9CA3AF;
  --accent:#E63950;
  --accent2:#3B6FE8;
  --accent3:#10B981;
  --accent-pale:#FEF1F3;
  --border:#E5E3DC;
  --border-light:#EDECE6;
  --sidebar-w:232px;
  --topbar-h:72px;
  --radius:12px;
  --radius-sm:8px;
  --shadow:0 1px 3px rgba(0,0,0,.06),0 4px 16px rgba(0,0,0,.04);
  --shadow-md:0 4px 12px rgba(0,0,0,.08),0 12px 40px rgba(0,0,0,.06);
  --font-ui:'Outfit',sans-serif;
  --font-display:'Fraunces',serif;
}}
html{{font-size:15px;-webkit-font-smoothing:antialiased}}
body{{
  font-family:var(--font-ui);
  background:var(--bg);
  color:var(--text);
  min-height:100vh;
  overflow-x:hidden;
}}

/* ── SIDEBAR ── */
#sidebar{{
  position:fixed;top:0;left:0;
  width:var(--sidebar-w);height:100vh;
  background:var(--sidebar);
  display:flex;flex-direction:column;
  z-index:100;
  overflow-y:auto;
  scrollbar-width:none;
}}
#sidebar::-webkit-scrollbar{{display:none}}
.sidebar-logo{{
  padding:28px 24px 20px;
  border-bottom:1px solid rgba(255,255,255,.07);
}}
.sidebar-logo-mark{{
  display:flex;align-items:center;gap:10px;
}}
.logo-gem{{
  width:32px;height:32px;
  background:linear-gradient(135deg,var(--accent) 0%,#FF8FA3 100%);
  border-radius:8px;
  display:flex;align-items:center;justify-content:center;
  font-size:14px;font-weight:700;color:#fff;
  letter-spacing:-.5px;
}}
.logo-text{{
  font-family:var(--font-display);
  font-size:17px;font-weight:600;
  color:#fff;letter-spacing:-.3px;
}}
.logo-sub{{
  font-size:10.5px;color:rgba(255,255,255,.35);
  margin-top:2px;letter-spacing:.5px;text-transform:uppercase;
  font-weight:400;
}}
.sidebar-section-label{{
  padding:20px 24px 8px;
  font-size:9.5px;font-weight:600;
  color:rgba(255,255,255,.25);
  letter-spacing:1.2px;text-transform:uppercase;
}}
.sidebar-nav{{
  list-style:none;
  padding:0 12px;
  flex:1;
}}
.sidebar-nav li a{{
  display:flex;align-items:center;gap:10px;
  padding:9px 12px;border-radius:8px;
  text-decoration:none;
  font-size:13.5px;font-weight:400;
  color:rgba(255,255,255,.55);
  transition:all .15s ease;
  margin-bottom:1px;
}}
.sidebar-nav li a:hover{{
  background:rgba(255,255,255,.07);
  color:rgba(255,255,255,.9);
}}
.sidebar-nav li a.active{{
  background:rgba(230,57,80,.15);
  color:#FF7A8A;
}}
.nav-icon{{
  width:16px;height:16px;opacity:.7;flex-shrink:0;
}}
.sidebar-footer{{
  padding:16px 24px;
  border-top:1px solid rgba(255,255,255,.07);
  font-size:11px;color:rgba(255,255,255,.2);
  line-height:1.5;
}}

/* ── MAIN LAYOUT ── */
#main{{
  margin-left:var(--sidebar-w);
  min-height:100vh;
  display:flex;flex-direction:column;
}}

/* ── TOPBAR ── */
#topbar{{
  position:sticky;top:0;z-index:50;
  height:var(--topbar-h);
  background:rgba(247,246,242,.92);
  backdrop-filter:blur(12px);
  -webkit-backdrop-filter:blur(12px);
  border-bottom:1px solid var(--border);
  display:flex;align-items:center;
  padding:0 32px;
  gap:24px;
}}
.topbar-title{{
  font-family:var(--font-display);
  font-size:20px;font-weight:600;
  color:var(--text);letter-spacing:-.4px;
  flex-shrink:0;
  margin-right:auto;
}}
.topbar-title span{{color:var(--accent)}}
.as-of-badge{{
  font-size:11.5px;font-weight:500;
  color:var(--text-muted);
  background:var(--surface2);
  border:1px solid var(--border);
  padding:5px 12px;border-radius:20px;
  white-space:nowrap;flex-shrink:0;
}}
.range-control{{
  display:flex;align-items:center;gap:16px;flex-shrink:0;
}}
.range-label{{
  font-size:11.5px;font-weight:600;
  color:var(--text-muted);
  letter-spacing:.3px;text-transform:uppercase;
  white-space:nowrap;
}}
.range-pills{{
  display:flex;gap:4px;
}}
.pill{{
  padding:5px 12px;border-radius:20px;
  font-size:12.5px;font-weight:500;
  color:var(--text-muted);
  background:var(--surface);
  border:1px solid var(--border);
  cursor:pointer;
  transition:all .15s ease;
  white-space:nowrap;
  user-select:none;
}}
.pill:hover{{background:var(--surface2);color:var(--text)}}
.pill.active{{
  background:var(--accent);
  color:#fff;
  border-color:var(--accent);
  box-shadow:0 2px 8px rgba(230,57,80,.3);
}}

/* ── CONTENT ── */
#content{{
  padding:32px;
  display:flex;flex-direction:column;gap:32px;
  max-width:1400px;
  width:100%;
}}

/* ── SECTION ── */
.section{{display:flex;flex-direction:column;gap:16px}}
.section-header{{
  display:flex;align-items:baseline;gap:12px;
}}
.section-title{{
  font-family:var(--font-display);
  font-size:18px;font-weight:600;
  color:var(--text);letter-spacing:-.3px;
  font-style:italic;
}}
.section-subtitle{{
  font-size:12px;color:var(--text-dim);font-weight:400;
}}
.section-divider{{
  height:1px;background:var(--border-light);
  margin-bottom:2px;
}}

/* ── CARDS ── */
.cards-row{{
  display:grid;gap:16px;
}}
.cards-2{{grid-template-columns:1fr 1fr}}
.cards-3{{grid-template-columns:1fr 1fr 1fr}}
.cards-full{{grid-template-columns:1fr}}

.card{{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:var(--radius);
  padding:24px;
  box-shadow:var(--shadow);
  transition:box-shadow .2s ease;
}}
.card:hover{{box-shadow:var(--shadow-md)}}
.card-title{{
  font-size:12px;font-weight:600;
  color:var(--text-muted);
  letter-spacing:.4px;text-transform:uppercase;
  margin-bottom:4px;
}}
.card-desc{{
  font-size:11.5px;color:var(--text-dim);
  margin-bottom:20px;line-height:1.4;
}}
.chart-wrap{{
  position:relative;
  height:240px;
}}
.chart-wrap-sm{{
  position:relative;
  height:200px;
}}

/* ── KPI ROW ── */
.kpi-row{{
  display:grid;
  grid-template-columns:repeat(4,1fr);
  gap:12px;
  margin-bottom:4px;
}}
.kpi{{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:var(--radius-sm);
  padding:16px 18px;
  box-shadow:var(--shadow);
}}
.kpi-label{{
  font-size:11px;font-weight:600;
  color:var(--text-dim);
  letter-spacing:.5px;text-transform:uppercase;
  margin-bottom:6px;
}}
.kpi-value{{
  font-size:26px;font-weight:700;
  color:var(--text);line-height:1;
  letter-spacing:-1px;
}}
.kpi-delta{{
  font-size:11.5px;font-weight:500;
  margin-top:5px;
  display:flex;align-items:center;gap:4px;
}}
.delta-up{{color:#10B981}}
.delta-down{{color:#EF4444}}
.delta-neu{{color:var(--text-dim)}}

/* ── TABLES ── */
.tbl-wrap{{overflow-x:auto}}
table{{
  width:100%;border-collapse:collapse;
  font-size:13px;
}}
thead th{{
  font-size:11px;font-weight:600;
  color:var(--text-muted);
  letter-spacing:.4px;text-transform:uppercase;
  padding:10px 14px;
  background:var(--surface2);
  border-bottom:1px solid var(--border);
  text-align:left;
  cursor:pointer;
  user-select:none;
  white-space:nowrap;
}}
thead th:hover{{color:var(--text)}}
thead th.sort-asc::after{{content:" ↑"}}
thead th.sort-desc::after{{content:" ↓"}}
tbody tr{{
  border-bottom:1px solid var(--border-light);
  transition:background .1s;
}}
tbody tr:last-child{{border-bottom:none}}
tbody tr:hover{{background:var(--surface2)}}
tbody td{{
  padding:10px 14px;
  color:var(--text);
  vertical-align:middle;
}}
.page-cell{{
  max-width:320px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font-family:monospace;font-size:11.5px;
  color:var(--accent2);
}}
.num-cell{{text-align:right;font-variant-numeric:tabular-nums}}
.bar-cell{{min-width:100px}}
.mini-bar{{
  height:6px;border-radius:3px;
  background:linear-gradient(90deg,var(--accent2),#93C5FD);
  transition:width .4s ease;
}}
.change-pos{{color:#10B981;font-weight:600}}
.change-neg{{color:#EF4444;font-weight:600}}
.rank-badge{{
  display:inline-flex;align-items:center;justify-content:center;
  width:20px;height:20px;border-radius:50%;
  background:var(--surface2);
  font-size:10px;font-weight:700;color:var(--text-muted);
}}

/* ── FUNNEL METRICS ── */
.funnel-grid{{
  display:grid;grid-template-columns:repeat(3,1fr);gap:16px;
}}
.funnel-card{{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:var(--radius);
  padding:20px 24px;
  box-shadow:var(--shadow);
}}
.funnel-event-name{{
  font-size:10.5px;font-weight:700;
  color:var(--accent);
  letter-spacing:.8px;text-transform:uppercase;
  margin-bottom:4px;
  display:flex;align-items:center;gap:6px;
}}
.funnel-event-name::before{{
  content:'';display:inline-block;
  width:6px;height:6px;border-radius:50%;
  background:currentColor;
}}
.funnel-title{{
  font-size:13.5px;font-weight:600;color:var(--text);
  margin-bottom:16px;
}}

/* ── LEGEND ── */
.chart-legend{{
  display:flex;gap:16px;flex-wrap:wrap;
  margin-top:10px;
}}
.legend-item{{
  display:flex;align-items:center;gap:6px;
  font-size:11.5px;color:var(--text-muted);
}}
.legend-dot{{
  width:8px;height:8px;border-radius:50%;flex-shrink:0;
}}
.legend-dash{{
  width:16px;height:2px;flex-shrink:0;
  border-top:2px dashed currentColor;
  opacity:.7;
}}

/* ── SCROLLBAR ── */
::-webkit-scrollbar{{width:5px;height:5px}}
::-webkit-scrollbar-track{{background:transparent}}
::-webkit-scrollbar-thumb{{background:var(--border);border-radius:3px}}

/* ── LOADING ── */
#loading{{
  position:fixed;inset:0;
  background:var(--sidebar);
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  z-index:999;
  opacity:1;transition:opacity .5s ease;
}}
#loading.hide{{opacity:0;pointer-events:none}}
.loading-logo{{
  font-family:var(--font-display);font-size:28px;font-weight:600;
  color:#fff;margin-bottom:24px;font-style:italic;
}}
.loading-logo span{{color:var(--accent)}}
.loading-bar-track{{
  width:200px;height:3px;background:rgba(255,255,255,.1);border-radius:2px;
}}
.loading-bar-fill{{
  height:100%;background:var(--accent);border-radius:2px;
  animation:loadbar 1.2s ease-in-out forwards;
}}
@keyframes loadbar{{from{{width:0}}to{{width:100%}}}}
.loading-text{{
  margin-top:14px;font-size:12px;color:rgba(255,255,255,.3);
  letter-spacing:.5px;
}}

/* ── ANIMATIONS ── */
@keyframes fadeUp{{
  from{{opacity:0;transform:translateY(16px)}}
  to{{opacity:1;transform:translateY(0)}}
}}
.section{{
  animation:fadeUp .4s ease both;
}}
.section:nth-child(1){{animation-delay:.05s}}
.section:nth-child(2){{animation-delay:.1s}}
.section:nth-child(3){{animation-delay:.15s}}
.section:nth-child(4){{animation-delay:.2s}}
.section:nth-child(5){{animation-delay:.25s}}
.section:nth-child(6){{animation-delay:.3s}}
.section:nth-child(7){{animation-delay:.35s}}
</style>
</head>
<body>

<!-- Loading Screen -->
<div id="loading">
  <div class="loading-logo">Visme <span>Analytics</span></div>
  <div class="loading-bar-track"><div class="loading-bar-fill"></div></div>
  <div class="loading-text">Building dashboard…</div>
</div>

<!-- SIDEBAR -->
<nav id="sidebar">
  <div class="sidebar-logo">
    <div class="sidebar-logo-mark">
      <div class="logo-gem">V</div>
      <div>
        <div class="logo-text">Visme</div>
        <div class="logo-sub">Marketing Analytics</div>
      </div>
    </div>
  </div>

  <div class="sidebar-section-label">Overview</div>
  <ul class="sidebar-nav">
    <li><a href="#section-kpi" class="active">
      <svg class="nav-icon" viewBox="0 0 16 16" fill="none"><rect x="1" y="9" width="4" height="6" rx="1" fill="currentColor" opacity=".6"/><rect x="6" y="5" width="4" height="10" rx="1" fill="currentColor" opacity=".8"/><rect x="11" y="1" width="4" height="14" rx="1" fill="currentColor"/></svg>
      KPI Summary
    </a></li>
    <li><a href="#section-sessions">
      <svg class="nav-icon" viewBox="0 0 16 16" fill="none"><polyline points="1,12 5,7 8,10 12,4 15,6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>
      Sessions
    </a></li>
    <li><a href="#section-users">
      <svg class="nav-icon" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="5" r="3" stroke="currentColor" stroke-width="1.4" fill="none"/><path d="M2 14c0-3.3 2.7-6 6-6s6 2.7 6 6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" fill="none"/></svg>
      New Users
    </a></li>
  </ul>

  <div class="sidebar-section-label">Audience</div>
  <ul class="sidebar-nav">
    <li><a href="#section-nvr">
      <svg class="nav-icon" viewBox="0 0 16 16" fill="none"><circle cx="5" cy="5" r="3" stroke="currentColor" stroke-width="1.4" fill="none"/><circle cx="11" cy="5" r="3" stroke="currentColor" stroke-width="1.4" fill="none"/><path d="M1 14c0-2.2 1.8-4 4-4M11 10c2.2 0 4 1.8 4 4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" fill="none"/></svg>
      New vs Returning
    </a></li>
  </ul>

  <div class="sidebar-section-label">Acquisition</div>
  <ul class="sidebar-nav">
    <li><a href="#section-channels">
      <svg class="nav-icon" viewBox="0 0 16 16" fill="none"><path d="M8 1L8 15M1 8L15 8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><circle cx="8" cy="8" r="4" stroke="currentColor" stroke-width="1.4" fill="none"/></svg>
      Traffic Channels
    </a></li>
    <li><a href="#section-geo">
      <svg class="nav-icon" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="6.5" stroke="currentColor" stroke-width="1.4" fill="none"/><path d="M8 1.5C8 1.5 5 5 5 8s3 6.5 3 6.5S11 11 11 8 8 1.5 8 1.5z" stroke="currentColor" stroke-width="1.2" fill="none"/><path d="M1.5 8h13" stroke="currentColor" stroke-width="1.2"/></svg>
      US vs Non-US
    </a></li>
  </ul>

  <div class="sidebar-section-label">Content</div>
  <ul class="sidebar-nav">
    <li><a href="#section-landing">
      <svg class="nav-icon" viewBox="0 0 16 16" fill="none"><rect x="1" y="2" width="14" height="12" rx="2" stroke="currentColor" stroke-width="1.4" fill="none"/><path d="M1 6h14" stroke="currentColor" stroke-width="1.2"/><path d="M5 9h6M5 12h4" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>
      Landing Pages
    </a></li>
  </ul>

  <div class="sidebar-section-label">Conversions</div>
  <ul class="sidebar-nav">
    <li><a href="#section-funnel">
      <svg class="nav-icon" viewBox="0 0 16 16" fill="none"><path d="M2 2h12l-4 6v5l-4-2V8L2 2z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" fill="none"/></svg>
      Conversion Funnel
    </a></li>
  </ul>

  <div class="sidebar-footer">
    Data from Google Analytics 4<br/>
    Property: {PROPERTY_ID}
  </div>
</nav>

<!-- MAIN -->
<div id="main">
  <!-- TOPBAR -->
  <div id="topbar">
    <div class="topbar-title">Marketing <span>Dashboard</span></div>
    <div class="as-of-badge" id="asOfBadge">Data as of —</div>
    <div class="range-control">
      <span class="range-label">Range</span>
      <div class="range-pills" id="rangePills">
        <span class="pill" data-w="8">8W</span>
        <span class="pill active" data-w="13">13W</span>
        <span class="pill" data-w="26">26W</span>
        <span class="pill" data-w="52">52W</span>
        <span class="pill" data-w="104">104W</span>
      </div>
    </div>
  </div>

  <!-- CONTENT -->
  <div id="content">

    <!-- KPI SUMMARY -->
    <div class="section" id="section-kpi">
      <div class="section-header">
        <h2 class="section-title">KPI Summary</h2>
        <span class="section-subtitle" id="kpiSubtitle"></span>
      </div>
      <div class="section-divider"></div>
      <div class="kpi-row">
        <div class="kpi">
          <div class="kpi-label">Total Sessions</div>
          <div class="kpi-value" id="kpi-sessions">—</div>
          <div class="kpi-delta" id="kpi-sessions-delta"></div>
        </div>
        <div class="kpi">
          <div class="kpi-label">New Users</div>
          <div class="kpi-value" id="kpi-newusers">—</div>
          <div class="kpi-delta" id="kpi-newusers-delta"></div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Registrations</div>
          <div class="kpi-value" id="kpi-reg">—</div>
          <div class="kpi-delta" id="kpi-reg-delta"></div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Purchases</div>
          <div class="kpi-value" id="kpi-purchase">—</div>
          <div class="kpi-delta" id="kpi-purchase-delta"></div>
        </div>
      </div>
    </div>

    <!-- SESSIONS -->
    <div class="section" id="section-sessions">
      <div class="section-header">
        <h2 class="section-title">Weekly Sessions</h2>
        <span class="section-subtitle">Current period vs. same period prior year</span>
      </div>
      <div class="section-divider"></div>
      <div class="card">
        <div class="card-title">Total Sessions / Week</div>
        <div class="chart-wrap"><canvas id="chartSessions"></canvas></div>
        <div class="chart-legend">
          <div class="legend-item"><div class="legend-dot" style="background:#3B6FE8"></div> Current Period</div>
          <div class="legend-item"><div class="legend-dot" style="background:#93C5FD;opacity:.6"></div><span style="opacity:.6">Prior Year (dashed)</span></div>
        </div>
      </div>
    </div>

    <!-- NEW USERS -->
    <div class="section" id="section-users">
      <div class="section-header">
        <h2 class="section-title">Weekly New Users</h2>
        <span class="section-subtitle">Current period vs. same period prior year</span>
      </div>
      <div class="section-divider"></div>
      <div class="card">
        <div class="card-title">New Users / Week</div>
        <div class="chart-wrap"><canvas id="chartNewUsers"></canvas></div>
        <div class="chart-legend">
          <div class="legend-item"><div class="legend-dot" style="background:#10B981"></div> Current Period</div>
          <div class="legend-item"><div class="legend-dot" style="background:#6EE7B7;opacity:.6"></div><span style="opacity:.6">Prior Year (dashed)</span></div>
        </div>
      </div>
    </div>

    <!-- NEW vs RETURNING -->
    <div class="section" id="section-nvr">
      <div class="section-header">
        <h2 class="section-title">New vs. Returning Users</h2>
        <span class="section-subtitle">Weekly session composition</span>
      </div>
      <div class="section-divider"></div>
      <div class="card">
        <div class="card-title">Weekly Sessions by User Type</div>
        <div class="chart-wrap"><canvas id="chartNvR"></canvas></div>
        <div class="chart-legend">
          <div class="legend-item"><div class="legend-dot" style="background:#3B6FE8"></div> New</div>
          <div class="legend-item"><div class="legend-dot" style="background:#E63950"></div> Returning</div>
        </div>
      </div>
    </div>

    <!-- CHANNELS -->
    <div class="section" id="section-channels">
      <div class="section-header">
        <h2 class="section-title">Traffic by Channel</h2>
        <span class="section-subtitle">Top 15 channels — click headers to sort</span>
      </div>
      <div class="section-divider"></div>
      <div class="card">
        <div class="tbl-wrap">
          <table id="tblChannels">
            <thead>
              <tr>
                <th>#</th>
                <th onclick="sortTable('channels',0)" data-col="0">Channel</th>
                <th onclick="sortTable('channels',1)" data-col="1" class="sort-desc">Current Sessions</th>
                <th onclick="sortTable('channels',2)" data-col="2">YoY Sessions</th>
                <th onclick="sortTable('channels',3)" data-col="3">Change</th>
                <th>Share</th>
              </tr>
            </thead>
            <tbody id="tbodyChannels"></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- GEO -->
    <div class="section" id="section-geo">
      <div class="section-header">
        <h2 class="section-title">US vs. Non-US Traffic</h2>
        <span class="section-subtitle">Weekly geographic split with YoY overlay</span>
      </div>
      <div class="section-divider"></div>
      <div class="cards-row cards-2">
        <div class="card">
          <div class="card-title">United States Sessions / Week</div>
          <div class="chart-wrap-sm"><canvas id="chartUS"></canvas></div>
        </div>
        <div class="card">
          <div class="card-title">Non-US Sessions / Week</div>
          <div class="chart-wrap-sm"><canvas id="chartNonUS"></canvas></div>
        </div>
      </div>
    </div>

    <!-- LANDING PAGES -->
    <div class="section" id="section-landing">
      <div class="section-header">
        <h2 class="section-title">Top 10 Landing Pages</h2>
        <span class="section-subtitle">Current period vs. prior period — click headers to sort</span>
      </div>
      <div class="section-divider"></div>
      <div class="card">
        <div class="tbl-wrap">
          <table id="tblLanding">
            <thead>
              <tr>
                <th>#</th>
                <th onclick="sortTable('landing',0)" data-col="0">Landing Page</th>
                <th onclick="sortTable('landing',1)" data-col="1" class="sort-desc">Sessions (Current)</th>
                <th onclick="sortTable('landing',2)" data-col="2">New Users</th>
                <th onclick="sortTable('landing',3)" data-col="3">Bounce Rate</th>
                <th>Traffic Share</th>
              </tr>
            </thead>
            <tbody id="tbodyLanding"></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- FUNNEL -->
    <div class="section" id="section-funnel">
      <div class="section-header">
        <h2 class="section-title">Conversion Funnel</h2>
        <span class="section-subtitle">Weekly event rate per session — current vs. prior year</span>
      </div>
      <div class="section-divider"></div>
      <div class="funnel-grid">
        <div class="funnel-card">
          <div class="funnel-event-name">Step 1</div>
          <div class="funnel-title">Account Registrations / Session</div>
          <div class="chart-wrap-sm"><canvas id="chartFunnel1"></canvas></div>
        </div>
        <div class="funnel-card">
          <div class="funnel-event-name" style="color:#F59E0B">Step 2</div>
          <div class="funnel-title">Payment Page Visits / Session</div>
          <div class="chart-wrap-sm"><canvas id="chartFunnel2"></canvas></div>
        </div>
        <div class="funnel-card">
          <div class="funnel-event-name" style="color:#10B981">Step 3</div>
          <div class="funnel-title">Purchases / Session</div>
          <div class="chart-wrap-sm"><canvas id="chartFunnel3"></canvas></div>
        </div>
      </div>
    </div>

  </div><!-- /content -->
</div><!-- /main -->

<script>
// ── EMBEDDED DATA ──────────────────────────────────────────────────────────────
const GA4 = {DATA_JSON};

// ── STATE ─────────────────────────────────────────────────────────────────────
let activeWeeks = 13;
const charts = {{}};
let channelSortCol = 1, channelSortAsc = false;
let landingSortCol = 1, landingSortAsc = false;

// ── INIT ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {{
  document.getElementById('asOfBadge').textContent = 'Data as of ' + GA4.asOfDate;
  buildAllCharts();
  setupPills();
  setupNavHighlight();
  setTimeout(() => document.getElementById('loading').classList.add('hide'), 1400);
}});

function setupPills() {{
  document.querySelectorAll('.pill').forEach(p => {{
    p.addEventListener('click', () => {{
      document.querySelectorAll('.pill').forEach(x => x.classList.remove('active'));
      p.classList.add('active');
      activeWeeks = parseInt(p.dataset.w);
      updateAll();
    }});
  }});
}}

function setupNavHighlight() {{
  const links = document.querySelectorAll('.sidebar-nav a');
  const sections = document.querySelectorAll('.section[id]');
  const obs = new IntersectionObserver(entries => {{
    entries.forEach(e => {{
      if (e.isIntersecting) {{
        links.forEach(l => l.classList.remove('active'));
        const link = document.querySelector('.sidebar-nav a[href="#' + e.target.id + '"]');
        if (link) link.classList.add('active');
      }}
    }});
  }}, {{threshold: 0.3}});
  sections.forEach(s => obs.observe(s));
}}

// ── DATA SLICING ──────────────────────────────────────────────────────────────
function getSlice(n) {{
  // Current = last n weeks; Prior = same n weeks, 52 weeks back
  const allW = GA4.weeks;
  const total = allW.length;
  const currEnd   = total;
  const currStart = Math.max(0, total - n);
  const prevEnd   = Math.max(0, total - 52);
  const prevStart = Math.max(0, total - 52 - n);
  return {{
    curr: allW.slice(currStart, currEnd),
    prev: allW.slice(prevStart, prevEnd),
    currLabels: GA4.weekLabels.slice(currStart, currEnd),
    prevLabels: GA4.weekLabels.slice(prevStart, prevEnd),
  }};
}}

function sumMetric(weeks, obj) {{
  return weeks.reduce((a, w) => a + (obj[w] || 0), 0);
}}

// ── UPDATE ALL ────────────────────────────────────────────────────────────────
function updateAll() {{
  updateKPIs();
  updateChart_Sessions();
  updateChart_NewUsers();
  updateChart_NvR();
  updateTable_Channels();
  updateChart_Geo();
  updateTable_Landing();
  updateChart_Funnels();
}}

// ── KPIs ──────────────────────────────────────────────────────────────────────
function fmtNum(n) {{
  if (n >= 1e6) return (n/1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(1) + 'K';
  return n.toLocaleString();
}}
function fmtDelta(curr, prev, id) {{
  const el = document.getElementById(id);
  if (!el || prev === 0) {{ if(el) el.innerHTML=''; return; }}
  const pct = ((curr - prev) / prev * 100);
  const sign = pct >= 0 ? '+' : '';
  const cls  = pct >= 0 ? 'delta-up' : 'delta-down';
  const arrow = pct >= 0 ? '↑' : '↓';
  el.innerHTML = `<span class="${{cls}}">${{arrow}} ${{sign}}${{pct.toFixed(1)}}%</span> <span style="color:var(--text-dim);font-size:10.5px">vs prior year</span>`;
}}
function updateKPIs() {{
  const s = getSlice(activeWeeks);
  const n = activeWeeks;
  const subtitle = `Last ${{n}} weeks vs. same ${{n}} weeks prior year`;
  document.getElementById('kpiSubtitle').textContent = subtitle;

  const cSess = sumMetric(s.curr, GA4.sessions);
  const pSess = sumMetric(s.prev, GA4.sessions);
  document.getElementById('kpi-sessions').textContent = fmtNum(cSess);
  fmtDelta(cSess, pSess, 'kpi-sessions-delta');

  const cNU = sumMetric(s.curr, GA4.newUsers);
  const pNU = sumMetric(s.prev, GA4.newUsers);
  document.getElementById('kpi-newusers').textContent = fmtNum(cNU);
  fmtDelta(cNU, pNU, 'kpi-newusers-delta');

  const cReg = s.curr.reduce((a,w)=>a+(GA4.events[w]?.create_an_account||0),0);
  const pReg = s.prev.reduce((a,w)=>a+(GA4.events[w]?.create_an_account||0),0);
  document.getElementById('kpi-reg').textContent = fmtNum(cReg);
  fmtDelta(cReg, pReg, 'kpi-reg-delta');

  const cPur = s.curr.reduce((a,w)=>a+(GA4.events[w]?.purchase||0),0);
  const pPur = s.prev.reduce((a,w)=>a+(GA4.events[w]?.purchase||0),0);
  document.getElementById('kpi-purchase').textContent = fmtNum(cPur);
  fmtDelta(cPur, pPur, 'kpi-purchase-delta');
}}

// ── CHART HELPERS ─────────────────────────────────────────────────────────────
const CHART_DEFAULTS = {{
  responsive: true,
  maintainAspectRatio: false,
  interaction: {{ mode: 'index', intersect: false }},
  plugins: {{
    legend: {{ display: false }},
    tooltip: {{
      backgroundColor: '#0E1117',
      titleColor: 'rgba(255,255,255,.9)',
      bodyColor: 'rgba(255,255,255,.7)',
      padding: 12,
      cornerRadius: 8,
      titleFont: {{ family: 'Outfit', size: 12, weight: '600' }},
      bodyFont: {{ family: 'Outfit', size: 11.5 }},
    }}
  }},
  scales: {{
    x: {{
      grid: {{ display: false }},
      ticks: {{ color: '#9CA3AF', font: {{ family:'Outfit', size:10.5 }}, maxRotation:45, autoSkipPadding:12 }},
      border: {{ display: false }},
    }},
    y: {{
      grid: {{ color: '#F0EFE9', lineWidth:1 }},
      ticks: {{ color:'#9CA3AF', font:{{family:'Outfit',size:10.5}}, callback: v => fmtNum(v) }},
      border: {{ display:false }},
    }}
  }},
}};

function makeLineDS(label, data, color, dashed=false) {{
  return {{
    label, data,
    borderColor: color,
    backgroundColor: dashed ? 'transparent' : color + '14',
    borderWidth: dashed ? 1.5 : 2,
    borderDash: dashed ? [4,3] : [],
    pointRadius: data.length > 40 ? 0 : 3,
    pointHoverRadius: 5,
    pointBackgroundColor: color,
    fill: !dashed,
    tension: 0.35,
  }};
}}

function initChart(id, type, data, opts) {{
  const ctx = document.getElementById(id);
  if (!ctx) return;
  if (charts[id]) {{ charts[id].destroy(); }}
  charts[id] = new Chart(ctx, {{ type, data, options: {{...CHART_DEFAULTS, ...opts}} }});
}}
function updateChartData(id, datasets, labels) {{
  const ch = charts[id];
  if (!ch) return;
  ch.data.labels = labels;
  ch.data.datasets = datasets;
  ch.update('active');
}}

// ── SESSIONS CHART ────────────────────────────────────────────────────────────
function buildAllCharts() {{
  initChart('chartSessions', 'line', {{labels:[],datasets:[]}}, {{}});
  initChart('chartNewUsers', 'line', {{labels:[],datasets:[]}}, {{}});
  initChart('chartNvR',      'line', {{labels:[],datasets:[]}}, {{}});
  initChart('chartUS',       'line', {{labels:[],datasets:[]}}, {{plugins:{{legend:{{display:false}}}}, scales:{{y:{{ticks:{{callback:v=>fmtNum(v)}}}}}}}});
  initChart('chartNonUS',    'line', {{labels:[],datasets:[]}}, {{plugins:{{legend:{{display:false}}}}, scales:{{y:{{ticks:{{callback:v=>fmtNum(v)}}}}}}}});
  initChart('chartFunnel1',  'line', {{labels:[],datasets:[]}}, {{scales:{{y:{{ticks:{{callback:v=>v.toFixed(3)}}}}}}}});
  initChart('chartFunnel2',  'line', {{labels:[],datasets:[]}}, {{scales:{{y:{{ticks:{{callback:v=>v.toFixed(3)}}}}}}}});
  initChart('chartFunnel3',  'line', {{labels:[],datasets:[]}}, {{scales:{{y:{{ticks:{{callback:v=>v.toFixed(4)}}}}}}}});
  updateAll();
}}

function updateChart_Sessions() {{
  const s = getSlice(activeWeeks);
  const currData = s.curr.map(w => GA4.sessions[w]||0);
  const prevData = s.prev.map(w => GA4.sessions[w]||0);
  updateChartData('chartSessions', [
    makeLineDS('Current', currData, '#3B6FE8'),
    makeLineDS('Prior Year', prevData, '#93C5FD', true),
  ], s.currLabels);
}}

function updateChart_NewUsers() {{
  const s = getSlice(activeWeeks);
  const currData = s.curr.map(w => GA4.newUsers[w]||0);
  const prevData = s.prev.map(w => GA4.newUsers[w]||0);
  updateChartData('chartNewUsers', [
    makeLineDS('Current', currData, '#10B981'),
    makeLineDS('Prior Year', prevData, '#6EE7B7', true),
  ], s.currLabels);
}}

function updateChart_NvR() {{
  const s = getSlice(activeWeeks);
  const newD = s.curr.map(w => GA4.nvr[w]?.new||0);
  const retD = s.curr.map(w => GA4.nvr[w]?.returning||0);
  updateChartData('chartNvR', [
    makeLineDS('New', newD, '#3B6FE8'),
    makeLineDS('Returning', retD, '#E63950'),
  ], s.currLabels);
}}

// ── CHANNELS TABLE ────────────────────────────────────────────────────────────
function updateTable_Channels() {{
  const s = getSlice(activeWeeks);
  const rows = GA4.topChannels.map(ch => {{
    const curr = s.curr.reduce((a,w)=>a+(GA4.channels[w]?.[ch]||0),0);
    const prev = s.prev.reduce((a,w)=>a+(GA4.channels[w]?.[ch]||0),0);
    const chg  = prev > 0 ? ((curr-prev)/prev*100) : null;
    return {{ ch, curr, prev, chg }};
  }});

  // sort
  rows.sort((a,b) => {{
    let av, bv;
    if (channelSortCol===0) {{ av=a.ch; bv=b.ch; return channelSortAsc ? av.localeCompare(bv) : bv.localeCompare(av); }}
    if (channelSortCol===1) {{ av=a.curr; bv=b.curr; }}
    if (channelSortCol===2) {{ av=a.prev; bv=b.prev; }}
    if (channelSortCol===3) {{ av=a.chg??-Infinity; bv=b.chg??-Infinity; }}
    return channelSortAsc ? av-bv : bv-av;
  }});

  const maxSess = Math.max(...rows.map(r=>r.curr), 1);
  const totalSess = rows.reduce((a,r)=>a+r.curr,0)||1;
  const tbody = document.getElementById('tbodyChannels');
  tbody.innerHTML = rows.map((r,i) => `
    <tr>
      <td><span class="rank-badge">${{i+1}}</span></td>
      <td><strong style="font-size:13px">${{r.ch||'(none)'}}</strong></td>
      <td class="num-cell">${{r.curr.toLocaleString()}}</td>
      <td class="num-cell">${{r.prev.toLocaleString()}}</td>
      <td class="num-cell">${{r.chg===null?'—':`<span class="${{r.chg>=0?'change-pos':'change-neg'}}">${{r.chg>=0?'+':''}}${{r.chg.toFixed(1)}}%</span>`}}</td>
      <td class="bar-cell" style="min-width:120px">
        <div style="display:flex;align-items:center;gap:8px">
          <div class="mini-bar" style="width:${{Math.round(r.curr/maxSess*100)}}%"></div>
          <span style="font-size:11px;color:var(--text-dim)">${{(r.curr/totalSess*100).toFixed(1)}}%</span>
        </div>
      </td>
    </tr>`).join('');
  updateSortHeaders('tblChannels', channelSortCol, channelSortAsc);
}}

// ── GEO CHARTS ────────────────────────────────────────────────────────────────
function updateChart_Geo() {{
  const s = getSlice(activeWeeks);
  const usC  = s.curr.map(w=>GA4.geo[w]?.us||0);
  const usP  = s.prev.map(w=>GA4.geo[w]?.us||0);
  const nuC  = s.curr.map(w=>GA4.geo[w]?.nonUs||0);
  const nuP  = s.prev.map(w=>GA4.geo[w]?.nonUs||0);
  updateChartData('chartUS', [
    makeLineDS('US Current', usC, '#3B6FE8'),
    makeLineDS('US Prior Year', usP, '#93C5FD', true),
  ], s.currLabels);
  updateChartData('chartNonUS', [
    makeLineDS('Non-US Current', nuC, '#E63950'),
    makeLineDS('Non-US Prior Year', nuP, '#FCA5A5', true),
  ], s.currLabels);
}}

// ── LANDING PAGES TABLE ──────────────────────────────────────────────────────
function updateTable_Landing() {{
  const rows = [...GA4.landingPages];
  rows.sort((a,b) => {{
    let av, bv;
    if (landingSortCol===0)  {{ av=a.page; bv=b.page; return landingSortAsc?av.localeCompare(bv):bv.localeCompare(av); }}
    if (landingSortCol===1)  {{ av=a.sessions; bv=b.sessions; }}
    if (landingSortCol===2)  {{ av=a.newUsers; bv=b.newUsers; }}
    if (landingSortCol===3)  {{ av=a.bounceRate; bv=b.bounceRate; }}
    return landingSortAsc?av-bv:bv-av;
  }});
  const maxS = Math.max(...rows.map(r=>r.sessions),1);
  const totalS = rows.reduce((a,r)=>a+r.sessions,0)||1;
  const tbody = document.getElementById('tbodyLanding');
  tbody.innerHTML = rows.slice(0,10).map((r,i) => `
    <tr>
      <td><span class="rank-badge">${{i+1}}</span></td>
      <td class="page-cell" title="${{r.page}}">${{r.page}}</td>
      <td class="num-cell">${{r.sessions.toLocaleString()}}</td>
      <td class="num-cell">${{r.newUsers.toLocaleString()}}</td>
      <td class="num-cell">${{r.bounceRate}}%</td>
      <td class="bar-cell" style="min-width:120px">
        <div style="display:flex;align-items:center;gap:8px">
          <div class="mini-bar" style="width:${{Math.round(r.sessions/maxS*100)}}%"></div>
          <span style="font-size:11px;color:var(--text-dim)">${{(r.sessions/totalS*100).toFixed(1)}}%</span>
        </div>
      </td>
    </tr>`).join('');
  updateSortHeaders('tblLanding', landingSortCol, landingSortAsc);
}}

// ── FUNNEL CHARTS ─────────────────────────────────────────────────────────────
function updateChart_Funnels() {{
  const s = getSlice(activeWeeks);
  const rate = (weeks, evt) => weeks.map(w => {{
    const sess = GA4.sessions[w]||0;
    const ev   = GA4.events[w]?.[evt]||0;
    return sess > 0 ? parseFloat((ev/sess).toFixed(4)) : 0;
  }});
  const rC1 = rate(s.curr,'create_an_account');
  const rP1 = rate(s.prev,'create_an_account');
  const rC2 = rate(s.curr,'visit_payment_page');
  const rP2 = rate(s.prev,'visit_payment_page');
  const rC3 = rate(s.curr,'purchase');
  const rP3 = rate(s.prev,'purchase');
  updateChartData('chartFunnel1',[makeLineDS('Current',rC1,'#E63950'),makeLineDS('Prior Year',rP1,'#FCA5A5',true)],s.currLabels);
  updateChartData('chartFunnel2',[makeLineDS('Current',rC2,'#F59E0B'),makeLineDS('Prior Year',rP2,'#FCD34D',true)],s.currLabels);
  updateChartData('chartFunnel3',[makeLineDS('Current',rC3,'#10B981'),makeLineDS('Prior Year',rP3,'#6EE7B7',true)],s.currLabels);
}}

// ── TABLE SORT ────────────────────────────────────────────────────────────────
function sortTable(which, col) {{
  if (which==='channels') {{
    if (channelSortCol===col) channelSortAsc=!channelSortAsc;
    else {{ channelSortCol=col; channelSortAsc=false; }}
    updateTable_Channels();
  }} else {{
    if (landingSortCol===col) landingSortAsc=!landingSortAsc;
    else {{ landingSortCol=col; landingSortAsc=false; }}
    updateTable_Landing();
  }}
}}
function updateSortHeaders(tableId, sortCol, asc) {{
  const ths = document.querySelectorAll(`#${{tableId}} thead th`);
  ths.forEach((th,i) => {{
    th.classList.remove('sort-asc','sort-desc');
    if (parseInt(th.dataset.col)===sortCol) th.classList.add(asc?'sort-asc':'sort-desc');
  }});
}}
</script>
</body>
</html>"""

# ─── WRITE HTML ───────────────────────────────────────────────────────────────
HTML = HTML.replace("{DATA_JSON}", DATA_JSON)

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    f.write(HTML)

size_kb = os.path.getsize(OUTPUT_FILE) / 1024
print(f"\n✅  Dashboard written to:")
print(f"   {OUTPUT_FILE}")
print(f"   Size: {size_kb:.0f} KB")
if not CI:
    print(f"\n🚀  Opening in browser…")
    import webbrowser
    webbrowser.open(f"file:///{OUTPUT_FILE.replace(chr(92), '/')}")
