from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import EmailStr, TypeAdapter

from src.core.config import ROOT, Settings

CLAIMS = (
    "iss",
    "aud",
    "sub",
    "jti",
    "iat",
    "nbf",
    "exp",
    "type",
    "email",
    "role",
    "is_active",
    "scope",
)
Role = Literal["STUDENT", "COMPANY", "ADMIN"]
principal_role = TypeAdapter(Role)
email_address = TypeAdapter(EmailStr)
bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    id: UUID
    email: str
    role: Role
    scopes: frozenset[str]


def load_verification_key(settings: Settings) -> RSAPublicKey | None:
    try:
        if settings.jwt_public_key:
            pem = settings.jwt_public_key.replace("\\n", "\n").encode()
        else:
            pem = (ROOT / settings.jwt_public_key_path).read_bytes()
        key = load_pem_public_key(pem)
        if isinstance(key, RSAPublicKey) and key.key_size >= 2048:
            return key
    except (OSError, ValueError, UnsupportedAlgorithm):
        pass
    return None


def authenticated_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    key = request.app.state.verification_key
    settings = request.app.state.settings
    unauthorized = HTTPException(
        401, "Invalid or missing access token.", headers={"WWW-Authenticate": "Bearer"}
    )
    if key is None or credentials is None:
        raise unauthorized
    try:
        claims = jwt.decode(
            credentials.credentials,
            key,
            algorithms=["RS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audiences,
            options={"require": CLAIMS},
        )
        if any(type(claims[field]) is not int for field in ("exp", "iat", "nbf")):
            raise ValueError
        if not isinstance(claims["aud"], list) or not all(
            isinstance(item, str) for item in claims["aud"]
        ):
            raise ValueError
        if claims["type"] != "access" or claims["is_active"] is not True:
            raise ValueError
        if not all(
            isinstance(claims[field], str) for field in ("sub", "jti", "email", "scope")
        ):
            raise ValueError
        UUID(claims["jti"])
        return Principal(
            UUID(claims["sub"]),
            email_address.validate_python(claims["email"]),
            principal_role.validate_python(claims["role"]),
            frozenset(claims["scope"].split()),
        )
    except (jwt.InvalidTokenError, ValueError, TypeError, OverflowError):
        raise unauthorized from None
