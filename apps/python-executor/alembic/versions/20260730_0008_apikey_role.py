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

revision = "20260730_0008"
down_revision = "20260730_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # RBAC role per API key. Existing keys default to "owner" (full access) so the
    # rollout is non-breaking. Idempotent add for pre-existing databases.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("api_keys")}
    if "role" not in columns:
        op.add_column("api_keys", sa.Column("role", sa.String(length=32), nullable=False, server_default="owner"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("api_keys")}
    if "role" in columns:
        op.drop_column("api_keys", "role")
