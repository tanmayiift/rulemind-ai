"""Record WHICH VERSION of a policy produced each decision.

The question this product exists to answer is "why was this application rejected eight months
ago". Today you can answer which condition failed and by how much -- `rule_results` is genuinely
good. What you cannot answer is **which version of the policy was live at the time**, because
`Decision` carried only `policy_id`, and policies are mutated in place with `Policy.version`
incrementing on the row.

So the lineage had to be inferred: take the decision's `created_at`, walk `EntityHistory` for
that policy, and pick the snapshot that was current. For a regulator question that inference IS
the answer, and the product made the examiner do it by hand.

`bundle_hash` (added in 20260806_0015) is the same shape of gap from the other direction: the
column exists, `add_decision` reads it, and only the SDK sync path ever supplied it -- so
ON-DEVICE decisions carried bundle lineage and SERVER decisions did not. That one is not fixed
by a column: the executor path does not load a bundle at all (it reads rules and scorecards live
from the DB), so `bundle_hash` is only meaningful on the fast path, and is populated there.

Nullable on purpose. Existing rows genuinely do not know their policy version, and backfilling a
guess from EntityHistory would put a fabricated lineage into an attestable record -- the same
class of mistake as the hardcoded adverse-action reason codes this round removed. NULL means
"not recorded", which is true.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260924_0031"
down_revision = "20260916_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent per COLUMN, the house pattern: Storage.__init__ runs create_all, so on a fresh
    # database `decisions` already carries policy_version from the model by the time this runs.
    # Checking the TABLE alone would skip a partially-migrated one.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("decisions"):
        return
    existing = {column["name"] for column in inspector.get_columns("decisions")}
    if "policy_version" not in existing:
        op.add_column("decisions", sa.Column("policy_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("decisions"):
        return
    existing = {column["name"] for column in inspector.get_columns("decisions")}
    if "policy_version" in existing:
        op.drop_column("decisions", "policy_version")
