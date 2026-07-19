import json
import os
import shutil
import stat
import tempfile
import unittest

import check

CFG = {"seats_aero_url_template": "https://seats.aero/search?origins={origin}&destinations={dest}&startDate={date}&endDate={date}"}
PARAMS = {"cabin": "business", "max_miles": 90000, "min_seats": 1, "only_direct": False}


def native_row(**over):
    row = {
        "JAvailable": True,
        "JMileageCost": 82000,
        "JRemainingSeats": 2,
        "JTotalTaxes": 36640,
        "JDirect": False,
        "JAirlines": "NH",
        "Route": {"OriginAirport": "SFO", "DestinationAirport": "NRT",
                  "DestinationRegion": "Asia", "Source": "aeroplan"},
        "Source": "aeroplan",
        "Date": "2026-11-20",
        "AvailabilityTrips": [
            {"Cabin": "business", "MileageCost": 82000, "TotalDuration": 700,
             "Connections": ["MNL"], "Stops": 1},
        ],
    }
    row.update(over)
    return row


class TestFilterRows(unittest.TestCase):
    def test_native_row_produces_a_hit(self):
        hits = check.filter_rows([native_row()], PARAMS, set(), CFG, "SFO")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["miles"], 82000)
        self.assertEqual(hits[0]["dest"], "NRT")
        self.assertIn("MNL", hits[0]["connections"])
        self.assertIn("seats.aero", hits[0]["link"])

    def test_raw_suffixed_row_produces_nothing(self):
        # The old CLI shape (JMileageCostRaw etc.) must NOT match — proves the rewire.
        raw = {"JAvailableRaw": True, "JMileageCostRaw": 82000,
               "JRemainingSeatsRaw": 2, "Route": {"DestinationAirport": "NRT"}}
        self.assertEqual(check.filter_rows([raw], PARAMS, set(), CFG, "SFO"), [])

    def test_over_cap_dropped(self):
        self.assertEqual(check.filter_rows([native_row(JMileageCost=95000)],
                                           PARAMS, set(), CFG, "SFO"), [])

    def test_unavailable_dropped(self):
        self.assertEqual(check.filter_rows([native_row(JAvailable=False)],
                                           PARAMS, set(), CFG, "SFO"), [])

    def test_too_few_seats_dropped(self):
        p = dict(PARAMS, min_seats=3)
        self.assertEqual(check.filter_rows([native_row(JRemainingSeats=2)],
                                           p, set(), CFG, "SFO"), [])

    def test_excluded_airline_dropped(self):
        self.assertEqual(check.filter_rows([native_row(JAirlines="AI")],
                                           PARAMS, {"AI"}, CFG, "SFO"), [])

    def test_only_direct_drops_connection(self):
        # JDirect False AND the trip has a stop -> a direct-only alert drops it
        r = native_row(JDirect=False)
        r["AvailabilityTrips"] = [{"Cabin": "business", "MileageCost": 82000,
                                   "TotalDuration": 700, "Connections": ["MNL"], "Stops": 1}]
        p = dict(PARAMS, only_direct=True)
        self.assertEqual(check.filter_rows([r], p, set(), CFG, "SFO"), [])

    def test_only_direct_keeps_nonstop_trip_when_jdirect_unset(self):
        # JDirect unset but the trip is Stops==0 -> a direct-only alert KEEPS it
        r = native_row(JDirect=False)
        r["AvailabilityTrips"] = [{"Cabin": "business", "MileageCost": 82000,
                                   "TotalDuration": 700, "Connections": [], "Stops": 0}]
        p = dict(PARAMS, only_direct=True)
        self.assertEqual(len(check.filter_rows([r], p, set(), CFG, "SFO")), 1)

    def test_economy_cabin_reads_Y_fields(self):
        row = {"YAvailable": True, "YMileageCost": 40000, "YRemainingSeats": 4,
               "YTotalTaxes": 100, "YDirect": True, "YAirlines": "UA",
               "Route": {"OriginAirport": "SFO", "DestinationAirport": "NRT"},
               "Date": "2026-11-20",
               "AvailabilityTrips": [{"Cabin": "economy", "MileageCost": 40000,
                                      "TotalDuration": 600, "Stops": 0}]}
        hits = check.filter_rows([row], dict(PARAMS, cabin="economy"), set(), CFG, "SFO")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["miles"], 40000)
        self.assertEqual(hits[0]["cabin"], "economy")

    def test_dateless_row_skipped(self):
        r = native_row()
        del r["Date"]
        self.assertEqual(check.filter_rows([r], PARAMS, set(), CFG, "SFO"), [])

    def test_null_trips_still_produces_hit(self):
        hits = check.filter_rows([native_row(AvailabilityTrips=None)], PARAMS, set(), CFG, "SFO")
        self.assertEqual(len(hits), 1)
        self.assertIsNone(hits[0]["duration_min"])

    def test_non_string_airlines_does_not_crash(self):
        hits = check.filter_rows([native_row(JAirlines=["NH", "UA"])], PARAMS, set(), CFG, "SFO")
        self.assertEqual(len(hits), 1)  # coerced to str, no AttributeError


