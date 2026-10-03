"""FastAPI application: JSON API + single-page UI."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from . import body as B
from . import nutrition as N
from . import repo
from .db import db, get_settings, init_db, set_setting

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")

MIGRATION_NOTES: list[str] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global MIGRATION_NOTES
    MIGRATION_NOTES = init_db()
    yield


app = FastAPI(title="CalorieTracker", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def _today() -> str:
    return date.today().isoformat()


# ------------------------------------------------------------- schemas

class FoodIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    brand: str | None = None
    kind: str = "food"
    base_amount: float = Field(gt=0, default=100)
    energy: float                       # may be negative for activities
    energy_unit: str = "kj"
    protein: float | None = Field(default=None, ge=0)
    carbs: float | None = Field(default=None, ge=0)
    fat: float | None = Field(default=None, ge=0)
    sodium: float | None = Field(default=None, ge=0)
    sugar: float | None = Field(default=None, ge=0)
    fiber: float | None = Field(default=None, ge=0)
    grams_per_ml: float = Field(default=1.0, gt=0)
    grams_per_piece: float | None = Field(default=None, gt=0)
    notes: str | None = None

    @field_validator("energy_unit")
    @classmethod
    def _energy_unit(cls, v: str) -> str:
        v = v.lower()
        if v not in ("kj", "kcal"):
            raise ValueError("energy unit must be kj or kcal")
        return v

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        v = v.lower()
        if v not in ("food", "activity"):
            raise ValueError("kind must be food or activity")
        return v


class EntryIn(BaseModel):
    person_id: int
    food_id: int
    date: str | None = None
    meal: str = "snack"
    amount: float = Field(gt=0)
    unit: str = "g"

    @field_validator("unit")
    @classmethod
    def _unit(cls, v: str) -> str:
        v = v.lower()
        if v not in ("g", "ml", "piece"):
            raise ValueError("unit must be g, ml or piece")
        return v

    @field_validator("meal")
    @classmethod
    def _meal(cls, v: str) -> str:
        v = v.lower()
        if v not in repo.MEALS:
            raise ValueError(f"meal must be one of {', '.join(repo.MEALS)}")
        return v


class PersonIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    dob: str
    sex: str = "other"
    sex_offset_kcal: float = 0.0
    height_cm: float = Field(gt=0, le=300)
    activity_multiplier: float = Field(default=1.2, gt=0, le=10)
    target_energy_kj: float | None = Field(default=None, ge=0)
    target_protein_g: float | None = Field(default=None, ge=0)
    target_carbs_g: float | None = Field(default=None, ge=0)
    target_fat_g: float | None = Field(default=None, ge=0)
    target_sugar_g: float | None = Field(default=None, ge=0)
    target_fiber_g: float | None = Field(default=None, ge=0)
    target_sodium_mg: float | None = Field(default=None, ge=0)

    @field_validator("sex")
    @classmethod
    def _sex(cls, v: str) -> str:
        v = v.lower()
        if v not in ("male", "female", "other"):
            raise ValueError("sex must be male, female or other")
        return v

    @field_validator("dob")
    @classmethod
    def _dob(cls, v: str) -> str:
        d = date.fromisoformat(v)
        if d >= date.today():
            raise ValueError("date of birth must be in the past")
        if d.year < 1900:
            raise ValueError("date of birth looks wrong")
        return v


class WeightIn(BaseModel):
    measured_on: str | None = None
    weight_kg: float = Field(gt=0, le=700)

    @field_validator("measured_on")
    @classmethod
    def _d(cls, v: str | None) -> str | None:
        if v is None:
            return None
        d = date.fromisoformat(v)
        if d > date.today():
            raise ValueError("cannot weigh in the future")
        return v


class SettingsIn(BaseModel):
    energy_display: str | None = None
    kj_per_kg: float | None = None
    lag_weights: list[float] | None = None
    calibration_days: int | None = None
    n_paths: int | None = None
    target_energy: str | None = None
    target_protein: str | None = None
    target_carbs: str | None = None
    target_fat: str | None = None
    target_sugar: str | None = None
    target_fiber: str | None = None

    @field_validator("energy_display")
    @classmethod
    def _display(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.lower()
        if v not in ("kj", "kcal"):
            raise ValueError("energy_display must be kj or kcal")
        return v

    @field_validator("lag_weights")
    @classmethod
    def _lag(cls, v: list[float] | None) -> list[float] | None:
        if v is None:
            return v
        if not v or any(w < 0 for w in v):
            raise ValueError("lag weights must be non-negative and non-empty")
        return v


def _food_payload(body: FoodIn) -> dict:
    energy_kj = N.to_kj(body.energy, body.energy_unit)
    is_activity = body.kind == "activity"
    if is_activity and energy_kj > 0:
        raise ValueError("an activity must have negative energy (it burns calories)")
    if not is_activity and energy_kj < 0:
        raise ValueError("food cannot have negative energy - use kind=activity")
    return {
        "name": body.name.strip(),
        "brand": (body.brand or None) and body.brand.strip(),
        "kind": body.kind,
        # An activity's energy is per session, not per 100 g.
        "base_amount": 1.0 if is_activity else body.base_amount,
        "base_unit": body.energy_unit,
        "energy_kj": energy_kj,
        # Activities carry no macros, so they can never subtract from macro tiles.
        "protein_g": None if is_activity else body.protein,
        "carbs_g": None if is_activity else body.carbs,
        "fat_g": None if is_activity else body.fat,
        "sodium_mg": None if is_activity else body.sodium,
        "sugar_g": None if is_activity else body.sugar,
        "fiber_g": None if is_activity else body.fiber,
        "grams_per_ml": 1.0 if is_activity else body.grams_per_ml,
        "grams_per_piece": None if is_activity else body.grams_per_piece,
        "notes": (body.notes or None) and body.notes.strip(),
    }


def _person_payload(body: PersonIn) -> dict:
    data = body.model_dump()
    data["sex_offset_kcal"] = (B.SEX_OFFSETS_KCAL.get(body.sex, 0.0)
                               if body.sex_offset_kcal == 0.0
                               else body.sex_offset_kcal)
    return data


def _require_person(conn, person_id: int) -> dict:
    person = repo.get_person(conn, person_id)
    if not person:
        raise HTTPException(404, "person not found")
    return person


# ---------------------------------------------------------------- pages

@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(
        request=request, name="index.html", context={"today": _today()})


# ---------------------------------------------------------------- config

@app.get("/api/config")
def api_config():
    with db() as conn:
        settings = get_settings(conn)
        # Full rows, not just id/name: the client keeps these as its source of
        # truth for the person form and the targets panel, so truncating them
        # here would blank those fields on the next save.
        persons = repo.list_persons(conn)
    return {
        "meals": list(repo.MEALS),
        "kinds": ["food", "activity"],
        "energy_display": settings["energy_display"],
        "labels": N.LABELS,
        "units": N.UNITS,
        "nutrients": list(N.NUTRIENTS),
        # Which nutrients get their own tile and their own completeness warning,
        # and where each one's target is stored. Published so the client does not
        # have to guess that sodium lives in target_sodium_mg.
        "tiles": list(N.TILES),
        "minor": list(N.MINOR),
        "columns": dict(N.COLUMN),
        "target_columns": {n: N.target_column(n) for n in N.NUTRIENTS},
        "kj_per_kcal": N.KJ_PER_KCAL,
        "settings": settings,
        "persons": [dict(p) for p in persons],
        "horizons": list(B.HORIZONS),
        "percentiles": [str(q) for q in B.PERCENTILES],
        "kj_per_kg_default": B.DEFAULT_KJ_PER_KG,
        "activity_default": B.DEFAULT_ACTIVITY,
        "sex_offsets_kcal": B.SEX_OFFSETS_KCAL,
        "migration_notes": MIGRATION_NOTES,
    }


# ------------------------------------------------------------ blueprints

@app.get("/api/foods")
def api_search_foods(q: str = "", kind: str | None = None, display: str | None = None):
    with db() as conn:
        unit = display or get_settings(conn)["energy_display"]
        return [N.normalize_food(r, unit)
                for r in repo.search_foods(conn, q, kind)]


@app.post("/api/foods", status_code=201)
def api_create_food(body: FoodIn):
    with db() as conn:
        food_id = repo.create_food(conn, _food_payload(body))
        return N.normalize_food(repo.get_food(conn, food_id),
                                 get_settings(conn)["energy_display"])


@app.get("/api/foods/{food_id}")
def api_get_food(food_id: int):
    with db() as conn:
        food = repo.get_food(conn, food_id)
        if not food:
            raise HTTPException(404, "food not found")
        out = N.normalize_food(food, get_settings(conn)["energy_display"])
        out["stored"] = food
        return out


@app.put("/api/foods/{food_id}")
def api_update_food(food_id: int, body: FoodIn):
    with db() as conn:
        if not repo.get_food(conn, food_id):
            raise HTTPException(404, "food not found")
        repo.update_food(conn, food_id, _food_payload(body))
        return N.normalize_food(repo.get_food(conn, food_id),
                                 get_settings(conn)["energy_display"])


@app.delete("/api/foods/{food_id}")
def api_delete_food(food_id: int):
    with db() as conn:
        if not repo.get_food(conn, food_id):
            raise HTTPException(404, "food not found")
        repo.delete_food(conn, food_id)
    return {"deleted": food_id}


@app.post("/api/foods/{food_id}/derive-energy")
def api_derive_energy(food_id: int):
    """Estimate energy from the blueprint's own macros (Atwater factors)."""
    with db() as conn:
        food = repo.get_food(conn, food_id)
        if not food:
            raise HTTPException(404, "food not found")
        derived = N.derive_energy_kj(food["protein_g"], food["carbs_g"], food["fat_g"])
        if derived is None:
            raise HTTPException(400, "food has no macros to derive energy from")
        conn.execute("UPDATE foods SET energy_kj = ? WHERE id = ?", (derived, food_id))
        return N.normalize_food(repo.get_food(conn, food_id),
                                 get_settings(conn)["energy_display"])


