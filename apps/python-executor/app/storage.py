from __future__ import annotations

import base64
import copy
import logging
import hashlib
import hmac
import json
import time as _time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy import case, delete, desc, func, inspect, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

from .archiver import normalize_retention_months, retention_window_days
from .audit_chain import GENESIS_HASH, compute_hash as compute_audit_hash, verify as verify_audit_chain_rows
from .idempotency import is_expired as is_idempotency_expired, should_remember as should_remember_idempotent
from .quota import METRIC_DECISIONS
from sqlalchemy.orm import Session

LOGGER = logging.getLogger(__name__)

from .auth import (
    api_key_kid,
    bcrypt_hash,
    bcrypt_verify,
    generate_api_key,
    generate_otp_code,
    generate_session_token,
    key_lookup_hash,
    mask_api_key,
    otp_code_hash,
    session_token_hash,
)
from . import env
from .context import get_current_tenant_id
from .db import engine_for, session_factory
from .logic import generate_rule_expression_definition, json_dumps, now_iso
from . import ai as ai_module
from .models import (
    ModelEvaluation,
    ActionLog,
    ApiKey,
    AuditEvent,
    Base,
    Bundle,
    Connector,
    CronSchedule,
    Decision,
    DecisionTable,
    EmailOutbox,
    EntityHistory,
    ErrorEvent,
    Experiment,
    HostedModel,
    MemberOtp,
    MemberSession,
    PlatformAdminUser,
    Policy,
    Promotion,
    ReportDefinition,
    ReviewTask,
    Rule,
    LoginAttempt,
    SamlAssertionSeen,
    SchedulerLease,
    Scorecard,
    SdkEvent,
    Setting,
    IdempotencyRecord,
    Tenant,
    UsageRecord,
    Variable,
    ModelSet,
    Journey,
    Predictor,
    PredictorValue,
    Webhook,
    WebhookSubscription,
    WorkflowExecution,
    WorkspaceMember,
    uuid4_str,
)
from .seed_data import CONNECTORS, DEFAULT_SETTINGS, POLICIES, RULES, SCORECARDS, VARIABLES


SECRET_FIELD_MARKERS = (
    "token", "secret", "password", "api_key", "apikey", "client_secret", "private_key",
    # Added with outbound client certificates (P02). "password" does NOT match
    # "passphrase", and nothing here matched "client_cert_p12" -- so a PKCS#12 bundle and
    # its passphrase were stored in the clear and echoed back unredacted, which is a
    # private key in a connector row. This list is a substring test, so a new secret-ish
    # config key is only protected if a marker actually appears in it; naming alone is not
    # enough.
    "passphrase", "client_cert_p12",
)

# Verified API keys are cached per-Storage-instance (bcrypt is deliberately
# ~200ms; running it per request caps the whole API at a few req/s). TTL below.
#
# env.env_float, not float(os.getenv(...)): this runs at MODULE IMPORT, so a set-but-blank
# API_KEY_CACHE_TTL made `float("")` raise ValueError before a single request handler existed —
# the process died at import with a traceback that named neither the variable nor the fix.
_API_KEY_CACHE_TTL = env.env_float("API_KEY_CACHE_TTL", 300.0)


def _parse_client_datetime(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp sent by an SDK (e.g. an on-device decision time),
    returning a naive UTC datetime, or None if absent/unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is not None:
        # Normalize to naive UTC (not naive *local*): stored created_at is naive UTC and
        # serialize_datetime treats naive values as UTC, so the round-trip must too.
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def serialize_datetime(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(tz=None).replace(tzinfo=None)
    return value.replace(microsecond=0).isoformat() + "Z"


# ── config-at-rest encryption ────────────────────────────────────────────────
#
# Everything below shares one master secret, RULEMIND_CONFIG_KEY. Two things were wrong with
# how it was used, and both fail in the same direction — quietly.
#
# 1. THE KEY WAS UNROTATABLE. `fernet_key()` derived exactly one key and every decrypt used it
#    alone, so changing RULEMIND_CONFIG_KEY orphaned every encrypted column at once. There was
#    no second chance: no MultiFernet, no old-key list, nothing anywhere in the repo.
#
# 2. EVERY DECRYPT RETURNED SOMETHING PLAUSIBLE ON THE WRONG KEY. `except: return value`
#    handed the CIPHERTEXT back as if it were the plaintext. So a rotation did not fail — it
#    "succeeded" and then sent the base64 of a connector token as a live Authorization header,
#    fed ciphertext bytes to `pickle.loads`, and emptied every connector config to `{}`. The
#    operator's signal that the key was wrong was a slow, distributed outage.
#
# The chain below fixes both: MultiFernet tries the primary key then every configured old key,
# and a token that decrypts under NONE of them raises DecryptionError instead of degrading.
# The one legacy path that IS deliberate — a row written before encryption existed — is now
# gated on the Fernet version marker rather than on "an exception happened".

# A Fernet token is urlsafe-base64 whose first byte is the version 0x80, which always encodes
# as "gAAAAA". That marker is what separates "this column holds ciphertext" (a decrypt failure
# here is a real error) from "this column holds a pre-encryption plaintext" (returning it is
# correct). Before, the two were indistinguishable because both arrived as `except Exception`.
_FERNET_PREFIX = "gAAAAA"
_FERNET_PREFIX_BYTES = b"gAAAAA"

CONFIG_KEY_VAR = "RULEMIND_CONFIG_KEY"
CONFIG_KEY_OLD_VAR = "RULEMIND_CONFIG_KEY_OLD"
_CONFIG_KEY_DEFAULT = "rulemind-dev-master-key"

# A fixed application salt. The master secret lives only in the environment and there is
# nowhere to persist a random per-deployment salt that every replica would agree on, so the
# salt is a constant and the work factor carries the weight. This is still a large improvement
# on the bare `sha256(raw)` it replaces: a single SHA-256 over an operator-chosen passphrase is
# brute-forceable at billions of guesses/second on commodity GPUs, which matters because the
# thing being guessed is the key to every stored secret and every decision payload.
_KDF_SALT = b"rulemind.config-key.pbkdf2.v1"
_KDF_ITERATIONS = 200_000

# Derivation is ~100ms, and the old code derived a key on EVERY encrypt and decrypt call.
# Memoised per distinct secret; the process already holds these values in os.environ.
_DERIVED_KEY_CACHE: Dict[str, bytes] = {}
_KEY_CHAIN_CACHE: Dict[Tuple[str, Tuple[str, ...]], MultiFernet] = {}


class DecryptionError(RuntimeError):
    """Stored ciphertext could not be decrypted with any configured key.

    Deliberately loud. The alternative this replaces — returning the ciphertext, or `{}` —
    turned a wrong RULEMIND_CONFIG_KEY into corrupt-but-plausible data flowing outward.
    """


def _looks_encrypted(value: str) -> bool:
    return value.startswith(_FERNET_PREFIX)


def _derive_key(secret: str) -> bytes:
    """PBKDF2-HMAC-SHA256 -> a urlsafe-base64 Fernet key."""
    cached = _DERIVED_KEY_CACHE.get(secret)
    if cached is not None:
        return cached
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=_KDF_SALT, iterations=_KDF_ITERATIONS
    )
    derived = base64.urlsafe_b64encode(kdf.derive(secret.encode("utf-8")))
    _DERIVED_KEY_CACHE[secret] = derived
    return derived


def _legacy_sha256_key(secret: str) -> bytes:
    """The pre-KDF derivation: one unsalted SHA-256 of the raw secret.

    Kept ONLY as a decryption key. Switching to PBKDF2 changes the derived key, so every row
    already in the database was encrypted under this one; dropping it would render every
    connector config, provider key, SMTP password, SSO secret, webhook secret, model blob and
    decision payload permanently unreadable, with no error at write time to warn anyone. It
    stays in the chain so old rows keep decrypting, and `scripts/rotate_config_key.py`
    re-encrypts them onto the KDF key at the operator's convenience.
    """
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())


def _config_key_secrets() -> Tuple[str, Tuple[str, ...]]:
    """(primary, previous...) — env.env_str so a set-but-BLANK RULEMIND_CONFIG_KEY falls back to
    the dev default rather than deriving the key from "", which is publicly computable and would
    silently encrypt every tenant's secrets under a key printed in this comment."""
    primary = env.env_str(CONFIG_KEY_VAR, _CONFIG_KEY_DEFAULT)
    return primary, tuple(env.env_list(CONFIG_KEY_OLD_VAR))


def crypto() -> MultiFernet:
    """The key chain: encrypt() uses the FIRST key, decrypt() tries them in order.

    Order is primary-KDF, primary-legacy-sha256, then each RULEMIND_CONFIG_KEY_OLD entry in
    both derivations. That means: new writes use the primary KDF key; reads still succeed for
    data written before the KDF change and for data written under a previous master secret.
    """
    primary, olds = _config_key_secrets()
    cache_key = (primary, olds)
    cached = _KEY_CHAIN_CACHE.get(cache_key)
    if cached is not None:
        return cached
    raw_keys = [_derive_key(primary), _legacy_sha256_key(primary)]
    for old in olds:
        raw_keys.append(_derive_key(old))
        raw_keys.append(_legacy_sha256_key(old))
    chain = MultiFernet([Fernet(key) for key in raw_keys])
    _KEY_CHAIN_CACHE[cache_key] = chain
    return chain


def fernet_key() -> bytes:
    """The PRIMARY key — what new writes are encrypted under. Reads must go through `crypto()`
    so previous keys are tried too."""
    primary, _ = _config_key_secrets()
    return _derive_key(primary)


def _decryption_error(field: str) -> DecryptionError:
    return DecryptionError(
        "Could not decrypt {0}: the stored value is Fernet ciphertext but none of the "
        "configured keys open it. {1} has most likely been changed or rotated. Set the "
        "PREVIOUS key(s) in {2} (comma-separated) so reads keep working, then run "
        "scripts/rotate_config_key.py to re-encrypt onto the new key. Refusing to return the "
        "ciphertext as if it were the plaintext value.".format(
            field, CONFIG_KEY_VAR, CONFIG_KEY_OLD_VAR
        )
    )


def encrypt_config_payload(value: Dict[str, Any]) -> str:
    return crypto().encrypt(json.dumps(value).encode("utf-8")).decode("utf-8")


def decrypt_config_payload(value: str) -> Dict[str, Any]:
    """A connector's config (base URLs, auth headers, credentials).

    Used to return `{}` on any failure, which is the worst possible answer: the connector kept
    working, just with no auth and no configured endpoint, so the failure surfaced as confusing
    downstream 401s rather than as "the key is wrong".
    """
    if not value:
        return {}
    if not _looks_encrypted(value):
        # Deliberate legacy path: rows written before encrypted_config was encrypted held raw
        # JSON. Narrow — gated on the value not being ciphertext at all, not on "it threw".
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise DecryptionError(
                "Connector config is neither Fernet ciphertext nor legacy JSON; the column is "
                "corrupt and cannot be interpreted."
            ) from exc
        return parsed if isinstance(parsed, dict) else {}
    try:
        return json.loads(crypto().decrypt(value.encode("utf-8")).decode("utf-8"))
    except InvalidToken as exc:
        raise _decryption_error("connector config") from exc


def decision_encryption_enabled() -> bool:
    """Encrypt decision payloads at rest (default ON). Financial/KYC/health/any-PII inputs must
    not sit in the DB as plaintext. Disable only for a plaintext-required migration."""
    return env.env_bool("DECISION_ENCRYPT_AT_REST", True)


def encrypt_decision_field(value: Any) -> Any:
    """Encrypt a decision JSON blob (payload/variables/rule results/scorecard/trace) for storage.
    Returns a Fernet token string when encryption is enabled, else the value unchanged."""
    if value is None or not decision_encryption_enabled():
        return value
    return crypto().encrypt(json_dumps(value).encode("utf-8")).decode("utf-8")


def decrypt_decision_field(value: Any) -> Any:
    """Decrypt a stored decision blob. Handles both encrypted (Fernet token string) and legacy
    plaintext (a JSON object already), so old rows keep reading after encryption is turned on."""
    if not isinstance(value, str):
        return value  # legacy plaintext dict/list, or None
    if not _looks_encrypted(value):
        # Deliberate legacy path: a pre-encryption row, or a JSON column that round-tripped the
        # blob as a JSON *string*. Not a decrypt failure — there is no ciphertext here.
        try:
            return json.loads(value)
        except ValueError:
            return value
    try:
        return json.loads(crypto().decrypt(value.encode("utf-8")).decode("utf-8"))
    except InvalidToken as exc:
        raise _decryption_error("decision field") from exc


def audit_encryption_enabled() -> bool:
    """Encrypt audit metadata and source IPs at rest (default ON).

    The audit trail was the one PII store left in plaintext while the decisions beside it were
    encrypted. `metadata_json` carries the emails, usernames and source IPs of failed sign-ins,
    invitations and role changes; `ip_address` is a source IP by definition. Disable only for a
    plaintext-required migration, exactly as with DECISION_ENCRYPT_AT_REST.
    """
    return env.env_bool("AUDIT_ENCRYPT_AT_REST", True)


def encrypt_audit_field(value: Any) -> Any:
    """Encrypt an audit metadata blob for storage. No-op when disabled or empty."""
    if value is None or not audit_encryption_enabled():
        return value
    return crypto().encrypt(json_dumps(value).encode("utf-8")).decode("utf-8")


def encrypt_audit_text(value: Optional[str]) -> Optional[str]:
    """Encrypt a short audit string (the source IP). No-op when disabled or empty."""
    if not value or not audit_encryption_enabled():
        return value
    return crypto().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_audit_text(value: Optional[str]) -> Optional[str]:
    """Decrypt a stored audit string, tolerating pre-encryption plaintext rows.

    Raises DecryptionError only when the value IS ciphertext and no configured key opens it --
    the rotated-key case, which the caller reports rather than crashing on.
    """
    if not isinstance(value, str) or not _looks_encrypted(value):
        return value
    try:
        return crypto().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise _decryption_error("audit ip_address") from exc


def encrypt_secret_text(value: str) -> str:
    return crypto().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret_text(value: Optional[str]) -> Optional[str]:
    """Decrypt one stored secret (LLM provider key, SMTP password, SSO client secret, webhook
    signing secret).

    This one returned the CIPHERTEXT on failure, and every caller passes the result straight
    into an outbound request — so a wrong key meant a base64 blob was sent as a live
    Authorization header / SMTP password / OIDC client_secret to a third party, and the only
    symptom was an authentication failure at the far end.
    """
    if not value:
        return None
    if not _looks_encrypted(value):
        # Deliberate legacy path: a secret stored before encrypt_secret_text existed. Narrow —
        # only for values that carry no Fernet version marker.
        return value
    try:
        return crypto().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise _decryption_error("stored secret") from exc


def encrypt_model_blob(blob: bytes) -> bytes:
    """Encrypt a hosted model's raw bytes at rest. The DB (and any dump / backup /
    replica) only ever holds ciphertext — the model's weights are unreadable without
    RULEMIND_CONFIG_KEY. Protects proprietary / black-box model IP."""
    return crypto().encrypt(blob)


def decrypt_model_blob(blob: bytes) -> bytes:
    """Decrypt a hosted model's pickled bytes.

    Returning the raw ciphertext on failure fed it to `pickle.loads`, which is both guaranteed
    to fail confusingly and a deserialisation path that should never be handed attacker- or
    corruption-shaped bytes.
    """
    if not blob:
        return blob
    if not blob.startswith(_FERNET_PREFIX_BYTES):
        # Deliberate legacy path: a model uploaded before blobs were encrypted.
        return blob
    try:
        return crypto().decrypt(blob)
    except InvalidToken as exc:
        raise _decryption_error("hosted model blob") from exc