class TestBestTrip(unittest.TestCase):
    def test_prefers_cost_match_then_shortest(self):
        row = native_row(AvailabilityTrips=[
            {"Cabin": "business", "MileageCost": 82000, "TotalDuration": 800, "Connections": [], "Stops": 0},
            {"Cabin": "business", "MileageCost": 82000, "TotalDuration": 600, "Connections": [], "Stops": 0},
            {"Cabin": "economy", "MileageCost": 40000, "TotalDuration": 500},
        ])
        dur, conns, stops = check.best_trip(row, 82000, "business")
        self.assertEqual(dur, 600)
        self.assertEqual(stops, 0)

    def test_no_cost_match_falls_back_to_shortest(self):
        row = native_row(AvailabilityTrips=[
            {"Cabin": "business", "MileageCost": 82000, "TotalDuration": 800, "Stops": 1},
            {"Cabin": "business", "MileageCost": 85000, "TotalDuration": 600, "Stops": 0},
        ])
        dur, _, _ = check.best_trip(row, 999999, "business")  # no exact match
        self.assertEqual(dur, 600)

    def test_string_duration_does_not_crash(self):
        row = native_row(AvailabilityTrips=[
            {"Cabin": "business", "MileageCost": 82000, "TotalDuration": "oops", "Stops": 0},
            {"Cabin": "business", "MileageCost": 82000, "TotalDuration": 600, "Stops": 0},
        ])
        dur, _, _ = check.best_trip(row, 82000, "business")
        self.assertEqual(dur, 600)  # non-numeric sorted last, no TypeError

    def test_no_matching_cabin(self):
        self.assertEqual(check.best_trip({"AvailabilityTrips": []}, 1, "business"),
                         (None, [], None))


class TestResolveAndId(unittest.TestCase):
    def test_resolve_override_and_inherit(self):
        defaults = {"cabin": "business", "max_miles": 90000, "min_seats": 1}
        params = check.resolve({"max_miles": 70000}, defaults)
        self.assertEqual(params["max_miles"], 70000)   # override
        self.assertEqual(params["cabin"], "business")  # inherited

    def test_content_id_stable_and_distinct(self):
        a = {"origins": ["SFO"], "destinations": ["NRT", "HND"], "cabin": "business",
             "max_miles": 90000, "start_date": "2026-11-01", "end_date": "2026-11-30"}
        a_reordered = dict(a, destinations=["HND", "NRT"])  # order-insensitive
        self.assertEqual(check.content_id(a), check.content_id(a_reordered))
        # every identity field must distinguish otherwise-identical alerts
        self.assertNotEqual(check.content_id(a), check.content_id(dict(a, max_miles=80000)))
        self.assertNotEqual(check.content_id(a), check.content_id(dict(a, min_seats=4)))
        self.assertNotEqual(check.content_id(a), check.content_id(dict(a, only_direct=True)))


class TestDedupe(unittest.TestCase):
    def _hit(self, **over):
        h = {"alert_id": "a1", "origin": "SFO", "dest": "NRT", "date": "2026-11-20",
             "airlines": "NH", "program": "aeroplan", "miles": 82000, "seats": 2}
        h.update(over)
        return h

    def test_new_then_seen_then_cheaper(self):
        state = {}
        new, state = check.dedupe([self._hit()], state, "2026-07-18")
        self.assertEqual(len(new), 1)
        k = check.hit_key(self._hit())
        self.assertEqual(state[k], {"miles": 82000, "seats": 2,
                                    "last_seen": "2026-07-18", "date": "2026-11-20"})
        new, state = check.dedupe([self._hit()], state, "2026-07-18")
        self.assertEqual(new, [])  # already seen
        new, state = check.dedupe([self._hit(miles=75000)], state, "2026-07-18")
        self.assertEqual(len(new), 1)  # cheaper -> new

    def test_tracks_floor_not_last_price(self):
        state = {}
        _, state = check.dedupe([self._hit(miles=82000)], state, "2026-07-18")
        _, state = check.dedupe([self._hit(miles=75000)], state, "2026-07-18")  # dips
        # price rises to 80000 — must NOT re-alert (still above the 75000 floor)
        new, state = check.dedupe([self._hit(miles=80000)], state, "2026-07-18")
        self.assertEqual(new, [])
        self.assertEqual(state[check.hit_key(self._hit())]["miles"], 75000)  # floor kept

    def test_per_alert_independence(self):
        state = {}
        _, state = check.dedupe([self._hit(alert_id="a1")], state, "2026-07-18")
        # same physical seat under a different alert id is a different key -> new
        new, state = check.dedupe([self._hit(alert_id="a2")], state, "2026-07-18")
        self.assertEqual(len(new), 1)

    def test_past_dated_state_pruned(self):
        state = {}
        _, state = check.dedupe([self._hit(date="2020-01-01")], state, "2026-07-18")
        self.assertEqual(state, {})  # travel date in the past -> pruned