# ---------------------------------------------------------------- people

@app.get("/api/persons")
def api_list_persons():
    with db() as conn:
        unit = get_settings(conn)["energy_display"]
        return [{**p, "bmi": None} for p in repo.list_persons(conn)]


@app.post("/api/persons", status_code=201)
def api_create_person(body: PersonIn):
    with db() as conn:
        settings = get_settings(conn)
        data = _person_payload(body)
        # Seed targets from the global defaults so a new person starts sane.
        for n in N.NUTRIENTS:
            key = f"target_{n}"
            field = N.target_column(n)
            if data.get(field) is None and settings.get(key):
                try:
                    data[field] = float(settings[key])
                except ValueError:
                    pass
        person_id = repo.create_person(conn, data)
        return repo.get_person(conn, person_id)


@app.put("/api/persons/{person_id}")
def api_update_person(person_id: int, body: PersonIn):
    with db() as conn:
        _require_person(conn, person_id)
        repo.update_person(conn, person_id, _person_payload(body))
        return repo.get_person(conn, person_id)


@app.delete("/api/persons/{person_id}")
def api_delete_person(person_id: int):
    with db() as conn:
        _require_person(conn, person_id)
        repo.delete_person(conn, person_id)
    return {"deleted": person_id}


@app.get("/api/persons/{person_id}/metrics")
def api_person_metrics(person_id: int):
    with db() as conn:
        _require_person(conn, person_id)
        return repo.person_metrics(conn, person_id,
                                  get_settings(conn)["energy_display"])


