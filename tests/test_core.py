"""Blueprint-level tests: nutrition maths and the food/activity split.

These are the checks from the first version that still apply, now that foods can
be negative-energy activities and records belong to a person.
"""
import os
import sys
import tempfile

os.environ["CALORIE_TRACKER_DB"] = os.path.join(tempfile.mkdtemp(), "core.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

KJ = 4.184
fails = []


def check(label, got, want, tol=0.01):
    if isinstance(want, (str, list)) or isinstance(got, (str, list)) \
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
    check("default display unit is kJ", cfg["energy_display"], "kj")
    check("meals", cfg["meals"], ["breakfast", "lunch", "dinner", "snack"])
    check("kcal conversion is exact", cfg["kj_per_kcal"], KJ)

    pid = c.post("/api/persons", json={"name": "T", "dob": "1990-01-01",
                                       "sex": "female", "height_cm": 165}).json()["id"]

    # ------------------------------------------------ per-100 g basis
    print("\n--- entering values for any amount ---")
    r = c.post("/api/foods", json={"name": "Pasta, dry", "brand": "Barilla",
                                   "base_amount": 250, "energy_unit": "kj",
                                   "energy": 3750, "carbs": 775, "protein": 125,
                                   "fat": 15, "grams_per_ml": 0.45})
    assert r.status_code == 201, r.text
    pasta = r.json()
    check("per-250 g scaled to per-100 g energy", pasta["energy_kj"], 1500.0)
    check("per-250 g scaled carbs", pasta["per100"]["carbs"], 310.0)
    check_true("blank fields marked unknown",
               not pasta["known"]["sugar"] and not pasta["known"]["fiber"])
    check("base amount remembered for the edit form", pasta["base_amount"], 250.0)
    check("entered unit remembered", pasta["base_unit"], "kj")

    kcalfood = c.post("/api/foods", json={"name": "Chicken breast", "energy_unit": "kcal",
                                          "energy": 165, "protein": 31, "fat": 3.6,
                                          "sugar": 0, "fiber": 0,
                                          "grams_per_piece": 120}).json()
    check("kcal entry converted to kJ on store", kcalfood["energy_kj"], 165 * KJ)
    check("its own unit retained for editing", kcalfood["base_unit"], "kcal")

    edited = c.get(f"/api/foods/{kcalfood['id']}").json()
    check("stored kJ available to the editor",
          edited["stored"]["energy_kj"], 165 * KJ)

    # ------------------------------------------------------- day maths
    print("\n--- day aggregation across units ---")
    d = "2026-01-15"
    CHICKEN_KJ = 200 * 165 * KJ / 100          # 200 g at 165 kcal/100 g
    c.post("/api/entries", json={"person_id": pid, "food_id": kcalfood["id"],
                                 "date": d, "meal": "lunch", "amount": 200, "unit": "g"})
    day = c.get(f"/api/day/{d}?person_id={pid}").json()
    check("200 g chicken alone", day["totals_kj"]["energy"], CHICKEN_KJ, 0.01)
    check("its grams", day["total_grams"], 200.0)

    c.post("/api/entries", json={"person_id": pid, "food_id": pasta["id"],
                                 "date": d, "meal": "dinner", "amount": 100, "unit": "g"})
    day = c.get(f"/api/day/{d}?person_id={pid}").json()
    check("plus 100 g pasta", day["totals_kj"]["energy"], CHICKEN_KJ + 1500, 0.01)

    c.post("/api/entries", json={"person_id": pid, "food_id": pasta["id"],
                                 "date": d, "meal": "dinner", "amount": 111.1,
                                 "unit": "ml"})
    day = c.get(f"/api/day/{d}?person_id={pid}").json()
    ML_G = 111.1 * 0.45                        # density 0.45 g/ml
    check("plus 111.1 ml pasta at 0.45 g/ml", day["totals_kj"]["energy"],
          CHICKEN_KJ + 1500 + ML_G * 15, 0.01)
    check("grams resolved across units", day["total_grams"], 200 + 100 + ML_G, 0.001)
    check("protein", day["totals"]["protein"], 62 + 50 + ML_G * 0.5, 0.01)
    check("carbs", day["totals"]["carbs"], 0 + 310 + ML_G * 3.1, 0.01)
    check("protein fully covered", day["coverage"]["protein"], 1.0)
    check_true("sugar flagged partial", day["partial"]["sugar"] is True)
    check_true("fiber flagged partial", day["partial"]["fiber"] is True)
    check_true("protein not flagged", day["partial"]["protein"] is False)

    # ------------------------------------------------- macro coverage
    print("\n--- missing fields are surfaced, not hidden ---")
    # Only the chicken carries sugar, so coverage is its share of the day's mass.
    check("sugar coverage is the share with data",
          day["coverage"]["sugar"], 200 / (200 + 100 + ML_G), 0.002)
    check("sugar sum uses only the food that has it",
          day["totals"]["sugar"], 0.0)
    check_true("a fully-covered nutrient is not partial",
               day["partial"]["fat"] is False)

    d2 = "2026-01-16"
    c.post("/api/entries", json={"person_id": pid, "food_id": pasta["id"],
                                 "date": d2, "meal": "dinner", "amount": 100, "unit": "g"})
    day2 = c.get(f"/api/day/{d2}?person_id={pid}").json()
    check("nothing recorded the value -> reported as no data, not 0",
          day2["totals"]["sugar"], None)
    check("coverage says zero", day2["coverage"]["sugar"], 0.0)
    check_true("and it is flagged", day2["partial"]["sugar"] is True)

    # ---------------------------------------------------------- units
    print("\n--- display unit switching ---")
    c.put("/api/settings", json={"energy_display": "kcal"})
    day = c.get(f"/api/day/{d}?person_id={pid}").json()
    check("unit label", day["unit"], "kcal")
    check("energy in kcal", day["totals"]["energy"],
          day["totals_kj"]["energy"] / KJ, 0.001)
    check_true("macros unaffected by the display unit", day["totals"]["protein"] == 136.9975)
    check_true("blueprint list switches too",
               c.get("/api/foods?display=kcal").json()[0]["energy"]
               != c.get("/api/foods?display=kj").json()[0]["energy"])
    c.put("/api/settings", json={"energy_display": "kj"})

    # --------------------------------------------------- validation
    print("\n--- validation ---")
    for payload, why in (
        ({"name": "", "energy": 10}, "blank name"),
        ({"name": "x", "energy": 10, "base_amount": 0}, "zero basis"),
        ({"name": "x", "energy": 10, "energy_unit": "MJ"}, "bad energy unit"),
        ({"name": "x", "energy": 10, "kind": "spaceship"}, "bad kind"),
        ({"name": "x", "energy": 10, "protein": -1}, "negative macro"),
    ):
        r = c.post("/api/foods", json=payload)
        check_true(f"rejects {why}", r.status_code == 422, f"got {r.status_code}")

    # Signed energy is a semantic rule, not a schema rule, so it surfaces as 400.
    r = c.post("/api/foods", json={"name": "x", "energy": -5})
    check_true("negative-energy food rejected", r.status_code == 400)
    check_true("and says why", "activity" in r.json()["detail"])
    r = c.post("/api/foods", json={"name": "x", "kind": "activity", "energy": 5})
    check_true("positive-energy activity rejected", r.status_code == 400)

    # Zero is legitimate: water and black tea really do have no energy.
    water = c.post("/api/foods", json={"name": "Water", "energy": 0}).json()
    check("zero-energy food is allowed", water["energy_kj"], 0.0)
    check_true("and counts as known, not missing", water["known"]["energy"] is True)
    check("per100 stays zero", water["per100"]["energy"], 0.0)

    check_true("missing food 404s", c.get("/api/foods/9999").status_code == 404)
    check_true("missing blueprint on update 404s",
               c.put("/api/foods/9999", json={"name": "x", "energy": 10}).status_code == 404)
    check_true("missing food on delete 404s",
               c.delete("/api/foods/9999").status_code == 404)

    print("\n--- crud ---")
    new = c.post("/api/foods", json={"name": "Oats", "base_amount": 100,
                                     "energy_unit": "kj", "energy": 1,
                                     "protein": 13.2, "carbs": 67.7, "fat": 6.5}).json()
    upd = c.put(f"/api/foods/{new['id']}", json={"name": "Rolled oats",
                                                 "base_amount": 40, "energy_unit": "kj",
                                                 "energy": 1500, "protein": 5.3,
                                                 "carbs": 27, "fat": 2.6}).json()
    check("update rescales to per-100 g", upd["energy_kj"], 3750.0)
    check("name updated", upd["name"], "Rolled oats")
    check_true("delete works", c.delete(f"/api/foods/{upd['id']}").status_code == 200)
    check_true("then 404s", c.delete(f"/api/foods/{upd['id']}").status_code == 404)

    eid = c.post("/api/entries", json={"person_id": pid, "food_id": pasta["id"],
                                       "date": "2026-02-01", "amount": 100,
                                       "unit": "g"}).json()["id"]
    check("entry counted",
          len(c.get("/api/day/2026-02-01?person_id=%d" % pid).json()["entries"]), 1)
    c.delete(f"/api/entries/{eid}")
    check("entry removed",
          len(c.get("/api/day/2026-02-01?person_id=%d" % pid).json()["entries"]), 0)
    check_true("then 404s", c.delete(f"/api/entries/{eid}").status_code == 404)

    print("\n--- deleting a blueprint cascades its records ---")
    eid = c.post("/api/entries", json={"person_id": pid, "food_id": pasta["id"],
                                       "date": "2026-02-02", "amount": 100,
                                       "unit": "g"}).json()["id"]
    check("record exists",
          len(c.get("/api/day/2026-02-02?person_id=%d" % pid).json()["entries"]), 1)
    c.delete(f"/api/foods/{pasta['id']}")
    check("record went with it",
          len(c.get("/api/day/2026-02-02?person_id=%d" % pid).json()["entries"]), 0)

    print("\n--- search ---")
    check("prefix match",
          [f["name"] for f in c.get("/api/foods", params={"q": "chi"}).json()],
          ["Chicken breast"])
    branded = c.post("/api/foods", json={"name": "Greek yoghurt", "brand": "Fage",
                                         "energy": 405.848, "protein": 9,
                                         "carbs": 3.6, "fat": 5, "sugar": 3.6,
                                         "grams_per_piece": 170}).json()
    check("brand match",
          [f["name"] for f in c.get("/api/foods", params={"q": "Fage"}).json()],
          ["Greek yoghurt"])
    check("brand match is case-insensitive",
          len(c.get("/api/foods", params={"q": "fage"}).json()), 1)
    check("name match ignores surrounding spaces",
          len(c.get("/api/foods", params={"q": "  yoghurt "}).json()), 1)
    check("no match is an empty list, not an error",
          c.get("/api/foods", params={"q": "zzzz"}).json(), [])
    c.delete(f"/api/foods/{branded['id']}")

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    sys.exit(1)
print("ALL BLUEPRINT CHECKS PASSED")
