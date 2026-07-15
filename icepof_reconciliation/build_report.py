"""Renders the ICEPOF raw-vs-mapped reconciliation report as a standalone HTML file."""

from __future__ import annotations

import html
import json


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _pct(n: int, d: int) -> float:
    return round(100 * n / d, 1) if d else 0.0


def _severity_for_pct(pct: float) -> str:
    if pct >= 15:
        return "critical"
    if pct >= 3:
        return "serious"
    if pct > 0:
        return "watch"
    return "good"


def _field_rows_html(field_stats: dict, total: int) -> str:
    rows = []
    for f, stats in sorted(field_stats.items(), key=lambda kv: -kv[1]["mismatches"]):
        pct = _pct(stats["mismatches"], stats["compared"])
        sev = _severity_for_pct(pct)
        rows.append(f"""
        <tr>
          <td class="field-name">{_esc(f)}</td>
          <td class="num">{stats['mismatches']}/{stats['compared']}</td>
          <td class="num pct-{sev}">{pct:.1f}%</td>
          <td class="bar-cell"><div class="bar-track"><div class="bar-fill bar-{sev}" style="width:{min(pct,100)}%"></div></div></td>
        </tr>""")
    return "".join(rows)


def _example_table(examples: list[dict], field_label: str) -> str:
    if not examples:
        return ""
    rows = "".join(
        f"""<tr><td class="mono">{_esc(e['ORIG_TRAN_ID'])}</td><td class="mono">{_esc(e['expected'])}</td><td class="mono">{_esc(e['actual'])}</td></tr>"""
        for e in examples
    )
    return f"""
    <table class="cmp">
      <thead><tr><th>ORIG_TRAN_ID</th><th>Expected ({_esc(field_label)})</th><th>Actual ({_esc(field_label)})</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def build_html(*, report_date: str, orders_result: dict, trades_result: dict,
               diagnostics: list[dict], unit_currency_finding: dict,
               price_diff_summary: dict, notes: list[str]) -> str:

    orders_fields = orders_result["field_stats"]
    trades_fields = trades_result["field_stats"]

    orders_issue_fields = sum(1 for s in orders_fields.values() if s["mismatches"] > 0)
    trades_issue_fields = sum(1 for s in trades_fields.values() if s["mismatches"] > 0)

    diag_reasons = {}
    for d in diagnostics:
        key = d["reason"].split(":")[0].split(" for ")[0]
        diag_reasons[key] = diag_reasons.get(key, 0) + 1
    diag_html = "".join(f"<li><span class=\"mono\">{_esc(k)}</span>: {v}</li>" for k, v in diag_reasons.items())

    unit_examples_html = _example_table(unit_currency_finding["unit_examples"], "UNIT")
    ccy_examples_html = _example_table(unit_currency_finding["currency_examples"], "CURRENCY")
    price_examples_html = _example_table(price_diff_summary["examples"], "PRICE")

    lifecycle_o = orders_result["lifecycle_mismatches"]
    lifecycle_t = trades_result["lifecycle_mismatches"]
    lifecycle_o_rows = "".join(
        f"<tr><td class='mono'>{_esc(m['ORIG_TRAN_ID'])}</td><td class='num'>{m['expected_rows']}</td><td class='num'>{m['actual_rows']}</td></tr>"
        for m in lifecycle_o[:15]
    )
    lifecycle_t_rows = "".join(
        f"<tr><td class='mono'>{_esc(m['ORIG_TRAN_ID'])}</td><td class='num'>{m['expected_rows']}</td><td class='num'>{m['actual_rows']}</td></tr>"
        for m in lifecycle_t[:15]
    )

    notes_html = "".join(f"<li>{_esc(n)}</li>" for n in notes)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ICEPOF Raw-vs-Mapped Reconciliation — {_esc(report_date)}</title>
<style>
:root {{
  --bg: #F3F4F6; --surface: #FFFFFF; --surface-2: #EAECEF; --ink: #171E27; --muted: #5B6472;
  --accent: #2A8F87; --critical: #C23B3F; --serious: #B8791E; --good: #2E8B57; --border: #D7DAE0;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #10151C; --surface: #171E27; --surface-2: #1D2530; --ink: #E9ECEF; --muted: #8B93A3;
    --accent: #4FBEB5; --critical: #E2585C; --serious: #E0A23D; --good: #4CAF7D; --border: #2B3542;
  }}
}}
:root[data-theme="dark"] {{
  --bg: #10151C; --surface: #171E27; --surface-2: #1D2530; --ink: #E9ECEF; --muted: #8B93A3;
  --accent: #4FBEB5; --critical: #E2585C; --serious: #E0A23D; --good: #4CAF7D; --border: #2B3542;
}}
:root[data-theme="light"] {{
  --bg: #F3F4F6; --surface: #FFFFFF; --surface-2: #EAECEF; --ink: #171E27; --muted: #5B6472;
  --accent: #2A8F87; --critical: #C23B3F; --serious: #B8791E; --good: #2E8B57; --border: #D7DAE0;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; background: var(--bg); color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  font-size: 15px; line-height: 1.6;
}}
.mono {{ font-family: ui-monospace, "Cascadia Code", "SF Mono", Consolas, monospace; font-variant-numeric: tabular-nums; }}
#layout {{ display: flex; min-height: 100vh; }}
#nav {{
  width: 250px; min-width: 250px; background: var(--surface); border-right: 1px solid var(--border);
  position: sticky; top: 0; height: 100vh; overflow-y: auto; padding: 24px 20px;
}}
#nav .brand {{ font-family: ui-monospace, monospace; font-weight: 700; font-size: 14px; letter-spacing: 0.02em; color: var(--accent); margin-bottom: 4px; }}
#nav .sub {{ font-size: 12px; color: var(--muted); margin-bottom: 28px; }}
#nav a {{ display: block; color: var(--muted); text-decoration: none; font-size: 13px; padding: 6px 0; border-bottom: 1px solid transparent; }}
#nav a:hover {{ color: var(--ink); }}
#main {{ flex: 1; padding: 40px 48px 80px; max-width: 1100px; }}
h1 {{ font-family: ui-monospace, monospace; font-size: 28px; letter-spacing: -0.01em; margin: 0 0 6px; text-wrap: balance; }}
h1 .accent {{ color: var(--accent); }}
.lede {{ color: var(--muted); font-size: 14px; margin: 0 0 40px; max-width: 65ch; }}
h2 {{ font-family: ui-monospace, monospace; font-size: 18px; margin: 56px 0 16px; padding-bottom: 8px; border-bottom: 1px solid var(--border); }}
h3 {{ font-size: 16px; margin: 0 0 8px; }}
p {{ max-width: 68ch; }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; margin-bottom: 8px; }}
.tile {{ background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 16px; }}
.tile .val {{ font-family: ui-monospace, monospace; font-size: 26px; font-weight: 700; }}
.tile .lbl {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
.tile.accent-good .val {{ color: var(--good); }}
.tile.accent-critical .val {{ color: var(--critical); }}
.finding {{ background: var(--surface); border: 1px solid var(--border); border-left: 4px solid var(--muted); border-radius: 6px; padding: 18px 20px; margin-bottom: 18px; }}
.finding.sev-critical {{ border-left-color: var(--critical); }}
.finding.sev-serious {{ border-left-color: var(--serious); }}
.finding.sev-watch {{ border-left-color: var(--accent); }}
.badge {{ display: inline-block; font-size: 11px; font-weight: 700; letter-spacing: 0.03em; text-transform: uppercase; padding: 2px 8px; border-radius: 3px; margin-right: 8px; }}
.badge.critical {{ background: color-mix(in srgb, var(--critical) 20%, transparent); color: var(--critical); }}
.badge.serious {{ background: color-mix(in srgb, var(--serious) 20%, transparent); color: var(--serious); }}
.badge.watch {{ background: color-mix(in srgb, var(--accent) 18%, transparent); color: var(--accent); }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; margin: 10px 0; }}
th {{ text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em; color: var(--muted); padding: 6px 10px; border-bottom: 1px solid var(--border); }}
td {{ padding: 6px 10px; border-bottom: 1px solid var(--border); }}
td.num {{ text-align: right; font-family: ui-monospace, monospace; }}
td.field-name {{ font-family: ui-monospace, monospace; }}
.table-wrap {{ overflow-x: auto; }}
.bar-cell {{ width: 160px; }}
.bar-track {{ height: 8px; background: var(--surface-2); border-radius: 4px; overflow: hidden; }}
.bar-fill {{ height: 100%; }}
.bar-good {{ background: var(--good); }}
.bar-watch {{ background: var(--accent); }}
.bar-serious {{ background: var(--serious); }}
.bar-critical {{ background: var(--critical); }}
.pct-good {{ color: var(--good); }}
.pct-watch {{ color: var(--accent); }}
.pct-serious {{ color: var(--serious); }}
.pct-critical {{ color: var(--critical); }}
ul.plain {{ padding-left: 20px; }}
ul.plain li {{ margin-bottom: 6px; color: var(--muted); font-size: 13.5px; }}
code {{ font-family: ui-monospace, monospace; background: var(--surface-2); padding: 1px 5px; border-radius: 3px; font-size: 0.9em; }}
</style>
</head>
<body>
<div id="layout">
  <nav id="nav">
    <div class="brand">ICEPOF RECONCILIATION</div>
    <div class="sub">{_esc(report_date)} · raw FIX vs. CLIENT_ORDERS/TRADES</div>
    <a href="#summary">Executive summary</a>
    <a href="#critical">Critical findings</a>
    <a href="#orders-fields">Orders — field mismatch</a>
    <a href="#trades-fields">Trades — field mismatch</a>
    <a href="#lifecycle">Lifecycle integrity</a>
    <a href="#unresolved">Unresolved / excluded</a>
    <a href="#notes">Methodology &amp; caveats</a>
  </nav>
  <main id="main">
    <h1>ICEPOF Raw <span class="accent">→</span> Mapped Reconciliation</h1>
    <p class="lede">Spec-driven reconstruction of CLIENT_ORDERS / CLIENT_TRADES from raw FIX messages (ICE POF interface), diffed against the actual staging output for {_esc(report_date)}.</p>

    <h2 id="summary">Executive summary</h2>
    <div class="tiles">
      <div class="tile"><div class="val">{orders_result['common_keys']}/{orders_result['expected_keys']}</div><div class="lbl">Order ORIG_TRAN_IDs matched</div></div>
      <div class="tile"><div class="val">{orders_result['aligned_row_pairs']}</div><div class="lbl">Order rows field-compared</div></div>
      <div class="tile accent-critical"><div class="val">{orders_issue_fields}</div><div class="lbl">Order fields with any mismatch</div></div>
      <div class="tile"><div class="val">{trades_result['common_keys']}/{trades_result['expected_keys']}</div><div class="lbl">Trade ORIG_TRAN_IDs matched</div></div>
      <div class="tile"><div class="val">{trades_result['aligned_row_pairs']}</div><div class="lbl">Trade rows field-compared</div></div>
      <div class="tile accent-critical"><div class="val">{trades_issue_fields}</div><div class="lbl">Trade fields with any mismatch</div></div>
    </div>

    <h2 id="critical">Critical findings</h2>

    <div class="finding sev-critical">
      <div><span class="badge critical">Critical</span><h3 style="display:inline">UNIT and CURRENCY silently inherit the previous trade's values</h3></div>
      <p>Every UNIT mismatch ({unit_currency_finding['unit_mismatch_count']} of {trades_result['aligned_row_pairs']} trade rows, {_pct(unit_currency_finding['unit_mismatch_count'], trades_result['aligned_row_pairs'])}%) and every CURRENCY mismatch ({unit_currency_finding['ccy_mismatch_count']}, {_pct(unit_currency_finding['ccy_mismatch_count'], trades_result['aligned_row_pairs'])}%) exactly equals the immediately preceding trade row's value — a 100% correlation across {unit_currency_finding['unit_mismatch_count']} checked cases. A Crude Oil trade (North Sea, IFEU) is landing with <code>UNIT=MWh</code>/<code>CURRENCY=EUR</code> — a Natural Gas / European-power unit and currency — while its COMMODITY, COUNTRY, MARKET_AREA and MARKET_PLACE are all correctly derived. This points to shared, un-reset mutable state carried across execution reports (consistent with the code-review finding on <code>MapperBase.cs:45</code>, "mutable per-call state on shared instance bleeds between calls").</p>
      {unit_examples_html}
      {ccy_examples_html}
    </div>

    <div class="finding sev-serious">
      <div><span class="badge serious">Serious</span><h3 style="display:inline">Trades' PRICE mostly off by tick-size, consistent with using order price instead of fill price</h3></div>
      <p>{price_diff_summary['mismatch_count']} of {trades_result['aligned_row_pairs']} trade PRICE values ({_pct(price_diff_summary['mismatch_count'], trades_result['aligned_row_pairs'])}%) don't match the spec-documented value (Tag-31 / fill price). Most differences are small (0.01–0.03) — the size and direction expected if the actual code uses the order's limit price (Tag-44) instead of the trade's fill price, matching the code-review finding on <code>TradeMapper.cs:138</code> ("uses order price instead of fill price"). {price_diff_summary['outlier_count']} trades differ by a much larger margin ({price_diff_summary['outlier_range']}) and likely need individual investigation rather than being explained by the same tick-size pattern.</p>
      {price_examples_html}
    </div>

    <h2 id="orders-fields">Orders — field-by-field mismatch rate</h2>
    <p class="lede">{orders_result['aligned_row_pairs']} row-pairs compared across {orders_result['common_keys']} matched ORIG_TRAN_IDs (only ORIG_TRAN_IDs with the same lifecycle-event count on both sides are row-paired — see Lifecycle integrity below for the rest).</p>
    <div class="table-wrap">
    <table>
      <thead><tr><th>Field</th><th style="text-align:right">Mismatch</th><th style="text-align:right">Rate</th><th></th></tr></thead>
      <tbody>{_field_rows_html(orders_fields, orders_result['aligned_row_pairs'])}</tbody>
    </table>
    </div>

    <h2 id="trades-fields">Trades — field-by-field mismatch rate</h2>
    <p class="lede">{trades_result['aligned_row_pairs']} row-pairs compared across {trades_result['common_keys']} matched ORIG_TRAN_IDs.</p>
    <div class="table-wrap">
    <table>
      <thead><tr><th>Field</th><th style="text-align:right">Mismatch</th><th style="text-align:right">Rate</th><th></th></tr></thead>
      <tbody>{_field_rows_html(trades_fields, trades_result['aligned_row_pairs'])}</tbody>
    </table>
    </div>

    <h2 id="lifecycle">Lifecycle event-count integrity</h2>
    <p class="lede">ORIG_TRAN_IDs where the reconstructed expected row count and the actual row count differ — these are excluded from the field-mismatch stats above since row-for-row pairing would be arbitrary. {len(lifecycle_o)} orders and {len(lifecycle_t)} trades affected (showing up to 15 of each).</p>
    <div class="table-wrap">
    <table>
      <thead><tr><th>Orders — ORIG_TRAN_ID</th><th style="text-align:right">Expected rows</th><th style="text-align:right">Actual rows</th></tr></thead>
      <tbody>{lifecycle_o_rows}</tbody>
    </table>
    </div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>Trades — ORIG_TRAN_ID</th><th style="text-align:right">Expected rows</th><th style="text-align:right">Actual rows</th></tr></thead>
      <tbody>{lifecycle_t_rows}</tbody>
    </table>
    </div>

    <h2 id="unresolved">Unresolved / excluded execution reports</h2>
    <p class="lede">{len(diagnostics)} of the day's execution reports were excluded from reconstruction entirely (not counted in any comparison above):</p>
    <ul class="plain">{diag_html}</ul>

    <h2 id="notes">Methodology &amp; caveats</h2>
    <ul class="plain">{notes_html}</ul>
  </main>
</div>
</body>
</html>"""
