"""Quality-control issuance of observation batches.

The analyst signs off a set of temperature / pH readings before the analysis
database is loaded.  A batch is issued only when *every* requested sample
exists and both readings are non-NULL and inside the submitted closed
intervals.  Issuance snapshots the exact readings that were checked; later
legacy corrections never change an issued batch.

Concurrency
===========
A long batch is checked in two phases:

1. **Snapshot check** -- a read-only transaction on ``samples``.  It never
   takes SQLite's write lock, so the legacy acquisition program can keep
   committing corrections while a large batch is checked.  Before cutover the
   wide table is read; after cutover the compatibility view is read.  A single
   transaction picks exactly one source and never mixes a partially-copied
   long table with the wide table.
2. **Commit re-check** -- a short ``BEGIN IMMEDIATE`` transaction that re-reads
   the same samples and validates them again against the same intervals, then
   inserts the batch and its snapshotted rows atomically.  If a legacy
   correction committed after phase 1, this either passes with the newest
   consistent values or rejects the request and leaves no batch behind.

Everything stored in ``issuance_items`` is the value from the commit-time
re-check.  The tables are append-only (enforced by schema triggers), so an
issued batch is immutable.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from .config import Settings
from .db import connect, transaction
from .repository import DEFAULT_PAGE, InvalidQuery, _validate_limit
from .schema import create_issuance_schema

MAX_BATCH_SAMPLES = 1000
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
METRICS = ("temperature", "ph")
SQL_CHUNK = 500  # ids per IN (...) statement (well under SQLite's limit)


# --------------------------------------------------------------- API models
class IntervalIn(BaseModel):
    """A closed interval for one metric."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    min: float
    max: float

    @model_validator(mode="after")
    def _ordered(self) -> "IntervalIn":
        if self.min > self.max:
            raise ValueError("min must be <= max")
        return self


class IssueBatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_ids: list[StrictInt] = Field(
        min_length=1,
        max_length=MAX_BATCH_SAMPLES,
        description="Distinct sample ids to issue, at most 1000.",
    )
    temperature: IntervalIn = Field(description="Closed temperature range.")
    ph: IntervalIn = Field(description="Closed pH range.")

    @field_validator("sample_ids")
    @classmethod
    def _distinct(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("sample_ids must be distinct")
        out_of_range = [v for v in value if not INT64_MIN <= v <= INT64_MAX]
        if out_of_range:
            # SQLite binds integers as signed 64-bit; larger values would fail
            # with an operational error instead of a clear validation message.
            raise ValueError(
                f"sample_ids must fit in a signed 64-bit integer: {out_of_range[0]}"
            )
        return value


class BatchNotFound(LookupError):
    pass


class IssuanceRejected(Exception):
    """One or more samples failed validation; no batch was created."""

    def __init__(self, errors: list[dict[str, Any]]):
        self.errors = errors
        super().__init__(
            f"issuance rejected: {len(errors)} sample(s) failed validation"
        )


# --------------------------------------------------------------------- utils
def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _is_view(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = 'samples'"
    ).fetchone()
    return row is not None and row["type"] == "view"


def _cursor_value(value: int | None, name: str) -> int | None:
    """Validate a keyset cursor before it is bound to SQLite."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidQuery(f"{name} must be an integer")
    if not INT64_MIN <= value <= INT64_MAX:
        raise InvalidQuery(f"{name} is out of range")
    return value


def _chunks(values: list[int], size: int) -> Iterator[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _read_samples(
    conn: sqlite3.Connection, sample_ids: list[int]
) -> tuple[str, dict[int, tuple[float | None, float | None]]]:
    """Read the requested samples from exactly one committed source.

    Both before and after cutover the legacy name ``samples`` is used: during
    migration it is the real wide table, and after the atomic cutover it is the
    compatibility view over ``sample_metrics``.  A single transaction cannot
    span the cutover, and the long table is never read directly while it is
    still being copied, so the check never mixes the two storage layouts.

    Returns ``(source, {sample_id: (temperature, ph)})``.  Missing samples are
    absent from the map.  The caller must be inside a transaction so all
    chunks come from the same snapshot.
    """
    source = "compatibility_view" if _is_view(conn) else "wide_table"
    found: dict[int, tuple[float | None, float | None]] = {}
    for chunk in _chunks(sample_ids, SQL_CHUNK):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT id, temperature, ph FROM samples"
            f" WHERE id IN ({placeholders})",
            chunk,
        ).fetchall()
        for row in rows:
            found[row["id"]] = (row["temperature"], row["ph"])
    return source, found


def _check(
    sample_ids: Iterable[int],
    found: dict[int, tuple[float | None, float | None]],
    temperature: IntervalIn,
    ph: IntervalIn,
) -> tuple[list[dict[str, Any]], list[tuple[int, float, float]]]:
    """Validate every sample; return ``(errors, accepted_rows)``.

    Errors are reported per sample in request order.  A sample with any
    problem contributes no accepted row, so a rejected request can never
    produce a partial batch.
    """
    errors: list[dict[str, Any]] = []
    accepted: list[tuple[int, float, float]] = []
    bounds = {"temperature": temperature, "ph": ph}
    for sample_id in sample_ids:
        reasons: list[dict[str, str]] = []
        if sample_id not in found:
            reasons.append(
                {
                    "field": "sample_id",
                    "code": "not_found",
                    "message": "sample does not exist",
                }
            )
            errors.append({"sample_id": sample_id, "reasons": reasons})
            continue
        temperature_value, ph_value = found[sample_id]
        values = {"temperature": temperature_value, "ph": ph_value}
        for metric in METRICS:
            value = values[metric]
            interval = bounds[metric]
            if value is None:
                reasons.append(
                    {
                        "field": metric,
                        "code": "null",
                        "message": f"{metric} is null",
                    }
                )
            elif (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                # SQLite's dynamic typing lets legacy SQL store text in a REAL
                # column; such a reading cannot be range-checked.
                reasons.append(
                    {
                        "field": metric,
                        "code": "not_numeric",
                        "message": f"{metric} is not a finite number",
                    }
                )
            elif not (interval.min <= value <= interval.max):
                reasons.append(
                    {
                        "field": metric,
                        "code": "out_of_range",
                        "message": (
                            f"{metric} is outside"
                            f" [{interval.min}, {interval.max}]"
                        ),
                    }
                )
        if reasons:
            errors.append({"sample_id": sample_id, "reasons": reasons})
        else:
            accepted.append((sample_id, temperature_value, ph_value))
    return errors, accepted


class IssuanceService:
    def __init__(self, settings: Settings):
        self.settings = settings

    # ------------------------------------------------------------ read paths
    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.settings, read_only=True)
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            if conn.in_transaction:
                conn.execute("COMMIT")
            conn.close()

    def ensure_schema(self) -> None:
        """Create issuance storage if the process skipped normal startup."""
        conn = connect(self.settings)
        try:
            with transaction(conn):
                create_issuance_schema(conn)
        finally:
            conn.close()

    @contextmanager
    def _read_issuance(self) -> Iterator[sqlite3.Connection]:
        """Read issued batches, creating their storage if this DB predates it."""
        with self._read() as conn:
            present = conn.execute(
                "SELECT 1 FROM sqlite_master"
                " WHERE type = 'table' AND name = 'issuance_batches'"
            ).fetchone()
        if present is None:
            self.ensure_schema()
        with self._read() as conn:
            yield conn

    def get_batch(self, batch_id: int) -> dict[str, Any]:
        batch_id = _cursor_value(batch_id, "batch_id")
        with self._read_issuance() as conn:
            row = conn.execute(
                "SELECT * FROM issuance_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if row is None:
                raise BatchNotFound(f"issuance batch {batch_id} not found")
            return self._batch_payload(conn, row)

    def list_batches(
        self, *, after_batch_id: int | None = None, limit: int = DEFAULT_PAGE
    ) -> dict[str, Any]:
        limit = _validate_limit(limit)
        after_batch_id = _cursor_value(after_batch_id, "after_batch_id")
        with self._read_issuance() as conn:
            sql = "SELECT * FROM issuance_batches"
            params: list[Any] = []
            if after_batch_id is not None:
                sql += " WHERE batch_id > ?"
                params.append(after_batch_id)
            sql += " ORDER BY batch_id LIMIT ?"
            params.append(limit + 1)
            rows = conn.execute(sql, params).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            return {
                "items": [self._batch_payload(conn, row) for row in rows],
                "count": len(rows),
                "next_after_batch_id": (
                    rows[-1]["batch_id"] if has_more and rows else None
                ),
                "has_more": has_more,
            }

    def list_batch_items(
        self,
        batch_id: int,
        *,
        after_sample_id: int | None = None,
        limit: int = DEFAULT_PAGE,
    ) -> dict[str, Any]:
        limit = _validate_limit(limit)
        batch_id = _cursor_value(batch_id, "batch_id")
        after_sample_id = _cursor_value(after_sample_id, "after_sample_id")
        with self._read_issuance() as conn:
            batch = conn.execute(
                "SELECT * FROM issuance_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise BatchNotFound(f"issuance batch {batch_id} not found")
            sql = (
                "SELECT sample_id, temperature, ph FROM issuance_items"
                " WHERE batch_id = ?"
            )
            params: list[Any] = [batch_id]
            if after_sample_id is not None:
                sql += " AND sample_id > ?"
                params.append(after_sample_id)
            sql += " ORDER BY sample_id LIMIT ?"
            params.append(limit + 1)
            rows = conn.execute(sql, params).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            payload = self._batch_payload(conn, batch)
            payload.update(
                {
                    "items": [dict(row) for row in rows],
                    "count": len(rows),
                    "next_after_sample_id": (
                        rows[-1]["sample_id"] if has_more and rows else None
                    ),
                    "has_more": has_more,
                }
            )
            return payload

    @staticmethod
    def _batch_payload(
        conn: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        return {
            "batch_id": row["batch_id"],
            "created_at": row["created_at"],
            "temperature": {
                "min": row["temperature_min"],
                "max": row["temperature_max"],
            },
            "ph": {"min": row["ph_min"], "max": row["ph_max"]},
            "sample_count": row["sample_count"],
            "source": row["source"],
        }

    # --------------------------------------------------------------- issuance
    def issue(self, request: IssueBatchIn) -> dict[str, Any]:
        """Check all samples and, if everything passes, issue an immutable batch.

        Raises :class:`IssuanceRejected` with per-sample reasons when any
        sample is missing, has a NULL reading, or falls outside the interval.
        No batch is written in that case.
        """
        sample_ids = request.sample_ids
        with self._read() as conn:
            _source, found = _read_samples(conn, sample_ids)
            errors, _accepted = _check(
                sample_ids, found, request.temperature, request.ph
            )
        if errors:
            raise IssuanceRejected(errors)

        # Short write transaction: re-read the newest committed state and
        # validate it again before inserting.  The read-only phase above held
        # no write lock, so legacy corrections were free to commit meanwhile.
        conn = connect(self.settings)
        try:
            with transaction(conn, "IMMEDIATE"):
                create_issuance_schema(conn)
                source, found = _read_samples(conn, sample_ids)
                errors, accepted = _check(
                    sample_ids, found, request.temperature, request.ph
                )
                if errors:
                    raise IssuanceRejected(errors)
                cur = conn.execute(
                    "INSERT INTO issuance_batches"
                    " (created_at, temperature_min, temperature_max,"
                    "  ph_min, ph_max, sample_count, source)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        utcnow(),
                        request.temperature.min,
                        request.temperature.max,
                        request.ph.min,
                        request.ph.max,
                        len(accepted),
                        source,
                    ),
                )
                batch_id = int(cur.lastrowid)
                conn.executemany(
                    "INSERT INTO issuance_items"
                    " (batch_id, sample_id, temperature, ph)"
                    " VALUES (?, ?, ?, ?)",
                    [
                        (batch_id, sample_id, temperature, ph)
                        for sample_id, temperature, ph in accepted
                    ],
                )
                row = conn.execute(
                    "SELECT * FROM issuance_batches WHERE batch_id = ?", (batch_id,)
                ).fetchone()
                return self._batch_payload(conn, row)
        finally:
            conn.close()
