"""
Disaster-recovery services.

Primary infrastructure runs on AWS (see app/services/aws/, infra/aws/).
Two independent secondary/DR sites are supported, mirrored at the code
level (not just documentation):

  - GCP  (app/services/dr/gcp_dr_service.py)   — infra/gcp_dr/
  - Azure (app/services/dr/azure_dr_service.py) — infra/azure_dr/

Both follow the same pattern as app/services/aws/*: production-shaped
SDK calls wrapped so local dev without cloud credentials never breaks —
a missing/invalid credential degrades a health check to "unreachable"
rather than raising and taking the process down.
"""
