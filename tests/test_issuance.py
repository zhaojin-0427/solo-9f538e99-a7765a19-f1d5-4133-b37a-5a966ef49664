"""Issuance API tests: validation, immutability, pagination and concurrency."""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

import legacy.acquisition as legacy
from app.issuance import IssuanceRejected, IssuanceService, IssueBatchIn
from app.main import create_app

GOOD_RANGES = {"temperature": {"min": 30.0, "max": 42.0}, "ph": {"min": 6.0, "max": 8.0}}


@pytest.fixture()
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def issue(client: TestClient, sample_ids, ranges=None, **extra):
    body = {"sample_ids": sample_ids, **(ranges or GOOD_RANGES), **extra}
    return client.post("/issuances", json=body)


def batch_count(settings) -> int:
    conn = sqlite3.connect(settings.database_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM issuance_batches").fetchone()[0]
    finally:
        conn.close()


# ------------------------------------------------------------------ validation
def test_issue_valid_batch_snapshots_readings(client):
    resp = issue(client, [1, 2, 5])
    assert resp.status_code == 201, resp.text
    batch = resp.json()["batch"]
    assert batch["sample_count"] == 3
    assert batch["temperature"] == {"min": 30.0, "max": 42.0}
    assert batch["ph"] == {"min": 6.0, "max": 8.0}
    assert batch["source"] in {"wide_table", "compatibility_view"}
    assert batch["batch_id"] >= 1

    items = client.get(f"/issuances/{batch['batch_id']}/items").json()
    assert items["items"] == [
        {"sample_id": 1, "temperature": 36.5, "ph": 7.2},
        {"sample_id": 2, "temperature": 36.6, "ph": 7.18},
        {"sample_id": 5, "temperature": 37.1, "ph": 7.41},
    ]
    assert items["has_more"] is False
    assert items["count"] == 3


def test_missing_sample_reports_reason_and_creates_no_batch(client, settings):
    resp = issue(client, [1, 999, 2])
    assert resp.status_code == 422
    body = resp.json()
    assert body["sample_count"] == 1
    assert body["errors"] == [
        {
            "sample_id": 999,
            "reasons": [
                {
                    "field": "sample_id",
                    "code": "not_found",
                    "message": "sample does not exist",
                }
            ],
        }
    ]
    assert batch_count(settings) == 0


def test_null_readings_rejected_with_reason(client, settings):
    # seed ids: 3 temperature NULL, 4 ph NULL, 7 both NULL
    resp = issue(client, [3, 4, 7, 1])
    assert resp.status_code == 422
    errors = {e["sample_id"]: e["reasons"] for e in resp.json()["errors"]}
    assert set(errors) == {3, 4, 7}
    assert [(r["field"], r["code"]) for r in errors[3]] == [("temperature", "null")]
    assert [(r["field"], r["code"]) for r in errors[4]] == [("ph", "null")]
    assert [(r["field"], r["code"]) for r in errors[7]] == [
        ("temperature", "null"),
        ("ph", "null"),
    ]
    assert batch_count(settings) == 0


def test_out_of_range_rejected_with_reason(client, settings):
    resp = issue(client, [1, 9, 40], ranges={
        "temperature": {"min": 36.0, "max": 37.0},
        "ph": {"min": 7.0, "max": 7.3},
    })
    assert resp.status_code == 422
    errors = {e["sample_id"]: e["reasons"] for e in resp.json()["errors"]}
    assert set(errors) == {9, 40}
    assert errors[9][0]["field"] == "temperature"
    assert errors[9][0]["code"] == "out_of_range"
    assert {r["field"] for r in errors[40]} == {"temperature", "ph"}
    assert batch_count(settings) == 0


def test_closed_interval_boundaries_inclusive(client):
    # sample 5 is exactly 37.1 / 7.41
    resp = issue(
        client,
        [5],
        ranges={"temperature": {"min": 37.1, "max": 37.1}, "ph": {"min": 7.41, "max": 7.41}},
    )
    assert resp.status_code == 201, resp.text


def test_non_numeric_reading_rejected(client, settings):
    # SQLite's dynamic typing lets legacy SQL store text in the REAL column.
    conn = legacy.open_db(settings.database_path)
    try:
        legacy.insert(conn, sample_id=777, temperature=36.5, ph=7.0)
        conn.execute("UPDATE samples SET temperature = 'n/a' WHERE id = 777")
    finally:
        conn.close()
    resp = issue(client, [777])
    assert resp.status_code == 422
    reasons = resp.json()["errors"][0]["reasons"]
    assert [(r["field"], r["code"]) for r in reasons] == [
        ("temperature", "not_numeric")
    ]
    assert batch_count(settings) == 0


def test_request_shape_validation(client, settings):
    assert issue(client, [1, 1]).status_code == 422
    assert issue(client, []).status_code == 422
    assert issue(client, list(range(1, 1002))).status_code == 422
    assert issue(
        client,
        [1],
        ranges={"temperature": {"min": 40.0, "max": 30.0}, "ph": {"min": 6.0, "max": 8.0}},
    ).status_code == 422
    # non-integer ids / missing ranges / extra keys
    assert client.post(
        "/issuances",
        json={"sample_ids": ["1"], "temperature": {"min": 0, "max": 1}, "ph": {"min": 0, "max": 1}},
    ).status_code == 422
    assert client.post("/issuances", json={"sample_ids": [1]}).status_code == 422
    assert client.post(
        "/issuances",
        json={
            "sample_ids": [1],
            "temperature": {"min": 0, "max": 1, "extra": 1},
            "ph": {"min": 0, "max": 1},
        },
    ).status_code == 422
    assert batch_count(settings) == 0
    # integers must fit SQLite's signed 64-bit range
    assert issue(client, [2**63]).status_code == 422


def test_max_batch_of_1000_distinct_samples(client, settings):
    conn = legacy.open_db(settings.database_path)
    try:
        for i in range(10000, 11000):
            legacy.insert(conn, sample_id=i, temperature=36.0, ph=7.0)
    finally:
        conn.close()
    ids = list(range(10000, 11000))
    resp = issue(client, ids)
    assert resp.status_code == 201, resp.text
    batch_id = resp.json()["batch"]["batch_id"]

    seen = []
    cursor = None
    while True:
        params = {"limit": 200}
        if cursor is not None:
            params["after_sample_id"] = cursor
        page = client.get(f"/issuances/{batch_id}/items", params=params).json()
        seen.extend(i["sample_id"] for i in page["items"])
        cursor = page["next_after_sample_id"]
        if not page["has_more"]:
            break
    assert seen == ids


# ------------------------------------------------------------ immutability
def test_later_legacy_correction_does_not_change_issued_batch(client, settings):
    resp = issue(client, [1, 2])
    batch_id = resp.json()["batch"]["batch_id"]

    conn = legacy.open_db(settings.database_path)
    try:
        legacy.update(conn, 1, temperature=41.9, ph=6.1)
        legacy.delete(conn, 2)
    finally:
        conn.close()

    items = client.get(f"/issuances/{batch_id}/items").json()["items"]
    assert items == [
        {"sample_id": 1, "temperature": 36.5, "ph": 7.2},
        {"sample_id": 2, "temperature": 36.6, "ph": 7.18},
    ]
    # the current values did change, the snapshot did not
    assert client.get("/samples/1/metrics").json()["temperature"] == 41.9
    assert client.get("/samples/2/metrics").status_code == 404


def test_batches_are_append_only_in_storage(engine, settings):
    service = IssuanceService(settings)
    batch = service.issue(        IssueBatchIn(
            sample_ids=[1],
            temperature={"min": 30, "max": 42},
            ph={"min": 6, "max": 8},
        )
    )
    conn = sqlite3.connect(settings.database_path)
    try:
        with pytest.raises(sqlite3.Error, match="immutable"):
            conn.execute(
                "UPDATE issuance_batches SET temperature_min = 0 WHERE batch_id = ?",
                (batch["batch_id"],),
            )
        with pytest.raises(sqlite3.Error, match="immutable"):
            conn.execute(
                "DELETE FROM issuance_items WHERE batch_id = ?", (batch["batch_id"],)
            )
        with pytest.raises(sqlite3.Error, match="immutable"):
            conn.execute(
                "DELETE FROM issuance_batches WHERE batch_id = ?", (batch["batch_id"],)
            )
        conn.rollback()
    finally:
        conn.close()


# --------------------------------------------------------------- read paths
def test_batch_listing_and_pagination(client):
    ids = [issue(client, [1]).json()["batch"]["batch_id"] for _ in range(3)]
    first = client.get("/issuances", params={"limit": 2}).json()
    assert first["count"] == 2
    assert first["has_more"] is True
    assert [b["batch_id"] for b in first["items"]] == ids[:2]
    rest = client.get(
        "/issuances", params={"limit": 2, "after_batch_id": first["next_after_batch_id"]}
    ).json()
    assert [b["batch_id"] for b in rest["items"]] == ids[2:]
    assert rest["has_more"] is False
    assert client.get(f"/issuances/{ids[0]}").json()["batch_id"] == ids[0]
    assert client.get("/issuances/9999").status_code == 404
    assert client.get("/issuances/9999/items").status_code == 404
    # values SQLite cannot bind are a client error, not a server error
    assert client.get("/issuances/99999999999999999999999999").status_code == 422
    assert (
        client.get("/issuances/1/items", params={"after_sample_id": 2**70}).status_code
        == 422
    )
    assert client.get("/issuances", params={"limit": 0}).status_code == 422


def test_issue_reads_wide_table_then_compat_view(client, settings):
    before = issue(client, [1]).json()["batch"]
    assert before["source"] == "wide_table"

    client.post("/migrations")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        state = client.get("/migrations/current").json()["migration"]
        if state["state"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert state["state"] == "completed"

    after = issue(client, [1]).json()["batch"]
    assert after["source"] == "compatibility_view"
    for batch in (before, after):
        items = client.get(f"/issuances/{batch['batch_id']}/items").json()["items"]
        assert items == [{"sample_id": 1, "temperature": 36.5, "ph": 7.2}]


def test_issued_batch_survives_migration(client, settings):
    batch_id = issue(client, [1, 2]).json()["batch"]["batch_id"]
    client.post("/migrations")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        state = client.get("/migrations/current").json()["migration"]
        if state["state"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert state["state"] == "completed"
    items = client.get(f"/issuances/{batch_id}/items").json()["items"]
    assert [i["sample_id"] for i in items] == [1, 2]


# -------------------------------------------------------------- concurrency
def test_correction_between_check_and_commit_is_rechecked(engine, settings, monkeypatch):
    """A correction committed after phase 1 is caught by the commit-time check.

    The hook pauses after the read-only snapshot transaction has ended (so the
    legacy program is free to write) and before the short commit transaction
    starts.
    """
    service = IssuanceService(settings)
    original_read = service._read
    phase1_done = threading.Event()
    correction_done = threading.Event()
    state = {"hooked": False}
    hook_errors: list[BaseException] = []

    @contextmanager
    def read_with_hook():
        with original_read() as conn:
            yield conn
        # Phase 1's transaction is closed here: no SQLite locks are held.
        if not state["hooked"]:
            state["hooked"] = True
            phase1_done.set()
            if not correction_done.wait(10):
                hook_errors.append(AssertionError("correction did not commit"))

    def correct_temperature():
        phase1_done.wait()
        writer = legacy.open_db(settings.database_path)
        try:
            legacy.update(writer, 9, temperature=99.0)
        finally:
            writer.close()
        correction_done.set()

    monkeypatch.setattr(service, "_read", read_with_hook)
    corrector = threading.Thread(target=correct_temperature, daemon=True)
    corrector.start()
    with pytest.raises(IssuanceRejected) as info:
        service.issue(
            IssueBatchIn(
                sample_ids=[9],
                temperature={"min": 30, "max": 42},
                ph={"min": 6, "max": 8},
            )
        )
    corrector.join(10)
    assert hook_errors == []
    assert info.value.errors[0]["sample_id"] == 9
    assert info.value.errors[0]["reasons"][0]["code"] == "out_of_range"
    assert batch_count(settings) == 0


def test_correction_visible_at_commit_is_what_gets_issued(engine, settings, monkeypatch):
    """If the re-check passes, the batch stores the newest consistent values."""
    service = IssuanceService(settings)
    original_read = service._read
    state = {"hooked": False}

    @contextmanager
    def read_with_hook():
        with original_read() as conn:
            yield conn
        if not state["hooked"]:
            state["hooked"] = True
            writer = legacy.open_db(settings.database_path)
            try:
                legacy.update(writer, 9, temperature=37.5)
            finally:
                writer.close()

    monkeypatch.setattr(service, "_read", read_with_hook)
    batch = service.issue(
        IssueBatchIn(
            sample_ids=[9],
            temperature={"min": 30, "max": 42},
            ph={"min": 6, "max": 8},
        )
    )
    items = service.list_batch_items(batch["batch_id"])["items"]
    assert items == [{"sample_id": 9, "temperature": 37.5, "ph": 7.52}]


def test_legacy_writes_not_blocked_during_long_issuance_check(engine, settings, monkeypatch):
    """A 1000-sample check does not hold the write lock; a write can commit."""
    service = IssuanceService(settings)
    conn = legacy.open_db(settings.database_path)
    try:
        for i in range(20000, 21000):
            legacy.insert(conn, sample_id=i, temperature=36.0, ph=7.0)
    finally:
        conn.close()

    original_read = service._read
    phase1_done = threading.Event()
    writer_done = threading.Event()
    state = {"hooked": False}
    hook_errors: list[BaseException] = []

    @contextmanager
    def read_with_hook():
        with original_read() as conn:
            yield conn
        if not state["hooked"]:
            state["hooked"] = True
            # The validation of all 1000 samples is finished and its read
            # transaction is closed.  A legacy write must be able to commit
            # now, before the issuance tries to take the write lock.
            phase1_done.set()
            if not writer_done.wait(10):
                hook_errors.append(AssertionError("writer did not finish"))

    observed: dict[str, float | BaseException | None] = {"error": None}

    def writer():
        phase1_done.wait()
        w = legacy.open_db(settings.database_path)
        try:
            started = time.monotonic()
            legacy.update(w, 20000, temperature=36.5)
            observed["elapsed"] = time.monotonic() - started
        except BaseException as exc:  # noqa: BLE001
            observed["error"] = exc
        finally:
            w.close()
        writer_done.set()

    monkeypatch.setattr(service, "_read", read_with_hook)
    t_write = threading.Thread(target=writer, daemon=True)
    t_write.start()
    ids = list(range(20000, 21000))
    batch = service.issue(
        IssueBatchIn(
            sample_ids=ids,
            temperature={"min": 30, "max": 42},
            ph={"min": 6, "max": 8},
        )
    )
    t_write.join(10)

    assert hook_errors == []
    assert observed["error"] is None
    assert observed["elapsed"] < 5
    assert batch["sample_count"] == 1000
    items = service.list_batch_items(batch["batch_id"], limit=1000)["items"]
    # The commit-time re-check stored the correction that landed after phase 1.
    assert items[0] == {"sample_id": 20000, "temperature": 36.5, "ph": 7.0}
    assert len(items) == 1000

