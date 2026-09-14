from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    user_ref: str = Field(..., min_length=1, max_length=20)
    otp: str = Field(..., min_length=4, max_length=8)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=10)


class InternalTokenRequest(BaseModel):
    """Issues an analyst/admin token. Gated by a shared bootstrap key, not
    by OTP - this is meant for internal tooling/service accounts, not
    end-customer login. Swap for proper SSO/Cognito-group-based issuance
    before production."""
    admin_bootstrap_key: str = Field(..., min_length=1)
    user_ref: str = Field(..., min_length=1, max_length=20)
    role: str = Field(..., pattern="^(analyst|admin)$")
