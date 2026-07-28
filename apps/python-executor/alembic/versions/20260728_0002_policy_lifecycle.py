from __future__ import annotations

from pathlib import Path
import sys

import sqlalchemy as sa
from alembic import op

CURRENT_FILE = Path(__file__).resolve()
candidate_roots = []
if len(CURRENT_FILE.parents) > 4:
    candidate_roots.append(CURRENT_FILE.parents[4] / "apps" / "python-executor")
candidate_roots.extend([CURRENT_FILE.parents[2], Path("/app/apps/python-executor"), Path("/app")])
APP_ROOT = next((c for c in candidate_roots if c.exists() and (c / "app").exists()), CURRENT_FILE.parents[2])
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

revision = "20260728_0002"
down_revision = "20260326_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The initial migration builds tables from the live models via create_all,
    # so on a fresh chain this column already exists — only add it for older
    # databases that predate it. Idempotent to keep both paths clean.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("policies")}
    if "lifecycle_status" not in columns:
        op.add_column(
            "policies",
            sa.Column("lifecycle_status", sa.String(length=24), nullable=False, server_default="draft"),
        )
    indexes = {ix["name"] for ix in inspector.get_indexes("policies")}
    if "ix_policies_lifecycle_status" not in indexes:
        op.create_index("ix_policies_lifecycle_status", "policies", ["lifecycle_status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {ix["name"] for ix in inspector.get_indexes("policies")}
    if "ix_policies_lifecycle_status" in indexes:
        op.drop_index("ix_policies_lifecycle_status", table_name="policies")
    columns = {col["name"] for col in inspector.get_columns("policies")}
    if "lifecycle_status" in columns:
        op.drop_column("policies", "lifecycle_status")
