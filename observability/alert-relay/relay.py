"""Turn an Alertmanager webhook into a notification a human can act on at 3am.

WHY THIS EXISTS. Alertmanager's webhook body is a JSON envelope: a `status`, a `receiver`, and
an `alerts` array of label/annotation maps. Point it straight at a push service and the page you
get is that envelope, rendered as a wall of JSON on a phone screen. It is technically a
notification and practically useless -- you cannot tell from it what broke, how bad it is, or
where the runbook is, which is everything you need in the ten seconds before you decide whether
to get out of bed.

So this maps the envelope onto the fields a notification actually has:

    title      [FIRING] RuleMindApiDown        what broke, and whether it is still broken
    priority   urgent | default | min          from `severity`, so a critical buzzes through
                                               a silenced phone and a `severity: none`
                                               heartbeat never does
    tags       an emoji the eye resolves before the text does
    click      the runbook, one tap away

DESTINATION comes from a file, never a flag or an environment variable, for the same reason the
rest of this directory does: a webhook URL IS the credential, and `url_file` semantics mean a
missing destination fails LOUDLY at notify time rather than silently at load time.

SELF-HOSTING. The default destination is ntfy.sh, which needs no account -- the topic name is
the secret. That is the right trade for getting paging working today and the wrong one for a
production decisioning engine, because alert summaries leave your perimeter. ntfy is open
source and the server is one container; point RULEMIND_NTFY_BASE at your own and nothing else
here changes.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

NTFY_BASE = os.environ.get("RULEMIND_NTFY_BASE", "https://ntfy.sh").rstrip("/")
TOPIC_FILE = pathlib.Path(os.environ.get("RULEMIND_NTFY_TOPIC_FILE", "/etc/alert-relay/topic"))
PORT = int(os.environ.get("PORT", "9097"))

# severity -> (ntfy priority, tag). `none` is the Watchdog's severity: a heartbeat must never
# buzz a phone, which is how heartbeats get muted and stop being heartbeats.
#
# PRIORITY IS AN INTEGER, 1..5. ntfy's HTTP-HEADER api accepts the words ("urgent", "high"), its
# JSON api does not -- it answers 400 Bad Request, with no indication of which field is wrong.
# Sending the words cost a delivery here: the relay was healthy, the network was fine, ntfy was
# reachable, and every page was silently rejected upstream. Caught because the relay returns 502
# when a delivery fails rather than 200, so Alertmanager counted it and the failure was visible.
DEFAULT_PRIORITY = 3
SEVERITY = {
    "critical": (5, "rotating_light"),
    "warning": (4, "warning"),
    "info": (3, "information_source"),
    "none": (1, "heartbeat"),
}


def topic() -> str:
    """Read at NOTIFY time, not at start. A destination added after the container came up must
    work without a restart, and one removed must fail visibly rather than keep using a cached
    value."""
    return TOPIC_FILE.read_text().strip() if TOPIC_FILE.exists() else ""


def render(payload: dict) -> list[dict]:
    """One notification per alert, not one per group.

    Alertmanager groups alerts to reduce noise, and that is correct for delivery. It is wrong
    for display: a group of three becomes one notification whose title names only the first,
    and the other two are invisible unless you expand the JSON.
    """
    out = []
    status = (payload.get("status") or "firing").lower()
    for alert in payload.get("alerts") or []:
        labels = alert.get("labels") or {}
        ann = alert.get("annotations") or {}
        severity = (labels.get("severity") or "info").lower()
        priority, tag = SEVERITY.get(severity, (DEFAULT_PRIORITY, "bell"))
        if status == "resolved":
            priority, tag = DEFAULT_PRIORITY, "white_check_mark"
        name = labels.get("alertname") or "alert"

        body = ann.get("summary") or ann.get("description") or "(no summary)"
        if ann.get("description") and ann.get("summary"):
            body = f"{ann['summary']}\n\n{ann['description']}"
        instance = labels.get("instance") or labels.get("source")
        if instance:
            body += f"\n\ninstance: {instance}"

        note = {
            "topic": topic(),
            "title": f"[{status.upper()}] {name}",
            "message": body,
            "priority": priority,
            "tags": [tag],
        }
        runbook = ann.get("runbook")
        if runbook:
            note["message"] += f"\n\nrunbook: {runbook}"
            if runbook.startswith("http"):
                note["click"] = runbook
        out.append(note)
    return out


def deliver(note: dict) -> None:
    req = urllib.request.Request(
        NTFY_BASE + "/",
        data=json.dumps(note).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as res:
        res.read()


class Relay(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            self.send_response(400); self.end_headers(); self.wfile.write(b"bad json"); return

        if not topic():
            # 500, deliberately. Alertmanager retries a 5xx and counts it in
            # alertmanager_notifications_failed_total, which RuleMindPagingIsFailing alerts on.
            # A 200 here would mean "delivered" for an alert that reached nobody.
            self.send_response(500); self.end_headers()
            self.wfile.write(b"no topic configured"); return

        failed = 0
        for note in render(payload):
            try:
                deliver(note)
            except (urllib.error.URLError, OSError) as exc:
                failed += 1
                print(f"deliver failed: {exc}", file=sys.stderr, flush=True)
        if failed:
            self.send_response(502); self.end_headers(); self.wfile.write(b"upstream failed")
        else:
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

    def do_GET(self):
        # Liveness for compose's health gate. Says whether a destination is configured, which is
        # the one thing worth knowing about this service from the outside.
        ok = bool(topic())
        self.send_response(200 if ok else 503)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok" if ok else "no topic", "ntfy": NTFY_BASE}).encode())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"alert-relay listening on :{PORT} -> {NTFY_BASE}", flush=True)
    HTTPServer(("0.0.0.0", PORT), Relay).serve_forever()
