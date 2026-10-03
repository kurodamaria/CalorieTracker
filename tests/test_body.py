"""Validate the body model against synthetic people whose true burn is known.

This is the test that matters: if fit_burn cannot recover a planted burn from
noisy weight data, the whole projection is fiction.
"""
import math
import os
import random
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.body import (  # noqa: E402
    DEFAULT_LAG_WEIGHTS, DEFAULT_KJ_PER_KG, HORIZONS, age_years, bmi, bmi_band,
    effective_intake, fit_burn, formula_burn_kj, interpolate_weights,
    mifflin_bmr_kcal, percentile, score_forecasts, simulate, smooth, summarise,
)

fails = []


def check(label, got, want, tol=0.01):
    ok = got is not None and abs(got - want) <= tol
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got:.4g} want={want:.4g} "
          f"(tol {tol})")
    if not ok:
        fails.append(label)


def check_true(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label} {detail}")
    if not cond:
        fails.append(label)


# ============================================================ BMR formula
print("\n--- Mifflin-St Jeor (published kcal/day examples) ---")
# Male, 80 kg, 180 cm, 30 y, +5: 800 + 1125 - 150 + 5 = 1780 kcal
check("male BMR", mifflin_bmr_kcal(80, 180, 30, 5.0), 1780.0, 0.5)
# Female, 65 kg, 165 cm, 30 y, -5: 650 + 1031.25 - 150 - 5 = 1526.25 kcal
check("female BMR", mifflin_bmr_kcal(65, 165, 30, -5.0), 1526.25, 0.5)

PERSON = {"dob": "1990-03-15", "height_cm": 175.0, "sex_offset_kcal": 5.0,
          "activity_multiplier": 1.2}
DOB = date(1990, 3, 15)
check("age years", age_years(DOB, date(2026, 10, 3)), 36.5534, 0.001)
check("age before birthday", age_years(DOB, date(2026, 3, 14)), 35.9973, 0.001)
check("age on the birthday itself is exact", age_years(DOB, date(2026, 3, 15)), 36.0, 1e-9)
check_true("leap-day dob does not crash", isinstance(
    age_years(date(2000, 2, 29), date(2026, 1, 1)), float))
_age = age_years(DOB, date(2026, 10, 3))
check("formula burn kJ (Mifflin x 1.2, fractional age)",
      formula_burn_kj(PERSON, 80.0, date(2026, 10, 3)),
      mifflin_bmr_kcal(80, 175, _age, 5.0) * 1.2 * 4.184, 1e-6)
check_true("formula burn is in kJ not kcal",
           formula_burn_kj(PERSON, 80.0, date(2026, 10, 3)) > 7000)
check("BMI", bmi(80, 180), 24.69, 0.01)
check("BMI at 100kg/180cm", bmi(100, 180), 30.86, 0.01)
check_true("BMI band obese", bmi_band(bmi(100, 180)) == "obese")
check_true("BMI zero height is None", bmi(80, 0) is None)

# ================================================== interpolation & smoothing
print("\n--- weight series ---")
ws = interpolate_weights([(date(2026, 1, 1), 80.0), (date(2026, 1, 11), 79.0)],
                         date(2026, 1, 1), date(2026, 1, 20))
check_true("no extrapolation before first weigh-in",
           date(2025, 12, 31) not in ws)
check_true("no extrapolation after last weigh-in",
           date(2026, 1, 12) not in ws)
check("interpolated midpoint", ws[date(2026, 1, 6)], 79.5, 1e-9)
check("measured point preserved", ws[date(2026, 1, 11)], 79.0, 1e-9)
check_true("empty measurements -> empty", interpolate_weights([], date(2026, 1, 1),
                                                            date(2026, 1, 5)) == {})
sm = smooth([1, 2, 3, 4, 5], 3)
check_true("smoothing preserves length", len(sm) == 5)
check_true("smoothing removes a spike",
           smooth([10, 10, 10, 10, 90], 3)[2] < 30)
eff = effective_intake([100.0] * 6, DEFAULT_LAG_WEIGHTS)
check_true("constant intake passes kernel unchanged",
           all(abs(e - 100.0) < 1e-9 for e in eff))

