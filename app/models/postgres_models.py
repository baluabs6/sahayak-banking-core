"""
SQLAlchemy ORM models — structured, relational data for every domain:
inclusion (users, credit scores), fraud (flags), lending (loans),
insurance (claims). Kept in one module for a small service; split into
per-domain modules if the codebase grows.
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.postgres import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_ref: Mapped[str] = mapped_column(String(20), unique=True, index=True)  # e.g. USR-1001
    full_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(20), unique=True)
    region: Mapped[str] = mapped_column(String(20))  # rural | semi_urban | urban
    state: Mapped[str] = mapped_column(String(60))
    occupation: Mapped[str] = mapped_column(String(60))
    monthly_income_inr: Mapped[float] = mapped_column(Numeric(12, 2))
    has_bank_account: Mapped[bool] = mapped_column(Boolean, default=False)
    has_formal_credit_history: Mapped[bool] = mapped_column(Boolean, default=False)
    kyc_status: Mapped[str] = mapped_column(String(20), default="not_started")
    language_pref: Mapped[str] = mapped_column(String(5), default="en")
    aadhaar_linked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    credit_scores: Mapped[list["CreditScore"]] = relationship(back_populates="user")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="user")
    loan_applications: Mapped[list["LoanApplication"]] = relationship(back_populates="user")
    insurance_claims: Mapped[list["InsuranceClaim"]] = relationship(back_populates="user")


class CreditScore(Base):
    """Alt-data-driven inclusion credit score — output of the scoring engine."""
    __tablename__ = "credit_scores"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    score: Mapped[int] = mapped_column()  # 300-900 scale, like CIBIL
    score_band: Mapped[str] = mapped_column(String(20))  # poor | fair | good | excellent
    model_version: Mapped[str] = mapped_column(String(30))
    explanation: Mapped[str] = mapped_column(Text)  # human-readable reasoning (LLM-generated)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="credit_scores")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    txn_ref: Mapped[str] = mapped_column(String(20), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    amount_inr: Mapped[float] = mapped_column(Numeric(12, 2))
    txn_type: Mapped[str] = mapped_column(String(30))  # UPI_OUT | NEFT_OUT | IMPS_OUT | CASH_DEPOSIT
    merchant: Mapped[str] = mapped_column(String(120))
    location: Mapped[str] = mapped_column(String(120))
    device_id: Mapped[str] = mapped_column(String(30))
    is_flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    fraud_score: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="transactions")


class LoanApplication(Base):
    __tablename__ = "loan_applications"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    application_ref: Mapped[str] = mapped_column(String(20), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    loan_type: Mapped[str] = mapped_column(String(40))  # personal | msme_working_capital | agri_credit | microfinance
    requested_amount_inr: Mapped[float] = mapped_column(Numeric(14, 2))
    purpose: Mapped[str] = mapped_column(String(120))
    collateral_provided: Mapped[bool] = mapped_column(Boolean, default=False)
    gst_data_available: Mapped[bool] = mapped_column(Boolean, default=False)
    bank_statement_months_provided: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(20), default="under_review")
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="loan_applications")


class InsuranceClaim(Base):
    __tablename__ = "insurance_claims"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    claim_ref: Mapped[str] = mapped_column(String(20), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    policy_type: Mapped[str] = mapped_column(String(40))  # crop_insurance | health | shop_property
    trigger_event: Mapped[str] = mapped_column(String(60))
    region: Mapped[str] = mapped_column(String(80))
    claim_amount_inr: Mapped[float] = mapped_column(Numeric(14, 2))
    parametric_data_source: Mapped[str | None] = mapped_column(String(60), nullable=True)
    auto_triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="filed")
    filed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="insurance_claims")
