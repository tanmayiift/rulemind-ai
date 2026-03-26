from __future__ import annotations

from pathlib import Path
import sys

from alembic import op

CURRENT_FILE = Path(__file__).resolve()
candidate_roots = []
if len(CURRENT_FILE.parents) > 4:
    candidate_roots.append(CURRENT_FILE.parents[4] / "apps" / "python-executor")
candidate_roots.extend(
    [
        CURRENT_FILE.parents[2],
        Path("/app/apps/python-executor"),
        Path("/app"),
    ]
)
APP_ROOT = next((candidate for candidate in candidate_roots if candidate.exists() and (candidate / "app").exists()), CURRENT_FILE.parents[2])
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.models import Base  # noqa: E402


revision = "20260326_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
