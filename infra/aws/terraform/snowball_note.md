# AWS Snowball — where it actually fits

Snowball is physical, offline bulk data-transfer hardware (50TB/80TB
devices shipped to you). It is **not** part of the live request path of
this backend — it doesn't belong in an API call chain.

It fits exactly one scenario here:

**One-time bulk migration of historical data** — e.g. migrating years of
existing core-banking transaction history, scanned KYC archives, or
branch-level paper-digitized records into S3 (`sahayak-data-lake`)
during initial platform onboarding, when the volume is too large or the
site's internet link too slow for a network transfer.

Workflow:
1. Order a Snowball Edge device via the AWS Console/API for the target region (`ap-south-1`).
2. Copy the historical dataset onto the device on-site (bank branch/data center).
3. Ship it back to AWS; data lands directly in the `sahayak-data-lake` S3 bucket.
4. Trigger a Glue crawler / batch job to catalog and load it into the analytics pipeline.

For ongoing, day-to-day data movement (nightly transaction exports, KYC
document uploads, model training data refreshes), use standard S3
`PutObject` (as in `S3Service.export_to_data_lake`) or AWS DataSync —
not Snowball. Snowball is scoped to the initial migration only.
