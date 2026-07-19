import os
import stat
import tempfile
import unittest

import check

CFG = {"seats_aero_url_template": "https://seats.aero/search?origins={origin}&destinations={dest}&startDate={date}&endDate={date}"}
PARAMS = {"max_miles": 90000, "min_seats": 1, "only_direct": False}


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
        p = dict(PARAMS, only_direct=True)
        self.assertEqual(check.filter_rows([native_row(JDirect=False)],
                                           p, set(), CFG, "SFO"), [])


class TestBestBusinessTrip(unittest.TestCase):
    def test_prefers_cost_match_then_shortest(self):
        row = native_row(AvailabilityTrips=[
            {"Cabin": "business", "MileageCost": 82000, "TotalDuration": 800, "Connections": [], "Stops": 0},
            {"Cabin": "business", "MileageCost": 82000, "TotalDuration": 600, "Connections": [], "Stops": 0},
            {"Cabin": "economy", "MileageCost": 40000, "TotalDuration": 500},
        ])
        dur, conns, stops = check.best_business_trip(row, 82000)
        self.assertEqual(dur, 600)
        self.assertEqual(stops, 0)

    def test_no_business_trip(self):
        self.assertEqual(check.best_business_trip({"AvailabilityTrips": []}, 1),
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
        b = dict(a, max_miles=80000)
        self.assertNotEqual(check.content_id(a), check.content_id(b))


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
        new, state = check.dedupe([self._hit()], state, "2026-07-18")
        self.assertEqual(new, [])  # already seen
        new, state = check.dedupe([self._hit(miles=75000)], state, "2026-07-18")
        self.assertEqual(len(new), 1)  # cheaper -> new

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


if __name__ == "__main__":
    unittest.main()
