"""Widen audit_events.ip_address so the column can hold ciphertext.

Decision payloads have been encrypted at rest since DECISION_ENCRYPT_AT_REST landed, but the
audit trail was not, and it is not a lesser store of personal data: `metadata_json` carries the
email addresses, source IPs and usernames of failed sign-ins, member invitations and role
changes, and `ip_address` is a source IP by definition. A database dump, a stolen backup or a
read-replica credential exposed all of it in plaintext while the decisions beside it were
opaque.

Only the column width changes here. Encryption is applied on write by
`storage.encrypt_audit_field`, and existing plaintext rows keep reading through the same
legacy path `decrypt_decision_field` already uses -- so there is no backfill and no window in
which old rows are unreadable. Re-encrypting them onto the current key is
`scripts/rotate_config_key.py`'s job, which now covers this table.

The width is the reason a migration is needed at all. A Fernet token is
`base64(1 + 8 + 16 + padded_plaintext + 32)` bytes; an IPv6 address padded to 48 bytes lands
around 140 characters, which does not fit in the String(128) this column was. SQLite ignores
declared widths, so this would have passed every local test and failed only on Postgres, on
IPv6 traffic, in production.

The chain hash is unaffected: `audit_chain.CHAINED_FIELDS` commits to `metadata` and
`ip_address` as the application sees them, and both are decrypted before
`_audit_event_to_dict` returns. Every existing hash still verifies.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260904_0023"
down_revision = "20260904_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent and backend-aware, matching the house pattern. SQLite cannot ALTER a column
    # type and does not enforce VARCHAR lengths anyway, so widening there is a no-op that
    # batch_alter_table would nonetheless implement as a full table rebuild -- skip it.
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    columns = {col["name"]: col for col in sa.inspect(bind).get_columns("audit_events")}
    current = columns.get("ip_address")
    if current is None:
        return
    length = getattr(current["type"], "length", None)
    if length is not None and length < 512:
        op.alter_column("audit_events", "ip_address",
                        existing_type=sa.String(length=length), type_=sa.String(length=512),
                        existing_nullable=True)


def downgrade() -> None:
    # Deliberately not narrowing: any row written since the upgrade holds ciphertext longer
    # than 128 characters, and narrowing would truncate it into an undecryptable value -- a
    # silent, unrecoverable loss of the audit trail this table exists to protect.
    pass
