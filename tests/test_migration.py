"""Verify the schema migration preserves an existing database."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date, timedelta

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "calorie_tracker.db")

fails = []


def check(label, got, want, tol=0.01):
    if isinstance(want, (str, list, dict)) or isinstance(got, (str, list, dict)) \
            or want is None or got is None:
        ok = got == want
    else:
        ok = abs(got - want) <= tol
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        fails.append(label)


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
check_true("fresh foods has sodium_mg",
           "sodium_mg" in [r["name"] for r in c2.execute("PRAGMA table_info(foods)")])
check_true("fresh db has person_targets",
           c2.execute("SELECT 1 FROM sqlite_master WHERE name='person_targets'"
                      ).fetchone())
pcols = [r["name"] for r in c2.execute("PRAGMA table_info(persons)")]
check_true("persons holds identity only", not any(c.startswith("target_") for c in pcols),
           str([c for c in pcols if c.startswith("target_")]))
tcols = {r["name"] for r in c2.execute("PRAGMA table_info(person_targets)")}
check_true("person_targets holds every target column",
           all(c in tcols for c in ("target_energy_kj", "target_protein_g",
                                    "target_carbs_g", "target_fat_g",
                                    "target_sugar_g", "target_fiber_g",
                                    "target_sodium_mg")), str(sorted(tcols)))
n_settings = c2.execute("SELECT COUNT(*) n FROM settings").fetchone()["n"]
check_true("fresh defaults seeded incl. target_sodium", n_settings == 12, f"{n_settings}")
check_true("sodium default is 2000 mg",
           c2.execute("SELECT value FROM settings WHERE key='target_sodium'")
           .fetchone()["value"] == "2000")
c2.close()

# A database from *before* sodium existed: people and food present, no sodium
# column anywhere. The migration must add it without touching existing values.
print("\n--- pre-sodium database gains the columns ---")
old = os.path.join(tmp, "pre_sodium.db")
oc = sqlite3.connect(old)
oc.executescript("""
    CREATE TABLE persons (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, dob TEXT NOT NULL,
        sex TEXT NOT NULL DEFAULT 'other', sex_offset_kcal REAL NOT NULL DEFAULT 0.0,
        height_cm REAL NOT NULL, activity_multiplier REAL NOT NULL DEFAULT 1.2,
        target_energy_kj REAL, target_protein_g REAL, target_carbs_g REAL,
        target_fat_g REAL, target_sugar_g REAL, target_fiber_g REAL, target_sodium_mg REAL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE foods (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, brand TEXT,
        kind TEXT NOT NULL DEFAULT 'food', base_amount REAL NOT NULL DEFAULT 100,
        base_unit TEXT NOT NULL DEFAULT 'kj', energy_kj REAL NOT NULL,
        protein_g REAL, carbs_g REAL, fat_g REAL, sugar_g REAL, fiber_g REAL,
        grams_per_ml REAL NOT NULL DEFAULT 1.0, grams_per_piece REAL, notes TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')));
""")
oc.execute("INSERT INTO persons (name, dob, height_cm, target_energy_kj)"
           " VALUES ('Existing', '1990-01-01', 180, 9000)")
oc.execute("INSERT INTO foods (name, energy_kj, protein_g, carbs_g, fat_g)"
           " VALUES ('Old food', 500, 10, 20, 5)")
oc.commit()
oc.close()

notes = json.loads(run_migrate(old))
check_true("reported dating the targets",
           any("targets: dated" in n for n in notes), str(notes))
check_true("reported moving them off the person row",
           any("targets moved to person_targets" in n for n in notes), str(notes))
oc = sqlite3.connect(old)
oc.row_factory = sqlite3.Row
check_true("foods gained sodium_mg",
           "sodium_mg" in [r["name"] for r in oc.execute("PRAGMA table_info(foods)")])
check_true("person_targets exists",
           oc.execute("SELECT 1 FROM sqlite_master WHERE name='person_targets'"
                      ).fetchone())
f = oc.execute("SELECT * FROM foods").fetchone()
check_true("existing food kept its values",
           (f["name"], f["energy_kj"], f["protein_g"]) == ("Old food", 500, 10))
check_true("and sodium starts empty, not zero", f["sodium_mg"] is None)
p = oc.execute("SELECT * FROM persons").fetchone()
check_true("existing person row kept its identity",
           (p["name"], p["height_cm"]) == ("Existing", 180))
check_true("targets dropped from the person row",
           not any(r["name"].startswith("target_")
                   for r in oc.execute("PRAGMA table_info(persons)")))
pt = oc.execute("SELECT * FROM person_targets").fetchone()
check_true("and preserved in the new table",
           pt is not None and pt["person_id"] == p["id"] and pt["target_energy_kj"] == 9000,
           str(dict(pt)) if pt else "none")
check_true("undated because that person has no records",
           pt["effective_on"] == date.today().isoformat(), pt["effective_on"])
oc.close()
check_true("second run over it is a no-op", json.loads(run_migrate(old)) == [])

# A person WITH history gets their existing targets dated from their first day,
# so history keeps rendering exactly as it did before the migration.
print("\n--- targets dated from the first day of data ---")
old2 = os.path.join(tmp, "with_history.db")
oc = sqlite3.connect(old2)
oc.executescript("""
    CREATE TABLE persons (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, dob TEXT NOT NULL,
        sex TEXT NOT NULL DEFAULT 'other', sex_offset_kcal REAL NOT NULL DEFAULT 0.0,
        height_cm REAL NOT NULL, activity_multiplier REAL NOT NULL DEFAULT 1.2,
        target_energy_kj REAL, target_protein_g REAL, target_carbs_g REAL,
        target_fat_g REAL, target_sugar_g REAL, target_fiber_g REAL, target_sodium_mg REAL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE foods (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, brand TEXT,
        kind TEXT NOT NULL DEFAULT 'food', base_amount REAL NOT NULL DEFAULT 100,
        base_unit TEXT NOT NULL DEFAULT 'kj', energy_kj REAL NOT NULL,
        protein_g REAL, carbs_g REAL, fat_g REAL, sugar_g REAL, fiber_g REAL,
        grams_per_ml REAL NOT NULL DEFAULT 1.0, grams_per_piece REAL, notes TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
        food_id INTEGER NOT NULL REFERENCES foods(id) ON DELETE CASCADE,
        logged_on TEXT NOT NULL, meal TEXT NOT NULL DEFAULT 'snack',
        amount REAL NOT NULL, unit TEXT NOT NULL DEFAULT 'g', note TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')));
""")
first_day = (date.today() - timedelta(days=45)).isoformat()
oc.execute("INSERT INTO persons (name, dob, height_cm, target_energy_kj,"
           " target_protein_g) VALUES ('Older', '1990-01-01', 180, 7500, 130)")
oc.execute("INSERT INTO foods (name, energy_kj) VALUES ('Old food', 500)")
oc.execute("INSERT INTO entries (person_id, food_id, logged_on, amount)"
           " VALUES (1, 1, ?, 100)", (first_day,))
oc.execute("INSERT INTO entries (person_id, food_id, logged_on, amount)"
           " VALUES (1, 1, ?, 100)", ((date.today() - timedelta(days=5)).isoformat(),))
oc.commit()
oc.close()

run_migrate(old2)
oc = sqlite3.connect(old2)
oc.row_factory = sqlite3.Row
pt = oc.execute("SELECT * FROM person_targets ORDER BY effective_on").fetchall()
check_true("one set migrated", len(pt) == 1, str(len(pt)))
check("anchored to their earliest record, not today",
      pt[0]["effective_on"], first_day)
check("energy preserved", pt[0]["target_energy_kj"], 7500.0)
check("protein preserved", pt[0]["target_protein_g"], 130.0)
check_true("carbs was null and stays null", pt[0]["target_carbs_g"] is None)
n_entries = oc.execute("SELECT COUNT(*) n FROM entries").fetchone()["n"]
check_true("entries untouched", n_entries == 2, str(n_entries))
oc.close()
check_true("second run over it is a no-op", json.loads(run_migrate(old2)) == [])

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    sys.exit(1)
print("ALL MIGRATION CHECKS PASSED")
