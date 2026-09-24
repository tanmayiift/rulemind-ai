"""Mask connector secrets already written to entity_histories.

`Storage.create_connector` snapshotted the full connector payload into `entity_histories` --
config included, in plaintext JSON -- while the connector's own config column is encrypted. Every
credential a connector was created with was therefore readable at rest and in every backup. The
write is masked from this release on; this migration masks the rows written before it, using the
same key markers the API uses to mask secrets on read.

Idempotent: a masked value stays masked. Irreversible by design -- there is no downgrade that
puts a secret back.
"""
from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "20260924_0033"
down_revision = "20260924_0032"
branch_labels = None
depends_on = None

MASK = "••••••••"
# Mirrors storage.SECRET_FIELD_MARKERS. Copied, not imported: a migration must keep meaning what
# it meant when it was written, even if the application's list changes later.
MARKERS = ("token", "secret", "password", "api_key", "apikey", "client_secret", "private_key",
           "passphrase", "client_cert_p12")


def _mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: (MASK if any(m in str(key).lower() for m in MARKERS) else _mask(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_mask(item) for item in value]
    return value


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("entity_histories"):
        return
    table = sa.table("entity_histories", sa.column("id", sa.String), sa.column("entity_type", sa.String),
                     sa.column("snapshot", sa.JSON))
    rows = bind.execute(sa.select(table.c.id, table.c.snapshot)
                        .where(table.c.entity_type == "connector")).fetchall()
    for row_id, snapshot in rows:
        data = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
        masked = _mask(data)
        if masked != data:
            bind.execute(sa.update(table).where(table.c.id == row_id).values(snapshot=masked))


def downgrade() -> None:
    # Nothing to restore: the secrets are gone from history by intent.
    pass
