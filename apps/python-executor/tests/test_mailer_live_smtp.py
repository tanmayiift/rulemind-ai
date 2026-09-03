"""Report email against a REAL SMTP server (#127).

`_default_smtp` (mailer.py:18-29) is marked `# pragma: no cover - real network` and has never
executed: every existing test substitutes `_SMTP_FACTORY`. So the actual connect, the
smtplib handshake, MIME serialization over the wire and the CSV attachment round trip are all
unverified -- and the CSV is the part a customer opens.

aiosmtpd is a REAL SMTP server, not a mock. It speaks the protocol, so leaving `_SMTP_FACTORY`
at None means `_default_smtp` genuinely runs. That distinction is the whole point: a fake
transport asserts the shape the test author already assumed, and "the attachment survives
serialization" is exactly the assumption worth doubting.

Skipped when aiosmtpd is absent so a stale venv skips rather than errors.
"""
from __future__ import annotations

import email
import importlib.util
import os
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

HAS_AIOSMTPD = importlib.util.find_spec("aiosmtpd") is not None

from app import mailer  # noqa: E402

if HAS_AIOSMTPD:
    from aiosmtpd.controller import Controller


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Collector:
    """Minimal aiosmtpd handler that keeps every message it receives."""

    def __init__(self):
        self.messages = []

    async def handle_DATA(self, server, session, envelope):
        self.messages.append({
            "mail_from": envelope.mail_from,
            "rcpt_tos": list(envelope.rcpt_tos),
            "content": envelope.content,
        })
        return "250 Message accepted for delivery"


@unittest.skipUnless(HAS_AIOSMTPD, "pip install aiosmtpd to run the live SMTP suite")
class LiveSmtpTest(unittest.TestCase):
    def setUp(self):
        self.collector = _Collector()
        # An explicitly-chosen free port, not port=0: aiosmtpd's Controller.start() does a
        # self-check connection, and with port=0 it dials port 0 and fails to bind.
        self.controller = Controller(self.collector, hostname="127.0.0.1", port=_free_port())
        self.controller.start()
        self.addCleanup(self.controller.stop)
        self.config = {
            "host": self.controller.hostname,
            "port": self.controller.port,
            "from_addr": "reports@rulemind.local",
            # aiosmtpd here speaks plaintext; STARTTLS would fail against it. The TLS
            # branch of _default_smtp stays uncovered, and saying so is better than
            # faking a certificate and pretending otherwise.
            "use_tls": False,
            "use_ssl": False,
        }
        # Deliberately NOT set: this is what makes _default_smtp run for real.
        self.assertIsNone(mailer._SMTP_FACTORY,
                          "a substituted factory would defeat the point of this suite")

    def _sent(self):
        self.assertEqual(len(self.collector.messages), 1, "expected exactly one message")
        raw = self.collector.messages[0]
        return raw, email.message_from_bytes(raw["content"])

    def test_a_report_is_delivered_over_real_smtp(self):
        result = mailer.send_report_email(
            self.config, ["ops@example.com"], "Daily decisions", "Attached.",
            "id,outcome\nd1,approve\n", "decisions.csv")
        self.assertTrue(result["delivered"], result)
        raw, msg = self._sent()
        self.assertEqual(raw["mail_from"], "reports@rulemind.local")
        self.assertEqual(raw["rcpt_tos"], ["ops@example.com"])
        self.assertEqual(msg["Subject"], "Daily decisions")

    def test_the_csv_attachment_round_trips_byte_identically(self):
        """The assertion worth the most. A customer opens this file; if MIME encoding
        mangles a newline or the charset, the report is wrong in a way no unit test on
        build_message would show."""
        csv = "id,outcome,score\nd1,approve,742\nd2,reject,310\n"
        mailer.send_report_email(self.config, ["ops@example.com"], "R", "Body", csv, "report.csv")
        _, msg = self._sent()

        attachments = [p for p in msg.walk() if p.get_filename()]
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_filename(), "report.csv")
        self.assertEqual(attachments[0].get_content_type(), "text/csv")
        self.assertEqual(attachments[0].get_payload(decode=True).decode("utf-8"), csv)

    def test_multiple_recipients_all_receive_it(self):
        mailer.send_report_email(self.config, ["a@example.com", "b@example.com"],
                                 "R", "Body", "x\n", "r.csv")
        raw, _ = self._sent()
        self.assertEqual(sorted(raw["rcpt_tos"]), ["a@example.com", "b@example.com"])

    def test_an_unreachable_server_reports_failure_rather_than_raising(self):
        """The durability contract: send_report_email makes ONE attempt and returns a record,
        because the caller persists a failure to the outbox and a retry job drains it. If it
        raised instead, the scheduled-report job would die and the outbox would never see it."""
        config = dict(self.config, port=1)  # nothing listens on port 1
        result = mailer.send_report_email(config, ["ops@example.com"], "R", "B", "x\n", "r.csv")
        self.assertFalse(result["delivered"])
        self.assertIn("error", result)
        self.assertEqual(self.collector.messages, [])

    def test_no_recipients_is_refused_before_connecting(self):
        result = mailer.send_report_email(self.config, [], "R", "B", "x\n", "r.csv")
        self.assertFalse(result["delivered"])
        self.assertEqual(result["transport"], "none")
        self.assertEqual(self.collector.messages, [])


if __name__ == "__main__":
    unittest.main()
