# Using sync psycopg on purpose - the async driver doesn't work on the default
# Windows event loop. FastAPI runs plain def endpoints in a threadpool anyway.
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from psycopg import Cursor
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

_pool: ConnectionPool | None = None


def open_pool() -> None:
    global _pool
    if _pool is not None:
        return
    s = get_settings()
    _pool = ConnectionPool(
        conninfo=s.database_url,
        min_size=s.db_pool_min,
        max_size=s.db_pool_max,
        kwargs={
            "row_factory": dict_row,
            # all date maths ("today", "this month") happen in India time
            "options": "-c TimeZone=Asia/Kolkata",
        },
        open=True,
    )
    _pool.wait(timeout=15)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def tx() -> Iterator[Cursor[dict[str, Any]]]:
    # commits on success, rolls back if anything raises
    if _pool is None:
        open_pool()
    assert _pool is not None
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            yield cur


def one(cur: Cursor, sql: str, params: Any = None) -> dict[str, Any] | None:
    cur.execute(sql, params)
    return cur.fetchone()


def all_rows(cur: Cursor, sql: str, params: Any = None) -> list[dict[str, Any]]:
    cur.execute(sql, params)
    return cur.fetchall()


def scalar(cur: Cursor, sql: str, params: Any = None) -> Any:
    cur.execute(sql, params)
    row = cur.fetchone()
    return None if row is None else next(iter(row.values()))