# ============================================ recover a planted known burn
def make_person_history(n_days, true_burn_kj, mean_intake_kj, intake_sd,
                        start_w, kj_per_kg, weigh_every=2, scale_noise=0.15,
                        seed=1, weekly=0.0):
    """Generate a plausible history from a known burn, then hand back daily rows
    exactly as the app would see them."""
    rng = random.Random(seed)
    day0 = date(2026, 1, 1)
    days, intake, act, weights = [], [], [], []
    w = start_w
    # a weekend bump so the day-of-week structure is real and blockable
    pattern = [1.0] * 7
    if weekly:
        pattern = [1.0, 1.0, 1.0, 1.0, 1.0, 1.15, 1.3]
    for i in range(n_days):
        d = day0 + timedelta(days=i)
        f = rng.gauss(mean_intake_kj * pattern[d.weekday()],
                      mean_intake_kj * intake_sd)
        a = 0.0
        days.append(d)
        intake.append(max(500.0, f))
        act.append(a)
        w += (intake[-1] - a - true_burn_kj) / kj_per_kg
        weights.append(w)

    # weigh-ins: noisy observations of the true trajectory
    measurements = []
    for i in range(0, n_days, weigh_every):
        d = day0 + timedelta(days=i)
        measurements.append((d, weights[i] + rng.gauss(0, scale_noise)))
    measurements.append((day0 + timedelta(days=n_days - 1),
                         weights[-1] + rng.gauss(0, scale_noise)))

    series = interpolate_weights(measurements, day0, day0 + timedelta(days=n_days - 1))
    daily = [{"date": day0 + timedelta(days=i), "net_kj": intake[i],
              "weight_kg": series.get(day0 + timedelta(days=i))} for i in range(n_days)]
    return daily, measurements


print("\n--- recovering a known burn (intake noise 8%, scale noise 0.15 kg) ---")
for true_burn in (8000.0, 10000.0, 12500.0):
    daily, meas = make_person_history(120, true_burn, true_burn + 400.0, 0.08,
                                      80.0, DEFAULT_KJ_PER_KG, seed=7, weekly=0.15)
    fit = fit_burn(daily)
    check_true(f"fit ok (true burn {true_burn:.0f})", fit["ok"], fit.get("reason") or "")
    check(f"recovered burn (true {true_burn:.0f})", fit["burn_kj"], true_burn,
          true_burn * 0.06)

print("\n--- robustness: noisier data ---")
for sd_in, sd_w, tol in ((0.15, 0.3, 0.12), (0.25, 0.5, 0.20)):
    daily, meas = make_person_history(150, 10000.0, 10500.0, sd_in, 80.0,
                                      DEFAULT_KJ_PER_KG, scale_noise=sd_w, seed=11)
    fit = fit_burn(daily)
    check(f"intake sd {sd_in:.0%} scale sd {sd_w} kg", fit["burn_kj"], 10000.0,
          10000.0 * tol)

print("\n--- a real weight trend is recovered as a trend, not as a wrong burn ---")
daily, meas = make_person_history(120, 10000.0, 8000.0, 0.05, 80.0,
                                  DEFAULT_KJ_PER_KG, seed=3)
fit = fit_burn(daily)
check("burn while in a 2000 kJ deficit", fit["burn_kj"], 10000.0, 500.0)
check_true("weight actually fell", fit["weight_end"] < fit["weight_start"],
           f"{fit['weight_start']:.2f} -> {fit['weight_end']:.2f}")
check_true("fit reports n_days and scatter",
           fit["n_days"] >= 100 and fit["residual_sd_kj"] is not None)

print("\n--- guard rails ---")
check_true("no data -> not ok", fit_burn([])["ok"] is False)
check_true("1 weigh-in -> not ok", fit_burn(
    [{"date": date(2026, 1, 1), "net_kj": 9000, "weight_kg": 80.0}])["ok"] is False)
short = [{"date": date(2026, 1, 1) + timedelta(days=i), "net_kj": 9000,
          "weight_kg": 80.0} for i in range(4)]
check_true("too few days -> explains itself",
           "at least 7 days" in (fit_burn(short).get("reason") or ""))
noW = [{"date": date(2026, 1, 1) + timedelta(days=i), "net_kj": 9000,
        "weight_kg": None} for i in range(30)]
