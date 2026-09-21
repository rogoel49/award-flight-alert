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
import wallet

# Map an alert's cabin to the API field prefix and trip Cabin value.
CABIN_PREFIX = {"economy": "Y", "premium": "W", "business": "J", "first": "F"}
# Pseudo-cabin: watch every cabin under one cap, for the price of ONE API call per
# leg (vs. four single-cabin alerts burning 4x the daily Partner API quota).
ANY_CABIN = "any"
# An alert with a return window is a round trip. These fields define it.
ROUND_TRIP_FIELDS = ("return_start", "return_end", "min_nights", "max_nights",
                     "max_total_miles")


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
# Outbox of hits not yet delivered. check.py appends, notify.py acks on success — so
# a failed send is retried next poll instead of being lost behind the dedupe state.
PENDING_PATH = os.environ.get("AWARD_PENDING", os.path.join(HERE, "pending_hits.json"))
PENDING_MAX = 200  # bound the outbox if delivery stays broken for a long time


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
    # Newer optional fields join the identity only when set, so alerts created before
    # they existed keep their id (and their dedupe state).
    if alert.get("programs"):
        key += "|programs=" + ",".join(sorted(alert["programs"]))
    for field in ROUND_TRIP_FIELDS:
        if alert.get(field) is not None:
            key += f"|{field}={alert[field]}"
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
    programs = params.get("programs")    # set of bookable programs, or None for all
    funding = params.get("funding") or {}
    hits = []
    for r in rows:
        if not r.get(f"{pfx}Available"):
            continue
        miles = r.get(f"{pfx}MileageCost") or 0
        if miles <= 0 or miles >= cap:
            continue
        # Several programs (e.g. american) report 0 remaining seats on an *available*
        # row — that means "count unknown", not "none". Availability itself proves
        # one seat, so keep it for a 1-seat alert; a 2+ seat alert can't verify it.
        seats = r.get(f"{pfx}RemainingSeats") or 0
        if seats < min_seats and not (seats == 0 and min_seats <= 1):
            continue
        program = r.get("Source") or (r.get("Route") or {}).get("Source") or ""
        if programs is not None and program not in programs:
            continue  # a seat in a program you can't book is noise
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
            "program": program,
            "funding": funding.get(program, []),
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


def alert_cabins(params):
    """The concrete cabins an alert watches: all of them for ``any``, else its one."""
    return list(CABIN_PREFIX) if params["cabin"] == ANY_CABIN else [params["cabin"]]


def api_cabin(params):
    """The cabin filter to send to the API — none for ``any`` (one call, all cabins)."""
    return None if params["cabin"] == ANY_CABIN else params["cabin"]


def filter_alert_rows(rows, params, excluded, cfg, origin):
    """filter_rows across every cabin the alert watches. A row can yield one hit per
    cabin (economy AND business open on the same date are separate hits)."""
    hits = []
    for cabin in alert_cabins(params):
        hits.extend(filter_rows(rows, dict(params, cabin=cabin), excluded, cfg, origin))
    return hits


# --------------------------------------------------------------------------- #
# round trips
# --------------------------------------------------------------------------- #
def is_round_trip(alert):
    return bool(alert.get("return_start") and alert.get("return_end"))


def _nights(out_day, back_day):
    try:
        return (date.fromisoformat(back_day) - date.fromisoformat(out_day)).days
    except (TypeError, ValueError):
        return None


def pair_round_trips(outs, backs, min_nights=1, max_nights=None, max_total_miles=None):
    """Join outbound and return leg-hits into bookable round trips.

    A pair is valid when the return departs ``min_nights..max_nights`` days after the
    outbound, both legs are the same cabin, and (if set) the combined miles beat
    ``max_total_miles``. Each leg already passed the alert's per-leg filters. The
    legs may be different programs — they're booked as two one-way awards.

    Many leg combinations share a date pair, so only the best (cheapest, then
    fastest) is reported per (outbound date, return date, cabin): one alert per
    *trip you could take*, not per permutation of flights.
    """
    best = {}
    for o in outs:
        for b in backs:
            if o["cabin"] != b["cabin"]:
                continue
            nights = _nights(o["date"], b["date"])
            if nights is None or nights < min_nights:
                continue
            if max_nights is not None and nights > max_nights:
                continue
            total = o["miles"] + b["miles"]
            if max_total_miles is not None and total >= max_total_miles:
                continue
            rank = (total, (o.get("duration_min") or 10 ** 6) + (b.get("duration_min") or 10 ** 6))
            k = (o["date"], b["date"], o["cabin"])
            if k not in best or rank < best[k][0]:
                best[k] = (rank, o, b, nights)
    return [_pair_hit(o, b, nights) for _rank, o, b, nights in best.values()]


