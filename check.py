#!/usr/bin/env python3
"""
Award-space monitor.

Reads settings from ``config.json`` and the alert list from ``alerts.json``,
queries the Seats.aero Partner API per alert (via ``seats_aero.py``), filters to
award space under each alert's mileage cap, and dedupes against ``state.json`` so
only *new* — or newly cheaper — seats are reported. Writes ``new_hits.json`` for
``notify.py`` (or an agent) to consume.

Output contract (stdout):  ``NEW_HITS: <n>``  then a compact JSON array when n > 0.
Human-readable detail goes to stderr. Exit code is 0 unless config/auth is broken.

Single-poller guard: the poll runs for real only on the host recorded in
``.poll-host`` (written by ``setup.sh``). On any other host it warns, reports
``NEW_HITS: 0``, and writes nothing — so a stray second poller can't double-alert,
and an ad-hoc off-host run can't re-alert everything from an empty state.
"""
import fcntl
import hashlib
import json
import os
import socket
import sys
import tempfile
from datetime import date, timedelta
from urllib.parse import quote

import seats_aero

# Map an alert's cabin to the API field prefix and trip Cabin value.
CABIN_PREFIX = {"economy": "Y", "premium": "W", "business": "J", "first": "F"}


def cabin_prefix(cabin):
    return CABIN_PREFIX.get((cabin or "business").lower(), "J")

HERE = os.path.dirname(os.path.abspath(__file__))
# Paths default to files next to the code but can be overridden via env — useful
# for tests and for relocating the local data directory.
CONFIG_PATH = os.environ.get("AWARD_CONFIG", os.path.join(HERE, "config.json"))
ALERTS_PATH = os.environ.get("AWARD_ALERTS", os.path.join(HERE, "alerts.json"))
STATE_PATH = os.environ.get("AWARD_STATE", os.path.join(HERE, "state.json"))
NEW_HITS_PATH = os.environ.get("AWARD_NEW_HITS", os.path.join(HERE, "new_hits.json"))
POLL_HOST_PATH = os.environ.get("AWARD_POLL_HOST", os.path.join(HERE, ".poll-host"))


# --------------------------------------------------------------------------- #
# config + alert loading
# --------------------------------------------------------------------------- #
def load_config(path=None):
    path = path or CONFIG_PATH  # resolve at call time, not import time
    with open(path) as f:
        cfg = json.load(f)
    cfg.setdefault("base_url", seats_aero.DEFAULT_BASE_URL)
    cfg.setdefault("api_key_env", "SEATS_AERO_API_KEY")
    cfg.setdefault("defaults", {})
    cfg["_excluded_airlines"] = set(cfg.get("excluded_airlines", []))
    return cfg


def load_alerts(path=None):
    path = path or ALERTS_PATH
    with open(path) as f:
        data = json.load(f)
    return data.get("alerts", [])


def content_id(alert):
    """Stable id derived from an alert's identity, so removing then re-adding the
    same watch reuses the id and rejoins its dedupe state instead of re-alerting.

    Identity = every field that makes two watches semantically distinct. Keep this
    list in sync with the alert fields set by manage.py's `add` — two alerts that
    differ only in an omitted field here would collide and silently replace."""
    key = "|".join([
        ",".join(sorted(alert.get("origins", []))),
        ",".join(sorted(alert.get("destinations", []))),
        str(alert.get("cabin", "")),
        str(alert.get("max_miles", "")),
        str(alert.get("min_seats", "")),
        str(bool(alert.get("only_direct", False))),
        str(alert.get("start_date", "")),
        str(alert.get("end_date", "")),
    ])
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def resolve(alert, defaults):
    """Resolve an alert's effective params: alert value, else the config default."""
    def g(k, dv):
        v = alert.get(k)
        return v if v is not None else defaults.get(k, dv)
    return {
        "cabin": g("cabin", "business"),
        "max_miles": int(g("max_miles", 90000)),
        "min_seats": int(g("min_seats", 1)),
        "only_direct": bool(g("only_direct", False)),
        "respect_exclusions": bool(g("respect_exclusions", True)),
    }


def alert_window(alert, defaults):
    if alert.get("start_date") and alert.get("end_date"):
        return alert["start_date"], alert["end_date"]
    today = date.today()
    end = today + timedelta(days=int(defaults.get("rolling_days", 90)))
    return today.isoformat(), end.isoformat()


