# All errors go out as {"success": false, "message", "code"} - the frontend
# apiClient reads `message`. DB constraint errors get turned into 409/422.
from __future__ import annotations

import logging

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import errors as pg
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("eway")


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code or {
            400: "bad_request", 401: "unauthorized", 402: "payment_required", 403: "forbidden",
            404: "not_found", 409: "conflict", 422: "invalid", 423: "locked", 429: "too_many_requests",
        }.get(status, "error")


def not_found(what: str) -> ApiError:
    return ApiError(404, f"{what} not found")


CONSTRAINT_MESSAGES = {
    "ex_reservation_charger_overlap": "This charger is already booked for an overlapping time slot.",
    "ex_reservation_vehicle_overlap": "This vehicle already has a booking that overlaps this time slot.",
    "ux_sessions_live_point": "This charger already has an active charging session.",
    "ux_sessions_live_vehicle": "This vehicle is already charging.",
    "ux_vehicles_plate": "A vehicle with this licence plate is already registered.",
    "ux_vehicles_one_default": "Only one vehicle can be the default vehicle.",
    "ux_payment_methods_one_default": "Only one payment method can be the default.",
    "ex_tariff_overlap": "An active tariff with the same zone, vehicle type and priority already covers these dates.",
    "ex_settlement_period_overlap": "A settlement already covers part of this period for this admin and area.",
    "ux_users_email": "This email address is already registered.",
    "ux_users_phone": "This phone number is already registered.",
    "fk_customer_admin_zone": "The selected admin does not manage this customer's area.",
    "fk_collection_admin_zone": "The admin is not assigned to this area.",
    "fk_settlement_admin_zone": "The admin is not assigned to this area.",
    "fk_collection_settlement": "The collection and the settlement batch belong to different admins or areas.",
    "wallets_balance_check": "Insufficient wallet balance.",
    "zones_code_key": "A zone with this code already exists.",
    "stations_code_key": "A station with this code already exists.",
    "stations_zone_id_name_key": "A station with this name already exists in the zone.",
    "charging_points_point_code_key": "A charging point with this code already exists.",
    "charging_points_ocpp_id_key": "This OCPP ID is already used by another charger.",
    "charging_points_station_id_charger_number_key": "This charger number already exists at the station.",
    "roles_name_key": "A role with this name already exists.",
    "roles_code_key": "A role with this code already exists.",
    "tariff_plans_code_key": "A tariff with this code already exists.",
    "admin_settlements_payment_reference_utr_key": "This UTR number has already been used for another settlement.",
    "users_public_code_key": "This code is already in use.",
}


def _db_error_response(exc: psycopg.Error) -> JSONResponse:
    diag = exc.diag
    constraint = diag.constraint_name or ""
    primary = diag.message_primary or str(exc)
    friendly = CONSTRAINT_MESSAGES.get(constraint)

    if isinstance(exc, (pg.UniqueViolation, pg.ExclusionViolation)):
        status, code = 409, "conflict"
    elif isinstance(exc, pg.RestrictViolation) or (
        isinstance(exc, pg.ForeignKeyViolation) and "update or delete" in primary
    ):
        status, code = 409, "in_use"
        friendly = friendly or "This record is still referenced by other data and cannot be removed."
    elif isinstance(exc, pg.ForeignKeyViolation):
        status, code = 422, "invalid_reference"
    elif isinstance(exc, (pg.CheckViolation, pg.NotNullViolation, pg.InvalidTextRepresentation,
                          pg.NumericValueOutOfRange, pg.StringDataRightTruncation, pg.InvalidDatetimeFormat,
                          pg.DatetimeFieldOverflow)):
        status, code = 422, "invalid"
    elif isinstance(exc, pg.IntegrityConstraintViolation):   # custom trigger errors (23000)
        status, code = 409, "conflict"
    elif isinstance(exc, pg.RaiseException):
        status, code = 400, "rejected"
    else:
        log.exception("database error", exc_info=exc)
        return JSONResponse(status_code=500, content={"success": False, "message": "Database error.", "code": "db_error"})

    return JSONResponse(status_code=status, content={"success": False, "message": friendly or primary, "code": code})


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content={"success": False, "message": exc.message, "code": exc.code})

    @app.exception_handler(psycopg.Error)
    async def _db_error(_: Request, exc: psycopg.Error):
        return _db_error_response(exc)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        return JSONResponse(status_code=exc.status_code, content={"success": False, "message": str(exc.detail), "code": "http_error"})

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        errors = exc.errors()
        first = errors[0] if errors else {}
        field = ".".join(str(p) for p in first.get("loc", []) if p not in ("body", "query", "path"))
        message = f"{field}: {first.get('msg')}" if field else str(first.get("msg", "Invalid request"))
        return JSONResponse(status_code=422, content={
            "success": False, "message": message, "code": "validation_error",
            "errors": [{"field": ".".join(map(str, e.get("loc", []))), "message": e.get("msg")} for e in errors],
        })
