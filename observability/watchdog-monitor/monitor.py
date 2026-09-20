"""Watch for the heartbeat's SILENCE, and page when it stops.

WHAT THIS IS, AND WHAT IT HONESTLY IS NOT.

The `Watchdog` alert fires unconditionally and Alertmanager repeats it every 5 minutes. Its value
is entirely in its absence: if the pipe dies you stop being woken up, which is indistinguishable
from everything being fine. Something has to notice the silence.

The textbook answer is an external service, because a monitor on the same host shares fate with
what it watches. That is true and it is also incomplete, and the incompleteness is the reason
this file exists. Ranked by how often they actually happen, the ways alerting dies are:

    the relay's token expires or its upstream changes        caught here
    Alertmanager's config breaks on reload                   caught here
    a receiver is misrouted, or points at a dead URL         caught here
    Prometheus stops scraping, or the rule stops evaluating  caught here
    a container dies and does not come back                  caught here
    the whole machine dies                                   NOT caught here

Only the last one needs a different failure domain, and it is the rarest. A local monitor closes
five of six for free and with no account; `observability/secrets/watchdog_url` closes the sixth
and needs one paste from a human. Shipping the five is better than shipping none while waiting
for the sixth, PROVIDED nobody is told they have a complete dead man's switch -- which is why
that limit is stated here, in the README, and in the page this thing sends.

HOW IT WORKS. The heartbeat is already being delivered to an ntfy topic. This polls that topic
for recent messages; if none has arrived within the grace period, it pages the ON-CALL topic at
urgent priority. It re-arms after a recovery, so a flapping pipe pages once per outage rather
than once per poll.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

NTFY = os.environ.get("RULEMIND_NTFY_BASE", "https://ntfy.sh").rstrip("/")
HEARTBEAT_TOPIC_FILE = pathlib.Path(os.environ.get(
    "RULEMIND_HEARTBEAT_TOPIC_FILE", "/etc/watchdog-monitor/heartbeat_topic"))
PAGING_TOPIC_FILE = pathlib.Path(os.environ.get(
    "RULEMIND_TOPIC_FILE", "/etc/watchdog-monitor/topic"))
# The EXTERNAL dead man's switch, optional. This monitor pings it only while it can SEE the
# heartbeat arriving -- which makes the external service the backstop for the one case this
# monitor structurally cannot cover: itself, or the whole host, going away. Ping it
# unconditionally and it would report healthy while the pipeline it is supposed to vouch for was
# dead; ping it only on a verified heartbeat and its silence means "the local chain stopped
# proving itself", which is exactly the thing worth escalating.
EXTERNAL_URL_FILE = pathlib.Path(os.environ.get(
    "RULEMIND_WATCHDOG_URL_FILE", "/etc/watchdog-monitor/watchdog_url"))

# Alertmanager repeats Watchdog every 5m. 16m is three missed beats plus slack, so one slow
# delivery does not page anyone -- the same reasoning the external-monitor interval uses.
GRACE_S = int(os.environ.get("RULEMIND_WATCHDOG_GRACE_S", "960"))
POLL_S = int(os.environ.get("RULEMIND_WATCHDOG_POLL_S", "60"))
# Do not page during the first window after start: there is legitimately no heartbeat yet, and a
# monitor whose first act on every restart is a false page is a monitor people mute.
WARMUP_S = int(os.environ.get("RULEMIND_WATCHDOG_WARMUP_S", "360"))


def read(path: pathlib.Path) -> str:
    return path.read_text().strip() if path.exists() else ""


def last_heartbeat_age(topic: str) -> float | None:
    """Seconds since the most recent heartbeat, or None if the topic could not be read.

    None is deliberately NOT treated as silence. Failing to reach ntfy means this monitor is
    blind, not that the pipeline is down, and paging on a blind spot trains people to ignore it.
    """
    since = GRACE_S + 600
    url = f"{NTFY}/{urllib.parse.quote(topic)}/json?poll=1&since={since}s"
    try:
        with urllib.request.urlopen(url, timeout=20) as res:
            newest = None
            for line in res.read().decode("utf-8", "replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if m.get("event") != "message":
                    continue
                ts = m.get("time")
                if isinstance(ts, (int, float)) and (newest is None or ts > newest):
                    newest = ts
            return None if newest is None else max(0.0, time.time() - newest)
    except (urllib.error.URLError, OSError) as exc:
        print(f"could not poll the heartbeat topic: {exc}", file=sys.stderr, flush=True)
        return None


def ping_external() -> None:
    """Forward proof-of-life to the external monitor. Best-effort and never fatal: this monitor's
    own job is unaffected by whether the escalation path is reachable."""
    url = read(EXTERNAL_URL_FILE)
    if not url:
        return
    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=15) as res:
            res.read()
    except (urllib.error.URLError, OSError) as exc:
        print(f"external heartbeat ping failed: {exc}", file=sys.stderr, flush=True)


def page(topic: str, title: str, message: str, priority: int) -> None:
    body = json.dumps({
        "topic": topic, "title": title, "message": message,
        "priority": priority, "tags": ["skull" if priority >= 5 else "white_check_mark"],
    }).encode()
    req = urllib.request.Request(NTFY + "/", data=body,
                                 headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as res:
        res.read()


# Alertmanager's repeat_interval for the Watchdog route. The grace MUST exceed it, with room for
# a slow delivery -- otherwise the monitor pages during entirely normal operation, every cycle,
# and gets muted. Found the honest way: testing with a 90s grace against a 5m repeat produced a
# page immediately after an Alertmanager restart, because the first post-restart heartbeat had
# not been sent yet. That is a misconfiguration the monitor should refuse to run in, not a
# discovery each operator makes for themselves at 3am.
EXPECTED_REPEAT_S = int(os.environ.get("RULEMIND_WATCHDOG_REPEAT_S", "300"))


def main() -> int:
    started = time.time()
    firing = False

    if GRACE_S <= EXPECTED_REPEAT_S:
        print(f"REFUSING TO START: grace={GRACE_S}s is not greater than Alertmanager's "
              f"repeat_interval={EXPECTED_REPEAT_S}s. A grace shorter than the heartbeat's own "
              f"period pages during normal operation -- every cycle -- and a monitor that cries "
              f"wolf is a monitor nobody looks at. Use at least 3x the repeat interval.",
              file=sys.stderr, flush=True)
        return 2
    if GRACE_S < EXPECTED_REPEAT_S * 2:
        print(f"WARNING: grace={GRACE_S}s is under 2x the repeat interval "
              f"({EXPECTED_REPEAT_S}s). One slow delivery will page you.",
              file=sys.stderr, flush=True)

    print(f"watchdog-monitor: grace={GRACE_S}s poll={POLL_S}s warmup={WARMUP_S}s "
          f"repeat={EXPECTED_REPEAT_S}s -> {NTFY}", flush=True)

    while True:
        hb_topic, page_topic = read(HEARTBEAT_TOPIC_FILE), read(PAGING_TOPIC_FILE)
        if not hb_topic or not page_topic:
            print("no heartbeat/paging topic configured; idling", file=sys.stderr, flush=True)
            time.sleep(POLL_S)
            continue

        age = last_heartbeat_age(hb_topic)
        warming = (time.time() - started) < WARMUP_S

        if age is None:
            pass  # blind, not silent — see last_heartbeat_age
        elif age > GRACE_S and not warming:
            if not firing:
                firing = True
                mins = int(age // 60)
                page(page_topic, "[ALERTING IS DOWN] no heartbeat",
                     f"No Watchdog heartbeat for {mins} minutes.\n\n"
                     f"Alerts are NOT reaching you. Check, in order: the alert-relay container, "
                     f"Alertmanager's config and logs, and whether Prometheus is still "
                     f"evaluating rules.\n\n"
                     f"NOTE: this monitor runs on the same host as the pipeline it watches, so "
                     f"it cannot tell you the host itself is gone. That is what "
                     f"observability/secrets/watchdog_url is for.", 5)
                print(f"PAGED: heartbeat silent for {mins}m", flush=True)
        else:
            # Verified heartbeat -> vouch for the chain upstream.
            ping_external()
            if firing:
                firing = False
                page(page_topic, "[RECOVERED] heartbeat is back",
                     f"Watchdog heartbeats are arriving again "
                     f"(last one {int(age)}s ago).", 3)
                print("recovered", flush=True)

        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
