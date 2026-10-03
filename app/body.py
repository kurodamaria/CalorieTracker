"""Body-composition modelling: BMR, burn fitting from real data, and projection.

The energy balance for one day is

    dW/dt = (food - logged_activity - burn) / kJ_PER_KG

`burn` is everything the body spends that is *not* deliberate logged activity:
resting metabolism, everyday movement, and the thermic effect of food. It is
estimated two ways and reconciled:

  * `formula_burn` - Mifflin-St Jeor BMR x the person's unlogged-movement
    multiplier. Available immediately, but it is only a guess.
  * `fitted_burn`  - solved from the person's own weight and intake history.
    Needs real data, but it is a measurement rather than an assumption.

Uncertainty is handled by simulating the *inputs*, never the weight. Weight is the
integral of the balance, so simulating it directly compounds error into a random
walk whose band is useless within a year. Instead we draw realistic future
intake/activity paths and push each one through the deterministic equation above.
"""
from __future__ import annotations

import math
import random
from datetime import date, timedelta

KJ_PER_KCAL = 4.184

# 7700 kcal per kg of body-mass change.
DEFAULT_KJ_PER_KG = 32213.0
DEFAULT_ACTIVITY = 1.2

# Mifflin-St Jeor sex constants, in kcal/day. Stored per person so they can be
# overridden without editing the formula.
SEX_OFFSETS_KCAL = {"male": 5.0, "female": -5.0}

# Weight on day t responds to intake over the preceding few days (fluid, glycogen,
# gut contents). These are the weights on lag 0, 1, 2, 3.
DEFAULT_LAG_WEIGHTS = (0.20, 0.30, 0.30, 0.20)

HORIZONS = (7, 14, 30, 90, 180, 365)
PERCENTILES = (5, 25, 50, 75, 95)
MODEL_VERSION = 1


# --------------------------------------------------------------------- BMR

def _safe_anniversary(year: int, dob: date) -> date:
    """Birthday in `year`, clamping 29 Feb to 28 Feb in common years."""
    try:
        return date(year, dob.month, dob.day)
    except ValueError:
        return date(year, 2, 28)


def age_years(dob: date, on: date | None = None) -> float:
    on = on or date.today()
    years = on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))
    # Fractional part, so BMR varies smoothly instead of stepping on birthdays.
    had_birthday = (on.month, on.day) >= (dob.month, dob.day)
    anniversary = _safe_anniversary(on.year if had_birthday else on.year - 1, dob)
    next_anniversary = _safe_anniversary(anniversary.year + 1, dob)
    span = (next_anniversary - anniversary).days
    return years + ((on - anniversary).days / span if span else 0.0)


def mifflin_bmr_kcal(weight_kg: float, height_cm: float, age: float,
                     sex_offset_kcal: float = 0.0) -> float:
    """Resting energy expenditure. The published formula is in kcal/day."""
    return max(0.0, 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age + sex_offset_kcal)


def formula_burn_kj(person: dict, weight_kg: float, on: date | None = None,
                    activity: float | None = None) -> float:
    """Formula estimate of non-activity burn, in kJ/day."""
    bmr = mifflin_bmr_kcal(
        weight_kg,
        person["height_cm"],
        age_years(date.fromisoformat(person["dob"]), on),
        person["sex_offset_kcal"],
    )
    mult = person["activity_multiplier"] if activity is None else activity
    return bmr * mult * KJ_PER_KCAL


def bmi(weight_kg: float, height_cm: float) -> float | None:
    if not height_cm:
        return None
    m = height_cm / 100.0
    return weight_kg / (m * m)


def bmi_band(value: float) -> str:
    if value < 18.5:
        return "underweight"
    if value < 25:
        return "normal"
    if value < 30:
        return "overweight"
    return "obese"


# ------------------------------------------------------------ weight series