def _pair_hit(o, b, nights):
    """A round trip in the ordinary hit shape (so every notifier/agent that handles a
    hit handles this), plus ``trip``/``return_date``/``nights`` and the two legs."""
    seats = 0 if 0 in (o["seats"], b["seats"]) else min(o["seats"], b["seats"])
    return {
        "trip": "round",
        "origin": o["origin"], "dest": o["dest"], "region": o.get("region", ""),
        "date": o["date"], "return_date": b["date"], "nights": nights,
        "cabin": o["cabin"],
        "airlines": f"{o['airlines']} / {b['airlines']}",
        "program": f"{o['program']} / {b['program']}",
        "funding": [],
        "miles": o["miles"] + b["miles"],
        "seats": seats,  # 0 = at least one leg's count is unknown
        "direct": bool(o["direct"] and b["direct"]),
        "duration_min": None, "connections": [], "stops": None,
        "taxes_cents": (o.get("taxes_cents") or 0) + (b.get("taxes_cents") or 0),
        "link": o.get("link", ""), "return_link": b.get("link", ""),
        "outbound": o, "inbound": b,
    }


# --------------------------------------------------------------------------- #
# formatting + dedupe key
# --------------------------------------------------------------------------- #
def fmt_duration(minutes):
    if not minutes:
        return "?"
    h, m = divmod(int(minutes), 60)
    return f"{h}h{m:02d}m"


def fmt_seats(h):
    """Seat count for display; 0 means the program didn't report one."""
    return str(h.get("seats") or "?")


def fmt_program(h):
    """Program plus how you'd fund it, e.g. ``aeroplan ← Chase UR, Amex MR``."""
    program = h.get("program") or ""
    how = [f for f in (h.get("funding") or []) if f != "miles you hold"]
    return f"{program} \u2190 {', '.join(how)}" if how else program


def fmt_route(h, sep="-"):
    if h.get("trip") == "round":
        return f"{h.get('origin', '?')}\u21c4{h.get('dest', '?')}"
    return f"{h.get('origin', '?')}{sep}{h.get('dest', '?')}"


def fmt_dates(h):
    if h.get("trip") == "round":
        return f"{h.get('date', '?')} \u2192 {h.get('return_date', '?')}"
    return str(h.get("date", ""))


def fmt_leg(leg):
    """One leg of a round trip, e.g. ``NH aeroplan ← Chase UR 75,000 (Nonstop)``."""
    return (f"{leg.get('airlines', '?')} {fmt_program(leg)} {leg.get('miles', 0):,} "
            f"({fmt_note(leg)})")


def fmt_note(h):
    if h.get("trip") == "round":
        return (f"{h.get('nights', '?')} nights \u00b7 out: {fmt_leg(h['outbound'])} "
                f"\u00b7 back: {fmt_leg(h['inbound'])}")
    if h.get("direct"):
        return "Nonstop"
    conns = h.get("connections") or []
    if conns:
        return "via " + "/".join(conns)
    stops = h.get("stops")
    return f"{stops} stop" + ("s" if (stops or 0) != 1 else "") if stops else "1+ stop"


def hit_key(h):
    if h.get("trip") == "round":
        # Keyed on the trip, not the flights: re-alert for a new date pair or a lower
        # total, not because the cheapest pairing moved to a different airline.
        return f"{h['alert_id']}|RT|{h['date']}|{h['return_date']}|{h.get('cabin','')}"
    # cabin is part of the key: an any-cabin alert yields several hits per row, and
    # a cheap economy seat must not mask (dedupe away) a business seat on the same date.
    return (f"{h['alert_id']}|{h.get('origin','')}|{h['dest']}|{h['date']}|"
            f"{h['airlines']}|{h.get('program','')}|{h.get('cabin','')}")


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


def load_pending(path=None):
    path = path or PENDING_PATH
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def queue_pending(new_hits, today_iso, path=None):
    """Add this poll's new hits to the outbox (keyed, so a re-detected hit replaces
    itself rather than duplicating) and drop entries whose flight date has passed."""
    pending = {hit_key(h): h for h in load_pending(path)
               if str(h.get("date", "9999")) >= today_iso}
    for h in new_hits:
        pending[hit_key(h)] = h
    items = list(pending.values())[-PENDING_MAX:]
    _atomic_write(path or PENDING_PATH, json.dumps(items, indent=2))
    return items


