# Settings from env. Order (later wins): repo/.env.local, backend/.env, real env vars.
from __future__ import annotations

import logging
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent

log = logging.getLogger("eway")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_DIR / ".env.local", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"            # development | production
    database_url: str
    db_pool_min: int = 1
    db_pool_max: int = 10

    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    customer_token_days: int = 30
    staff_token_hours: int = 12             # absolute lifetime; idle timeout comes from system_settings

    cors_origins: str = "http://localhost:3000,http://localhost:5173"

    # demo mode: OTP is always demo_otp and gets sent back in the response.
    # switch off once SMS is hooked up (see TODO in routers/auth.py)
    otp_demo_mode: bool = True
    demo_otp: str = "123456"
    otp_ttl_seconds: int = 300
    otp_resend_cooldown_seconds: int = 29
    otp_max_attempts: int = 5

    # fake the kWh from charger power + elapsed time. turn off when real chargers
    # send meter values to /ocpp
    charging_simulation: bool = True
    simulation_speedup: float = 1.0
    charging_efficiency: float = 0.92

    ocpp_gateway_key: str = ""              # shared secret for /ocpp/*; empty disables those routes

    payment_provider: str = "sandbox"       # only the sandbox provider is implemented
    currency: str = "INR"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if not s.jwt_secret:
        if s.is_production:
            raise RuntimeError("JWT_SECRET must be set in production")
        s.jwt_secret = secrets.token_urlsafe(48)
        log.warning("JWT_SECRET not set - using a random secret; tokens will not survive a restart")
    if s.is_production and s.otp_demo_mode:
        raise RuntimeError("OTP_DEMO_MODE must be false in production")
    return s
