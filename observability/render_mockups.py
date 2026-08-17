#!/usr/bin/env python3
"""Render the dashboard mockups used in the README.

The Grafana definitions in observability/dashboards/*.json are the real
artifact; they cannot be screenshotted without a deployed Grafana and a
reporting fleet, neither of which exists. This script reproduces the same panel
set as static HTML so the README can show what the boards look like.

Data is synthetic and every board is stamped MOCKUP, because an unlabeled
dashboard screenshot is indistinguishable from a claim that a system is running.

Usage:
    python observability/render_mockups.py            # writes dashboards.html
    # then, with any Chromium:
    chrome --headless --force-device-scale-factor=2 --window-size=1600,1600 \
        --screenshot=docs/images/fleet-health.png file://$PWD/fleet-health.html

Panels mirror observability/dashboards/*.json.

Palette is the validated dark set: series slots 1-3 (blue/orange/aqua) which
pass all-pairs CVD separation and 3:1 contrast on the dark surface, plus the
reserved status colours. Status colour never carries meaning alone; every
threshold mark has a text label.
"""

from __future__ import annotations

import math
from pathlib import Path

OUT = Path(__file__).with_name("dashboards.html")

# Keep in sync with observability/dashboards/*.json when panels change.

SURFACE = "#1a1a19"
PLANE = "#0d0d0d"
INK = "#ffffff"
INK2 = "#c3c2b7"
MUTED = "#898781"
GRID = "#2c2c2a"
AXIS = "#383835"

