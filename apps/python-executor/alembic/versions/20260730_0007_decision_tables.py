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

revision = "20260730_0007"
down_revision = "20260730_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Decision tables (grid authoring + optimiser). Idempotent: create_all makes it
    # on a fresh chain, so only create it for pre-existing databases.
    from app.models import DecisionTable  # noqa: WPS433

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "decision_tables" not in inspector.get_table_names():
        DecisionTable.__table__.create(bind=bind)


def downgrade() -> None:
    from app.models import DecisionTable  # noqa: WPS433

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "decision_tables" in inspector.get_table_names():
        DecisionTable.__table__.drop(bind=bind)
