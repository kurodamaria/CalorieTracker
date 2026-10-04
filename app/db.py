"""SQLite storage layer for CalorieTracker.

A food (or activity) is a reusable blueprint. A record is one instance of a
blueprint attributed to exactly one person on one day.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

DB_PATH = Path(
    os.environ.get("CALORIE_TRACKER_DB")
    or Path(__file__).resolve().parent.parent / "calorie_tracker.db"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS persons (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL,
    dob                 TEXT    NOT NULL,                 -- YYYY-MM-DD
    sex                 TEXT    NOT NULL DEFAULT 'other',
    sex_offset_kcal     REAL    NOT NULL DEFAULT 0.0,
    height_cm           REAL    NOT NULL,
    activity_multiplier REAL    NOT NULL DEFAULT 1.2,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Targets are a protocol, not an identity: a person changes what they are aiming
-- for, and history has to keep judging each day against the goal that was
-- actually in force then. One row per target set, effective from `effective_on`
-- (inclusive) until a later row supersedes it.
CREATE TABLE IF NOT EXISTS person_targets (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id         INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    effective_on      TEXT    NOT NULL,                    -- YYYY-MM-DD, inclusive
    target_energy_kj  REAL,
    target_protein_g  REAL,
    target_carbs_g    REAL,
    target_fat_g      REAL,
    target_sugar_g    REAL,
    target_fiber_g    REAL,
    target_sodium_mg  REAL,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (person_id, effective_on)
);

CREATE TABLE IF NOT EXISTS weights (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    measured_on TEXT    NOT NULL,
    weight_kg   REAL    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (person_id, measured_on)
);

CREATE TABLE IF NOT EXISTS foods (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    brand           TEXT,
    kind            TEXT    NOT NULL DEFAULT 'food',     -- food | activity
    base_amount     REAL    NOT NULL DEFAULT 100,
    base_unit       TEXT    NOT NULL DEFAULT 'kj',
    energy_kj       REAL    NOT NULL,                    -- may be negative
    protein_g       REAL,
    carbs_g         REAL,
    fat_g           REAL,
    sodium_mg       REAL,                     -- milligrams, as labels state it
    sugar_g         REAL,
    fiber_g         REAL,
    grams_per_ml    REAL    NOT NULL DEFAULT 1.0,
    grams_per_piece REAL,
    notes           TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    food_id    INTEGER NOT NULL REFERENCES foods(id) ON DELETE CASCADE,
    logged_on  TEXT    NOT NULL,
    meal       TEXT    NOT NULL DEFAULT 'snack',
    amount     REAL    NOT NULL,
    unit       TEXT    NOT NULL DEFAULT 'g',
    note       TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS forecasts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id      INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    as_of          TEXT    NOT NULL,                     -- day the forecast was made
    model_version  INTEGER NOT NULL,
    burn_kj        REAL,
    burn_source    TEXT,
    kj_per_kg      REAL    NOT NULL,
    lag_weights    TEXT    NOT NULL,
    params_json    TEXT    NOT NULL,
    summary_json   TEXT    NOT NULL,
    n_paths        INTEGER NOT NULL,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (person_id, as_of, model_version)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULTS = {
    "energy_display": "kj",
    # Energy per kg of body-mass change. 7700 kcal/kg. Editable because fat loss
    # and lean gain do not convert at the same rate.
    "kj_per_kg": "32213",
    # Weights on intake lag 0..3 days, JSON list. Hydration and glycogen mean
    # today's scale reflects a few days of eating, not just today.
    "lag_weights": "[0.20, 0.30, 0.30, 0.20]",
    "calibration_days": "30",
    "n_paths": "1500",
    "target_energy": "8500",
    "target_protein": "140",
    "target_carbs": "180",
    "target_fat": "70",
    "target_sugar": "50",
    "target_fiber": "30",
    # General adult guideline: 2000 mg/day. Blank this target if you do not care.
    "target_sodium": "2000",
}

NUTRIENTS = ("energy", "protein", "carbs", "fat", "sugar", "fiber")
KINDS = ("food", "activity")


def _row_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = _row_factory
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Bring an older database up to the current schema without losing data."""
    notes = []

    # Pre-person databases had `entries` with no person, and no persons table.
    # Build a home for those records rather than orphaning them.
    if _table_exists(conn, "entries") and "person_id" not in _columns(conn, "entries"):
        notes.append("entries: adding person_id")
        legacy = [
            {**dict(r), "day": r["logged_on"]}
            for r in conn.execute("SELECT * FROM entries").fetchall()
        ]
        conn.execute("ALTER TABLE entries RENAME TO entries_legacy")
        conn.executescript(SCHEMA)
        conn.execute("DROP TABLE IF EXISTS entries_legacy")

        person_id = None
        if legacy:
            cur = conn.execute(
                "INSERT INTO persons (name, dob, height_cm) VALUES (?, ?, ?)",
                ("Me", "1990-01-01", 170.0))
            person_id = cur.lastrowid
            for r in legacy:
                conn.execute(
                    "INSERT INTO entries (person_id, food_id, logged_on, meal,"
                    " amount, unit, note) VALUES (?,?,?,?,?,?,?)",
                    (person_id, r["food_id"], r["logged_on"], r["meal"],
                     r["amount"], r["unit"], r["note"]),
                )
            notes.append(f"moved {len(legacy)} record(s) to a default person")

    conn.executescript(SCHEMA)
    _migrate_person_targets(conn, notes)
    if _table_exists(conn, "foods") and "kind" not in _columns(conn, "foods"):
        notes.append("foods: adding kind")
        conn.execute("ALTER TABLE foods ADD COLUMN kind TEXT NOT NULL DEFAULT 'food'")
    # Activity energy is per session; older rows have a per-100 g basis.
    if _table_exists(conn, "foods"):
        conn.execute("UPDATE foods SET base_amount = 1.0 WHERE kind = 'activity' "
                     "AND base_amount <> 1.0")
    _add_missing_columns(conn, notes)
    return notes