# --------------------------------------------------------------- weights

@app.get("/api/persons/{person_id}/weights")
def api_weights(person_id: int, days: int = 400):
    with db() as conn:
        _require_person(conn, person_id)
        rows = repo.list_weights(conn, person_id)
        cutoff = (date.today()).toordinal() - days
        rows = [w for w in rows if date.fromisoformat(w["measured_on"]).toordinal()
                >= cutoff]
        return [{**w, "bmi": B.bmi(w["weight_kg"], repo.get_person(conn, person_id)
                                   ["height_cm"])} for w in rows]


@app.post("/api/persons/{person_id}/weights", status_code=201)
def api_add_weight(person_id: int, body: WeightIn):
    with db() as conn:
        _require_person(conn, person_id)
        repo.add_weight(conn, person_id, body.measured_on or _today(), body.weight_kg)
        return {"ok": True, "measured_on": body.measured_on or _today(),
                "weight_kg": body.weight_kg}


@app.delete("/api/weights/{weight_id}")
def api_delete_weight(weight_id: int):
    with db() as conn:
        repo.delete_weight(conn, weight_id)
    return {"deleted": weight_id}


# --------------------------------------------------------------- records

@app.post("/api/entries", status_code=201)
def api_add_entry(body: EntryIn):
    logged_on = body.date or _today()
    date.fromisoformat(logged_on)
    with db() as conn:
        _require_person(conn, body.person_id)
        food = repo.get_food(conn, body.food_id)
        if not food:
            raise HTTPException(404, "food not found")
        try:
            N.grams_for(food, body.amount, body.unit)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        entry_id = repo.add_entry(conn, {
            "person_id": body.person_id, "food_id": body.food_id,
            "logged_on": logged_on, "meal": body.meal, "amount": body.amount,
            "unit": body.unit, "note": None,
        })
        return {"id": entry_id, "date": logged_on}