check_true("no weights -> not ok", fit_burn(noW)["ok"] is False)
check_true("unstable input is reported via r2",
           fit_burn([{"date": date(2026, 1, 1) + timedelta(days=i),
                      "net_kj": 9000 + (5000 if i % 2 else -5000),
                      "weight_kg": 80.0 + (1.0 if i % 2 else -1.0)}
                     for i in range(60)])["r2"] is not None)

# ============================================================ projection
print("\n--- projection bands ---")
N_DAYS, START = 200, date(2026, 7, 1)
daily, meas = make_person_history(N_DAYS, 10000.0, 10400.0, 0.10, 80.0,
                                  DEFAULT_KJ_PER_KG, seed=5, weekly=0.15)
fit = fit_burn(daily)
end_w = interpolate_weights(meas, date(2026, 1, 1),
                            date(2026, 1, 1) + timedelta(days=N_DAYS - 1)
                            )[date(2026, 1, 1) + timedelta(days=N_DAYS - 1)]

days_hist = [d["date"] for d in daily]
food_hist = [d["net_kj"] for d in daily]
act_hist = [0.0] * N_DAYS
sim = simulate(days_hist, food_hist, act_hist, end_w, fit["burn_kj"],
               DEFAULT_KJ_PER_KG, START, 365, n_paths=600, seed=42)
summ = summarise(sim, 180.0)

check_true("band has 365 days", len(summ["days"]) == 365)
check_true("percentiles are ordered",
           all(summ["weight"]["5"][i] <= summ["weight"]["25"][i]
               <= summ["weight"]["50"][i] <= summ["weight"]["75"][i]
               <= summ["weight"]["95"][i] for i in range(365)))
check_true("p05 <= p95 everywhere",
           all(summ["weight"]["5"][i] <= summ["weight"]["95"][i] for i in range(365)))

w_now, w_30 = end_w, end_w + 30 * (10400.0 - fit["burn_kj"]) / DEFAULT_KJ_PER_KG
p30 = summ["weight"]["50"][29]
check("median 30-day projection matches the arithmetic", p30, w_30, 0.6)
check_true("band widens with horizon",
           (summ["weight"]["95"][299] - summ["weight"]["5"][299])
           > (summ["weight"]["95"][29] - summ["weight"]["5"][29]))

check_true("horizon table covers <=365", [r["days"] for r in summ["horizon_table"]]
           == [h for h in HORIZONS if h <= 365])
check_true("horizon rows carry BMI",
           all("bmi" in r and r["bmi"] is not None for r in summ["horizon_table"]))
check_true("intake + activity curves present",
           len(summ["intake"]["50"]) == 365 and len(summ["activity"]["50"]) == 365)

check_true("determinism: same seed -> same band",
           summarise(simulate(days_hist, food_hist, act_hist, end_w,
                              fit["burn_kj"], DEFAULT_KJ_PER_KG, START, 365,
                              n_paths=200, seed=9), 180.0)["weight"]["50"]
           == summarise(simulate(days_hist, food_hist, act_hist, end_w,
                                 fit["burn_kj"], DEFAULT_KJ_PER_KG, START, 365,
                                 n_paths=200, seed=9), 180.0)["weight"]["50"])
check_true("different seed -> different band",
           summarise(simulate(days_hist, food_hist, act_hist, end_w,
                              fit["burn_kj"], DEFAULT_KJ_PER_KG, START, 365,
                              n_paths=200, seed=1), 180.0)["weight"]["50"]
           != summarise(simulate(days_hist, food_hist, act_hist, end_w,
                                 fit["burn_kj"], DEFAULT_KJ_PER_KG, START, 365,
                                 n_paths=200, seed=2), 180.0)["weight"]["50"])

print("\n--- the projection must NOT be a random walk ---")
spread_30 = summ["weight"]["95"][29] - summ["weight"]["5"][29]
spread_365 = summ["weight"]["95"][364] - summ["weight"]["5"][364]
check_true("1-year band is wider than 1-month band", spread_365 > spread_30,
           f"{spread_30:.2f} kg -> {spread_365:.2f} kg")
