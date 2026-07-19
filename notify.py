#!/usr/bin/env python3
"""
Email new award hits via SMTP (a Gmail app password).

Reads ``new_hits.json`` (written by ``check.py``) and sends an HTML summary to
``config.json -> notify.email_to`` using ``smtplib`` over SSL. No-op (exit 0, no
email) when there are no new hits, so it is safe to chain after ``check.py`` on
every run. Best-effort: a missing password or an SMTP error prints to stderr and
never fails the run — the hits are always in ``new_hits.json`` regardless, which is
also the seam an agent can read to notify you its own way.

Usage:
    notify.py            # send if new_hits.json is non-empty
    notify.py --test     # send a one-row test email to verify the SMTP path
"""
import html
import json
import os
import smtplib
import sys
from email.message import EmailMessage

import check
from check import fmt_duration, fmt_note  # single source of truth for formatting

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def load_notify_cfg():
    return check.load_config().get("notify", {})


def load_hits():
    if not os.path.isfile(check.NEW_HITS_PATH):
        return []
    try:
        with open(check.NEW_HITS_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def route_cell(h):
    # Escape everything: origin/dest come from the API; a value must never break
    # out of the HTML. Only render a clickable anchor for https links.
    label = html.escape(f"{h.get('origin', '?')}→{h.get('dest', '?')}")
    link = h.get("link") or ""
    if link.startswith("https://"):
        return f'<a href="{html.escape(link, quote=True)}">{label}</a>'
    return label


def build_html(hits):
    # alert_name is free-form and airline/program come from the API — escape all of
    # them so a hostile value can't inject markup into an email you trust.
    rows = "\n".join(
        f"<tr><td>{html.escape(str(h.get('alert_name', '')))}</td><td>{route_cell(h)}</td>"
        f"<td>{html.escape(str(h.get('date', '')))}</td>"
        f"<td>{html.escape(str(h.get('airlines', '')))}</td>"
        f"<td>{html.escape(str(h.get('program', '')))}</td>"
        f"<td align=\"right\">{h.get('miles', 0):,}</td>"
        f"<td align=\"right\">{html.escape(str(h.get('seats', '?')))}</td>"
        f"<td align=\"right\">{html.escape(fmt_duration(h.get('duration_min')))}</td>"
        f"<td>{html.escape(fmt_note(h))}</td></tr>"
        for h in hits
    )
    return (
        f"<p>Your award monitor found <b>{len(hits)} new award seat(s)</b>. "
        "Click a route to open it on <a href=\"https://seats.aero\">seats.aero</a> "
        "&mdash; and always confirm the space on the airline's own site before you "
        "transfer points.</p>"
        "<table border=\"1\" cellpadding=\"6\" cellspacing=\"0\" "
        "style=\"border-collapse:collapse;font-family:sans-serif;font-size:14px\">"
        "<tr style=\"background:#f0f0f0\">"
        "<th align=\"left\">Alert</th><th align=\"left\">Route</th>"
        "<th align=\"left\">Date</th><th align=\"left\">Airline</th>"
        "<th align=\"left\">Program</th><th align=\"right\">Miles</th>"
        "<th align=\"right\">Seats</th><th align=\"right\">Length</th>"
        "<th align=\"left\">Notes</th></tr>"
        f"{rows}</table>"
        "<p style=\"color:#888;font-size:12px\">Only new or cheaper seats are shown. "
        "Manage alerts by talking to your agent (manage.py).</p>"
    )


def send(subject, html, notify_cfg, password, smtp_factory=None):
    """Send one HTML email. ``smtp_factory`` is an injection seam for tests."""
    to = notify_cfg.get("email_to")
    user = notify_cfg.get("smtp_user") or to
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content("This is an HTML email — view it in an HTML-capable client.")
    msg.add_alternative(html, subtype="html")
    factory = smtp_factory or (lambda: smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30))
    with factory() as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)


def notify(hits, notify_cfg, test=False):
    """Best-effort email. Never raises. Returns True iff an email was sent."""
    to = notify_cfg.get("email_to")
    pw_env = notify_cfg.get("smtp_password_env", "AWARD_SMTP_PASSWORD")
    password = os.environ.get(pw_env, "").strip()
    if not to:
        print("notify: no notify.email_to configured; skipping email.", file=sys.stderr)
        return False
    if not password:
        print(f"notify: {pw_env} not set; skipping email (hits are in new_hits.json).",
              file=sys.stderr)
        return False
    try:
        if test:
            subject = "✈️ Award alert test (SMTP path)"
            html_body = build_html([{"alert_name": "test", "origin": "SFO", "dest": "TST",
                                     "date": "2026-01-01", "airlines": "ZZ", "program": "test",
                                     "miles": 1, "seats": 1, "direct": True}])
        else:
            cheapest = min((h.get("miles", 0) for h in hits), default=0)
            subject = f"✈️ Award alert: {len(hits)} new seat(s) from {cheapest:,} mi"
            html_body = build_html(hits)
        send(subject, html_body, notify_cfg, password)
        print(f"notify: email sent to {to}.")
        return True
    except Exception as e:  # best-effort — a send/format failure must never fail the run
        print(f"notify: SMTP send failed ({e}); hits are in new_hits.json.", file=sys.stderr)
        return False


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cfg = load_notify_cfg()
    if "--test" in argv:
        notify([], cfg, test=True)
        return
    hits = load_hits()
    if not hits:
        print("notify: no new hits — nothing to send.")
        return
    notify(hits, cfg)


if __name__ == "__main__":
    main()
