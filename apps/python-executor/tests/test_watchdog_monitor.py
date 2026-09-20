"""The watchdog monitor's decision logic — when it pages, and when it deliberately does not.

The value of a dead man's switch is entirely in its absence-detection, so the two failure modes
that matter are opposite: paging when nothing is wrong (which gets it muted) and staying quiet
when the pipeline is dead (which is the thing it exists to prevent).
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest
from unittest import mock

MON = (pathlib.Path(__file__).resolve().parents[3]
       / "observability" / "watchdog-monitor" / "monitor.py")
_spec = importlib.util.spec_from_file_location("watchdog_monitor", MON)
monitor = importlib.util.module_from_spec(_spec)
sys.modules["watchdog_monitor"] = monitor
_spec.loader.exec_module(monitor)


class GraceGuardTest(unittest.TestCase):
    """A grace shorter than the heartbeat's own period pages during normal operation."""

    def _main_with(self, grace, repeat):
        with mock.patch.object(monitor, "GRACE_S", grace), \
             mock.patch.object(monitor, "EXPECTED_REPEAT_S", repeat), \
             mock.patch.object(monitor, "read", return_value="t"), \
             mock.patch.object(monitor, "time") as t:
            t.time.return_value = 0
            t.sleep.side_effect = KeyboardInterrupt  # stop the loop if it is ever reached
            try:
                return monitor.main()
            except KeyboardInterrupt:
                return "entered-loop"

    def test_a_grace_shorter_than_the_repeat_interval_refuses_to_start(self):
        """Found by testing: a 90s grace against a 5m repeat paged immediately after an
        Alertmanager restart, because the first post-restart heartbeat had not been sent yet.
        That is a misconfiguration to refuse, not one each operator rediscovers at 3am."""
        self.assertEqual(self._main_with(90, 300), 2)

    def test_a_grace_equal_to_the_repeat_interval_also_refuses(self):
        self.assertEqual(self._main_with(300, 300), 2)

    def test_a_sane_grace_starts(self):
        self.assertEqual(self._main_with(960, 300), "entered-loop")


class SilenceDetectionTest(unittest.TestCase):
    def test_an_unreachable_topic_is_blind_not_silent(self):
        """Failing to reach ntfy means this monitor cannot see, not that the pipeline is down.
        Paging on a blind spot trains people to ignore it."""
        with mock.patch.object(monitor.urllib.request, "urlopen", side_effect=OSError("no net")):
            self.assertIsNone(monitor.last_heartbeat_age("topic"))

    def test_an_empty_topic_reads_as_no_heartbeat(self):
        class Empty:
            def read(self): return b""
            def __enter__(self): return self
            def __exit__(self, *a): return False
        with mock.patch.object(monitor.urllib.request, "urlopen", return_value=Empty()):
            self.assertIsNone(monitor.last_heartbeat_age("topic"))

    def test_the_newest_message_wins(self):
        """ntfy returns the window's messages in no guaranteed order; taking the last line rather
        than the newest timestamp would report a stale age and page during healthy operation."""
        import json as _json
        lines = b"\n".join(_json.dumps(m).encode() for m in [
            {"event": "message", "time": 1000},
            {"event": "message", "time": 5000},   # newest, not last
            {"event": "message", "time": 3000},
        ])

        class Body:
            def read(self): return lines
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with mock.patch.object(monitor.urllib.request, "urlopen", return_value=Body()), \
             mock.patch.object(monitor.time, "time", return_value=5030):
            self.assertAlmostEqual(monitor.last_heartbeat_age("t"), 30, delta=1)

    def test_non_message_events_are_ignored(self):
        """ntfy emits keepalive/open events on the same stream. Counting one as a heartbeat would
        make the monitor permanently happy while the pipeline was dead — the exact failure it
        exists to catch."""
        import json as _json
        lines = b"\n".join(_json.dumps(m).encode() for m in [
            {"event": "open", "time": 9999},
            {"event": "keepalive", "time": 9999},
        ])

        class Body:
            def read(self): return lines
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with mock.patch.object(monitor.urllib.request, "urlopen", return_value=Body()):
            self.assertIsNone(monitor.last_heartbeat_age("t"))


class ExternalPingTest(unittest.TestCase):
    def test_no_external_url_configured_is_a_no_op(self):
        with mock.patch.object(monitor, "read", return_value=""), \
             mock.patch.object(monitor.urllib.request, "urlopen") as up:
            monitor.ping_external()
            up.assert_not_called()

    def test_a_failing_external_ping_never_raises(self):
        """The escalation path being unreachable must not stop this monitor doing its own job."""
        with mock.patch.object(monitor, "read", return_value="https://example.invalid/hb"), \
             mock.patch.object(monitor.urllib.request, "urlopen", side_effect=OSError("down")):
            monitor.ping_external()  # must not raise


if __name__ == "__main__":
    unittest.main()
