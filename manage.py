#!/usr/bin/env python3
"""
manage.py — the agent-facing CLI for editing award alerts.

This is the neutral control-plane interface: any agent (OpenClaw, Hermes, or a
person at a terminal) shells out to ``manage.py add/list/rm/enable/disable`` to edit
``alerts.json``. It validates input, writes atomically under an advisory lock, and
prints a JSON result to stdout so the caller can confirm what happened.

It runs NO git and NO shell — it only edits the local alert file. Free-form values
like ``--name`` are stored as JSON data and never interpreted.

Examples:
  manage.py add --name "SFO-Tokyo Nov" --from SFO --to NRT,HND,KIX \\
      --cabin business --max-miles 90000 --min-seats 2 --start 2026-11-01 --end 2026-11-30
  manage.py list --json
  manage.py disable --id <id>
  manage.py rm --id <id>
"""
import argparse
import fcntl
import json
import os
import re
import sys
from datetime import datetime

import check  # reuse content_id, resolve, load_config, ALERTS_PATH, _atomic_write

ALERTS_PATH = check.ALERTS_PATH
LOCK_PATH = ALERTS_PATH + ".lock"
AIRPORT_RE = re.compile(r"^[A-Z]{3}$")


def _fail(msg, code=2):
    print(json.dumps({"ok": False, "error": msg}))
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _ok(payload):
    out = {"ok": True}
    out.update(payload)
    print(json.dumps(out, indent=2))


def _parse_airports(raw, flag):
    codes = [c.strip().upper() for c in (raw or "").split(",") if c.strip()]
    if not codes:
        _fail(f"{flag} requires at least one airport code")
    for c in codes:
        if not AIRPORT_RE.match(c):
            _fail(f"invalid airport code '{c}' (expected 3 letters, e.g. SFO)")
    return codes


def _parse_date(s, flag):
    if s is None:
        return None
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        _fail(f"{flag} must be YYYY-MM-DD, got '{s}'")
    return s


def _load():
    if os.path.isfile(ALERTS_PATH):
        with open(ALERTS_PATH) as f:
            data = json.load(f)
    else:
        data = {}
    data.setdefault("alerts", [])
    return data


def _save(data):
    check._atomic_write(ALERTS_PATH, json.dumps(data, indent=2))


def _with_lock(fn):
    """Run fn() while holding an exclusive advisory lock, so two agents editing at
    once serialize rather than clobbering each other."""
    d = os.path.dirname(LOCK_PATH) or "."
    os.makedirs(d, exist_ok=True)
    with open(LOCK_PATH, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _resolved_view(alert):
    """The alert plus its effective params (after defaults merge), so an agent can
    confirm an override actually took effect."""
    try:
        defaults = check.load_config().get("defaults", {})
    except (OSError, json.JSONDecodeError):
        defaults = {}
    view = dict(alert)
    view["_resolved"] = check.resolve(alert, defaults)
    return view


def cmd_add(args):
    origins = _parse_airports(args.origin, "--from")
    dests = _parse_airports(args.to, "--to")
    if args.cabin is not None:
        # An unknown cabin would be sent to the API verbatim yet filtered as
        # business — the alert would silently never match. Reject it up front.
        args.cabin = args.cabin.strip().lower()
        valid = [*check.CABIN_PREFIX, check.ANY_CABIN]
        if args.cabin not in valid:
            _fail(f"invalid cabin '{args.cabin}' (expected one of: {', '.join(valid)})")
    if args.max_miles is not None and args.max_miles <= 0:
        _fail("--max-miles must be positive")
    if args.min_seats is not None and args.min_seats < 1:
        _fail("--min-seats must be >= 1")
    start = _parse_date(args.start, "--start")
    end = _parse_date(args.end, "--end")
    if start and end and end < start:
        _fail("--end must be on or after --start")

    alert = {
        "name": args.name,           # free-form; stored as DATA only
        "origins": origins,
        "destinations": dests,
        "cabin": args.cabin,
        "max_miles": args.max_miles,
        "min_seats": args.min_seats,
        "only_direct": True if args.only_direct else None,
        "start_date": start,
        "end_date": end,
        "enabled": True,
    }
    alert = {k: v for k, v in alert.items() if v is not None}  # drop unset -> inherit
    alert["id"] = check.content_id(alert)

    def op():
        data = _load()
        # same content -> same id: replace (idempotent add / re-enable)
        data["alerts"] = [a for a in data["alerts"] if a.get("id") != alert["id"]]
        data["alerts"].append(alert)
        _save(data)
        return alert

    _ok({"action": "added", "alert": _resolved_view(_with_lock(op))})


def cmd_list(args):
    data = _load()
    alerts = data["alerts"]
    if args.json:
        _ok({"alerts": [_resolved_view(a) for a in alerts], "count": len(alerts)})
        return
    for a in alerts:
        flag = "on " if a.get("enabled", True) else "off"
        print(f"[{flag}] {a.get('id')}  {a.get('name', '')}  "
              f"{','.join(a.get('origins', []))}->{','.join(a.get('destinations', []))}")
    print(f"{len(alerts)} alert(s)", file=sys.stderr)


def _mutate_by_id(alert_id, fn, action):
    def op():
        data = _load()
        for a in list(data["alerts"]):
            if a.get("id") == alert_id:
                fn(data, a)
                _save(data)
                return a
        return None

    if _with_lock(op) is None:
        _fail(f"no alert with id '{alert_id}'")
    _ok({"action": action, "id": alert_id})


def cmd_rm(args):
    _mutate_by_id(args.id, lambda data, a: data["alerts"].remove(a), "removed")


def cmd_enable(args):
    _mutate_by_id(args.id, lambda data, a: a.__setitem__("enabled", True), "enabled")


def cmd_disable(args):
    _mutate_by_id(args.id, lambda data, a: a.__setitem__("enabled", False), "disabled")


def build_parser():
    p = argparse.ArgumentParser(prog="manage.py", description="Manage award alerts.")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="add an alert")
    a.add_argument("--name", required=True)
    a.add_argument("--from", dest="origin", required=True, help="origin airport(s), comma-separated")
    a.add_argument("--to", dest="to", required=True, help="destination airport(s), comma-separated")
    a.add_argument("--cabin", default=None, help="economy|premium|business|first|any (default: config default)")
    a.add_argument("--max-miles", dest="max_miles", type=int, default=None)
    a.add_argument("--min-seats", dest="min_seats", type=int, default=None)
    a.add_argument("--only-direct", dest="only_direct", action="store_true")
    a.add_argument("--start", default=None, help="YYYY-MM-DD (else rolling window)")
    a.add_argument("--end", default=None, help="YYYY-MM-DD")
    a.set_defaults(func=cmd_add)

    lst = sub.add_parser("list", help="list alerts")
    lst.add_argument("--json", action="store_true", help="machine-readable output")
    lst.set_defaults(func=cmd_list)

    for name, fn, helptext in (("rm", cmd_rm, "remove an alert"),
                               ("enable", cmd_enable, "enable an alert"),
                               ("disable", cmd_disable, "pause an alert")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--id", required=True)
        s.set_defaults(func=fn)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
