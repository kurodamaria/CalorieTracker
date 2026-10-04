"""Render the four screens with a realistic demo dataset -> shot-*.png"""
import os
import random
import sys
import tempfile
import threading
from datetime import date, timedelta

os.environ["CALORIE_TRACKER_DB"] = os.path.join(tempfile.mkdtemp(), "shots.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from app import repo  # noqa: E402
from app.db import db, init_db  # noqa: E402
from app.main import app  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "screenshots")
os.makedirs(OUT, exist_ok=True)
PORT = 8781
init_db()
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
threading.Thread(target=server.run, daemon=True).start()
while not server.started:
    pass

TODAY = date.today()
KJ_PER_G = 50.0

FOODS = [
    # name, kJ/100g, protein, carbs, fat, sodium mg, sugar, fiber, g/piece, brand
    ("煮鸡蛋", 649, 12.6, 0.6, 10.6, 131, 0.6, 0, 50, ""),
    ("Greek yoghurt", 406, 9.0, 3.6, 5.0, 36, 3.6, 0, 170, "Fage"),
    ("Oatflakes", 1585, 13.2, 67.7, 6.5, 62, 1.0, 10.1, None, "Quaker"),
    ("Whole milk", 268, 3.3, 4.8, 3.6, 44, 4.8, 0, None, ""),
    ("Chicken breast, grilled", 690, 31.0, 0.0, 3.6, 74, 0.0, 0, None, ""),
    # rice: the label lists no sodium, sugar or fiber - which is exactly the
    # case the coverage warnings exist for
    ("Basmati rice, dry", 1410, 8.5, 28.6, 0.6, None, None, None, None, ""),
    ("Almonds, raw", 2514, 21.2, 21.6, 49.9, 1, 4.4, 12.5, 1.2, ""),
    ("Salmon fillet", 2080, 20.0, 0.0, 13.0, 59, 0.0, 0, None, ""),
    ("Banana", 370, 1.1, 22.8, 0.3, 1, 12.2, 2.6, None, ""),
]
ACTIVITIES = [
    ("Run, 5 km", -1500),
    ("Strength session", -2200),
    ("Cycle, 40 min", -1800),
    ("Yoga, 45 min", -350),
]
MEAL_KJ = 5000.0          # the generated day-long meal record


def seed():
    with db() as conn:
        pid = repo.create_person(conn, {
            "name": "Test Person", "dob": "1990-03-15", "sex": "male",
            "sex_offset_kcal": 5.0, "height_cm": 175.0, "activity_multiplier": 1.25,
        })
        # Two dated target sets, so the screenshot shows a real history rather
        # than a single row: a cutting phase that has since been relaxed.
        repo.save_targets(conn, pid, (TODAY - timedelta(days=45)).isoformat(), {
            "target_energy_kj": 8000, "target_protein_g": 170,
            "target_carbs_g": 180, "target_fat_g": 60,
            "target_sugar_g": 40, "target_fiber_g": 30, "target_sodium_mg": 1800,
        })
        repo.save_targets(conn, pid, (TODAY - timedelta(days=10)).isoformat(), {
            "target_energy_kj": 9500, "target_protein_g": 150,
            "target_carbs_g": 220, "target_fat_g": 70,
            "target_sugar_g": 50, "target_fiber_g": 30, "target_sodium_mg": 2000,
        })
        repo.create_person(conn, {
            "name": "Guest", "dob": "1995-05-05", "sex": "female",
            "sex_offset_kcal": -5.0, "height_cm": 165.0, "activity_multiplier": 1.2,
        })

        food_ids = {}
        for name, kj, p, cb, f, na, s, fib, gpp, brand in FOODS:
            food_ids[name] = repo.create_food(conn, {
                "name": name, "brand": brand or None, "kind": "food",
                "base_amount": 100.0, "base_unit": "kj", "energy_kj": float(kj),
                "protein_g": p, "carbs_g": cb, "fat_g": f, "sodium_mg": na,
                "sugar_g": s, "fiber_g": fib, "grams_per_ml": 1.0,
                "grams_per_piece": gpp, "notes": None})
        act_ids = {n: repo.create_food(conn, {
            "name": n, "brand": None, "kind": "activity", "base_amount": 1.0,
            "base_unit": "kj", "energy_kj": float(k), "protein_g": None,
            "carbs_g": None, "fat_g": None, "sodium_mg": None, "sugar_g": None,
            "fiber_g": None, "grams_per_ml": 1.0, "grams_per_piece": None,
            "notes": None})
            for n, k in ACTIVITIES}

        meal = repo.create_food(conn, {
            "name": "Mixed meal", "brand": None, "kind": "food",
            "base_amount": 100.0, "base_unit": "kj", "energy_kj": MEAL_KJ,
            "protein_g": 45.0, "carbs_g": 60.0, "fat_g": 22.0, "sodium_mg": 1100,
            "sugar_g": 8.0, "fiber_g": 6.0, "grams_per_ml": 1.0,
            "grams_per_piece": None, "notes": None})

        # 60 days of history with a known burn, so the model calibrates
        rng = random.Random(11)
        w, drift = 82.0, 0.0
        for i in range(60):
            day = (TODAY - timedelta(days=60 - i)).isoformat()
            net = 9600 + rng.gauss(0, 600) + (900 if i % 7 >= 5 else 0)
            repo.add_entry(conn, {
                "person_id": pid, "food_id": meal, "logged_on": day,
                "meal": "lunch", "amount": net / KJ_PER_G, "unit": "g", "note": None})
            act = 0
            if i % 3 == 0:
                act = 2200
                repo.add_entry(conn, {
                    "person_id": pid, "food_id": act_ids["Strength session"],
                    "logged_on": day, "meal": "snack", "amount": 1,
                    "unit": "piece", "note": None})
            w += (net - act - 10000.0) / 32213.0
            if i % 2 == 0:
                repo.add_weight(conn, pid, day, round(w + rng.gauss(0, 0.2), 1))
            drift = w

        # today: a real-looking breakfast, lunch and a run
        today = TODAY.isoformat()
        plan = [("Oatflakes", "breakfast", 70, "g"),
                ("Whole milk", "breakfast", 250, "ml"),
                ("煮鸡蛋", "breakfast", 2, "piece"),
                ("Chicken breast, grilled", "lunch", 180, "g"),
                ("Basmati rice, dry", "lunch", 80, "g"),
                ("Salmon fillet", "dinner", 150, "g"),
                ("Banana", "snack", 120, "g"),
                ("Almonds, raw", "snack", 30, "g")]
        for name, mealname, amt, unit in plan:
            repo.add_entry(conn, {
                "person_id": pid, "food_id": food_ids[name], "logged_on": today,
                "meal": mealname, "amount": float(amt), "unit": unit, "note": None})
        repo.add_entry(conn, {
            "person_id": pid, "food_id": act_ids["Run, 5 km"], "logged_on": today,
            "meal": "snack", "amount": 1, "unit": "piece", "note": None})
        return pid


