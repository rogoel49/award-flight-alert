import json
import os
import shutil
import smtplib
import tempfile
import unittest

import check
import notify


class FakeSMTP:
    def __init__(self, record):
        self.record = record

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        self.record["login"] = (user, password)

    def send_message(self, msg):
        self.record["msg"] = msg


CFG = {"smtp_user": "u@example.com", "email_to": "t@example.com",
       "smtp_password_env": "AWARD_TEST_PW_XYZ"}


class TestSend(unittest.TestCase):
    def test_send_logs_in_and_sends(self):
        rec = {}
        notify.send("Subj", "<b>hi</b>", CFG, "PW", smtp_factory=lambda: FakeSMTP(rec))
        self.assertEqual(rec["login"], ("u@example.com", "PW"))
        msg = rec["msg"]
        self.assertEqual(msg["Subject"], "Subj")
        self.assertEqual(msg["To"], "t@example.com")
        self.assertEqual(msg["From"], "u@example.com")
        # HTML alternative is present
        self.assertTrue(any(p.get_content_type() == "text/html"
                            for p in msg.walk()))


class TestNotify(unittest.TestCase):
    def setUp(self):
        os.environ.pop("AWARD_TEST_PW_XYZ", None)

    def tearDown(self):
        os.environ.pop("AWARD_TEST_PW_XYZ", None)

    def test_empty_password_is_noop(self):
        sent = notify.notify([{"miles": 1, "dest": "X", "date": "d", "airlines": "ZZ"}], CFG)
        self.assertFalse(sent)  # no password -> skipped, no send attempted

    def test_send_failure_is_caught(self):
        os.environ["AWARD_TEST_PW_XYZ"] = "pw"
        orig = notify.send
        notify.send = lambda *a, **k: (_ for _ in ()).throw(smtplib.SMTPException("nope"))
        try:
            sent = notify.notify(
                [{"miles": 1, "dest": "X", "date": "d", "airlines": "ZZ"}], CFG)
            self.assertFalse(sent)  # exception swallowed, run continues
        finally:
            notify.send = orig

    def test_no_email_to_is_noop(self):
        self.assertFalse(notify.notify([{"miles": 1}], {"email_to": ""}))


HIT = {"alert_name": "SFO-Tokyo", "origin": "SFO", "dest": "NRT", "date": "2026-11-20",
       "cabin": "business", "airlines": "NH", "program": "aeroplan", "miles": 82000,
       "seats": 2, "direct": True, "duration_min": 675}


class TestMacos(unittest.TestCase):
    def fake_run(self, calls, fail=False):
        def run(cmd, **kw):
            calls.append(cmd)
            if fail:
                raise OSError("no osascript")
        return run

    def test_banner_text(self):
        title, subtitle, message = notify.macos_banner(HIT)
        self.assertIn("SFO→NRT", title)
        self.assertIn("82,000 mi business", title)
        self.assertEqual(subtitle, "2026-11-20 · NH via aeroplan")
        self.assertEqual(message, "2 seats · Nonstop · 11h15m")

    def test_values_go_through_argv_not_the_script(self):
        calls = []
        evil = dict(HIT, airlines='" & (do shell script "rm -rf ~") & "')
        self.assertTrue(notify.notify_macos([evil], {}, _run=self.fake_run(calls)))
        cmd = calls[0]
        self.assertEqual(cmd[:2], ["osascript", "-e"])
        self.assertNotIn("do shell script", cmd[2])      # the script itself is constant
        self.assertTrue(any("do shell script" in a for a in cmd[3:]))  # data rides in argv

    def test_caps_banners_and_adds_summary(self):
        calls = []
        notify.notify_macos([HIT] * 8, {"macos_max_banners": 3}, _run=self.fake_run(calls))
        self.assertEqual(len(calls), 4)                  # 3 hits + 1 summary
        self.assertIn("+5 more new seat(s)", calls[-1])

    def test_failure_is_caught(self):
        calls = []
        self.assertFalse(notify.notify_macos([HIT], {}, _run=self.fake_run(calls, fail=True)))


