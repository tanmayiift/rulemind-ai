"""Add saml_assertions_seen — make a SAML assertion single-use.

SAML has no built-in replay defence. An assertion is a signed bearer token, and its signature
stays valid for as long as the IdP's certificate does, so an attacker who captured one POST to
the ACS endpoint — from a proxy log, a shared browser, an intercepted redirect — could replay it
indefinitely and log in as that user. Nothing tracked consumed assertions.

The UNIQUE(tenant_id, assertion_id) constraint is what makes consumption a claim rather than a
check-then-write: two concurrent POSTs of the same captured assertion race to INSERT and exactly
one wins. Keyed per tenant because two IdPs can legitimately mint the same assertion ID, and a
global key would let one workspace probe another's.

not_on_or_after is stored so rows can be pruned once the assertion could no longer be accepted on
its own merits — otherwise the table grows forever.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260903_0021"
down_revision = "20260903_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent, matching the house pattern: Storage.__init__ runs create_all, so on a fresh
    # database the table already exists by the time this migration runs and a bare create_table
    # would abort the whole chain.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "saml_assertions_seen" in inspector.get_table_names():
        return
    op.create_table(
        "saml_assertions_seen",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("assertion_id", sa.String(length=255), nullable=False),
        sa.Column("not_on_or_after", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        # The claim: concurrent POSTs of one captured assertion race here; exactly one wins.
        sa.UniqueConstraint("tenant_id", "assertion_id", name="uq_saml_assertion_tenant_id"),
    )
    op.create_index("ix_saml_assertions_seen_tenant_id", "saml_assertions_seen", ["tenant_id"])
    op.create_index("ix_saml_assertions_seen_not_on_or_after", "saml_assertions_seen", ["not_on_or_after"])


def downgrade() -> None:
    op.drop_table("saml_assertions_seen")
