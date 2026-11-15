"""Build the daily email (HTML) and send it.

The email is a summary that links out: every ticker opens its stock card on the published site.
Mail clients strip <style> blocks and scripts, so everything here is tables with inline styles.

    python src/email_digest.py email.html     # send a built email (subject = its <title>)
"""

import html
import os
import re
import smtplib
import sys
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

SCORE_KEYS = ("quality", "valuation", "technical")
SCORE_LABELS = {"quality": "Quality", "valuation": "Valuation", "technical": "Technical"}
INK, MUTED, BORDER, PAPER = "#1d2116", "#6b6f60", "#e3e5da", "#f6f6f1"
GREEN, AMBER, RED, OLIVE = "#2f7d32", "#b7791f", "#b4392c", "#4f5d2b"
PHASES = {1: "Startup", 2: "Hyper Growth", 3: "Operating Leverage", 4: "Capital Return", 5: "Decline"}
PHASE_COLORS = {1: "#6e44b3", 2: "#1f5fc4", 3: "#1b7d45", 4: "#9a5b00", 5: "#b8352a"}
VERDICT_COLORS = {"Undervalued": GREEN, "Overvalued": RED}
TH = f"padding:6px 8px;font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;color:{MUTED};text-align:left"
TD = f"padding:8px;border-top:1px solid {BORDER};vertical-align:top;font-size:13px;color:{INK}"

e = html.escape


def _score_color(v: float | None) -> str:
    if v is None:
        return MUTED
    return GREEN if v >= 70 else AMBER if v >= 40 else RED


def _bar(label: str, v: float | None) -> str:
    """A labeled 0-100 bar as nested tables (divs with % widths aren't reliable in Outlook)."""
    width = 0 if v is None else max(2, int(round(v)))
    value = "–" if v is None else f"{v:.0f}"
    fill = (f"<td width='{width}%' style='background:{_score_color(v)};height:6px;line-height:6px;font-size:0'>&nbsp;</td>"
            if width else "")
    return (f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='margin:1px 0'><tr>"
            f"<td width='18' style='font-size:11px;color:{MUTED}'>{label[0]}</td>"
            f"<td><table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='background:{BORDER}'>"
            f"<tr>{fill}<td style='height:6px;line-height:6px;font-size:0'>&nbsp;</td></tr></table></td>"
            f"<td width='26' align='right' style='font-size:11px;font-weight:700;color:{INK}'>{value}</td></tr></table>")


def _pill(text: str, color: str) -> str:
    return (f"<span style='display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:700;"
            f"color:{color};border:1px solid {color};white-space:nowrap'>{e(text)}</span>")


def _ticker(r: dict, site_url: str) -> str:
    url = f"{site_url}/cards/{r['ticker']}.html"
    return (f"<a href='{e(url)}' style='color:{INK};font-weight:800;font-size:14px;text-decoration:none'>{e(r['ticker'])}</a>"
            f"<div style='font-size:11px;color:{MUTED}'>{e(r['name'])}</div>")


def _reason(r: dict) -> str:
    """One line on why a stock ranks where it does: its strongest and weakest score, plus the
    life-cycle phase, the model valuation and the trend."""
    d = r["daily"]
    present = sorted(((SCORE_LABELS[k], d[k]) for k in SCORE_KEYS if d[k] is not None), key=lambda x: -x[1])
    bits = []
    if len(present) >= 2:
        bits.append(f"Strongest on {present[0][0].lower()} ({present[0][1]:.0f}), weakest on {present[-1][0].lower()} ({present[-1][1]:.0f}).")
    valuation = r.get("valuation_verdict") or "n/a"
    upside = r.get("base_upside")
    if upside is not None:
        valuation_text = f"DCF says {valuation.lower()} (base case {upside:+.0%})"
    else:
        valuation_text = f"no DCF possible, multiples say {valuation.lower()}"
    bits.append(f"{r['phase_name']} phase; {valuation_text}; {d['trend_state'].lower()}.")
    # The Valuation score is relative (vs this universe); the DCF verdict is absolute. When they
    # disagree, say so rather than leave "Valuation 80" next to "Overvalued" unexplained.
    v = d["valuation"]
    if v is not None and v >= 60 and valuation == "Overvalued":
        bits.append("Cheap relative to this universe, but not against its own DCF — the whole group may be richly priced.")
    elif v is not None and v <= 40 and valuation == "Undervalued":
        bits.append("Its DCF looks attractive, but peers screen cheaper on multiples.")
    return " ".join(bits)


