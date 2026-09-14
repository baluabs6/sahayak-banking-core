"""
Unit tests for ensure_owner_or_role() — the object-level-authorization
check that closes the IDOR gap described in app/core/auth_middleware.py
(a route previously trusted a client-supplied user_ref at face value).
Pure function of (identity, requested_user_ref, allowed_roles); no auth
middleware or live request needed to exercise it.
"""
from app.core.auth_middleware import ensure_owner_or_role


def _identity(sub: str, role: str) -> dict:
    return {"sub": sub, "role": role}


def test_customer_may_act_on_own_record():
    deny = ensure_owner_or_role(_identity("USR-1001", "customer"), "USR-1001", allowed_roles=("analyst", "admin"))
    assert deny is None


def test_customer_may_not_act_on_another_customers_record():
    deny = ensure_owner_or_role(_identity("USR-1001", "customer"), "USR-9999", allowed_roles=("analyst", "admin"))
    assert deny is not None  # a 403 Response


def test_analyst_may_act_cross_user_when_role_is_allowed():
    deny = ensure_owner_or_role(_identity("ANALYST-1", "analyst"), "USR-9999", allowed_roles=("analyst", "admin"))
    assert deny is None


def test_analyst_denied_when_route_does_not_allow_the_role():
    # e.g. a route that's owner-only and never lists "analyst" as allowed.
    deny = ensure_owner_or_role(_identity("ANALYST-1", "analyst"), "USR-9999", allowed_roles=())
    assert deny is not None


def test_allowed_roles_is_trusted_as_given_by_the_route():
    # ensure_owner_or_role() itself doesn't second-guess which roles a route
    # declares as allowed — that's a per-route decision the route author
    # makes deliberately (e.g. fraud-event review explicitly lists
    # "analyst"). This test documents that the function does exactly what
    # its allowed_roles argument says, so a route accidentally passing
    # ("customer",) would incorrectly permit cross-user access — the
    # safeguard against that is code review of each route's allowed_roles,
    # not this function.
    deny = ensure_owner_or_role(_identity("USR-1001", "customer"), "USR-9999", allowed_roles=("customer",))
    assert deny is None
