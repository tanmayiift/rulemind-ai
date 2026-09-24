"""Record each hosted model's format.

`model_executor` scored pickles only; it now also loads ONNX, PMML, XGBoost JSON and LightGBM
model files, chosen by the blob's own bytes. The format is stored for listing -- the blob is
encrypted at rest, and sniffing it per list call would decrypt every model -- never for routing:
scoring always re-reads the bytes. Every model uploaded before this was a pickle, so that is the
backfill.

Idempotent, the house pattern: Storage.__init__ runs create_all, so on a fresh database the column
already exists.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260924_0034"
down_revision = "20260924_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("hosted_models"):
        return
    if "model_format" in {c["name"] for c in inspector.get_columns("hosted_models")}:
        return
    # Nullable, matching the model (see HostedModel.model_format); the server default backfills
    # every existing row with "pickle", which is what each of them is.
    op.add_column("hosted_models", sa.Column("model_format", sa.String(length=24), nullable=True,
                                             server_default="pickle"))


def downgrade() -> None:
    bind = op.get_bind()
    if "model_format" in {c["name"] for c in sa.inspect(bind).get_columns("hosted_models")}:
        op.drop_column("hosted_models", "model_format")
