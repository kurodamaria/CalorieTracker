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

OUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8781
init_db()
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
threading.Thread(target=server.run, daemon=True).start()
while not server.started:
    pass

TODAY = date.today()
KJ_PER_G = 50.0

FOODS = [
    # name, kJ/100g, protein, carbs, fat, sugar, fiber, g/piece, brand
    ("煮鸡蛋", 649, 12.6, 0.6, 10.6, 0.6, 0, 50, ""),
    ("Greek yoghurt", 406, 9.0, 3.6, 5.0, 3.6, 0, 170, "Fage"),
    ("Oatflakes", 1585, 13.2, 67.7, 6.5, 1.0, 10.1, None, "Quaker"),
    ("Whole milk", 268, 3.3, 4.8, 3.6, 4.8, 0, None, ""),
    ("Chicken breast, grilled", 690, 31.0, 0.0, 3.6, 0.0, 0, None, ""),
    ("Basmati rice, dry", 1410, 8.5, 28.6, 0.6, None, None, None, ""),
    ("Almonds, raw", 2514, 21.2, 21.6, 49.9, 4.4, 12.5, 1.2, ""),
    ("Salmon fillet", 2080, 20.0, 0.0, 13.0, 0.0, 0, None, ""),
    ("Banana", 370, 1.1, 22.8, 0.3, 12.2, 2.6, None, ""),
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
            "target_energy_kj": 9500, "target_protein_g": 150,
            "target_carbs_g": 220, "target_fat_g": 70,
            "target_sugar_g": 50, "target_fiber_g": 30,
        })
        repo.create_person(conn, {
            "name": "Guest", "dob": "1995-05-05", "sex": "female",
            "sex_offset_kcal": -5.0, "height_cm": 165.0, "activity_multiplier": 1.2,
        })

        food_ids = {}
        for name, kj, p, cb, f, s, fib, gpp, brand in FOODS:
            food_ids[name] = repo.create_food(conn, {
                "name": name, "brand": brand or None, "kind": "food",
                "base_amount": 100.0, "base_unit": "kj", "energy_kj": float(kj),
                "protein_g": p, "carbs_g": cb, "fat_g": f, "sugar_g": s,
                "fiber_g": fib, "grams_per_ml": 1.0, "grams_per_piece": gpp,
                "notes": None})
        act_ids = {n: repo.create_food(conn, {
            "name": n, "brand": None, "kind": "activity", "base_amount": 1.0,
            "base_unit": "kj", "energy_kj": float(k), "protein_g": None,
            "carbs_g": None, "fat_g": None, "sugar_g": None, "fiber_g": None,
            "grams_per_ml": 1.0, "grams_per_piece": None, "notes": None})
            for n, k in ACTIVITIES}

        meal = repo.create_food(conn, {
            "name": "Mixed meal", "brand": None, "kind": "food",
            "base_amount": 100.0, "base_unit": "kj", "energy_kj": MEAL_KJ,
            "protein_g": 45.0, "carbs_g": 60.0, "fat_g": 22.0, "sugar_g": 8.0,
            "fiber_g": 6.0, "grams_per_ml": 1.0, "grams_per_piece": None,
            "notes": None})

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


def main():
    pid = seed()
    base = f"http://127.0.0.1:{PORT}/"
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1360, "height": 1000})
        page.goto(base, wait_until="networkidle")
        page.wait_for_function(
            "() => document.querySelectorAll('#person-select option').length === 2")
        page.select_option("#person-select", label="Test Person")
        page.wait_for_timeout(1200)

        page.screenshot(path=os.path.join(OUT, "shot-log.png"), full_page=True)

        page.click('[data-view="people"]')
        page.wait_for_timeout(900)
        page.screenshot(path=os.path.join(OUT, "shot-people.png"), full_page=True)

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
        page.screenshot(path=os.path.join(OUT, "shot-predict.png"), full_page=True)

        page.click('.ct[data-series="intake"]')
        page.wait_for_timeout(900)
        page.screenshot(path=os.path.join(OUT, "shot-intake.png"), full_page=True)

        page.click('[data-view="foods"]')
        page.wait_for_timeout(700)
        page.screenshot(path=os.path.join(OUT, "shot-foods.png"), full_page=True)
        page.click('.kind-btn[data-kind="activity"]')
        page.wait_for_timeout(500)
        page.screenshot(path=os.path.join(OUT, "shot-activities.png"), full_page=True)
        b.close()
    print("screenshots written")


try:
    main()
finally:
    server.should_exit = True