check_true("1-year band stays physically plausible (<40 kg)",
           spread_365 < 40, f"{spread_365:.2f} kg")

print("\n--- burn uncertainty propagates ---")
tight = summarise(simulate(days_hist, food_hist, act_hist, end_w, fit["burn_kj"],
                           DEFAULT_KJ_PER_KG, START, 365, n_paths=400,
                           burn_sd=0.0, seed=3), 180.0)
loose = summarise(simulate(days_hist, food_hist, act_hist, end_w, fit["burn_kj"],
                           DEFAULT_KJ_PER_KG, START, 365, n_paths=400,
                           burn_sd=600.0, seed=3), 180.0)
check_true("wider burn uncertainty -> wider band",
           (loose["weight"]["95"][299] - loose["weight"]["5"][299])
           > (tight["weight"]["95"][299] - tight["weight"]["5"][299]))

print("\n--- a genuine weight trend shows in the median path ---")
trend_sim = summarise(simulate(days_hist, food_hist, act_hist, end_w, 10000.0,
                               DEFAULT_KJ_PER_KG, START, 365, n_paths=400,
                               seed=3), 180.0)
check_true("surplus intake projects weight gain",
           trend_sim["weight"]["50"][299] > end_w,
           f"{end_w:.2f} -> {trend_sim['weight']['50'][299]:.2f} kg")

# ============================================================== scoring
print("\n--- scoring forecasts against what happened ---")
fc = [{
    "id": 1, "as_of": "2026-01-01",
    "summary": {"horizon_table": [
        {"days": 30, "weight": {"5": 78.0, "50": 79.0, "95": 80.0}},
        {"days": 90, "weight": {"5": 77.0, "50": 78.0, "95": 79.0}},
    ]},
}]
sc = score_forecasts(fc, [(date(2026, 1, 31), 79.4), (date(2026, 4, 1), 77.5)])
check("MAE (over- and under-shoot)", sc["mae"], (0.4 + 0.5) / 2, 1e-9)
check("bias (signed, not absolute)", sc["bias"], (0.4 - 0.5) / 2, 1e-9)
check_true("MAE != bias when errors cancel", sc["mae"] > sc["bias"])
check("both inside band", sc["band_hit_rate"], 1.0, 1e-9)
check("all within 1 kg", sc["hit_rate_1kg"], 1.0, 1e-9)

sc2 = score_forecasts(fc, [(date(2026, 1, 31), 83.0)])
check_true("a 4 kg miss is flagged", sc2["hit_rate_1kg"] == 0.0)
check_true("and falls outside the band", sc2["band_hit_rate"] == 0.0)

sc3 = score_forecasts(fc, [(date(2026, 1, 20), 79.0)])
check_true("no weigh-in near the horizon -> not scored", sc3["total_points"] == 0)

sc3b = score_forecasts(fc, [(date(2026, 1, 20), 79.0), (date(2026, 3, 28), 78.0)])
check_true("a horizon with no nearby weigh-in is skipped, not guessed",
           [p["days"] for p in sc3b["forecasts"][0]["points"]] == [90],
           f"scored {[p['days'] for p in sc3b['forecasts'][0]['points']]}")
check_true("the 11-day-stale weigh-in was not used for the 30-day horizon",
           sc3b["forecasts"][0]["points"][0]["actual"] == 78.0)

fc2 = [dict(fc[0], id=2, as_of="2026-02-01",
            summary={"horizon_table": [{"days": 30, "weight": {"5": 77.0, "50": 78.0,
                                                              "95": 79.0}}]})]
sc4 = score_forecasts(fc + fc2, [(date(2026, 1, 31), 79.4), (date(2026, 3, 3), 79.0),
                                  (date(2026, 4, 1), 77.5)])
check_true("multiple forecasts scored, ordered oldest first",
           [r["as_of"] for r in sc4["forecasts"]] == ["2026-01-01", "2026-02-01"])
check("all three horizons pooled", sc4["total_points"], 3, 0)
check("bias pooled across forecasts", sc4["bias"],
      (0.4 - 0.5 + 1.0) / 3, 1e-9)

print()
if fails:
    print(f"{len(fails)} FAILURES:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("ALL BODY-MODEL CHECKS PASSED")