class TestRoundRobin(unittest.TestCase):
    def test_every_alert_gets_a_slot_before_any_gets_two(self):
        hits = [dict(HIT, alert_id="mex", miles=m) for m in (1, 2, 3)] + \
               [dict(HIT, alert_id="hnd", miles=9)]
        self.assertEqual([(h["alert_id"], h["miles"]) for h in notify.round_robin(hits)],
                         [("mex", 1), ("hnd", 9), ("mex", 2), ("mex", 3)])


class TestOutboxDelivery(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._saved = (check.PENDING_PATH, check.CONFIG_PATH, notify.notify)
        check.PENDING_PATH = os.path.join(self.dir, "pending_hits.json")
        check.CONFIG_PATH = os.path.join(self.dir, "config.json")
        check.queue_pending([dict(HIT, alert_id="a1")], "2026-07-18")

    def tearDown(self):
        check.PENDING_PATH, check.CONFIG_PATH, notify.notify = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def _main(self, channels, delivered):
        with open(check.CONFIG_PATH, "w") as f:
            json.dump({"notify": {"channels": channels}}, f)
        notify.notify = lambda hits, cfg, test=False: delivered
        notify.main([])

    def test_failed_delivery_keeps_hits_for_retry(self):
        self._main(["macos"], delivered=False)
        self.assertEqual(len(check.load_pending()), 1)

    def test_successful_delivery_acks(self):
        self._main(["macos"], delivered=True)
        self.assertEqual(check.load_pending(), [])

    def test_explicitly_no_channels_drains_the_outbox(self):
        self._main([], delivered=False)  # agent-owned notification: don't grow forever
        self.assertEqual(check.load_pending(), [])


class TestChannels(unittest.TestCase):
    def with_channels(self, record):
        orig = dict(notify.CHANNELS)
        notify.CHANNELS.update(
            email=lambda hits, cfg, test=False: record.append("email") or True,
            macos=lambda hits, cfg, test=False: record.append("macos") or True)
        self.addCleanup(lambda: notify.CHANNELS.update(orig))

    def test_default_channel_is_email(self):
        rec = []
        self.with_channels(rec)
        self.assertTrue(notify.notify([HIT], {}))
        self.assertEqual(rec, ["email"])

    def test_macos_only_skips_email(self):
        rec = []
        self.with_channels(rec)
        notify.notify([HIT], {"channels": ["macos"]})
        self.assertEqual(rec, ["macos"])

    def test_unknown_channel_is_skipped_not_fatal(self):
        rec = []
        self.with_channels(rec)
        self.assertTrue(notify.notify([HIT], {"channels": ["pager", "macos"]}))
        self.assertEqual(rec, ["macos"])


class TestBuildHtml(unittest.TestCase):
    def test_renders_link_note_and_miles(self):
        html = notify.build_html([{
            "alert_name": "SFO-Tokyo", "origin": "SFO", "dest": "NRT", "date": "2026-11-20",
            "airlines": "NH", "program": "aeroplan", "miles": 82000, "seats": 2,
            "direct": False, "connections": ["MNL"], "link": "https://seats.aero/x"}])
        self.assertIn("https://seats.aero/x", html)
        self.assertIn("via MNL", html)
        self.assertIn("82,000", html)
        self.assertIn("SFO-Tokyo", html)


class TestHtmlEscaping(unittest.TestCase):
    def test_malicious_name_airline_and_link_are_neutralized(self):
        h = {"alert_name": "<img src=x onerror=alert(1)>", "origin": "SFO", "dest": "NRT",
             "date": "2026-11-20", "airlines": "<b>NH</b>", "program": "aeroplan",
             "miles": 82000, "seats": 2, "direct": True, "link": "javascript:alert(1)"}
        out = notify.build_html([h])
        self.assertNotIn("<img src=x", out)        # free-form name escaped
        self.assertIn("&lt;img src=x", out)
        self.assertNotIn("<b>NH</b>", out)          # API airline escaped
        self.assertNotIn('href="javascript:', out)  # non-https link is not linkified

    def test_https_link_is_linkified(self):
        h = {"origin": "SFO", "dest": "NRT", "date": "d", "airlines": "NH",
             "program": "x", "miles": 1, "seats": 1, "link": "https://seats.aero/ok"}
        self.assertIn('href="https://seats.aero/ok"', notify.build_html([h]))


if __name__ == "__main__":
    unittest.main()
