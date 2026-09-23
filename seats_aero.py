#!/usr/bin/env python3
"""Stdlib urllib client for the Seats.aero Partner API cached search.

No third-party dependencies. Returns availability rows with the *native* API field
names (un-suffixed — e.g. ``JMileageCost``, not ``JMileageCostRaw``), and coerces
the string-typed mileage/seat/tax fields to ``int`` so callers can compare them
numerically without a ``TypeError``.

Trip detail (duration, connections, stops) comes back inline on each row under
``AvailabilityTrips`` because we pass ``include_trips=true`` — there is no separate
``/trips/{id}`` call.
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://seats.aero/partnerapi"
# Seats.aero is fronted by Cloudflare, which rejects the default Python urllib
# User-Agent ("Python-urllib/x.y") with HTTP 403 / error 1010. Send an explicit one.
USER_AGENT = "award-flight-alert/1.0 (+https://github.com/haiguan28/award-flight-alert)"
_AUTH_STATUSES = (401, 403)
# Row-level fields the API may return as JSON strings; coerce to int for comparisons.
# Covered for every cabin prefix (Y=economy, W=premium, J=business, F=first).
_INT_FIELDS = tuple(f"{p}{s}" for p in ("Y", "W", "J", "F")
                    for s in ("MileageCost", "RemainingSeats", "TotalTaxes"))


class SearchError(Exception):
    """Recoverable per-leg error — the caller should skip this leg and continue."""


class AuthError(Exception):
    """Fatal auth error (401/403) — the caller should stop the whole run."""


def _coerce_ints(row):
    """Cast the string-typed J* fields to int in place (defensive if already int)."""
    for field in _INT_FIELDS:
        value = row.get(field)
        if isinstance(value, str):
            try:
                row[field] = int(value)
            except ValueError:
                row[field] = 0
    return row


def build_search_url(base_url, origin, dest, cabin, start_date, end_date, take=500,
                     skip=0, cursor=None, sources=None):
    params = {
        "origin_airport": origin,
        "destination_airport": dest,
        "start_date": start_date,
        "end_date": end_date,
        "include_trips": "true",
        "take": str(take),
    }
    if cabin:  # omitted -> rows for every cabin (each row carries all Y/W/J/F fields)
        params["cabin"] = cabin
    if sources:  # only these programs — fewer rows, fewer pages
        params["sources"] = ",".join(sorted(sources))
    if cursor is not None:  # page N>1: the API wants the first page's cursor + a row offset
        params["cursor"] = str(cursor)
        params["skip"] = str(skip)
    return base_url.rstrip("/") + "/search?" + urllib.parse.urlencode(params)


def _get_json(url, api_key, label, urlopen, timeout, max_retries):
    """GET one page with retry/backoff. Raises AuthError / SearchError (see search)."""
    req = urllib.request.Request(url, headers={
        "Partner-Authorization": api_key,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,  # required — Cloudflare blocks the default urllib UA
    })
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8")) or {}
        except urllib.error.HTTPError as e:
            if e.code in _AUTH_STATUSES:
                raise AuthError(f"HTTP {e.code}: Seats.aero rejected the API key") from e
            if e.code == 429 and attempt < max_retries:
                time.sleep((attempt + 1) * 2)  # backoff, then retry
                last_err = e
                continue
            raise SearchError(f"HTTP {e.code} for {label}") from e
        except (urllib.error.URLError, OSError) as e:  # OSError: reset, timeout, DNS, TLS
            last_err = e
            if attempt < max_retries:
                time.sleep(attempt + 1)
                continue
            raise SearchError(f"network error for {label}: {e}") from e
        except (json.JSONDecodeError, ValueError) as e:
            raise SearchError(f"non-JSON response for {label}") from e
    raise SearchError(f"exhausted retries for {label}: {last_err}")


def search(base_url, api_key, origin, dest, cabin, start_date, end_date,
           take=500, timeout=30, rate_limit_sleep=0.4, max_retries=2, max_pages=10,
           sources=None, _urlopen=None):
    """Query one origin->dest leg. Returns a list of native-shaped rows.

    Follows the API's ``hasMore``/``cursor`` pagination so a wide window isn't
    silently truncated at ``take`` rows. Each page is one API call, so ``max_pages``
    bounds the quota a single leg can burn; hitting it is reported on stderr.

    Raises ``AuthError`` on 401/403 (fatal — bad or missing key); ``SearchError``
    on any other failure (429 after retries, timeout, non-JSON, other HTTP) so the
    caller can skip the leg without crashing the run. ``_urlopen`` is an injection
    seam for tests.
    """
    urlopen = _urlopen or urllib.request.urlopen
    label = f"{origin}-{dest}"
    rows, cursor = [], None
    for _page in range(max_pages):
        url = build_search_url(base_url, origin, dest, cabin, start_date, end_date,
                               take, skip=len(rows), cursor=cursor, sources=sources)
        payload = _get_json(url, api_key, label, urlopen, timeout, max_retries)
        page = payload.get("data") or []
        rows.extend(page)
        if rate_limit_sleep:
            time.sleep(rate_limit_sleep)  # be polite between calls
        cursor = payload.get("cursor")
        if not payload.get("hasMore") or not page or cursor is None:
            break
    else:
        print(f"  ! {label}: stopped after {max_pages} pages ({len(rows)} rows); "
              "results may be incomplete — narrow the date window", file=sys.stderr)
    return [_coerce_ints(r) for r in rows]
