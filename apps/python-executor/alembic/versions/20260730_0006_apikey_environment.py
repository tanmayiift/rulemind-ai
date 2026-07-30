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

revision = "20260730_0006"
down_revision = "20260729_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("api_keys")}
    if "environment" not in columns:
        op.add_column("api_keys", sa.Column("environment", sa.String(length=16), nullable=False, server_default="prod"))
    if "label" not in columns:
        op.add_column("api_keys", sa.Column("label", sa.String(length=128), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("api_keys")}
    if "label" in columns:
        op.drop_column("api_keys", "label")
    if "environment" in columns:
        op.drop_column("api_keys", "environment")
