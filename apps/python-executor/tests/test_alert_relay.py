"""The alert relay's rendering — the part that decides whether a 3am page is actionable.

Alertmanager's webhook body is a JSON envelope. Pointed straight at a push service it renders as
a wall of JSON on a phone, which is a notification that tells you nothing in the ten seconds
before you decide whether to get up. These tests pin the mapping onto the fields a notification
actually has: title, priority, tags, and a tappable runbook.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

RELAY = pathlib.Path(__file__).resolve().parents[3] / "observability" / "alert-relay" / "relay.py"
_spec = importlib.util.spec_from_file_location("alert_relay", RELAY)
relay = importlib.util.module_from_spec(_spec)
sys.modules["alert_relay"] = relay
_spec.loader.exec_module(relay)


def envelope(status="firing", **labels):
    ann = {k: labels.pop(k) for k in ("summary", "description", "runbook") if k in labels}
    return {"status": status, "alerts": [{"labels": labels, "annotations": ann}]}


class RenderTest(unittest.TestCase):
    def test_a_critical_alert_pages_urgently(self):
        note = relay.render(envelope(alertname="RuleMindApiDown", severity="critical",
                                     summary="The API is not being scraped"))[0]
        self.assertEqual(note["title"], "[FIRING] RuleMindApiDown")
        self.assertEqual(note["priority"], 5,
                         "a critical must cut through a silenced phone, or paging is decoration")
        self.assertIn("rotating_light", note["tags"])

    def test_the_heartbeat_never_buzzes(self):
        """severity: none is the Watchdog. A heartbeat that wakes someone every interval to say
        nothing is wrong is how heartbeats get muted and stop being heartbeats."""
        note = relay.render(envelope(alertname="Watchdog", severity="none",
                                     summary="Alerting pipeline heartbeat"))[0]
        self.assertEqual(note["priority"], 1)

    def test_resolved_is_visibly_different_and_never_urgent(self):
        note = relay.render(envelope(status="resolved", alertname="RuleMindApiDown",
                                     severity="critical", summary="back up"))[0]
        self.assertTrue(note["title"].startswith("[RESOLVED]"))
        self.assertEqual(note["priority"], 3,
                         "an all-clear must not page at the same urgency as the outage")
        self.assertIn("white_check_mark", note["tags"])

    def test_the_runbook_is_one_tap_away(self):
        note = relay.render(envelope(alertname="X", severity="critical", summary="s",
                                     runbook="https://example.com/runbook#api-down"))[0]
        self.assertEqual(note["click"], "https://example.com/runbook#api-down")
        self.assertIn("runbook:", note["message"])

    def test_a_relative_runbook_is_shown_but_not_linked(self):
        """The repo's own annotations are repo-relative paths. Those belong in the text; making
        them a `click` would hand the phone a URL it cannot open."""
        note = relay.render(envelope(alertname="X", severity="warning",
                                     summary="s", runbook="docs/runbooks/production.md"))[0]
        self.assertNotIn("click", note)
        self.assertIn("docs/runbooks/production.md", note["message"])

    def test_every_alert_in_a_group_gets_its_own_notification(self):
        """Alertmanager groups to reduce noise, which is right for delivery and wrong for
        display: one notification for three alerts names only the first, and the rest are
        invisible."""
        payload = {"status": "firing", "alerts": [
            {"labels": {"alertname": "A", "severity": "critical"}, "annotations": {"summary": "a"}},
            {"labels": {"alertname": "B", "severity": "warning"}, "annotations": {"summary": "b"}},
            {"labels": {"alertname": "C", "severity": "info"}, "annotations": {"summary": "c"}},
        ]}
        notes = relay.render(payload)
        self.assertEqual([n["title"] for n in notes],
                         ["[FIRING] A", "[FIRING] B", "[FIRING] C"])
        self.assertEqual([n["priority"] for n in notes], [5, 4, 3])

    def test_summary_and_description_both_survive(self):
        note = relay.render(envelope(alertname="X", severity="critical",
                                     summary="short", description="the long form"))[0]
        self.assertIn("short", note["message"])
        self.assertIn("the long form", note["message"])

    def test_an_alert_with_no_annotations_still_renders(self):
        """A rule authored without a summary must still page. Dropping it because the template
        had nothing to fill in is the worst possible failure for an alert."""
        note = relay.render(envelope(alertname="Bare", severity="critical"))[0]
        self.assertEqual(note["title"], "[FIRING] Bare")
        self.assertTrue(note["message"])

    def test_an_unknown_severity_still_pages(self):
        note = relay.render(envelope(alertname="X", severity="chartreuse", summary="s"))[0]
        self.assertEqual(note["priority"], 3)

    def test_priority_is_an_integer_because_ntfys_json_api_rejects_the_words(self):
        """ntfy's HTTP-HEADER api accepts "urgent"/"high"; its JSON api answers 400 Bad Request
        and does not say which field is wrong. Sending the words made every page fail upstream
        while the relay, the network and ntfy itself were all healthy."""
        for sev in ("critical", "warning", "info", "none", "unknown-severity"):
            note = relay.render(envelope(alertname="X", severity=sev, summary="s"))[0]
            self.assertIsInstance(note["priority"], int, f"{sev} rendered a non-integer priority")
            self.assertIn(note["priority"], range(1, 6))

    def test_the_instance_label_reaches_the_page(self):
        """Which host is down is the first question after what is down."""
        note = relay.render(envelope(alertname="X", severity="critical", summary="s",
                                     instance="api-7f9c"))[0]
        self.assertIn("api-7f9c", note["message"])


if __name__ == "__main__":
    unittest.main()
