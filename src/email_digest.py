"""Build and send the daily HTML email digest of screener matches."""

import os
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def build_html(matches: list[dict], run_date: date) -> str:
    rows = ""
    for m in matches:
        signals = ", ".join(m["signals"])
        rsi_display = f"{m['rsi']:.1f}" if m["rsi"] is not None else "-"
        rows += (
            "<tr>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd'><b>{m['ticker']}</b></td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd'>${m['price']:.2f}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd'>{rsi_display}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd'>{signals}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd'><b>{m['score']}</b></td>"
            "</tr>"
        )

    return f"""
    <html><body style="font-family:Arial,sans-serif;color:#222">
      <h2>Stock Screener — {run_date.isoformat()}</h2>
      <p>{len(matches)} ticker(s) matched at least one criterion, sorted by score (highest first).</p>
      <table style="border-collapse:collapse;width:100%">
        <tr style="background:#f2f2f2;text-align:left">
          <th style="padding:6px 10px">Ticker</th>
          <th style="padding:6px 10px">Price</th>
          <th style="padding:6px 10px">RSI</th>
          <th style="padding:6px 10px">Signals</th>
          <th style="padding:6px 10px">Score</th>
        </tr>
        {rows}
      </table>
    </body></html>
    """


def send_digest(matches: list[dict], run_date: date) -> None:
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
    msg.attach(MIMEText(build_html(matches, run_date), "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, recipients, msg.as_string())

    print(f"Sent digest with {len(matches)} matches to {recipients}")
