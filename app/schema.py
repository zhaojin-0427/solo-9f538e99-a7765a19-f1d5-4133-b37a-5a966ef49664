"""SQLite schema definitions for the wide-to-long migration.

The legacy acquisition program reads and writes ``samples(id, temperature, ph)``
directly.  While migration is running that table remains a real table.  The
cutover replaces it *inside one transaction* with a view over the long table,
backed by INSTEAD OF triggers, so unmodified legacy SQL keeps working.
"""

# Long (analysis) storage: exactly one row per sample and metric.
LONG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sample_metrics (
    sample_id INTEGER NOT NULL,
    metric    TEXT    NOT NULL CHECK (metric IN ('temperature', 'ph')),
    value     REAL,
    PRIMARY KEY (sample_id, metric)
) WITHOUT ROWID
"""

# Bookkeeping for the one logical migration.  Repeated POST requests must not
# rebuild a completed database, so this is the source of truth for state.
MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS migration_state (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    run_id         TEXT    NOT NULL,
    state          TEXT    NOT NULL
                   CHECK (state IN ('copying', 'completed', 'failed')),
    created_at     TEXT    NOT NULL,
    started_at     TEXT,
    completed_at   TEXT,
    last_error     TEXT,
    total_samples  INTEGER NOT NULL DEFAULT 0,
    samples_copied INTEGER NOT NULL DEFAULT 0,
    last_id        INTEGER,
    attempt        INTEGER NOT NULL DEFAULT 0
)
"""

# Issued quality-control batches are append-only.  ``issuance_items`` stores the
# exact wide-table readings that were checked, so later legacy corrections can
# never change what a batch says was issued.
ISSUANCE_BATCHES_SQL = """
CREATE TABLE IF NOT EXISTS issuance_batches (
    batch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL,
    temperature_min REAL    NOT NULL,
    temperature_max REAL    NOT NULL,
    ph_min          REAL    NOT NULL,
    ph_max          REAL    NOT NULL,
    sample_count    INTEGER NOT NULL CHECK (sample_count > 0),
    source          TEXT    NOT NULL,
    CHECK (temperature_min <= temperature_max),
    CHECK (ph_min <= ph_max)
)
"""

ISSUANCE_ITEMS_SQL = """
CREATE TABLE IF NOT EXISTS issuance_items (
    batch_id    INTEGER NOT NULL REFERENCES issuance_batches(batch_id),
    sample_id   INTEGER NOT NULL,
    temperature REAL    NOT NULL,
    ph          REAL    NOT NULL,
    PRIMARY KEY (batch_id, sample_id)
) WITHOUT ROWID
"""

# Enforce append-only at the storage level as well as in the API: a batch (and
# its snapshotted readings) can never be edited or deleted.  INSERT OR REPLACE
# is also blocked because REPLACE deletes the old row first.
ISSUANCE_IMMUTABLE_TRIGGER_SQL = (
    """
    CREATE TRIGGER IF NOT EXISTS issuance_batches_no_update
    BEFORE UPDATE ON issuance_batches
    BEGIN
        SELECT RAISE(ABORT, 'issued batches are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS issuance_batches_no_delete
    BEFORE DELETE ON issuance_batches
    BEGIN
        SELECT RAISE(ABORT, 'issued batches are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS issuance_items_no_update
    BEFORE UPDATE ON issuance_items
    BEGIN
        SELECT RAISE(ABORT, 'issued batch items are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS issuance_items_no_delete
    BEFORE DELETE ON issuance_items
    BEGIN
        SELECT RAISE(ABORT, 'issued batch items are immutable');
    END
    """,
)

MIGRATION_EVENT_SQL = """
CREATE TABLE IF NOT EXISTS migration_events (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT    NOT NULL,
    at     TEXT    NOT NULL,
    event  TEXT    NOT NULL,
    detail TEXT
)
"""

# Helper for the compatibility INSERT trigger: remembers the id allocated when
# the legacy program inserts without an explicit id.  One row only.
INSERT_CONTEXT_SQL = """
CREATE TABLE IF NOT EXISTS _insert_context (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    sample_id INTEGER NOT NULL
)
"""

