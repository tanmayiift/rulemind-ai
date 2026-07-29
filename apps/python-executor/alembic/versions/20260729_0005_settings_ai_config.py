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

revision = "20260729_0005"
down_revision = "20260729_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("settings")}
    if "ai_config" not in columns:
        op.add_column("settings", sa.Column("ai_config", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("settings")}
    if "ai_config" in columns:
        op.drop_column("settings", "ai_config")