def interpolate_weights(measurements: list[tuple[date, float]],
                        start: date, end: date) -> dict[date, float]:
    """Spread weigh-ins across every day between them.

    Linear between measurements. Deliberately no extrapolation before the first
    or after the last weigh-in, so the fit never claims to know weight the person
    has not actually measured.
    """
    if not measurements:
        return {}
    pts = sorted(measurements, key=lambda m: m[0])
    out: dict[date, float] = {}
    for i in range((end - start).days + 1):
        d = start + timedelta(days=i)
        if d < pts[0][0] or d > pts[-1][0]:
            continue
        if d == pts[-1][0]:
            out[d] = pts[-1][1]
            continue
        for j in range(len(pts) - 1):
            lo, hi = pts[j], pts[j + 1]
            if lo[0] <= d < hi[0]:
                span = (hi[0] - lo[0]).days
                frac = (d - lo[0]).days / span
                out[d] = lo[1] + (hi[1] - lo[1]) * frac
                break
    return out


def smooth(values: list[float], window: int) -> list[float]:
    """Centred moving average with shrinking edges."""
    if window <= 1 or len(values) < window:
        return list(values)
    half = window // 2
    out = []
    for i in range(len(values)):
        lo = max(0, i - half)
        hi = min(len(values), i + half + 1)
        chunk = values[lo:hi]
        out.append(sum(chunk) / len(chunk))
    return out


def effective_intake(intake: list[float], lag_weights: tuple[float, ...]) -> list[float]:
    """Apply the lag kernel, renormalising over available history at the edges."""
    k = len(lag_weights)
    out = []
    for i in range(len(intake)):
        num = 0.0
        den = 0.0
        for j, w in enumerate(lag_weights):
            idx = i - j
            if 0 <= idx < len(intake):
                num += w * intake[idx]
                den += w
        out.append(num / den if den else 0.0)
    return out


# ----------------------------------------------------------------- the fit

def fit_burn(daily: list[dict], kj_per_kg: float = DEFAULT_KJ_PER_KG,
             lag_weights: tuple[float, ...] = DEFAULT_LAG_WEIGHTS) -> dict:
    """Solve for burn from observed daily weight change and lagged net intake.

    `daily` is a list of {date, net_kj, weight_kg} for completed days, ascending,
    carrying at least two weigh-ins. The model has one unknown and a fixed slope,
    so the least-squares solution is closed-form:

        burn = mean(effective_intake - dW * kJ_per_kg)
    """
    usable = [d for d in daily if d.get("weight_kg") is not None]
    result = {
        "burn_kj": None, "burn_kcal": None, "ok": False, "n_days": 0,
        "residual_sd_kj": None, "r2": None, "reason": None,
        "weight_start": None, "weight_end": None,
    }
    if len(usable) < 2:
        result["reason"] = "need at least 2 weigh-ins on completed days"
        return result
    if len(usable) < 7:
        result["reason"] = "need at least 7 days with weight data"
        return result

    intake = [d["net_kj"] for d in usable]
    weights = [d["weight_kg"] for d in usable]
    # Light smoothing: we need mean(dW), which telescopes, but smoothing keeps a
    # pathological single-day scale glitch from dominating one residual.
    w_smooth = smooth(weights, 3)
    eff = effective_intake(intake, lag_weights)

    dw = [w_smooth[i] - w_smooth[i - 1] for i in range(1, len(w_smooth))]
    eff_used = eff[1:]
    # dW[0] pairs with eff[1]: weight changed *over* the day, so it reflects the
    # intake already digested by the next morning.
    obs = [dw[i] * kj_per_kg for i in range(len(dw))]
    cand = [eff_used[i] - obs[i] for i in range(len(obs))]
    burn = sum(cand) / len(cand)

    resid = [obs[i] - (eff_used[i] - burn) for i in range(len(obs))]
    mean_r = sum(resid) / len(resid)
    var = sum((r - mean_r) ** 2 for r in resid) / len(resid)
    sd = math.sqrt(var)
    obs_mean = sum(obs) / len(obs)
    tot = sum((o - obs_mean) ** 2 for o in obs)
    r2 = None if tot <= 1e-12 else max(0.0, 1.0 - var / tot)

    result.update({
        "burn_kj": burn,
        "burn_kcal": burn / KJ_PER_KCAL,
        "ok": True,
        "n_days": len(usable),
        "residual_sd_kj": sd,
        "r2": r2,
        "weight_start": w_smooth[0],
        "weight_end": w_smooth[-1],
    })
    return result