# Compatibility view: exactly the legacy column names, values and NULLs.
COMPAT_VIEW_SQL = """
CREATE VIEW samples (id, temperature, ph) AS
SELECT
    sample_id,
    MAX(CASE WHEN metric = 'temperature' THEN value END),
    MAX(CASE WHEN metric = 'ph'          THEN value END)
FROM sample_metrics
GROUP BY sample_id
"""

# INSERT: explicit duplicate ids are rejected like the legacy PRIMARY KEY did.
# An omitted / NULL id is allocated as MAX(id)+1, matching SQLite's legacy
# INTEGER PRIMARY KEY behaviour (no AUTOINCREMENT).
#
# Both metric rows always get the same effective id (given or allocated).  The
# one-row helper also lets a legacy program recover the id allocated for an
# INSERT without an id: SQLite restores ``last_insert_rowid()`` when a trigger
# finishes, so ``cursor.lastrowid`` cannot report it.
COMPAT_INSERT_TRIGGER_SQL = """
CREATE TRIGGER samples_insert INSTEAD OF INSERT ON samples
BEGIN
    SELECT RAISE(ABORT, 'UNIQUE constraint failed: samples.id')
    WHERE NEW.id IS NOT NULL
      AND EXISTS (SELECT 1 FROM sample_metrics WHERE sample_id = NEW.id);

    DELETE FROM _insert_context;
    INSERT INTO _insert_context (id, sample_id)
    VALUES (
        1,
        COALESCE(NEW.id, (SELECT COALESCE(MAX(sample_id), 0) + 1 FROM sample_metrics))
    );

    INSERT INTO sample_metrics (sample_id, metric, value)
    SELECT c.sample_id, m.metric,
           CASE m.metric
               WHEN 'temperature' THEN NEW.temperature
               WHEN 'ph'          THEN NEW.ph
           END
    FROM _insert_context AS c
    CROSS JOIN (SELECT 'temperature' AS metric UNION ALL SELECT 'ph') AS m;
END
"""

# UPDATE: changing the id moves the two rows; changing a value overwrites only
# that metric.  Both rows are kept, even with NULL values.
COMPAT_UPDATE_TRIGGER_SQL = """
CREATE TRIGGER samples_update INSTEAD OF UPDATE ON samples
BEGIN
    -- Match the wide table: updating the INTEGER PRIMARY KEY to NULL was a
    -- "datatype mismatch" error there.
    SELECT RAISE(ABORT, 'datatype mismatch') WHERE NEW.id IS NULL;

    SELECT RAISE(ABORT, 'UNIQUE constraint failed: samples.id')
    WHERE NEW.id IS NOT OLD.id
      AND EXISTS (SELECT 1 FROM sample_metrics WHERE sample_id = NEW.id);

    DELETE FROM sample_metrics
    WHERE sample_id = OLD.id AND NEW.id IS NOT OLD.id;

    INSERT INTO sample_metrics (sample_id, metric, value)
    SELECT NEW.id, m.metric,
           CASE m.metric
               WHEN 'temperature' THEN NEW.temperature
               WHEN 'ph'          THEN NEW.ph
           END
    FROM (SELECT 'temperature' AS metric UNION ALL SELECT 'ph') AS m
    WHERE NEW.id IS NOT NULL
    ON CONFLICT (sample_id, metric) DO UPDATE SET value = excluded.value;
END
"""

COMPAT_DELETE_TRIGGER_SQL = """
CREATE TRIGGER samples_delete INSTEAD OF DELETE ON samples
BEGIN
    DELETE FROM sample_metrics WHERE sample_id = OLD.id;
END
"""

_COMPAT_DROP_SQL = """
DROP TRIGGER IF EXISTS samples_insert;
DROP TRIGGER IF EXISTS samples_update;
DROP TRIGGER IF EXISTS samples_delete;
DROP VIEW IF EXISTS samples;
"""

