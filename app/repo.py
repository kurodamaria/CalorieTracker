"""Queries over blueprints, records, people, weights and forecasts."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from . import body as B
from . import nutrition as N

MEALS = ("breakfast", "lunch", "dinner", "snack")

FOOD_COLUMNS = (
    "id", "name", "brand", "kind", "notes", "base_amount", "base_unit",
    "grams_per_ml", "grams_per_piece", "energy_kj", "protein_g", "carbs_g",
    "fat_g", "sugar_g", "fiber_g",
)


# ------------------------------------------------------------- blueprints

def search_foods(conn: sqlite3.Connection, q: str = "", kind: str | None = None,
                 limit: int = 200) -> list[dict]:
    like = f"%{q.strip()}%"
    sql = ["SELECT * FROM foods WHERE (name LIKE ? COLLATE NOCASE OR"
           " (brand IS NOT NULL AND brand LIKE ? COLLATE NOCASE))"]
    params: list = [like, like]
    if kind in ("food", "activity"):
        sql.append("AND kind = ?")
        params.append(kind)
    sql.append("ORDER BY name COLLATE NOCASE LIMIT ?")
    params.append(limit)
    return conn.execute(" ".join(sql), params).fetchall()


def get_food(conn: sqlite3.Connection, food_id: int) -> dict | None:
    return conn.execute("SELECT * FROM foods WHERE id = ?", (food_id,)).fetchone()


def create_food(conn: sqlite3.Connection, data: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO foods
            (name, brand, kind, base_amount, base_unit, energy_kj, protein_g,
             carbs_g, fat_g, sugar_g, fiber_g, grams_per_ml, grams_per_piece, notes)
        VALUES (:name, :brand, :kind, :base_amount, :base_unit, :energy_kj,
                :protein_g, :carbs_g, :fat_g, :sugar_g, :fiber_g, :grams_per_ml,
                :grams_per_piece, :notes)
        """,
        data,
    )
    return cur.lastrowid


def update_food(conn: sqlite3.Connection, food_id: int, data: dict) -> None:
    conn.execute(
        """
        UPDATE foods SET
            name = :name, brand = :brand, kind = :kind, base_amount = :base_amount,
            base_unit = :base_unit, energy_kj = :energy_kj, protein_g = :protein_g,
            carbs_g = :carbs_g, fat_g = :fat_g, sugar_g = :sugar_g,
            fiber_g = :fiber_g, grams_per_ml = :grams_per_ml,
            grams_per_piece = :grams_per_piece, notes = :notes
        WHERE id = :id
        """,
        {**data, "id": food_id},
    )


def delete_food(conn: sqlite3.Connection, food_id: int) -> None:
    conn.execute("DELETE FROM foods WHERE id = ?", (food_id,))


# ----------------------------------------------------------------- people

PERSON_COLUMNS = (
    "id", "name", "dob", "sex", "sex_offset_kcal", "height_cm",
    "activity_multiplier", "target_energy_kj", "target_protein_g",
    "target_carbs_g", "target_fat_g", "target_sugar_g", "target_fiber_g",
)


def list_persons(conn: sqlite3.Connection) -> list[dict]:
    return conn.execute("SELECT * FROM persons ORDER BY name COLLATE NOCASE").fetchall()


def get_person(conn: sqlite3.Connection, person_id: int) -> dict | None:
    return conn.execute("SELECT * FROM persons WHERE id = ?", (person_id,)).fetchone()


def create_person(conn: sqlite3.Connection, data: dict) -> int:
    cols = ", ".join(PERSON_COLUMNS[1:])
    marks = ", ".join(f":{c}" for c in PERSON_COLUMNS[1:])
    # Tolerate partial dicts so callers only supply what they care about.
    payload = {c: data.get(c) for c in PERSON_COLUMNS[1:]}
    return conn.execute(
        f"INSERT INTO persons ({cols}) VALUES ({marks})", payload).lastrowid


def update_person(conn: sqlite3.Connection, person_id: int, data: dict) -> None:
    sets = ", ".join(f"{c} = :{c}" for c in PERSON_COLUMNS[1:])
    payload = {c: data.get(c) for c in PERSON_COLUMNS[1:]}
    conn.execute(f"UPDATE persons SET {sets} WHERE id = :id",
                 {**payload, "id": person_id})


def delete_person(conn: sqlite3.Connection, person_id: int) -> None:
    conn.execute("DELETE FROM persons WHERE id = ?", (person_id,))


