#!/usr/bin/env python3
"""Re-encrypt every encrypted column under a new RULEMIND_CONFIG_KEY.

WHY THIS EXISTS
---------------
`RULEMIND_CONFIG_KEY` is the master secret behind every encrypted column: connector configs,
LLM provider keys, SMTP passwords, SSO client secrets, webhook signing secrets, hosted model
blobs and decision payloads. Until this script there was no way to change it. `fernet_key()`
derived exactly one key, every decrypt used that key alone, and every decrypt helper returned a
*plausible* value on failure (the ciphertext, or `{}`), so rotating the key did not raise — it
silently turned every stored secret into garbage that then went out over the wire.

So a key rotation, the standard response to a suspected key compromise, was the one operation
guaranteed to cause a silent, unrecoverable data incident.

HOW TO ROTATE
-------------
1. Keep serving on the old key. Nothing to do yet.
2. Set BOTH variables and restart the API:

       RULEMIND_CONFIG_KEY=<new key>
       RULEMIND_CONFIG_KEY_OLD=<old key>        # comma-separated if there are several

   Reads now try the new key first and fall back to the old one, so the app keeps working
   while nothing has been re-encrypted yet. New writes already use the new key.
3. Run this script (with the same two variables set). It rewrites every stored ciphertext onto
   the new key.
4. Once it reports 0 remaining, drop RULEMIND_CONFIG_KEY_OLD and restart.

It is RESUMABLE (progress is checkpointed per table to a JSON state file, and it commits in
batches) and SAFE TO RUN TWICE: a value that already decrypts under the new key alone is left
untouched, so a second run is a no-op that reports 0 rotated.

THE KDF CHANGE
--------------
Key derivation moved from a bare `sha256(secret)` to PBKDF2-HMAC-SHA256. That changes the
derived key even when the secret is unchanged, so existing rows were written under a key the
new derivation does not produce. `storage._legacy_sha256_key` keeps the old derivation in the
decryption chain for exactly that reason, and running this script with the SAME value in
RULEMIND_CONFIG_KEY (and nothing in RULEMIND_CONFIG_KEY_OLD) migrates the data onto the KDF
key. That is a supported, useful invocation: upgrade the derivation without changing the secret.

USAGE
    python -m scripts.rotate_config_key --dry-run
    python -m scripts.rotate_config_key
    python -m scripts.rotate_config_key --state-file /var/lib/rulemind/rotation.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from cryptography.fernet import Fernet, InvalidToken  # noqa: E402

from app import storage as storage_mod  # noqa: E402
from app.db import session_factory  # noqa: E402
from app.models import Connector, Decision, HostedModel, Setting, Tenant, Webhook  # noqa: E402

LOGGER = logging.getLogger("rulemind.rotate")

DEFAULT_STATE_FILE = ".runtime/config_key_rotation.json"
DEFAULT_BATCH_SIZE = 500


# ── "is this already on the new key?" ────────────────────────────────────────
def _primary_only() -> Fernet:
    """A Fernet holding ONLY the current primary key.

    The skip test has to be "does this decrypt under the primary key *alone*", not "does this
    decrypt at all" — `storage.crypto()` includes the old keys, so it opens everything and
    would make every row look already-rotated.
    """
    return Fernet(storage_mod.fernet_key())


def _already_rotated(token: Any) -> bool:
    if token is None:
        return True
    raw = token.encode("utf-8") if isinstance(token, str) else token
    if not isinstance(raw, bytes) or not raw.startswith(storage_mod._FERNET_PREFIX_BYTES):
        # Not ciphertext: a legacy pre-encryption plaintext row. Rotating it would ENCRYPT it,
        # which is a different operation with different risks (a column-width change on some
        # backends, and a one-way step for anything still reading it raw). Left alone
        # deliberately; the decrypt helpers still read it.
        return True
    try:
        _primary_only().decrypt(raw)
        return True
    except InvalidToken:
        return False


def _reencrypt_text(token: Optional[str]) -> Optional[str]:
    """Decrypt with the full chain, re-encrypt with the primary. Returns None to mean 'skip'."""
    if not token or _already_rotated(token):
        return None
    plaintext = storage_mod.crypto().decrypt(token.encode("utf-8"))
    return storage_mod.crypto().encrypt(plaintext).decode("utf-8")


def _reencrypt_blob(blob: Optional[bytes]) -> Optional[bytes]:
    if not blob or _already_rotated(blob):
        return None
    return storage_mod.crypto().encrypt(storage_mod.crypto().decrypt(blob))


def _reencrypt_nested(container: Dict[str, Any], key: str) -> bool:
    """Re-encrypt `container[key]` in place. True when it changed."""
    rotated = _reencrypt_text(container.get(key))
    if rotated is None:
        return False
    container[key] = rotated
    return True


# ── per-table workers ────────────────────────────────────────────────────────
# Each returns the number of rows it actually rewrote. Each takes an open session and a
# (start_after, limit) window so the caller can checkpoint between batches.


def _rotate_connectors(session: Any, after: Optional[str], limit: int) -> Tuple[int, Optional[str]]:
    rows = _window(session, Connector, Connector.pk, after, limit)
    changed = 0
    for row in rows:
        rotated = _reencrypt_text(row.encrypted_config)
        if rotated is not None:
            row.encrypted_config = rotated
            changed += 1
    return changed, (rows[-1].pk if rows else None)


_DECISION_COLUMNS = ("payload_preview", "computed_variables", "rule_results", "scorecard_result", "trace")


def _rotate_decisions(session: Any, after: Optional[str], limit: int) -> Tuple[int, Optional[str]]:
    rows = _window(session, Decision, Decision.id, after, limit)
    changed = 0
    for row in rows:
        touched = False
        for column in _DECISION_COLUMNS:
            value = getattr(row, column)
            if not isinstance(value, str):
                continue  # legacy plaintext dict/list, or NULL
            rotated = _reencrypt_text(value)
            if rotated is not None:
                setattr(row, column, rotated)
                touched = True
        changed += 1 if touched else 0
    return changed, (rows[-1].id if rows else None)


def _rotate_models(session: Any, after: Optional[str], limit: int) -> Tuple[int, Optional[str]]:
    rows = _window(session, HostedModel, HostedModel.id, after, limit)
    changed = 0
    for row in rows:
        rotated = _reencrypt_blob(row.model_blob)
        if rotated is not None:
            row.model_blob = rotated
            changed += 1
    return changed, (rows[-1].id if rows else None)


def _rotate_webhooks(session: Any, after: Optional[str], limit: int) -> Tuple[int, Optional[str]]:
    rows = _window(session, Webhook, Webhook.id, after, limit)
    changed = 0
    for row in rows:
        rotated = _reencrypt_text(row.secret_hash)
        if rotated is not None:
            row.secret_hash = rotated
            changed += 1
    return changed, (rows[-1].id if rows else None)


def _rotate_tenants(session: Any, after: Optional[str], limit: int) -> Tuple[int, Optional[str]]:
    """SMTP password + SSO client secret live inside the tenant's JSON config."""
    rows = _window(session, Tenant, Tenant.id, after, limit)
    changed = 0
    for row in rows:
        config = json.loads(json.dumps(row.config or {}))  # detach from the JSON column
        touched = _reencrypt_nested(config.get("email") or {}, "password_encrypted")
        touched |= _reencrypt_nested(config.get("sso") or {}, "client_secret_encrypted")
        if touched:
            # Re-assign, don't mutate: SQLAlchemy's plain JSON type is not change-tracked, so an
            # in-place edit of row.config would be silently dropped at flush.
            row.config = config
            changed += 1
    return changed, (rows[-1].id if rows else None)