def daily_burn_sd(fit: dict, n_paths: int) -> float:
    """Rough 1-sigma uncertainty on the burn itself, from the residual scatter."""
    if not fit.get("ok") or not fit.get("residual_sd_kj"):
        return 0.0
    return fit["residual_sd_kj"] / math.sqrt(max(1, fit["n_days"]))


# ------------------------------------------------------------- simulation

def _build_block_pools(days: list[date], rows: list[tuple[float, float, float]],
                       block_lengths: tuple[int, ...]) -> dict[tuple[int, int], list[int]]:
    """Group history into blocks, keyed by (length, starting weekday).

    Sampling a whole block at the target's weekday keeps both the weekly rhythm
    and the within-block serial correlation intact, which single-day resampling
    destroys.
    """
    pools: dict[tuple[int, int], list[int]] = {}
    n = len(rows)
    for L in block_lengths:
        if n < L:
            continue
        for i in range(n - L + 1):
            wd = days[i].weekday()
            pools.setdefault((L, wd), []).append(i)
    return pools


def simulate(days_hist: list[date], food_hist: list[float], act_hist: list[float],
             weight_now: float, burn_kj: float, kj_per_kg: float,
             start: date, horizon_days: int, n_paths: int = 1500,
             block_lengths: tuple[int, ...] = (3, 4, 5, 6, 7),
             burn_sd: float = 0.0, seed: int = 0) -> dict:
    """Monte-Carlo projection over realistic future intake, via block bootstrap.

    Draws burn from N(fitted, burn_sd) on each path so parameter uncertainty
    propagates into the band as well.
    """
    rng = random.Random(seed)
    net_hist = [f - a for f, a in zip(food_hist, act_hist)]
    pools = _build_block_pools(days_hist, list(zip(food_hist, act_hist)), block_lengths)

    usable_days = [d for d in days_hist if d >= start]
    fallback = list(range(len(days_hist)))

    paths_weight: list[list[float]] = []
    paths_food: list[list[float]] = []
    paths_act: list[list[float]] = []

    for _ in range(n_paths):
        w = weight_now
        burn_i = burn_kj + (rng.gauss(0.0, burn_sd) if burn_sd > 0 else 0.0)
        wseries, fseries, aseries = [w], [], []
        for i in range(1, horizon_days + 1):
            d = start + timedelta(days=i - 1)
            wd = d.weekday()
            if d in usable_days:
                idx = days_hist.index(d)
                f, a = food_hist[idx], act_hist[idx]
            else:
                L = rng.choice(block_lengths)
                pool = pools.get((L, wd)) or [
                    i for (l, w_), v in pools.items() if w_ == wd for i in v
                ] or fallback
                if pool:
                    src = rng.choice(pool)
                    if src + i - 1 < len(days_hist):
                        f, a = food_hist[src + i - 1], act_hist[src + i - 1]
                    else:
                        f, a = food_hist[src], act_hist[src]
                else:
                    f = rng.choice(food_hist)
                    a = rng.choice(act_hist)
            fseries.append(f)
            aseries.append(a)
            w += (f - a - burn_i) / kj_per_kg
            wseries.append(w)
        paths_weight.append(wseries)
        paths_food.append(fseries)
        paths_act.append(aseries)

    return {
        "weights": paths_weight,
        "food": paths_food,
        "activity": paths_act,
        "horizons": HORIZONS,
        "n_paths": n_paths,
    }


def percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * (q / 100.0)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return s[int(pos)]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def summarise(sim: dict, height_cm: float) -> dict:
    """Collapse the simulated paths into the bands the UI draws.

    Percentile keys are strings throughout, so the dict survives a JSON round
    trip unchanged (JSON object keys are always strings).
    """
    weights = sim["weights"]
    if not weights:
        return {"days": [], "weight": {}, "bmi": {}, "intake": {}, "activity": {},
                "horizon_table": [], "percentiles": [str(q) for q in PERCENTILES]}
    n_days = len(weights[0]) - 1
    keys = [str(q) for q in PERCENTILES]

    days = []
    band = {k: [] for k in keys}
    bmi_band_ = {k: [] for k in keys}
    food_band = {k: [] for k in keys}
    act_band = {k: [] for k in keys}
    for i in range(1, n_days + 1):
        col = [p[i] for p in weights]
        fcol = [p[i - 1] for p in sim["food"]]
        acol = [p[i - 1] for p in sim["activity"]]
        days.append(i)
        for q, k in zip(PERCENTILES, keys):
            band[k].append(percentile(col, q))
            if height_cm:
                bmi_band_[k].append(percentile([bmi(x, height_cm) for x in col], q))
            food_band[k].append(percentile(fcol, q))
            act_band[k].append(percentile(acol, q))

    horizon_table = []
    for h in HORIZONS:
        if h > n_days:
            continue
        col = [p[h] for p in weights]
        row = {"days": h, "weight": {k: percentile(col, q) for q, k in zip(PERCENTILES, keys)}}
        if height_cm:
            row["bmi"] = {k: percentile([bmi(x, height_cm) for x in col], q)
                          for q, k in zip(PERCENTILES, keys)}
        horizon_table.append(row)

    return {
        "days": days,
        "weight": band,
        "bmi": bmi_band_,
        "intake": food_band,
        "activity": act_band,
        "horizon_table": horizon_table,
        "percentiles": keys,
    }


# -------------------------------------------------------------- scoring

def score_forecasts(forecasts: list[dict], actual: list[tuple[date, float]],
                    tolerance_days: int = 4) -> dict:
    """Compare each stored forecast against what actually happened.

    `actual` is the person's real weigh-ins. A horizon counts as scored only if
    there is a real weigh-in within `tolerance_days` of the target date -
    otherwise it is skipped rather than scored against a stale number.
    """
    actual_sorted = sorted(actual, key=lambda a: a[0])
    rows = []
    for f in forecasts:
        per_h = []
        for hrow in f["summary"].get("horizon_table", []):
            target = date.fromisoformat(f["as_of"]) + timedelta(days=hrow["days"])
            best, best_gap = None, None
            for d, w in actual_sorted:
                gap = abs((d - target).days)
                if gap <= tolerance_days and (best_gap is None or gap < best_gap):
                    best, best_gap = w, gap
            if best is None:
                continue
            pred = hrow["weight"]["50"]
            per_h.append({
                "days": hrow["days"],
                "target": target.isoformat(),
                "predicted": pred,
                "actual": best,
                "error": best - pred,
                "p05": hrow["weight"]["5"],
                "p95": hrow["weight"]["95"],
                "inside_band": hrow["weight"]["5"] <= best <= hrow["weight"]["95"],
            })
        if not per_h:
            continue
        errs = [r["error"] for r in per_h]
        rows.append({
            "id": f["id"],
            "as_of": f["as_of"],
            "points": per_h,
            "mae": sum(abs(e) for e in errs) / len(errs),
            "bias": sum(errs) / len(errs),
            "hit_rate_1kg": sum(1 for e in errs if abs(e) <= 1.0) / len(errs),
            "hit_rate_2kg": sum(1 for e in errs if abs(e) <= 2.0) / len(errs),
            "band_hit_rate": sum(1 for r in per_h if r["inside_band"]) / len(per_h),
        })
    rows.sort(key=lambda r: r["as_of"])
    all_errs = [e for r in rows for e in [p["error"] for p in r["points"]]]
    return {
        "forecasts": rows,
        "total_points": len(all_errs),
        "mae": (sum(abs(e) for e in all_errs) / len(all_errs)) if all_errs else None,
        "bias": (sum(all_errs) / len(all_errs)) if all_errs else None,
        "hit_rate_1kg": (sum(1 for e in all_errs if abs(e) <= 1.0) / len(all_errs))
                        if all_errs else None,
        "band_hit_rate": (
            sum(1 for r in rows for p in r["points"] if p["inside_band"])
            / max(1, sum(len(r["points"]) for r in rows))
        ) if rows else None,
    }
