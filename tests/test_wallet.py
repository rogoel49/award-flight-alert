import unittest

import check
import wallet

CFG = {"seats_aero_url_template": ""}
PARAMS = {"cabin": "business", "max_miles": 90000, "min_seats": 1, "only_direct": False}


def row(source, miles=70000):
    return {"JAvailable": True, "JMileageCost": miles, "JRemainingSeats": 2, "JAirlines": "NH",
            "Source": source, "Date": "2026-12-13",
            "Route": {"OriginAirport": "SFO", "DestinationAirport": "HND"}}


class TestFunding(unittest.TestCase):
    def test_no_wallet_means_no_filtering(self):
        self.assertIsNone(wallet.funding({}))
        self.assertIsNone(wallet.funding({"wallet": {"programs": [], "currencies": []}}))
        self.assertEqual(wallet.resolve_programs({}, {}), (None, {}))

    def test_currencies_expand_to_transfer_partners(self):
        fund = wallet.funding({"wallet": {"currencies": ["chase", "bilt"], "programs": ["delta"]}})
        self.assertEqual(fund["united"], ["Chase UR", "Bilt"])
        self.assertEqual(fund["alaska"], ["Bilt"])          # bilt-only partner
        self.assertEqual(fund["delta"], ["miles you hold"])
        self.assertNotIn("american", fund)

    def test_every_partner_is_a_known_source(self):
        for key, (_label, progs) in wallet.CURRENCIES.items():
            for p in progs:
                self.assertIn(p, wallet.SOURCES, f"{key} -> {p}")

    def test_transfer_table_can_be_overridden(self):
        fund = wallet.funding({"wallet": {"currencies": ["chase"],
                                          "transfer_partners": {"chase": ["united"]}}})
        self.assertEqual(fund, {"united": ["Chase UR"]})

    def test_unknown_currency_or_program_is_an_error(self):
        with self.assertRaises(wallet.WalletError):
            wallet.funding({"wallet": {"currencies": ["monopoly"]}})
        with self.assertRaises(wallet.WalletError):
            wallet.funding({"wallet": {"programs": ["untied"]}})

    def test_alert_programs_override_wallet_but_keep_funding_labels(self):
        cfg = {"wallet": {"currencies": ["chase"]}}
        allowed, fund = wallet.resolve_programs({"programs": ["alaska", "united"]}, cfg)
        self.assertEqual(allowed, {"alaska", "united"})
        self.assertEqual(fund["united"], ["Chase UR"])


class TestProgramFilter(unittest.TestCase):
    def test_unbookable_programs_are_dropped_and_funding_attached(self):
        allowed, fund = wallet.resolve_programs({}, {"wallet": {"currencies": ["chase"]}})
        p = dict(PARAMS, programs=allowed, funding=fund)
        hits = check.filter_rows([row("aeroplan"), row("alaska"), row("american")],
                                 p, set(), CFG, "SFO")
        self.assertEqual([h["program"] for h in hits], ["aeroplan"])
        self.assertEqual(hits[0]["funding"], ["Chase UR"])
        self.assertEqual(check.fmt_program(hits[0]), "aeroplan ← Chase UR")

    def test_no_filter_keeps_everything(self):
        hits = check.filter_rows([row("aeroplan"), row("alaska")], PARAMS, set(), CFG, "SFO")
        self.assertEqual(len(hits), 2)
        self.assertEqual(check.fmt_program(hits[1]), "alaska")

    def test_programs_join_the_alert_id_only_when_set(self):
        base = {"origins": ["SFO"], "destinations": ["HND"], "cabin": "business"}
        self.assertEqual(check.content_id(base), check.content_id(dict(base, programs=None)))
        self.assertNotEqual(check.content_id(base),
                            check.content_id(dict(base, programs=["united"])))


if __name__ == "__main__":
    unittest.main()