# ---------------------------------------------------------------- weights

def add_weight(conn: sqlite3.Connection, person_id: int, measured_on: str,
               weight_kg: float) -> None:
    """One weight per calendar day; weighing twice re-records the same day."""
    conn.execute(
        "INSERT INTO weights (person_id, measured_on, weight_kg) VALUES (?,?,?) "
        "ON CONFLICT(person_id, measured_on) DO UPDATE SET weight_kg = excluded.weight_kg",
        (person_id, measured_on, weight_kg),
    )


def list_weights(conn: sqlite3.Connection, person_id: int,
                 since: str | None = None) -> list[dict]:
    if since:
        return conn.execute(
            "SELECT * FROM weights WHERE person_id = ? AND measured_on >= ? "
            "ORDER BY measured_on", (person_id, since)).fetchall()
    return conn.execute(
        "SELECT * FROM weights WHERE person_id = ? ORDER BY measured_on",
        (person_id,)).fetchall()


def delete_weight(conn: sqlite3.Connection, weight_id: int) -> None:
    conn.execute("DELETE FROM weights WHERE id = ?", (weight_id,))


# ---------------------------------------------------------------- records

def add_entry(conn: sqlite3.Connection, data: dict) -> int:
    cur = conn.execute(
        "INSERT INTO entries (person_id, food_id, logged_on, meal, amount, unit, note)"
        " VALUES (:person_id, :food_id, :logged_on, :meal, :amount, :unit, :note)",
        data,
    )
    return cur.lastrowid


def delete_entry(conn: sqlite3.Connection, entry_id: int) -> None:
    conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))


def get_entry(conn: sqlite3.Connection, entry_id: int) -> dict | None:
    return conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()


def day_detail(conn: sqlite3.Connection, person_id: int, logged_on: str) -> list[dict]:
    """One person's records for a day, each with resolved grams and nutrients."""
    cols = ", ".join(f"f.{c}" for c in FOOD_COLUMNS)
    rows = conn.execute(
        f"""
        SELECT e.id AS entry_id, e.person_id, e.meal, e.amount, e.unit,
               e.note AS entry_note, {cols}
        FROM entries e JOIN foods f ON f.id = e.food_id
        WHERE e.person_id = ? AND e.logged_on = ?
        ORDER BY CASE f.kind WHEN 'activity' THEN 1 ELSE 0 END, e.id
        """,
        (person_id, logged_on),
    ).fetchall()
    out = []
    for row in rows:
        food = {k: row[k] for k in FOOD_COLUMNS}
        grams = N.grams_for(food, row["amount"], row["unit"])
        out.append({
            "entry": {
                "id": row["entry_id"], "person_id": row["person_id"],
                "meal": row["meal"], "amount": row["amount"], "unit": row["unit"],
                "note": row["entry_note"],
            },
            "name": row["name"], "brand": row["brand"], "kind": row["kind"],
            "grams": grams,
            "nutrients": N.entry_nutrients(food, row["amount"], row["unit"]),
        })
    return out


def day_summary(conn: sqlite3.Connection, person_id: int, logged_on: str,
                display_unit: str) -> dict:
    person = get_person(conn, person_id)
    details = day_detail(conn, person_id, logged_on)
    foods = [d for d in details if d["kind"] != "activity"]
    acts = [d for d in details if d["kind"] == "activity"]

    agg = N.aggregate(details)
    unit = "kcal" if display_unit == "kcal" else "kJ"

    def disp(kj):
        return N.from_kj(kj, display_unit)

    targets = {}
    for n in N.NUTRIENTS:
        value = person[f"target_{n}_kj"] if n == "energy" else person[f"target_{n}_g"]
        if value is None:
            targets[n] = None
        elif n == "energy" and display_unit == "kcal":
            targets[n] = value / N.KJ_PER_KCAL
        else:
            targets[n] = value

    by_meal = {}
    for meal in MEALS:
        sub = N.aggregate([d for d in details if d["entry"]["meal"] == meal])
        by_meal[meal] = {
            "energy": disp(sub["totals"]["energy"]),
            "food_energy": disp(sub["food_energy"]),
            "activity_energy": disp(sub["activity_energy"]),
            "coverage": sub["coverage"],
            "energy_basis": sub["energy_basis"],
            "totals_kj": sub["totals"],
        }

    return {
        "date": logged_on,
        "person_id": person_id,
        "unit": unit,
        "display_unit": display_unit,
        "totals_kj": agg["totals"],
        "totals": {n: (disp(agg["totals"][n]) if n == "energy" else agg["totals"][n])
                   for n in N.NUTRIENTS},
        "food_energy": disp(agg["food_energy"]),
        "activity_energy": disp(agg["activity_energy"]),
        "coverage": agg["coverage"],
        "partial": agg["partial"],
        "energy_basis": agg["energy_basis"],
        "total_grams": agg["total_grams"],
        "activity_count": agg["activity_count"],
        "targets": targets,
        "remaining": {n: (None if targets[n] is None or agg["totals"][n] is None
                          else targets[n] - (disp(agg["totals"][n]) if n == "energy"
                                             else agg["totals"][n]))
                      for n in N.NUTRIENTS},
        "meals": by_meal,
        "entries": details,
        "labels": N.LABELS,
    }


