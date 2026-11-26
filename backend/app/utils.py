from __future__ import annotations

import re
import secrets
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from .errors import ApiError

TWO_PLACES = Decimal("0.01")


class CamelModel(BaseModel):
    # takes camelCase or snake_case, ignores extra keys
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore",
                              str_strip_whitespace=True)


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def new_code(prefix: str) -> str:
    # e.g. RSV-20261108-7F3K9Q2A. not guaranteed unique, the unique index catches clashes
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    return f"{prefix}-{datetime.now():%Y%m%d}-" + "".join(secrets.choice(alphabet) for _ in range(8))


def parse_enum(value: str | None, allowed: set[str], field: str) -> str | None:
    """'In Progress' / 'IN_PROGRESS' / 'in-progress' -> 'in_progress' (if allowed)."""
    if value is None:
        return None
    norm = re.sub(r"[\s\-]+", "_", value.strip()).lower()
    if norm not in allowed:
        raise ApiError(422, f"{field} must be one of: {', '.join(sorted(allowed))}")
    return norm


VEHICLE_TYPES = {"car", "bike", "bus"}
CONNECTORS = {"ccs2": "CCS2", "type 2": "Type 2", "type2": "Type 2", "gb/t": "GB/T", "gbt": "GB/T", "chademo": "CHAdeMO"}


def parse_vehicle_type(value: str | None) -> str | None:
    return parse_enum(value, VEHICLE_TYPES, "vehicleType")


def parse_connector(value: str | None) -> str | None:
    if value is None:
        return None
    key = value.strip().lower().replace("_", " ")
    if key not in CONNECTORS:
        raise ApiError(422, "connector must be one of: CCS2, Type 2, GB/T, CHAdeMO")
    return CONNECTORS[key]


PHONE_RE = re.compile(r"^[6-9][0-9]{9}$")


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if not PHONE_RE.match(digits):
        raise ApiError(422, "Enter a valid 10-digit Indian mobile number.")
    return digits


PLATE_RE = re.compile(r"^[A-Z]{2}[0-9]{1,2}[A-Z]{0,3}[0-9]{1,4}$")


def normalize_plate(raw: str) -> str:
    # keep the spacing they typed, but validate without it (AP39AB1234)
    plate = re.sub(r"\s+", "-", raw.strip().upper())
    if not PLATE_RE.match(re.sub(r"[^A-Z0-9]", "", plate)):
        raise ApiError(422, "Enter a valid vehicle registration number, e.g. AP-39-AB-1234.")
    return plate


def initials(name: str | None) -> str:
    parts = [p for p in re.split(r"[\s.]+", name or "") if p]
    return ("".join(p[0] for p in parts[:2]) or "EV").upper()


def mask_phone(phone: str | None) -> str | None:
    return None if not phone else f"{phone[:2]}******{phone[-2:]}"


def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return email
    user, domain = email.split("@", 1)
    return f"{user[:2]}***@{domain}"


def clamp_limit(limit: int | None, default: int = 100, maximum: int = 500) -> int:
    if limit is None:
        return default
    return max(1, min(maximum, limit))