SHOT_CSS = """
  /* A sticky header lands mid-page in a full-page capture and covers content.
     Pin it to the top of the document for the shot instead. */
  header { position: static !important; }
  /* toasts are transient; never let one land in a screenshot */
  .toast { display: none !important; }
  html { scroll-behavior: auto !important; }
"""


def main():
    seed()
    base = f"http://127.0.0.1:{PORT}/"
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1280, "height": 900},
                          device_scale_factor=1)
        page.goto(base, wait_until="networkidle")
        page.add_style_tag(content=SHOT_CSS)
        page.wait_for_function(
            "() => document.querySelectorAll('#person-select option').length === 2")
        page.select_option("#person-select", label="Test Person")
        page.wait_for_timeout(1200)
        shot(page, "log.png")

        page.click('[data-view="foods"]')
        page.wait_for_timeout(700)
        shot(page, "database-foods.png")
        page.click('.kind-btn[data-kind="activity"]')
        page.wait_for_timeout(500)
        shot(page, "database-activities.png")

        page.click('[data-view="people"]')
        page.wait_for_timeout(900)
        shot(page, "people.png", css=PEOPLE_TRIM)

        page.click('[data-view="predict"]')
        page.wait_for_function(
            "() => document.querySelector('.fc-badges') !== null", timeout=90000)
        page.wait_for_timeout(600)
        page.click("#fc-save")
        page.wait_for_function(
            "() => document.querySelectorAll('#forecast-table tbody tr').length >= 1",
            timeout=30000)
        page.check("#forecast-table input[type=checkbox]")
        page.wait_for_timeout(900)
        shot(page, "predict-weight.png")

        page.click('.ct[data-series="intake"]')
        page.wait_for_timeout(900)
        shot(page, "predict-intake.png")

        page.click('.ct[data-series="bmi"]')
        page.wait_for_timeout(900)
        shot(page, "predict-bmi.png")

        page.click('[data-view="targets"]')
        page.wait_for_timeout(700)
        shot(page, "targets.png")
        b.close()
    print(f"screenshots written to {OUT}")


def shot(page, name, full=True, css=""):
    # let any transient toast expire so it cannot appear in the capture
    page.wait_for_timeout(2900)
    page.add_style_tag(content=SHOT_CSS + css)
    page.screenshot(path=os.path.join(OUT, name), full_page=full)


# The People page is very tall. For a README grid a full-page capture would be
# absurd, so trim it to the parts worth looking at: the burn comparison and the
# weight curve, without the 30-row weigh-in table.
#
# Scoped to #view-people: injected styles persist for the whole session, and an
# unscoped ".panel:nth-of-type(1) { display:none }" would follow us onto the
# Predict tab and hide the panel holding the forecast controls.
PEOPLE_TRIM = """
  #view-people > .panel:nth-of-type(1) { display: none !important; }
  #view-people #weight-table,
  #view-people .weight-row { display: none !important; }
"""


try:
    main()
finally:
    server.should_exit = True