def ack_pending(delivered, path=None):
    """Remove delivered hits from the outbox, leaving any queued since they were read."""
    done = {hit_key(h) for h in delivered}
    rest = [h for h in load_pending(path) if hit_key(h) not in done]
    _atomic_write(path or PENDING_PATH, json.dumps(rest, indent=2))


def search_legs(cfg, api_key, params, excluded, origins, dests, start, end, cache):
    """Search + filter every origin x dest leg for one direction of an alert. A leg
    that errors is reported and skipped; an auth failure stops the whole run."""
    out = []
    sources = params.get("programs")
    for origin in origins:
        for dest in dests:
            ck = (origin, dest, api_cabin(params), start, end,
                  tuple(sorted(sources)) if sources else None)
            try:
                if ck not in cache:
                    cache[ck] = seats_aero.search(cfg["base_url"], api_key, origin, dest,
                                                  api_cabin(params), start, end,
                                                  sources=sources)
                rows = cache[ck]
            except seats_aero.AuthError as e:
                sys.exit(f"ERROR: {e}")
            except seats_aero.SearchError as e:
                print(f"  ! {origin}-{dest}: {e}", file=sys.stderr)
                continue
            try:
                hits = filter_alert_rows(rows, params, excluded, cfg, origin)
            except Exception as e:  # a malformed row must not kill the whole poll
                print(f"  ! {origin}-{dest}: filter error: {e}", file=sys.stderr)
                continue
            out.extend(hits)
            print(f"  {origin}-{dest} {start}..{end}: {len(rows)} rows, {len(hits)} under cap",
                  file=sys.stderr)
    return out


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

    try:
        wallet.funding(cfg)
    except wallet.WalletError as e:
        sys.exit(f"ERROR: {e}")

    defaults = cfg["defaults"]
    alerts = [a for a in load_alerts() if a.get("enabled", True)]
    state = load_state()
    today_iso = date.today().isoformat()

    cache = {}  # one poll, one call per distinct search — overlapping alerts share rows
    all_hits = []
    for alert in alerts:
        aid = alert.get("id") or content_id(alert)
        name = alert.get("name", aid)
        params = resolve(alert, defaults)
        params["programs"], params["funding"] = wallet.resolve_programs(alert, cfg)
        excluded = cfg["_excluded_airlines"] if params["respect_exclusions"] else set()
        start, end = alert_window(alert, defaults)
        origins, dests = alert.get("origins", []), alert.get("destinations", [])
        arrow = "<->" if is_round_trip(alert) else "->"
        print(f"[{name}] {','.join(origins)} {arrow} {','.join(dests)}  {start}..{end} "
              f"({params['cabin']} < {params['max_miles']:,})", file=sys.stderr)
        hits = search_legs(cfg, api_key, params, excluded, origins, dests, start, end, cache)
        if is_round_trip(alert):
            backs = search_legs(cfg, api_key, params, excluded, dests, origins,
                                alert["return_start"], alert["return_end"], cache)
            n_out = len(hits)
            hits = pair_round_trips(hits, backs, int(alert.get("min_nights") or 1),
                                    alert.get("max_nights"), alert.get("max_total_miles"))
            print(f"  round trip: {n_out} outbound x {len(backs)} return legs -> "
                  f"{len(hits)} bookable date pair(s)", file=sys.stderr)
        for h in hits:
            h["alert_id"] = aid
            h["alert_name"] = name
        all_hits.extend(hits)

    new_hits, state = dedupe(all_hits, state, today_iso)
    # Outbox BEFORE state: if we die between the two writes the hits are re-detected
    # next poll (and replace themselves in the outbox) rather than vanishing.
    queue_pending(new_hits, today_iso)
    save_state(state)

    new_hits.sort(key=lambda h: (h.get("alert_name", ""), h["miles"], h["date"]))
    _atomic_write(NEW_HITS_PATH, json.dumps(new_hits, indent=2))

    print(f"NEW_HITS: {len(new_hits)}")
    if new_hits:
        print(json.dumps(new_hits))

    if new_hits:
        print(f"\n{len(new_hits)} NEW under-cap seats:", file=sys.stderr)
        for h in new_hits:
            print(f"  {fmt_route(h):<11}{fmt_dates(h):<12} {h['airlines']:<9}"
                  f"{h['miles']:>8,}  {fmt_seats(h):>3} seat  "
                  f"{fmt_duration(h['duration_min']):>7}  {fmt_note(h)}", file=sys.stderr)
    else:
        print("No new availability since last run.", file=sys.stderr)


if __name__ == "__main__":
    main()
