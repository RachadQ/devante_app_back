from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "production"] = "production"
    service_name: str = "internal-portal-administration-backend"
    mongodb_uri: str
    jwt_secret: str = Field(min_length=32)
    session_secret: str = Field(min_length=32)
    jwt_issuer: str = "internal-portal-administration"
    jwt_ttl_minutes: int = Field(default=60, ge=5, le=1440)
    frontend_url: str
    cors_allowed_origins: str
    trusted_hosts: str
    auth_cookie_secure: bool = True
    session_cookie_secure: bool = True
    auth_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_tenant_id: str = ""
    dev_auth_bypass: bool = False
    dev_auth_email: str = "developer@localhost"
    dev_auth_name: str = "Local Developer"
    allowed_email_domains: str = ""
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, ge=1024)
    ocr_engine: Literal["paddle", "rapid"] = "paddle"
    google_drive_credentials_json: str = ""
    google_drive_root_folder_id: str = ""
    local_upload_directory: str = "uploads"

    @field_validator("mongodb_uri")
    @classmethod
    def validate_mongodb_uri(cls, value: str) -> str:
        if not value.startswith(("mongodb://", "mongodb+srv://")):
            raise ValueError("MONGODB_URI must be a MongoDB connection URI")
        return value

    @field_validator("frontend_url")
    @classmethod
    def validate_frontend_url(cls, value: str) -> str:
        clean = value.strip().rstrip("/")
        parsed = urlparse(clean)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("FRONTEND_URL must be an absolute HTTP(S) origin without credentials")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("FRONTEND_URL must not contain a path, query, or fragment")
        return clean

    @field_validator("cors_allowed_origins")
    @classmethod
    def validate_cors_origins(cls, value: str) -> str:
        for item in (part.strip() for part in value.split(",")):
            if not item:
                continue
            parsed = urlparse(item)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Every CORS origin must be an absolute HTTP(S) origin")
            if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise ValueError("CORS origins must not contain paths, queries, or fragments")
        return value

    @property
    def origins(self) -> list[str]:
        return [item.strip().rstrip("/") for item in self.cors_allowed_origins.split(",") if item.strip()]

    @property
    def hosts(self) -> list[str]:
        return [item.strip() for item in self.trusted_hosts.split(",") if item.strip()]

    @property
    def email_domains(self) -> set[str]:
        return {item.strip().lower().lstrip("@") for item in self.allowed_email_domains.split(",") if item.strip()}

    def validate_production(self) -> None:
        if self.app_env == "development":
            loopback_hosts = {"localhost", "127.0.0.1", "::1", "testserver"}
            if urlparse(self.frontend_url).hostname not in loopback_hosts:
                raise RuntimeError("Development FRONTEND_URL must use a loopback host")
            if any(urlparse(origin).hostname not in loopback_hosts for origin in self.origins):
                raise RuntimeError("Development CORS origins must use loopback hosts")
            if any(host not in loopback_hosts for host in self.hosts):
                raise RuntimeError("Development TRUSTED_HOSTS must contain only loopback hosts")
            return
        if self.jwt_secret == self.session_secret:
            raise RuntimeError("JWT_SECRET and SESSION_SECRET must be different in production")
        if not self.auth_cookie_secure or not self.session_cookie_secure:
            raise RuntimeError("Secure cookies are required in production")
        if not self.microsoft_client_id or not self.microsoft_client_secret:
            raise RuntimeError("Microsoft OAuth credentials are required in production")
        if self.microsoft_tenant_id.strip().lower() in {"common", "organizations", "consumers"}:
            raise RuntimeError("A tenant-specific Microsoft tenant ID is required in production")
        if self.dev_auth_bypass:
            raise RuntimeError("Development authentication bypass is forbidden in production")
        if "*" in self.origins:
            raise RuntimeError("Wildcard CORS is forbidden in production")
        if urlparse(self.frontend_url).scheme != "https":
            raise RuntimeError("FRONTEND_URL must use HTTPS in production")
        if any(urlparse(origin).scheme != "https" for origin in self.origins):
            raise RuntimeError("Every production CORS origin must use HTTPS")
        if "*" in self.hosts or not self.hosts:
            raise RuntimeError("Production TRUSTED_HOSTS must contain explicit hostnames")
        if self.auth_cookie_samesite == "none" and not self.auth_cookie_secure:
            raise RuntimeError("SameSite=None requires secure authentication cookies")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()  # type: ignore[call-arg]
    settings.validate_production()
    return settings