# --------------------------------------------------------------- modelling

def person_daily_series(conn: sqlite3.Connection, person_id: int, window_days: int,
                        today: date | None = None) -> list[dict]:
    """Completed days only, each with net intake and the interpolated weight.

    Today is excluded by construction: an incomplete day would drag the fit.
    """
    today = today or date.today()
    end = today - timedelta(days=1)
    start = end - timedelta(days=window_days - 1)
    return _series_between(conn, person_id, start, end)


def _series_between(conn: sqlite3.Connection, person_id: int, start: date,
                    end: date) -> list[dict]:
    """Per-day food/activity energy for a date range, via the same arithmetic as
    the day view so the model can never disagree with what the UI displays."""
    cols = ", ".join(f"f.{c}" for c in FOOD_COLUMNS)
    rows = conn.execute(
        f"""
        SELECT e.logged_on, e.amount, e.unit, {cols}
        FROM entries e JOIN foods f ON f.id = e.food_id
        WHERE e.person_id = ? AND e.logged_on BETWEEN ? AND ?
        ORDER BY e.logged_on, e.id
        """,
        (person_id, start.isoformat(), end.isoformat()),
    ).fetchall()

    food: dict[str, float] = {}
    act: dict[str, float] = {}
    for r in rows:
        kj = N.entry_nutrients({k: r[k] for k in FOOD_COLUMNS},
                               r["amount"], r["unit"])["energy"] or 0.0
        if r["kind"] == "activity" or kj < 0:
            # Positive magnitude of energy burned, matching nutrition.aggregate.
            act[r["logged_on"]] = act.get(r["logged_on"], 0.0) + abs(kj)
        else:
            food[r["logged_on"]] = food.get(r["logged_on"], 0.0) + kj

    weights = [(date.fromisoformat(w["measured_on"]), w["weight_kg"])
               for w in conn.execute(
                   "SELECT measured_on, weight_kg FROM weights "
                   "WHERE person_id = ? AND measured_on BETWEEN ? AND ? "
                   "ORDER BY measured_on",
                   (person_id, start.isoformat(), end.isoformat())).fetchall()]
    series = B.interpolate_weights(weights, start, end)

    out = []
    for i in range((end - start).days + 1):
        d = start + timedelta(days=i)
        key = d.isoformat()
        f, a = food.get(key, 0.0), act.get(key, 0.0)
        out.append({
            "date": d,
            "day": key,
            "food_kj": f,
            "activity_kj": a,
            "net_kj": f - a,
            "weight_kg": series.get(d),
            "logged": key in food or key in act,
        })
    return out


