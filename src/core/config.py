from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

ROOT = Path(__file__).resolve().parents[2]
ProducerSecret = Annotated[SecretStr, Field(min_length=32)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    database_url: SecretStr
    db_echo: bool = False
    app_env: Literal["development", "testing", "production"] = "development"
    host: str = Field(default="0.0.0.0", min_length=1)
    port: int = Field(default=8000, ge=1, le=65535)
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)
    db_pool_timeout: float = Field(default=5, gt=0, le=120)
    db_pool_recycle: int = Field(default=1800, ge=1)
    db_connect_timeout: float = Field(default=5, gt=0, le=120)
    db_health_timeout: float = Field(default=2, gt=0, le=30)
    jwt_public_key: str = ""
    jwt_public_key_path: Path = Path("certs/jwt_public.pem")
    jwt_issuer: str = Field(
        default="https://auth.conectanexus.ufps.edu.co", min_length=1
    )
    jwt_audiences: list[str] = Field(
        default_factory=lambda: ["conecta-nexus-gateway", "conecta-nexus-services"],
        min_length=1,
    )
    application_event_secret: ProducerSecret | None = None
    challenge_event_secret: ProducerSecret | None = None

    @field_validator(
        "application_event_secret", "challenge_event_secret", mode="before"
    )
    @classmethod
    def disable_empty_producer_secret(cls, value):
        return None if value == "" else value

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, secret: SecretStr) -> SecretStr:
        try:
            url = make_url(secret.get_secret_value())
        except (ArgumentError, ValueError):
            raise ValueError("Provide a valid PostgreSQL connection URL.") from None
        if url.drivername not in {"postgres", "postgresql", "postgresql+asyncpg"}:
            raise ValueError("Only PostgreSQL with asyncpg is supported.")
        if not url.database:
            raise ValueError("Specify a PostgreSQL database name.")
        return SecretStr(
            url.set(drivername="postgresql+asyncpg").render_as_string(
                hide_password=False
            )
        )

    @field_validator("jwt_audiences")
    @classmethod
    def validate_audiences(cls, audiences: list[str]) -> list[str]:
        if any(not audience or audience != audience.strip() for audience in audiences):
            raise ValueError("Provide nonempty audience names.")
        return audiences
