"""initial schema — baseline for all six current tables

This is a baseline migration, not a from-scratch `alembic revision
--autogenerate` output — the app previously relied on
`Base.metadata.create_all()` in local dev (see app/db/postgres.py::init_postgres)
with no migration history at all. This revision hand-codes the schema to
match app/models/postgres_models.py as of the point Alembic was introduced,
so staging/production can be brought under migration control without a
data-losing drop/recreate. Every future schema change should be its own
migration on top of this one.

Revision ID: 6f9e8e6681b9
Revises:
Create Date: 2026-09-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "6f9e8e6681b9"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_ref", sa.String(20), nullable=False),
        sa.Column("full_name", sa.String(120), nullable=False),
        sa.Column("phone", sa.String(20), nullable=False),
        sa.Column("region", sa.String(20), nullable=False),
        sa.Column("state", sa.String(60), nullable=False),
        sa.Column("occupation", sa.String(60), nullable=False),
        sa.Column("monthly_income_inr", sa.Numeric(12, 2), nullable=False),
        sa.Column("has_bank_account", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("has_formal_credit_history", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("kyc_status", sa.String(20), nullable=False, server_default="not_started"),
        sa.Column("language_pref", sa.String(5), nullable=False, server_default="en"),
        sa.Column("aadhaar_linked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint("uq_users_user_ref", "users", ["user_ref"])
    op.create_index("ix_users_user_ref", "users", ["user_ref"])
    op.create_unique_constraint("uq_users_phone", "users", ["phone"])

    op.create_table(
        "credit_scores",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("score_band", sa.String(20), nullable=False),
        sa.Column("model_version", sa.String(30), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("signal_breakdown", sa.JSON(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "transactions",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("txn_ref", sa.String(20), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount_inr", sa.Numeric(12, 2), nullable=False),
        sa.Column("txn_type", sa.String(30), nullable=False),
        sa.Column("merchant", sa.String(120), nullable=False),
        sa.Column("location", sa.String(120), nullable=False),
        sa.Column("device_id", sa.String(30), nullable=False),
        sa.Column("is_flagged", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("fraud_score", sa.Numeric(5, 4), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint("uq_transactions_txn_ref", "transactions", ["txn_ref"])

    op.create_table(
        "loan_applications",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("application_ref", sa.String(20), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("loan_type", sa.String(40), nullable=False),
        sa.Column("requested_amount_inr", sa.Numeric(14, 2), nullable=False),
        sa.Column("purpose", sa.String(120), nullable=False),
        sa.Column("collateral_provided", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("gst_data_available", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("bank_statement_months_provided", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="under_review"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint("uq_loan_applications_application_ref", "loan_applications", ["application_ref"])

    op.create_table(
        "insurance_claims",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("claim_ref", sa.String(20), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("policy_type", sa.String(40), nullable=False),
        sa.Column("trigger_event", sa.String(60), nullable=False),
        sa.Column("region", sa.String(80), nullable=False),
        sa.Column("claim_amount_inr", sa.Numeric(14, 2), nullable=False),
        sa.Column("parametric_data_source", sa.String(60), nullable=True),
        sa.Column("auto_triggered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(20), nullable=False, server_default="filed"),
        sa.Column("filed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint("uq_insurance_claims_claim_ref", "insurance_claims", ["claim_ref"])

    op.create_table(
        "loan_repayments",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("repayment_ref", sa.String(20), nullable=False),
        sa.Column("loan_application_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("loan_applications.id"), nullable=False),
        sa.Column("installment_number", sa.Integer(), nullable=False),
        sa.Column("amount_due_inr", sa.Numeric(12, 2), nullable=False),
        sa.Column("due_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("amount_paid_inr", sa.Numeric(12, 2), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint("uq_loan_repayments_repayment_ref", "loan_repayments", ["repayment_ref"])
    op.create_index("ix_loan_repayments_loan_application_id", "loan_repayments", ["loan_application_id"])


def downgrade() -> None:
    op.drop_table("loan_repayments")
    op.drop_table("insurance_claims")
    op.drop_table("loan_applications")
    op.drop_table("transactions")
    op.drop_table("credit_scores")
    op.drop_table("users")