def _section(title: str, subtitle: str, body: str, accent: str = INK) -> str:
    return (f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
            f"style='background:#fff;border:1px solid {BORDER};border-top:3px solid {accent};border-radius:8px;margin:0 0 16px'>"
            f"<tr><td style='padding:14px 16px 4px'><div style='font-size:15px;font-weight:800;color:{INK}'>{e(title)}</div>"
            f"<div style='font-size:12px;color:{MUTED};margin-top:2px'>{subtitle}</div></td></tr>"
            f"<tr><td style='padding:4px 8px 10px'>{body}</td></tr></table>")


def _top_ideas(results: list[dict], site_url: str, n: int) -> str:
    rows = ""
    for r in [r for r in results if r["daily"]["rank"] is not None][:n]:
        d = r["daily"]
        bars = "".join(_bar(SCORE_LABELS[k], d[k]) for k in SCORE_KEYS)
        verdict = r.get("valuation_verdict") or "n/a"
        basis = "DCF" if r.get("base_upside") is not None else "Multiples"
        rows += (f"<tr><td style='{TD};font-weight:800;color:{MUTED}'>{d['rank']}</td>"
                 f"<td style='{TD}'>{_ticker(r, site_url)}</td>"
                 f"<td style='{TD};font-size:22px;font-weight:800;color:{_score_color(d['composite'])}'>{d['composite']:.0f}</td>"
                 f"<td style='{TD};width:150px'>{bars}</td>"
                 f"<td style='{TD}'>{_pill(PHASES[r['phase']], PHASE_COLORS[r['phase']])}<br>"
                 f"<span style='font-size:11px;font-weight:700;color:{VERDICT_COLORS.get(verdict, MUTED)}'>{basis}: {e(verdict)}</span></td></tr>"
                 f"<tr><td></td><td colspan='4' style='padding:0 8px 10px;font-size:12px;color:{MUTED};line-height:1.45'>{e(_reason(r))}</td></tr>")
    head = (f"<tr><th style='{TH}'>#</th><th style='{TH}'>Ticker</th><th style='{TH}'>Score</th>"
            f"<th style='{TH}'>Q · V · T</th><th style='{TH}'>Phase · DCF</th></tr>")
    return f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0'>{head}{rows}</table>"


def _movers_table(movers: list[dict], by_ticker: dict, site_url: str, up: bool) -> str:
    if not movers:
        return f"<p style='font-size:12px;color:{MUTED};margin:6px 8px'>No moves of that size.</p>"
    color = GREEN if up else RED
    rows = ""
    for m in movers:
        r = by_ticker[m["ticker"]]
        d5 = "" if m["d5"] is None else f"<div style='font-size:11px;color:{MUTED}'>{m['d5']:+.0f} over 5 runs</div>"
        rows += (f"<tr><td style='{TD}'>{_ticker(r, site_url)}</td>"
                 f"<td style='{TD};white-space:nowrap'><b style='font-size:16px;color:{color}'>{m['d1']:+.0f}</b>"
                 f"<span style='color:{MUTED}'> → {m['composite']:.0f}</span>{d5}</td>"
                 f"<td style='{TD};font-size:12px;color:{MUTED}'>{e(m['driver'])}</td></tr>")
    return f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0'>{rows}</table>"


def _state_text(s: dict) -> str:
    if s["kind"] == "Phase":
        return f"{PHASES[s['from']]} → {PHASES[s['to']]}"
    if s["kind"].startswith("Top"):
        return f"entered {s['kind'].lower()} {s['to'][3:]}" if s["to"] != "out" else f"left the {s['kind'].lower()}"
    return f"{s['from']} → {s['to']}"


def _states(states: list[dict], by_ticker: dict, site_url: str) -> str:
    order = {"Phase": 0, "Valuation": 1, "Trend": 2}
    rows = ""
    for s in sorted(states, key=lambda s: (order.get(s["kind"], 3), s["ticker"])):
        r = by_ticker[s["ticker"]]
        rows += (f"<tr><td style='{TD};width:90px'>{_ticker(r, site_url)}</td>"
                 f"<td style='{TD};width:90px;font-size:11px;font-weight:700;color:{MUTED};text-transform:uppercase'>{e(s['kind'])}</td>"
                 f"<td style='{TD}'>{e(_state_text(s))}</td></tr>")
    return f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0'>{rows}</table>"


def _alerts(results: list[dict], site_url: str) -> str:
    rows = ""
    for r in results:
        if r["daily"]["alerts"]:
            tags = " ".join(_pill(a, OLIVE) for a in r["daily"]["alerts"])
            rows += f"<tr><td style='{TD};width:90px'>{_ticker(r, site_url)}</td><td style='{TD};line-height:1.9'>{tags}</td></tr>"
    return f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0'>{rows}</table>" if rows else ""


