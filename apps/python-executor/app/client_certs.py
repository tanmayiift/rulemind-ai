"""Outbound client certificates from a PKCS#12 bundle (parity P02).

WHAT EXISTED BEFORE. `app/mtls.py` is INBOUND mutual TLS: it evaluates a certificate a caller
presents TO this server (`evaluate_client_cert`, fingerprint pinning, expiry). This is the
opposite direction -- presenting OUR certificate to a partner that requires one -- and nothing
did it. There was no `cert=` anywhere in the codebase, so a datasource behind mutual TLS simply
could not be connected to, and the failure surfaced as an opaque handshake error.

Authoring format is PKCS#12 (.p12 / .pfx) rather than a PEM pair, because that is what a
partner's onboarding pack actually contains: one password-protected file holding the key, the
leaf and usually the chain. Asking an operator to split it into PEMs with `openssl` before they
can use it is asking them to handle a private key by hand.

WHY A TEMPORARY FILE, WHICH LOOKS WRONG AND IS THE LEAST-WORST OPTION. Python's `ssl` module can
only build a client identity through `SSLContext.load_cert_chain`, which reads from PATHS -- it
has no in-memory equivalent (a long-standing CPython gap; `cryptography` can parse the bundle but
cannot hand the result to `ssl`). So the decrypted PEM has to touch a file descriptor somewhere.
What this module controls is how narrow that window is:

  * the file is created with mode 0600 by `mkstemp`, so it is never world- or group-readable,
  * it is unlinked IMMEDIATELY after `load_cert_chain` returns -- the key lives on afterwards
    only inside the SSLContext, in process memory,
  * it is written under the system temp dir, never inside the repo or a mounted volume,
  * and it is unlinked in a `finally`, so a failure to load still removes it.

The alternative -- keeping decrypted PEMs on disk for the life of the service -- is what this
exists to avoid.

CONTEXTS ARE CACHED PER (tenant, connector, bundle fingerprint). Building one costs a PBKDF2
unwrap of the PKCS#12, which is deliberately slow, and a decision path cannot pay that per
request. Keying on the bundle's fingerprint rather than just the connector id is what makes a
rotation take effect: change the bundle, the fingerprint changes, the old context is not reused.
"""
from __future__ import annotations

import hashlib
import logging
import os
import ssl
import tempfile
import threading
from typing import Any, Dict, Optional, Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12

logger = logging.getLogger("rulemind.client_certs")


