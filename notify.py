#!/usr/bin/env python3
"""
Deliver new award hits through the channels in ``config.json -> notify.channels``.

Reads ``new_hits.json`` (written by ``check.py``) and fans out to each channel:

- ``email`` (the default, for backward compatibility) — an HTML summary to
  ``notify.email_to`` via ``smtplib`` over SSL (a Gmail app password).
- ``macos`` — native Notification Center banners via ``osascript``. No account, no
  password; only useful when the poll host is the Mac you're sitting at.

No-op (exit 0) when there are no new hits, so it is safe to chain after
``check.py`` on every run. Best-effort: a misconfigured or failing channel prints
to stderr and never fails the run or blocks the other channels — the hits are
always in ``new_hits.json`` regardless, which is also the seam an agent can read
to notify you its own way.

Usage:
    notify.py            # deliver if new_hits.json is non-empty
    notify.py --test     # send one test notification through every channel
"""
import html
import json
import os
import smtplib
import subprocess
import sys
from email.message import EmailMessage

import check
from check import fmt_duration, fmt_note  # single source of truth for formatting

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
DEFAULT_CHANNELS = ("email",)
# Cap per-hit banners so a wide alert opening up can't flood Notification Center;
# the remainder is rolled into one summary banner.
MACOS_MAX_BANNERS = 5
# The strings are passed as argv, never spliced into the script — an alert name or
# API value can't break out into AppleScript.
_OSASCRIPT = (
    "on run argv\n"
    "display notification (item 1 of argv) with title (item 2 of argv) "
    "subtitle (item 3 of argv) sound name \"Glass\"\n"
    "end run"
)
TEST_HIT = {"alert_name": "test", "origin": "SFO", "dest": "TST", "date": "2026-01-01",
            "cabin": "business", "airlines": "ZZ", "program": "test", "miles": 1,
            "seats": 1, "direct": True}


def load_notify_cfg():
    return check.load_config().get("notify", {})


def load_hits():
    """Undelivered hits: the outbox check.py fills and we ack after a successful send
    (not new_hits.json, which only ever holds the latest poll)."""
    return check.load_pending()


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
        f"<td>{html.escape(check.fmt_program(h))}</td>"
        f"<td align=\"right\">{h.get('miles', 0):,}</td>"
        f"<td align=\"right\">{html.escape(check.fmt_seats(h))}</td>"
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


def notify_email(hits, notify_cfg, test=False):
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
            html_body = build_html([TEST_HIT])
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


def macos_banner(h):
    """(title, subtitle, message) for one hit."""
    title = (f"\u2708\ufe0f {h.get('origin', '?')}\u2192{h.get('dest', '?')}  "
             f"{h.get('miles', 0):,} mi {h.get('cabin', '')}").rstrip()
    subtitle = f"{h.get('date', '?')} \u00b7 {h.get('airlines', '?')}"
    if h.get("program"):
        subtitle += f" via {check.fmt_program(h)}"
    seats = check.fmt_seats(h)
    message = f"{seats} seat{'' if seats == '1' else 's'} \u00b7 {fmt_note(h)}"
    if h.get("duration_min"):
        message += f" \u00b7 {fmt_duration(h['duration_min'])}"
    return title, subtitle, message


def round_robin(hits):
    """Interleave hits across alerts (order within an alert kept), so a capped
    notifier shows every alert's best hit before any alert's second-best."""
    queues = {}
    for h in hits:
        queues.setdefault(h.get("alert_id") or h.get("alert_name"), []).append(h)
    out = []
    while queues:
        for k in list(queues):
            out.append(queues[k].pop(0))
            if not queues[k]:
                del queues[k]
    return out


def notify_macos(hits, notify_cfg, test=False, _run=None):
    """Best-effort Notification Center banners. Never raises. Returns True iff at
    least one banner was posted. ``_run`` is an injection seam for tests."""
    run = _run or subprocess.run
    if sys.platform != "darwin" and _run is None:
        print("notify: macos channel needs macOS; skipping.", file=sys.stderr)
        return False
    hits = [TEST_HIT] if test else hits
    limit = int(notify_cfg.get("macos_max_banners", MACOS_MAX_BANNERS))
    # hits arrive sorted by (alert, miles); interleave so one busy alert can't crowd
    # the others out of the cap
    banners = [macos_banner(h) for h in round_robin(hits)[:limit]]
    extra = len(hits) - len(banners)
    if extra > 0:
        banners.append(("\u2708\ufe0f Award alert", f"+{extra} more new seat(s)",
                        "Full list is in new_hits.json"))
    sent = 0
    for title, subtitle, message in banners:
        try:
            run(["osascript", "-e", _OSASCRIPT, message, title, subtitle],
                check=True, capture_output=True, timeout=15)
            sent += 1
        except Exception as e:  # best-effort — never fail the run
            print(f"notify: macOS notification failed ({e}); hits are in new_hits.json.",
                  file=sys.stderr)
            break
    if sent:
        print(f"notify: posted {sent} macOS notification(s).")
    return bool(sent)


CHANNELS = {"email": notify_email, "macos": notify_macos}


def notify(hits, notify_cfg, test=False):
    """Fan out to every configured channel. Never raises. Returns True iff any
    channel delivered. An unknown channel is reported and skipped."""
    delivered = False
    channels = notify_cfg.get("channels")
    for name in (DEFAULT_CHANNELS if channels is None else channels):
        fn = CHANNELS.get(name)
        if fn is None:
            print(f"notify: unknown channel '{name}' (expected one of: "
                  f"{', '.join(CHANNELS)}); skipping.", file=sys.stderr)
            continue
        delivered = fn(hits, notify_cfg, test=test) or delivered
    return delivered


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cfg = load_notify_cfg()
    if "--test" in argv:
        notify([], cfg, test=True)
        return
    hits = load_hits()
    if not hits:
        return  # quiet: this runs after every poll
    if cfg.get("channels") == []:
        # Explicitly no built-in channel: an agent delivers from new_hits.json itself.
        check.ack_pending(hits)
        return
    if notify(hits, cfg):
        check.ack_pending(hits)
    else:
        print(f"notify: nothing delivered; {len(hits)} hit(s) stay queued in "
              "pending_hits.json and will be retried next poll.", file=sys.stderr)


if __name__ == "__main__":
    main()
