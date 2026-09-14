"""
Request-level authentication + ownership authorization for BlackSheep.

This closes the gap the codebase had: `core/security.py` could issue and
verify JWTs, but no route ever checked one, so any caller could pass any
`user_ref` in a request body and read/act on someone else's financial data
(classic IDOR / broken access control).

Usage in a controller:

    from app.core.auth_middleware import require_identity, ensure_owner_or_role

    @post("/credit-score")
    async def compute_credit_score(self, request: Request) -> json:
        identity = require_identity(request)          # 401 if missing/invalid
        payload = await request.json()
        user_ref = payload.get("user_ref")
        deny = ensure_owner_or_role(identity, user_ref, allowed_roles=("analyst", "admin"))
        if deny:
            return deny                                 # 403 Response
        ...
"""
from blacksheep import Request, Response, json

from app.core.security import verify_access_token

# Paths that must remain reachable without a token.
PUBLIC_PATH_PREFIXES = (
    "/health",
    "/openapi",
    "/docs",
    "/api/v1/auth/",  # login/refresh/internal-token issuance itself
    "/",  # exact match handled separately below
)


def is_public_path(path: str) -> bool:
    if path == "/":
        return True
    return any(path.startswith(p) for p in PUBLIC_PATH_PREFIXES if p != "/")


async def auth_middleware(request: Request, handler):
    """Register via `app.middlewares.append(auth_middleware)` in main.py.
    Verifies the Bearer token up front and stashes the decoded identity on
    the request so controllers don't have to re-parse it. Controllers still
    call `require_identity`/`ensure_owner_or_role` for per-route enforcement
    (object-level authorization can't be generalized in middleware alone,
    since it depends on which user_ref the route touches)."""
    path = request.url.path.decode() if isinstance(request.url.path, bytes) else request.url.path

    if is_public_path(path):
        return await handler(request)

    auth_header = request.get_first_header(b"Authorization")
    if not auth_header:
        return json({"error": "Missing Authorization header"}, status=401)

    header_value = auth_header.decode() if isinstance(auth_header, bytes) else auth_header
    if not header_value.startswith("Bearer "):
        return json({"error": "Authorization header must be a Bearer token"}, status=401)

    token = header_value[len("Bearer "):].strip()
    identity = verify_access_token(token)
    if identity is None:
        return json({"error": "Invalid or expired token"}, status=401)

    # Stash on the request for controllers to read via require_identity().
    request.identity = identity  # type: ignore[attr-defined]
    return await handler(request)


def require_identity(request: Request) -> dict:
    """Fetch the identity the middleware attached. Raises if somehow called
    on an unauthenticated request (shouldn't happen if middleware is wired
    up correctly) - fail loud rather than silently treating as anonymous."""
    identity = getattr(request, "identity", None)
    if identity is None:
        raise RuntimeError(
            "require_identity() called without auth_middleware having run - "
            "check app.middlewares registration in main.py."
        )
    return identity


def ensure_owner_or_role(identity: dict, requested_user_ref: str | None, allowed_roles: tuple[str, ...] = ()) -> Response | None:
    """Core IDOR fix. Returns a 403 Response if the caller may NOT act on
    `requested_user_ref`, or None if they may proceed.

    A "customer" token may only touch its own user_ref (identity["sub"]).
    "analyst"/"admin" tokens may touch any user_ref *if* their role is in
    `allowed_roles` for this particular endpoint (e.g. fraud-event review
    needs analyst access; a customer never should).
    """
    role = identity.get("role")
    own_ref = identity.get("sub")

    if requested_user_ref and requested_user_ref == own_ref:
        return None  # acting on your own record is always fine

    if role in allowed_roles:
        return None  # elevated role explicitly permitted for this route

    return json(
        {"error": "Forbidden: you may not access this user's data with your current role/token."},
        status=403,
    )
