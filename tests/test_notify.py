import os
import smtplib
import unittest

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


if __name__ == "__main__":
    unittest.main()