# Categorical slots 1-3, dark steps.
S1, S2, S3 = "#3987e5", "#d95926", "#199e70"
# Sequential blue ramp for the ordinal funnel (starts at step 250 for contrast).
RAMP = ["#86b6ef", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95"]
# Reserved status palette.
GOOD, WARN, SERIOUS, CRIT = "#0ca30c", "#fab219", "#ec835a", "#d03b3b"

W, H = 1600, None  # width fixed; height flows


def series(n: int, base: float, amp: float, phase: float, drift: float = 0.0, seed: int = 1):
    """Deterministic pseudo-signal. No RNG so renders are reproducible."""
    out = []
    for i in range(n):
        x = i / n
        v = (
            base
            + amp * math.sin(6.3 * x + phase)
            + 0.45 * amp * math.sin(17.0 * x + phase * 2.1 + seed)
            + drift * x
        )
        out.append(v)
    return out


def path_for(vals, x0, y0, w, h, vmin, vmax):
    span = (vmax - vmin) or 1
    pts = []
    for i, v in enumerate(vals):
        x = x0 + w * i / (len(vals) - 1)
        y = y0 + h - h * (v - vmin) / span
        pts.append(f"{x:.1f},{y:.1f}")
    return "M" + " L".join(pts)


def area_for(vals_hi, vals_lo, x0, y0, w, h, vmin, vmax):
    span = (vmax - vmin) or 1
    up, dn = [], []
    for i, v in enumerate(vals_hi):
        x = x0 + w * i / (len(vals_hi) - 1)
        up.append(f"{x:.1f},{y0 + h - h * (v - vmin) / span:.1f}")
    for i in range(len(vals_lo) - 1, -1, -1):
        x = x0 + w * i / (len(vals_lo) - 1)
        dn.append(f"{x:.1f},{y0 + h - h * (vals_lo[i] - vmin) / span:.1f}")
    return "M" + " L".join(up + dn) + " Z"


def grid_lines(x0, y0, w, h, vmin, vmax, steps, fmt="{:.0f}"):
    out = []
    for i in range(steps + 1):
        v = vmin + (vmax - vmin) * i / steps
        y = y0 + h - h * i / steps
        out.append(
            f'<line x1="{x0}" y1="{y:.1f}" x2="{x0 + w}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>'
        )
        out.append(
            f'<text x="{x0 - 10}" y="{y + 4:.1f}" fill="{MUTED}" font-size="12" '
            f'text-anchor="end" font-family="system-ui">{fmt.format(v)}</text>'
        )
    return "\n".join(out)


def time_labels(x0, y0, w, labels):
    out = []
    for i, lab in enumerate(labels):
        x = x0 + w * i / (len(labels) - 1)
        out.append(
            f'<text x="{x:.1f}" y="{y0 + 20}" fill="{MUTED}" font-size="12" '
            f'text-anchor="middle" font-family="system-ui">{lab}</text>'
        )
    return "\n".join(out)


def stat(title, value, sub, colour, note=""):
    return f"""
    <div class="stat">
      <div class="stat-title">{title}</div>
      <div class="stat-value" style="color:{colour}">{value}</div>
      <div class="stat-sub">{sub}</div>
      {f'<div class="stat-note">{note}</div>' if note else ""}
    </div>"""


def legend(items):
    li = "".join(f'<span class="lg"><i style="background:{c}"></i>{n}</span>' for n, c in items)
    return f'<div class="legend">{li}</div>'


# ---------------------------------------------------------------- fleet health


def fleet_health() -> str:
    n = 96
    x0, y0, w, h = 56, 24, 640, 210
    canary = series(n, 34, 3.0, 0.4, drift=2.0, seed=1)
    pilot = series(n, 28, 2.2, 1.9, seed=2)
    prod = series(n, 25, 1.8, 3.1, seed=3)
    vmin, vmax = 15, 50
    thr_y = y0 + h - h * (45 - vmin) / (vmax - vmin)

    latency = f"""
    <svg viewBox="0 0 720 280" width="100%">
      {grid_lines(x0, y0, w, h, vmin, vmax, 5)}
      <line x1="{x0}" y1="{thr_y:.1f}" x2="{x0 + w}" y2="{thr_y:.1f}"
            stroke="{CRIT}" stroke-width="2" stroke-dasharray="6 5"/>
      <text x="{x0 + w - 4}" y="{thr_y - 8:.1f}" fill="{CRIT}" font-size="12"
            text-anchor="end" font-family="system-ui">45 ms budget</text>
      <path d="{path_for(prod, x0, y0, w, h, vmin, vmax)}" fill="none" stroke="{S3}" stroke-width="2"/>
      <path d="{path_for(pilot, x0, y0, w, h, vmin, vmax)}" fill="none" stroke="{S2}" stroke-width="2"/>
      <path d="{path_for(canary, x0, y0, w, h, vmin, vmax)}" fill="none" stroke="{S1}" stroke-width="2"/>
      {time_labels(x0, y0 + h, w, ["-24h", "-18h", "-12h", "-6h", "now"])}
      <text x="{x0 - 44}" y="14" fill="{MUTED}" font-size="12" font-family="system-ui">ms</text>
    </svg>"""

    det = series(n, 0.31, 0.05, 1.2, seed=5)
    hi = [0.31 + 0.11] * n
    lo = [0.31 - 0.11] * n
    dmin, dmax = 0.10, 0.55
    personnel = f"""
    <svg viewBox="0 0 720 280" width="100%">
      {grid_lines(x0, y0, w, h, dmin, dmax, 5, "{:.2f}")}
      <path d="{area_for(hi, lo, x0, y0, w, h, dmin, dmax)}" fill="{MUTED}" fill-opacity="0.10"/>
      <path d="{path_for(hi, x0, y0, w, h, dmin, dmax)}" fill="none" stroke="{MUTED}"
            stroke-width="1" stroke-dasharray="6 6"/>
      <path d="{path_for(lo, x0, y0, w, h, dmin, dmax)}" fill="none" stroke="{MUTED}"
            stroke-width="1" stroke-dasharray="6 6"/>
      <path d="{path_for(det, x0, y0, w, h, dmin, dmax)}" fill="none" stroke="{S1}" stroke-width="2"/>
      <text x="{x0 + w - 4}" y="{y0 + 16}" fill="{MUTED}" font-size="12"
            text-anchor="end" font-family="system-ui">ring baseline +/- 3 sigma</text>
      {time_labels(x0, y0 + h, w, ["-24h", "-18h", "-12h", "-6h", "now"])}
      <text x="{x0 - 44}" y="14" fill="{MUTED}" font-size="12" font-family="system-ui">/km</text>
    </svg>"""

    sites = [
        ("alpha", 0.997),
        ("bravo", 0.994),
        ("charlie", 0.988),
        ("delta", 0.999),
        ("echo", 0.982),
        ("foxtrot", 0.996),
    ]
    rows = []
    for name, v in sites:
        c = GOOD if v >= 0.995 else WARN if v >= 0.99 else SERIOUS
        label = "ok" if v >= 0.995 else "watch" if v >= 0.99 else "degraded"
        frac = (v - 0.97) / 0.03
        rows.append(f"""
        <div class="bar-row">
          <div class="bar-label">{name}</div>
          <div class="bar-track"><div class="bar-fill" style="width:{frac * 100:.1f}%;background:{c}"></div></div>
          <div class="bar-val">{v * 100:.1f}%<span class="bar-tag" style="color:{c}">{label}</span></div>
        </div>""")
    thermal = "".join(rows)

    return f"""
  <section class="board">
    <header>
      <div><h1>edgeforge: Fleet Health</h1>
        <p>Is the fleet safe right now? On-call and ops supervisors. Refresh 1m.</p></div>
      <div class="mock">MOCKUP: synthetic data</div>
    </header>

    <div class="row four">
      {stat("Invariant breaches (24h)", "0", "A1 + A2 + A3", GOOD, "clear")}
      {stat("Perception availability", "99.62%", "B1, objective 99.5%, 28d", GOOD, "budget 76% left")}
      {stat("Devices on intended bundle", "99.4%", "B5, objective 99.0%", GOOD, "39 of 40 online")}
      {stat("Active rollbacks", "0", "none in flight", INK2)}
    </div>

    <div class="row two">
      <div class="panel">
        <h2>Inference latency p99 by ring</h2>
        <p class="cap">B2. One axis. The 45 ms line is the perception budget, not a warning level.</p>
        {latency}
        {legend([("canary", S1), ("pilot", S2), ("production", S3)])}
      </div>
      <div class="panel">
        <h2>Personnel detections per km vs ring baseline</h2>
        <p class="cap">Watched in both directions: a collapse means blindness, a spike means nuisance stops.</p>
        {personnel}
        {legend([("canary observed", S1), ("baseline band", MUTED)])}
      </div>
    </div>

    <div class="row two">
      <div class="panel">
        <h2>Thermal headroom by site (28d)</h2>
        <p class="cap">B4. Kept separate from latency: throttling makes every latency figure untrustworthy.</p>
        <div class="bars">{thermal}</div>
      </div>
      <div class="panel">
        <h2>SLO status: fleet group</h2>
        <p class="cap">Written by slo.py. Also the non-colour reading of every threshold on this page.</p>
        <table>
          <tr><th>ID</th><th>Indicator</th><th>Observed</th><th>Objective</th><th>Budget</th><th>Status</th></tr>
          <tr><td>B1</td><td>perception_availability</td><td>99.620%</td><td>99.500%</td><td>76.0%</td><td style="color:{GOOD}">meeting</td></tr>
          <tr><td>B2</td><td>inference_latency</td><td>99.140%</td><td>99.000%</td><td>14.0%</td><td style="color:{WARN}">AT RISK</td></tr>
          <tr><td>B3</td><td>frame_completeness</td><td>99.810%</td><td>99.500%</td><td>62.0%</td><td style="color:{GOOD}">meeting</td></tr>
          <tr><td>B4</td><td>thermal_headroom</td><td>99.260%</td><td>99.000%</td><td>26.0%</td><td style="color:{GOOD}">meeting</td></tr>
          <tr><td>B5</td><td>bundle_convergence</td><td>99.400%</td><td>99.000%</td><td>40.0%</td><td style="color:{GOOD}">meeting</td></tr>
          <tr><td>B6</td><td>rollback_latency</td><td>100.000%</td><td>99.000%</td><td>100.0%</td><td style="color:{GOOD}">meeting</td></tr>
          <tr><td>B7</td><td>model_freshness</td><td>97.500%</td><td>95.000%</td><td>50.0%</td><td style="color:{GOOD}">meeting</td></tr>
        </table>
      </div>
    </div>
  </section>"""


# -------------------------------------------------------------- pipeline health


def pipeline_health() -> str:
    stages = [
        ("captured", 41_500_000),
        ("uploaded", 498_000),
        ("quality passed", 398_000),
        ("deduped", 139_000),
        ("curated", 132_000),
        ("routed to human", 38_300),
        ("in snapshot", 180_000),
    ]
    top = stages[0][1]
    rows = []
    for i, (name, v) in enumerate(stages):
        frac = math.log10(v) / math.log10(top)
        pct = 100.0 * v / top
        rows.append(f"""
        <div class="bar-row">
          <div class="bar-label wide">{name}</div>
          <div class="bar-track"><div class="bar-fill" style="width:{frac * 100:.1f}%;background:{RAMP[i]}"></div></div>
          <div class="bar-val">{v:,}<span class="bar-tag">{pct:.2f}% of captured</span></div>
        </div>""")
    funnel = "".join(rows)

    buckets = [
        (4, 1),
        (5, 2),
        (6, 5),
        (7, 9),
        (8, 14),
        (9, 17),
        (10, 15),
        (11, 11),
        (12, 7),
        (13, 4),
        (14, 2),
        (15, 1),
    ]
    bx0, by0, bw, bh = 56, 24, 620, 190
    top_n = max(b[1] for b in buckets)
    bars = []
    for i, (day, cnt) in enumerate(buckets):
        x = bx0 + (bw / len(buckets)) * i + 4
        bwid = bw / len(buckets) - 8
        bhgt = bh * cnt / top_n
        c = S1 if day <= 11 else SERIOUS
        bars.append(
            f'<rect x="{x:.1f}" y="{by0 + bh - bhgt:.1f}" width="{bwid:.1f}" '
            f'height="{bhgt:.1f}" fill="{c}" rx="3"/>'
        )
        bars.append(
            f'<text x="{x + bwid / 2:.1f}" y="{by0 + bh + 18}" fill="{MUTED}" font-size="11" '
            f'text-anchor="middle" font-family="system-ui">{day}</text>'
        )
    thr_x = bx0 + (bw / len(buckets)) * 8
    hist = f"""
    <svg viewBox="0 0 700 275" width="100%">
      {grid_lines(bx0, by0, bw, bh, 0, top_n, 4)}
      {"".join(bars)}
      <line x1="{thr_x:.1f}" y1="{by0}" x2="{thr_x:.1f}" y2="{by0 + bh}"
            stroke="{CRIT}" stroke-width="2" stroke-dasharray="6 5"/>
      <text x="{thr_x + 8:.1f}" y="{by0 + 14}" fill="{CRIT}" font-size="12"
            font-family="system-ui">11-day objective</text>
      <text x="{bx0 + bw / 2:.0f}" y="{by0 + bh + 38}" fill="{MUTED}" font-size="12"
            text-anchor="middle" font-family="system-ui">loop time (days)</text>
    </svg>"""

    n = 72
    depth = series(n, 3800, 900, 0.9, drift=-1400, seed=7)
    aged = series(n, 640, 260, 2.4, drift=-380, seed=9)
    qx0, qy0, qw, qh = 56, 24, 620, 190
    qmax = 5200
    queue = f"""
    <svg viewBox="0 0 700 250" width="100%">
      {grid_lines(qx0, qy0, qw, qh, 0, qmax, 4, "{:,.0f}")}
      <path d="{path_for(depth, qx0, qy0, qw, qh, 0, qmax)}" fill="none" stroke="{S1}" stroke-width="2"/>
      <path d="{path_for(aged, qx0, qy0, qw, qh, 0, qmax)}" fill="none" stroke="{S2}" stroke-width="2"/>
      {time_labels(qx0, qy0 + qh, qw, ["-7d", "-5d", "-3d", "-1d", "now"])}
      <text x="{qx0 - 44}" y="14" fill="{MUTED}" font-size="12" font-family="system-ui">frames</text>
    </svg>"""

    return f"""
  <section class="board">
    <header>
      <div><h1>edgeforge: Pipeline Health</h1>
        <p>Is the loop turning, and where is it stuck? Platform and ML teams. Refresh 5m.</p></div>
      <div class="mock">MOCKUP: synthetic data</div>
    </header>

    <div class="row four">
      {stat("Uplink yield", "41.2%", "D3, objective 40%", GOOD, "on-robot triage is choosing well")}
      {stat("T0 event delivery", "99.93%", "C1, objective 99.9%", GOOD, "safety events are evidence")}
      {stat("Curation success", "98.6%", "C3, unattended runs only", GOOD)}
      {stat("Reproducibility canary", "100%", "C5, 13 of 13 weekly re-runs", GOOD)}
    </div>

    <div class="row two">
      <div class="panel wide-panel">
        <h2>Frame funnel: captured to snapshot</h2>
        <p class="cap">Log-scaled bars, annotated with retention against captured. ~250x fewer frames
        reach a labeler than the sensors produce. Snapshot exceeds routed because it includes
        synthetic and previously-labeled frames.</p>
        <div class="bars">{funnel}</div>
      </div>
      <div class="panel">
        <h2>Label queue depth and age</h2>
        <p class="cap">C4. The longest pole in the 11-day loop. Both series on one count axis.</p>
        {queue}
        {legend([("queued", S1), ("older than 72h", S2)])}
      </div>
    </div>

    <div class="row two">
      <div class="panel">
        <h2>Loop time distribution vs the 11-day objective</h2>
        <p class="cap">C7. Field condition observed to production rollout, measured from pipeline
        events rather than estimated in a retrospective. 88 loops over 90 days.</p>
        {hist}
        {legend([("within objective", S1), ("over objective", SERIOUS)])}
      </div>
      <div class="panel">
        <h2>SLO status: pipeline group</h2>
        <p class="cap">Written by slo.py. C4 is the indicator to watch: adjudication latency is
        the longest pole in the loop and the one that is staffed, not automated.</p>
        <table>
          <tr><th>ID</th><th>Indicator</th><th>Observed</th><th>Objective</th><th>Budget</th><th>Status</th></tr>
          <tr><td>C1</td><td>t0_event_delivery</td><td>99.930%</td><td>99.900%</td><td>30.0%</td><td style="color:{GOOD}">meeting</td></tr>
          <tr><td>C2</td><td>ingest_freshness</td><td>96.400%</td><td>95.000%</td><td>28.0%</td><td style="color:{GOOD}">meeting</td></tr>
          <tr><td>C3</td><td>curation_success</td><td>98.600%</td><td>98.000%</td><td>30.0%</td><td style="color:{GOOD}">meeting</td></tr>
          <tr><td>C4</td><td>label_queue_latency</td><td>90.800%</td><td>90.000%</td><td>8.0%</td><td style="color:{CRIT}">BREACHING</td></tr>
          <tr><td>C5</td><td>training_reproducibility</td><td>100.000%</td><td>100.000%</td><td>100.0%</td><td style="color:{GOOD}">meeting</td></tr>
          <tr><td>C6</td><td>gate_evidence_completeness</td><td>100.000%</td><td>100.000%</td><td>100.0%</td><td style="color:{GOOD}">meeting</td></tr>
          <tr><td>C7</td><td>loop_time</td><td>84.100%</td><td>80.000%</td><td>20.5%</td><td style="color:{GOOD}">meeting</td></tr>
        </table>
        <p class="cap" style="margin-top:12px">Release posture: <b style="color:{WARN}">CONSTRAINED</b>
        , C4 error budget below 10%. No discretionary edge-plane changes until it recovers.</p>
      </div>
    </div>
  </section>"""


# ------------------------------------------------------------------- efficiency


def efficiency() -> str:
    n = 90
    cost = series(n, 1650, 40, 0.6, drift=-380, seed=11)
    cx0, cy0, cw, ch = 56, 20, 1420, 200
    spark = f"""
    <svg viewBox="0 0 1500 250" width="100%">
      {grid_lines(cx0, cy0, cw, ch, 1100, 1800, 4, "${:,.0f}")}
      <path d="{path_for(cost, cx0, cy0, cw, ch, 1100, 1800)}" fill="none" stroke="{S1}" stroke-width="2"/>
      {time_labels(cx0, cy0 + ch, cw, ["-90d", "-68d", "-45d", "-22d", "now"])}
    </svg>"""

    return f"""
  <section class="board">
    <header>
      <div><h1>edgeforge: Efficiency and Unit Economics</h1>
        <p>What does it cost, and which way is it going? Engineering leadership. Refresh 1h.</p></div>
      <div class="mock">MOCKUP: synthetic data</div>
    </header>

    <div class="row four">
      {stat("Cost per robot-month", "$1,271", "D7, 90-day trend", S1, "falling")}
      {stat("Label auto-accept rate", "72.4%", "D1, objective 70%", GOOD, "biggest cost lever")}
      {stat("GPU utilization", "68.1%", "D4, allocated not provisioned", GOOD)}
      {stat("Spot share (interruptible)", "94.0%", "D5, objective 90%", GOOD, "final runs excluded")}
    </div>

    <div class="row one">
      <div class="panel">
        <h2>Cost per robot-month, 90 days</h2>
        <p class="cap">D7. The objective is a direction, not a level: the absolute figure depends on
        fleet size, so a threshold would say nothing. Slope must not be positive.</p>
        {spark}
      </div>
    </div>
  </section>"""


CSS = f"""
* {{ box-sizing: border-box; }}
body {{ margin:0; background:{PLANE}; color:{INK};
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }}
.board {{ width:{W}px; background:{PLANE}; padding:28px 32px 34px; }}
header {{ display:flex; justify-content:space-between; align-items:flex-start;
  border-bottom:1px solid {AXIS}; padding-bottom:16px; margin-bottom:20px; }}
h1 {{ margin:0 0 6px; font-size:24px; font-weight:650; letter-spacing:-0.2px; }}
header p {{ margin:0; color:{INK2}; font-size:14px; }}
.mock {{ color:{WARN}; border:1px solid {WARN}; border-radius:5px;
  padding:5px 11px; font-size:12px; font-weight:600; letter-spacing:0.4px; white-space:nowrap; }}
.row {{ display:grid; gap:16px; margin-bottom:16px; }}
.row.four {{ grid-template-columns:repeat(4,1fr); }}
.row.two {{ grid-template-columns:1fr 1fr; }}
.row.one {{ grid-template-columns:1fr; }}
.stat, .panel {{ background:{SURFACE}; border:1px solid {AXIS}; border-radius:9px; padding:16px 18px; }}
.stat-title {{ color:{INK2}; font-size:13px; margin-bottom:8px; }}
.stat-value {{ font-size:34px; font-weight:680; line-height:1.05; letter-spacing:-0.5px; }}
.stat-sub {{ color:{MUTED}; font-size:12px; margin-top:7px; }}
.stat-note {{ color:{INK2}; font-size:12px; margin-top:3px; }}
.panel h2 {{ margin:0 0 4px; font-size:15px; font-weight:620; }}
.cap {{ margin:0 0 12px; color:{MUTED}; font-size:12px; line-height:1.45; }}
.legend {{ margin-top:8px; display:flex; gap:18px; flex-wrap:wrap; }}
.lg {{ color:{INK2}; font-size:12px; display:flex; align-items:center; gap:7px; }}
.lg i {{ width:11px; height:11px; border-radius:2px; display:inline-block; }}
.bars {{ display:flex; flex-direction:column; gap:9px; margin-top:4px; }}
.bar-row {{ display:grid; grid-template-columns:110px 1fr 210px; align-items:center; gap:12px; }}
.bar-label {{ color:{INK2}; font-size:13px; }}
.bar-label.wide {{ width:130px; }}
.bar-track {{ background:{GRID}; height:16px; border-radius:4px; overflow:hidden; }}
.bar-fill {{ height:100%; border-radius:4px; }}
.bar-val {{ color:{INK}; font-size:13px; font-variant-numeric:tabular-nums; }}
.bar-tag {{ color:{MUTED}; font-size:11px; margin-left:9px; }}
table {{ width:100%; border-collapse:collapse; font-size:12.5px; }}
th {{ text-align:left; color:{MUTED}; font-weight:550; padding:6px 8px;
  border-bottom:1px solid {AXIS}; }}
td {{ padding:6px 8px; color:{INK2}; border-bottom:1px solid {GRID};
  font-variant-numeric:tabular-nums; }}
.wide-panel {{ grid-column: span 1; }}
"""


def main() -> None:
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>{CSS}</style></head>
<body>
{fleet_health()}
{pipeline_health()}
{efficiency()}
</body></html>"""
    OUT.write_text(html)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