def build_html(results: list[dict], changed: dict, *, run_date: str, regime: str, universe_label: str,
               error_count: int, risk_free: float, site_url: str, top_n: int, weights: dict[str, float],
               min_change: float, stale_count: int = 0) -> tuple[str, str]:
    """Returns (html, subject). `results` must be sorted by composite rank."""
    by_ticker = {r["ticker"]: r for r in results}
    ranked = [r for r in results if r["daily"]["rank"] is not None]
    day = date.fromisoformat(run_date)
    regime_color = GREEN if regime == "uptrend" else RED

    recipe = " + ".join(f"{weights[k]:.0%} {SCORE_LABELS[k]}" for k in SCORE_KEYS)
    sections = _section(f"Top {top_n} by composite score",
                        f"Composite = {recipe}, each a 0–100 percentile across today's universe. "
                        "Valuation is relative (cheaper than X% of the universe); the DCF verdict is absolute.",
                        _top_ideas(ranked, site_url, top_n))
    if changed["has_history"]:
        sub = f"Composite change since {changed['prev_date']} (moves of {min_change:g}+ points), with the score that drove it."
        if changed["up"] or changed["down"]:
            sections += _section("Moved up", sub, _movers_table(changed["up"], by_ticker, site_url, True), GREEN)
            sections += _section("Moved down", sub, _movers_table(changed["down"], by_ticker, site_url, False), RED)
        else:
            sections += _section("Movers", f"No composite moved {min_change:g}+ points since {changed['prev_date']} — a quiet day.", "")
        if changed["states"]:
            sections += _section("State changes", f"Life-cycle phase, model valuation, 200-day trend and top-20 membership vs {changed['prev_date']}.",
                                 _states(changed["states"], by_ticker, site_url), OLIVE)
    else:
        sections += _section("Movers", "No earlier snapshot yet — movers and state changes start with the next run.", "")
    alerts = _alerts(ranked, site_url)
    if alerts:
        sections += _section("Technical alerts", "One-day events (crosses, band touches, volume spikes). Context only — they don't move the scores.", alerts, AMBER)

    up, down, states = len(changed["up"]), len(changed["down"]), len(changed["states"])
    leaders = ", ".join(r["ticker"] for r in ranked[:3])
    subject = f"[Stock Screener] {day.strftime('%b %d')} · Top: {leaders}"
    if changed["has_history"]:
        subject += f" · {up} up, {down} down, {states} state changes"

    body = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(subject)}</title></head>
<body style="margin:0;padding:0;background:{PAPER};font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;color:{INK}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAPER}"><tr><td align="center" style="padding:20px 10px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:680px">
<tr><td style="background:{INK};border-radius:8px 8px 0 0;padding:18px 20px">
<div style="font-size:11px;letter-spacing:.1em;color:#b8bca8;font-weight:700">STOCK SCREENER / DAILY SCORES</div>
<div style="font-size:20px;font-weight:800;color:#fff;margin-top:4px">{day.strftime('%A, %B %d, %Y')}</div>
<div style="margin-top:10px">
<span style="display:inline-block;padding:2px 10px;border-radius:10px;background:#fff;color:{regime_color};font-size:11px;font-weight:800">Market: {regime.title()}</span>
<span style="font-size:12px;color:#b8bca8;margin-left:8px">{len(ranked)} scored · {e(universe_label)} · 10-yr Treasury {risk_free * 100:.2f}%</span></div>
</td></tr>
<tr><td style="padding:16px 0 0">{sections}</td></tr>
<tr><td style="text-align:center;padding:4px 0 20px">
<a href="{e(site_url)}/screener.html" style="display:inline-block;background:{OLIVE};color:#fff;font-weight:700;font-size:13px;padding:10px 18px;border-radius:6px;text-decoration:none">Open the full screener</a>
<p style="font-size:11px;color:{MUTED};line-height:1.5;margin:14px 10px 0">Click any ticker for its stock card. Scores are model output from Yahoo Finance data
(free, delayed){f"; {error_count} ticker{'s' if error_count != 1 else ''} skipped for missing data" if error_count else ""}{f"; {stale_count} on older fundamentals because today's refresh failed" if stale_count else ""}. Not investment advice.</p></td></tr>
</table></td></tr></table></body></html>"""
    return body, subject


def send(html_path: str) -> None:
    body = open(html_path, encoding="utf-8").read()
    subject = html.unescape(re.search(r"<title>(.*?)</title>", body, re.S).group(1))
    smtp_user = os.environ["EMAIL_SMTP_USER"]
    smtp_password = os.environ["EMAIL_SMTP_PASSWORD"]
    recipients = [r.strip() for r in os.environ["RECIPIENTS"].split(",") if r.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, recipients, msg.as_string())
    print(f"Sent '{subject}' to {len(recipients)} recipient(s)")


if __name__ == "__main__":
    send(sys.argv[1])