class ClientCertificateInvalid(ValueError):
    """The bundle cannot be turned into a client identity. Raised at authoring time where
    possible, so a connector is not saved in a state that can only fail at decision time."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def configured(connector: Any) -> bool:
    config = connector.get("config") if isinstance(connector, dict) else None
    return bool(isinstance(config, dict) and config.get("client_cert_p12"))


def _fingerprint(p12_base64: str, passphrase: str) -> str:
    # The passphrase is part of the key: the same bundle with a corrected passphrase must build a
    # NEW context rather than reuse the one that failed.
    return hashlib.sha256(
        "{0}\x00{1}".format(p12_base64, passphrase).encode("utf-8")).hexdigest()


def load_identity(p12_base64: str, passphrase: str = "") -> Tuple[bytes, Optional[str]]:
    """Unwrap a PKCS#12 bundle into a single PEM blob (key + leaf + chain).

    Returns (pem_bytes, subject_common_name). Raises ClientCertificateInvalid with a code an
    operator can act on -- "wrong passphrase" and "not a p12 at all" are different problems and
    `cryptography` reports both as the same ValueError.
    """
    import base64
    import binascii

    try:
        raw = base64.b64decode(p12_base64, validate=True)
    except (binascii.Error, ValueError) as bad:
        raise ClientCertificateInvalid(
            "not_base64", "client_cert_p12 must be the base64 of a .p12/.pfx file.") from bad
    if not raw:
        raise ClientCertificateInvalid("empty", "client_cert_p12 is empty.")

    try:
        key, cert, chain = pkcs12.load_key_and_certificates(
            raw, passphrase.encode("utf-8") if passphrase else None)
    except Exception as bad:  # noqa: BLE001 - cryptography raises several unrelated types here
        # Deliberately not echoing the underlying message: it can contain fragments of the
        # bundle, and "mac verify failure" tells an operator nothing they can act on.
        raise ClientCertificateInvalid(
            "unreadable",
            "Could not read the PKCS#12 bundle. The passphrase may be wrong, or the file may "
            "not be a .p12/.pfx.") from bad

    if key is None or cert is None:
        raise ClientCertificateInvalid(
            "incomplete",
            "The bundle has no private key and certificate pair, so it cannot identify a client.")

    parts = [
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            # No passphrase on the PEM: it is written to a 0600 file that is unlinked within
            # microseconds, and load_cert_chain would need the passphrase in memory anyway.
            encryption_algorithm=serialization.NoEncryption()),
        cert.public_bytes(serialization.Encoding.PEM),
    ]
    # The intermediates matter: without them a partner that does not have the chain cached
    # rejects the handshake, and the error looks like a bad certificate rather than a missing one.
    for extra in (chain or []):
        parts.append(extra.public_bytes(serialization.Encoding.PEM))

    common_name = None
    try:
        from cryptography.x509.oid import NameOID

        attributes = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if attributes:
            common_name = str(attributes[0].value)
    except Exception:  # pragma: no cover - a missing CN is not an error
        pass
    return b"".join(parts), common_name


def _build_context(p12_base64: str, passphrase: str) -> ssl.SSLContext:
    pem, _ = load_identity(p12_base64, passphrase)
    context = ssl.create_default_context()
    handle, path = tempfile.mkstemp(prefix="rulemind-clientcert-", suffix=".pem")
    try:
        # 0600 from mkstemp, and written through the fd we already own so there is no window in
        # which the path exists with different permissions.
        with os.fdopen(handle, "wb") as fh:
            fh.write(pem)
        context.load_cert_chain(certfile=path)
    except ssl.SSLError as bad:
        raise ClientCertificateInvalid(
            "rejected_by_ssl",
            "The key and certificate in the bundle do not match, or the key is unsupported.") from bad
    finally:
        # In a `finally`: a load failure must not leave a decrypted private key on disk.
        try:
            os.unlink(path)
        except OSError:  # pragma: no cover
            pass
    return context


_CACHE: Dict[str, ssl.SSLContext] = {}
_CACHE_LOCK = threading.Lock()


def context_for(connector: Dict[str, Any], tenant_id: str = "") -> Optional[ssl.SSLContext]:
    """The SSLContext this connector should present, or None if it has no client certificate.

    Cached per (tenant, connector, bundle fingerprint). The fingerprint is in the key so that
    rotating the bundle takes effect immediately rather than after a restart -- a cache keyed on
    the connector id alone would keep presenting the revoked certificate.
    """
    config = connector.get("config") if isinstance(connector, dict) else None
    if not isinstance(config, dict):
        return None
    bundle = config.get("client_cert_p12")
    if not bundle:
        return None
    passphrase = str(config.get("client_cert_passphrase") or "")
    key = "{0}\x00{1}\x00{2}".format(
        tenant_id, connector.get("id") or "", _fingerprint(str(bundle), passphrase))
    with _CACHE_LOCK:
        found = _CACHE.get(key)
        if found is not None:
            return found
    # Built OUTSIDE the lock: the PBKDF2 unwrap is deliberately slow, and holding a global lock
    # across it would serialise every tenant's decisions behind one connector's first request.
    context = _build_context(str(bundle), passphrase)
    with _CACHE_LOCK:
        return _CACHE.setdefault(key, context)


def reset_cache() -> None:
    """Test hook, and the operator escape hatch if a context ever needs discarding."""
    with _CACHE_LOCK:
        _CACHE.clear()
