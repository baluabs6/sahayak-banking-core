"""
Seeds local Postgres + MongoDB with the dummy datasets in data/dummy_datasets/.
Run after `docker-compose up -d` and `alembic upgrade head` (or app startup
auto-create in local mode):

    python -m data.generate_dummy_data
"""
import asyncio
import json
from pathlib import Path

from app.db.mongo import COLLECTION_KYC_DOCUMENTS, ensure_indexes, get_mongo_db
from app.db.postgres import AsyncSessionLocal, init_postgres
from app.models.postgres_models import InsuranceClaim, LoanApplication, Transaction, User

DATA_DIR = Path(__file__).parent / "dummy_datasets"


def _load(name: str) -> list[dict]:
    with open(DATA_DIR / name, encoding="utf-8") as f:
        return json.load(f)


async def seed_postgres() -> dict[str, str]:
    """Returns a mapping of user_ref -> internal UUID for use when seeding
    dependent tables (transactions, loans, claims)."""
    users_data = _load("users.json")
    txns_data = _load("transactions.json")
    loans_data = _load("loan_applications.json")
    claims_data = _load("insurance_claims.json")

    ref_to_id: dict[str, str] = {}

    async with AsyncSessionLocal() as db:
        for u in users_data:
            user = User(
                user_ref=u["user_id"],
                full_name=u["full_name"],
                phone=u["phone"],
                region=u["region"],
                state=u["state"],
                occupation=u["occupation"],
                monthly_income_inr=u["monthly_income_inr"],
                has_bank_account=u["has_bank_account"],
                has_formal_credit_history=u["has_formal_credit_history"],
                kyc_status=u["kyc_status"],
                language_pref=u["language_pref"],
                aadhaar_linked=u["aadhaar_linked"],
            )
            db.add(user)
            await db.flush()
            ref_to_id[u["user_id"]] = user.id
        await db.commit()

        for t in txns_data:
            db.add(
                Transaction(
                    txn_ref=t["txn_id"],
                    user_id=ref_to_id[t["user_id"]],
                    amount_inr=t["amount_inr"],
                    txn_type=t["type"],
                    merchant=t["merchant"],
                    location=t["location"],
                    device_id=t["device_id"],
                    is_flagged=t["is_flagged"],
                )
            )

        for l in loans_data:
            db.add(
                LoanApplication(
                    application_ref=l["application_id"],
                    user_id=ref_to_id[l["user_id"]],
                    loan_type=l["loan_type"],
                    requested_amount_inr=l["requested_amount_inr"],
                    purpose=l["purpose"],
                    collateral_provided=l["collateral_provided"],
                    gst_data_available=l["gst_data_available"],
                    bank_statement_months_provided=l["bank_statement_months_provided"],
                    status=l["status"],
                )
            )

        for c in claims_data:
            db.add(
                InsuranceClaim(
                    claim_ref=c["claim_id"],
                    user_id=ref_to_id[c["user_id"]],
                    policy_type=c["policy_type"],
                    trigger_event=c["trigger_event"],
                    region=c["region"],
                    claim_amount_inr=c["claim_amount_inr"],
                    parametric_data_source=c["parametric_data_source"],
                    auto_triggered=c["auto_triggered"],
                    status=c["status"],
                )
            )

        await db.commit()

    return ref_to_id


async def seed_mongo() -> None:
    kyc_data = _load("kyc_documents.json")
    db = get_mongo_db()
    for doc in kyc_data:
        await db[COLLECTION_KYC_DOCUMENTS].update_one({"user_id": doc["user_id"]}, {"$set": doc}, upsert=True)


async def main() -> None:
    await init_postgres()
    await ensure_indexes()
    ref_map = await seed_postgres()
    await seed_mongo()
    print(f"Seeded {len(ref_map)} users + related records into Postgres, KYC docs into MongoDB.")


if __name__ == "__main__":
    asyncio.run(main())
