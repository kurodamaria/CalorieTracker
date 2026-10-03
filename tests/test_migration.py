"""Verify the schema migration preserves an existing database."""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "calorie_tracker.db")

fails = []


def check_true(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label} {detail}")
    if not cond:
        fails.append(label)


def build_v1_db(path):
    """A pre-person database: old entries schema, no persons, no kinds."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE foods (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            brand TEXT, base_amount REAL NOT NULL DEFAULT 100,
            base_unit TEXT NOT NULL DEFAULT 'kj', energy_kj REAL,
            protein_g REAL, carbs_g REAL, fat_g REAL, sugar_g REAL, fiber_g REAL,
            grams_per_ml REAL NOT NULL DEFAULT 1.0, grams_per_piece REAL,
            notes TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            food_id INTEGER NOT NULL REFERENCES foods(id) ON DELETE CASCADE,
            logged_on TEXT NOT NULL, meal TEXT NOT NULL DEFAULT 'snack',
            amount REAL NOT NULL, unit TEXT NOT NULL DEFAULT 'g', note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    conn.execute("INSERT INTO foods (name, energy_kj, protein_g, carbs_g, fat_g)"
                 " VALUES ('煮鸡蛋', 649, 12.6, 0.6, 10.6)")
    conn.execute("INSERT INTO entries (food_id, logged_on, meal, amount, unit)"
                 " VALUES (1, '2026-01-05', 'breakfast', 100, 'g')")
    conn.execute("INSERT INTO entries (food_id, logged_on, meal, amount, unit)"
                 " VALUES (1, '2026-01-06', 'lunch', 50, 'g')")
    conn.execute("INSERT INTO settings VALUES ('energy_display','kcal')")
    conn.commit()
    conn.close()


def run_migrate(path):
    code = (
        "import sys, json; sys.path.insert(0, %r)\n"
        "from app.db import init_db, db\n"
        "print(json.dumps(init_db()))\n"
        % os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    env = {**os.environ, "CALORIE_TRACKER_DB": path, "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, env=env)
    if out.returncode != 0:
        print(out.stdout, out.stderr)
        raise SystemExit("migrate failed")
    return out.stdout.strip()


tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "v1.db")
build_v1_db(path)

print("--- migrating a pre-person database ---")
print("  notes:", run_migrate(path))

conn = sqlite3.connect(path)
conn.row_factory = sqlite3.Row
cols = [r["name"] for r in conn.execute("PRAGMA table_info(foods)")]
check_true("foods gained kind", "kind" in cols)
check_true("persons table exists", conn.execute(
    "SELECT 1 FROM sqlite_master WHERE name='persons'").fetchone())
check_true("weights table exists", conn.execute(
    "SELECT 1 FROM sqlite_master WHERE name='weights'").fetchone())
check_true("forecasts table exists", conn.execute(
    "SELECT 1 FROM sqlite_master WHERE name='forecasts'").fetchone())

people = conn.execute("SELECT * FROM persons").fetchall()
check_true("a default person was created for the orphaned records", len(people) == 1,
           f"name={people[0]['name']!r}" if people else "")
check_true("default person has a height", people[0]["height_cm"] > 0)

entries = conn.execute("SELECT * FROM entries ORDER BY id").fetchall()
check_true("no records lost", len(entries) == 2, f"{len(entries)} rows")
check_true("records re-attached to the default person",
           all(e["person_id"] == people[0]["id"] for e in entries))
check_true("record amounts survived",
           [e["amount"] for e in entries] == [100.0, 50.0])
check_true("record dates survived", entries[0]["logged_on"] == "2026-01-05")
check_true("meals survived", entries[0]["meal"] == "breakfast")

food = conn.execute("SELECT * FROM foods").fetchone()
check_true("food kept its name", food["name"] == "煮鸡蛋")
check_true("food kept its energy", food["energy_kj"] == 649)
check_true("existing food defaults to kind=food", food["kind"] == "food")

disp = conn.execute("SELECT value FROM settings WHERE key='energy_display'").fetchone()
check_true("existing settings preserved", disp["value"] == "kcal")

# second run must be a no-op
print("--- second run is idempotent ---")
notes2 = run_migrate(path)
check_true("no further changes", notes2 == "[]", f"notes={notes2}")
check_true("still 2 records", conn.execute(
    "SELECT COUNT(*) n FROM entries").fetchone()["n"] == 2)
check_true("still 1 person", conn.execute(
    "SELECT COUNT(*) n FROM persons").fetchone()["n"] == 1)
conn.close()

# a brand-new database
print("--- fresh database ---")
fresh = os.path.join(tmp, "fresh.db")
run_migrate(fresh)
c2 = sqlite3.connect(fresh)
c2.row_factory = sqlite3.Row
check_true("fresh db has persons/weights/forecasts/entries",
           all(c2.execute("SELECT 1 FROM sqlite_master WHERE name=?",
                          (t,)).fetchone() for t in
               ("persons", "weights", "forecasts", "entries", "foods", "settings")))
check_true("fresh entries has person_id NOT NULL",
           [r["name"] for r in c2.execute("PRAGMA table_info(entries)")]
           .count("person_id") == 1)
check_true("fresh foods defaults seeded",
           c2.execute("SELECT COUNT(*) n FROM settings").fetchone()["n"] == 11)
c2.close()

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    sys.exit(1)
print("ALL MIGRATION CHECKS PASSED")