# --------------------------------------------------------------------------- #
# state (atomic, 0600)
# --------------------------------------------------------------------------- #
def load_state(path=None):
    path = path or STATE_PATH
    if not os.path.isfile(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError:
        # A corrupt state file must not silently reset dedupe every run — make it
        # visible and set it aside so the reset happens once, not repeatedly.
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
        print(f"WARNING: {path} was corrupt; reset to empty (saved to {path}.corrupt). "
              "You may get one repeat alert.", file=sys.stderr)
        return {}
    except OSError:
        return {}


def _atomic_write(path, text, mode=0o600):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_state(state, path=None):
    _atomic_write(path or STATE_PATH, json.dumps(state, indent=2, sort_keys=True))


# --------------------------------------------------------------------------- #
# filtering (native API fields — NOT the CLI's *Raw names)
# --------------------------------------------------------------------------- #
def best_trip(row, miles, cabin):
    """Pick the representative itinerary for this row's cabin: prefer a trip whose
    cost matches the row mileage; among those take the shortest duration.
    Returns (duration_min, connections, stops) or (None, [], None)."""
    want = (cabin or "business").lower()
    trips = [t for t in (row.get("AvailabilityTrips") or [])
             if (t.get("Cabin") or "").lower() == want]
    if not trips:
        return None, [], None

    def dur(t):
        v = t.get("TotalDuration")
        return v if isinstance(v, (int, float)) else 10 ** 9

    exact = [t for t in trips if t.get("MileageCost") == miles]
    pool = exact or trips
    pool.sort(key=dur)
    t = pool[0]
    conns = [c for c in (t.get("Connections") or []) if c]
    return t.get("TotalDuration"), conns, t.get("Stops")


def build_link(cfg, origin, dest, day):
    tpl = cfg.get("seats_aero_url_template")
    if not tpl:
        return ""
    # URL-encode components — dest/date come from the API and could carry & or ".
    return tpl.format(origin=quote(str(origin)), dest=quote(str(dest)),
                      date=quote(str(day)))


def filter_rows(rows, params, excluded, cfg, origin):
    """Keep rows for the alert's cabin that beat the cap and pass its filters.

    Reads the native API field names for the requested cabin via its prefix
    (Y/W/J/F). The *MileageCost/*RemainingSeats/*TotalTaxes fields are already
    ``int`` (coerced in seats_aero.search), so the numeric comparisons are safe.
    """
    cabin = params["cabin"]
    pfx = cabin_prefix(cabin)
    cap = params["max_miles"]
    min_seats = params["min_seats"]
    only_direct = params["only_direct"]
    hits = []
    for r in rows:
        if not r.get(f"{pfx}Available"):
            continue
        miles = r.get(f"{pfx}MileageCost") or 0
        if miles <= 0 or miles >= cap:
            continue
        seats = r.get(f"{pfx}RemainingSeats") or 0
        if seats < min_seats:
            continue
        day = r.get("Date")
        if not day:  # a dateless hit isn't actionable and would leak a permanent state entry
            continue
        airlines = str(r.get(f"{pfx}Airlines") or "").strip()
        codes = {c.strip() for c in airlines.replace("/", ",").split(",") if c.strip()}
        if excluded and codes & excluded:
            continue
        route = r.get("Route") or {}
        dest = route.get("DestinationAirport", "?")
        duration_min, connections, stops = best_trip(r, int(miles), cabin)
        direct = bool(r.get(f"{pfx}Direct")) or stops == 0
        if only_direct and not direct:  # gate AFTER computing direct (the field may be unset)
            continue
        hits.append({
            "origin": route.get("OriginAirport", origin),
            "dest": dest,
            "region": route.get("DestinationRegion", ""),
            "date": day,
            "cabin": cabin,
            "airlines": airlines or "?",
            "program": r.get("Source") or route.get("Source") or "",
            "miles": int(miles),
            "seats": int(seats),
            "direct": direct,
            "duration_min": duration_min,
            "connections": connections,
            "stops": stops,
            "taxes_cents": r.get(f"{pfx}TotalTaxes") or 0,
            "link": build_link(cfg, origin, dest, day),
        })
    return hits


# --------------------------------------------------------------------------- #
# formatting + dedupe key
# --------------------------------------------------------------------------- #
def fmt_duration(minutes):
    if not minutes:
        return "?"
    h, m = divmod(int(minutes), 60)
    return f"{h}h{m:02d}m"


def fmt_note(h):
    if h.get("direct"):
        return "Nonstop"
    conns = h.get("connections") or []
    if conns:
        return "via " + "/".join(conns)
    stops = h.get("stops")
    return f"{stops} stop" + ("s" if (stops or 0) != 1 else "") if stops else "1+ stop"


def hit_key(h):
    return (f"{h['alert_id']}|{h.get('origin','')}|{h['dest']}|{h['date']}|"
            f"{h['airlines']}|{h.get('program','')}")


def dedupe(all_hits, state, today_iso):
    """Return (new_hits, pruned_state). A hit is new if unseen, or cheaper than the
    lowest miles previously seen for its key. Past-dated state entries are pruned."""
    new_hits = []
    for h in all_hits:
        k = hit_key(h)
        prev = state.get(k)
        prev_miles = prev.get("miles") if isinstance(prev, dict) else None
        if prev_miles is None or h["miles"] < prev_miles:
            new_hits.append(h)
        # Track the lowest price ever seen (the floor), not the last-seen price, so a
        # price that rises then dips (without beating the floor) doesn't re-alert.
        floor = h["miles"] if prev_miles is None else min(h["miles"], prev_miles)
        state[k] = {"miles": floor, "seats": h["seats"],
                    "last_seen": today_iso, "date": h["date"]}
    state = {k: v for k, v in state.items()
             if not (isinstance(v, dict) and v.get("date", "9999") < today_iso)}
    return new_hits, state


def is_designated_poller():
    """True if this host may poll for real. A missing marker (fresh/single host)
    allows; a marker for a different host blocks."""
    if not os.path.isfile(POLL_HOST_PATH):
        return True
    try:
        with open(POLL_HOST_PATH) as f:
            recorded = f.read().strip()
    except OSError:
        return False  # marker exists but unreadable -> fail closed, don't double-alert
    return not recorded or recorded == socket.gethostname()


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    cfg = load_config()

    if not is_designated_poller():
        print(f"Not the designated poll host (this={socket.gethostname()}); "
              f"skipping poll, writing nothing.", file=sys.stderr)
        print("NEW_HITS: 0")
        return

    # Single-instance guard: if a prior (slow) poll still holds the lock, skip this
    # tick rather than racing state.json. The OS releases the lock when we exit.
    lock_fd = open(os.path.join(HERE, ".run.lock"), "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another poll is still running; skipping this tick", file=sys.stderr)
        print("NEW_HITS: 0")
        return

    api_key = os.environ.get(cfg["api_key_env"], "").strip()
    if not api_key:
        sys.exit(f"ERROR: {cfg['api_key_env']} not set in the environment")

    defaults = cfg["defaults"]
    alerts = [a for a in load_alerts() if a.get("enabled", True)]
    state = load_state()
    today_iso = date.today().isoformat()

    all_hits = []
    for alert in alerts:
        aid = alert.get("id") or content_id(alert)
        name = alert.get("name", aid)
        params = resolve(alert, defaults)
        excluded = cfg["_excluded_airlines"] if params["respect_exclusions"] else set()
        start, end = alert_window(alert, defaults)
        print(f"[{name}] {','.join(alert.get('origins', []))} -> "
              f"{','.join(alert.get('destinations', []))}  {start}..{end} "
              f"(business < {params['max_miles']:,})", file=sys.stderr)
        for origin in alert.get("origins", []):
            for dest in alert.get("destinations", []):
                try:
                    rows = seats_aero.search(cfg["base_url"], api_key, origin, dest,
                                             params["cabin"], start, end)
                except seats_aero.AuthError as e:
                    sys.exit(f"ERROR: {e}")
                except seats_aero.SearchError as e:
                    print(f"  ! {origin}-{dest}: {e}", file=sys.stderr)
                    continue
                try:
                    hits = filter_rows(rows, params, excluded, cfg, origin)
                except Exception as e:  # a malformed row must not kill the whole poll
                    print(f"  ! {origin}-{dest}: filter error: {e}", file=sys.stderr)
                    continue
                for h in hits:
                    h["alert_id"] = aid
                    h["alert_name"] = name
                all_hits.extend(hits)
                print(f"  {origin}-{dest}: {len(rows)} rows, {len(hits)} under cap",
                      file=sys.stderr)

    new_hits, state = dedupe(all_hits, state, today_iso)
    save_state(state)

    new_hits.sort(key=lambda h: (h.get("alert_name", ""), h["miles"], h["date"]))
    _atomic_write(NEW_HITS_PATH, json.dumps(new_hits, indent=2))

    print(f"NEW_HITS: {len(new_hits)}")
    if new_hits:
        print(json.dumps(new_hits))

    if new_hits:
        print(f"\n{len(new_hits)} NEW under-cap seats:", file=sys.stderr)
        for h in new_hits:
            route = f"{h.get('origin','?')}-{h['dest']}"
            print(f"  {route:<11}{h['date']:<12}{h['airlines']:<9}"
                  f"{h['miles']:>8,}  {h['seats']:>3} seat  "
                  f"{fmt_duration(h['duration_min']):>7}  {fmt_note(h)}", file=sys.stderr)
    else:
        print("No new availability since last run.", file=sys.stderr)


if __name__ == "__main__":
    main()
