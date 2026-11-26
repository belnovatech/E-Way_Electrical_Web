# E-Way API. Run from backend/:  uvicorn app.main:app --reload --port 8000
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import db
from .config import get_settings
from .errors import install_error_handlers
from .routers import admin, auth, charging, customer, notifications, ocpp, public, roles, super_admin, wallet

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.open_pool()
    yield
    db.close_pool()


settings = get_settings()
app = FastAPI(
    title="E-Way Electrical API",
    version="1.0.0",
    description="Backend for the E-Way / VoltGrid EV charging platform (customer, area-admin and super-admin portals).",
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/v1/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
install_error_handlers(app)

v1 = APIRouter(prefix="/v1")
for r in (auth.router, public.router, customer.router, customer.legacy, charging.router, wallet.router,
          notifications.router, admin.router, roles.router, super_admin.router, ocpp.router):
    v1.include_router(r)
app.include_router(v1)


@app.get("/health", tags=["Health"])
def health():
    with db.tx() as cur:
        cur.execute("SELECT 1")
    return {"status": "ok", "database": "ok", "environment": settings.app_env}