def _rotate_settings(session: Any, after: Optional[str], limit: int) -> Tuple[int, Optional[str]]:
    """Per-provider LLM API keys live inside settings.ai_config."""
    rows = _window(session, Setting, Setting.id, after, limit)
    changed = 0
    for row in rows:
        config = json.loads(json.dumps(row.ai_config or {}))
        touched = False
        for slot in (config.get("providers") or {}).values():
            if isinstance(slot, dict):
                touched |= _reencrypt_nested(slot, "key_encrypted")
        if touched:
            row.ai_config = config
            changed += 1
    return changed, (rows[-1].id if rows else None)


def _window(session: Any, model: Any, pk: Any, after: Optional[str], limit: int) -> List[Any]:
    """One ordered page of rows after `after`. Keyset pagination, not OFFSET, so a resumed run
    cannot skip or repeat rows if the table is written to while the rotation is in flight."""
    from sqlalchemy import select

    query = select(model).order_by(pk).limit(limit)
    if after is not None:
        query = query.where(pk > after)
    return list(session.scalars(query).all())


TABLES: List[Tuple[str, Callable[[Any, Optional[str], int], Tuple[int, Optional[str]]]]] = [
    ("connectors", _rotate_connectors),
    ("tenants", _rotate_tenants),
    ("settings", _rotate_settings),
    ("webhooks", _rotate_webhooks),
    ("hosted_models", _rotate_models),
    ("decisions", _rotate_decisions),  # last: much the largest, and the least urgent
]