def person_metrics(conn: sqlite3.Connection, person_id: int,
                   display_unit: str = "kj") -> dict:
    """Current weight, BMI, both burn estimates, and the calibration fit."""
    person = get_person(conn, person_id)
    if not person:
        return {}
    today = date.today()
    weights = list_weights(conn, person_id)
    latest = weights[-1] if weights else None
    weight_now = latest["weight_kg"] if latest else None

    settings = {r["key"]: r["value"] for r in
                conn.execute("SELECT key, value FROM settings")}
    kj_per_kg = float(settings.get("kj_per_kg") or B.DEFAULT_KJ_PER_KG)
    lag = tuple(json.loads(settings.get("lag_weights")
                           or json.dumps(list(B.DEFAULT_LAG_WEIGHTS))))
    window = int(settings.get("calibration_days") or 30)

    series = person_daily_series(conn, person_id, window, today)
    logged_days = [d for d in series if d["logged"]]
    fit = B.fit_burn(series, kj_per_kg, lag)

    formula = (B.formula_burn_kj(person, weight_now, today) if weight_now else None)
    fit_warning = None
    if fit.get("ok") and formula:
        ratio = fit["burn_kj"] / formula
        # A fit this far from the formula means something is wrong with the inputs,
        # not that the person burns 1 MJ a day: a mis-typed weight, a food logged
        # in the wrong unit, or one enormous day dominating a short window.
        if not 0.5 <= ratio <= 2.0:
            fit_warning = (
                f"Fitted burn ({fit['burn_kj'] / N.KJ_PER_KCAL:.0f} kcal/day) is far "
                f"from the formula estimate ({formula / N.KJ_PER_KCAL:.0f}), so the "
                f"fit was rejected. Check for a mistyped weight or a record logged "
                f"in the wrong unit."
            )
            fit = {**fit, "ok": False, "reason": "implausible fit", "rejected": True}

    if fit.get("ok"):
        burn, source = fit["burn_kj"], "fitted"
    else:
        burn, source = formula, "formula"

    return {
        "person": person,
        "weight_now": weight_now,
        "weight_date": latest["measured_on"] if latest else None,
        "bmi_now": B.bmi(weight_now, person["height_cm"]) if weight_now else None,
        "bmi_band": (B.bmi_band(B.bmi(weight_now, person["height_cm"]))
                     if weight_now else None),
        "age": B.age_years(date.fromisoformat(person["dob"]), today),
        "bmr_kcal": (B.mifflin_bmr_kcal(weight_now, person["height_cm"],
                                        B.age_years(date.fromisoformat(person["dob"]),
                                                    today),
                                        person["sex_offset_kcal"])
                     if weight_now else None),
        "formula_burn_kj": formula,
        "formula_burn": N.from_kj(formula, display_unit),
        "fitted_burn_kj": fit.get("burn_kj"),
        "fitted_burn": N.from_kj(fit.get("burn_kj"), display_unit),
        "burn_kj": burn,
        "burn": N.from_kj(burn, display_unit),
        "burn_source": source,
        "burn_sd_kj": B.daily_burn_sd(fit, 0) if fit.get("ok") else 0.0,
        "fit": fit,
        "kj_per_kg": kj_per_kg,
        "lag_weights": list(lag),
        "window_days": window,
        "logged_days": len(logged_days),
        "mean_intake_kj": (sum(d["net_kj"] for d in logged_days) / len(logged_days)
                           if logged_days else None),
        "energy_unit": "kcal" if display_unit == "kcal" else "kJ",
        "calibrated": bool(fit.get("ok")),
        "not_calibrated_reason": None if fit.get("ok") else fit.get("reason"),
        "fit_warning": fit_warning,
    }


# --------------------------------------------------------------- forecasts

def build_forecast(conn: sqlite3.Connection, person_id: int, horizon_days: int = 365,
                   n_paths: int | None = None, today: date | None = None,
                   ) -> dict:
    """Run the projection from today's real data. Not stored - see save_forecast."""
    today = today or date.today()
    metrics = person_metrics(conn, person_id)
    if not metrics:
        return {}
    settings = {r["key"]: r["value"] for r in
                conn.execute("SELECT key, value FROM settings")}
    paths = n_paths or int(settings.get("n_paths") or 1500)

    window = metrics["window_days"]
    series = person_daily_series(conn, person_id, window, today)
    # Only days that actually have records can teach the sampler anything.
    usable = [d for d in series if d["logged"]]
    if len(usable) < 7:
        usable = series

    days_hist = [d["date"] for d in usable]
    food_hist = [d["food_kj"] for d in usable]
    act_hist = [d["activity_kj"] for d in usable]

    # Project from the last real weight, not from today: the person may not have
    # weighed in recently.
    weights = list_weights(conn, person_id)
    weight_now = weights[-1]["weight_kg"] if weights else None
    weight_date = weights[-1]["measured_on"] if weights else None

    burn = metrics["burn_kj"]
    if weight_now is None or burn is None:
        return {
            "person_id": person_id, "as_of": today.isoformat(), "ok": False,
            "reason": "add a weight to project",
            "metrics": metrics,
        }

    # Deterministic seed: the same as-of date and inputs must redraw the same
    # band, or the chart would jitter on every render.
    seed = (person_id * 1_000_003 + today.toordinal() * 7919
            + int(round(burn)) + int(len(usable))) % (2 ** 31)

    sim = B.simulate(days_hist, food_hist, act_hist, weight_now, burn,
                     metrics["kj_per_kg"], today, horizon_days,
                     n_paths=paths, burn_sd=metrics.get("burn_sd_kj") or 0.0,
                     seed=seed)
    summary = B.summarise(sim, metrics["person"]["height_cm"])

    return {
        "person_id": person_id,
        "as_of": today.isoformat(),
        "ok": True,
        "weight_now": weight_now,
        "weight_date": weight_date,
        "burn_kj": burn,
        "burn_source": metrics["burn_source"],
        "calibrated": metrics["calibrated"],
        "not_calibrated_reason": metrics["not_calibrated_reason"],
        "fit_warning": metrics.get("fit_warning"),
        "n_days_history": len(usable),
        "n_paths": paths,
        "seed": seed,
        "summary": summary,
        "metrics": metrics,
    }