@app.delete("/api/entries/{entry_id}")
def api_delete_entry(entry_id: int):
    with db() as conn:
        if not repo.get_entry(conn, entry_id):
            raise HTTPException(404, "entry not found")
        repo.delete_entry(conn, entry_id)
    return {"deleted": entry_id}


@app.get("/api/day/{logged_on}")
def api_day(logged_on: str, person_id: int | None = None):
    date.fromisoformat(logged_on)
    with db() as conn:
        pid = person_id or _default_person(conn)
        _require_person(conn, pid)
        return repo.day_summary(conn, pid, logged_on,
                                get_settings(conn)["energy_display"])


def _default_person(conn) -> int:
    people = repo.list_persons(conn)
    if not people:
        raise HTTPException(400, "create a person first")
    return people[0]["id"]


@app.get("/api/history")
def api_history(person_id: int, days: int = 14):
    with db() as conn:
        _require_person(conn, person_id)
        return repo.history(conn, person_id, days,
                            get_settings(conn)["energy_display"])


# ------------------------------------------------------------- forecasts

@app.get("/api/persons/{person_id}/forecast")
def api_forecast(person_id: int, save: bool = False):
    with db() as conn:
        _require_person(conn, person_id)
        fc = repo.build_forecast(conn, person_id)
        if save and fc.get("ok"):
            fc["id"] = repo.save_forecast(conn, person_id, fc)
        return fc


@app.get("/api/persons/{person_id}/forecasts")
def api_forecasts(person_id: int):
    with db() as conn:
        _require_person(conn, person_id)
        return repo.forecast_scorecard(conn, person_id)


@app.delete("/api/forecasts/{forecast_id}")
def api_delete_forecast(forecast_id: int):
    with db() as conn:
        repo.delete_forecast(conn, forecast_id)
    return {"deleted": forecast_id}


# -------------------------------------------------------------- settings

@app.put("/api/settings")
def api_update_settings(body: SettingsIn):
    with db() as conn:
        data = body.model_dump(exclude_none=True)
        for key, value in data.items():
            if key == "lag_weights":
                total = sum(value)
                if total <= 0:
                    raise HTTPException(400, "lag weights must sum above zero")
                set_setting(conn, key, json.dumps([round(w / total, 4) for w in value]))
            elif key == "energy_display":
                set_setting(conn, key, value)
            elif key in ("kj_per_kg", "calibration_days", "n_paths"):
                if value <= 0:
                    raise HTTPException(400, f"{key} must be positive")
                set_setting(conn, key, str(value))
            else:
                if value != "":
                    try:
                        num = float(value)
                    except ValueError as exc:
                        raise HTTPException(400, f"{key} must be a number or empty") from exc
                    if num < 0:
                        raise HTTPException(400, f"{key} must not be negative")
                set_setting(conn, key, str(value))
        return get_settings(conn)


@app.exception_handler(ValueError)
def _value_error(request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)
