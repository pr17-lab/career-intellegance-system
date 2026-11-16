"""
Pydantic schemas for the Authentication module (login, registration, user responses).
"""

from uuid import UUID
from typing import Optional
from pydantic import BaseModel, EmailStr, Field, field_validator


BCRYPT_MAX_BYTES = 72


def _check_password_bytes(value: str) -> str:
    """Reject (never truncate) passwords bcrypt cannot hash: over 72 bytes as UTF-8."""
    if len(value.encode('utf-8')) > BCRYPT_MAX_BYTES:
        raise ValueError(
            f"Password must be at most {BCRYPT_MAX_BYTES} bytes when encoded as UTF-8 "
            "(fewer characters if it contains emoji or non-ASCII letters)."
        )
    return value


# ---------------------------------------------------------------------------
# Token / auth response
# ---------------------------------------------------------------------------

class Token(BaseModel):
    access_token: str
    token_type: str


class UserResponse(BaseModel):
    id: UUID
    email: str
    student_id: str | None = None
    name: str | None = None
    branch: str | None = None
    semester: int | None = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Self-service update schemas
# ---------------------------------------------------------------------------

class UpdateProfileRequest(BaseModel):
    """Payload for PATCH /auth/me — student updates their own profile."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    semester: Optional[int] = Field(None, ge=1, le=8)


class ChangePasswordRequest(BaseModel):
    """Payload for PUT /auth/change-password."""
    current_password: str
    new_password: str = Field(..., min_length=8)

    @field_validator('new_password')
    @classmethod
    def _new_password_bytes(cls, v: str) -> str:
        return _check_password_bytes(v)


# ---------------------------------------------------------------------------
# Registration payload schemas
# ---------------------------------------------------------------------------

class StudentRegistration(BaseModel):
    full_name: str
    email: EmailStr
    student_id: str | None = None
    password: str
    department: str
    batch_year: int
    current_semester: int

    @field_validator('password')
    @classmethod
    def _password_bytes(cls, v: str) -> str:
        return _check_password_bytes(v)
