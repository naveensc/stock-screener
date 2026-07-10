"""Build and send the daily HTML email digest of screener matches."""

import os
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# (text color, background color) per signal — green/red convey bullish/bearish framing,
# blue is neutral (volume spikes don't have an inherent direction).
BULLISH = ("#1a7f37", "#e6f4ea")
BEARISH = ("#b42318", "#fdeceb")
NEUTRAL = ("#0550ae", "#e8f0fe")
SIGNAL_STYLE = {
    "RSI oversold": BULLISH,
    "RSI overbought": BEARISH,
    "Golden cross (50/200)": BULLISH,
    "Death cross (50/200)": BEARISH,
    "Volume spike": NEUTRAL,
    "At upper Bollinger Band": BEARISH,
    "At lower Bollinger Band": BULLISH,
}
DEFAULT_STYLE = ("#444444", "#eeeeee")


def _signal_badge(signal: str) -> str:
    fg, bg = SIGNAL_STYLE.get(signal, DEFAULT_STYLE)
    return (
        f"<span style='display:inline-block;padding:3px 9px;margin:2px 4px 2px 0;"
        f"border-radius:12px;font-size:12px;font-weight:600;color:{fg};background:{bg};"
        f"white-space:nowrap'>{signal}</span>"
    )


def _score_badge(score: int) -> str:
    # Darker/bolder badge the higher the score — the highest-scoring rows are the ones to action.
    intensity = min(score, 4)
    shades = {
        1: ("#3730a3", "#e0e7ff"),
        2: ("#3730a3", "#c7d2fe"),
        3: ("#312e81", "#a5b4fc"),
        4: ("#1e1b4b", "#818cf8"),
    }
    fg, bg = shades.get(intensity, shades[4])
    return (
        f"<span style='display:inline-block;min-width:22px;text-align:center;padding:4px 10px;"
        f"border-radius:14px;font-size:14px;font-weight:700;color:#fff;background:{fg}'>{score}</span>"
    )


def build_html(matches: list[dict], run_date: date, universe_size: int | None = None, fetched_count: int | None = None) -> str:
    rows = ""
    for i, m in enumerate(matches):
        row_bg = "#ffffff" if i % 2 == 0 else "#fafafa"
        badges = "".join(_signal_badge(s) for s in m["signals"])
        rsi_display = f"{m['rsi']:.1f}" if m["rsi"] is not None else "-"
        ticker_link = f"https://finance.yahoo.com/quote/{m['ticker']}"
        rows += (
            f"<tr style='background:{row_bg}'>"
            f"<td style='padding:10px 12px;border-bottom:1px solid #eee'>"
            f"<a href='{ticker_link}' style='color:#111;font-weight:700;text-decoration:none;font-size:14px'>{m['ticker']}</a></td>"
            f"<td style='padding:10px 12px;border-bottom:1px solid #eee;color:#333'>${m['price']:.2f}</td>"
            f"<td style='padding:10px 12px;border-bottom:1px solid #eee;color:#333'>{rsi_display}</td>"
            f"<td style='padding:10px 12px;border-bottom:1px solid #eee'>{badges}</td>"
            f"<td style='padding:10px 12px;border-bottom:1px solid #eee;text-align:center'>{_score_badge(m['score'])}</td>"
            "</tr>"
        )

    stats_bits = [f"{len(matches)} matched"]
    if fetched_count is not None:
        stats_bits.insert(0, f"{fetched_count} screened")
    if universe_size is not None:
        stats_bits.insert(0, f"{universe_size} in universe")
    stats_line = " · ".join(stats_bits)

    return f"""
    <html><body style="margin:0;padding:0;background:#f5f5f7;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1a1a1a">
      <div style="max-width:720px;margin:0 auto;padding:24px 16px">
        <div style="background:#111827;border-radius:10px 10px 0 0;padding:20px 24px">
          <h1 style="margin:0;color:#fff;font-size:20px;font-weight:700">Stock Screener</h1>
          <p style="margin:6px 0 0;color:#9ca3af;font-size:13px">{run_date.strftime('%A, %B %d, %Y')}</p>
        </div>

        <div style="background:#fff;padding:16px 24px;border-bottom:1px solid #eee;font-size:13px;color:#666">
          {stats_line} — sorted by score (highest = most signals matched)
        </div>

        <div style="background:#fff;border-radius:0 0 10px 10px;overflow:hidden">
          <table style="border-collapse:collapse;width:100%">
            <tr style="background:#f9fafb;text-align:left">
              <th style="padding:10px 12px;font-size:12px;text-transform:uppercase;letter-spacing:0.03em;color:#6b7280">Ticker</th>
              <th style="padding:10px 12px;font-size:12px;text-transform:uppercase;letter-spacing:0.03em;color:#6b7280">Price</th>
              <th style="padding:10px 12px;font-size:12px;text-transform:uppercase;letter-spacing:0.03em;color:#6b7280">RSI</th>
              <th style="padding:10px 12px;font-size:12px;text-transform:uppercase;letter-spacing:0.03em;color:#6b7280">Signals</th>
              <th style="padding:10px 12px;font-size:12px;text-transform:uppercase;letter-spacing:0.03em;color:#6b7280;text-align:center">Score</th>
            </tr>
            {rows}
          </table>
        </div>

        <p style="text-align:center;color:#aaa;font-size:11px;margin-top:16px">
          Generated automatically · S&amp;P 500 + NASDAQ-100 · free/delayed data via Yahoo Finance
        </p>
      </div>
    </body></html>
    """


def send_digest(matches: list[dict], run_date: date, universe_size: int | None = None, fetched_count: int | None = None) -> None:
    if not matches:
        print("No matches today — no email sent.")
        return

    smtp_user = os.environ["EMAIL_SMTP_USER"]
    smtp_password = os.environ["EMAIL_SMTP_PASSWORD"]
    recipients = [r.strip() for r in os.environ["RECIPIENTS"].split(",") if r.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Stock Screener] {len(matches)} matches — {run_date.isoformat()}"
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(build_html(matches, run_date, universe_size, fetched_count), "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, recipients, msg.as_string())

    print(f"Sent digest with {len(matches)} matches to {recipients}")
