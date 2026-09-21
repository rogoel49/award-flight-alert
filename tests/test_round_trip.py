import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import check
import notify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def leg(origin, dest, day, miles, cabin="business", program="aeroplan", airlines="NH",
        seats=2, direct=True, duration=600):
    return {"origin": origin, "dest": dest, "date": day, "cabin": cabin, "miles": miles,
            "program": program, "funding": [], "airlines": airlines, "seats": seats,
            "direct": direct, "duration_min": duration, "connections": [], "stops": 0,
            "taxes_cents": 5000, "link": f"https://seats.aero/{origin}{dest}{day}"}


OUT = [leg("SFO", "HND", "2026-12-12", 75000), leg("SFO", "HND", "2026-12-14", 60000)]
BACK = [leg("HND", "SFO", "2026-12-20", 70000, program="alaska", airlines="JL"),
        leg("HND", "SFO", "2026-12-26", 50000)]


class TestPairing(unittest.TestCase):
    def pairs(self, **kw):
        hits = check.pair_round_trips(OUT, BACK, **kw)
        return sorted((h["date"], h["return_date"], h["miles"]) for h in hits)

    def test_all_valid_date_pairs(self):
        self.assertEqual(len(self.pairs()), 4)

    def test_trip_length_window(self):
        self.assertEqual(self.pairs(min_nights=7, max_nights=9),
                         [("2026-12-12", "2026-12-20", 145000)])

    def test_combined_cap_is_exclusive_like_the_leg_cap(self):
        self.assertEqual(self.pairs(max_total_miles=130000),
                         [("2026-12-14", "2026-12-26", 110000),
                          ("2026-12-12", "2026-12-26", 125000)][::-1])
        self.assertEqual(self.pairs(max_total_miles=110000), [])

    def test_return_before_outbound_never_pairs(self):
        early = [leg("HND", "SFO", "2026-12-12", 1000), leg("HND", "SFO", "2026-12-10", 1000)]
        self.assertEqual(check.pair_round_trips(OUT[:1], early), [])

    def test_best_pairing_per_date_pair_cheapest_then_fastest(self):
        outs = [leg("SFO", "HND", "2026-12-12", 75000, airlines="UA", duration=900),
                leg("SFO", "HND", "2026-12-12", 75000, airlines="NH", duration=600),
                leg("SFO", "NRT", "2026-12-12", 90000, airlines="JL")]
        hits = check.pair_round_trips(outs, BACK[:1])
        self.assertEqual(len(hits), 1)  # one alert per trip, not per permutation
        self.assertEqual(hits[0]["outbound"]["airlines"], "NH")

    def test_cabins_are_not_mixed(self):
        outs = [leg("SFO", "HND", "2026-12-12", 30000, cabin="economy")]
        self.assertEqual(check.pair_round_trips(outs, BACK), [])

    def test_pair_hit_shape(self):
        h = check.pair_round_trips(OUT[:1], BACK[:1])[0]
        self.assertEqual((h["trip"], h["nights"], h["miles"]), ("round", 8, 145000))
        self.assertEqual(h["program"], "aeroplan / alaska")
        self.assertEqual(h["taxes_cents"], 10000)
        self.assertEqual(h["return_link"], BACK[0]["link"])
        unknown = check.pair_round_trips(OUT[:1], [dict(BACK[0], seats=0)])[0]
        self.assertEqual(check.fmt_seats(unknown), "?")


class TestRoundTripDedupeAndFormat(unittest.TestCase):
    def hit(self, **over):
        h = check.pair_round_trips(OUT[:1], BACK[:1])[0]
        h.update(alert_id="rt1", alert_name="Tokyo", **over)
        return h

    def test_realerts_on_cheaper_total_not_on_airline_shuffle(self):
        new, state = check.dedupe([self.hit()], {}, "2026-09-21")
        self.assertEqual(len(new), 1)
        new, state = check.dedupe([self.hit(airlines="UA / JL")], state, "2026-09-21")
        self.assertEqual(new, [])
        new, state = check.dedupe([self.hit(miles=120000)], state, "2026-09-21")
        self.assertEqual(len(new), 1)

    def test_banner_and_email_render_both_legs(self):
        title, subtitle, message = notify.macos_banner(self.hit())
        self.assertIn("SFO⇄HND", title)
        self.assertIn("145,000 mi business", title)
        self.assertEqual(subtitle, "2026-12-12 → 2026-12-20 · 8 nights")
        self.assertIn("out: NH aeroplan 75,000 (Nonstop)", message)
        self.assertIn("back: JL alaska 70,000 (Nonstop)", message)
        html = notify.build_html([self.hit()])
        self.assertIn("8 nights", html)
        self.assertIn("2026-12-20", html)


