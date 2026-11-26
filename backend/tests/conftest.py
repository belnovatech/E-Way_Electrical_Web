# Creates eway_test_db from schema.sql + seed.sql, runs the app against it, drops it after.
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[2]
TEST_DB = "eway_test_db"
DEMO_OTP = "123456"
STAFF_PASSWORD = "Admin@2026!"
SUPER_ADMIN = ("superadmin@ewaytoyourevs.in", "SuperAdmin@2026!")


def _base_url() -> str:
    from app.config import Settings
    return os.environ.get("TEST_DATABASE_SERVER_URL") or Settings().database_url


@pytest.fixture(scope="session")
def test_db_url():
    base = _base_url()
    admin_url, test_url = make_conninfo(base, dbname="postgres"), make_conninfo(base, dbname=TEST_DB)
    with psycopg.connect(admin_url, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {TEST_DB} ENCODING 'UTF8' TEMPLATE template0")
    with psycopg.connect(test_url, autocommit=True) as c:
        c.execute((ROOT / "database" / "schema.sql").read_text(encoding="utf-8"))
        c.execute((ROOT / "database" / "seed.sql").read_text(encoding="utf-8"))
    os.environ.update({
        "DATABASE_URL": test_url, "JWT_SECRET": "test-secret-not-for-production-0123456789",
        "OCPP_GATEWAY_KEY": "test-gateway-key", "SIMULATION_SPEEDUP": "3600", "OTP_DEMO_MODE": "true",
        "APP_ENV": "test",
    })
    from app.config import get_settings
    get_settings.cache_clear()
    yield test_url
    from app import db
    db.close_pool()
    with psycopg.connect(admin_url, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


@pytest.fixture(scope="session")
def client(test_db_url):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def sql(test_db_url):
    def run(query: str, params=None):
        with psycopg.connect(test_db_url, autocommit=True) as c:
            return c.execute(query, params).fetchall()
    return run


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def customer_login(client):
    def login(phone: str) -> dict:
        r = client.post("/v1/auth/send-otp", json={"countryCode": "+91", "phoneNumber": phone})
        assert r.status_code in (200, 429), r.text
        r = client.post("/v1/auth/verify-otp", json={"countryCode": "+91", "phoneNumber": phone, "otp": DEMO_OTP})
        assert r.status_code == 200, r.text
        return bearer(r.json()["token"])
    return login


@pytest.fixture(scope="session")
def staff_login(client):
    def login(email: str, password: str = STAFF_PASSWORD, role: str = "admin") -> dict:
        r = client.post("/v1/auth/login", json={"email": email, "password": password, "role": role})
        assert r.status_code == 200, r.text
        return bearer(r.json()["token"])
    return login


@pytest.fixture(scope="session")
def bala(customer_login):
    return customer_login("8074407557")


@pytest.fixture(scope="session")
def super_admin(staff_login):
    return staff_login(*SUPER_ADMIN, role="super_admin")


@pytest.fixture(scope="session")
def ravi(staff_login):            # ADM-AP01, Zone Operations Manager for AP-Z01 + AP-Z08
    return staff_login("ravi.kumar@powergrid.in")


@pytest.fixture(scope="session")
def ops_super(staff_login):       # STF-001, super admin using the admin console
    return staff_login("admin@powergrid.in")