def decision_payload_hash(payload: Any) -> str:
    """A KEYED fingerprint of a decision payload.

    Was a bare `sha256(canonical_json)`. Decision payloads are overwhelmingly low-entropy PII —
    a PAN, a phone number, a date of birth, a pincode, a loan amount — so an unsalted, unkeyed
    digest is reversible by enumeration: anyone with read access to the `decisions` table (a
    replica, a backup, a support export) can recover the exact input by hashing candidates.
    That defeats the point of encrypting payload_preview in the same row.

    HMAC under a key derived from RULEMIND_CONFIG_KEY makes enumeration require the key, which
    is exactly the property the encrypted columns already depend on. The subkey is separated by
    label so this never reuses the Fernet encryption key material directly.

    Note for operators: rotating RULEMIND_CONFIG_KEY changes the hash produced for NEW rows.
    Nothing in the codebase looks a decision up by payload_hash (verified), so this is a
    fingerprint for correlation only, not an index.
    """
    primary, _ = _config_key_secrets()
    subkey = hmac.new(b"rulemind.payload-hash.v1", primary.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(subkey, json_dumps(payload).encode("utf-8"), hashlib.sha256).hexdigest()


def mask_secret_values(value: Any) -> Any:
    if isinstance(value, dict):
        masked: Dict[str, Any] = {}
        for key, item in value.items():
            if any(marker in key.lower() for marker in SECRET_FIELD_MARKERS):
                masked[key] = "••••••••"
            else:
                masked[key] = mask_secret_values(item)
        return masked
    if isinstance(value, list):
        return [mask_secret_values(item) for item in value]
    return value


_AUDIT_LOG = logging.getLogger("rulemind.storage.audit_listeners")

# ── audit listeners ────────────────────────────────────────────────────────
# Storage does not import webhook_dispatch, and must not: that would make the persistence layer
# depend on outbound HTTP, and this file is already the largest in the backend. The dependency is
# inverted instead -- interested modules register here at startup, and storage only knows it holds
# a list of callables.
#
# Every listener is best effort and isolated. A written audit event is a fact; a listener that
# raises must not turn it into an exception for the caller who was merely recording a promotion.
_AUDIT_LISTENERS: List[Any] = []


def register_audit_listener(listener: Any) -> None:
    """Call `listener(storage, event_dict)` after every audit event is durably written."""
    if listener not in _AUDIT_LISTENERS:
        _AUDIT_LISTENERS.append(listener)


def clear_audit_listeners() -> None:
    """Test hook. Listeners are process-global, so a suite that registers one must be able to
    take it back off rather than leaking it into every later test in the same process."""
    _AUDIT_LISTENERS.clear()


_DECISION_LISTENERS: List[Any] = []


def register_decision_listener(listener: Any) -> None:
    """Call `listener(storage, decision_dict)` after every Decision row is written."""
    if listener not in _DECISION_LISTENERS:
        _DECISION_LISTENERS.append(listener)


def clear_decision_listeners() -> None:
    """Test hook, for the same reason as clear_audit_listeners."""
    _DECISION_LISTENERS.clear()


def _notify_decision_listeners(storage: Any, decision: Dict[str, Any]) -> None:
    for listener in list(_DECISION_LISTENERS):
        try:
            listener(storage, decision)
        except Exception:  # pragma: no cover - a listener must never break the decision
            _AUDIT_LOG.warning("decision listener %r failed",
                               getattr(listener, "__name__", listener), exc_info=True)


def _notify_audit_listeners(storage: Any, event: Dict[str, Any]) -> None:
    for listener in list(_AUDIT_LISTENERS):
        try:
            listener(storage, event)
        except Exception:  # pragma: no cover - a listener must never break the write
            _AUDIT_LOG.warning("audit listener %r failed",
                               getattr(listener, "__name__", listener), exc_info=True)


class Storage:
    def __init__(self, path: Optional[str] = None, force_sqlite: bool = False):
        # force_sqlite: for the few tests that are ABOUT the SQLite backend (file backup and
        # restore, WAL mode, the rotation script run against a .db path) and build a
        # `sqlite:///...` URL by hand afterwards. See db.test_schema_for.
        self.path = path
        self.engine = engine_for(path, force_sqlite=force_sqlite)
        self.SessionLocal = session_factory(path, force_sqlite=force_sqlite)
        self._create_schema()
        self._reconcile_sqlite_columns()
        self.default_tenant_id: Optional[str] = None
        self.default_api_key: Optional[str] = None
        # Per-instance verified-API-key cache (see get_tenant_by_api_key). Keeping
        # it on the instance keeps it correct in prod (Storage is a singleton) and
        # isolated in tests (each Storage/DB gets its own cache).
        self._api_key_cache: Dict[str, Any] = {}
        # Resolved human-login sessions (token_hash -> (result, expiry)). Same
        # rationale as the API-key cache; role is read live so a role change /
        # deactivation takes effect within the TTL (and eagerly, see _invalidate_member).
        self._member_session_cache: Dict[str, Any] = {}
        # env.env_str throughout: a set-but-blank RULEMIND_ADMIN_PASSWORD would otherwise seed the
        # platform admin with an EMPTY password (os.getenv returns "" and the default never
        # applies) — and security_config's default-secret check passes, because "" is not the
        # built-in default it looks for. A blank RULEMIND_ADMIN_EMAIL is just as bad in a
        # different way: the seeded admin gets an unaddressable account nobody can log in as.
        self.default_admin_email = env.env_str("RULEMIND_ADMIN_EMAIL", "admin@rulemind.local")
        self.default_admin_password = env.env_str("RULEMIND_ADMIN_PASSWORD", "rulemind-admin")
        self.seed_if_empty()

    @contextmanager
    def connect(self) -> Iterator[Session]:
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _tenant_id(self, tenant_id: Optional[str] = None, payload_tenant: Optional[str] = None) -> str:
        """Resolve the tenant to operate on, in strict precedence order.

        `payload_tenant` comes LAST, after the authenticated context. Fifteen writers used to
        resolve `tenant_id or payload.get("tenant_id")`, which put a request-body field AHEAD of
        the auth context whenever the caller relied on the context (i.e. passed no tenant_id) --
        the precedence was backwards. Not exploitable today because no Pydantic model declares a
        tenant_id field, so the key never survives request parsing; but it is one `extra="allow"`
        or one new field away from a cross-tenant write primitive, and the ordering should not be
        the thing standing between those. Internal callers that carry the tenant inside the
        payload (batch import, WAL replay) still work -- they simply no longer outrank auth.
        """
        resolved = tenant_id or get_current_tenant_id() or payload_tenant or self.default_tenant_id
        if not resolved:
            raise ValueError("Tenant context is required.")
        return resolved

    def _seed_default_tenant(self, session: Session) -> Tenant:
        # env.env_str: a blank RULEMIND_DEV_API_KEY would seed a tenant whose API key is the
        # EMPTY STRING — and key_lookup_hash("") is a fixed, publicly computable digest.
        plaintext = env.env_str("RULEMIND_DEV_API_KEY", "rm_live_devlocaltenantkey000000000000")
        lookup_hash = key_lookup_hash(plaintext)
        tenant = session.scalar(select(Tenant).where(Tenant.api_key_hash == lookup_hash).limit(1))
        if tenant:
            return tenant
        tenant = session.scalar(select(Tenant).order_by(Tenant.created_at).limit(1))
        if tenant:
            return tenant
        tenant = Tenant(name="Default Tenant", plan="standard", config={"locale": "en-IN"}, is_active=True)
        session.add(tenant)
        session.flush()
        return tenant

    def _seed_default_api_key(self, session: Session, tenant: Tenant) -> None:
        # env.env_str: a blank RULEMIND_DEV_API_KEY would seed a tenant whose API key is the
        # EMPTY STRING — and key_lookup_hash("") is a fixed, publicly computable digest.
        plaintext = env.env_str("RULEMIND_DEV_API_KEY", "rm_live_devlocaltenantkey000000000000")
        lookup_hash = key_lookup_hash(plaintext)
        existing_for_lookup = session.scalar(select(ApiKey).where(ApiKey.lookup_hash == lookup_hash).limit(1))
        if existing_for_lookup:
            self.default_api_key = plaintext
            self.default_tenant_id = existing_for_lookup.tenant_id
            existing_tenant = session.get(Tenant, existing_for_lookup.tenant_id)
            if existing_tenant and existing_tenant.api_key_hash != lookup_hash:
                existing_tenant.api_key_hash = lookup_hash
            return
        api_key = session.scalar(select(ApiKey).where(ApiKey.tenant_id == tenant.id, ApiKey.is_active.is_(True)))
        if api_key:
            self.default_api_key = plaintext
            self.default_tenant_id = tenant.id
            return

        model = ApiKey(
            tenant_id=tenant.id,
            kid=api_key_kid(plaintext),
            masked_key=mask_api_key(plaintext),
            lookup_hash=lookup_hash,
            key_hash=bcrypt_hash(plaintext),
            is_active=True,
        )
        tenant.api_key_hash = lookup_hash
        session.add(model)
        self.default_api_key = plaintext
        self.default_tenant_id = tenant.id

    def _seed_default_admin(self, session: Session) -> None:
        existing = session.scalar(select(PlatformAdminUser).where(PlatformAdminUser.email == self.default_admin_email))
        if existing:
            return
        session.add(
            PlatformAdminUser(
                email=self.default_admin_email,
                name="RuleMind Admin",
                password_hash=bcrypt_hash(self.default_admin_password),
                is_active=True,
            )
        )

    def seed_if_empty(self) -> None:
        """Seed the default tenant, API key and admin — safely when several processes do it at once.

        EVERY REPLICA RUNS THIS AT BOOT, against one shared database. The body below is
        check-then-insert with nothing between the check and the insert, so two processes
        starting together both see an empty table and both insert. One wins; the other gets

            IntegrityError: duplicate key value violates unique constraint
            "ix_api_keys_lookup_hash"

        and, because this runs during startup, the process dies. Found on a local kind cluster
        where the api and worker pods boot simultaneously (the worker CrashLoopBackOff'd once);
        the chart's own production defaults are api=3 + worker=2, so a real install races five
        ways on first boot. SQLite hid it completely — one process, one file.

        The retry is the fix rather than a lock: losing the race means the winner has already
        written exactly the rows this process wanted, so re-reading gets the same result a
        successful seed would have. Retrying once is enough — the second pass takes the
        `if existing` branch, which is why this is not a retry loop.
        """
        try:
            self._seed_if_empty_once()
        except IntegrityError:
            # The winner committed between our SELECT and our INSERT. Its rows are the rows we
            # were about to write, so re-reading is the whole recovery.
            LOGGER.info("seed_if_empty lost a concurrent seed race; re-reading the winner's rows")
            self._seed_if_empty_once()

    def _seed_if_empty_once(self) -> None:
        with self.connect() as session:
            tenant = self._seed_default_tenant(session)
            self._seed_default_api_key(session, tenant)
            if self.default_tenant_id and self.default_tenant_id != tenant.id:
                tenant = session.get(Tenant, self.default_tenant_id) or tenant
            self._seed_default_admin(session)

            existing_variable = session.scalar(select(Variable).where(Variable.tenant_id == tenant.id).limit(1))
            if existing_variable:
                self._ensure_settings(session, tenant.id)
                self._ensure_seed_inventory(session, tenant.id)
                return

            # Demo inventory (sample connectors/variables/rules/scorecards/policies)
            # is OPT-IN: a fresh clone starts clean so a new customer builds their own
            # via guided onboarding. Set RULEMIND_SEED_DEMO=1 to load the samples (the
            # test suite sets it per-file). Workspace settings always exist regardless.
            if not env.env_bool("RULEMIND_SEED_DEMO"):
                self._ensure_settings(session, tenant.id)
                return

            now = datetime.utcnow()
            for connector in CONNECTORS:
                session.add(
                    Connector(
                        tenant_id=tenant.id,
                        public_id=connector["id"],
                        name=connector["name"],
                        icon=connector.get("icon"),
                        color=connector.get("color"),
                        description=connector.get("description"),
                        schema_fields=copy.deepcopy(connector.get("schema_paths", [])),
                        sample_payload=copy.deepcopy(connector.get("sample_payload", {})),
                        is_active=bool(connector.get("is_active", True)),
                        encrypted_config=encrypt_config_payload(copy.deepcopy(connector.get("config", {}))),
                        created_at=now,
                        updated_at=now,
                    )
                )

            for variable in VARIABLES:
                last_test_result = {
                    "value": variable["seed_value"],
                    "error": None,
                    "latency_ms": 1.0,
                    "tested_at": now_iso(),
                    "passed": True,
                }
                session.add(
                    Variable(
                        tenant_id=tenant.id,
                        public_id=variable["id"],
                        name=variable["name"],
                        category=variable["category"],
                        source_id=variable["source_id"],
                        code=variable["code"],
                        description=variable.get("description"),
                        status=variable["status"],
                        last_test_result=last_test_result,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.add(
                    EntityHistory(
                        tenant_id=tenant.id,
                        entity_type="variable",
                        entity_id=variable["id"],
                        version=1,
                        snapshot={**copy.deepcopy(variable), "last_test_result": last_test_result},
                        created_at=now,
                    )
                )

            variable_lookup = {item["id"]: item for item in VARIABLES}
            for rule in RULES:
                expression = generate_rule_expression_definition(rule, variable_lookup)
                session.add(
                    Rule(
                        tenant_id=tenant.id,
                        public_id=rule["id"],
                        name=rule["name"],
                        nodes=copy.deepcopy(rule.get("nodes")),
                        tree=copy.deepcopy(rule.get("tree")),
                        rule_format=rule.get("rule_format", "v1"),
                        expression=expression,
                        status=rule["status"],
                        last_test_result=None,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.add(
                    EntityHistory(
                        tenant_id=tenant.id,
                        entity_type="rule",
                        entity_id=rule["id"],
                        version=1,
                        snapshot={**copy.deepcopy(rule), "expression": expression},
                        created_at=now,
                    )
                )

            for scorecard in SCORECARDS:
                session.add(
                    Scorecard(
                        tenant_id=tenant.id,
                        public_id=scorecard["id"],
                        name=scorecard["name"],
                        base_score=scorecard["base_score"],
                        max_score=scorecard["max_score"],
                        bins=copy.deepcopy(scorecard["bins"]),
                        status=scorecard["status"],
                        last_test_result=None,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.add(
                    EntityHistory(
                        tenant_id=tenant.id,
                        entity_type="scorecard",
                        entity_id=scorecard["id"],
                        version=1,
                        snapshot=copy.deepcopy(scorecard),
                        created_at=now,
                    )
                )

            for policy in POLICIES:
                session.add(
                    Policy(
                        tenant_id=tenant.id,
                        public_id=policy["id"],
                        name=policy["name"],
                        trigger=copy.deepcopy(policy.get("trigger")),
                        steps=copy.deepcopy(policy["steps"]),
                        default_outcome=policy.get("defaultOutcome"),
                        status=policy["status"],
                        last_test_result=None,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.add(
                    EntityHistory(
                        tenant_id=tenant.id,
                        entity_type="policy",
                        entity_id=policy["id"],
                        version=1,
                        snapshot=copy.deepcopy(policy),
                        created_at=now,
                    )
                )

            self._ensure_settings(session, tenant.id)
            # The session disables autoflush, so persist the freshly inserted seed
            # entities before the inventory backfill checks query for existing rows.
            session.flush()
            self._ensure_seed_inventory(session, tenant.id)

    def _create_schema(self) -> None:
        """Create any missing tables, tolerating another process doing the same.

        `create_all(checkfirst=True)` inspects and then creates in two steps, so it
        is NOT atomic. Every process that opens a Storage runs it, and compose starts
        the API and the worker together — both saw an empty schema, both issued the
        DDL, and Postgres failed the loser with

            UniqueViolation: duplicate key value violates unique constraint
            "pg_type_typname_nsp_index"

        which killed the worker on startup. The same race hits any multi-replica
        rollout. Serialise with a Postgres advisory lock where one is available, and
        still swallow a lost race: by the time the error is raised the object exists,
        which is the outcome we wanted. Alembic in entrypoint.sh remains the real
        schema authority; this is the fresh-database convenience path.
        """
        is_postgres = self.engine.dialect.name == "postgresql"
        if not is_postgres:
            Base.metadata.create_all(self.engine)
            return

        # Arbitrary constant, shared by every process creating this schema.
        lock_id = 8_493_217_006
        try:
            with self.engine.begin() as conn:
                conn.exec_driver_sql("SELECT pg_advisory_xact_lock(%s)", (lock_id,))
                Base.metadata.create_all(conn)
            return
        except (IntegrityError, ProgrammingError, OperationalError) as error:
            # A concurrent creator won, or advisory locks are unavailable (pgbouncer
            # in transaction mode, for instance). Verify the schema really is there
            # rather than assuming.
            if not self._schema_present():
                raise
            LOGGER.warning("schema creation raced with another process; schema is present (%s)",
                           str(error).splitlines()[0][:160])

    def _schema_present(self) -> bool:
        try:
            return inspect(self.engine).has_table(Tenant.__tablename__)
        except Exception:
            return False

    def _reconcile_sqlite_columns(self) -> None:
        """Add any additive, nullable columns the ORM models declare but an existing SQLite
        table is missing. `create_all` only creates missing *tables*, never ALTERs existing
        ones, so a new nullable column would otherwise be invisible on a dev/test DB until it
        was deleted. SQLite only by design: Postgres/prod schema changes go through Alembic
        migrations (the authoritative record); this is a dev-ergonomics safety net, and it only
        ever adds nullable columns (no backfill, no data loss)."""
        if self.engine.dialect.name != "sqlite":
            return
        from sqlalchemy import inspect as sa_inspect, text

        inspector = sa_inspect(self.engine)
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                if not inspector.has_table(table.name):
                    continue
                existing = {col["name"] for col in inspector.get_columns(table.name)}
                for column in table.columns:
                    if column.name in existing or not column.nullable:
                        continue
                    col_type = column.type.compile(dialect=self.engine.dialect)
                    conn.execute(text('ALTER TABLE "{0}" ADD COLUMN "{1}" {2}'.format(table.name, column.name, col_type)))

    def _ensure_settings(self, session: Session, tenant_id: str) -> None:
        existing = session.scalar(select(Setting).where(Setting.tenant_id == tenant_id))
        if existing:
            return
        session.add(
            Setting(
                tenant_id=tenant_id,
                api_base_url=DEFAULT_SETTINGS["api_base_url"],
                auth_config=copy.deepcopy(DEFAULT_SETTINGS["auth_config"]),
                engine_config=copy.deepcopy(DEFAULT_SETTINGS["engine_config"]),
                source_defaults=copy.deepcopy(DEFAULT_SETTINGS["source_defaults"]),
                audit_retention_days=DEFAULT_SETTINGS["audit_retention_days"],
                theme_mode=DEFAULT_SETTINGS["theme_mode"],
                branding=copy.deepcopy(DEFAULT_SETTINGS["branding"]),
                ai_config=copy.deepcopy(DEFAULT_SETTINGS.get("ai_config", {})),
            )
        )

    def _ensure_seed_inventory(self, session: Session, tenant_id: str) -> None:
        now = datetime.utcnow()
        for connector in CONNECTORS:
            existing = session.scalar(select(Connector).where(Connector.tenant_id == tenant_id, Connector.public_id == connector["id"]))
            if existing:
                continue
            session.add(
                Connector(
                    tenant_id=tenant_id,
                    public_id=connector["id"],
                    name=connector["name"],
                    icon=connector.get("icon"),
                    color=connector.get("color"),
                    description=connector.get("description"),
                    schema_fields=copy.deepcopy(connector.get("schema_paths", [])),
                    sample_payload=copy.deepcopy(connector.get("sample_payload", {})),
                    is_active=bool(connector.get("is_active", True)),
                    encrypted_config=encrypt_config_payload(copy.deepcopy(connector.get("config", {}))),
                    created_at=now,
                    updated_at=now,
                )
            )

        for variable in VARIABLES:
            # A distinct name per model: reusing `existing` pins its inferred type to
            # whichever branch mypy sees first.
            existing_variable = session.scalar(select(Variable).where(Variable.tenant_id == tenant_id, Variable.public_id == variable["id"]))
            if existing_variable:
                continue
            last_test_result = {
                "value": variable["seed_value"],
                "error": None,
                "latency_ms": 1.0,
                "tested_at": now_iso(),
                "passed": True,
            }
            session.add(
                Variable(
                    tenant_id=tenant_id,
                    public_id=variable["id"],
                    name=variable["name"],
                    category=variable["category"],
                    source_id=variable["source_id"],
                    code=variable["code"],
                    description=variable.get("description"),
                    status=variable["status"],
                    last_test_result=last_test_result,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                EntityHistory(
                    tenant_id=tenant_id,
                    entity_type="variable",
                    entity_id=variable["id"],
                    version=1,
                    snapshot={**copy.deepcopy(variable), "last_test_result": last_test_result},
                    created_at=now,
                )
            )

        variable_lookup = {item["id"]: item for item in VARIABLES}
        for rule in RULES:
            # A distinct name per model: reusing `existing` pins its inferred type to
            # whichever branch mypy sees first.
            existing_rule = session.scalar(select(Rule).where(Rule.tenant_id == tenant_id, Rule.public_id == rule["id"]))
            if existing_rule:
                continue
            expression = generate_rule_expression_definition(rule, variable_lookup)
            session.add(
                Rule(
                    tenant_id=tenant_id,
                    public_id=rule["id"],
                    name=rule["name"],
                    nodes=copy.deepcopy(rule.get("nodes")),
                    tree=copy.deepcopy(rule.get("tree")),
                    rule_format=rule.get("rule_format", "v2" if rule.get("tree") else "v1"),
                    expression=expression,
                    status=rule["status"],
                    last_test_result=None,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                EntityHistory(
                    tenant_id=tenant_id,
                    entity_type="rule",
                    entity_id=rule["id"],
                    version=1,
                    snapshot={**copy.deepcopy(rule), "expression": expression},
                    created_at=now,
                )
            )

        for scorecard in SCORECARDS:
            # A distinct name per model: reusing `existing` pins its inferred type to
            # whichever branch mypy sees first.
            existing_scorecard = session.scalar(select(Scorecard).where(Scorecard.tenant_id == tenant_id, Scorecard.public_id == scorecard["id"]))
            if existing_scorecard:
                continue
            session.add(
                Scorecard(
                    tenant_id=tenant_id,
                    public_id=scorecard["id"],
                    name=scorecard["name"],
                    base_score=scorecard["base_score"],
                    max_score=scorecard["max_score"],
                    bins=copy.deepcopy(scorecard["bins"]),
                    status=scorecard["status"],
                    last_test_result=None,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                EntityHistory(
                    tenant_id=tenant_id,
                    entity_type="scorecard",
                    entity_id=scorecard["id"],
                    version=1,
                    snapshot=copy.deepcopy(scorecard),
                    created_at=now,
                )
            )

        for policy in POLICIES:
            # A distinct name per model: reusing `existing` pins its inferred type to
            # whichever branch mypy sees first.
            existing_policy = session.scalar(select(Policy).where(Policy.tenant_id == tenant_id, Policy.public_id == policy["id"]))
            if existing_policy:
                continue
            session.add(
                Policy(
                    tenant_id=tenant_id,
                    public_id=policy["id"],
                    name=policy["name"],
                    trigger=copy.deepcopy(policy.get("trigger")),
                    steps=copy.deepcopy(policy["steps"]),
                    default_outcome=policy.get("defaultOutcome"),
                    status=policy["status"],
                    last_test_result=None,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                EntityHistory(
                    tenant_id=tenant_id,
                    entity_type="policy",
                    entity_id=policy["id"],
                    version=1,
                    snapshot=copy.deepcopy(policy),
                    created_at=now,
                )
            )

    def _connector_to_dict(self, model: Connector, include_secrets: bool = False) -> Dict[str, Any]:
        config = decrypt_config_payload(model.encrypted_config)
        return {
            "id": model.public_id,
            "name": model.name,
            "icon": model.icon,
            "color": model.color,
            "description": model.description,
            "schema_paths": copy.deepcopy(model.schema_fields or []),
            "sample_payload": copy.deepcopy(model.sample_payload or {}),
            "is_active": bool(model.is_active),
            "config": copy.deepcopy(config) if include_secrets else mask_secret_values(config),
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    @staticmethod
    def _variable_to_dict(model: Variable) -> Dict[str, Any]:
        return {
            "id": model.public_id,
            "name": model.name,
            "category": model.category,
            "source_id": model.source_id,
            "code": model.code,
            "description": model.description,
            "status": model.status,
            "last_test_result": copy.deepcopy(model.last_test_result),
            "version": model.version,
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    @staticmethod
    def _rule_to_dict(model: Rule) -> Dict[str, Any]:
        return {
            "id": model.public_id,
            "name": model.name,
            "nodes": copy.deepcopy(model.nodes or []),
            "tree": copy.deepcopy(model.tree),
            "rule_format": model.rule_format if model.rule_format else ("v2" if model.tree else "v1"),
            "expression": model.expression,
            "status": model.status,
            "last_test_result": copy.deepcopy(model.last_test_result),
            "version": model.version,
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    @staticmethod
    def _scorecard_to_dict(model: Scorecard) -> Dict[str, Any]:
        return {
            "id": model.public_id,
            "name": model.name,
            "base_score": model.base_score,
            "max_score": model.max_score,
            "bins": copy.deepcopy(model.bins or []),
            "status": model.status,
            "last_test_result": copy.deepcopy(model.last_test_result),
            "version": model.version,
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    @staticmethod
    def _policy_to_dict(model: Policy) -> Dict[str, Any]:
        return {
            "id": model.public_id,
            "name": model.name,
            "trigger": copy.deepcopy(model.trigger),
            "steps": copy.deepcopy(model.steps or []),
            "defaultOutcome": model.default_outcome,
            "status": model.status,
            "lifecycle_status": getattr(model, "lifecycle_status", "draft") or "draft",
            "last_test_result": copy.deepcopy(model.last_test_result),
            "version": model.version,
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    @staticmethod
    def _decision_to_dict(model: Decision) -> Dict[str, Any]:
        return {
            "id": model.id,
            "client_id": model.client_id,
            "user_id": model.user_id,
            "policy_id": model.policy_id,
            "payload": copy.deepcopy(decrypt_decision_field(model.payload_preview) or {}),
            "payload_hash": model.payload_hash,
            "computed_variables": copy.deepcopy(decrypt_decision_field(model.computed_variables) or {}),
            # rule_results / scorecard_result / trace are decrypted here for the same reason the
            # two above are: they carry the same PII. The trace records the evaluated INPUT
            # values at every step, so leaving it in the clear (as it was) meant encrypting
            # payload_preview protected almost nothing — the plaintext sat one column over, and
            # redact_payload is applied only to the payload, never to the trace.
            "rule_results": copy.deepcopy(decrypt_decision_field(model.rule_results) or []),
            "scorecard_result": copy.deepcopy(decrypt_decision_field(model.scorecard_result)),
            "trace": copy.deepcopy(decrypt_decision_field(model.trace) or []),
            "outcome": model.outcome,
            "latency_ms": model.latency_ms,
            "source": model.source,
            "sdk_version": model.sdk_version,
            "experiment_id": model.experiment_id,
            "experiment_variant": model.experiment_variant,
            "device_id": model.device_id,
            "bundle_hash": model.bundle_hash,
            "created_at": serialize_datetime(model.created_at),
            "received_at": serialize_datetime(model.received_at),
        }

    @staticmethod
    def _error_event_to_dict(model: ErrorEvent) -> Dict[str, Any]:
        return {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "scope": model.scope,
            "entity_type": model.entity_type,
            "entity_id": model.entity_id,
            "stage": model.stage,
            "message": model.message,
            "details": copy.deepcopy(model.details_json or {}),
            "created_at": serialize_datetime(model.created_at),
        }

    @staticmethod
    def _promotion_to_dict(model: Promotion) -> Dict[str, Any]:
        return {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "entity_type": model.entity_type,
            "entity_id": model.entity_id,
            "from_status": model.from_status,
            "to_status": model.to_status,
            "promoted_by": model.promoted_by,
            "reason": model.reason,
            "created_at": serialize_datetime(model.created_at),
            # Lightweight flag only — the (potentially large) snapshot itself is fetched on demand
            # via get_promotion(id) so the ledger listing stays cheap.
            "has_snapshot": model.snapshot_json is not None,
        }

    @staticmethod
    def _audit_event_to_dict(model: AuditEvent) -> Dict[str, Any]:
        """Rebuild the application-level event, decrypting metadata and ip_address.

        A wrong or rotated RULEMIND_CONFIG_KEY must not raise out of here. This function is what
        `verify_audit_chain` feeds the verifier, so raising turned "one workspace's key was
        rotated without RULEMIND_CONFIG_KEY_OLD" into a 500 from the compliance endpoint --
        the audit chain reporting itself as broken software rather than as a chain it cannot
        currently read. The undecryptable rows are flagged instead, and audit_chain.verify
        reports them as `metadata_undecryptable` rather than comparing a hash it cannot
        reconstruct and calling the row tampered.
        """
        undecryptable = False
        try:
            metadata = decrypt_decision_field(model.metadata_json)
        except DecryptionError:
            metadata, undecryptable = {}, True
        # deepcopy unconditionally. decrypt_decision_field returns a legacy plaintext row's dict
        # BY REFERENCE (it short-circuits on non-str), so without this the caller would be handed
        # the live JSON column off the SQLAlchemy model -- and this dict is returned straight out
        # of list_audit_events and the audit export. The pre-encryption code copied here for
        # exactly that reason; it must not be lost to the decrypt path.
        metadata = copy.deepcopy(metadata) if isinstance(metadata, dict) else {}
        try:
            ip_address = decrypt_audit_text(model.ip_address)
        except DecryptionError:
            ip_address, undecryptable = None, True
        event = {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "event_type": model.event_type,
            "entity_type": model.entity_type,
            "entity_id": model.entity_id,
            "detail": model.detail,
            "metadata": metadata,
            "user_id": model.user_id,
            "ip_address": ip_address,
            "created_at": serialize_datetime(model.created_at),
            "seq": model.seq,
            "prev_hash": model.prev_hash,
            "hash": model.hash,
        }
        if undecryptable:
            # Deliberately NOT in CHAINED_FIELDS -- it is a read-time observation about this
            # process's keys, not part of what the row committed to.
            event["metadata_undecryptable"] = True
        return event

    @staticmethod
    def _bundle_to_dict(model: Bundle) -> Dict[str, Any]:
        return {
            "id": model.id,
            "version": model.version,
            "content": copy.deepcopy(model.content or {}),
            "encrypted_content": model.encrypted_content,
            "encrypted_key": model.encrypted_key,
            "signature": model.signature,
            "checksum": model.checksum,
            "superseded": model.superseded,
            "compiled_at": serialize_datetime(model.compiled_at),
            "expires_at": serialize_datetime(model.expires_at),
        }

    def list_connectors(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(Connector).where(Connector.tenant_id == resolved).order_by(Connector.created_at)).all()
            return [self._connector_to_dict(row) for row in rows]

    def get_connector(self, connector_id: str, include_secrets: bool = False, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.scalar(select(Connector).where(Connector.tenant_id == resolved, Connector.public_id == connector_id))
            return self._connector_to_dict(model, include_secrets=include_secrets) if model else None

    def create_connector(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id, payload_tenant=payload.get("tenant_id"))
        now = datetime.utcnow()
        with self.connect() as session:
            session.add(
                Connector(
                    tenant_id=resolved,
                    public_id=payload["id"],
                    name=payload["name"],
                    icon=payload.get("icon"),
                    color=payload.get("color"),
                    description=payload.get("description"),
                    schema_fields=copy.deepcopy(payload.get("schema_paths", [])),
                    sample_payload=copy.deepcopy(payload.get("sample_payload", {})),
                    is_active=bool(payload.get("is_active", True)),
                    encrypted_config=encrypt_config_payload(copy.deepcopy(payload.get("config", {}))),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                EntityHistory(
                    tenant_id=resolved,
                    entity_type="connector",
                    entity_id=payload["id"],
                    version=1,
                    snapshot=copy.deepcopy(payload),
                    created_at=now,
                )
            )
        created = self.get_connector(payload["id"], tenant_id=resolved)
        if created is None:  # pragma: no cover - the row was just written in this transaction
            raise RuntimeError("connector {0} vanished immediately after creation".format(payload["id"]))
        return created

    def update_connector(self, connector_id: str, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_connector(connector_id, include_secrets=True, tenant_id=resolved)
        if not current:
            return None
        next_value = copy.deepcopy(current)
        next_value.update({key: value for key, value in patch.items() if value is not None})
        with self.connect() as session:
            model = session.scalar(select(Connector).where(Connector.tenant_id == resolved, Connector.public_id == connector_id))
            if not model:
                return None
            model.name = next_value["name"]
            model.icon = next_value.get("icon")
            model.color = next_value.get("color")
            model.description = next_value.get("description")
            model.schema_fields = copy.deepcopy(next_value.get("schema_paths", []))
            model.sample_payload = copy.deepcopy(next_value.get("sample_payload", {}))
            model.is_active = bool(next_value.get("is_active", True))
            model.encrypted_config = encrypt_config_payload(copy.deepcopy(next_value.get("config", {})))
            model.updated_at = datetime.utcnow()
        return self.get_connector(connector_id, tenant_id=resolved)

    def delete_connector(self, connector_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_connector(connector_id, tenant_id=resolved)
        if not current:
            return None
        with self.connect() as session:
            model = session.scalar(select(Connector).where(Connector.tenant_id == resolved, Connector.public_id == connector_id))
            if model:
                session.delete(model)
        return current

    def _query_by_public_id(self, session: Session, model_type, public_id: str, tenant_id: str):
        return session.scalar(select(model_type).where(model_type.tenant_id == tenant_id, model_type.public_id == public_id))

    def list_variables(self, source_id: Optional[str] = None, status: Optional[str] = None, category: Optional[str] = None, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            stmt = select(Variable).where(Variable.tenant_id == resolved)
            if source_id:
                stmt = stmt.where(Variable.source_id == source_id)
            if status:
                stmt = stmt.where(Variable.status == status)
            if category:
                stmt = stmt.where(Variable.category == category)
            rows = session.scalars(stmt.order_by(Variable.name)).all()
            return [self._variable_to_dict(row) for row in rows]

    def get_variable(self, variable_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = self._query_by_public_id(session, Variable, variable_id, resolved)
            return self._variable_to_dict(row) if row else None

    def create_variable(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id, payload_tenant=payload.get("tenant_id"))
        now = datetime.utcnow()
        snapshot = copy.deepcopy(payload)
        with self.connect() as session:
            model = Variable(
                tenant_id=resolved,
                public_id=payload["id"],
                name=payload["name"],
                category=payload["category"],
                source_id=payload["source_id"],
                code=payload["code"],
                description=payload.get("description"),
                status=payload.get("status", "dev"),
                last_test_result=copy.deepcopy(payload.get("last_test_result")),
                version=int(payload.get("version", 1)),
                created_at=now,
                updated_at=now,
            )
            session.add(model)
            session.add(
                EntityHistory(
                    tenant_id=resolved,
                    entity_type="variable",
                    entity_id=payload["id"],
                    version=int(payload.get("version", 1)),
                    snapshot=snapshot,
                    created_at=now,
                )
            )
        created = self.get_variable(payload["id"], tenant_id=resolved)
        if created is None:  # pragma: no cover - the row was just written in this transaction
            raise RuntimeError("variable {0} vanished immediately after creation".format(payload["id"]))
        self._audit_entity_change("variable", payload["id"], "created", resolved,
                                  version=created.get("version"))
        return created

    def update_variable(self, variable_id: str, patch: Dict[str, Any], bump_version: bool = True, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_variable(variable_id, tenant_id=resolved)
        if not current:
            return None
        next_value = copy.deepcopy(current)
        next_value.update({key: value for key, value in patch.items() if value is not None})
        next_value["version"] = current["version"] + 1 if bump_version and patch else current["version"]
        with self.connect() as session:
            model = self._query_by_public_id(session, Variable, variable_id, resolved)
            if not model:
                return None
            model.name = next_value["name"]
            model.category = next_value["category"]
            model.source_id = next_value["source_id"]
            model.code = next_value["code"]
            model.description = next_value.get("description")
            model.status = next_value["status"]
            model.last_test_result = copy.deepcopy(next_value.get("last_test_result"))
            model.version = next_value["version"]
            model.updated_at = datetime.utcnow()
            if bump_version:
                session.add(
                    EntityHistory(
                        tenant_id=resolved,
                        entity_type="variable",
                        entity_id=variable_id,
                        version=next_value["version"],
                        snapshot=copy.deepcopy(next_value),
                    )
                )
        self._audit_entity_change("variable", variable_id, "updated", resolved,
                                  version=next_value.get("version"))
        return self.get_variable(variable_id, tenant_id=resolved)

    def delete_variable(self, variable_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_variable(variable_id, tenant_id=resolved)
        if not current:
            return None
        with self.connect() as session:
            model = self._query_by_public_id(session, Variable, variable_id, resolved)
            if model:
                session.delete(model)
        # A deletion is the change most worth having in the trail, and the row is gone
        # afterwards -- EntityHistory keeps the snapshot, this records who removed it.
        self._audit_entity_change("variable", variable_id, "deleted", resolved)
        return current

    def list_rules(self, status: Optional[str] = None, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            stmt = select(Rule).where(Rule.tenant_id == resolved)
            if status:
                stmt = stmt.where(Rule.status == status)
            rows = session.scalars(stmt.order_by(Rule.name)).all()
            return [self._rule_to_dict(row) for row in rows]

    def get_rule(self, rule_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = self._query_by_public_id(session, Rule, rule_id, resolved)
            return self._rule_to_dict(row) if row else None

    def create_rule(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id, payload_tenant=payload.get("tenant_id"))
        now = datetime.utcnow()
        with self.connect() as session:
            session.add(
                Rule(
                    tenant_id=resolved,
                    public_id=payload["id"],
                    name=payload["name"],
                    nodes=copy.deepcopy(payload.get("nodes")),
                    tree=copy.deepcopy(payload.get("tree")),
                    rule_format=payload.get("rule_format", "v1"),
                    expression=payload.get("expression"),
                    status=payload.get("status", "dev"),
                    last_test_result=copy.deepcopy(payload.get("last_test_result")),
                    version=int(payload.get("version", 1)),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                EntityHistory(
                    tenant_id=resolved,
                    entity_type="rule",
                    entity_id=payload["id"],
                    version=int(payload.get("version", 1)),
                    snapshot=copy.deepcopy(payload),
                    created_at=now,
                )
            )
        created = self.get_rule(payload["id"], tenant_id=resolved)
        if created is None:  # pragma: no cover - the row was just written in this transaction
            raise RuntimeError("rule {0} vanished immediately after creation".format(payload["id"]))
        self._audit_entity_change("rule", payload["id"], "created", resolved,
                                  version=created.get("version"))
        return created

    def update_rule(self, rule_id: str, patch: Dict[str, Any], bump_version: bool = True, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_rule(rule_id, tenant_id=resolved)
        if not current:
            return None
        next_value = copy.deepcopy(current)
        next_value.update({key: value for key, value in patch.items() if value is not None})
        next_value["version"] = current["version"] + 1 if bump_version and patch else current["version"]
        with self.connect() as session:
            model = self._query_by_public_id(session, Rule, rule_id, resolved)
            if not model:
                return None
            model.name = next_value["name"]
            model.nodes = copy.deepcopy(next_value.get("nodes"))
            model.tree = copy.deepcopy(next_value.get("tree"))
            model.rule_format = next_value.get("rule_format", "v1")
            model.expression = next_value.get("expression")
            model.status = next_value["status"]
            model.last_test_result = copy.deepcopy(next_value.get("last_test_result"))
            model.version = next_value["version"]
            model.updated_at = datetime.utcnow()
            if bump_version:
                session.add(
                    EntityHistory(
                        tenant_id=resolved,
                        entity_type="rule",
                        entity_id=rule_id,
                        version=next_value["version"],
                        snapshot=copy.deepcopy(next_value),
                    )
                )
        self._audit_entity_change("rule", rule_id, "updated", resolved,
                                  version=next_value.get("version"))
        return self.get_rule(rule_id, tenant_id=resolved)

    def delete_rule(self, rule_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_rule(rule_id, tenant_id=resolved)
        if not current:
            return None
        with self.connect() as session:
            model = self._query_by_public_id(session, Rule, rule_id, resolved)
            if model:
                session.delete(model)
        # A deletion is the change most worth having in the trail, and the row is gone
        # afterwards -- EntityHistory keeps the snapshot, this records who removed it.
        self._audit_entity_change("rule", rule_id, "deleted", resolved)
        return current

    def list_scorecards(self, status: Optional[str] = None, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            stmt = select(Scorecard).where(Scorecard.tenant_id == resolved)
            if status:
                stmt = stmt.where(Scorecard.status == status)
            rows = session.scalars(stmt.order_by(Scorecard.name)).all()
            return [self._scorecard_to_dict(row) for row in rows]

    def get_scorecard(self, scorecard_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = self._query_by_public_id(session, Scorecard, scorecard_id, resolved)
            return self._scorecard_to_dict(row) if row else None

    def create_scorecard(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id, payload_tenant=payload.get("tenant_id"))
        now = datetime.utcnow()
        with self.connect() as session:
            session.add(
                Scorecard(
                    tenant_id=resolved,
                    public_id=payload["id"],
                    name=payload["name"],
                    base_score=int(payload.get("base_score", 300)),
                    max_score=int(payload.get("max_score", 900)),
                    bins=copy.deepcopy(payload.get("bins", [])),
                    status=payload.get("status", "dev"),
                    last_test_result=copy.deepcopy(payload.get("last_test_result")),
                    version=int(payload.get("version", 1)),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                EntityHistory(
                    tenant_id=resolved,
                    entity_type="scorecard",
                    entity_id=payload["id"],
                    version=int(payload.get("version", 1)),
                    snapshot=copy.deepcopy(payload),
                    created_at=now,
                )
            )
        created = self.get_scorecard(payload["id"], tenant_id=resolved)
        if created is None:  # pragma: no cover - the row was just written in this transaction
            raise RuntimeError("scorecard {0} vanished immediately after creation".format(payload["id"]))
        self._audit_entity_change("scorecard", payload["id"], "created", resolved,
                                  version=created.get("version"))
        return created

    def update_scorecard(self, scorecard_id: str, patch: Dict[str, Any], bump_version: bool = True, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_scorecard(scorecard_id, tenant_id=resolved)
        if not current:
            return None
        next_value = copy.deepcopy(current)
        next_value.update({key: value for key, value in patch.items() if value is not None})
        next_value["version"] = current["version"] + 1 if bump_version and patch else current["version"]
        with self.connect() as session:
            model = self._query_by_public_id(session, Scorecard, scorecard_id, resolved)
            if not model:
                return None
            model.name = next_value["name"]
            model.base_score = int(next_value.get("base_score", 300))
            model.max_score = int(next_value.get("max_score", 900))
            model.bins = copy.deepcopy(next_value.get("bins", []))
            model.status = next_value["status"]
            model.last_test_result = copy.deepcopy(next_value.get("last_test_result"))
            model.version = next_value["version"]
            model.updated_at = datetime.utcnow()
            if bump_version:
                session.add(
                    EntityHistory(
                        tenant_id=resolved,
                        entity_type="scorecard",
                        entity_id=scorecard_id,
                        version=next_value["version"],
                        snapshot=copy.deepcopy(next_value),
                    )
                )
        self._audit_entity_change("scorecard", scorecard_id, "updated", resolved,
                                  version=next_value.get("version"))
        return self.get_scorecard(scorecard_id, tenant_id=resolved)

    def delete_scorecard(self, scorecard_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_scorecard(scorecard_id, tenant_id=resolved)
        if not current:
            return None
        with self.connect() as session:
            model = self._query_by_public_id(session, Scorecard, scorecard_id, resolved)
            if model:
                session.delete(model)
        # A deletion is the change most worth having in the trail, and the row is gone
        # afterwards -- EntityHistory keeps the snapshot, this records who removed it.
        self._audit_entity_change("scorecard", scorecard_id, "deleted", resolved)
        return current

    def list_policies(self, status: Optional[str] = None, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            stmt = select(Policy).where(Policy.tenant_id == resolved)
            if status:
                stmt = stmt.where(Policy.status == status)
            rows = session.scalars(stmt.order_by(Policy.name)).all()
            return [self._policy_to_dict(row) for row in rows]

    def get_policy(self, policy_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = self._query_by_public_id(session, Policy, policy_id, resolved)
            return self._policy_to_dict(row) if row else None

    def create_policy(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id, payload_tenant=payload.get("tenant_id"))
        now = datetime.utcnow()
        with self.connect() as session:
            session.add(
                Policy(
                    tenant_id=resolved,
                    public_id=payload["id"],
                    name=payload["name"],
                    trigger=copy.deepcopy(payload.get("trigger")),
                    steps=copy.deepcopy(payload.get("steps", [])),
                    default_outcome=payload.get("defaultOutcome"),
                    status=payload.get("status", "dev"),
                    last_test_result=copy.deepcopy(payload.get("last_test_result")),
                    version=int(payload.get("version", 1)),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                EntityHistory(
                    tenant_id=resolved,
                    entity_type="policy",
                    entity_id=payload["id"],
                    version=int(payload.get("version", 1)),
                    snapshot=copy.deepcopy(payload),
                    created_at=now,
                )
            )
        created = self.get_policy(payload["id"], tenant_id=resolved)
        if created is None:  # pragma: no cover - the row was just written in this transaction
            raise RuntimeError("policy {0} vanished immediately after creation".format(payload["id"]))
        return created

    def update_policy(self, policy_id: str, patch: Dict[str, Any], bump_version: bool = True, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_policy(policy_id, tenant_id=resolved)
        if not current:
            return None
        next_value = copy.deepcopy(current)
        next_value.update({key: value for key, value in patch.items() if value is not None})
        next_value["version"] = current["version"] + 1 if bump_version and patch else current["version"]
        with self.connect() as session:
            model = self._query_by_public_id(session, Policy, policy_id, resolved)
            if not model:
                return None
            model.name = next_value["name"]
            model.trigger = copy.deepcopy(next_value.get("trigger"))
            model.steps = copy.deepcopy(next_value.get("steps", []))
            model.default_outcome = next_value.get("defaultOutcome")
            model.status = next_value["status"]
            model.lifecycle_status = next_value.get("lifecycle_status", getattr(model, "lifecycle_status", "draft")) or "draft"
            model.last_test_result = copy.deepcopy(next_value.get("last_test_result"))
            model.version = next_value["version"]
            model.updated_at = datetime.utcnow()
            if bump_version:
                session.add(
                    EntityHistory(
                        tenant_id=resolved,
                        entity_type="policy",
                        entity_id=policy_id,
                        version=next_value["version"],
                        snapshot=copy.deepcopy(next_value),
                    )
                )
        return self.get_policy(policy_id, tenant_id=resolved)

    def delete_policy(self, policy_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_policy(policy_id, tenant_id=resolved)
        if not current:
            return None
        with self.connect() as session:
            model = self._query_by_public_id(session, Policy, policy_id, resolved)
            if model:
                session.delete(model)
        return current

    def add_decision(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Persist one decision.

        Raises on a duplicate client_id rather than letting the IntegrityError escape as an
        unhandled 500 from the async writer. Only add_decisions_batch used to handle this, so a
        collision on the /decide path surfaced nowhere: the caller already had its 200, the row
        was gone, and usage was never recorded. The collision itself is now near-impossible
        (the subject moved to user_id) but a device genuinely replaying a client_id through this
        path must still fail loudly rather than silently.
        """
        resolved = self._tenant_id(tenant_id, payload_tenant=payload.get("tenant_id"))
        preview = copy.deepcopy(payload.get("payload", {}))
        # Computed here, never taken from the caller. `payload.get("payload_hash") or ...` let an
        # SDK client set the fingerprint of its own decision to any value it liked — including
        # one collided onto another decision's. Nothing in the codebase reads a decision by
        # payload_hash, so no caller relies on supplying it (verified across the API, the WAL
        # replay path and all five SDKs).
        payload_hash = decision_payload_hash(preview)
        with self.connect() as session:
            decision = Decision(
                id=payload.get("id") or uuid4_str(),
                tenant_id=resolved,
                client_id=payload.get("client_id") or payload.get("clientId"),
                user_id=payload.get("user_id") or payload.get("userId"),
                policy_id=payload.get("policy_id"),
                payload_hash=payload_hash,
                payload_preview=encrypt_decision_field(preview),
                computed_variables=encrypt_decision_field(copy.deepcopy(payload.get("computed_variables", {}))),
                rule_results=encrypt_decision_field(copy.deepcopy(payload.get("rule_results", []))),
                scorecard_result=encrypt_decision_field(copy.deepcopy(payload.get("scorecard_result"))),
                trace=encrypt_decision_field(copy.deepcopy(payload.get("trace", []))),
                outcome=payload["outcome"],
                latency_ms=int(payload.get("latency_ms", 0)),
                source=payload.get("source", "api"),
                sdk_version=payload.get("sdk_version"),
                experiment_id=payload.get("experiment_id"),
                experiment_variant=payload.get("experiment_variant"),
                device_id=payload.get("device_id") or payload.get("deviceId"),
                bundle_hash=payload.get("bundle_hash") or payload.get("bundleHash"),
                created_at=_parse_client_datetime(payload.get("created_at") or payload.get("createdAt")) or datetime.utcnow(),
                received_at=datetime.utcnow(),
            )
            session.add(decision)
            session.flush()
            result = self._decision_to_dict(decision)
            # THE TRANSACTIONAL OUTBOX (parity P09), and the whole reason it is on this line
            # rather than after the `with` block. The decision row and the outbox row commit
            # TOGETHER or not at all, which is the only way to publish a decision to a broker
            # without a window where one exists and the other does not. Outside the transaction
            # this would be ordinary "write then publish" and would lose a message on any crash
            # in between.
            #
            # Best-effort in the sense that a failure here must not lose the decision -- but
            # note the asymmetry with decision_bus below: that one is fire-and-forget because
            # the SSE feed is a live convenience, whereas an outbox row that fails to insert
            # means this decision will never reach a consumer. It is logged loudly rather than
            # swallowed silently.
            from . import decision_outbox

            decision_outbox.enqueue(session, resolved, result)
        # Best-effort real-time fan-out to the live SSE feed (no-op without Redis; never raises).
        from . import decision_bus

        decision_bus.publish_decision(resolved, result)
        # One server-side decision = one billable unit. Metered here, at the single place a
        # Decision row is written, rather than at /api/v1/decide -- that endpoint has two code
        # paths (fast and executor) and there are three more entry points besides it, so
        # metering at the handler would silently miss whichever path was added last.
        #
        # In a `finally` relative to the write: this used to sit after session.flush(), so any
        # exception during the write skipped it entirely and the tenant was under-billed for a
        # decision they had already been served. Metering follows the row, not the happy path.
        self.record_usage(METRIC_DECISIONS, 1, tenant_id=resolved)
        # Same argument as the metering directly above, and the same funnel: /decide has a fast
        # path and an executor path, plus three other entry points, so a hook at any handler
        # would miss whichever path was added last. Emitting from here is what stops outbound
        # webhooks becoming the fourth FAST_DECIDE divergence of this cycle.
        # The resolved tenant is passed in rather than read off `result`: _decision_to_dict does
        # not carry tenant_id, so a listener reading it off the row silently got None and did
        # nothing. Found by the end-to-end webhook test, not by inspection.
        _notify_decision_listeners(self, {**result, "tenant_id": resolved})
        return result

    def add_decisions_batch(self, decisions: List[Dict[str, Any]], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Idempotently ingest a batch of (device-originated) decisions.

        Dedupe is by the client-supplied stable ``id``: a decision whose id already
        exists — or repeats within the same batch — is acknowledged but NOT re-inserted,
        so an at-least-once client retry never double-counts (same guarantee as the
        /decide single-log fix). Every id in the batch is returned in ``acked`` so the
        device can safely clear its local outbox for exactly those rows.

        Returns {received, inserted, duplicates, acked:[id,...]}.
        """
        resolved = self._tenant_id(tenant_id)
        # The device-supplied id is the CLIENT id (stable, unique within the tenant); the stored PK is
        # server-generated. Dedupe is by (tenant_id, client_id): a decision whose client id already
        # exists for this tenant — or repeats within the batch — is acked but not re-inserted, so an
        # at-least-once retry never double-counts, and one tenant's id can never shadow another's.
        client_ids = [str(d["id"]) for d in decisions if d.get("id")]
        acked: List[str] = []
        inserted = 0
        duplicates = 0
        with self.connect() as session:
            existing: set = set()
            if client_ids:
                existing = set(session.scalars(
                    select(Decision.client_id).where(Decision.client_id.in_(client_ids), Decision.tenant_id == resolved)
                ).all())
            seen: set = set()
            now = datetime.utcnow()
            for record in decisions:
                client_id = str(record["id"]) if record.get("id") else None
                ack_id = client_id or uuid4_str()  # what the device references to clear its outbox
                acked.append(ack_id)
                if client_id is not None and (client_id in existing or client_id in seen):
                    duplicates += 1
                    continue
                if client_id is not None:
                    seen.add(client_id)
                preview = copy.deepcopy(record.get("payload", {}))
                payload_hash = decision_payload_hash(preview)  # keyed; never caller-supplied
                row = Decision(
                    id=uuid4_str(),
                    tenant_id=resolved,
                    client_id=client_id,
                    policy_id=record.get("policy_id") or record.get("policyId"),
                    payload_hash=payload_hash,
                    payload_preview=encrypt_decision_field(preview),
                    computed_variables=encrypt_decision_field(copy.deepcopy(record.get("computed_variables", {}))),
                    rule_results=encrypt_decision_field(copy.deepcopy(record.get("rule_results", []))),
                    scorecard_result=encrypt_decision_field(copy.deepcopy(record.get("scorecard_result"))),
                    trace=encrypt_decision_field(copy.deepcopy(record.get("trace", []))),
                    outcome=record.get("outcome", "pending"),
                    latency_ms=int(record.get("latency_ms", record.get("latencyMs", 0)) or 0),
                    source=record.get("source", "on_device"),
                    sdk_version=record.get("sdk_version") or record.get("sdkVersion"),
                    experiment_id=record.get("experiment_id") or record.get("experimentId"),
                    experiment_variant=record.get("experiment_variant") or record.get("experimentVariant"),
                    device_id=record.get("device_id") or record.get("deviceId"),
                    bundle_hash=record.get("bundle_hash") or record.get("bundleHash"),
                    # Preserve the on-device decision time when supplied; stamp the server receipt time
                    # separately so ordering/retention never depends on a drifted device clock.
                    created_at=_parse_client_datetime(record.get("created_at") or record.get("createdAt")) or now,
                    received_at=now,
                )
                # Per-row savepoint: a concurrent worker inserting the same (tenant_id, client_id)
                # trips the unique constraint (IntegrityError); we absorb it as a duplicate instead of
                # failing the whole batch — DB-level idempotency on top of the in-app dedupe, so
                # retries under races never double-count and never lose the rest of the batch.
                try:
                    with session.begin_nested():
                        session.add(row)
                        session.flush()
                    inserted += 1
                except IntegrityError:
                    duplicates += 1
        # Bill `inserted`, NOT `received`. A device retrying an unacked batch re-sends decisions
        # the server already stored; counting those would charge a customer for their own
        # network being unreliable. Outside the `with` block on purpose -- record_usage opens
        # its own session, and nesting one inside this transaction deadlocks on SQLite.
        self.record_usage(METRIC_DECISIONS, inserted, tenant_id=resolved)
        return {"received": len(decisions), "inserted": inserted, "duplicates": duplicates, "acked": acked}

    def list_decisions(self, tenant_id: Optional[str] = None, limit: int = 200, offset: int = 0,
                       client_id: Optional[str] = None, policy_id: Optional[str] = None,
                       outcome: Optional[str] = None, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        from . import decision_log
        decision_log.flush()  # read-after-write: see any just-submitted async writes
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            # Bounded by default: the decisions table grows without limit in
            # production (every /decide writes a row), so an unpaged fetch would
            # load tens of thousands of full-context rows and time out / OOM.
            # Filters are applied IN SQL: case-wise history ("this customer's
            # decisions") is unusable if the caller has to page the whole table
            # and filter client-side.
            query = select(Decision).where(Decision.tenant_id == resolved)
            if client_id:
                query = query.where(Decision.client_id == client_id)
            if user_id:
                query = query.where(Decision.user_id == user_id)
            if policy_id:
                query = query.where(Decision.policy_id == policy_id)
            if outcome:
                query = query.where(Decision.outcome == outcome)
            query = query.order_by(desc(Decision.created_at)).limit(max(1, min(limit, 1000))).offset(max(0, offset))
            rows = session.scalars(query).all()
            return [self._decision_to_dict(row) for row in rows]

    def get_decision(self, decision_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Point read of one decision, tolerant of a cross-worker write (T5.6).

        `decision_log.flush()` alone is not enough here. It waits on THIS process's pending
        futures, but the decision-log write is async and uvicorn runs several workers, so the
        `decision_id` that /decide just handed back is routinely still in flight in a DIFFERENT
        worker when the follow-up GET arrives -- and the caller gets a 404 for a decision that
        exists. `await_row` flushes locally and then re-polls briefly, so the cost lands only on
        the miss path and a hit is as fast as before.

        Deliberately NOT applied to list_decisions/sample_policy_decisions: a bounded wait only
        makes sense when a specific, known id is expected to be there.
        """
        from . import decision_log

        resolved = self._tenant_id(tenant_id)

        def _fetch() -> Optional[Dict[str, Any]]:
            with self.connect() as session:
                row = session.scalar(
                    select(Decision).where(Decision.tenant_id == resolved, Decision.id == decision_id))
                return self._decision_to_dict(row) if row else None

        return decision_log.await_row(_fetch)

    def sample_policy_decisions(self, policy_id: str, tenant_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        """The most recent full decision rows for one policy (payload + computed variables included,
        decrypted) — backs policy backtesting, which re-runs a candidate bundle over real traffic."""
        from . import decision_log
        decision_log.flush()
        resolved = self._tenant_id(tenant_id)
        capped = max(1, min(int(limit), 2000))
        with self.connect() as session:
            rows = session.scalars(
                select(Decision)
                .where(Decision.tenant_id == resolved, Decision.policy_id == policy_id)
                .order_by(desc(Decision.created_at))
                .limit(capped)
            ).all()
            return [self._decision_to_dict(row) for row in rows]

    def count_decisions(self, tenant_id: Optional[str] = None) -> int:
        from . import decision_log
        decision_log.flush()
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            return int(session.scalar(select(func.count()).select_from(Decision).where(Decision.tenant_id == resolved)) or 0)

    def iter_policy_decisions(
        self,
        policy_id: str,
        tenant_id: Optional[str] = None,
        page_size: int = 2000,
    ):
        """Stream ALL decisions for one policy, newest-first, one page at a time.

        Unlike ``sample_policy_decisions`` (bounded to <=2000, loaded at once) this is a generator
        that holds at most ``page_size`` rows in memory regardless of the total population — so a
        full-population backtest over 1M+ decisions stays memory-bounded (backs backtest full mode).
        Uses keyset pagination on ``(created_at, id)`` so it stays O(page) at any offset instead of
        the O(n^2) degradation of LIMIT/OFFSET on a large table."""
        from sqlalchemy import and_
        from . import decision_log

        decision_log.flush()
        resolved = self._tenant_id(tenant_id)
        last_created: Optional[datetime] = None
        last_id: Optional[str] = None
        while True:
            with self.connect() as session:
                query = select(Decision).where(
                    Decision.tenant_id == resolved, Decision.policy_id == policy_id
                )
                if last_created is not None:
                    # Strict keyset cursor: rows strictly "after" the last one in (created_at, id) desc order.
                    query = query.where(
                        or_(
                            Decision.created_at < last_created,
                            and_(Decision.created_at == last_created, Decision.id < last_id),
                        )
                    )
                rows = session.scalars(
                    query.order_by(desc(Decision.created_at), desc(Decision.id)).limit(page_size)
                ).all()
                if not rows:
                    return
                for row in rows:
                    last_created = row.created_at
                    last_id = row.id
                    yield self._decision_to_dict(row)
                if len(rows) < page_size:
                    return

    def iter_decisions_window(
        self,
        tenant_id: Optional[str] = None,
        since: Optional[datetime] = None,
        max_rows: int = 100000,
        page_size: int = 2000,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """Fetch decisions within a time window, paginated at the DB level so a
        report is complete (no silent 1000-row truncation) while memory stays
        bounded. The date filter is pushed into the query; returns
        (decisions, truncated) where truncated=True means max_rows was hit."""
        from . import decision_log
        decision_log.flush()
        resolved = self._tenant_id(tenant_id)
        collected: List[Dict[str, Any]] = []
        truncated = False
        with self.connect() as session:
            base = select(Decision).where(Decision.tenant_id == resolved)
            if since is not None:
                base = base.where(Decision.created_at >= since)
            base = base.order_by(desc(Decision.created_at))
            offset = 0
            while len(collected) < max_rows:
                rows = session.scalars(base.limit(page_size).offset(offset)).all()
                if not rows:
                    break
                for row in rows:
                    collected.append(self._decision_to_dict(row))
                    if len(collected) >= max_rows:
                        truncated = True
                        break
                if len(rows) < page_size:
                    break
                offset += page_size
        return collected, truncated

    def decisions_after(
        self,
        tenant_id: Optional[str] = None,
        after: Optional[datetime] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Return decisions created strictly after `after`, oldest-first.

        Backs the live SSE feed: each poll asks for rows newer than the last one it saw, so
        the stream advances a monotonic cursor without re-sending. `after=None` returns the
        most recent `limit` rows (flipped to oldest-first) as the stream's opening backlog.
        The (tenant_id, created_at) index keeps each poll cheap even as the table grows.
        """
        from . import decision_log
        decision_log.flush()  # read-after-write: see just-submitted async writes
        resolved = self._tenant_id(tenant_id)
        def _with_cursor(row: Decision) -> Dict[str, Any]:
            # Carry the full-precision created_at alongside the (second-truncated) serialized
            # form so the SSE loop advances a precise cursor and never re-sends a same-second row.
            data = self._decision_to_dict(row)
            data["_created_at_raw"] = row.created_at
            return data

        with self.connect() as session:
            query = select(Decision).where(Decision.tenant_id == resolved)
            if after is not None:
                query = query.where(Decision.created_at > after).order_by(Decision.created_at).limit(max(1, min(limit, 1000)))
                rows = session.scalars(query).all()
                return [_with_cursor(row) for row in rows]
            # Opening backlog: newest N, returned oldest-first so the cursor advances forward.
            query = query.order_by(desc(Decision.created_at)).limit(max(1, min(limit, 1000)))
            rows = list(session.scalars(query).all())
            rows.reverse()
            return [_with_cursor(row) for row in rows]

    def experiment_variant_rollup(
        self,
        tenant_id: Optional[str],
        experiment_id: str,
    ) -> Dict[str, Dict[str, Any]]:
        """Exact per-variant decision aggregates for ONE A/B experiment, computed in SQL over
        ALL of that experiment's decisions (no 200-row cap). Returns {variant_id: {users,
        approved, rejected, reviewed, latency_sum}}. Scoped by experiment_id — not by variant
        id, which repeats across experiments (two experiments both have a "champion") and would
        otherwise cross-count."""
        if not experiment_id:
            return {}
        resolved = self._tenant_id(tenant_id)
        approved = func.sum(case((Decision.outcome == "approve", 1), else_=0))
        rejected = func.sum(case((Decision.outcome == "reject", 1), else_=0))
        reviewed = func.sum(case((Decision.outcome == "review", 1), else_=0))
        with self.connect() as session:
            rows = session.execute(
                select(
                    Decision.experiment_variant,
                    func.count().label("users"),
                    approved.label("approved"),
                    rejected.label("rejected"),
                    reviewed.label("reviewed"),
                    func.coalesce(func.sum(Decision.latency_ms), 0).label("latency_sum"),
                )
                .where(Decision.tenant_id == resolved, Decision.experiment_id == experiment_id)
                .group_by(Decision.experiment_variant)
            ).all()
        return {
            str(row.experiment_variant): {
                "users": int(row.users or 0),
                "approved": int(row.approved or 0),
                "rejected": int(row.rejected or 0),
                "reviewed": int(row.reviewed or 0),
                "latency_sum": int(row.latency_sum or 0),
            }
            for row in rows
        }

    def decision_facts(
        self,
        tenant_id: Optional[str] = None,
        since: Optional[datetime] = None,
        max_rows: int = 200_000,
        page_size: int = 5_000,
    ) -> List[Dict[str, Any]]:
        """Lightweight, column-projected decision rows for analytics — only the fields the
        dashboards need (no payload/trace/variables), paginated at the DB level within a time
        window. Replaces the 200-row `list_decisions` cap in the analytics path so percentiles,
        timeseries, and rollups are computed over the full (windowed) set without loading heavy
        JSON columns or exhausting memory."""
        from . import decision_log
        decision_log.flush()
        resolved = self._tenant_id(tenant_id)
        collected: List[Dict[str, Any]] = []
        with self.connect() as session:
            base = select(
                Decision.created_at,
                Decision.outcome,
                Decision.source,
                Decision.latency_ms,
                Decision.policy_id,
                Decision.experiment_variant,
            ).where(Decision.tenant_id == resolved)
            if since is not None:
                base = base.where(Decision.created_at >= since)
            base = base.order_by(desc(Decision.created_at))
            offset = 0
            while len(collected) < max_rows:
                rows = session.execute(base.limit(page_size).offset(offset)).all()
                if not rows:
                    break
                for row in rows:
                    collected.append(
                        {
                            "created_at": serialize_datetime(row.created_at),
                            "outcome": row.outcome,
                            "source": row.source,
                            "latency_ms": row.latency_ms,
                            "policy_id": row.policy_id,
                            "experiment_variant": row.experiment_variant,
                        }
                    )
                    if len(collected) >= max_rows:
                        break
                if len(rows) < page_size:
                    break
                offset += page_size
        return collected

    def archive_and_purge_decisions(
        self,
        tenant_id: Optional[str],
        older_than: datetime,
        archiver,
        batch_size: int = 1000,
        max_batches: int = 10_000,
    ) -> int:
        """Move decisions created before `older_than` out of the hot DB into `archiver`, then
        delete them. Archive first, purge only on success — so a sink failure never loses data.
        The archive network write happens OUTSIDE any DB transaction (batches are read, then
        written, then deleted in separate short transactions) so it never holds row locks."""
        resolved = self._tenant_id(tenant_id)
        total = 0
        for _ in range(max_batches):
            with self.connect() as session:
                rows = session.scalars(
                    select(Decision)
                    .where(Decision.tenant_id == resolved, Decision.created_at < older_than)
                    .order_by(Decision.created_at)
                    .limit(batch_size)
                ).all()
                records = []
                ids = []
                for row in rows:
                    record = self._decision_to_dict(row)
                    record["tenant_id"] = resolved
                    records.append(record)
                    ids.append(row.id)
            if not records:
                break
            archiver.write(records)  # raises -> we never reach the purge below
            with self.connect() as session:
                session.execute(delete(Decision).where(Decision.id.in_(ids)))
            total += len(ids)
            if len(records) < batch_size:
                break
        return total

    def add_promotion(self, entity_type: str, entity_id: str, from_status: str, to_status: str, promoted_by: str, reason: str, tenant_id: Optional[str] = None, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            promotion = Promotion(
                tenant_id=resolved,
                entity_type=entity_type,
                entity_id=entity_id,
                from_status=from_status,
                to_status=to_status,
                promoted_by=promoted_by,
                reason=reason,
                snapshot_json=copy.deepcopy(snapshot) if snapshot else None,
            )
            session.add(promotion)
            session.flush()
            return self._promotion_to_dict(promotion)

    def last_promotion_snapshot(self, entity_type: str, entity_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The most recent stored definition snapshot for an entity — the baseline a draft is
        diffed against before the next promotion."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalars(
                select(Promotion)
                .where(Promotion.tenant_id == resolved, Promotion.entity_type == entity_type,
                       Promotion.entity_id == entity_id, Promotion.snapshot_json.isnot(None))
                .order_by(desc(Promotion.id)).limit(1)
            ).first()
            return copy.deepcopy(row.snapshot_json) if row and row.snapshot_json else None

    def get_promotion(self, promotion_id: int, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetch one promotion including its full definition snapshot (for release rollback)."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(
                select(Promotion).where(Promotion.id == int(promotion_id), Promotion.tenant_id == resolved)
            )
            if not row:
                return None
            result = self._promotion_to_dict(row)
            result["snapshot"] = copy.deepcopy(row.snapshot_json)
            return result

    def list_promotions(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(Promotion).where(Promotion.tenant_id == resolved).order_by(desc(Promotion.id))).all()
            return [self._promotion_to_dict(row) for row in rows]

    def last_promotion_actor(self, entity_type: str, entity_id: str, to_status: str, tenant_id: Optional[str] = None) -> Optional[str]:
        """The ``promoted_by`` (authenticated actor) of the most recent promotion of this entity to
        ``to_status`` — used by dual-control to enforce that the person approving to production is not
        the same one who promoted it to UAT. None if there is no such prior promotion."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalars(
                select(Promotion)
                .where(Promotion.tenant_id == resolved, Promotion.entity_type == entity_type,
                       Promotion.entity_id == entity_id, Promotion.to_status == to_status)
                .order_by(desc(Promotion.id)).limit(1)
            ).first()
            return row.promoted_by if row else None

    def dual_control_enabled(self, tenant_id: Optional[str] = None) -> bool:
        """Whether this workspace requires two-person control (maker != checker) for promotion to
        production. Stored under engine_config.require_dual_control; off by default."""
        settings = self.get_settings(tenant_id=tenant_id)
        return bool((settings.get("engine_config", {}) or {}).get("require_dual_control", False))

    def claim_saml_assertion(self, assertion_id: str, not_on_or_after: Optional[datetime] = None,
                             tenant_id: Optional[str] = None) -> bool:
        """Record a SAML assertion id as consumed. Returns False if it was already used.

        The UNIQUE(tenant_id, assertion_id) constraint is what makes this a claim rather than a
        check-then-write: two concurrent POSTs of the same captured assertion race to INSERT and
        exactly one wins, so a replay cannot slip through the window between reading and writing.
        """
        resolved = self._tenant_id(tenant_id)
        try:
            with self.connect() as session:
                session.add(SamlAssertionSeen(
                    tenant_id=resolved, assertion_id=assertion_id,
                    not_on_or_after=not_on_or_after, created_at=datetime.utcnow()))
            return True
        except IntegrityError:
            return False

    # ── durable login-failure counters (see models.LoginAttempt) ─────────
    def read_login_attempts(self, key: str) -> Tuple[int, Optional[datetime]]:
        """(failures, expires_at) for this key, or (0, None) once the window has passed.

        An expired row is deleted on read rather than left for a sweep: these rows are only ever
        consulted on the login path, so the reader is the one process guaranteed to notice, and
        the table stays bounded without a job that could silently stop running.
        """
        now = datetime.utcnow()
        with self.connect() as session:
            row = session.get(LoginAttempt, key)
            if row is None:
                return 0, None
            if row.expires_at <= now:
                session.delete(row)
                return 0, None
            return int(row.failures or 0), row.expires_at

    def bump_login_attempts(self, key: str, ttl_seconds: int) -> Tuple[int, datetime]:
        """Count one failure and return (failures, expires_at).

        The window is set on the FIRST failure and never extended, matching the Redis tier's
        one-shot EXPIRE -- refreshing it on every attempt would let an attacker keep a victim
        locked out indefinitely by continuing to guess.

        Retries on the primary-key race the way add_audit_event does: two replicas can read the
        same absent row and both INSERT, and the constraint turns that into a retry rather than
        a lost count.
        """
        now = datetime.utcnow()
        for attempt in range(5):
            try:
                with self.connect() as session:
                    row = session.get(LoginAttempt, key)
                    if row is not None and row.expires_at <= now:
                        session.delete(row)
                        session.flush()
                        row = None
                    if row is None:
                        expires = now + timedelta(seconds=max(1, ttl_seconds))
                        session.add(LoginAttempt(key=key, failures=1, expires_at=expires))
                        return 1, expires
                    row.failures = int(row.failures or 0) + 1
                    return int(row.failures), row.expires_at
            except IntegrityError:
                if attempt >= 4:
                    raise
        raise RuntimeError("unreachable")  # pragma: no cover

    def clear_login_attempts(self, key: str) -> None:
        """Reset after a successful authentication."""
        with self.connect() as session:
            row = session.get(LoginAttempt, key)
            if row is not None:
                session.delete(row)

    def prune_saml_assertions(self, tenant_id: Optional[str] = None) -> int:
        """Drop consumed assertions whose validity window has passed -- they can no longer be
        accepted on their own merits, so keeping them serves nothing and the table would grow
        without bound."""
        resolved = self._tenant_id(tenant_id)
        now = datetime.utcnow()
        with self.connect() as session:
            rows = session.scalars(
                select(SamlAssertionSeen).where(
                    SamlAssertionSeen.tenant_id == resolved,
                    SamlAssertionSeen.not_on_or_after.is_not(None),
                    SamlAssertionSeen.not_on_or_after < now)).all()
            for row in rows:
                session.delete(row)
            return len(rows)

    def add_error_event(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id, payload_tenant=payload.get("tenant_id"))
        with self.connect() as session:
            model = ErrorEvent(
                tenant_id=resolved,
                scope=payload.get("scope", "system"),
                entity_type=payload.get("entity_type"),
                entity_id=payload.get("entity_id"),
                stage=payload.get("stage", "unknown"),
                message=payload.get("message", "Unknown error"),
                details_json=copy.deepcopy(payload.get("details", {})),
                created_at=datetime.utcnow(),
            )
            session.add(model)
            session.flush()
            return self._error_event_to_dict(model)

    def list_error_events(self, tenant_id: Optional[str] = None, limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            query = select(ErrorEvent).where(ErrorEvent.tenant_id == resolved).order_by(desc(ErrorEvent.created_at)).limit(max(1, min(limit, 1000))).offset(max(0, offset))
            rows = session.scalars(query).all()
            return [self._error_event_to_dict(row) for row in rows]

    # ── idempotency (Track B6) ──────────────────────────────────────────────

    def claim_idempotency_key(self, key: str, request_hash: str,
                              tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Try to claim `key` for this request. Returns what the caller should do next.

          {"action": "execute"}                           -- we won the claim; run the handler
          {"action": "replay", "status": .., "body": ..}  -- a completed response exists
          {"action": "in_flight"}                         -- claimed but unfinished: a
                                                             concurrent duplicate, answer 409
          {"action": "mismatch"}                          -- same key, DIFFERENT request: 422

        The claim is an INSERT against uq(tenant_id, key), so two concurrent requests race at
        the database and exactly one wins. Checking-then-inserting in Python would let both
        pass the check and execute the write twice -- the exact bug this feature exists to
        prevent.
        """
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            existing = session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.tenant_id == resolved, IdempotencyRecord.key == key))
            if existing is not None:
                if is_idempotency_expired(existing.created_at):
                    # Expired: the key is free again. Reset in place rather than deleting and
                    # re-inserting, which would reopen the race the constraint closes.
                    existing.request_hash = request_hash
                    existing.status_code = None
                    existing.response_body = None
                    existing.created_at = datetime.utcnow()
                    return {"action": "execute"}
                if existing.request_hash != request_hash:
                    return {"action": "mismatch"}
                if existing.status_code is None:
                    return {"action": "in_flight"}
                return {"action": "replay", "status": int(existing.status_code),
                        "body": existing.response_body or ""}
            try:
                session.add(IdempotencyRecord(tenant_id=resolved, key=key,
                                              request_hash=request_hash,
                                              created_at=datetime.utcnow()))
                session.flush()
                return {"action": "execute"}
            except IntegrityError:
                # Lost the race to a concurrent claim between the SELECT and the INSERT.
                session.rollback()
                return {"action": "in_flight"}

    def complete_idempotency_key(self, key: str, status_code: int, body: str,
                                 tenant_id: Optional[str] = None) -> None:
        """Record the response, or RELEASE the claim if the status must not be remembered."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.tenant_id == resolved, IdempotencyRecord.key == key))
            if row is None:
                return
            if not should_remember_idempotent(status_code):
                # A 5xx is usually transient. Deleting frees the key so an honest retry can
                # actually be retried, instead of being pinned to a failure that has passed.
                session.delete(row)
                return
            row.status_code = int(status_code)
            row.response_body = body

    def release_idempotency_key(self, key: str, tenant_id: Optional[str] = None) -> None:
        """Drop a claim outright -- used when the handler raised, so the key is not left
        permanently in-flight and answering 409 forever."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.tenant_id == resolved, IdempotencyRecord.key == key))
            if row is not None:
                session.delete(row)

    # ── usage metering (Track B4) ───────────────────────────────────────────

    def record_usage(self, metric: str, amount: int = 1, tenant_id: Optional[str] = None,
                     day: Optional[date] = None) -> None:
        """Add `amount` to a tenant's counter for one metric on one UTC day.

        UPDATE-then-INSERT rather than read-modify-write: the UPDATE is atomic at the database,
        so two concurrent decisions increment rather than clobber. A read-modify-write would
        lose one of them and under-bill -- the failure mode that costs money silently instead
        of erroring loudly.

        Best effort by contract. Metering must never fail a decision the customer already
        received: refusing to serve because the meter is down is strictly worse than an
        under-count that reconciliation can repair. Enforcement is a separate, deliberate gate
        (see app/quota.py); this is only the counter.
        """
        if amount <= 0:
            return
        resolved = self._tenant_id(tenant_id)
        bucket = day or datetime.utcnow().date()
        try:
            with self.connect() as session:
                updated = session.execute(
                    update(UsageRecord)
                    .where(UsageRecord.tenant_id == resolved, UsageRecord.day == bucket,
                           UsageRecord.metric == metric)
                    .values(count=UsageRecord.count + amount, updated_at=datetime.utcnow())
                ).rowcount
                if updated:
                    return
                try:
                    session.add(UsageRecord(tenant_id=resolved, day=bucket, metric=metric,
                                            count=amount, updated_at=datetime.utcnow()))
                    session.flush()
                except IntegrityError:
                    # Another writer created the bucket between our UPDATE and INSERT. Roll back
                    # the failed insert and apply the increment to the row that now exists --
                    # dropping it here is exactly the lost-revenue bug the constraint exists to
                    # prevent.
                    session.rollback()
                    session.execute(
                        update(UsageRecord)
                        .where(UsageRecord.tenant_id == resolved, UsageRecord.day == bucket,
                               UsageRecord.metric == metric)
                        .values(count=UsageRecord.count + amount, updated_at=datetime.utcnow())
                    )
        except Exception:  # noqa: BLE001 - see the best-effort contract above
            LOGGER.warning("usage: failed to record %s+%s for tenant %s", metric, amount, resolved)

    def get_usage_daily(self, tenant_id: Optional[str] = None, start: Optional[date] = None,
                        end: Optional[date] = None, metric: Optional[str] = None) -> List[Dict[str, Any]]:
        """Daily rows in [start, end) -- end EXCLUSIVE, matching quota.period_bounds."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            stmt = select(UsageRecord).where(UsageRecord.tenant_id == resolved)
            if start is not None:
                stmt = stmt.where(UsageRecord.day >= start)
            if end is not None:
                stmt = stmt.where(UsageRecord.day < end)
            if metric:
                stmt = stmt.where(UsageRecord.metric == metric)
            rows = session.scalars(stmt.order_by(UsageRecord.day.asc(), UsageRecord.metric.asc())).all()
            return [
                {"day": row.day.isoformat(), "metric": row.metric, "count": int(row.count)}
                for row in rows
            ]

    def get_usage_total(self, metric: str, tenant_id: Optional[str] = None,
                        start: Optional[date] = None, end: Optional[date] = None) -> int:
        """Summed count for one metric over [start, end). This is the billable number."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            stmt = select(func.coalesce(func.sum(UsageRecord.count), 0)).where(
                UsageRecord.tenant_id == resolved, UsageRecord.metric == metric)
            if start is not None:
                stmt = stmt.where(UsageRecord.day >= start)
            if end is not None:
                stmt = stmt.where(UsageRecord.day < end)
            return int(session.execute(stmt).scalar() or 0)

    def _audit_entity_change(self, entity_type: str, entity_id: str, action: str,
                             tenant_id: str, version: Optional[int] = None) -> None:
        """Record a create/update/delete of an authoring entity in the audit trail.

        Rules, variables, scorecards and decision tables wrote an EntityHistory snapshot but
        never an AuditEvent -- so "who changed which rule, when" was missing for the most-edited
        entity types in a product whose whole job is governed decisioning. Policies, identity,
        SDK, experiments, reviews and webhooks all emitted one; these four were simply never
        wired up. Found by qa case 6.2, which had been written off as a test bug.

        Best-effort by contract, matching add_audit_event's other callers: an authoring change
        that succeeded must not be rolled back because the audit write failed. The chain's own
        integrity is unaffected -- a missing event is a gap, not a broken link.
        """
        try:
            self.add_audit_event(
                {
                    "event_type": f"{entity_type}_{action}",
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "detail": f"{entity_type.replace('_', ' ').title()} {entity_id} {action}.",
                    "metadata": {"version": version} if version is not None else {},
                },
                tenant_id=tenant_id,
            )
        except Exception:  # noqa: BLE001 - see the best-effort contract above
            LOGGER.warning("audit: failed to record %s %s for %s", entity_type, action, entity_id)

    # Reserved tenant id for events that belong to the PLATFORM, not to any workspace.
    #
    # The platform-admin account has no tenant, so `_tenant_id` fell through to
    # get_current_tenant_id() or default_tenant_id and filed superadmin events -- including the
    # failed-login alerts raised during a credential-stuffing run -- into an unrelated customer's
    # tamper-evident chain. That workspace's audit export then shows sign-in attempts against an
    # account its owners do not have and cannot investigate, and the chain they are asked to
    # attest to contains events that were never theirs.
    #
    # A reserved id rather than a new table: the chain, verification, retention and export all key
    # on tenant_id already, so this gets a real, separately-verifiable chain for free. Tenant ids
    # are uuid4 strings, so this cannot collide with one.
    PLATFORM_TENANT_ID = "__platform__"

    def add_audit_event(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Append one audit event, linked into the tenant's tamper-evident chain.

        Retries on the (tenant_id, seq) unique constraint: two concurrent appends can read the
        same tail and compute the same next seq, and the constraint turns that race into a
        retry instead of a silently forked chain. Serializing every audit write behind a lock
        would be the alternative and would make audit logging a throughput ceiling.
        """
        resolved = self._tenant_id(tenant_id, payload_tenant=payload.get("tenant_id"))
        attempts = 0
        while True:
            attempts += 1
            try:
                with self.connect() as session:
                    tail = session.execute(
                        select(AuditEvent.seq, AuditEvent.hash)
                        .where(AuditEvent.tenant_id == resolved, AuditEvent.seq.isnot(None))
                        .order_by(desc(AuditEvent.seq))
                        .limit(1)
                    ).first()
                    seq = (tail[0] + 1) if tail else 1
                    prev_hash = (tail[1] if tail and tail[1] else GENESIS_HASH)

                    model = AuditEvent(
                        tenant_id=resolved,
                        event_type=payload["event_type"],
                        entity_type=payload.get("entity_type"),
                        entity_id=payload.get("entity_id"),
                        detail=payload.get("detail", ""),
                        # Encrypted at rest; _audit_event_to_dict decrypts, so the chain hash
                        # below is still computed over the plaintext the verifier rebuilds and
                        # every pre-existing hash keeps verifying.
                        metadata_json=encrypt_audit_field(copy.deepcopy(payload.get("metadata", {}))),
                        user_id=payload.get("user_id"),
                        ip_address=encrypt_audit_text(payload.get("ip_address")),
                        created_at=datetime.utcnow(),
                        seq=seq,
                        prev_hash=prev_hash,
                    )
                    session.add(model)
                    session.flush()  # assigns id; created_at is set above
                    # Hash the dict the VERIFIER will rebuild, not a hand-rolled copy of it --
                    # any drift between the two shapes would show up as a false tamper report.
                    model.hash = compute_audit_hash(self._audit_event_to_dict(model))
                    session.flush()
                    written = self._audit_event_to_dict(model)
                _notify_audit_listeners(self, written)
                return written
            except IntegrityError:
                if attempts >= 5:
                    raise
                continue

    def list_audit_events(self, tenant_id: Optional[str] = None, event_type: Optional[str] = None,
                          entity_type: Optional[str] = None, entity_id: Optional[str] = None,
                          limit: Optional[int] = None, offset: int = 0) -> List[Dict[str, Any]]:
        """Read the audit trail.

        entity_type/entity_id/limit/offset were added when the trail got an HTTP read path: the
        table is append-only and unbounded, so an unpaged fetch of a mature tenant is the same
        timeout-and-OOM that list_decisions already documents. limit stays None by default so
        the existing internal callers, which filter to one event_type and expect every match,
        are unchanged.
        """
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            stmt = select(AuditEvent).where(AuditEvent.tenant_id == resolved)
            if event_type:
                stmt = stmt.where(AuditEvent.event_type == event_type)
            if entity_type:
                stmt = stmt.where(AuditEvent.entity_type == entity_type)
            if entity_id:
                stmt = stmt.where(AuditEvent.entity_id == entity_id)
            # seq is the chain position and is unique per tenant; created_at is not, so two
            # events written in the same tick could otherwise swap places between pages and a
            # paging reader would skip one. Matches verify_audit_chain's reasoning.
            stmt = stmt.order_by(desc(AuditEvent.seq), desc(AuditEvent.created_at))
            if offset:
                stmt = stmt.offset(offset)
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = session.scalars(stmt).all()
            return [self._audit_event_to_dict(row) for row in rows]

    def verify_audit_chain(self, tenant_id: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        """Walk this tenant's audit chain oldest-first and report on its integrity.

        Ordered by `seq`, not `created_at`: created_at has second-or-better resolution but is
        not guaranteed unique, and ordering the walk by a non-unique column would let two
        events written in the same tick swap places and report a spurious break.

        Legacy rows (seq IS NULL) sort first under both backends' NULLS-FIRST default for ASC,
        which is what verify() expects -- they precede the chain in real time too.
        """
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            stmt = select(AuditEvent).where(AuditEvent.tenant_id == resolved).order_by(AuditEvent.seq.asc().nullsfirst())
            if limit:
                stmt = stmt.limit(limit)
            rows = session.scalars(stmt).all()
            report = verify_audit_chain_rows(self._audit_event_to_dict(row) for row in rows)
            report["tenant_id"] = resolved
            report["total"] = len(rows)
            return report

    def get_history(self, entity_type: str, entity_id: str, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(
                select(EntityHistory)
                .where(EntityHistory.tenant_id == resolved, EntityHistory.entity_type == entity_type, EntityHistory.entity_id == entity_id)
                .order_by(desc(EntityHistory.version))
            ).all()
            return [
                {
                    "id": row.id,
                    "entity_type": row.entity_type,
                    "entity_id": row.entity_id,
                    "version": row.version,
                    "snapshot": copy.deepcopy(row.snapshot or {}),
                    "created_at": serialize_datetime(row.created_at),
                }
                for row in rows
            ]

    def tenant_pii_redact_keys(self, tenant_id: Optional[str] = None) -> List[str]:
        """A workspace's custom PII field names to redact from stored payloads (managed in the
        Data Protection settings, stored under engine_config.pii_redact_keys). Cached briefly so
        the decision-log hot path doesn't read settings on every decision."""
        resolved = self._tenant_id(tenant_id)
        cache: Optional[Dict[str, Tuple[List[str], float]]] = getattr(self, "_pii_keys_cache", None)
        if cache is None:
            fresh: Dict[str, Tuple[List[str], float]] = {}
            cache = self._pii_keys_cache = fresh
        entry = cache.get(resolved)
        now = _time.time()
        if entry and entry[1] > now:
            return entry[0]
        raw = (self.get_settings(tenant_id=resolved).get("engine_config", {}) or {}).get("pii_redact_keys", []) or []
        keys = [str(k) for k in raw if str(k).strip()]
        cache[resolved] = (keys, now + 60)
        return keys

    def get_settings(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(select(Setting).where(Setting.tenant_id == resolved))
            if not row:
                defaults = copy.deepcopy(DEFAULT_SETTINGS)
                # DEFAULT_SETTINGS predates months; keep the key present so callers never have to
                # distinguish "no settings row" from "months not configured".
                defaults.setdefault("audit_retention_months", None)
                return defaults
            return {
                "api_base_url": row.api_base_url,
                "auth_config": copy.deepcopy(row.auth_config or {}),
                "engine_config": copy.deepcopy(row.engine_config or {}),
                "source_defaults": copy.deepcopy(row.source_defaults or {}),
                # Both units are returned RAW. Resolving which one applies is
                # archiver.retention_cutoff's job, and deriving days here would make
                # update_settings' read-modify-write silently overwrite the stored value.
                "audit_retention_days": row.audit_retention_days,
                "audit_retention_months": row.audit_retention_months,
                "theme_mode": row.theme_mode,
                "branding": copy.deepcopy(row.branding or {}),
                "updated_at": serialize_datetime(row.updated_at),
            }

    def update_settings(self, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        getattr(self, "_pii_keys_cache", {}).pop(resolved, None)  # settings changed -> drop cached PII keys
        current = self.get_settings(tenant_id=resolved)
        next_value = copy.deepcopy(current)
        next_value.update({key: value for key, value in patch.items() if value is not None})
        with self.connect() as session:
            row = session.scalar(select(Setting).where(Setting.tenant_id == resolved))
            if not row:
                row = Setting(tenant_id=resolved, **DEFAULT_SETTINGS)
                session.add(row)
                session.flush()
            row.api_base_url = next_value["api_base_url"]
            row.auth_config = copy.deepcopy(next_value["auth_config"])
            row.engine_config = copy.deepcopy(next_value["engine_config"])
            row.source_defaults = copy.deepcopy(next_value["source_defaults"])
            # ── Retention unit precedence ──────────────────────────────────────────────────
            # A patch that names months makes calendar months authoritative (an explicit 0 or
            # negative clears them back to days); a patch that names only days reverts the
            # workspace to days, which is what the days-only endpoints and the dashboard send.
            # A patch that names neither leaves both columns exactly as stored.
            #
            # While months is set, audit_retention_days is kept as a truthful mirror rather than
            # left stale, so /api/v1/settings, workspace export and every other days-speaking
            # reader report the real window instead of whatever the tenant last typed in days.
            months_patch = patch.get("audit_retention_months")
            days_patch = patch.get("audit_retention_days")
            if months_patch is not None:
                months = normalize_retention_months(months_patch)
                row.audit_retention_months = months
                row.audit_retention_days = (
                    retention_window_days(months)
                    if months
                    else int(days_patch if days_patch is not None else next_value["audit_retention_days"])
                )
            elif days_patch is not None:
                row.audit_retention_months = None
                row.audit_retention_days = int(days_patch)
            else:
                row.audit_retention_days = int(next_value["audit_retention_days"])
            row.theme_mode = next_value["theme_mode"]
            row.branding = copy.deepcopy(next_value.get("branding") or {})
            row.updated_at = datetime.utcnow()
        return self.get_settings(tenant_id=resolved)

    # ── AI Copilot config (BYO key) ────────────────────────────────────
    def _ai_config_row(self, session: Session, tenant_id: str) -> Setting:
        self._ensure_settings(session, tenant_id)
        row = session.scalar(select(Setting).where(Setting.tenant_id == tenant_id))
        # _ensure_settings has just created it if it was missing, so None here would be a
        # real invariant break rather than an ordinary miss -- surface it instead of
        # returning Optional and pushing the check onto every caller.
        if row is None:  # pragma: no cover - _ensure_settings guarantees the row
            raise RuntimeError(f"settings row missing for tenant {tenant_id}")
        return row

    def get_ai_config_masked(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """UI-safe view — never exposes keys, only whether each provider is configured."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            cfg = (self._ai_config_row(session, resolved).ai_config or {})
        providers = cfg.get("providers", {}) or {}
        provider_view = {
            name: {"configured": bool((providers.get(name) or {}).get("key_encrypted")),
                   "model": (providers.get(name) or {}).get("model", "")}
            for name in ai_module.SUPPORTED_PROVIDERS
        }
        any_configured = any(p["configured"] for p in provider_view.values())
        # AI is "on" only when a key is present AND the admin hasn't explicitly
        # turned it off. No key -> never on (features stay hidden in the UI).
        enabled = any_configured and bool(cfg.get("enabled", True))
        return {
            "default_provider": cfg.get("default_provider", "anthropic"),
            "providers": provider_view,
            "any_configured": any_configured,
            "enabled": enabled,
        }

    def set_ai_config(self, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = self._ai_config_row(session, resolved)
            cfg = copy.deepcopy(row.ai_config or {})
            cfg.setdefault("providers", {})
            if patch.get("default_provider") in ai_module.SUPPORTED_PROVIDERS:
                cfg["default_provider"] = patch["default_provider"]
            if "enabled" in patch:  # admin can turn AI off even while a key stays stored
                cfg["enabled"] = bool(patch["enabled"])
            for name in ai_module.SUPPORTED_PROVIDERS:
                ppatch = patch.get(name) or {}
                slot = cfg["providers"].setdefault(name, {})
                if "model" in ppatch:
                    slot["model"] = ppatch["model"]
                # Not a secret -- it is an identifier, so it is stored in the clear alongside
                # the encrypted key. Required by Anthropic's identity-linked keys.
                if "workspace_id" in ppatch:
                    wid = (ppatch.get("workspace_id") or "").strip()
                    if wid:
                        slot["workspace_id"] = wid
                    else:
                        slot.pop("workspace_id", None)
                key = ppatch.get("key")
                if key == "__CLEAR__":
                    slot.pop("key_encrypted", None)
                elif key:  # a new plaintext key → encrypt at rest; empty string = leave as-is
                    slot["key_encrypted"] = encrypt_secret_text(key)
            row.ai_config = cfg
            row.updated_at = datetime.utcnow()
        return self.get_ai_config_masked(tenant_id=resolved)

    def get_ai_credentials(self, provider: Optional[str] = None, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Server-side only — decrypts the key for an outbound LLM call."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            cfg = (self._ai_config_row(session, resolved).ai_config or {})
        prov = provider or cfg.get("default_provider", "anthropic")
        slot = (cfg.get("providers", {}) or {}).get(prov) or {}
        enc = slot.get("key_encrypted")
        if not enc:
            return None
        return {"provider": prov, "api_key": decrypt_secret_text(enc), "model": slot.get("model") or None,
                "workspace_id": slot.get("workspace_id") or None}

    def replace_all(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> None:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            # Every model here carries tenant_id, but their common supertype is Base, which
            # does not -- so the attribute is checked per-model at import time instead.
            for model in [Connector, Variable, Rule, Scorecard, Policy, Promotion, EntityHistory, Decision, ErrorEvent, AuditEvent]:
                session.execute(delete(model).where(model.tenant_id == resolved))  # type: ignore[attr-defined]
        for connector in payload.get("connectors", []):
            current = copy.deepcopy(connector)
            if self.get_connector(current["id"], tenant_id=resolved):
                self.update_connector(current["id"], current, tenant_id=resolved)
            else:
                with self.connect() as session:
                    session.add(
                        Connector(
                            tenant_id=resolved,
                            public_id=current["id"],
                            name=current["name"],
                            icon=current.get("icon"),
                            color=current.get("color"),
                            description=current.get("description"),
                            schema_fields=copy.deepcopy(current.get("schema_paths", [])),
                            sample_payload=copy.deepcopy(current.get("sample_payload", {})),
                            is_active=bool(current.get("is_active", True)),
                            encrypted_config=encrypt_config_payload(copy.deepcopy(current.get("config", {}))),
                        )
                    )
        for variable in payload.get("variables", []):
            self.create_variable(variable, tenant_id=resolved)
        for rule in payload.get("rules", []):
            self.create_rule(rule, tenant_id=resolved)
        for scorecard in payload.get("scorecards", []):
            self.create_scorecard(scorecard, tenant_id=resolved)
        for policy in payload.get("policies", []):
            self.create_policy(policy, tenant_id=resolved)
        self.update_settings(payload.get("settings", DEFAULT_SETTINGS), tenant_id=resolved)

    def export_config(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        return {
            "connectors": self.list_connectors(tenant_id=resolved),
            "variables": self.list_variables(tenant_id=resolved),
            "rules": self.list_rules(tenant_id=resolved),
            "scorecards": self.list_scorecards(tenant_id=resolved),
            "policies": self.list_policies(tenant_id=resolved),
            "settings": self.get_settings(tenant_id=resolved),
            "error_events": self.list_error_events(tenant_id=resolved),
        }

    def list_tenants(self) -> List[Dict[str, Any]]:
        with self.connect() as session:
            rows = session.scalars(select(Tenant).order_by(Tenant.created_at)).all()
            return [
                {
                    "id": row.id,
                    "name": row.name,
                    "plan": row.plan,
                    "config": copy.deepcopy(row.config or {}),
                    "is_active": row.is_active,
                    "created_at": serialize_datetime(row.created_at),
                    "updated_at": serialize_datetime(row.updated_at),
                }
                for row in rows
            ]

    def create_tenant(self, name: str, plan: str = "standard", config: Optional[Dict[str, Any]] = None, is_active: bool = True) -> Dict[str, Any]:
        with self.connect() as session:
            tenant = Tenant(name=name, plan=plan, config=copy.deepcopy(config or {}), is_active=is_active)
            session.add(tenant)
            session.flush()
            self._ensure_settings(session, tenant.id)
            return {
                "id": tenant.id,
                "name": tenant.name,
                "plan": tenant.plan,
                "config": copy.deepcopy(tenant.config or {}),
                "is_active": tenant.is_active,
                "created_at": serialize_datetime(tenant.created_at),
                "updated_at": serialize_datetime(tenant.updated_at),
            }

    def get_tenant(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as session:
            tenant = session.get(Tenant, tenant_id)
            if not tenant:
                return None
            return {
                "id": tenant.id,
                "name": tenant.name,
                "plan": tenant.plan,
                "config": copy.deepcopy(tenant.config or {}),
                "is_active": tenant.is_active,
                "created_at": serialize_datetime(tenant.created_at),
                "updated_at": serialize_datetime(tenant.updated_at),
            }

    def update_tenant(self, tenant_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self.connect() as session:
            tenant = session.get(Tenant, tenant_id)
            if not tenant:
                return None
            if patch.get("name") is not None:
                tenant.name = patch["name"]
            if patch.get("plan") is not None:
                tenant.plan = patch["plan"]
            if patch.get("config") is not None:
                tenant.config = copy.deepcopy(patch["config"])
            if patch.get("is_active") is not None:
                tenant.is_active = bool(patch["is_active"])
            tenant.updated_at = datetime.utcnow()
        return self.get_tenant(tenant_id)

    def mark_bundle_compile_queued(self, tenant_id: str, debounce_seconds: int = 5, force: bool = False) -> bool:
        with self.connect() as session:
            tenant = session.get(Tenant, tenant_id)
            if not tenant:
                return False
            now = datetime.utcnow()
            if not force and tenant.last_bundle_queued_at and (now - tenant.last_bundle_queued_at).total_seconds() < debounce_seconds:
                return False
            tenant.last_bundle_queued_at = now
            tenant.updated_at = now
            return True

    def generate_api_key_for_tenant(self, tenant_id: str, environment: str = "prod", label: Optional[str] = None, role: str = "owner") -> Dict[str, Any]:
        from .rbac import normalize_role

        plaintext = generate_api_key()
        lookup_hash = key_lookup_hash(plaintext)
        env = environment if environment in ("dev", "prod", "sandbox") else "prod"
        resolved_role = normalize_role(role)
        with self.connect() as session:
            tenant = session.get(Tenant, tenant_id)
            if not tenant:
                raise ValueError("Tenant not found")
            model = ApiKey(
                tenant_id=tenant_id,
                kid=api_key_kid(plaintext),
                masked_key=mask_api_key(plaintext),
                lookup_hash=lookup_hash,
                key_hash=bcrypt_hash(plaintext),
                is_active=True,
                environment=env,
                label=label,
                role=resolved_role,
            )
            session.add(model)
            tenant.api_key_hash = lookup_hash
            session.flush()
            return {
                "id": model.id,
                "kid": model.kid,
                "masked_key": model.masked_key,
                "plaintext": plaintext,
                "environment": env,
                "label": label,
                "role": resolved_role,
                "created_at": serialize_datetime(model.created_at),
            }

    # ── Email (SMTP) config for scheduled report delivery ───────────────
    def set_email_config(self, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            if not tenant:
                raise ValueError("Tenant not found")
            cfg = copy.deepcopy(tenant.config or {})
            email = cfg.get("email") or {}
            for key in ("host", "port", "username", "from_addr", "use_tls", "use_ssl"):
                if key in patch:
                    email[key] = patch[key]
            pw = patch.get("password")
            if pw == "__CLEAR__":
                email.pop("password_encrypted", None)
            elif pw:
                email["password_encrypted"] = encrypt_secret_text(pw)
            cfg["email"] = email
            tenant.config = cfg
            session.flush()
        return self.get_email_config_masked(tenant_id=resolved)

    def get_email_config_masked(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            email = ((tenant.config or {}).get("email") or {}) if tenant else {}
        return {
            "host": email.get("host", ""),
            "port": email.get("port", 587),
            "username": email.get("username", ""),
            "from_addr": email.get("from_addr", ""),
            "use_tls": email.get("use_tls", True),
            "use_ssl": email.get("use_ssl", False),
            "password_set": bool(email.get("password_encrypted")),
            "configured": bool(email.get("host") and email.get("from_addr")),
        }

    def get_email_credentials(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Server-side only — decrypts the SMTP password for an outbound send."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            email = copy.deepcopy((tenant.config or {}).get("email") or {}) if tenant else {}
        enc = email.pop("password_encrypted", None)
        if enc:
            email["password"] = decrypt_secret_text(enc)
        return email

    # ── AI usage + cost accounting (per workspace) ───────────────────────
    def record_ai_usage(self, provider: str, model: str, input_tokens: int, output_tokens: int,
                        cost_usd: float, tenant_id: Optional[str] = None) -> None:
        """Accumulate token + estimated-cost counters for a workspace's AI usage.
        Off the critical concern of the call itself — best-effort, never raises."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            if not tenant:
                return
            cfg = copy.deepcopy(tenant.config or {})
            usage = cfg.get("ai_usage") or {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "by_model": {}}
            usage["calls"] = int(usage.get("calls", 0)) + 1
            usage["input_tokens"] = int(usage.get("input_tokens", 0)) + int(input_tokens)
            usage["output_tokens"] = int(usage.get("output_tokens", 0)) + int(output_tokens)
            usage["cost_usd"] = round(float(usage.get("cost_usd", 0.0)) + float(cost_usd), 6)
            per_model = usage.get("by_model") or {}
            key = "{0}/{1}".format(provider, model)
            row = per_model.get(key) or {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
            row["calls"] += 1
            row["input_tokens"] += int(input_tokens)
            row["output_tokens"] += int(output_tokens)
            row["cost_usd"] = round(float(row["cost_usd"]) + float(cost_usd), 6)
            per_model[key] = row
            usage["by_model"] = per_model
            usage["updated_at"] = now_iso()
            cfg["ai_usage"] = usage
            tenant.config = cfg

    def get_ai_usage(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            cfg = (tenant.config or {}) if tenant else {}
            usage = copy.deepcopy(cfg.get("ai_usage") or {})
            budget = float(((cfg.get("settings_ai") or {}).get("monthly_budget_usd", 0)) or 0)
        usage.setdefault("calls", 0)
        usage.setdefault("input_tokens", 0)
        usage.setdefault("output_tokens", 0)
        usage.setdefault("cost_usd", 0.0)
        usage.setdefault("by_model", {})
        usage["budget_usd"] = budget
        usage["over_budget"] = bool(budget) and float(usage.get("cost_usd", 0)) >= budget
        return usage

    def set_ai_budget(self, monthly_budget_usd: float, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            if not tenant:
                raise ValueError("Tenant not found")
            cfg = copy.deepcopy(tenant.config or {})
            settings_ai = cfg.get("settings_ai") or {}
            settings_ai["monthly_budget_usd"] = float(monthly_budget_usd or 0)
            cfg["settings_ai"] = settings_ai
            tenant.config = cfg
        return self.get_ai_usage(tenant_id=resolved)

    def reset_ai_usage(self, tenant_id: Optional[str] = None) -> None:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            if not tenant:
                return
            cfg = copy.deepcopy(tenant.config or {})
            cfg["ai_usage"] = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "by_model": {}, "updated_at": now_iso()}
            tenant.config = cfg

    # ── SSO (OIDC / SAML) connection config, per workspace ───────────────
    def set_sso_config(self, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Upsert the workspace's SSO connection. The OIDC client secret is stored
        Fernet-encrypted and never returned to the client."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            if not tenant:
                raise ValueError("Tenant not found")
            cfg = copy.deepcopy(tenant.config or {})
            sso = cfg.get("sso") or {}
            for key in ("provider", "enabled", "issuer", "client_id", "redirect_uri", "scope",
                        "authorization_endpoint", "token_endpoint", "jwks_uri",
                        "sp_entity_id", "sso_url", "acs_url", "idp_entity_id", "idp_cert",
                        "allowed_domains", "default_role", "jit_provisioning"):
                if key in patch:
                    sso[key] = patch[key]
            secret = patch.get("client_secret")
            if secret == "__CLEAR__":
                sso.pop("client_secret_encrypted", None)
            elif secret:
                sso["client_secret_encrypted"] = encrypt_secret_text(secret)
            cfg["sso"] = sso
            tenant.config = cfg
            session.flush()
        return self.get_sso_config_masked(tenant_id=resolved)

    def get_sso_config_masked(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            sso = copy.deepcopy((tenant.config or {}).get("sso") or {}) if tenant else {}
        has_secret = bool(sso.pop("client_secret_encrypted", None))
        sso["client_secret_set"] = has_secret
        sso.setdefault("provider", "oidc")
        sso.setdefault("enabled", False)
        sso.setdefault("default_role", "viewer")
        sso.setdefault("jit_provisioning", True)
        sso.setdefault("allowed_domains", [])
        return sso

    def get_sso_config_internal(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Server-side only — decrypts the client secret for the token exchange."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            sso = copy.deepcopy((tenant.config or {}).get("sso") or {}) if tenant else {}
        enc = sso.pop("client_secret_encrypted", None)
        if enc:
            sso["client_secret"] = decrypt_secret_text(enc)
        return sso

    def seed_sample_inventory(self, tenant_id: str) -> None:
        """Populate a new workspace with the sample connectors/variables/rules/
        scorecards/policies so an onboarding client can try a decision immediately."""
        with self.connect() as session:
            self._ensure_seed_inventory(session, tenant_id)

    # ── Onboarding state (stored on tenant.config["onboarding"]) ────────
    def get_onboarding(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            cfg = (tenant.config or {}) if tenant else {}
        return copy.deepcopy(cfg.get("onboarding") or {})

    def update_onboarding(self, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            if not tenant:
                raise ValueError("Tenant not found")
            cfg = copy.deepcopy(tenant.config or {})
            ob = cfg.get("onboarding") or {}
            ob.update(patch)
            cfg["onboarding"] = ob
            tenant.config = cfg
            session.flush()
            return copy.deepcopy(ob)

    def list_api_keys(self, tenant_id: str) -> List[Dict[str, Any]]:
        with self.connect() as session:
            rows = session.scalars(select(ApiKey).where(ApiKey.tenant_id == tenant_id).order_by(desc(ApiKey.created_at))).all()
            return [
                {
                    "id": row.id,
                    "kid": row.kid,
                    "masked_key": row.masked_key,
                    "is_active": row.is_active,
                    "environment": row.environment,
                    "label": row.label,
                    "role": row.role,
                    "revoked_at": serialize_datetime(row.revoked_at),
                    "last_used_at": serialize_datetime(row.last_used_at),
                    "created_at": serialize_datetime(row.created_at),
                }
                for row in rows
            ]

    def revoke_api_key(self, tenant_id: str, kid: str) -> bool:
        with self.connect() as session:
            row = session.scalar(select(ApiKey).where(ApiKey.tenant_id == tenant_id, ApiKey.kid == kid, ApiKey.is_active.is_(True)))
            if not row:
                return False
            row.is_active = False
            row.revoked_at = datetime.utcnow()
            # Eagerly drop any cached verification so the revoke takes effect now.
            self._api_key_cache.clear()
            return True

    def get_tenant_by_api_key(self, api_key: str) -> Optional[Dict[str, Any]]:
        # bcrypt-verifying the API key on EVERY request costs ~200ms and caps the
        # whole API at a few req/s. Verify once, then cache the resolved tenant for
        # a short TTL — subsequent requests skip both bcrypt and the DB round-trip.
        # Revocation is honoured within API_KEY_CACHE_TTL (and eagerly on revoke).
        now = _time.time()
        cached = self._api_key_cache.get(api_key)
        if cached is not None and cached[1] > now:
            return cached[0]
        lookup_hash = key_lookup_hash(api_key)
        with self.connect() as session:
            row = session.scalar(select(ApiKey).where(ApiKey.lookup_hash == lookup_hash, ApiKey.is_active.is_(True)))
            if not row:
                return None
            if not bcrypt_verify(api_key, row.key_hash):
                return None
            tenant = session.get(Tenant, row.tenant_id)
            if not tenant:
                return None
            # Throttle the last_used_at write so it isn't a DB write per request.
            last_used = row.last_used_at
            if last_used is None or (datetime.utcnow() - last_used).total_seconds() > 60:
                row.last_used_at = datetime.utcnow()
            result = {
                "tenant": {
                    "id": tenant.id,
                    "name": tenant.name,
                    "plan": tenant.plan,
                    "is_active": tenant.is_active,
                    "config": copy.deepcopy(tenant.config or {}),
                },
                "api_key": {
                    "id": row.id,
                    "kid": row.kid,
                    "masked_key": row.masked_key,
                    "role": row.role,
                },
            }
        if _API_KEY_CACHE_TTL > 0:
            self._api_key_cache[api_key] = (result, now + _API_KEY_CACHE_TTL)
        return result

    def update_api_key_role(self, tenant_id: str, kid: str, role: str) -> Optional[Dict[str, Any]]:
        """Change a key's role in place (role-change-in-place). Eagerly clears the
        verified-key cache so the new role takes effect on the next request."""
        from .rbac import normalize_role

        resolved_role = normalize_role(role)
        with self.connect() as session:
            row = session.scalar(select(ApiKey).where(ApiKey.tenant_id == tenant_id, ApiKey.kid == kid, ApiKey.is_active.is_(True)))
            if not row:
                return None
            row.role = resolved_role
            self._api_key_cache.clear()
            return {"kid": row.kid, "role": resolved_role}

    # ── Workspace members (human accounts with RBAC roles) ───────────────
    def _member_to_dict(self, row: WorkspaceMember) -> Dict[str, Any]:
        return {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "email": row.email,
            "name": row.name,
            "role": row.role,
            "auth_provider": row.auth_provider,
            # The IdP's own id for this person. Stored since the member model was written, but
            # never surfaced -- so SCIM could set it and never read it back, which breaks the
            # correlation an IdP uses to match its user to ours after an email change.
            "external_id": row.external_id,
            "is_active": row.is_active,
            "has_password": bool(row.password_hash),
            "last_login_at": serialize_datetime(row.last_login_at),
            "created_at": serialize_datetime(row.created_at),
            "updated_at": serialize_datetime(row.updated_at),
        }

    def _invalidate_member(self, member_id: str) -> None:
        """Drop cached sessions for a member so a role change / deactivation is
        honoured immediately rather than only after the TTL."""
        for token_hash in [k for k, v in self._member_session_cache.items()
                           if isinstance(v, tuple) and v[0] and v[0].get("member", {}).get("id") == member_id]:
            self._member_session_cache.pop(token_hash, None)

    def list_members(self, tenant_id: str) -> List[Dict[str, Any]]:
        with self.connect() as session:
            rows = session.scalars(
                select(WorkspaceMember).where(WorkspaceMember.tenant_id == tenant_id).order_by(WorkspaceMember.created_at)
            ).all()
            return [self._member_to_dict(row) for row in rows]

    def get_member(self, tenant_id: str, member_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as session:
            row = session.get(WorkspaceMember, member_id)
            if not row or row.tenant_id != tenant_id:
                return None
            return self._member_to_dict(row)

    def get_member_by_email(self, email: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Resolve a member by email. If tenant_id is given, scope to it; otherwise
        return the single match (None if the email is ambiguous across workspaces)."""
        normalized = (email or "").lower().strip()
        with self.connect() as session:
            stmt = select(WorkspaceMember).where(func.lower(WorkspaceMember.email) == normalized)
            if tenant_id:
                stmt = stmt.where(WorkspaceMember.tenant_id == tenant_id)
            rows = session.scalars(stmt).all()
            if len(rows) != 1:
                return None
            return self._member_to_dict(rows[0])

    def create_member(self, tenant_id: str, email: str, name: str, role: str,
                      password: Optional[str] = None, auth_provider: str = "password",
                      external_id: Optional[str] = None) -> Dict[str, Any]:
        from .rbac import normalize_role

        normalized = (email or "").lower().strip()
        with self.connect() as session:
            existing = session.scalar(
                select(WorkspaceMember).where(WorkspaceMember.tenant_id == tenant_id,
                                              func.lower(WorkspaceMember.email) == normalized)
            )
            if existing:
                raise ValueError("A member with this email already exists in the workspace.")
            row = WorkspaceMember(
                tenant_id=tenant_id,
                email=normalized,
                name=name or normalized,
                role=normalize_role(role),
                password_hash=bcrypt_hash(password) if password else None,
                auth_provider=auth_provider,
                external_id=external_id,
                is_active=True,
            )
            session.add(row)
            session.flush()
            return self._member_to_dict(row)

    def update_member(self, tenant_id: str, member_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        from .rbac import normalize_role

        with self.connect() as session:
            row = session.get(WorkspaceMember, member_id)
            if not row or row.tenant_id != tenant_id:
                return None
            if "name" in patch and patch["name"]:
                row.name = patch["name"]
            if "role" in patch and patch["role"]:
                row.role = normalize_role(patch["role"])
            if "is_active" in patch:
                row.is_active = bool(patch["is_active"])
            pw = patch.get("password")
            if pw == "__CLEAR__":
                row.password_hash = None
            elif pw:
                row.password_hash = bcrypt_hash(pw)
            result = self._member_to_dict(row)
        # Role/active change must take effect now, not after the session-cache TTL.
        self._invalidate_member(member_id)
        return result

    def verify_member_password(self, member_id: str, password: str) -> bool:
        with self.connect() as session:
            row = session.get(WorkspaceMember, member_id)
            if not row or not row.is_active or not row.password_hash:
                return False
            return bcrypt_verify(password, row.password_hash)

    def ensure_member(self, tenant_id: str, email: str, name: str, role: str,
                      password: Optional[str] = None) -> Dict[str, Any]:
        """Idempotent member bootstrap (used by dev seeding / tests)."""
        existing = self.get_member_by_email(email, tenant_id=tenant_id)
        if existing:
            return existing
        return self.create_member(tenant_id, email, name, role, password=password)

    # ── Member login sessions ────────────────────────────────────────────
    def create_member_session(self, tenant_id: str, member_id: str, ttl_hours: int = 12) -> Dict[str, Any]:
        token = generate_session_token()
        expires_at = datetime.utcnow() + timedelta(hours=ttl_hours)
        with self.connect() as session:
            member = session.get(WorkspaceMember, member_id)
            if member:
                member.last_login_at = datetime.utcnow()
            session.add(MemberSession(
                tenant_id=tenant_id, member_id=member_id,
                token_hash=session_token_hash(token), expires_at=expires_at,
            ))
        return {"token": token, "expires_at": serialize_datetime(expires_at)}

    def resolve_member_session(self, token: str) -> Optional[Dict[str, Any]]:
        """Resolve a bearer session token to its tenant + member + LIVE role. Cached
        for a short TTL; the role is re-read from the member so admin changes apply
        promptly (and eagerly via _invalidate_member)."""
        if not token:
            return None
        token_hash = session_token_hash(token)
        now = _time.time()
        cached = self._member_session_cache.get(token_hash)
        if cached is not None and cached[1] > now:
            return cached[0]
        with self.connect() as session:
            row = session.scalar(select(MemberSession).where(MemberSession.token_hash == token_hash))
            if not row or row.revoked or row.expires_at < datetime.utcnow():
                return None
            member = session.get(WorkspaceMember, row.member_id)
            if not member or not member.is_active:
                return None
            last_seen = row.last_seen_at
            if last_seen is None or (datetime.utcnow() - last_seen).total_seconds() > 60:
                row.last_seen_at = datetime.utcnow()
            result = {
                "tenant": {"id": member.tenant_id},
                "member": {"id": member.id, "email": member.email, "name": member.name, "role": member.role},
                "role": member.role,
                "session_id": row.id,
            }
        if _API_KEY_CACHE_TTL > 0:
            self._member_session_cache[token_hash] = (result, now + min(_API_KEY_CACHE_TTL, 60.0))
        return result

    def revoke_member_session(self, token: str) -> bool:
        token_hash = session_token_hash(token)
        with self.connect() as session:
            row = session.scalar(select(MemberSession).where(MemberSession.token_hash == token_hash))
            if not row:
                self._member_session_cache.pop(token_hash, None)
                return False
            row.revoked = True
        self._member_session_cache.pop(token_hash, None)
        return True

    # ── Email login OTP ──────────────────────────────────────────────────
    def issue_member_otp(self, tenant_id: str, email: str, ttl_minutes: int = 10) -> str:
        """Create a one-time passcode (returns the plaintext code for delivery)."""
        normalized = (email or "").lower().strip()
        code = generate_otp_code()
        with self.connect() as session:
            session.add(MemberOtp(
                tenant_id=tenant_id, email=normalized,
                code_hash=otp_code_hash(tenant_id, normalized, code),
                expires_at=datetime.utcnow() + timedelta(minutes=ttl_minutes),
            ))
        return code

    def verify_member_otp(self, tenant_id: str, email: str, code: str, max_attempts: int = 5) -> bool:
        normalized = (email or "").lower().strip()
        target = otp_code_hash(tenant_id, normalized, code)
        with self.connect() as session:
            row = session.scalar(
                select(MemberOtp).where(
                    MemberOtp.tenant_id == tenant_id, MemberOtp.email == normalized,
                    MemberOtp.consumed.is_(False),
                ).order_by(desc(MemberOtp.created_at))
            )
            if not row or row.expires_at < datetime.utcnow() or row.attempts >= max_attempts:
                return False
            row.attempts += 1
            if not hmac.compare_digest(row.code_hash, target):
                return False
            row.consumed = True
            return True

    def get_platform_admin_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        with self.connect() as session:
            row = session.scalar(select(PlatformAdminUser).where(PlatformAdminUser.email == email))
            if not row:
                return None
            return {
                "id": row.id,
                "email": row.email,
                "name": row.name,
                "password_hash": row.password_hash,
                "is_active": row.is_active,
                "created_at": serialize_datetime(row.created_at),
                "updated_at": serialize_datetime(row.updated_at),
            }

    def get_platform_admin_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as session:
            row = session.get(PlatformAdminUser, user_id)
            if not row:
                return None
            return {
                "id": row.id,
                "email": row.email,
                "name": row.name,
                "is_active": row.is_active,
                "created_at": serialize_datetime(row.created_at),
                "updated_at": serialize_datetime(row.updated_at),
            }

    def create_platform_admin_user(self, email: str, password: str, name: str) -> Dict[str, Any]:
        with self.connect() as session:
            existing = session.scalar(select(PlatformAdminUser).where(PlatformAdminUser.email == email))
            if existing:
                raise ValueError("Admin user already exists")
            model = PlatformAdminUser(email=email, name=name, password_hash=bcrypt_hash(password), is_active=True)
            session.add(model)
            session.flush()
            return {
                "id": model.id,
                "email": model.email,
                "name": model.name,
                "is_active": model.is_active,
                "created_at": serialize_datetime(model.created_at),
                "updated_at": serialize_datetime(model.updated_at),
            }

    def get_platform_admin_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        with self.connect() as session:
            row = session.scalar(select(PlatformAdminUser).where(PlatformAdminUser.email == email))
            if not row:
                return None
            return {
                "id": row.id,
                "email": row.email,
                "name": row.name,
                "is_active": row.is_active,
                "created_at": serialize_datetime(row.created_at),
                "updated_at": serialize_datetime(row.updated_at),
            }

    def create_or_update_experiment(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id, payload_tenant=payload.get("tenant_id"))
        public_id = payload.get("id")
        with self.connect() as session:
            model = self._query_by_public_id(session, Experiment, public_id, resolved) if public_id else None
            if not model:
                model = Experiment(
                    tenant_id=resolved,
                    public_id=public_id,
                    name=payload["name"],
                    description=payload.get("description"),
                    status=payload.get("status", "draft"),
                    variants=copy.deepcopy(payload.get("variants", [])),
                    hash_key=payload.get("hash_key", "user_id"),
                    target_policy_id=payload.get("target_policy_id"),
                    start_date=payload.get("start_date"),
                    end_date=payload.get("end_date"),
                )
                session.add(model)
            else:
                model.name = payload.get("name", model.name)
                model.description = payload.get("description", model.description)
                model.status = payload.get("status", model.status)
                model.variants = copy.deepcopy(payload.get("variants", model.variants))
                model.hash_key = payload.get("hash_key", model.hash_key)
                model.target_policy_id = payload.get("target_policy_id", model.target_policy_id)
                model.start_date = payload.get("start_date", model.start_date)
                model.end_date = payload.get("end_date", model.end_date)
                model.updated_at = datetime.utcnow()
            session.flush()
            return self._experiment_to_dict(model)

    def list_experiments(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(Experiment).where(Experiment.tenant_id == resolved).order_by(Experiment.created_at)).all()
            return [self._experiment_to_dict(row) for row in rows]

    def get_experiment(self, experiment_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = self._query_by_public_id(session, Experiment, experiment_id, resolved)
            return self._experiment_to_dict(row) if row else None

    @staticmethod
    def _experiment_to_dict(model: Experiment) -> Dict[str, Any]:
        return {
            "id": model.public_id,
            "name": model.name,
            "description": model.description,
            "status": model.status,
            "variants": copy.deepcopy(model.variants or []),
            "hash_key": model.hash_key,
            "target_policy_id": model.target_policy_id,
            "start_date": serialize_datetime(model.start_date),
            "end_date": serialize_datetime(model.end_date),
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    def delete_experiment(self, experiment_id: str, tenant_id: Optional[str] = None) -> bool:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = self._query_by_public_id(session, Experiment, experiment_id, resolved)
            if not row:
                return False
            session.delete(row)
            return True

    def save_bundle(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id, payload_tenant=payload.get("tenant_id"))
        with self.connect() as session:
            current = session.scalars(select(Bundle).where(Bundle.tenant_id == resolved, Bundle.superseded.is_(False))).all()
            for row in current:
                row.superseded = True
            bundle = Bundle(
                tenant_id=resolved,
                version=int(payload["version"]),
                content=copy.deepcopy(payload.get("content", {})),
                encrypted_content=payload["encrypted_content"],
                encrypted_key=payload.get("encrypted_key"),
                signature=payload["signature"],
                checksum=payload["checksum"],
                superseded=False,
                compiled_at=payload.get("compiled_at", datetime.utcnow()),
                expires_at=payload.get("expires_at", datetime.utcnow() + timedelta(days=30)),
            )
            session.add(bundle)
            session.flush()
            return self._bundle_to_dict(bundle)

    def latest_bundle(self, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(select(Bundle).where(Bundle.tenant_id == resolved, Bundle.superseded.is_(False)).order_by(desc(Bundle.version)).limit(1))
            return self._bundle_to_dict(row) if row else None

    def list_bundles(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(Bundle).where(Bundle.tenant_id == resolved).order_by(desc(Bundle.version))).all()
            return [self._bundle_to_dict(row) for row in rows]

    def get_bundle(self, version: int, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetch a specific (retained, possibly superseded) bundle version — backs decision replay
        against a historical policy version."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(
                select(Bundle).where(Bundle.tenant_id == resolved, Bundle.version == int(version)).limit(1)
            )
            return self._bundle_to_dict(row) if row else None

    def add_sdk_events(self, events: List[Dict[str, Any]], tenant_id: Optional[str] = None) -> int:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            for event in events:
                session.add(
                    SdkEvent(
                        tenant_id=resolved,
                        event_type=event.get("type", "unknown"),
                        payload=copy.deepcopy(event),
                        created_at=datetime.utcnow(),
                    )
                )
            return len(events)

    def list_sdk_events(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(SdkEvent).where(SdkEvent.tenant_id == resolved).order_by(desc(SdkEvent.created_at))).all()
            return [
                {
                    "id": row.id,
                    "tenant_id": row.tenant_id,
                    "type": row.event_type,
                    "payload": copy.deepcopy(row.payload or {}),
                    "created_at": serialize_datetime(row.created_at),
                }
                for row in rows
            ]

    def create_workflow_execution(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id, payload_tenant=payload.get("tenant_id"))
        with self.connect() as session:
            model = WorkflowExecution(
                id=payload.get("id") or uuid4_str(),
                tenant_id=resolved,
                policy_id=payload["policy_id"],
                status=payload.get("status", "running"),
                context=copy.deepcopy(payload.get("context", {})),
                current_step_index=int(payload.get("current_step_index", 0)),
                trigger_type=payload.get("trigger_type"),
                trigger_metadata=copy.deepcopy(payload.get("trigger_metadata")),
                started_at=payload.get("started_at", datetime.utcnow()),
                paused_at=payload.get("paused_at"),
                completed_at=payload.get("completed_at"),
            )
            session.add(model)
            session.flush()
            return self._workflow_execution_to_dict(model)

    def update_workflow_execution(self, execution_id: str, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(WorkflowExecution, execution_id)
            if not model or model.tenant_id != resolved:
                return None
            for field in ["status", "context", "current_step_index", "trigger_type", "trigger_metadata", "paused_at", "completed_at"]:
                if field in patch and patch[field] is not None:
                    setattr(model, field, copy.deepcopy(patch[field]) if isinstance(patch[field], (dict, list)) else patch[field])
            model.updated_at = datetime.utcnow()
            return self._workflow_execution_to_dict(model)

    def get_workflow_execution(self, execution_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(WorkflowExecution, execution_id)
            if not model or model.tenant_id != resolved:
                return None
            return self._workflow_execution_to_dict(model)

    @staticmethod
    def _workflow_execution_to_dict(model: WorkflowExecution) -> Dict[str, Any]:
        return {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "policy_id": model.policy_id,
            "status": model.status,
            "context": copy.deepcopy(model.context or {}),
            "current_step_index": model.current_step_index,
            "trigger_type": model.trigger_type,
            "trigger_metadata": copy.deepcopy(model.trigger_metadata or {}),
            "started_at": serialize_datetime(model.started_at),
            "paused_at": serialize_datetime(model.paused_at),
            "completed_at": serialize_datetime(model.completed_at),
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    def create_review_task(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id, payload_tenant=payload.get("tenant_id"))
        with self.connect() as session:
            model = ReviewTask(
                id=payload.get("id") or uuid4_str(),
                tenant_id=resolved,
                execution_id=payload["execution_id"],
                policy_id=payload.get("policy_id"),
                step_id=payload.get("step_id"),
                queue=payload.get("queue", "default"),
                status=payload.get("status", "pending"),
                context_snapshot=copy.deepcopy(payload.get("context_snapshot", {})),
                required_fields=copy.deepcopy(payload.get("required_fields", [])),
                reviewer_response=copy.deepcopy(payload.get("reviewer_response")),
                reviewed_by=payload.get("reviewed_by"),
                assigned_at=payload.get("assigned_at", datetime.utcnow()),
                timeout_at=payload.get("timeout_at"),
                reviewed_at=payload.get("reviewed_at"),
            )
            session.add(model)
            session.flush()
            return self._review_task_to_dict(model)

    def update_review_task(self, task_id: str, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(ReviewTask, task_id)
            if not model or model.tenant_id != resolved:
                return None
            for field in ["status", "context_snapshot", "required_fields", "reviewer_response", "reviewed_by", "timeout_at", "reviewed_at"]:
                if field in patch and patch[field] is not None:
                    setattr(model, field, copy.deepcopy(patch[field]) if isinstance(patch[field], (dict, list)) else patch[field])
            return self._review_task_to_dict(model)

    def get_review_task(self, task_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(ReviewTask, task_id)
            if not model or model.tenant_id != resolved:
                return None
            return self._review_task_to_dict(model)

    def list_review_tasks(self, queue: Optional[str] = None, status: Optional[str] = None, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            stmt = select(ReviewTask).where(ReviewTask.tenant_id == resolved)
            if queue:
                stmt = stmt.where(ReviewTask.queue == queue)
            if status:
                stmt = stmt.where(ReviewTask.status == status)
            rows = session.scalars(stmt.order_by(desc(ReviewTask.assigned_at))).all()
            return [self._review_task_to_dict(row) for row in rows]

    def get_expired_review_tasks(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.connect() as session:
            stmt = select(ReviewTask).where(
                ReviewTask.status == "pending",
                ReviewTask.timeout_at.is_not(None),
                ReviewTask.timeout_at < datetime.utcnow(),
            )
            if tenant_id is not None:
                stmt = stmt.where(ReviewTask.tenant_id == self._tenant_id(tenant_id))
            rows = session.scalars(stmt).all()
            return [self._review_task_to_dict(row) for row in rows]

    @staticmethod
    def _review_task_to_dict(model: ReviewTask) -> Dict[str, Any]:
        snapshot = copy.deepcopy(model.context_snapshot or {})
        routing = snapshot.get("routing", {}) if isinstance(snapshot, dict) else {}
        sla_at = routing.get("sla_at")
        sla_breached = False
        if sla_at and model.status == "pending":
            try:
                deadline = datetime.fromisoformat(str(sla_at).replace("Z", "+00:00")).replace(tzinfo=None)
                sla_breached = datetime.utcnow() > deadline
            except ValueError:
                sla_breached = False
        return {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "execution_id": model.execution_id,
            "policy_id": model.policy_id,
            "step_id": model.step_id,
            "queue": model.queue,
            "role": routing.get("role"),
            "priority": routing.get("priority", "normal"),
            "sla_at": sla_at,
            "sla_breached": sla_breached,
            "status": model.status,
            "context_snapshot": snapshot,
            "required_fields": copy.deepcopy(model.required_fields or []),
            "reviewer_response": copy.deepcopy(model.reviewer_response),
            "reviewed_by": model.reviewed_by,
            "assigned_at": serialize_datetime(model.assigned_at),
            "timeout_at": serialize_datetime(model.timeout_at),
            "reviewed_at": serialize_datetime(model.reviewed_at),
        }

    def create_webhook(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id, payload_tenant=payload.get("tenant_id"))
        with self.connect() as session:
            model = Webhook(
                id=payload.get("id") or uuid4_str(),
                tenant_id=resolved,
                policy_id=payload["policy_id"],
                endpoint_path=payload["endpoint_path"],
                is_active=bool(payload.get("is_active", True)),
                secret_hash=encrypt_secret_text(payload["secret_hash"]) if payload.get("secret_hash") else None,
                payload_mapping=copy.deepcopy(payload.get("payload_mapping")),
            )
            session.add(model)
            session.flush()
            return self._webhook_to_dict(model)

    def update_webhook(self, webhook_id: str, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(Webhook, webhook_id)
            if not model or model.tenant_id != resolved:
                return None
            for field in ["policy_id", "endpoint_path", "is_active", "secret_hash", "payload_mapping"]:
                if field in patch and patch[field] is not None:
                    value = copy.deepcopy(patch[field]) if isinstance(patch[field], (dict, list)) else patch[field]
                    if field == "secret_hash":
                        value = encrypt_secret_text(str(value))
                    setattr(model, field, value)
            model.updated_at = datetime.utcnow()
            return self._webhook_to_dict(model)

    def get_webhook(self, webhook_id: str, tenant_id: Optional[str] = None, include_secret: bool = False) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(Webhook, webhook_id)
            if not model or model.tenant_id != resolved:
                return None
            return self._webhook_to_dict(model, include_secret=include_secret)

    def get_webhook_for_trigger(self, webhook_id: str, include_secret: bool = False) -> Optional[Dict[str, Any]]:
        """Look a webhook up by id ALONE, for the unauthenticated receiver.

        The receiver at POST /api/v1/webhooks/{id} is called by a third party that holds no API
        key -- the HMAC signature is the authentication. So there is no auth context, and
        get_webhook() (which is tenant-scoped and falls back to the FIRST tenant) returned None
        for every tenant but the first: webhooks were simply broken for tenant #2 onward, and the
        symptom was a 404 "Webhook not found" with nothing to indicate why.

        Safe to be tenant-agnostic because the id is a UUID4 -- unguessable, and the signature is
        still verified afterwards. The returned row carries its own tenant_id, which is what the
        caller must use for everything downstream; that is the authoritative source here, not any
        ambient context.
        """
        with self.connect() as session:
            model = session.get(Webhook, webhook_id)
            return self._webhook_to_dict(model, include_secret=include_secret) if model else None

    def get_webhook_by_path(self, endpoint_path: str, include_secret: bool = False) -> Optional[Dict[str, Any]]:
        with self.connect() as session:
            model = session.scalar(select(Webhook).where(Webhook.endpoint_path == endpoint_path, Webhook.is_active.is_(True)))
            return self._webhook_to_dict(model, include_secret=include_secret) if model else None

    def list_webhooks(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(Webhook).where(Webhook.tenant_id == resolved).order_by(desc(Webhook.created_at))).all()
            return [self._webhook_to_dict(row) for row in rows]

    @staticmethod
    def _webhook_to_dict(model: Webhook, include_secret: bool = False) -> Dict[str, Any]:
        return {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "policy_id": model.policy_id,
            "endpoint_path": model.endpoint_path,
            "is_active": model.is_active,
            "secret_hash": decrypt_secret_text(model.secret_hash) if include_secret else ("••••••••" if model.secret_hash else None),
            "payload_mapping": copy.deepcopy(model.payload_mapping or {}),
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    # ── model sets (P05) ──────────────────────────────────────────────────
    def create_model_set(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id, payload_tenant=payload.get("tenant_id"))
        with self.connect() as session:
            model = ModelSet(
                id=payload.get("id") or uuid4_str(),
                tenant_id=resolved,
                public_id=payload.get("public_id") or payload.get("id") or uuid4_str(),
                name=payload["name"],
                description=payload.get("description"),
                members=copy.deepcopy(payload.get("members") or []),
                is_active=bool(payload.get("is_active", True)),
                status=payload.get("status", "dev"),
            )
            session.add(model)
            session.flush()
            return self._model_set_to_dict(model)

    def update_model_set(self, set_id: str, patch: Dict[str, Any],
                         tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.scalar(select(ModelSet).where(
                ModelSet.tenant_id == resolved, ModelSet.public_id == set_id))
            if not model:
                return None
            for field in ("name", "description", "members", "is_active", "status"):
                if field in patch:
                    setattr(model, field, copy.deepcopy(patch[field]))
            session.flush()
            return self._model_set_to_dict(model)

    def delete_model_set(self, set_id: str, tenant_id: Optional[str] = None) -> bool:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.scalar(select(ModelSet).where(
                ModelSet.tenant_id == resolved, ModelSet.public_id == set_id))
            if not model:
                return False
            session.delete(model)
            return True

    def get_model_set(self, set_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.scalar(select(ModelSet).where(
                ModelSet.tenant_id == resolved, ModelSet.public_id == set_id))
            return self._model_set_to_dict(model) if model else None

    def list_model_sets(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(
                select(ModelSet).where(ModelSet.tenant_id == resolved)
                .order_by(desc(ModelSet.created_at))).all()
            return [self._model_set_to_dict(row) for row in rows]

    @staticmethod
    def _model_set_to_dict(model: "ModelSet") -> Dict[str, Any]:
        return {
            "id": model.public_id,
            "row_id": model.id,
            "tenant_id": model.tenant_id,
            "name": model.name,
            "description": model.description,
            "members": copy.deepcopy(model.members or []),
            "is_active": model.is_active,
            "status": model.status,
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    # ── journeys (P15) ────────────────────────────────────────────────────
    def create_journey(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id, payload_tenant=payload.get("tenant_id"))
        with self.connect() as session:
            model = Journey(
                id=uuid4_str(),
                tenant_id=resolved,
                public_id=payload.get("public_id") or payload.get("id") or uuid4_str(),
                title=payload["title"],
                category=payload.get("category"),
                description=payload.get("description"),
                policy_id=payload.get("policy_id"),
                primary_color=payload.get("primary_color"),
                screens=copy.deepcopy(payload.get("screens") or []),
                is_locked=bool(payload.get("is_locked", False)),
                is_active=bool(payload.get("is_active", True)),
                status=payload.get("status", "dev"),
            )
            session.add(model)
            session.flush()
            result = self._journey_to_dict(model)
        self._audit_entity_change("journey", result["id"], "created", resolved)
        return result

    def update_journey(self, journey_id: str, patch: Dict[str, Any],
                       tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.scalar(select(Journey).where(
                Journey.tenant_id == resolved, Journey.public_id == journey_id))
            if not model:
                return None
            content_changed = False
            for field in ("title", "category", "description", "policy_id", "primary_color",
                          "screens", "is_locked", "is_active", "status"):
                if field in patch:
                    setattr(model, field, copy.deepcopy(patch[field]))
                    # The version tracks the CONTENT people fill in, so lock/unlock and the
                    # dev->prod lifecycle do not bump it. A version that moves when nothing a
                    # respondent sees has changed makes "which version did they answer?"
                    # unanswerable, which is the only question the column exists for.
                    if field in ("title", "category", "description", "policy_id",
                                 "primary_color", "screens"):
                        content_changed = True
            if content_changed:
                model.version = int(model.version or 1) + 1
            session.flush()
            result = self._journey_to_dict(model)
        self._audit_entity_change("journey", journey_id, "updated", resolved,
                                  version=result.get("version"))
        return result

    def delete_journey(self, journey_id: str, tenant_id: Optional[str] = None) -> bool:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.scalar(select(Journey).where(
                Journey.tenant_id == resolved, Journey.public_id == journey_id))
            if not model:
                return False
            session.delete(model)
        self._audit_entity_change("journey", journey_id, "deleted", resolved)
        return True

    def get_journey(self, journey_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.scalar(select(Journey).where(
                Journey.tenant_id == resolved, Journey.public_id == journey_id))
            return self._journey_to_dict(model) if model else None

    def list_journeys(self, tenant_id: Optional[str] = None,
                      active_only: bool = False) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            query = select(Journey).where(Journey.tenant_id == resolved)
            if active_only:
                query = query.where(Journey.is_active.is_(True))
            rows = session.scalars(query.order_by(desc(Journey.created_at))).all()
            return [self._journey_to_dict(row) for row in rows]

    @staticmethod
    def _journey_to_dict(model: "Journey") -> Dict[str, Any]:
        return {
            "id": model.public_id,
            "row_id": model.id,
            "tenant_id": model.tenant_id,
            "title": model.title,
            "category": model.category,
            "description": model.description,
            "policy_id": model.policy_id,
            "primary_color": model.primary_color,
            "screens": copy.deepcopy(model.screens or []),
            # Kept in the payload because the mobile studio's journey cards render it, and
            # deriving it client-side from `screens` would drift the moment a screen type stops
            # counting as a step.
            "step_count": len(model.screens or []),
            "is_locked": model.is_locked,
            "is_active": model.is_active,
            "status": model.status,
            "version": model.version,
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    # ── predictors (P20) ──────────────────────────────────────────────────
    def create_predictor(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id, payload_tenant=payload.get("tenant_id"))
        with self.connect() as session:
            model = Predictor(
                id=payload.get("id") or uuid4_str(),
                tenant_id=resolved,
                name=payload["name"],
                description=payload.get("description"),
                source_type=payload["source_type"],
                source_id=payload.get("source_id"),
                is_active=bool(payload.get("is_active", True)),
                baseline_days=int(payload.get("baseline_days", 30)),
                current_days=int(payload.get("current_days", 7)),
                bins=int(payload.get("bins", 10)),
            )
            session.add(model)
            session.flush()
            return self._predictor_to_dict(model)

    def update_predictor(self, predictor_id: str, patch: Dict[str, Any],
                         tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(Predictor, predictor_id)
            if not model or model.tenant_id != resolved:
                return None
            for field in ("name", "description", "source_type", "source_id", "is_active",
                          "baseline_days", "current_days", "bins"):
                if field in patch:
                    setattr(model, field, patch[field])
            session.flush()
            return self._predictor_to_dict(model)

    def delete_predictor(self, predictor_id: str, tenant_id: Optional[str] = None) -> bool:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(Predictor, predictor_id)
            if not model or model.tenant_id != resolved:
                return False
            # The observations go with it. Orphaned values would keep a deleted predictor's rows
            # growing forever with nothing able to read them.
            session.query(PredictorValue).filter(
                PredictorValue.tenant_id == resolved,
                PredictorValue.predictor_id == predictor_id).delete(synchronize_session=False)
            session.delete(model)
            return True

    def get_predictor(self, predictor_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(Predictor, predictor_id)
            if not model or model.tenant_id != resolved:
                return None
            return self._predictor_to_dict(model)

    def list_predictors(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(
                select(Predictor).where(Predictor.tenant_id == resolved)
                .order_by(desc(Predictor.created_at))).all()
            return [self._predictor_to_dict(row) for row in rows]

    def active_predictors(self, tenant_id: str) -> List[Dict[str, Any]]:
        """Read on EVERY decision, so it is a narrow query and nothing else."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(
                select(Predictor).where(Predictor.tenant_id == resolved,
                                        Predictor.is_active.is_(True))).all()
            return [self._predictor_to_dict(row) for row in rows]

    def record_predictor_values(self, tenant_id: str, rows: List[Dict[str, Any]]) -> int:
        """Bulk-insert observations. One statement for all predictors of one decision, because
        this is on the decision path and N predictors must not mean N round trips."""
        if not rows:
            return 0
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            session.add_all([
                PredictorValue(
                    tenant_id=resolved, predictor_id=row["predictor_id"],
                    decision_id=row.get("decision_id"), value=float(row["value"]),
                    # `created_at` is normally the column default (now), because on the decision
                    # path the observation IS being made now. A BACKFILL must override it with
                    # the decision's own timestamp: stamping a 20-day-old decision as today puts
                    # the entire replayed history inside the current window, which does not just
                    # fail to build a baseline -- it builds a WRONG one that looks populated, and
                    # the drift report then compares today against today and reports no drift.
                    **({"created_at": row["created_at"]} if row.get("created_at") else {}))
                for row in rows])
            return len(rows)

    def predictor_covered_decision_ids(self, tenant_id: str, predictor_id: str) -> set:
        """Decision ids this predictor already has a value for.

        There is no unique constraint on (predictor_id, decision_id) -- the decision path writes
        each decision exactly once, so it never needed one -- which means a backfill run twice
        would DOUBLE the series and halve the apparent drift. This is how the second run knows
        what to skip. Deliberately not a new constraint: adding one now would have to cope with
        the NULL decision_ids the observe path can legitimately write.
        """
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            return {row for row in session.scalars(
                select(PredictorValue.decision_id).where(
                    PredictorValue.tenant_id == resolved,
                    PredictorValue.predictor_id == predictor_id,
                    PredictorValue.decision_id.is_not(None))).all()}

    def predictor_values_between(self, tenant_id: str, predictor_id: str,
                                 start: datetime, end: datetime) -> List[float]:
        """The values in a window, as bare floats -- the only thing the drift maths needs, and
        materialising dicts for a 30-day window would be a lot of objects for nothing."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            return [float(v) for v in session.scalars(
                select(PredictorValue.value).where(
                    PredictorValue.tenant_id == resolved,
                    PredictorValue.predictor_id == predictor_id,
                    PredictorValue.created_at >= start,
                    PredictorValue.created_at < end)).all()]

    @staticmethod
    def _predictor_to_dict(model: "Predictor") -> Dict[str, Any]:
        return {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "name": model.name,
            "description": model.description,
            "source_type": model.source_type,
            "source_id": model.source_id,
            "is_active": model.is_active,
            "baseline_days": model.baseline_days,
            "current_days": model.current_days,
            "bins": model.bins,
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    # ── outbound webhook subscriptions (P03) ──────────────────────────────
    def create_webhook_subscription(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id, payload_tenant=payload.get("tenant_id"))
        with self.connect() as session:
            model = WebhookSubscription(
                id=payload.get("id") or uuid4_str(),
                tenant_id=resolved,
                name=payload["name"],
                url=payload["url"],
                event_types=copy.deepcopy(payload.get("event_types") or []),
                filters=copy.deepcopy(payload.get("filters") or []),
                secret=encrypt_secret_text(payload["secret"]) if payload.get("secret") else None,
                is_active=bool(payload.get("is_active", True)),
                timeout_ms=payload.get("timeout_ms"),
                retries=payload.get("retries"),
            )
            session.add(model)
            session.flush()
            return self._webhook_subscription_to_dict(model)

    def update_webhook_subscription(self, subscription_id: str, patch: Dict[str, Any],
                                    tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(WebhookSubscription, subscription_id)
            # The tenant check is the whole isolation boundary for this entity: session.get is
            # keyed by primary key alone, so without it any workspace could patch any other's
            # subscription by id -- including pointing its URL somewhere else.
            if not model or model.tenant_id != resolved:
                return None
            for field in ("name", "url", "event_types", "filters", "is_active", "timeout_ms", "retries"):
                if field in patch:
                    setattr(model, field, copy.deepcopy(patch[field]))
            # `secret` is deliberately NOT patchable here. Rotating it is a separate, explicit
            # action -- silently accepting one in a general PATCH is how a secret arrives from a
            # client that already had it in plaintext.
            session.flush()
            return self._webhook_subscription_to_dict(model)

    def delete_webhook_subscription(self, subscription_id: str, tenant_id: Optional[str] = None) -> bool:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(WebhookSubscription, subscription_id)
            if not model or model.tenant_id != resolved:
                return False
            session.delete(model)
            return True

    def get_webhook_subscription(self, subscription_id: str, tenant_id: Optional[str] = None,
                                 include_secret: bool = False) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(WebhookSubscription, subscription_id)
            if not model or model.tenant_id != resolved:
                return None
            return self._webhook_subscription_to_dict(model, include_secret=include_secret)

    def list_webhook_subscriptions(self, tenant_id: Optional[str] = None,
                                   include_secret: bool = False) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(
                select(WebhookSubscription)
                .where(WebhookSubscription.tenant_id == resolved)
                .order_by(desc(WebhookSubscription.created_at))).all()
            return [self._webhook_subscription_to_dict(r, include_secret=include_secret) for r in rows]

    def subscriptions_for_event(self, tenant_id: str, event_type: str) -> List[Dict[str, Any]]:
        """Active subscriptions in this tenant that name this event type.

        Returned WITH secrets, because the only caller is the dispatcher and it has to sign. The
        event-type filter is applied in SQL rather than in Python so a workspace with many
        subscriptions does not decrypt every secret on every event.
        """
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(
                select(WebhookSubscription).where(
                    WebhookSubscription.tenant_id == resolved,
                    WebhookSubscription.is_active.is_(True))).all()
            out = []
            for row in rows:
                if event_type in (row.event_types or []):
                    out.append(self._webhook_subscription_to_dict(row, include_secret=True))
            return out

    def record_webhook_delivery(self, subscription_id: str, ok: bool, error: Optional[str] = None,
                                tenant_id: Optional[str] = None) -> None:
        """Last-known delivery state, so a subscriber that has been silently failing is visible
        without trawling logs. Not a delivery log -- that would grow with event volume."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(WebhookSubscription, subscription_id)
            if not model or model.tenant_id != resolved:
                return
            model.last_delivery_at = datetime.utcnow()
            model.last_delivery_ok = bool(ok)
            model.last_delivery_error = None if ok else (error or "")[:1000]
            model.consecutive_failures = 0 if ok else int(model.consecutive_failures or 0) + 1

    @staticmethod
    def _webhook_subscription_to_dict(model: "WebhookSubscription", include_secret: bool = False) -> Dict[str, Any]:
        return {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "name": model.name,
            "url": model.url,
            "event_types": copy.deepcopy(model.event_types or []),
            "filters": copy.deepcopy(model.filters or []),
            # Same contract as an API key: the plaintext is returned exactly once, at creation.
            "secret": decrypt_secret_text(model.secret) if include_secret else ("••••••••" if model.secret else None),
            "is_active": model.is_active,
            "timeout_ms": model.timeout_ms,
            "retries": model.retries,
            "last_delivery_at": serialize_datetime(model.last_delivery_at),
            "last_delivery_ok": model.last_delivery_ok,
            "last_delivery_error": model.last_delivery_error,
            "consecutive_failures": model.consecutive_failures,
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    def create_schedule(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id, payload_tenant=payload.get("tenant_id"))
        with self.connect() as session:
            model = CronSchedule(
                id=payload.get("id") or uuid4_str(),
                tenant_id=resolved,
                policy_id=payload["policy_id"],
                cron_expression=payload["cron_expression"],
                is_active=bool(payload.get("is_active", True)),
                last_run_at=payload.get("last_run_at"),
                next_run_at=payload.get("next_run_at"),
                payload_source=copy.deepcopy(payload.get("payload_source")),
                config=copy.deepcopy(payload.get("config")),
            )
            session.add(model)
            session.flush()
            return self._schedule_to_dict(model)

    def update_schedule(self, schedule_id: str, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(CronSchedule, schedule_id)
            if not model or model.tenant_id != resolved:
                return None
            for field in ["policy_id", "cron_expression", "is_active", "last_run_at", "next_run_at", "payload_source", "config"]:
                if field in patch and patch[field] is not None:
                    setattr(model, field, copy.deepcopy(patch[field]) if isinstance(patch[field], (dict, list)) else patch[field])
            model.updated_at = datetime.utcnow()
            return self._schedule_to_dict(model)

    def get_schedule(self, schedule_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(CronSchedule, schedule_id)
            if not model or model.tenant_id != resolved:
                return None
            return self._schedule_to_dict(model)

    def list_schedules(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(CronSchedule).where(CronSchedule.tenant_id == resolved).order_by(desc(CronSchedule.created_at))).all()
            return [self._schedule_to_dict(row) for row in rows]

    def list_active_schedules(self) -> List[Dict[str, Any]]:
        with self.connect() as session:
            rows = session.scalars(select(CronSchedule).where(CronSchedule.is_active.is_(True))).all()
            return [self._schedule_to_dict(row) for row in rows]

    @staticmethod
    def _schedule_to_dict(model: CronSchedule) -> Dict[str, Any]:
        return {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "policy_id": model.policy_id,
            "cron_expression": model.cron_expression,
            "is_active": model.is_active,
            "last_run_at": serialize_datetime(model.last_run_at),
            "next_run_at": serialize_datetime(model.next_run_at),
            "payload_source": copy.deepcopy(model.payload_source or {}),
            "config": copy.deepcopy(model.config or {}),
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    def add_action_log(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id, payload_tenant=payload.get("tenant_id"))
        with self.connect() as session:
            model = ActionLog(
                tenant_id=resolved,
                execution_id=payload["execution_id"],
                step_id=payload.get("step_id"),
                action_name=payload.get("action_name"),
                url=payload.get("url"),
                method=payload.get("method"),
                request_body=copy.deepcopy(payload.get("request_body")),
                response_status=payload.get("response_status"),
                response_body=payload.get("response_body"),
                latency_ms=payload.get("latency_ms"),
                success=bool(payload.get("success", False)),
                retry_count=int(payload.get("retry_count", 0)),
                error=payload.get("error"),
                created_at=datetime.utcnow(),
            )
            session.add(model)
            session.flush()
            return self._action_log_to_dict(model)

    def list_action_logs(self, execution_id: Optional[str] = None, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            stmt = select(ActionLog).where(ActionLog.tenant_id == resolved)
            if execution_id:
                stmt = stmt.where(ActionLog.execution_id == execution_id)
            rows = session.scalars(stmt.order_by(desc(ActionLog.created_at))).all()
            return [self._action_log_to_dict(row) for row in rows]

    @staticmethod
    def _action_log_to_dict(model: ActionLog) -> Dict[str, Any]:
        return {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "execution_id": model.execution_id,
            "step_id": model.step_id,
            "action_name": model.action_name,
            "url": model.url,
            "method": model.method,
            "request_body": copy.deepcopy(model.request_body or {}),
            "response_status": model.response_status,
            "response_body": model.response_body,
            "latency_ms": model.latency_ms,
            "success": model.success,
            "retry_count": model.retry_count,
            "error": model.error,
            "created_at": serialize_datetime(model.created_at),
        }

    # ───────────────────────────────────────────────────────────────────
    # MODEL HOSTING — persisted in the DB (hosted_models). Must NOT live in
    # process memory: the API runs multiple uvicorn workers / replicas, so an
    # in-memory store makes uploads invisible to other workers (random 404s)
    # and loses every model on restart.
    # ───────────────────────────────────────────────────────────────────
    def _hosted_model_to_dict(self, row: HostedModel, include_blob: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": row.public_id,
            "name": row.name,
            "description": row.description,
            "model_type": row.model_type,
            "input_schema": copy.deepcopy(row.input_schema or {}),
            "output_schema": copy.deepcopy(row.output_schema or {}),
            "metrics": copy.deepcopy(row.metrics or {}),
            "status": row.status,
            "version": row.version,
            "has_predict": row.has_predict,
            "has_predict_proba": row.has_predict_proba,
            "last_test_result": copy.deepcopy(row.last_test_result or {}),
            "created_at": serialize_datetime(row.created_at),
            "updated_at": serialize_datetime(row.updated_at),
        }
        if include_blob:
            # Decrypt only for server-side execution; the blob is never sent over the API.
            out["model_blob"] = decrypt_model_blob(row.model_blob)
        return out

    def list_models(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(HostedModel).where(HostedModel.tenant_id == resolved).order_by(desc(HostedModel.created_at))).all()
            return [self._hosted_model_to_dict(row) for row in rows]

    def get_model(self, model_id: str, include_blob: bool = False, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(select(HostedModel).where(HostedModel.tenant_id == resolved, HostedModel.public_id == model_id))
            return self._hosted_model_to_dict(row, include_blob=include_blob) if row else None

    def create_model(self, data: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = HostedModel(
                tenant_id=resolved,
                public_id=data["id"],
                name=data["name"],
                description=data.get("description"),
                model_type=data.get("model_type", "sklearn"),
                model_blob=encrypt_model_blob(data["model_blob"]),
                input_schema=copy.deepcopy(data.get("input_schema") or {}),
                output_schema=copy.deepcopy(data.get("output_schema") or {}),
                metrics=copy.deepcopy(data.get("metrics") or {}),
                status=data.get("status", "dev"),
                version=int(data.get("version", 1)),
                has_predict=bool(data.get("has_predict", True)),
                has_predict_proba=bool(data.get("has_predict_proba", False)),
                last_test_result={},
            )
            session.add(row)
            session.flush()
            return self._hosted_model_to_dict(row)

    def update_model(self, model_id: str, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(select(HostedModel).where(HostedModel.tenant_id == resolved, HostedModel.public_id == model_id))
            if not row:
                return None
            for key in ("name", "description", "model_type", "input_schema", "output_schema", "metrics", "status", "version", "has_predict", "has_predict_proba", "last_test_result"):
                if key in patch:
                    setattr(row, key, patch[key])
            return self._hosted_model_to_dict(row)

    def delete_model(self, model_id: str, tenant_id: Optional[str] = None) -> bool:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            result = session.execute(delete(HostedModel).where(HostedModel.tenant_id == resolved, HostedModel.public_id == model_id))
            return result.rowcount > 0

    # ---- Decision tables --------------------------------------------------- #
    @staticmethod
    def _decision_table_to_dict(row: DecisionTable) -> Dict[str, Any]:
        return {
            "id": row.public_id,
            "name": row.name,
            "description": row.description,
            "hit_policy": row.hit_policy,
            "inputs": copy.deepcopy(row.inputs or []),
            "outputs": copy.deepcopy(row.outputs or []),
            "rows": copy.deepcopy(row.rows or []),
            "default_row": copy.deepcopy(row.default_row) if row.default_row else None,
            "status": row.status,
            "version": row.version,
            "last_test_result": copy.deepcopy(row.last_test_result) if row.last_test_result else None,
            "created_at": serialize_datetime(row.created_at),
            "updated_at": serialize_datetime(row.updated_at),
        }

    def list_decision_tables(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(DecisionTable).where(DecisionTable.tenant_id == resolved).order_by(DecisionTable.name)).all()
            return [self._decision_table_to_dict(row) for row in rows]

    def get_decision_table(self, table_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(select(DecisionTable).where(DecisionTable.tenant_id == resolved, DecisionTable.public_id == table_id))
            return self._decision_table_to_dict(row) if row else None

    def create_decision_table(self, data: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        now = datetime.utcnow()
        with self.connect() as session:
            row = DecisionTable(
                tenant_id=resolved,
                public_id=data["id"],
                name=data["name"],
                description=data.get("description"),
                hit_policy=data.get("hit_policy", "first"),
                inputs=copy.deepcopy(data.get("inputs") or []),
                outputs=copy.deepcopy(data.get("outputs") or []),
                rows=copy.deepcopy(data.get("rows") or []),
                default_row=copy.deepcopy(data.get("default_row")),
                status=data.get("status", "dev"),
                version=int(data.get("version", 1)),
                last_test_result=copy.deepcopy(data.get("last_test_result")),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.add(EntityHistory(
                tenant_id=resolved, entity_type="decision_table", entity_id=data["id"],
                version=1, snapshot=self._decision_table_to_dict(row), created_at=now,
            ))
            session.flush()
            result = self._decision_table_to_dict(row)
        self._audit_entity_change("decision_table", result["id"], "created", resolved,
                                  version=result.get("version"))
        return result

    def update_decision_table(self, table_id: str, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(select(DecisionTable).where(DecisionTable.tenant_id == resolved, DecisionTable.public_id == table_id))
            if not row:
                return None
            for key in ("name", "description", "hit_policy", "inputs", "outputs", "rows", "default_row", "status", "last_test_result"):
                if key in patch:
                    setattr(row, key, copy.deepcopy(patch[key]))
            row.version = (row.version or 1) + 1
            row.updated_at = datetime.utcnow()
            session.add(EntityHistory(
                tenant_id=resolved, entity_type="decision_table", entity_id=table_id,
                version=row.version, snapshot=self._decision_table_to_dict(row), created_at=row.updated_at,
            ))
            result = self._decision_table_to_dict(row)
        self._audit_entity_change("decision_table", result["id"], "updated", resolved,
                                  version=result.get("version"))
        return result

    def delete_decision_table(self, table_id: str, tenant_id: Optional[str] = None) -> bool:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            result = session.execute(delete(DecisionTable).where(DecisionTable.tenant_id == resolved, DecisionTable.public_id == table_id))
            deleted = result.rowcount > 0
        if deleted:
            self._audit_entity_change("decision_table", table_id, "deleted", resolved)
        return deleted

    # ---- Report definitions ------------------------------------------------ #
    @staticmethod
    def _report_to_dict(row: ReportDefinition) -> Dict[str, Any]:
        return {
            "id": row.public_id,
            "name": row.name,
            "description": row.description,
            "columns": copy.deepcopy(row.columns or []),
            "filters": copy.deepcopy(row.filters or {}),
            "timezone": row.timezone,
            "schedule": copy.deepcopy(row.schedule) if row.schedule else None,
            "last_run": copy.deepcopy(row.last_run) if row.last_run else None,
            "version": row.version,
            "created_at": serialize_datetime(row.created_at),
            "updated_at": serialize_datetime(row.updated_at),
        }

    def list_reports(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(ReportDefinition).where(ReportDefinition.tenant_id == resolved).order_by(ReportDefinition.name)).all()
            return [self._report_to_dict(row) for row in rows]

    def list_scheduled_reports(self) -> List[Dict[str, Any]]:
        """All reports (across tenants) with an enabled schedule — for the scheduler."""
        with self.connect() as session:
            rows = session.scalars(select(ReportDefinition)).all()
            out = []
            for row in rows:
                sched = row.schedule or {}
                if sched.get("enabled") and sched.get("cron"):
                    d = self._report_to_dict(row)
                    d["tenant_id"] = row.tenant_id
                    out.append(d)
            return out

    def get_report(self, report_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(select(ReportDefinition).where(ReportDefinition.tenant_id == resolved, ReportDefinition.public_id == report_id))
            return self._report_to_dict(row) if row else None

    def create_report(self, data: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        now = datetime.utcnow()
        with self.connect() as session:
            row = ReportDefinition(
                tenant_id=resolved,
                public_id=data["id"],
                name=data["name"],
                description=data.get("description"),
                columns=copy.deepcopy(data.get("columns") or []),
                filters=copy.deepcopy(data.get("filters") or {}),
                timezone=data.get("timezone", "UTC"),
                schedule=copy.deepcopy(data.get("schedule")),
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._report_to_dict(row)

    def update_report(self, report_id: str, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(select(ReportDefinition).where(ReportDefinition.tenant_id == resolved, ReportDefinition.public_id == report_id))
            if not row:
                return None
            for key in ("name", "description", "columns", "filters", "timezone", "schedule", "last_run"):
                if key in patch:
                    setattr(row, key, copy.deepcopy(patch[key]))
            if any(k in patch for k in ("name", "columns", "filters", "timezone", "schedule")):
                row.version = (row.version or 1) + 1
            row.updated_at = datetime.utcnow()
            return self._report_to_dict(row)

    def delete_report(self, report_id: str, tenant_id: Optional[str] = None) -> bool:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            result = session.execute(delete(ReportDefinition).where(ReportDefinition.tenant_id == resolved, ReportDefinition.public_id == report_id))
            return result.rowcount > 0

    # ---- Durable email outbox (report delivery) -------------------------- #
    @staticmethod
    def _outbox_to_dict(row: EmailOutbox) -> Dict[str, Any]:
        return {
            "id": row.id, "tenant_id": row.tenant_id, "recipients": list(row.recipients or []),
            "subject": row.subject, "body": row.body, "csv_content": row.csv_content,
            "csv_filename": row.csv_filename, "status": row.status, "attempts": row.attempts,
            "last_error": row.last_error, "created_at": serialize_datetime(row.created_at),
        }

    def enqueue_email(self, data: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id or data.get("tenant_id"))
        with self.connect() as session:
            row = EmailOutbox(
                tenant_id=resolved,
                recipients=copy.deepcopy(data.get("recipients") or []),
                subject=data.get("subject", ""),
                body=data.get("body", ""),
                csv_content=data.get("csv_content", ""),
                csv_filename=data.get("csv_filename", "report.csv"),
                status=data.get("status", "pending"),
                attempts=int(data.get("attempts", 0)),
                last_error=data.get("last_error"),
            )
            session.add(row)
            session.flush()
            return self._outbox_to_dict(row)

    def list_pending_emails(self, max_attempts: int = 5, limit: int = 100) -> List[Dict[str, Any]]:
        with self.connect() as session:
            rows = session.scalars(
                select(EmailOutbox).where(EmailOutbox.status == "pending", EmailOutbox.attempts < max_attempts)
                .order_by(EmailOutbox.created_at).limit(limit)
            ).all()
            return [self._outbox_to_dict(row) for row in rows]

    def mark_email(self, email_id: str, status: str, error: Optional[str] = None, increment_attempt: bool = False) -> None:
        with self.connect() as session:
            row = session.get(EmailOutbox, email_id)
            if not row:
                return
            row.status = status
            if error is not None:
                row.last_error = error
            if increment_attempt:
                row.attempts = (row.attempts or 0) + 1
            row.updated_at = datetime.utcnow()

    def count_outbox(self, status: Optional[str] = None, tenant_id: Optional[str] = None) -> int:
        with self.connect() as session:
            stmt = select(func.count()).select_from(EmailOutbox)
            if tenant_id:
                stmt = stmt.where(EmailOutbox.tenant_id == self._tenant_id(tenant_id))
            if status:
                stmt = stmt.where(EmailOutbox.status == status)
            return int(session.scalar(stmt) or 0)

    # MODEL EVALUATIONS — persisted metric runs (Gini/AUC/calibration/…).
    # Stores metrics + dataset summary only, never the raw scored rows.
    # ───────────────────────────────────────────────────────────────────
    @staticmethod
    def _model_evaluation_to_dict(row: ModelEvaluation) -> Dict[str, Any]:
        return {
            "id": row.public_id,
            "name": row.name,
            "description": row.description,
            "model_id": row.model_id,
            "task": row.task,
            "dataset_summary": copy.deepcopy(row.dataset_summary or {}),
            "metrics": copy.deepcopy(row.metrics or {}),
            "segments": copy.deepcopy(row.segments or {}),
            "temporal": copy.deepcopy(row.temporal or {}),
            "gate_status": row.gate_status,
            "gate_result": copy.deepcopy(row.gate_result or {}),
            "status": row.status,
            "created_at": serialize_datetime(row.created_at),
            "updated_at": serialize_datetime(row.updated_at),
        }

    def list_evaluations(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(
                select(ModelEvaluation).where(ModelEvaluation.tenant_id == resolved).order_by(desc(ModelEvaluation.created_at))
            ).all()
            return [self._model_evaluation_to_dict(row) for row in rows]

    def get_evaluation(self, evaluation_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(
                select(ModelEvaluation).where(
                    ModelEvaluation.tenant_id == resolved, ModelEvaluation.public_id == evaluation_id
                )
            )
            return self._model_evaluation_to_dict(row) if row else None

    def create_evaluation(self, data: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = ModelEvaluation(
                tenant_id=resolved,
                public_id=data["id"],
                name=data["name"],
                description=data.get("description"),
                model_id=data.get("model_id"),
                task=data.get("task", "binary"),
                dataset_summary=copy.deepcopy(data.get("dataset_summary") or {}),
                metrics=copy.deepcopy(data.get("metrics") or {}),
                segments=copy.deepcopy(data.get("segments") or {}),
                temporal=copy.deepcopy(data.get("temporal") or {}),
                gate_status=data.get("gate_status", "unknown"),
                gate_result=copy.deepcopy(data.get("gate_result") or {}),
                status=data.get("status", "dev"),
            )
            session.add(row)
            session.flush()
            return self._model_evaluation_to_dict(row)

    def delete_evaluation(self, evaluation_id: str, tenant_id: Optional[str] = None) -> bool:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            result = session.execute(
                delete(ModelEvaluation).where(
                    ModelEvaluation.tenant_id == resolved, ModelEvaluation.public_id == evaluation_id
                )
            )
            return result.rowcount > 0

    # ---- Scheduler leader lease (multi-replica single-fire) --------------- #
    def try_acquire_scheduler_lease(self, owner: str, ttl_seconds: int = 30) -> bool:
        """Atomically acquire or renew the singleton scheduler lease. Returns True
        iff this owner now holds it. Race-free across replicas: the conditional
        UPDATE is serialised by the DB, so at most one owner wins per interval."""
        now = datetime.utcnow()
        until = now + timedelta(seconds=ttl_seconds)
        with self.connect() as session:
            # Take (or keep) leadership if we already own it, or the lease expired.
            updated = session.execute(
                update(SchedulerLease)
                .where(SchedulerLease.id == "singleton",
                       or_(SchedulerLease.owner == owner, SchedulerLease.lease_until < now))
                .values(owner=owner, lease_until=until, updated_at=now)
            )
            if updated.rowcount and updated.rowcount > 0:
                return True
            # No row updated: either it exists and is held by someone else, or it
            # has never been created. Try to create it (first replica to boot wins).
            if session.get(SchedulerLease, "singleton") is None:
                try:
                    session.add(SchedulerLease(id="singleton", owner=owner, lease_until=until, updated_at=now))
                    session.flush()
                    return True
                except IntegrityError:
                    session.rollback()  # another replica created it first
                    return False
            return False

    def release_scheduler_lease(self, owner: str) -> None:
        """Relinquish leadership on graceful shutdown so another replica takes over
        immediately instead of waiting for the lease to expire."""
        now = datetime.utcnow()
        with self.connect() as session:
            session.execute(
                update(SchedulerLease)
                .where(SchedulerLease.id == "singleton", SchedulerLease.owner == owner)
                .values(lease_until=now, updated_at=now)
            )