def save_forecast(conn: sqlite3.Connection, person_id: int, forecast: dict) -> int:
    """Freeze a forecast so it can later be scored against reality.

    Snapshots are immutable on purpose: if the model improved and old forecasts
    were recomputed, you could never tell whether it actually got better.
    """
    params = {
        "burn_kj": forecast["burn_kj"],
        "burn_source": forecast["burn_source"],
        "calibrated": forecast["calibrated"],
        "weight_now": forecast["weight_now"],
        "weight_date": forecast["weight_date"],
        "n_days_history": forecast["n_days_history"],
        "seed": forecast["seed"],
        "model": "block-bootstrap",
    }
    conn.execute(
        "INSERT INTO forecasts (person_id, as_of, model_version, burn_kj,"
        " burn_source, kj_per_kg, lag_weights, params_json, summary_json, n_paths)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(person_id, as_of, model_version) DO UPDATE SET"
        " burn_kj = excluded.burn_kj, params_json = excluded.params_json,"
        " summary_json = excluded.summary_json",
        (person_id, forecast["as_of"], B.MODEL_VERSION, forecast["burn_kj"],
         forecast["burn_source"], forecast["metrics"]["kj_per_kg"],
         json.dumps(forecast["metrics"]["lag_weights"]),
         json.dumps(params), json.dumps(forecast["summary"]),
         forecast["n_paths"]),
    )
    row = conn.execute(
        "SELECT id FROM forecasts WHERE person_id = ? AND as_of = ? AND model_version = ?",
        (person_id, forecast["as_of"], B.MODEL_VERSION)).fetchone()
    return row["id"]


def list_forecasts(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM forecasts WHERE person_id = ? ORDER BY as_of, id",
        (person_id,)).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "as_of": r["as_of"],
            "model_version": r["model_version"], "burn_kj": r["burn_kj"],
            "burn_source": r["burn_source"], "created_at": r["created_at"],
            "params": json.loads(r["params_json"]),
            "summary": json.loads(r["summary_json"]),
        })
    return out


def delete_forecast(conn: sqlite3.Connection, forecast_id: int) -> None:
    conn.execute("DELETE FROM forecasts WHERE id = ?", (forecast_id,))


def forecast_scorecard(conn: sqlite3.Connection, person_id: int) -> dict:
    forecasts = list_forecasts(conn, person_id)
    actual = [(date.fromisoformat(w["measured_on"]), w["weight_kg"])
              for w in list_weights(conn, person_id)]
    scored = B.score_forecasts(forecasts, actual)
    # attach the stored median burn so the chart can label each line
    by_id = {f["id"]: f for f in forecasts}
    for row in scored["forecasts"]:
        f = by_id.get(row["id"], {})
        row["burn_kj"] = f.get("burn_kj")
        row["burn_source"] = f.get("burn_source")
        row["params"] = f.get("params")
    return {"scorecard": scored, "forecasts": forecasts,
            "all_ids": [f["id"] for f in forecasts]}


def history(conn: sqlite3.Connection, person_id: int, days: int = 14,
            display_unit: str = "kj") -> list[dict]:
    today = date.today()
    out = []
    for i in range(days):
        d = (today - timedelta(days=i)).isoformat()
        s = day_summary(conn, person_id, d, display_unit)
        out.append({
            "date": d,
            "energy": s["totals"]["energy"],
            "food_energy": s["food_energy"],
            "activity_energy": s["activity_energy"],
            "energy_basis": s["energy_basis"],
            "total_grams": s["total_grams"],
            "coverage": s["coverage"],
            "target_energy": s["targets"]["energy"],
            "unit": s["unit"],
            "logged": bool(s["entries"]),
        })
    return list(reversed(out))