class TestPollHostGuard(unittest.TestCase):
    def setUp(self):
        self.orig = check.POLL_HOST_PATH
        self.d = tempfile.mkdtemp()
        check.POLL_HOST_PATH = os.path.join(self.d, ".poll-host")

    def tearDown(self):
        check.POLL_HOST_PATH = self.orig

    def _write(self, host):
        with open(check.POLL_HOST_PATH, "w") as f:
            f.write(host)

    def test_missing_marker_allows(self):
        self.assertTrue(check.is_designated_poller())

    def test_matching_host_allows(self):
        import socket
        self._write(socket.gethostname())
        self.assertTrue(check.is_designated_poller())

    def test_other_host_blocks(self):
        self._write("some-other-host-that-is-not-this-one")
        self.assertFalse(check.is_designated_poller())


class TestAtomicWrite(unittest.TestCase):
    def test_written_0600(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "state.json")
        check._atomic_write(p, "{}")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        with open(p) as f:
            self.assertEqual(f.read(), "{}")


class TestLoadState(unittest.TestCase):
    def test_corrupt_state_resets_and_sets_aside(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "state.json")
        with open(p, "w") as f:
            f.write("{not valid json")
        self.assertEqual(check.load_state(p), {})           # degrades to empty
        self.assertTrue(os.path.exists(p + ".corrupt"))     # visible, not silent

    def test_missing_state_is_empty(self):
        self.assertEqual(check.load_state("/no/such/state.json"), {})


class TestMainIntegration(unittest.TestCase):
    PATHS = ("CONFIG_PATH", "ALERTS_PATH", "STATE_PATH", "NEW_HITS_PATH", "POLL_HOST_PATH")

    def setUp(self):
        import seats_aero
        self.seats = seats_aero
        self._orig_search = seats_aero.search
        self._saved = {a: getattr(check, a) for a in self.PATHS}
        self.dir = tempfile.mkdtemp()
        check.CONFIG_PATH = os.path.join(self.dir, "config.json")
        check.ALERTS_PATH = os.path.join(self.dir, "alerts.json")
        check.STATE_PATH = os.path.join(self.dir, "state.json")
        check.NEW_HITS_PATH = os.path.join(self.dir, "new_hits.json")
        check.POLL_HOST_PATH = os.path.join(self.dir, ".poll-host")  # absent -> allowed
        with open(check.CONFIG_PATH, "w") as f:
            json.dump({"api_key_env": "AWARD_TEST_KEY_MAIN",
                       "defaults": {"cabin": "business", "max_miles": 90000, "min_seats": 1},
                       "seats_aero_url_template": "https://seats.aero/x?o={origin}&d={dest}"}, f)
        with open(check.ALERTS_PATH, "w") as f:
            json.dump({"alerts": [{"id": "a1", "name": "SFO-NRT", "origins": ["SFO"],
                                   "destinations": ["NRT"], "enabled": True}]}, f)
        os.environ["AWARD_TEST_KEY_MAIN"] = "k"

    def tearDown(self):
        self.seats.search = self._orig_search
        for a, v in self._saved.items():
            setattr(check, a, v)
        os.environ.pop("AWARD_TEST_KEY_MAIN", None)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run_main(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            check.main()
        return buf.getvalue()

    def test_happy_path_writes_new_hits(self):
        self.seats.search = lambda *a, **k: [native_row()]
        out = self._run_main()
        self.assertIn("NEW_HITS: 1", out)
        with open(check.NEW_HITS_PATH) as f:
            hits = json.load(f)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["alert_name"], "SFO-NRT")
        self.assertEqual(hits[0]["alert_id"], "a1")

    def test_auth_error_is_fatal(self):
        self.seats.search = lambda *a, **k: (_ for _ in ()).throw(self.seats.AuthError("bad"))
        with self.assertRaises(SystemExit):
            self._run_main()

    def test_search_error_skips_leg_and_run_completes(self):
        self.seats.search = lambda *a, **k: (_ for _ in ()).throw(self.seats.SearchError("timeout"))
        out = self._run_main()
        self.assertIn("NEW_HITS: 0", out)


if __name__ == "__main__":
    unittest.main()