TARGET_COLUMNS = ("target_energy_kj", "target_protein_g", "target_carbs_g",
                  "target_fat_g", "target_sugar_g", "target_fiber_g",
                  "target_sodium_mg")


def _first_data_day(conn: sqlite3.Connection, person_id: int) -> str | None:
    """Earliest day this person has any data for."""
    parts = []
    for table, col in (("entries", "logged_on"), ("weights", "measured_on")):
        if not _table_exists(conn, table):
            continue
        row = conn.execute(
            f"SELECT MIN({col}) d FROM {table} WHERE person_id = ?",
            (person_id,)).fetchone()
        if row and row["d"]:
            parts.append(row["d"])
    return min(parts) if parts else None


def _migrate_person_targets(conn: sqlite3.Connection, notes: list[str]) -> None:
    """Move targets off the person row and onto dated rows.

    The existing single set becomes one row effective from the start of that
    person's data, so history renders exactly as it did before rather than
    going blank for every past day. From then on, changes are dated.
    """
    if not _table_exists(conn, "persons"):
        return
    present = [c for c in TARGET_COLUMNS if c in _columns(conn, "persons")]
    if not present:
        return

    today = date.today().isoformat()
    moved = 0
    for p in conn.execute("SELECT * FROM persons").fetchall():
        if not any(p[c] is not None for c in present):
            continue
        effective = _first_data_day(conn, p["id"]) or today
        conn.execute(
            "INSERT OR IGNORE INTO person_targets"
            " (person_id, effective_on, target_energy_kj, target_protein_g,"
            "  target_carbs_g, target_fat_g, target_sugar_g, target_fiber_g,"
            "  target_sodium_mg)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (p["id"], effective,
             *[p.get(c) for c in TARGET_COLUMNS]))
        moved += 1
    if moved:
        notes.append(f"targets: dated {moved} person target set(s) from their "
                     f"first day of data")

    for c in present:
        try:
            conn.execute(f"ALTER TABLE persons DROP COLUMN {c}")
        except sqlite3.OperationalError:
            # Older SQLite, or something else holds a reference. Harmless: no
            # code reads these any more.
            pass
    notes.append("persons: targets moved to person_targets")


# Columns added after the first release. A fresh database gets them from SCHEMA;
# an existing one needs ALTER TABLE, and every one of these is nullable so the
# statement is always safe.
ADDED_COLUMNS = (
    ("foods", "sodium_mg", "REAL"),
)


def _add_missing_columns(conn: sqlite3.Connection, notes: list[str]) -> None:
    for table, column, decl in ADDED_COLUMNS:
        if not _table_exists(conn, table):
            continue
        if column not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            notes.append(f"{table}: adding {column}")


def init_db() -> list[str]:
    with db() as conn:
        notes = migrate(conn)
        conn.executemany(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            list(DEFAULTS.items()),
        )
        return notes


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    if key not in DEFAULTS:
        raise ValueError(f"unknown setting: {key}")
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