# Triggers installed on the real wide table for the duration of the migration.
# Every committed legacy write is mirrored immediately, so a chunk that was
# copied earlier can never be rolled back to a stale value.
MIGRATION_TRIGGERS = (
    """
    CREATE TRIGGER _migration_samples_insert AFTER INSERT ON samples
    BEGIN
        INSERT INTO sample_metrics (sample_id, metric, value)
        VALUES (NEW.id, 'temperature', NEW.temperature)
        ON CONFLICT (sample_id, metric) DO UPDATE SET value = excluded.value;
        INSERT INTO sample_metrics (sample_id, metric, value)
        VALUES (NEW.id, 'ph', NEW.ph)
        ON CONFLICT (sample_id, metric) DO UPDATE SET value = excluded.value;
    END
    """,
    """
    CREATE TRIGGER _migration_samples_update AFTER UPDATE ON samples
    BEGIN
        DELETE FROM sample_metrics
        WHERE sample_id = OLD.id AND NEW.id IS NOT OLD.id;
        INSERT INTO sample_metrics (sample_id, metric, value)
        VALUES (NEW.id, 'temperature', NEW.temperature)
        ON CONFLICT (sample_id, metric) DO UPDATE SET value = excluded.value;
        INSERT INTO sample_metrics (sample_id, metric, value)
        VALUES (NEW.id, 'ph', NEW.ph)
        ON CONFLICT (sample_id, metric) DO UPDATE SET value = excluded.value;
    END
    """,
    """
    CREATE TRIGGER _migration_samples_delete AFTER DELETE ON samples
    BEGIN
        DELETE FROM sample_metrics WHERE sample_id = OLD.id;
    END
    """,
)

_MIGRATION_DROP_SQL = """
DROP TRIGGER IF EXISTS _migration_samples_insert;
DROP TRIGGER IF EXISTS _migration_samples_update;
DROP TRIGGER IF EXISTS _migration_samples_delete;
"""

WIDE_TABLE_SQL = """
CREATE TABLE samples (
    id INTEGER PRIMARY KEY,
    temperature REAL,
    ph REAL
)
"""


def create_base_schema(conn) -> None:
    """Create long storage, issuance storage and bookkeeping (idempotent)."""
    conn.execute(LONG_TABLE_SQL)
    conn.execute(MIGRATION_TABLE_SQL)
    conn.execute(MIGRATION_EVENT_SQL)
    conn.execute(INSERT_CONTEXT_SQL)
    conn.execute(ISSUANCE_BATCHES_SQL)
    conn.execute(ISSUANCE_ITEMS_SQL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_issuance_items_sample"
        " ON issuance_items(sample_id)"
    )
    for sql in ISSUANCE_IMMUTABLE_TRIGGER_SQL:
        conn.execute(sql)


def create_issuance_schema(conn) -> None:
    """Create the issuance tables for code paths that may run before startup."""
    conn.execute(ISSUANCE_BATCHES_SQL)
    conn.execute(ISSUANCE_ITEMS_SQL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_issuance_items_sample"
        " ON issuance_items(sample_id)"
    )
    for sql in ISSUANCE_IMMUTABLE_TRIGGER_SQL:
        conn.execute(sql)


def ensure_wide_table(conn) -> None:
    """Create the legacy wide table only when ``samples`` does not exist.

    After cutover ``samples`` is a view; ``CREATE TABLE IF NOT EXISTS`` is a
    no-op for views on SQLite, but checking sqlite_master keeps the intent
    explicit and works on older versions too.
    """
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = 'samples'"
    ).fetchone()
    if row is None:
        conn.execute(WIDE_TABLE_SQL)


def install_migration_triggers(conn) -> None:
    """Install mirroring triggers on the wide table (inside a transaction)."""
    drop_migration_triggers(conn)
    for sql in MIGRATION_TRIGGERS:
        conn.execute(sql)


def drop_migration_triggers(conn) -> None:
    for statement in _MIGRATION_DROP_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)


def replace_wide_table_with_view(conn) -> None:
    """Drop the wide table and install the compatibility view.

    Caller must be inside a write transaction.  SQLite DDL is transactional,
    so a failure at any point rolls the whole switch back.
    """
    conn.execute("DROP TABLE IF EXISTS samples")
    for statement in _COMPAT_DROP_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.execute(COMPAT_VIEW_SQL)
    conn.execute(COMPAT_INSERT_TRIGGER_SQL)
    conn.execute(COMPAT_UPDATE_TRIGGER_SQL)
    conn.execute(COMPAT_DELETE_TRIGGER_SQL)
