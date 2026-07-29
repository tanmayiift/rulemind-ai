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

revision = "20260729_0004"
down_revision = "20260728_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Persist hosted ML models (previously an in-memory dict — lost on restart and
    # invisible across workers). Idempotent: create_all already made it on a fresh
    # chain, so only create it for older databases.
    from app.models import HostedModel  # noqa: WPS433

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "hosted_models" not in inspector.get_table_names():
        HostedModel.__table__.create(bind=bind)


def downgrade() -> None:
    from app.models import HostedModel  # noqa: WPS433

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "hosted_models" in inspector.get_table_names():
        HostedModel.__table__.drop(bind=bind)
