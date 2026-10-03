"""Unit conversion, normalization, and coverage-aware aggregation."""
from __future__ import annotations

KJ_PER_KCAL = 4.184

# Atwater factors (gross energy).
KJ_PER_G = {"protein": 17.0, "carbs": 17.0, "fat": 37.0}

# Storage column for each nutrient. Sodium is in milligrams because that is how
# every nutrition label states it; everything else is grams and energy is kJ.
COLUMN = {
    "energy": "energy_kj",
    "carbs": "carbs_g",
    "protein": "protein_g",
    "fat": "fat_g",
    "sodium": "sodium_mg",
    "sugar": "sugar_g",
    "fiber": "fiber_g",
}
UNITS = {
    "energy": "kJ", "carbs": "g", "protein": "g", "fat": "g",
    "sodium": "mg", "sugar": "g", "fiber": "g",
}
NUTRIENTS = tuple(COLUMN)
LABELS = {
    "energy": "Energy",
    "carbs": "Carbs",
    "protein": "Protein",
    "fat": "Fat",
    "sodium": "Sodium",
    "sugar": "Sugar",
    "fiber": "Fiber",
}

# Given their own tile on the day summary, with a completeness warning of their
# own. These are the macros you steer your eating by.
TILES = ("carbs", "protein", "fat", "sodium")

# Stored, editable and targetable, but rolled into one shared completeness line.
# Labels are patchy for both: sugars only on some panels, and fibre tends to be
# absent unless it is the thing being sold.
MINOR = ("sugar", "fiber")

TARGET_SETTING = {n: f"target_{n}" for n in NUTRIENTS}
UNITS_PER_100G = {"g": 1.0, "ml": 1.0, "piece": 1.0}


def value_of(source: dict, nutrient: str) -> float | None:
    """Read a nutrient out of a foods or food-joined row by its storage column."""
    return source[COLUMN[nutrient]]


def target_column(nutrient: str) -> str:
    """The column a person's target for this nutrient lives in."""
    return f"target_{COLUMN[nutrient]}"


def to_kj(value: float, unit: str) -> float:
    """Convert an entered energy value to kJ."""
    if unit == "kj":
        return value
    if unit == "kcal":
        return value * KJ_PER_KCAL
    raise ValueError(f"unknown energy unit: {unit}")


def from_kj(value_kj: float | None, display_unit: str) -> float | None:
    if value_kj is None:
        return None
    if display_unit == "kcal":
        return value_kj / KJ_PER_KCAL
    return value_kj


def derive_energy_kj(protein: float | None, carbs: float | None, fat: float | None) -> float | None:
    """Estimate gross energy from macronutrients. Returns None if all are missing."""
    parts = (protein, carbs, fat)
    if all(v is None for v in parts):
        return None
    total = 0.0
    for name, value in zip(("protein", "carbs", "fat"), parts):
        total += (value or 0.0) * KJ_PER_G[name]
    return total


def normalize_food(food: dict, display_unit: str = "kj") -> dict:
    """Rescale a stored blueprint (values are per `base_amount`) to per-100 g.

    Activities are stored per *session* rather than per 100 g, so they are
    passed through unscaled. Also converts energy into the caller's display unit
    and marks which nutrients are known vs missing.
    """
    kind = food.get("kind", "food")
    is_activity = kind == "activity"
    factor = 1.0 if is_activity else 100.0 / food["base_amount"]
    out = {
        "id": food["id"],
        "name": food["name"],
        "brand": food["brand"],
        "notes": food["notes"],
        "kind": kind,
        "base_amount": food["base_amount"],
        "base_unit": food["base_unit"],
        "grams_per_ml": food["grams_per_ml"],
        "grams_per_piece": food["grams_per_piece"],
        "per100": {},
        "known": {},
    }
    for n in NUTRIENTS:
        raw = value_of(food, n)
        if raw is None:
            out["per100"][n] = None
            out["known"][n] = False
        else:
            out["per100"][n] = raw * factor
            out["known"][n] = True

    energy_kj = out["per100"]["energy"]
    out["energy_kj"] = energy_kj
    out["energy"] = from_kj(energy_kj, display_unit)

    if is_activity or not energy_kj:
        out["protein_pct"] = out["fat_pct"] = None
    else:
        out["protein_pct"] = (
            round(100 * out["per100"]["protein"] * KJ_PER_G["protein"] / energy_kj)
            if out["per100"]["protein"] is not None else None
        )
        out["fat_pct"] = (
            round(100 * out["per100"]["fat"] * KJ_PER_G["fat"] / energy_kj)
            if out["per100"]["fat"] is not None else None
        )
    return out


