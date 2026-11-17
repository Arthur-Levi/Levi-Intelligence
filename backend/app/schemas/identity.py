from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints
from app.models.identity import MembershipRole
from typing import Annotated

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", min_length=3, max_length=80)]


class RegisterRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=160)
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    organization_name: str = Field(min_length=2, max_length=160)
    organization_slug: Slug


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    organization_slug: Slug


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    email: EmailStr
    full_name: str


class OrganizationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    slug: str


class CurrentIdentityResponse(BaseModel):
    user: UserResponse
    organization: OrganizationResponse
    role: str


class MembershipRoleUpdateRequest(BaseModel):
    role: str

    @property
    def parsed_role(self):
        from app.models.identity import MembershipRole

        try:
            return MembershipRole(self.role)
        except ValueError:
            raise ValueError("Invalid membership role") from None