# ── resumable state ──────────────────────────────────────────────────────────
def _load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"cursors": {}, "done": [], "rotated": {}}
    try:
        return json.loads(path.read_text("utf-8"))
    except ValueError:
        LOGGER.warning("State file %s is unreadable; starting from the beginning.", path)
        return {"cursors": {}, "done": [], "rotated": {}}


def _save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: a crash mid-write must not leave a truncated checkpoint that makes the
    # next run restart a half-finished table from row zero.
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, indent=2), "utf-8")
    temp.replace(path)


def rotate(state_path: Path, batch_size: int, dry_run: bool) -> Dict[str, int]:
    state = _load_state(state_path)
    factory = session_factory()
    totals: Dict[str, int] = {}

    for name, worker in TABLES:
        if name in state["done"]:
            LOGGER.info("%-14s already complete (per %s)", name, state_path)
            # 0 for THIS run, not the checkpoint's cumulative count: the final line reports what
            # this invocation changed, so a repeat run truthfully says "0 row(s) re-encrypted"
            # instead of re-reporting the first run's work as if it had happened again.
            totals[name] = 0
            continue
        cursor = state["cursors"].get(name)
        rotated = 0
        while True:
            session = factory()
            try:
                changed, last = worker(session, cursor, batch_size)
                if dry_run:
                    session.rollback()
                else:
                    session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
            rotated += changed
            if last is None:
                state["done"].append(name)
                break
            cursor = last
            state["cursors"][name] = cursor
            state["rotated"][name] = rotated
            if not dry_run:
                _save_state(state_path, state)
        state["rotated"][name] = rotated
        if not dry_run:
            _save_state(state_path, state)
        totals[name] = rotated
        LOGGER.info("%-14s %d row(s) re-encrypted", name, rotated)
    return totals


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE,
                        help="Checkpoint file for resuming an interrupted run (default: %(default)s)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help="Rows per transaction (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change; roll back every batch.")
    parser.add_argument("--restart", action="store_true",
                        help="Ignore any existing checkpoint and start from the beginning.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    primary, olds = storage_mod._config_key_secrets()
    if not olds:
        # Not an error: rotating with no old key is exactly how you migrate sha256-derived data
        # onto the PBKDF2 key without changing the secret. But if the operator meant to change
        # the SECRET and forgot the old one, every row would fail to decrypt — say so first.
        LOGGER.warning(
            "%s is not set. This run can only re-encrypt data that already decrypts under the "
            "current %s (including the legacy sha256 derivation of it). If you have CHANGED the "
            "master secret, set %s to the previous value first or the rotation will fail.",
            storage_mod.CONFIG_KEY_OLD_VAR, storage_mod.CONFIG_KEY_VAR, storage_mod.CONFIG_KEY_OLD_VAR,
        )
    if primary == storage_mod._CONFIG_KEY_DEFAULT:
        LOGGER.warning("%s is the built-in development default. Rotating ONTO a public key.",
                       storage_mod.CONFIG_KEY_VAR)

    state_path = Path(args.state_file)
    if args.restart and state_path.exists():
        state_path.unlink()

    try:
        totals = rotate(state_path, max(1, args.batch_size), args.dry_run)
    except InvalidToken:
        LOGGER.error(
            "A stored value could not be decrypted with any configured key. Nothing in the "
            "current batch was written. Set the previous key(s) in %s (comma-separated) and "
            "re-run; the checkpoint in %s means completed tables are not redone.",
            storage_mod.CONFIG_KEY_OLD_VAR, state_path,
        )
        return 2

    total = sum(totals.values())
    LOGGER.info("%s%d row(s) re-encrypted onto the current %s.",
                "DRY RUN: would have " if args.dry_run else "", total, storage_mod.CONFIG_KEY_VAR)
    if total == 0 and not args.dry_run:
        LOGGER.info("Nothing left to rotate — safe to drop %s.", storage_mod.CONFIG_KEY_OLD_VAR)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    os.environ.setdefault("RULEMIND_SKIP_SECRET_CHECK", "0")
    raise SystemExit(main())
