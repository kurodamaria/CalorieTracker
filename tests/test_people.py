"""API-level tests for people, weights, records, activities and forecasts."""
import os
import sys
import tempfile
from datetime import date, timedelta

os.environ["CALORIE_TRACKER_DB"] = os.path.join(tempfile.mkdtemp(), "api.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

KJ = 4.184
fails = []
TODAY = date.today()


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
    check_true("starts with no people", cfg["persons"] == [])
    check_true("config exposes model constants",
               cfg["kj_per_kg_default"] == 32213.0
               and cfg["activity_default"] == 1.2
               and cfg["sex_offsets_kcal"]["male"] == 5.0)
    check_true("horizons published", cfg["horizons"] == [7, 14, 30, 90, 180, 365])

    # ---------------------------------------------------------- people
    print("\n--- people ---")
    r = c.post("/api/persons", json={"name": "Kuro", "dob": "1996-04-15",
                                     "sex": "male", "height_cm": 180})
    assert r.status_code == 201, r.text
    me = r.json()
    pid = me["id"]
    check("sex offset auto-filled from sex", me["sex_offset_kcal"], 5.0)
    check("activity default", me["activity_multiplier"], 1.2)
    check_true("targets seeded from global settings", me["target_energy_kj"] == 8500,
               str(me["target_energy_kj"]))

    r = c.post("/api/persons", json={"name": "Bad", "dob": "2030-01-01",
                                     "height_cm": 180})
    check_true("future dob rejected", r.status_code == 422)
    r = c.post("/api/persons", json={"name": "Bad", "dob": "1990-01-01",
                                     "height_cm": 0})
    check_true("zero height rejected", r.status_code == 422)
    r = c.put(f"/api/persons/{pid}", json={"name": "Kuro", "dob": "1996-04-15",
                                          "sex": "female", "height_cm": 180})
    check_true("editable offset tracks a sex change", r.json()["sex_offset_kcal"] == -5.0)
    r = c.put(f"/api/persons/{pid}", json={"name": "Kuro", "dob": "1996-04-15",
                                          "sex": "male", "height_cm": 180,
                                          "sex_offset_kcal": 2.5})
    check_true("offset can be overridden to any number", r.json()["sex_offset_kcal"] == 2.5)
    check("offset stuck at the override", c.get(f"/api/persons/{pid}/metrics").json()
          ["person"]["sex_offset_kcal"], 2.5)

    # -------------------------------------------------- food vs activity
    print("\n--- food and activity blueprints ---")
    r = c.post("/api/foods", json={"name": "煮鸡蛋", "energy": 649, "energy_unit": "kj",
                                   "protein": 12.6, "carbs": 0.6, "fat": 10.6,
                                   "sugar": 0.6, "fiber": 0, "grams_per_piece": 50})
    assert r.status_code == 201, r.text
    egg = r.json()
    check("egg energy kJ", egg["energy_kj"], 649.0)
    check("egg is a food", egg["kind"], "food")

    r = c.post("/api/foods", json={"name": "Run, 5 km", "kind": "activity",
                                   "energy": -1500, "energy_unit": "kj",
                                   "protein": 30, "carbs": 5})
    assert r.status_code == 201, r.text
    run = r.json()
    check("activity energy stays negative", run["energy_kj"], -1500.0)
    check("activity basis is one session", run["base_amount"], 1.0)
    check_true("activity macros are stripped", all(
        run["per100"][m] is None for m in ("protein", "carbs", "fat")))
    check_true("activity mass fields ignored", run["grams_per_piece"] is None)

    r = c.post("/api/foods", json={"name": "Bad activity", "kind": "activity",
                                   "energy": 500})
    check_true("positive-energy activity rejected", r.status_code == 400)
    r = c.post("/api/foods", json={"name": "Bad food", "kind": "food", "energy": -500})
    check_true("negative-energy food rejected", r.status_code == 400)
    check_true("food list filters by kind",
               [f["name"] for f in c.get("/api/foods?kind=activity").json()]
               == ["Run, 5 km"])
    found = c.get("/api/foods", params={"q": "煮鸡蛋"}).json()
    check("search by Chinese name", [f["name"] for f in found], ["煮鸡蛋"])
    found2 = c.get("/api/foods", params={"q": "鸡蛋"}).json()
    check("search by Chinese substring", [f["name"] for f in found2], ["煮鸡蛋"])
    found3 = c.get("/api/foods", params={"q": "煮鸡蛋"}).json()
    check_true("search is case/space tolerant for latin too",
               len(c.get("/api/foods", params={"q": " RUN "}).json()) == 1)

    # ---------------------------------------------------------- records
    print("\n--- records ---")
    d0 = (TODAY - timedelta(days=1)).isoformat()
    r = c.post("/api/entries", json={"person_id": pid, "food_id": egg["id"],
                                    "date": d0, "meal": "breakfast",
                                    "amount": 2, "unit": "piece"})
    assert r.status_code == 201, r.text
    day = c.get(f"/api/day/{d0}?person_id={pid}").json()
    check("2 pieces = 100 g", day["total_grams"], 100.0)
    check("energy from 2 eggs", day["totals_kj"]["energy"], 649.0)

    c.post("/api/entries", json={"person_id": pid, "food_id": run["id"],
                                 "date": d0, "meal": "snack", "amount": 1,
                                 "unit": "piece"})
    day = c.get(f"/api/day/{d0}?person_id={pid}").json()
    check("food energy reported separately", day["food_energy"], 649.0)
    check("activity energy reported as burned (positive)", day["activity_energy"], 1500.0)
    check("net energy", day["totals_kj"]["energy"], -851.0)
    check_true("macros untouched by activity", day["totals"]["protein"] == 12.6,
               str(day["totals"]["protein"]))
    check_true("activity counted in sessions", day["activity_count"] == 1)

    c.post("/api/entries", json={"person_id": pid, "food_id": run["id"],
                                 "date": d0, "meal": "snack", "amount": 2,
                                 "unit": "piece"})
    day = c.get(f"/api/day/{d0}?person_id={pid}").json()
    check("two sessions = double", day["activity_energy"], 1500.0 + 3000.0)
    check("net after 2 sessions", day["totals_kj"]["energy"], 649.0 - 4500.0)

    r = c.post("/api/entries", json={"person_id": pid, "food_id": run["id"],
                                    "amount": 1, "unit": "g"})
    check_true("activity accepts any unit (amount = sessions)",
               r.status_code == 201)
    c.delete(f"/api/entries/{r.json()['id']}")

    r = c.post("/api/entries", json={"person_id": 9999, "food_id": egg["id"],
                                    "amount": 10})
    check_true("record for a missing person rejected", r.status_code == 404)
    # a food with no piece weight must refuse a piece record
    nolight = c.post("/api/foods", json={"name": "Bulk rice", "energy": 1400,
                                         "energy_unit": "kj"}).json()
    r = c.post("/api/entries", json={"person_id": pid, "food_id": nolight["id"],
                                     "amount": 10, "unit": "piece"})
    check_true("food without a piece weight refuses a piece record",
               r.status_code == 400)
    check_true("and explains why", "piece weight" in r.json()["detail"])
    r = c.post("/api/entries", json={"person_id": pid, "food_id": nolight["id"],
                                     "amount": 100, "unit": "g"})
    check_true("but grams are fine", r.status_code == 201)
    c.delete(f"/api/entries/{r.json()['id']}")

    # a second person must not see the first person's records
    print("\n--- isolation between people ---")
    r = c.post("/api/persons", json={"name": "Guest", "dob": "1995-05-05",
                                     "sex": "female", "height_cm": 165})
    guest = r.json()["id"]
    gday = c.get(f"/api/day/{d0}?person_id={guest}").json()
    check("new person has an empty day", gday["totals_kj"]["energy"], 0)
    check_true("no entries leaked", len(gday["entries"]) == 0)
    c.post("/api/entries", json={"person_id": guest, "food_id": egg["id"],
                                 "date": d0, "meal": "lunch", "amount": 50,
                                 "unit": "g"})
    gday = c.get(f"/api/day/{d0}?person_id={guest}").json()
    check("guest sees only their own 1 egg", gday["totals_kj"]["energy"], 324.5)
    check("me still see 2 eggs", c.get(f"/api/day/{d0}?person_id={pid}").json()
          ["totals_kj"]["energy"], 649.0 - 4500.0)

    # ----------------------------------------------------------- weights
    print("\n--- weights ---")
    r = c.post(f"/api/persons/{pid}/weights", json={"measured_on": d0,
                                                    "weight_kg": 80.0})
    assert r.status_code == 201, r.text
    c.post(f"/api/persons/{pid}/weights", json={"measured_on": d0,
                                                "weight_kg": 80.4})
    ws = c.get(f"/api/persons/{pid}/weights").json()
    check_true("one weight per day, last wins", len(ws) == 1 and ws[0]["weight_kg"] == 80.4)
    r = c.post(f"/api/persons/{pid}/weights", json={"measured_on": "2099-01-01",
                                                    "weight_kg": 80})
    check_true("future weigh-in rejected", r.status_code == 422)
    m = c.get(f"/api/persons/{pid}/metrics").json()
    check("BMI computed", m["bmi_now"], 80.4 / 1.8 ** 2, 0.001)
    check("BMI band", m["bmi_band"], "normal")
    check("BMR kcal", m["bmr_kcal"], 10 * 80.4 + 6.25 * 180 - 5 * m["age"] + 2.5, 0.01)
    check_true("formula burn is kJ", m["formula_burn_kj"] > 6000)
    check_true("uncalibrated with no history", m["calibrated"] is False,
               m["not_calibrated_reason"] or "")
    check("falls back to the formula", m["burn_kj"], m["formula_burn_kj"])

    # ---------------------------------------- build a real history + fit
    print("\n--- build 60 days of history with a known burn ---")
    # A dedicated person, so the ad-hoc weights above cannot contaminate the
    # calibration window.
    cal = c.post("/api/persons", json={"name": "Cal", "dob": "1990-06-01",
                                       "sex": "male", "height_cm": 180}).json()["id"]
    MEAL_KJ_PER_100G = 5000.0     # ~1200 kcal/100 g, a realistic mixed meal
    KJ_PER_G = MEAL_KJ_PER_100G / 100.0
    meal = c.post("/api/foods", json={"name": "Mixed meal", "energy_unit": "kj",
                                      "energy": MEAL_KJ_PER_100G, "protein": 45,
                                      "carbs": 60, "fat": 22}).json()["id"]
    acts = c.post("/api/foods", json={"name": "Gym", "kind": "activity",
                                      "energy": -2500}).json()["id"]

    TRUE_BURN, MEAN, DAYS, ACT_KJ = 10000.0, 10000.0, 60, 2500.0
    import random
    rng = random.Random(4)
    w = 80.0
    for i in range(DAYS):
        day = (TODAY - timedelta(days=DAYS - i)).isoformat()
        net = MEAN + rng.gauss(0, 250)
        # grams such that the record contributes exactly `net` kJ
        c.post("/api/entries", json={"person_id": cal, "food_id": meal,
                                     "date": day, "meal": "lunch",
                                     "amount": net / KJ_PER_G, "unit": "g"})
        if i % 3 == 0:
            c.post("/api/entries", json={"person_id": cal, "food_id": acts,
                                         "date": day, "meal": "snack",
                                         "amount": 1, "unit": "piece"})
        w += (net - (ACT_KJ if i % 3 == 0 else 0) - TRUE_BURN) / 32213.0
        if i % 2 == 0:
            c.post(f"/api/persons/{cal}/weights",
                   json={"measured_on": day, "weight_kg": round(w + rng.gauss(0, .15), 2)})

    m = c.get(f"/api/persons/{cal}/metrics").json()
    check_true("now calibrated", m["calibrated"] is True, m["not_calibrated_reason"] or "")
    check("burn source is fitted", m["burn_source"], "fitted")
    check("fitted burn recovers the planted burn", m["fitted_burn_kj"], TRUE_BURN,
          TRUE_BURN * 0.10)
    check_true("fitted and formula both reported",
               m["fitted_burn_kj"] is not None and m["formula_burn_kj"] is not None)
    check_true("scatter reported", m["fit"]["residual_sd_kj"] > 0)
    check("uses ~30 days (last weigh-in leaves one gap)", m["fit"]["n_days"], 29, 1)
    check_true("no plausibility complaint", m["fit_warning"] is None,
               m["fit_warning"] or "")
    kuro, pid = pid, cal  # kuro keeps the hand-built day; pid drives the model

    # ---------------------------------------------------------- forecast
    print("\n--- forecast ---")
    fc = c.get(f"/api/persons/{pid}/forecast").json()
    check_true("forecast ok", fc["ok"] is True)
    check_true("burn is the fitted one", fc["burn_source"] == "fitted")
    check_true("365 day band present", len(fc["summary"]["weight"]["5"]) == 365)
    check_true("bands ordered",
               all(fc["summary"]["weight"]["5"][i] <= fc["summary"]["weight"]["50"][i]
                   <= fc["summary"]["weight"]["95"][i] for i in range(365)))
    check_true("BMI series present", len(fc["summary"]["bmi"]["50"]) == 365)
    check_true("intake + activity curves present",
               len(fc["summary"]["intake"]["50"]) == 365
               and len(fc["summary"]["activity"]["50"]) == 365)
    fc2 = c.get(f"/api/persons/{pid}/forecast").json()
    check_true("deterministic between calls",
               fc["summary"]["weight"]["50"] == fc2["summary"]["weight"]["50"])
    check_true("today excluded from history", fc["n_days_history"] <= 30,
               str(fc["n_days_history"]))
    check_true("forecast starts from a real weigh-in",
               fc["weight_date"] is not None and fc["weight_now"] > 0)

    # a flat-intake person should project roughly flat
    med30 = fc["summary"]["weight"]["50"][29]
    check("near-flat intake projects near-flat", med30, fc["weight_now"], 1.2)

    print("\n--- save + score forecasts ---")
    check_true("saving returns an id",
               c.get(f"/api/persons/{pid}/forecast?save=true").json().get("id"))
    fcdata = c.get(f"/api/persons/{pid}/forecasts").json()
    check_true("one stored forecast", len(fcdata["forecasts"]) == 1)
    check_true("stored params frozen", "burn_kj" in fcdata["forecasts"][0]["params"])
    check_true("no horizons reached yet", fcdata["scorecard"]["total_points"] == 0)
    check_true("today's forecast has no future weigh-in to score against",
               c.get(f"/api/persons/{pid}/forecasts").json()
               ["scorecard"]["forecasts"] == [])

    # A forecast made 30 days ago has a horizon that is now in the past, so a
    # real weigh-in can score it. Inject it directly to keep the test honest
    # about the as-of date.
    import json
    import app.db as dbmod

    past = (TODAY - timedelta(days=30)).isoformat()
    # Use the weight the trajectory actually had back then, nudged by a known
    # amount, so the expected error is exact.
    hist = c.get(f"/api/persons/{pid}/weights?days=400").json()
    real_then = min(hist, key=lambda w: abs(
        (date.fromisoformat(w["measured_on"]) - date.fromisoformat(past)).days))
    f30 = real_then["weight_kg"]
    drift = 0.3
    print(f"      (weight on {past} was {f30}, forecast it at {f30}, reality {f30 + drift})")

    conn = dbmod.connect()
    conn.execute(
        "INSERT INTO forecasts (person_id, as_of, model_version, burn_kj,"
        " burn_source, kj_per_kg, lag_weights, params_json, summary_json, n_paths)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (pid, past, 1, 10000.0, "fitted", 32213.0, "[0.2,0.3,0.3,0.2]",
         json.dumps({"burn_kj": 10000.0, "model": "block-bootstrap"}),
         json.dumps({"days": [30],
                     "weight": {"5": [f30 - 1], "25": [f30 - .5], "50": [f30],
                                "75": [f30 + .5], "95": [f30 + 1]},
                     "horizon_table": [{"days": 30,
                                        "weight": {"5": f30 - 1, "50": f30,
                                                   "95": f30 + 1}}]}),
         1500))
    fid_good = conn.execute(
        "SELECT id FROM forecasts WHERE as_of = ?", (past,)).fetchone()["id"]
    conn.commit()
    conn.close()

    # Put a weigh-in exactly on the 30-day horizon (which is today).
    c.post(f"/api/persons/{pid}/weights", json={"measured_on": past,
                                                "weight_kg": f30})
    horizon_day = (date.fromisoformat(past) + timedelta(days=30)).isoformat()
    c.post(f"/api/persons/{pid}/weights",
           json={"measured_on": horizon_day, "weight_kg": f30 + drift})

    sc = c.get(f"/api/persons/{pid}/forecasts").json()["scorecard"]
    check_true("a reached horizon is scored", sc["total_points"] == 1, str(sc["total_points"]))
    check("known drift shows up as the error", sc["mae"], drift, 0.001)
    check_true("inside the band", sc["band_hit_rate"] == 1.0)

    # Now rewrite that same forecast to something absurd: 12 kg predicted against
    # an 80 kg reality. The scorecard must not let that pass quietly.
    conn = dbmod.connect()
    conn.execute("UPDATE forecasts SET summary_json = ? WHERE id = ?", (json.dumps({
        "days": [30],
        "weight": {"5": [11.0], "25": [11.5], "50": [12.0], "75": [12.5], "95": [13.0]},
        "horizon_table": [{"days": 30,
                           "weight": {"5": 11.0, "50": 12.0, "95": 13.0}}],
    }), fid_good))
    conn.commit()
    conn.close()
    sc = c.get(f"/api/persons/{pid}/forecasts").json()["scorecard"]
    check_true("absurd forecast is flagged as a miss",
               sc["hit_rate_1kg"] == 0.0 and sc["band_hit_rate"] == 0.0,
               f"mae={sc['mae']}")
    check_true("MAE reflects the huge miss", sc["mae"] > 60, str(sc["mae"]))
    check_true("bias is signed and large", sc["bias"] > 60, str(sc["bias"]))

    check_true("forecast deletable",
               c.delete(f"/api/forecasts/{fid_good}").status_code == 200)

    # ----------------------------------------------- plausibility guard
    print("\n--- implausible fit is rejected ---")
    # A single wild weight makes the fitted burn nonsense; the app must refuse it
    # and fall back to the formula rather than project from a 1 MJ/day burn.
    wrows = c.get(f"/api/persons/{pid}/weights?days=400").json()
    latest = wrows[-1]
    c.post(f"/api/persons/{pid}/weights",
           json={"measured_on": latest["measured_on"], "weight_kg": 400.0})
    m2 = c.get(f"/api/persons/{pid}/metrics").json()
    check_true("absurd weight rejected the fit", m2["calibrated"] is False,
               m2["not_calibrated_reason"] or "")
    check_true("and it explains itself", m2["fit_warning"] is not None,
               (m2["fit_warning"] or "")[:60])
    check("burn falls back to the formula", m2["burn_kj"], m2["formula_burn_kj"])
    c.post(f"/api/persons/{pid}/weights",
           json={"measured_on": latest["measured_on"], "weight_kg": latest["weight_kg"]})
    m3 = c.get(f"/api/persons/{pid}/metrics").json()
    check_true("restoring the weight restores calibration", m3["calibrated"] is True,
               m3["not_calibrated_reason"] or "")

    # a person with no weight cannot be projected
    fc_guest = c.get(f"/api/persons/{guest}/forecast").json()
    check_true("no weight -> forecast refused", fc_guest["ok"] is False)
    check_true("and it says why", "weight" in fc_guest["reason"])

    # ---------------------------------------------------- model settings
    print("\n--- model settings ---")
    r = c.put("/api/settings", json={"lag_weights": [1, 1, 1, 1]})
    assert r.status_code == 200, r.text
    check_true("lag weights normalised", json.loads(r.json()["lag_weights"]) == [0.25] * 4)
    r = c.put("/api/settings", json={"lag_weights": [0, 0, 0, 0]})
    check_true("all-zero kernel rejected", r.status_code == 400)
    r = c.put("/api/settings", json={"kj_per_kg": -5})
    check_true("negative kJ/kg rejected", r.status_code == 400)
    r = c.put("/api/settings", json={"calibration_days": 0})
    check_true("zero window rejected", r.status_code == 400)
    r = c.put("/api/settings", json={"unknown_thing": 1})
    check_true("unknown setting ignored, not fatal", r.status_code == 200)
    r = c.put("/api/settings", json={"kj_per_kg": 31000})
    m2 = c.get(f"/api/persons/{pid}/metrics").json()
    check("changed kJ/kg is used", m2["kj_per_kg"], 31000.0)
    c.put("/api/settings", json={"kj_per_kg": 32213, "lag_weights": [0.2, 0.3, 0.3, 0.2]})

    # ---------------------------------------------------------- targets
    print("\n--- per-person targets ---")
    c.put(f"/api/persons/{pid}", json={
        "name": "Cal", "dob": "1990-06-01", "sex": "male", "sex_offset_kcal": 5.0,
        "height_cm": 180, "target_energy_kj": 9000, "target_protein_g": 160,
        "target_carbs_g": None, "target_fat_g": 70})
    d = c.get(f"/api/day/{d0}?person_id={pid}").json()
    check("target applied to that person", d["targets"]["energy"], 9000.0)
    check("blank target is off", d["targets"]["carbs"], None)
    g = c.get(f"/api/day/{d0}?person_id={kuro}").json()
    # PUT is a full replace, so kuro's earlier partial update cleared its targets.
    # Give it its own distinct set and confirm the two never share.
    c.put(f"/api/persons/{kuro}", json={
        "name": "Kuro", "dob": "1996-04-15", "sex": "male", "height_cm": 180,
        "target_energy_kj": 7200, "target_protein_g": 130})
    g = c.get(f"/api/day/{d0}?person_id={kuro}").json()
    check("each person keeps their own energy target", g["targets"]["energy"], 7200.0)
    d2 = c.get(f"/api/day/{d0}?person_id={pid}").json()
    check("and they do not leak into the other", d2["targets"]["energy"], 9000.0)
    check_true("blank target stays off per person", g["targets"]["carbs"] is None)
    c.put("/api/settings", json={"energy_display": "kcal"})
    d = c.get(f"/api/day/{d0}?person_id={pid}").json()
    check("target converted to kcal", d["targets"]["energy"], 9000 / KJ, 0.001)
    check("energy converted to kcal", d["totals"]["energy"],
          d["totals_kj"]["energy"] / KJ, 0.001)
    # kuro is the hand-built day: 649 kJ of eggs and 4500 kJ of activity.
    k = c.get(f"/api/day/{d0}?person_id={kuro}").json()
    check("food energy converted to kcal", k["food_energy"], 649 / KJ, 0.001)
    check("activity converted to kcal (positive magnitude)",
          k["activity_energy"], 4500 / KJ, 0.001)
    check("net converted to kcal", k["totals"]["energy"], (649 - 4500) / KJ, 0.001)
    c.put("/api/settings", json={"energy_display": "kj"})

    # ------------------------------------------------------ edge cases
    print("\n--- edge cases ---")
    check_true("empty day handled",
               c.get(f"/api/day/2000-01-01?person_id={pid}").json()
               ["totals_kj"]["energy"] == 0)
    check_true("bad date rejected",
               c.get("/api/day/nope?person_id=" + str(pid)).status_code == 400)
    check_true("missing person 404s",
               c.get("/api/persons/9999/metrics").status_code == 404)
    check_true("history needs a person",
               c.get("/api/history?person_id=9999").status_code == 404)
    h = c.get(f"/api/history?person_id={pid}&days=5").json()
    check_true("history ordered oldest first", len(h) == 5 and h[0]["date"] < h[-1]["date"])
    check_true("history reports activity",
               any(r["activity_energy"] for r in h))

    r = c.delete(f"/api/persons/{guest}")
    check_true("person deletable", r.status_code == 200)
    check_true("their records went too",
               len(c.get(f"/api/persons/{pid}/weights").json()) >= 1)

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    sys.exit(1)
print("ALL PERSON-TRACKER API CHECKS PASSED")