def grams_for(food: dict, amount: float, unit: str) -> float:
    """Convert a logged amount into grams.

    For activities this returns the session count instead: an activity has no
    mass, and its energy is already per session.
    """
    if food.get("kind") == "activity":
        return amount
    if unit == "g":
        return amount
    if unit == "ml":
        return amount * (food["grams_per_ml"] or 1.0)
    if unit == "piece":
        if not food["grams_per_piece"]:
            raise ValueError(
                f"'{food['name']}' has no piece weight set; log it in grams or set "
                f"grams-per-piece on the food."
            )
        return amount * food["grams_per_piece"]
    raise ValueError(f"unknown unit: {unit}")


def entry_nutrients(food: dict, amount: float, unit: str) -> dict[str, float]:
    """Nutrients contributed by one logged record, keyed by NUTRIENTS."""
    factor = (grams_for(food, amount, unit) / food["base_amount"]) if food["base_amount"] \
        else 0.0
    result = {}
    for n in NUTRIENTS:
        raw = value_of(food, n)
        result[n] = None if raw is None else raw * factor
    return result


def aggregate(entries: list[dict]) -> dict:
    """Sum a person's records for a day, tracking how much of the data was filled in.

    Activities carry negative energy and are kept entirely out of the macro
    totals: a 10 km run must never subtract 40 g of protein. They are reported
    separately and folded in only to produce the net energy figure.

    For the macros, coverage is the share of logged food mass that actually had
    a value, so the UI can flag an under-reported total instead of presenting a
    zero-filled one as fact.
    """
    totals = {n: None for n in NUTRIENTS}
    coverage = {n: 0.0 for n in NUTRIENTS}
    food_energy = 0.0
    activity_energy = 0.0
    food_mass = 0.0
    activity_count = 0.0

    for item in entries:
        contrib = item["nutrients"]
        grams = item.get("grams") or 0.0
        if item.get("kind") == "activity":
            # Held as a positive magnitude of energy burned, so the caller can
            # subtract it once and never twice.
            activity_energy += abs(contrib["energy"] or 0.0)
            activity_count += grams
            continue
        food_mass += grams
        if contrib["energy"] is not None:
            food_energy += contrib["energy"]
        for n in NUTRIENTS:
            if contrib[n] is not None:
                coverage[n] += grams
                totals[n] = (totals[n] or 0.0) + contrib[n]

    empty = food_mass == 0 and activity_count == 0
    if empty:
        coverage = {n: 1.0 for n in NUTRIENTS}
        totals = {n: 0.0 for n in NUTRIENTS}
    else:
        coverage = {n: (coverage[n] / food_mass if food_mass else 1.0)
                    for n in NUTRIENTS}

    net = food_energy - activity_energy
    if food_energy == 0 and activity_count > 0:
        # No food with an energy value at all, but activity logged.
        derived = derive_energy_kj(totals["protein"], totals["carbs"], totals["fat"])
        energy_basis = "derived-from-macros" if derived is not None else "unavailable"
        totals["energy"] = derived
    elif food_energy == 0 and activity_count == 0 and not empty:
        totals["energy"] = 0.0
        energy_basis = "measured"
    else:
        totals["energy"] = net
        energy_basis = "measured"

    return {
        "totals": totals,
        "coverage": coverage,
        "food_energy": food_energy,
        "activity_energy": activity_energy,
        "net_energy": totals["energy"],
        "total_grams": food_mass,
        "activity_count": activity_count,
        "energy_basis": energy_basis,
        "partial": {n: (n != "energy" and not empty and food_mass > 0
                        and coverage[n] < 1.0) for n in NUTRIENTS},
    }


def kcal_display(value_kj: float | None, display_unit: str) -> float | None:
    if value_kj is None:
        return None
    return from_kj(value_kj, display_unit)
