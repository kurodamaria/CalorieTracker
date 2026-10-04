"""Dated targets: history must not be rewritten when a target changes."""
import os
import sys
import tempfile
from datetime import date, timedelta

os.environ["CALORIE_TRACKER_DB"] = os.path.join(tempfile.mkdtemp(), "targets.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

KJ = 4.184
TODAY = date.today()
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


with TestClient(app) as c:
    cfg = c.get("/api/config").json()
    check("energy target column name", cfg["target_columns"]["energy"],
          "target_energy_kj")
    check("sodium target column name", cfg["target_columns"]["sodium"],
          "target_sodium_mg")

    pid = c.post("/api/persons", json={"name": "Test Person", "dob": "1990-01-01",
                                      "sex": "male", "height_cm": 180}).json()["id"]
    sets = c.get(f"/api/persons/{pid}/targets").json()
    check_true("a new person gets one seeded target set", len(sets) == 1, str(sets))
    check("seeded from the global default", sets[0]["energy"], 8500.0)
    check("seeded effective today", sets[0]["effective_on"], TODAY.isoformat())

    # This test drives its own timeline, so drop the seeded set. It is the
    # latest and would otherwise win for today and every later day.
    c.delete(f"/api/targets/{sets[0]['id']}")
    check("seeded set removed", c.get(f"/api/persons/{pid}/targets").json(), [])

    # ---------------------------------------------- log across three days
    food = c.post("/api/foods", json={"name": "Meal", "energy": 9000,
                                      "protein": 40, "carbs": 100, "fat": 30,
                                      "sodium": 800}).json()["id"]
    d1 = (TODAY - timedelta(days=20)).isoformat()
    d2 = (TODAY - timedelta(days=10)).isoformat()
    d3 = (TODAY - timedelta(days=2)).isoformat()
    for day in (d1, d2, d3):
        r = c.post("/api/entries", json={"person_id": pid, "food_id": food,
                                         "date": day, "meal": "lunch",
                                         "amount": 100, "unit": "g"})
        assert r.status_code == 201, r.text

    print("\n--- a target cannot govern days before it existed ---")
    for day in (d1, d2, d3):
        s = c.get(f"/api/day/{day}?person_id={pid}").json()
        check_true(f"{day} has no target yet", s["targets"]["energy"] is None,
                   f"got {s['targets']['energy']}")
        check_true(f"{day} has no effective date",
                   s["targets_effective_on"] is None)
        check_true(f"{day} has no remaining", s["remaining"]["energy"] is None)

    # Backdate a first set so there is a baseline to protect.
    r = c.put(f"/api/persons/{pid}/targets", json={
        "effective_on": d1, "energy": 8500, "protein": 140, "carbs": 180,
        "fat": 70, "sugar": 50, "fiber": 30, "sodium": 2000})
    assert r.status_code == 200, r.text
    check("backdated set stored", r.json()["energy"], 8500.0)

    print("\n--- one target set in force ---")
    for day in (d1, d2, d3):
        s = c.get(f"/api/day/{day}?person_id={pid}").json()
        check(f"{day} target", s["targets"]["energy"], 8500.0)
        check(f"{day} effective_on", s["targets_effective_on"], d1)
        # 9000 eaten against an 8500 target -> 500 over
        check(f"{day} remaining", s["remaining"]["energy"], -500.0)

    print("\n--- add a later target set; the past must not move ---")
    r = c.put(f"/api/persons/{pid}/targets", json={
        "effective_on": d2, "energy": 10000, "protein": 150,
        "carbs": 200, "fat": 70, "sugar": 50, "fiber": 30, "sodium": 2000})
    assert r.status_code == 200, r.text
    check("new set stored", r.json()["energy"], 10000.0)

    s1 = c.get(f"/api/day/{d1}?person_id={pid}").json()
    check("day BEFORE the change keeps the old target", s1["targets"]["energy"], 8500.0)
    check("...and is still judged 500 over", s1["remaining"]["energy"], -500.0)
    check("...still using the old set's date", s1["targets_effective_on"], d1)

    s2 = c.get(f"/api/day/{d2}?person_id={pid}").json()
    check("day ON the change uses the new target", s2["targets"]["energy"], 10000.0)
    check("...now 1000 under", s2["remaining"]["energy"], 1000.0)
    check("...dated to the change", s2["targets_effective_on"], d2)

    s3 = c.get(f"/api/day/{d3}?person_id={pid}").json()
    check("a later day uses the new target too", s3["targets"]["energy"], 10000.0)
    check("later days report the current set as equal",
          s3["targets_current"]["energy"], 10000.0)
    check_true("but the past day reports a different one in force",
               s1["targets_current"]["energy"] == 10000.0 and
               s1["targets"]["energy"] == 8500.0)

    print("\n--- every nutrient travelled with its own column ---")
    s = c.get(f"/api/day/{d2}?person_id={pid}").json()
    check("protein target", s["targets"]["protein"], 150.0)
    check("carbs target", s["targets"]["carbs"], 200.0)
    check("fat target", s["targets"]["fat"], 70.0)
    check("sugar target", s["targets"]["sugar"], 50.0)
    check("fiber target", s["targets"]["fiber"], 30.0)
    check("sodium target", s["targets"]["sodium"], 2000.0)
    check("sodium sum", s["totals"]["sodium"], 800.0)
    check("sodium remaining", s["remaining"]["sodium"], 1200.0)

    print("\n--- three sets in force ---")
    d0 = (TODAY - timedelta(days=5)).isoformat()
    c.put(f"/api/persons/{pid}/targets", json={"effective_on": d0, "energy": 7000,
                                               "protein": 120})
    allsets = c.get(f"/api/persons/{pid}/targets").json()
    check_true("three dated sets listed", len(allsets) == 3, str(len(allsets)))
    check("listed oldest first", [s["effective_on"] for s in allsets],
          [d1, d2, d0])
    check("d2 now resolves to the d2 set",
          c.get(f"/api/day/{d2}?person_id={pid}").json()["targets"]["energy"], 10000.0)
    check("a day between d0 and today resolves to the d0 set",
          c.get(f"/api/day/{TODAY.isoformat()}?person_id={pid}").json()
          ["targets"]["energy"], 7000.0)
    check("and its protein from the same set",
          c.get(f"/api/day/{TODAY.isoformat()}?person_id={pid}").json()
          ["targets"]["protein"], 120.0)
    check_true("carbs not restated by that set -> off",
               c.get(f"/api/day/{TODAY.isoformat()}?person_id={pid}").json()
               ["targets"]["carbs"] is None)
    check("d1 still resolves to the original set",
          c.get(f"/api/day/{d1}?person_id={pid}").json()["targets"]["energy"], 8500.0)

    print("\n--- editing a set in place ---")
    c.put(f"/api/persons/{pid}/targets", json={"effective_on": d0, "energy": 7200})
    check_true("still three sets", len(c.get(f"/api/persons/{pid}/targets").json()) == 3)
    check("the d0 set changed",
          c.get(f"/api/day/{TODAY.isoformat()}?person_id={pid}").json()
          ["targets"]["energy"], 7200.0)

    print("\n--- deleting a set falls back to the one before it ---")
    sets = c.get(f"/api/persons/{pid}/targets").json()
    mid = next(s for s in sets if s["effective_on"] == d0)
    assert c.delete(f"/api/targets/{mid['id']}").status_code == 200
    check("today falls back to the d2 set",
          c.get(f"/api/day/{TODAY.isoformat()}?person_id={pid}").json()
          ["targets"]["energy"], 10000.0)

    print("\n--- before any target existed ---")
    fresh = c.post("/api/persons", json={"name": "Fresh", "dob": "1995-05-05",
                                         "height_cm": 165}).json()["id"]
    for s in c.get(f"/api/persons/{fresh}/targets").json():
        c.delete(f"/api/targets/{s['id']}")
    day = c.get(f"/api/day/{TODAY.isoformat()}?person_id={fresh}").json()
    check_true("no target set means no target", day["targets"]["energy"] is None)
    check_true("and the effective date is null", day["targets_effective_on"] is None)
    check_true("history before the first set is targetless, not wrong",
               c.get(f"/api/day/2020-01-01?person_id={fresh}").json()
               ["targets"]["energy"] is None)

    print("\n--- validation ---")
    check_true("future effective date rejected", c.put(
        f"/api/persons/{pid}/targets",
        json={"effective_on": (TODAY + timedelta(days=3)).isoformat(),
              "energy": 8000}).status_code == 400)
    check_true("negative target rejected", c.put(
        f"/api/persons/{pid}/targets", json={"energy": -1}).status_code == 422)
    check_true("missing person 404s", c.put(
        "/api/persons/9999/targets", json={"energy": 8000}).status_code == 404)

    print("\n--- display unit conversion on dated sets ---")
    c.put("/api/settings", json={"energy_display": "kcal"})
    s = c.get(f"/api/day/{d2}?person_id={pid}").json()
    check("dated energy target in kcal", s["targets"]["energy"], 10000 / KJ, 0.001)
    check("energy converted", s["totals"]["energy"], 9000 / KJ, 0.001)
    rows = c.get(f"/api/persons/{pid}/targets").json()
    by_date = {r["effective_on"]: r for r in rows}
    check("listed sets in kcal", by_date[d2]["energy"], 10000 / KJ, 0.001)
    check("d0's set is gone", d0 in by_date, False)
    check_true("macros untouched by the display unit", s["targets"]["protein"] == 150.0)
    c.put("/api/settings", json={"energy_display": "kj"})

    print("\n--- the target scenario on the forecast ---")
    c.post(f"/api/persons/{pid}/weights", json={"measured_on": TODAY.isoformat(),
                                                "weight_kg": 80.0})
    c.post(f"/api/persons/{pid}/weights",
           json={"measured_on": (TODAY - timedelta(days=3)).isoformat(),
                 "weight_kg": 80.3})
    fc = c.get(f"/api/persons/{pid}/forecast").json()
    check_true("forecast built", fc["ok"] is True, fc.get("reason") or "")
    sc = fc["target_scenario"]
    check_true("scenario present", sc is not None)
    check("scenario uses the CURRENT target", sc["target_kj"], 10000.0)
    check("365 points", len(sc["points"]), 365)
    check_true("starts at today's weight",
               abs(sc["points"][0] - (80.0 + sc["daily_change_kg"])) < 1e-9,
               f"{sc['points'][0]:.4f}")
    check_true("slope matches the arithmetic",
               abs(sc["points"][299] - (80.0 + 300 * sc["daily_change_kg"])) < 1e-9)
    check_true("a surplus projects upward",
               sc["daily_change_kg"] > 0, f"{sc['daily_change_kg']*1000:.2f} g/day")

    c.put(f"/api/persons/{pid}/targets", json={"effective_on": TODAY.isoformat(),
                                               "energy": 8000})
    fc2 = c.get(f"/api/persons/{pid}/forecast").json()
    check_true("recomputed against the new target",
               fc2["target_scenario"]["daily_change_kg"] != sc["daily_change_kg"])

    c.put(f"/api/persons/{pid}/targets", json={"effective_on": TODAY.isoformat()})
    fc3 = c.get(f"/api/persons/{pid}/forecast").json()
    check_true("no energy target -> no scenario line", fc3["target_scenario"] is None)

    print("\n--- deleting a person takes their target sets with them ---")
    n_before = len(c.get(f"/api/persons/{pid}/targets").json())
    check_true("had target sets", n_before > 0)
    c.delete(f"/api/persons/{pid}")
    check_true("person and their targets are gone",
               c.get(f"/api/persons/{pid}/targets").status_code in (404, 400),
               str(c.get(f"/api/persons/{pid}/targets").status_code))

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    sys.exit(1)
print("ALL DATED-TARGET CHECKS PASSED")