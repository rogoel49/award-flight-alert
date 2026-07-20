import json
import unittest
import urllib.error

import seats_aero


class FakeResp:
    def __init__(self, body_bytes):
        self._body = body_bytes

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def urlopen_returning(body_str, captured=None):
    def _u(req, timeout=None):
        if captured is not None:
            captured.append(req)
        return FakeResp(body_str.encode("utf-8"))
    return _u


def urlopen_raising(exc):
    def _u(req, timeout=None):
        raise exc
    return _u


class TestSearch(unittest.TestCase):
    def setUp(self):
        self._sleep = seats_aero.time.sleep
        seats_aero.time.sleep = lambda *a, **k: None  # keep retries instant

    def tearDown(self):
        seats_aero.time.sleep = self._sleep

    def test_returns_rows_and_coerces_string_mileage(self):
        body = json.dumps({"data": [
            {"JAvailable": True, "JMileageCost": "82000", "JRemainingSeats": 2,
             "JTotalTaxes": 36640, "Route": {"DestinationAirport": "NRT"}}
        ]})
        rows = seats_aero.search("https://x/partnerapi", "KEY", "SFO", "NRT",
                                 "business", "2026-11-01", "2026-11-30",
                                 rate_limit_sleep=0, _urlopen=urlopen_returning(body))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["JMileageCost"], 82000)
        self.assertIsInstance(rows[0]["JMileageCost"], int)

    def test_url_carries_include_trips_and_auth_header(self):
        captured = []
        seats_aero.search("https://x/partnerapi", "SECRET", "SFO", "NRT",
                          "business", "2026-11-01", "2026-11-30",
                          rate_limit_sleep=0,
                          _urlopen=urlopen_returning('{"data": []}', captured))
        req = captured[0]
        self.assertIn("include_trips=true", req.full_url)
        self.assertIn("origin_airport=SFO", req.full_url)
        self.assertEqual(req.get_header("Partner-authorization"), "SECRET")
        # Cloudflare-bypass headers must be present (error 1010 otherwise)
        self.assertEqual(req.get_header("User-agent"), seats_aero.USER_AGENT)
        self.assertEqual(req.get_header("Accept"), "application/json")

    def test_401_raises_auth_error(self):
        err = urllib.error.HTTPError("u", 401, "Unauthorized", None, None)
        with self.assertRaises(seats_aero.AuthError):
            seats_aero.search("https://x/partnerapi", "BAD", "SFO", "NRT",
                              "business", "2026-11-01", "2026-11-30",
                              rate_limit_sleep=0, _urlopen=urlopen_raising(err))

    def test_429_retries_then_search_error(self):
        err = urllib.error.HTTPError("u", 429, "Too Many Requests", None, None)
        with self.assertRaises(seats_aero.SearchError):
            seats_aero.search("https://x/partnerapi", "K", "SFO", "NRT",
                              "business", "2026-11-01", "2026-11-30",
                              rate_limit_sleep=0, max_retries=1,
                              _urlopen=urlopen_raising(err))

    def test_malformed_json_raises_search_error(self):
        with self.assertRaises(seats_aero.SearchError):
            seats_aero.search("https://x/partnerapi", "K", "SFO", "NRT",
                              "business", "2026-11-01", "2026-11-30",
                              rate_limit_sleep=0, _urlopen=urlopen_returning("not json{"))


if __name__ == "__main__":
    unittest.main()