class TestRoundTripMain(unittest.TestCase):
    """main() searches both directions with the right windows and emits pairs only."""
    PATHS = ("CONFIG_PATH", "ALERTS_PATH", "STATE_PATH", "NEW_HITS_PATH", "POLL_HOST_PATH",
             "PENDING_PATH")

    def setUp(self):
        import seats_aero
        self.seats, self._orig = seats_aero, seats_aero.search
        self._saved = {a: getattr(check, a) for a in self.PATHS}
        self.dir = tempfile.mkdtemp()
        for a in self.PATHS:
            setattr(check, a, os.path.join(self.dir, a.lower()))
        with open(check.CONFIG_PATH, "w") as f:
            json.dump({"api_key_env": "AWARD_TEST_KEY_RT", "defaults": {"max_miles": 90000}}, f)
        os.environ["AWARD_TEST_KEY_RT"] = "k"

    def tearDown(self):
        self.seats.search = self._orig
        for a, v in self._saved.items():
            setattr(check, a, v)
        os.environ.pop("AWARD_TEST_KEY_RT", None)
        shutil.rmtree(self.dir, ignore_errors=True)

    def row(self, o, d, day, miles):
        return {"JAvailable": True, "JMileageCost": miles, "JRemainingSeats": 2,
                "JAirlines": "NH", "Source": "aeroplan", "Date": day,
                "Route": {"OriginAirport": o, "DestinationAirport": d}}

    def run_main(self, alert, rows_by_leg):
        import contextlib
        import io
        calls = []

        def search(base, key, o, d, cabin, start, end, **kw):
            calls.append((o, d, start, end))
            return rows_by_leg.get((o, d), [])
        self.seats.search = search
        with open(check.ALERTS_PATH, "w") as f:
            json.dump({"alerts": [alert]}, f)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            check.main()
        with open(check.NEW_HITS_PATH) as f:
            return calls, json.load(f)

    ALERT = {"id": "rt1", "name": "Tokyo", "origins": ["SFO"], "destinations": ["HND"],
             "start_date": "2026-12-12", "end_date": "2026-12-16",
             "return_start": "2026-12-18", "return_end": "2026-12-24",
             "min_nights": 6, "max_nights": 10}

    def test_both_directions_searched_and_paired(self):
        calls, hits = self.run_main(self.ALERT, {
            ("SFO", "HND"): [self.row("SFO", "HND", "2026-12-13", 75000)],
            ("HND", "SFO"): [self.row("HND", "SFO", "2026-12-20", 70000)]})
        self.assertEqual(calls, [("SFO", "HND", "2026-12-12", "2026-12-16"),
                                 ("HND", "SFO", "2026-12-18", "2026-12-24")])
        self.assertEqual([(h["trip"], h["miles"], h["nights"]) for h in hits],
                         [("round", 145000, 7)])

    def test_one_direction_alone_is_silent(self):
        _, hits = self.run_main(self.ALERT, {
            ("HND", "SFO"): [self.row("HND", "SFO", "2026-12-20", 70000)]})
        self.assertEqual(hits, [])

    def test_identical_searches_share_one_api_call(self):
        one_way = {"id": "ow", "name": "leg", "origins": ["SFO"], "destinations": ["HND"],
                   "start_date": "2026-12-12", "end_date": "2026-12-16"}
        with open(check.ALERTS_PATH, "w") as f:
            json.dump({"alerts": [one_way, self.ALERT]}, f)
        calls = []
        self.seats.search = lambda b, k, o, d, *a, **kw: calls.append((o, d)) or []
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            check.main()
        self.assertEqual(calls, [("SFO", "HND"), ("HND", "SFO")])  # not SFO-HND twice


class TestRoundTripCli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.alerts = os.path.join(self.dir, "alerts.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def cli(self, *args):
        base = ["add", "--name", "t", "--from", "SFO", "--to", "HND"]
        return subprocess.run([sys.executable, os.path.join(ROOT, "manage.py"), *base, *args],
                              capture_output=True, text=True, cwd=ROOT,
                              env=dict(os.environ, AWARD_ALERTS=self.alerts))

    def test_round_trip_alert_is_stored(self):
        r = self.cli("--start", "2026-12-12", "--end", "2026-12-16",
                     "--return-start", "2026-12-18", "--return-end", "2026-12-24",
                     "--min-nights", "6", "--max-nights", "10", "--max-total-miles", "200000")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.alerts) as f:
            a = json.load(f)["alerts"][0]
        self.assertTrue(check.is_round_trip(a))
        self.assertEqual((a["min_nights"], a["max_nights"], a["max_total_miles"]),
                         (6, 10, 200000))

    def test_invalid_round_trips_rejected(self):
        bad = (["--return-start", "2026-12-18"],                                  # half a window
               ["--return-start", "2026-12-18", "--return-end", "2026-12-24"],    # no outbound window
               ["--start", "2026-12-12", "--end", "2026-12-16", "--min-nights", "5"],  # not a round trip
               ["--start", "2026-12-12", "--end", "2026-12-16",
                "--return-start", "2026-12-01", "--return-end", "2026-12-05"])    # returns before leaving
        for args in bad:
            r = self.cli(*args)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertFalse(json.loads(r.stdout)["ok"])

    def test_one_way_id_unchanged_by_round_trip_fields(self):
        a = {"origins": ["SFO"], "destinations": ["HND"], "cabin": "business"}
        self.assertEqual(check.content_id(a), check.content_id(dict(a, return_start=None)))
        self.assertNotEqual(check.content_id(a), check.content_id(
            dict(a, return_start="2026-12-18", return_end="2026-12-24")))


if __name__ == "__main__":
    unittest.main()
