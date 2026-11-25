from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    EmailStr,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
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
    smtp_host: str | None = None
    smtp_port: int = Field(default=1025, ge=1, le=65535)
    smtp_username: SecretStr | None = None
    smtp_password: SecretStr | None = None
    smtp_from_email: EmailStr | None = None
    smtp_from_name: str = "Conecta Nexus"
    smtp_tls_mode: Literal["plain", "starttls", "tls"] = "plain"
    smtp_timeout: float = Field(default=10, gt=0, le=120, allow_inf_nan=False)
    smtp_retry_base_delay: float = Field(default=1, gt=0, le=60, allow_inf_nan=False)
    frontend_base_url: AnyHttpUrl | None = None

    @field_validator(
        "smtp_host",
        "smtp_username",
        "smtp_password",
        "smtp_from_email",
        "frontend_base_url",
        mode="before",
    )
    @classmethod
    def empty_optional_email_settings(cls, value):
        if value == "":
            return None
        return value

    @field_validator("smtp_from_email", mode="before")
    @classmethod
    def safe_sender(cls, value):
        if value is not None and (
            not isinstance(value, str)
            or not value.isascii()
            or any(ch.isspace() or ch in "<>" for ch in value)
        ):
            raise ValueError("Provide one plain ASCII sender address.")
        return value

    @field_validator("smtp_from_name")
    @classmethod
    def safe_sender_name(cls, value):
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("Sender name contains a control character.")
        return value

    @field_validator("frontend_base_url")
    @classmethod
    def frontend_origin(cls, value):
        if value and (
            value.username
            or value.password
            or value.path != "/"
            or value.query is not None
            or value.fragment is not None
        ):
            raise ValueError(
                "Provide a frontend origin without credentials, path, query or fragment."
            )
        return value

    @model_validator(mode="after")
    def smtp_transport_policy(self):
        if self.smtp_host:
            if not self.smtp_host.isascii() or any(
                ch.isspace() or ord(ch) < 32 for ch in self.smtp_host
            ):
                raise ValueError("Provide a valid SMTP host.")
            if bool(self.smtp_username) != bool(self.smtp_password):
                raise ValueError("Configure both SMTP credentials or neither.")
            if self.smtp_tls_mode == "plain" and (
                self.smtp_username or self.app_env == "production"
            ):
                raise ValueError(
                    "SMTP credentials and production delivery require TLS."
                )
        return self

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
