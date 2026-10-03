"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  config: null,
  personId: null,
  persons: [],
  date: null,
  selected: null,          // chosen blueprint
  results: [],
  activeResult: 0,
  cache: { food: [], activity: [] },
  kind: "food",
  metrics: null,
  forecast: null,
  series: "weight",
  weights: [],
  showForecasts: new Set(),
};

const MACROS = ["protein", "carbs", "fat", "sugar", "fiber"];
const NUTRIENTS = ["energy", ...MACROS];

/* ============================================================ helpers */

const fmt = (v, dp = 0) =>
  v === null || v === undefined || Number.isNaN(v)
    ? "–"
    : v.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });

const fmtRaw = (v, dp = 3) =>
  v === null || v === undefined || Number.isNaN(v) ? "" : String(Number(v.toFixed(dp)));

const todayISO = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

const shiftDate = (iso, days) => {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10);
};

const numOrNull = (sel) => {
  const raw = $(sel).value.trim();
  if (raw === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
};

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

let toastTimer;
function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("err", isError);
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2800);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
  return body;
}

const energyUnit = () => (state.config.energy_display === "kcal" ? "kcal" : "kJ");
const fromKj = (kj) =>
  kj === null || kj === undefined
    ? null
    : state.config.energy_display === "kcal"
    ? kj / state.config.kj_per_kcal
    : kj;
const toKj = (v) =>
  v === null || v === undefined
    ? null
    : state.config.energy_display === "kcal"
    ? v * state.config.kj_per_kcal
    : v;

/* ============================================================ bootstrap */

async function init() {
  state.config = await api("/api/config");
  state.persons = state.config.persons;
  state.date = todayISO();
  $("#log-date").value = state.date;

  if (state.persons.length) state.personId = state.persons[0].id;
  renderPersonSelect();
  renderTargetFields();
  bindEvents();
  await refreshAll();

  if (state.config.migration_notes?.length) {
    toast(`Database upgraded: ${state.config.migration_notes.join("; ")}`);
  }
}

function personName(id = state.personId) {
  const p = state.persons.find((x) => x.id === id);
  return p ? p.name : "nobody";
}

function renderPersonSelect() {
  const sel = $("#person-select");
  if (!state.persons.length) {
    sel.innerHTML = `<option value="">no person</option>`;
    sel.disabled = true;
  } else {
    sel.disabled = false;
    sel.innerHTML = state.persons
      .map((p) => `<option value="${p.id}"${p.id === state.personId ? " selected" : ""}>${escapeHtml(p.name)}</option>`)
      .join("");
  }
  $("#no-person-banner").hidden = state.persons.length > 0;
  const who = personName();
  $("#log-who").textContent = state.personId ? `for ${who}` : "";
  $("#metric-who").textContent = state.personId ? who : "";
  $("#predict-who").textContent = state.personId ? who : "";
  $("#targets-who").textContent = who;
  $("#history-who").textContent = state.personId ? who : "";
  $$(".view").forEach((v) => v.classList.toggle("needs-person", !state.personId));
}

function showView(name) {
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  if (name === "predict" && state.personId) loadForecast();
  if (name === "targets" && state.personId) { loadTargets(); loadHistory(); }
}

async function refreshAll() {
  await loadBlueprints();
  if (state.personId) {
    await Promise.all([refreshDay(), loadMetrics(), loadWeights()]);
  } else {
    $("#entry-list").innerHTML = `<div class="empty-state">Create a person to start logging.</div>`;
    $("#metric-grid").innerHTML = "";
    $("#burn-compare").innerHTML = "";
  }
  renderFoodTable();
}

/* ================================================================= LOG */

async function refreshDay() {
  const d = await api(`/api/day/${state.date}?person_id=${state.personId}`);
  renderSummary(d);
  renderEntries(d);
}

function renderSummary(d) {
  const totals = d.totals;
  const targetE = d.targets.energy;
  const net = totals.energy;
  const pct = targetE && net !== null ? (net / targetE) * 100 : 0;

  $("#energy-total").textContent = fmt(net);
  $("#energy-unit").textContent = energyUnit();

  const heroTarget = $("#energy-target");
  if (targetE) {
    heroTarget.textContent =
      `of ${fmt(targetE)} ${d.unit} target · ${fmt(targetE - net)} left`;
    heroTarget.classList.toggle("over", net > targetE);
  } else {
    heroTarget.textContent = "no energy target set";
    heroTarget.classList.remove("over");
  }

  // eaten / burned / net, so activity never silently rewrites the food total
  const bal = $("#balance-line");
  if (d.activity_count > 0) {
    bal.innerHTML =
      `ate <b>${fmt(d.food_energy)}</b> ${d.unit}` +
      ` − activity <b>${fmt(Math.abs(d.activity_energy))}</b> ${d.unit}` +
      ` = net <b>${fmt(net)}</b> ${d.unit}` +
      ` <span class="hint-inline">(${fmt(d.activity_count, 1)} session${d.activity_count === 1 ? "" : "s"})</span>`;
    bal.classList.add("has-activity");
  } else {
    bal.textContent = "";
    bal.classList.remove("has-activity");
  }

  const bar = $("#energy-bar");
  bar.style.width = `${Math.max(0, Math.min(pct, 100))}%`;
  bar.classList.toggle("over", pct > 100);

  $("#macro-grid").innerHTML = MACROS.map((n) => {
    const value = totals[n];
    const target = d.targets[n];
    const partial = d.partial[n];
    const pctW = target && value !== null ? Math.min((value / target) * 100, 100) : 0;
    const missingPct = partial ? Math.round((1 - d.coverage[n]) * 100) : 0;
    const note = partial
      ? `<span class="macro-note">~${missingPct}% of today's food has no ${n} value</span>`
      : "";
    let sub;
    if (!target) sub = "no target";
    else if (value === null) sub = `target ${fmt(target)} g`;
    else sub = `target ${fmt(target)} g · ${fmt(target - value)} left`;
    return `<div class="macro${partial ? " partial" : ""}">
      <div class="macro-head">
        <span class="macro-name">${state.config.labels[n]}</span>
        <span class="macro-val${value === null ? " unknown" : ""}">${
          value === null ? "no data" : `${fmt(value)} g`
        }</span>
      </div>
      <div class="macro-bar"><i style="width:${pctW}%"></i></div>
      <div class="macro-target">${sub}</div>${note}</div>`;
  }).join("");

  const hints = [];
  if (d.energy_basis === "derived-from-macros") {
    hints.push("Energy estimated from macros (no food listed a measured energy value).");
  } else if (d.energy_basis === "unavailable") {
    hints.push("No energy data available for today's records.");
  }
  const partialNames = MACROS.filter((n) => d.partial[n]);
  if (partialNames.length) {
    hints.push(`${partialNames.join(", ")} totals cover only part of today's food — missing values were treated as 0.`);
  }
  $("#energy-hint").textContent = hints.join(" ");
  $("#energy-hint").classList.toggle("error", d.energy_basis === "unavailable");
}

function renderEntries(d) {
  const list = $("#entry-list");
  if (!d.entries.length) {
    list.innerHTML = `<div class="empty-state">Nothing logged for ${d.date}.</div>`;
    return;
  }
  const unit = d.unit;
  let out = "";
  for (const meal of state.config.meals) {
    const items = d.entries.filter((e) => e.entry.meal === meal && e.kind !== "activity");
    if (!items.length) continue;
    const b = d.meals[meal];
    // Food only: activity gets its own group below, and folding it in here
    // would print a nonsense subtotal like "snack -302 kJ".
    const sub = b.food_energy === null
      ? "no energy data"
      : `${fmt(b.food_energy)} ${unit}`;
    out += `<div class="entry-group-label">${meal} — ${sub}</div>`;
    for (const item of items) out += entryRow(item, unit, false);
  }
  const acts = d.entries.filter((e) => e.kind === "activity");
  if (acts.length) {
    const b = d.meals["snack"];
    const burned = Math.abs(d.activity_energy);
    out += `<div class="entry-group-label activity">activity — burned ${fmt(burned)} ${unit}</div>`;
    for (const item of acts) out += entryRow(item, unit, true);
  }
  list.innerHTML = out;
}

function entryRow(item, unit, isActivity) {
  const e = item.entry;
  const n = item.nutrients.energy;
  const missing = MACROS.filter((k) => item.nutrients[k] === null);
  const missingNote = !isActivity && missing.length
    ? ` · blank: ${missing.join(", ")}` : "";
  const qty = isActivity
    ? `${fmt(e.amount, 1)} session${e.amount === 1 ? "" : "s"}`
    : `${fmt(e.amount, 1)} ${e.unit} · ${fmt(item.grams, 1)} g`;
  const energyText = n === null
    ? "–"
    : `${isActivity ? "−" : ""}${fmt(Math.abs(fromKj(n)))} ${unit}`;
  return `<div class="entry${isActivity ? " is-activity" : ""}">
    <div>
      <div class="e-name">${escapeHtml(item.name)}${
        isActivity ? ` <span class="tag tag-act">activity</span>` : ""}</div>
      <div class="e-qty">${qty}${missingNote}</div>
    </div>
    <div></div>
    <div class="e-right">
      <div class="e-kj">${energyText}</div>
      <button class="del" data-entry="${e.id}" title="Remove">&times;</button>
    </div></div>`;
}

/* ========================================================== blueprints */

async function loadBlueprints() {
  const [foods, acts] = await Promise.all([
    api("/api/foods?kind=food"),
    api("/api/foods?kind=activity"),
  ]);
  state.cache.food = foods;
  state.cache.activity = acts;
  $("#no-foods-hint").hidden = foods.length + acts.length > 0;
  doSearch($("#food-search").value, false);
}

/* Searches the local cache synchronously so the dropdown can never be stale.
   `show` is false when only priming state (on load), so the list does not
   appear before the user has focused or typed. */
function doSearch(q, show = true) {
  const needle = q.trim().toLowerCase();
  const all = [...state.cache.food, ...state.cache.activity];
  state.results = all.filter(
    (f) =>
      !needle ||
      f.name.toLowerCase().includes(needle) ||
      (f.brand || "").toLowerCase().includes(needle)
  );
  state.activeResult = show && state.results.length ? 0 : -1;
  renderResults(show);
}

function renderResults(show = true) {
  const ul = $("#search-results");
  if (!state.results.length) {
    ul.innerHTML = `<li class="empty">Nothing matches. Add it under Database.</li>`;
    if (show) ul.classList.add("open");
    return;
  }
  const unit = energyUnit();
  ul.innerHTML = state.results
    .map((f, i) => {
      const known = MACROS.filter((n) => f.known[n]).length;
      const blank = known < MACROS.length ? ` · ${MACROS.length - known} blank` : "";
      const per = f.kind === "activity" ? "per session" : "per 100 g";
      return `<li data-idx="${i}" data-name="${escapeHtml(f.name)}" class="${
        i === state.activeResult ? "active" : ""
      } ${f.kind === "activity" ? "is-activity" : ""}">
        <div class="r-name">${escapeHtml(f.name)}${
          f.brand ? ` <span class="r-meta">${escapeHtml(f.brand)}</span>` : ""}${
          f.kind === "activity" ? ` <span class="tag tag-act">activity</span>` : ""}</div>
        <div class="r-meta">${fmt(f.energy)} ${unit} ${per}${blank}</div>
      </li>`;
    })
    .join("");
  if (show) ul.classList.add("open");
}

function closeResults() { $("#search-results").classList.remove("open"); }

function pickResult(idx) {
  const f = state.results[idx];
  if (!f) return;
  state.selected = f;
  $("#food-search").value = f.name;

  const opts = [];
  if (f.kind === "activity") {
    opts.push(`<option value="piece">session</option>`);
    $("#add-unit").innerHTML = opts.join("");
  } else {
    opts.push(`<option value="g">g</option>`);
    if (f.grams_per_piece)
      opts.push(`<option value="piece">piece (${fmt(f.grams_per_piece, 1)} g)</option>`);
    if (Math.abs(f.grams_per_ml - 1.0) > 1e-9)
      opts.push(`<option value="ml">ml (${fmt(f.grams_per_ml, 2)} g/ml)</option>`);
    const cur = $("#add-unit").value;
    $("#add-unit").innerHTML = opts.join("");
    $("#add-unit").value = $("#add-unit").querySelector(`option[value="${cur}"]`)
      ? cur : "g";
  }
  closeResults();
  updateAddHint();
  $("#add-amount").focus();
}

function updateAddHint() {
  const hint = $("#selected-hint");
  const f = state.selected;
  if (!f) {
    hint.textContent = "Pick something from the database first.";
    hint.className = "hint";
    return;
  }
  const amount = Number($("#add-amount").value);
  const unit = $("#add-unit").value;
  const isAct = f.kind === "activity";

  if (!amount || amount <= 0) {
    hint.textContent = `${f.name} selected — ${fmt(f.energy)} ${energyUnit()} ${
      isAct ? "per session" : "per 100 g"}. Enter an amount.`;
    hint.className = "hint";
    return;
  }
  if (!isAct && unit === "piece" && !f.grams_per_piece) {
    hint.textContent = `${f.name} has no piece weight set — log it in grams.`;
    hint.className = "hint error";
    return;
  }
  const grams = isAct ? amount
    : unit === "g" ? amount
    : unit === "ml" ? amount * (f.grams_per_ml || 1)
    : amount * (f.grams_per_piece || 0);
  const factor = isAct ? amount : grams / f.base_amount;
  const kj = f.energy_kj * factor;
  const unitName = energyUnit();
  const macros = isAct ? "" : " · " + MACROS.filter((n) => f.known[n])
    .map((n) => `${state.config.labels[n]} ${fmt(f.per100[n] * factor, 1)} g`)
    .join(" · ");
  hint.textContent = `${fmt(amount, 1)} ${isAct ? "session" : unit} → ${
    kj < 0 ? "−" : ""}${fmt(Math.abs(fromKj(kj)))} ${unitName} ${
    isAct ? "burned" : ""}${macros}`;
  hint.className = "hint";
}

/* ======================================================= database view */

function setKind(kind) {
  state.kind = kind;
  $$(".kind-btn").forEach((b) => b.classList.toggle("active", b.dataset.kind === kind));
  const isAct = kind === "activity";
  $("#basis-fields").hidden = isAct;
  $("#macro-fields").hidden = isAct;
  $("#mass-fields").hidden = isAct;
  $("#derive-row").hidden = isAct;
  $("#energy-label").textContent = isAct
    ? "Energy burned (negative) *"
    : "Gross energy *";
  $("#kind-help").textContent = isAct
    ? "Energy must be negative. One log = one session; log 2 for two sessions."
    : "Values are per 100 g by default; change the amount if your label says otherwise.";
  $("#list-title").innerHTML = isAct
    ? `Your activities <span class="count" id="food-count"></span>`
    : `Your foods <span class="count" id="food-count"></span>`;
  resetFoodForm();
  renderFoodTable();
}

function resetFoodForm() {
  $("#food-form").reset();
  $("#f-id").value = "";
  $("#f-base-amount").value = "100";
  $("#f-energy-unit").value = "kj";
  $("#f-g-per-ml").value = "1";
  document.getElementById("food-form-title").textContent =
    state.kind === "activity" ? "New activity" : "New food";
  $("#form-msg").textContent = "";
  $("#form-msg").className = "hint";
  updateBasisNote();
}

function updateBasisNote() {
  const base = Number($("#f-base-amount").value) || 0;
  const note = $("#basis-note");
  if (base > 0 && Math.abs(base - 100) > 1e-9) {
    note.textContent = `Scaled to per-100 g on save (×${fmt(100 / base, 3)}).`;
  } else {
    note.textContent = "Default: values per 100 g.";
  }
}

function renderFoodTable() {
  const rows = state.cache[state.kind] || [];
  const q = $("#food-list-search").value.trim().toLowerCase();
  const filtered = rows.filter(
    (f) => !q || f.name.toLowerCase().includes(q) || (f.brand || "").toLowerCase().includes(q)
  );
  const countEl = $("#food-count");
  if (countEl) countEl.textContent = `${filtered.length} item${filtered.length === 1 ? "" : "s"}`;

  const isAct = state.kind === "activity";
  $("#food-thead").innerHTML = isAct
    ? `<th>Activity</th><th class="num">Energy burned</th><th></th>`
    : `<th>Food</th><th class="num">Energy</th><th class="num">Protein</th>
       <th class="num">Carbs</th><th class="num">Fat</th><th class="num">Sugar</th>
       <th class="num">Fiber</th><th class="num">g/piece</th><th></th>`;
  const colspan = isAct ? 3 : 9;

  const tbody = $("#food-table tbody");
  if (!filtered.length) {
    tbody.innerHTML = `<tr><td colspan="${colspan}" class="empty-state">Nothing here yet.</td></tr>`;
    return;
  }
  const unit = energyUnit();
  tbody.innerHTML = filtered
    .map((f) => {
      const actions = `<div class="row-actions">
          <button data-edit="${f.id}">Edit</button>
          <button data-remove="${f.id}">Delete</button></div>`;
      if (isAct) {
        return `<tr>
          <td><strong>${escapeHtml(f.name)}</strong>${f.brand
            ? `<br /><span class="hint">${escapeHtml(f.brand)}</span>` : ""}
            ${f.notes ? `<br /><span class="hint">${escapeHtml(f.notes)}</span>` : ""}</td>
          <td class="num">−${fmt(Math.abs(f.energy))} ${unit}</td>
          <td>${actions}</td></tr>`;
      }
      const cell = (n) => f.known[n] ? fmt(f.per100[n], 1) : `<span class="unknown">–</span>`;
      return `<tr>
        <td><strong>${escapeHtml(f.name)}</strong>${f.brand
          ? `<br /><span class="hint">${escapeHtml(f.brand)}</span>` : ""}
          <br /><span class="hint">per ${fmt(f.base_amount, 0)} g as entered (${f.base_unit})</span></td>
        <td class="num">${fmt(f.energy, 1)} ${unit}</td>
        ${MACROS.map((n) => `<td class="num">${cell(n)}</td>`).join("")}
        <td class="num">${f.grams_per_piece
          ? fmt(f.grams_per_piece, 1) : `<span class="unknown">–</span>`}</td>
        <td>${actions}</td></tr>`;
    })
    .join("");
}

function fillFoodForm(id) {
  api(`/api/foods/${id}`).then((f) => {
    const s = f.stored;
    setKind(s.kind);
    $("#f-id").value = s.id;
    $("#f-name").value = s.name;
    $("#f-brand").value = s.brand || "";
    $("#f-base-amount").value = s.base_amount;
    $("#f-energy-unit").value = s.base_unit;
    const kj = s.energy_kj;
    $("#f-energy").value = s.base_unit === "kcal"
      ? fmtRaw(kj / state.config.kj_per_kcal)
      : fmtRaw(kj);
    $("#f-protein").value = s.protein_g ?? "";
    $("#f-carbs").value = s.carbs_g ?? "";
    $("#f-fat").value = s.fat_g ?? "";
    $("#f-sugar").value = s.sugar_g ?? "";
    $("#f-fiber").value = s.fiber_g ?? "";
    $("#f-g-per-ml").value = s.grams_per_ml;
    $("#f-g-per-piece").value = s.grams_per_piece ?? "";
    $("#f-notes").value = s.notes || "";
    document.getElementById("food-form-title").textContent = `Edit: ${s.name}`;
    updateBasisNote();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
}

async function submitFood(ev) {
  ev.preventDefault();
  const id = $("#f-id").value;
  const kind = $("#f-kind-hidden")?.value || state.kind;
  const payload = {
    name: $("#f-name").value.trim(),
    brand: $("#f-brand").value.trim(),
    kind,
    base_amount: Number($("#f-base-amount").value) || 100,
    energy: Number($("#f-energy").value),
    energy_unit: $("#f-energy-unit").value,
    protein: numOrNull("#f-protein"),
    carbs: numOrNull("#f-carbs"),
    fat: numOrNull("#f-fat"),
    sugar: numOrNull("#f-sugar"),
    fiber: numOrNull("#f-fiber"),
    grams_per_ml: Number($("#f-g-per-ml").value) || 1,
    grams_per_piece: numOrNull("#f-g-per-piece"),
    notes: $("#f-notes").value.trim(),
  };
  try {
    if (id) {
      await api(`/api/foods/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      toast("Saved");
    } else {
      await api("/api/foods", { method: "POST", body: JSON.stringify(payload) });
      toast(kind === "activity" ? "Activity added" : "Food added");
    }
    resetFoodForm();
    await loadBlueprints();
    renderFoodTable();
  } catch (err) {
    $("#form-msg").textContent = err.message;
    $("#form-msg").className = "hint error";
  }
}

function deriveEnergy() {
  const p = numOrNull("#f-protein");
  const c = numOrNull("#f-carbs");
  const f = numOrNull("#f-fat");
  if (p === null && c === null && f === null) {
    return toast("Fill in protein, carbs or fat first", true);
  }
  const kj = (p || 0) * 17 + (c || 0) * 17 + (f || 0) * 37;
  const unit = $("#f-energy-unit").value;
  $("#f-energy").value = unit === "kcal"
    ? fmtRaw(kj / state.config.kj_per_kcal) : fmtRaw(kj);
  toast("Estimated from macros (17/17/37 kJ per g)");
}

async function deleteFood(id) {
  if (!confirm("Delete this? Any records of it are removed too.")) return;
  await api(`/api/foods/${id}`, { method: "DELETE" });
  toast("Deleted");
  if ($("#f-id").value === String(id)) resetFoodForm();
  if (state.selected?.id === Number(id)) { state.selected = null; updateAddHint(); }
  await loadBlueprints();
  renderFoodTable();
  if (state.personId) await refreshDay();
}

/* ============================================================== people */

function renderTargetFields() {
  $("#target-grid").innerHTML = NUTRIENTS.map((n) => {
    const unit = n === "energy" ? "kJ" : "g";
    return `<label>${state.config.labels[n]} (${unit})
      <input type="number" step="any" min="0" id="t-${n}" placeholder="off" /></label>`;
  }).join("");
}

function resetPersonForm() {
  $("#person-form").reset();
  $("#p-id").value = "";
  $("#p-sex").value = "male";
  $("#p-sex-offset").value = state.config.sex_offsets_kcal.male;
  $("#p-activity").value = state.config.activity_default;
  document.getElementById("person-form-title").textContent = "New person";
  $("#p-delete").hidden = true;
  $("#person-msg").textContent = "";
}

function fillPersonForm(id) {
  const p = state.persons.find((x) => x.id === id);
  if (!p) return resetPersonForm();
  $("#p-id").value = p.id;
  $("#p-name").value = p.name;
  $("#p-dob").value = p.dob;
  $("#p-height").value = p.height_cm;
  $("#p-sex").value = p.sex;
  $("#p-sex-offset").value = p.sex_offset_kcal;
  $("#p-activity").value = p.activity_multiplier;
  document.getElementById("person-form-title").textContent = `Edit: ${p.name}`;
  $("#p-delete").hidden = false;
  $("#person-msg").textContent = "";
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function submitPerson(ev) {
  ev.preventDefault();
  const id = $("#p-id").value;
  const payload = {
    name: $("#p-name").value.trim(),
    dob: $("#p-dob").value,
    sex: $("#p-sex").value,
    sex_offset_kcal: Number($("#p-sex-offset").value) || 0,
    height_cm: Number($("#p-height").value),
    activity_multiplier: Number($("#p-activity").value) || 1.2,
    target_energy_kj: numOrNull("#t-energy") !== null ? toKj(numOrNull("#t-energy")) : null,
    target_protein_g: numOrNull("#t-protein"),
    target_carbs_g: numOrNull("#t-carbs"),
    target_fat_g: numOrNull("#t-fat"),
    target_sugar_g: numOrNull("#t-sugar"),
    target_fiber_g: numOrNull("#t-fiber"),
  };
  try {
    let saved;
    if (id) {
      saved = await api(`/api/persons/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      toast("Person updated");
    } else {
      saved = await api("/api/persons", { method: "POST", body: JSON.stringify(payload) });
      toast("Person added");
    }
    await reloadPersons();
    state.personId = saved.id;
    renderPersonSelect();
    fillPersonForm(saved.id);
    await Promise.all([refreshDay(), loadMetrics(), loadWeights()]);
  } catch (err) {
    $("#person-msg").textContent = err.message;
    $("#person-msg").className = "hint error";
  }
}

async function reloadPersons() {
  state.config = await api("/api/config");
  state.persons = state.config.persons;
}

async function switchPerson(id) {
  state.personId = Number(id);
  renderPersonSelect();
  fillPersonForm(state.personId);
  state.forecast = null;
  await Promise.all([refreshDay(), loadMetrics(), loadWeights()]);
  loadTargets();
  if ($("#view-predict").classList.contains("active")) loadForecast();
}

async function loadMetrics() {
  const m = await api(`/api/persons/${state.personId}/metrics`);
  state.metrics = m;
  const unit = energyUnit();

  const tiles = [
    ["Weight", m.weight_now === null ? "–" : `${fmt(m.weight_now, 1)} kg`,
      m.weight_date || "never weighed"],
    ["BMI", m.bmi_now === null ? "–" : fmt(m.bmi_now, 1),
      m.bmi_band || "needs a weight"],
    ["Age", fmt(m.age, 1), "years"],
    ["BMR", m.bmr_kcal === null ? "–" : fmt(m.bmr_kcal),
      "kcal/day, at rest"],
    ["Mean intake", m.mean_intake_kj === null ? "–" : fmt(fromKj(m.mean_intake_kj)),
      `${unit}/day over ${m.logged_days} logged days`],
  ];
  $("#metric-grid").innerHTML = tiles
    .map(([k, v, s]) => `<div class="metric"><span class="m-name">${k}</span>
      <span class="m-val">${v}</span><span class="m-sub">${escapeHtml(s)}</span></div>`)
    .join("");

  const bc = $("#burn-compare");
  if (!m.weight_now) {
    bc.innerHTML = `<p class="hint">Record a weight to unlock the energy model.</p>`;
    return;
  }
  const fitted = m.fitted_burn_kj === null ? null
    : `${fmt(m.fitted_burn)} ${unit}`;
  const formula = m.formula_burn === null ? null
    : `${fmt(m.formula_burn)} ${unit}`;
  const diff = m.fitted_burn_kj && m.formula_burn_kj
    ? ((m.fitted_burn_kj - m.formula_burn_kj) / m.formula_burn_kj) * 100 : null;

  bc.innerHTML = `
    <div class="burn-card${m.burn_source === "fitted" ? " primary" : ""}">
      <span class="b-label">Daily burn used</span>
      <span class="b-val">${fmt(m.burn)} ${unit}</span>
      <span class="b-sub">${
        m.burn_source === "fitted"
          ? "measured from your weight + intake history"
          : "formula estimate — not yet calibrated"}</span>
    </div>
    <div class="burn-card">
      <span class="b-label">Fitted from data</span>
      <span class="b-val">${fitted ?? "–"}</span>
      <span class="b-sub">${m.fit.ok
        ? `${m.fit.n_days} days · ${fmt(m.fit.residual_sd_kj / state.config.kj_per_kcal, 0)} kcal/day scatter`
        : escapeHtml(m.not_calibrated_reason || "not enough data")}</span>
    </div>
    <div class="burn-card">
      <span class="b-label">Formula (Mifflin ×${fmt(m.person.activity_multiplier, 2)})</span>
      <span class="b-val">${formula ?? "–"}</span>
      <span class="b-sub">${diff === null ? "waiting for fitted value"
        : `${diff >= 0 ? "+" : ""}${fmt(diff, 1)}% vs fitted`}</span>
    </div>
    ${m.calibrated ? "" : `<p class="hint warn">Uncalibrated: ${
      escapeHtml(m.not_calibrated_reason || "not enough data")}. The projection below
      is formula-only, so treat it as a rough shape rather than a forecast.</p>`}`;
}

/* ============================================================= weights */

async function loadWeights() {
  const rows = await api(`/api/persons/${state.personId}/weights?days=400`);
  state.weights = rows;
  renderWeightTable();
  renderWeightChart();
}

function renderWeightTable() {
  const tbody = $("#weight-table tbody");
  if (!state.weights.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty-state">No weights recorded.</td></tr>`;
    return;
  }
  const m = state.metrics;
  tbody.innerHTML = state.weights
    .slice()
    .reverse()
    .map((w) => `<tr>
      <td>${w.measured_on}</td>
      <td class="num">${fmt(w.weight_kg, 1)}</td>
      <td class="num">${w.bmi === null ? "–" : fmt(w.bmi, 1)}</td>
      <td><div class="row-actions">
        <button data-del-weight="${w.id}">Delete</button></div></td></tr>`)
    .join("");
}

async function addWeight() {
  const kg = Number($("#w-kg").value);
  if (!kg || kg <= 0) return toast("Enter a weight", true);
  const day = $("#w-date").value || todayISO();
  await api(`/api/persons/${state.personId}/weights`, {
    method: "POST",
    body: JSON.stringify({ measured_on: day, weight_kg: kg }),
  });
  $("#w-kg").value = "";
  toast("Weight recorded");
  await Promise.all([loadWeights(), loadMetrics(), refreshDay()]);
}

/* ============================================================== charts */

function drawChart(el, spec) {
  const width = el.clientWidth || 900;
  const height = spec.height || 320;
  const pad = { t: 14, r: 16, b: 30, l: 52 };
  const iw = width - pad.l - pad.r;
  const ih = height - pad.t - pad.b;

  // A band-only series has no `points`, so gather the domain from every shape
  // explicitly rather than assuming points exist.
  const series = spec.series.filter((s) => !s.hide);
  const xs = [];
  const ys = [];
  for (const s of series) {
    for (const p of s.points || []) {
      xs.push(p.x);
      if (p.y !== null && !Number.isNaN(p.y)) ys.push(p.y);
    }
    for (const b of [s.band, s.band2]) {
      if (!b) continue;
      for (const arr of [b.lo, b.hi]) {
        for (let i = 0; i < arr.length; i++) {
          xs.push(i + 1);
          if (!Number.isNaN(arr[i])) ys.push(arr[i]);
        }
      }
    }
  }
  if (!xs.length || !ys.length) {
    el.innerHTML = `<div class="empty-state">Nothing to plot yet.</div>`;
    return;
  }
  let x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (spec.yMin !== undefined) y0 = spec.yMin;
  if (spec.yMax !== undefined) y1 = spec.yMax;
  const padY = (y1 - y0) * 0.08 || 1;
  y0 -= padY; y1 += padY;
  if (x1 === x0) x1 = x0 + 1;

  const sx = (x) => pad.l + ((x - x0) / (x1 - x0)) * iw;
  const sy = (y) => pad.t + ih - ((y - y0) / (y1 - y0)) * ih;

  const path = (pts) =>
    pts.filter((p) => p.y !== null && !Number.isNaN(p.y))
      .map((p, i) => `${i ? "L" : "M"}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`)
      .join(" ");

  const bandPath = (lo, hi) => {
    const up = [], down = [];
    for (let i = 0; i < lo.length; i++) {
      up.push(`${sx(i + 1).toFixed(1)},${sy(hi[i]).toFixed(1)}`);
      down.unshift(`${sx(i + 1).toFixed(1)},${sy(lo[i]).toFixed(1)}`);
    }
    return `M${up.join(" L")} L${down.join(" L")} Z`;
  };

  const parts = [];
  // grid + axes
  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const v = y0 + ((y1 - y0) * i) / ticks;
    const y = sy(v);
    parts.push(`<line class="grid" x1="${pad.l}" y1="${y.toFixed(1)}" x2="${pad.l + iw}" y2="${y.toFixed(1)}"/>`);
    parts.push(`<text class="ax" x="${pad.l - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end">${
      spec.yFmt ? spec.yFmt(v) : fmt(v, 1)}</text>`);
  }
  const xTicks = Math.min(6, Math.max(2, Math.floor(iw / 110)));
  for (let i = 0; i <= xTicks; i++) {
    const v = x0 + ((x1 - x0) * i) / xTicks;
    parts.push(`<text class="ax" x="${sx(v).toFixed(1)}" y="${pad.t + ih + 20}" text-anchor="middle">${
      spec.xFmt ? spec.xFmt(v) : fmt(v, 0)}</text>`);
  }
  // today marker
  if (spec.todayX !== undefined && spec.todayX >= x0 && spec.todayX <= x1) {
    parts.push(`<line class="today" x1="${sx(spec.todayX).toFixed(1)}" y1="${pad.t}"
      x2="${sx(spec.todayX).toFixed(1)}" y2="${pad.t + ih}"/>`);
  }
  // horizontal reference lines (targets)
  (spec.refs || []).forEach((r) => {
    if (r.value < y0 || r.value > y1) return;
    parts.push(`<line class="ref" x1="${pad.l}" y1="${sy(r.value).toFixed(1)}"
      x2="${pad.l + iw}" y2="${sy(r.value).toFixed(1)}"/>`);
    parts.push(`<text class="ref-label" x="${pad.l + iw - 4}" y="${(sy(r.value) - 5).toFixed(1)}"
      text-anchor="end">${escapeHtml(r.label)}</text>`);
  });

  // bands first so lines sit on top
  series.filter((s) => s.band).forEach((s) => {
    parts.push(`<path class="band ${s.cls || ""}" d="${bandPath(s.band.lo, s.band.hi)}"/>`);
  });
  series.filter((s) => s.band2).forEach((s) => {
    parts.push(`<path class="band inner ${s.cls || ""}" d="${bandPath(s.band2.lo, s.band2.hi)}"/>`);
  });
  series.filter((s) => s.dots).forEach((s) => {
    parts.push((s.points || []).filter((p) => p.y !== null).map((p) =>
      `<circle class="dot ${s.cls || ""}" cx="${sx(p.x).toFixed(1)}" cy="${sy(p.y).toFixed(1)}" r="3.2"><title>${escapeHtml(p.label || "")}</title></circle>`).join(""));
  });
  series.filter((s) => s.line && !s.band).forEach((s) => {
    parts.push(`<path class="line ${s.cls || ""}" d="${path(s.points || [])}"/>`);
  });

  el.innerHTML = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}"
    preserveAspectRatio="none">${parts.join("")}</svg>`;
}

function renderWeightChart() {
  const el = $("#weight-chart");
  if (state.weights.length < 2) {
    el.innerHTML = `<div class="empty-state">Record at least two weights to see a trend.</div>`;
    return;
  }
  const toDay = (iso) => Math.floor(Date.parse(iso + "T00:00:00Z") / 86400000);
  const series = [{
    key: "w", line: true, dots: true, cls: "actual",
    points: state.weights.map((w) => ({ x: toDay(w.measured_on), y: w.weight_kg, label: `${w.measured_on}: ${w.weight_kg} kg, BMI ${w.bmi ? w.bmi.toFixed(1) : "-"}` })),
  }];
  drawChart(el, {
    height: 240, series,
    yFmt: (v) => `${v.toFixed(1)}`,
    xFmt: (v) => {
      const d = new Date(v * 86400000);
      return `${d.getUTCMonth() + 1}/${d.getUTCDate()}`;
    },
  });
}

/* =========================================================== forecast */

async function loadForecast() {
  const box = $("#fc-status");
  box.innerHTML = `<p class="hint">Calculating…</p>`;
  try {
    const fc = await api(`/api/persons/${state.personId}/forecast`);
    state.forecast = fc;
  } catch (err) {
    box.innerHTML = `<p class="hint error">${escapeHtml(err.message)}</p>`;
    return;
  }
  renderForecast();
  renderHorizons();
  loadScorecard();
}

function renderForecast() {
  const fc = state.forecast;
  const box = $("#fc-status");
  const unit = energyUnit();
  if (!fc || !fc.ok) {
    box.innerHTML = `<p class="hint error">${escapeHtml(fc?.reason || "no data")}</p>`;
    $("#forecast-chart").innerHTML = "";
    $("#forecast-legend").innerHTML = "";
    return;
  }
  const m = fc.metrics;
  const burnKj = fc.burn_kj;
  const parts = [];
  parts.push(`<div class="fc-badges">
    <span class="badge ${fc.calibrated ? "ok" : "warn"}">${
      fc.calibrated ? "calibrated to your data" : "uncalibrated"}</span>
    <span class="badge">burn ${fmt(fromKj(burnKj))} ${unit}/day (${
      fc.burn_source === "fitted" ? "fitted from " + fc.n_days_history + " days"
                                  : "formula estimate"})</span>
    <span class="badge">${fc.n_paths} simulated futures</span>
    <span class="badge">${fmt(state.metrics.kj_per_kg / state.config.kj_per_kcal)} kcal/kg</span>
  </div>`);
  if (!fc.calibrated) {
    parts.push(`<p class="hint warn">Uncalibrated: ${escapeHtml(
      fc.not_calibrated_reason || "not enough history")}. The shape of the curve is
      meaningful; the exact number is not. Add weigh-ins on a few different days and
      keep logging food.</p>`);
  }
  box.innerHTML = parts.join("");

  const s = fc.summary;
  const days = s.days;
  const series = [];
  const legend = [];

  // stored forecasts the user has ticked
  const archive = state.archive || { forecasts: [], all_ids: [] };
  for (const f of archive.forecasts) {
    if (!state.showForecasts.has(f.id)) continue;
    const fs = f.summary;
    series.push({
      key: `f${f.id}`, line: true, cls: `saved s${f.id % 5}`,
      points: (fs.weight["50"] || []).map((y, i) => ({ x: i + 1, y })),
    });
    legend.push(`<span class="lg"><i class="sw saved s${f.id % 5}"></i>saved ${f.as_of}</span>`);
  }

  if (state.series === "weight" || state.series === "bmi") {
    const key = state.series;
    const band = s[key];
    series.push({
      key: "band", band: { lo: band["5"], hi: band["95"] }, cls: "p90",
      band2: { lo: band["25"], hi: band["75"] },
    });
    series.push({
      key: "med", line: true, cls: "median",
      points: (band["50"] || []).map((y, i) => ({ x: i + 1, y })),
    });
    legend.unshift(
      `<span class="lg"><i class="sw p90"></i>5–95% of futures</span>`,
      `<span class="lg"><i class="sw p50"></i>25–75%</span>`,
      `<span class="lg"><i class="sw median"></i>median</span>`);
  } else {
    const intake = s.intake["50"].map((v) => toKj(v));
    const act = s.activity["50"].map((v) => toKj(v));
    series.push({
      key: "iband", band: { lo: s.intake["5"].map(toKj), hi: s.intake["95"].map(toKj) }, cls: "p90",
    });
    series.push({
      key: "actband", band: { lo: s.activity["5"].map(toKj), hi: s.activity["95"].map(toKj) }, cls: "pact",
    });
    series.push({ key: "intake", line: true, cls: "median", points: intake.map((y, i) => ({ x: i + 1, y })) });
    series.push({ key: "act", line: true, cls: "actline", points: act.map((y, i) => ({ x: i + 1, y })) });
    series.push({ key: "burn", line: true, cls: "burnline", points: days.map((_, i) => ({ x: i + 1, y: burnKj })) });
    legend.unshift(
      `<span class="lg"><i class="sw median"></i>simulated intake (median)</span>`,
      `<span class="lg"><i class="sw p90"></i>intake 5–95%</span>`,
      `<span class="lg"><i class="sw pact"></i>activity 5–95%</span>`,
      `<span class="lg"><i class="sw actline"></i>simulated activity</span>`,
      `<span class="lg"><i class="sw burnline"></i>daily burn</span>`);
  }

  // actual weights, if they fall inside the window
  const todayDay = Math.floor(Date.parse(todayISO() + "T00:00:00Z") / 86400000);
  const first = state.weights.length ? state.weights[0].measured_on : null;
  if (first) {
    const startDay = Math.floor(Date.parse(first + "T00:00:00Z") / 86400000);
    const pts = state.weights
      .map((w) => {
        const d = Math.floor(Date.parse(w.measured_on + "T00:00:00Z") / 86400000);
        return { x: d - todayDay, y: w.weight_kg, label: `${w.measured_on}: ${w.weight_kg} kg` };
      })
      .filter((p) => p.x <= 0 && p.x >= startDay - todayDay);
    if (pts.length) {
      series.push({ key: "act", dots: true, cls: "actual", points: pts });
      if (state.series !== "intake") {
        legend.unshift(`<span class="lg"><i class="sw actual"></i>your weigh-ins</span>`);
      }
    }
  }

  const target = m.person.target_energy_kj;
  const refs = [];
  if (state.series === "intake" && target) {
    refs.push({ value: target, label: "energy target" });
  }

  drawChart($("#forecast-chart"), {
    height: 340,
    series,
    refs,
    todayX: 0,
    yFmt: state.series === "bmi" ? (v) => v.toFixed(1)
      : (v) => fmt(v, state.series === "intake" ? 0 : 1),
    xFmt: (v) => (v === 0 ? "today" : `+${Math.round(v)}d`),
  });
  $("#forecast-legend").innerHTML = legend.join("");
}

function renderHorizons() {
  const fc = state.forecast;
  const tbody = $("#horizon-table tbody");
  if (!fc?.ok) { tbody.innerHTML = `<tr><td colspan="7" class="empty-state">–</td></tr>`; return; }
  const h = state.metrics.person.height_cm;
  tbody.innerHTML = fc.summary.horizon_table.map((row) => {
    const w = row.weight;
    const b = row.bmi;
    return `<tr>
      <td>+${row.days} days</td>
      <td class="num">${fmt(w["5"], 1)}</td>
      <td class="num">${fmt(w["25"], 1)}</td>
      <td class="num strong">${fmt(w["50"], 1)}</td>
      <td class="num">${fmt(w["75"], 1)}</td>
      <td class="num">${fmt(w["95"], 1)}</td>
      <td class="num">${b ? fmt(b["50"], 1) : "–"}</td></tr>`;
  }).join("");
}

async function loadScorecard() {
  const data = await api(`/api/persons/${state.personId}/forecasts`);
  state.archive = data;
  const sc = data.scorecard;
  const byId = Object.fromEntries(sc.forecasts.map((f) => [f.id, f]));

  $("#scorecard").innerHTML = sc.total_points
    ? `<div class="sc-grid">
        <div class="sc"><span class="sc-label">Mean absolute error</span>
          <span class="sc-val">${fmt(sc.mae, 2)} kg</span>
          <span class="sc-sub">over ${sc.total_points} scored horizon${sc.total_points === 1 ? "" : "s"}</span></div>
        <div class="sc"><span class="sc-label">Bias</span>
          <span class="sc-val ${sc.bias > 0 ? "hi" : sc.bias < 0 ? "lo" : ""}">${
            sc.bias > 0 ? "+" : ""}${fmt(sc.bias, 2)} kg</span>
          <span class="sc-sub">${
            Math.abs(sc.bias) < 0.3 ? "unbiased"
            : sc.bias > 0 ? "you weigh more than predicted"
            : "you weigh less than predicted"}</span></div>
        <div class="sc"><span class="sc-label">Within 1 kg</span>
          <span class="sc-val">${fmt(sc.hit_rate_1kg * 100, 0)}%</span>
          <span class="sc-sub">median was close</span></div>
        <div class="sc"><span class="sc-label">Inside the 5–95% band</span>
          <span class="sc-val">${fmt(sc.band_hit_rate * 100, 0)}%</span>
          <span class="sc-sub">90% band is honest if ≥80%</span></div>
      </div>`
    : `<p class="hint">No saved forecast has reached its horizon yet. Save one, then
       check back once the date arrives and weigh in — that is when scoring kicks in.</p>`;

  const tbody = $("#forecast-table tbody");
  if (!data.forecasts.length) {
    tbody.innerHTML = `<tr><td colspan="10" class="empty-state">Nothing saved yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = data.forecasts.slice().reverse().map((f) => {
    const s = byId[f.id];
    const p50 = (d) => {
      const row = (f.summary.horizon_table || []).find((r) => r.days === d);
      return row ? fmt(row.weight["50"], 1) : "–";
    };
    return `<tr>
      <td><input type="checkbox" data-show-fc="${f.id}" ${
        state.showForecasts.has(f.id) ? "checked" : ""}></td>
      <td>${f.as_of}<br /><span class="hint">${f.burn_source} · ${
        fmt(fromKj(f.burn_kj))} ${energyUnit()}/day</span></td>
      <td class="num">${fmt(fromKj(f.burn_kj))}</td>
      <td class="num">${p50(30)}</td>
      <td class="num">${p50(90)}</td>
      <td class="num">${s ? fmt(s.mae, 2) : "–"}</td>
      <td class="num">${s ? fmt(s.bias, 2) : "–"}</td>
      <td class="num">${s ? fmt(s.hit_rate_1kg * 100, 0) + "%" : "–"}</td>
      <td class="num">${s ? fmt(s.band_hit_rate * 100, 0) + "%" : "–"}</td>
      <td><div class="row-actions"><button data-del-fc="${f.id}">Delete</button></div></td>
    </tr>`;
  }).join("");
}

/* ============================================================ targets */

async function loadTargets() {
  const p = state.persons.find((x) => x.id === state.personId);
  if (!p) return;
  NUTRIENTS.forEach((n) => {
    const key = n === "energy" ? "target_energy_kj" : `target_${n}_g`;
    $("#t-" + n).value = p[key] === null || p[key] === undefined ? "" : fmtRaw(p[key], 1);
  });
}

async function saveTargets() {
  if (!state.personId) return toast("Pick a person first", true);
  const p = state.persons.find((x) => x.id === state.personId);
  if (!p) return toast("Person not loaded yet", true);
  const body = {
    name: p.name, dob: p.dob, sex: p.sex, sex_offset_kcal: p.sex_offset_kcal,
    height_cm: p.height_cm, activity_multiplier: p.activity_multiplier,
    target_energy_kj: numOrNull("#t-energy") !== null ? toKj(numOrNull("#t-energy")) : null,
    target_protein_g: numOrNull("#t-protein"),
    target_carbs_g: numOrNull("#t-carbs"),
    target_fat_g: numOrNull("#t-fat"),
    target_sugar_g: numOrNull("#t-sugar"),
    target_fiber_g: numOrNull("#t-fiber"),
  };
  try {
    await api(`/api/persons/${state.personId}`, { method: "PUT", body: JSON.stringify(body) });
    await reloadPersons();
    $("#targets-msg").textContent = "Saved.";
    $("#targets-msg").className = "hint";
    await Promise.all([refreshDay(), loadMetrics()]);
  } catch (err) {
    $("#targets-msg").textContent = err.message;
    $("#targets-msg").className = "hint error";
  }
}

async function loadHistory() {
  const rows = await api(`/api/history?person_id=${state.personId}&days=14`);
  $("#history").innerHTML = rows.map((r) => {
    if (!r.logged) {
      return `<div class="hist-row"><span class="hist-date">${r.date}</span>
        <span class="hist-bar"></span><span class="hist-val none">nothing logged</span></div>`;
    }
    const pct = r.target_energy ? Math.min((r.energy / r.target_energy) * 100, 100) : 0;
    const act = r.activity_energy ? ` · −${fmt(Math.abs(r.activity_energy))} act` : "";
    return `<div class="hist-row"><span class="hist-date">${r.date}</span>
      <span class="hist-bar"><i style="width:${pct}%"></i></span>
      <span class="hist-val">${fmt(r.energy)} ${r.unit}${act}</span></div>`;
  }).join("");
}

function loadModelSettings() {
  const s = state.config.settings;
  $("#s-kj-per-kg").value = s.kj_per_kg;
  $("#s-cal-days").value = s.calibration_days;
  $("#s-paths").value = s.n_paths;
  const lag = JSON.parse(s.lag_weights);
  [0, 1, 2, 3].forEach((i) => { $(`#s-lag${i}`).value = lag[i] ?? 0; });
}

async function saveModelSettings() {
  const body = {
    kj_per_kg: Number($("#s-kj-per-kg").value),
    calibration_days: Number($("#s-cal-days").value),
    n_paths: Number($("#s-paths").value),
    lag_weights: [0, 1, 2, 3].map((i) => Number($(`#s-lag${i}`).value)),
  };
  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
    await reloadPersons();
    // show back exactly what the server stored, including the normalisation it
    // applied to the lag kernel
    loadModelSettings();
    $("#model-msg").textContent = "Saved.";
    $("#model-msg").className = "hint";
    await Promise.all([loadMetrics(), loadWeights()]);
    if ($("#view-predict").classList.contains("active")) loadForecast();
  } catch (err) {
    $("#model-msg").textContent = err.message;
    $("#model-msg").className = "hint error";
  }
}

async function setDisplayUnit(unit) {
  if (unit === state.config.energy_display) return;
  await api("/api/settings", { method: "PUT", body: JSON.stringify({ energy_display: unit }) });
  await reloadPersons();
  syncUnitButtons();
  await Promise.all([loadBlueprints(), refreshDay(), loadMetrics()]);
  renderFoodTable();
  if (state.forecast) { await loadForecast(); loadTargets(); loadHistory(); }
}

function syncUnitButtons() {
  $$(".unit-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.unit === state.config.energy_display));
}

/* ============================================================= events */

function bindEvents() {
  $$(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));
  $$(".unit-btn").forEach((b) =>
    b.addEventListener("click", () => setDisplayUnit(b.dataset.unit)));
  $$(".kind-btn").forEach((b) =>
    b.addEventListener("click", () => setKind(b.dataset.kind)));
  $$(".ct").forEach((b) => b.addEventListener("click", () => {
    $$(".ct").forEach((x) => x.classList.toggle("active", x === b));
    state.series = b.dataset.series;
    renderForecast();
  }));

  $("#person-select").addEventListener("change", (e) => switchPerson(e.target.value));
  $("#person-add").addEventListener("click", () => {
    showView("people");
    resetPersonForm();
  });
  $("#p-new").addEventListener("click", () => resetPersonForm());
  $("#p-sex").addEventListener("change", (e) => {
    $("#p-sex-offset").value = state.config.sex_offsets_kcal[e.target.value] ?? 0;
  });
  $("#person-form").addEventListener("submit", submitPerson);
  $("#p-delete").addEventListener("click", async () => {
    const id = Number($("#p-id").value);
    if (!id || !confirm("Delete this person, all their records and forecasts?")) return;
    await api(`/api/persons/${id}`, { method: "DELETE" });
    toast("Person deleted");
    await reloadPersons();
    state.personId = state.persons[0]?.id ?? null;
    resetPersonForm();
    renderPersonSelect();
    await refreshAll();
  });

  $("#log-date").addEventListener("change", (e) => {
    state.date = e.target.value || todayISO();
    refreshDay();
  });
  $("#prev-day").addEventListener("click", () => {
    state.date = shiftDate(state.date, -1);
    $("#log-date").value = state.date;
    refreshDay();
  });
  $("#next-day").addEventListener("click", () => {
    state.date = shiftDate(state.date, 1);
    $("#log-date").value = state.date;
    refreshDay();
  });
  $("#today-btn").addEventListener("click", () => {
    state.date = todayISO();
    $("#log-date").value = state.date;
    refreshDay();
  });

  $("#food-search").addEventListener("input", (e) => {
    state.selected = null;
    doSearch(e.target.value);
  });
  $("#food-search").addEventListener("focus", (e) => {
    state.selected = null;
    doSearch(e.target.value);
  });
  $("#food-search").addEventListener("keydown", (e) => {
    const n = state.results.length;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!n) return;
      state.activeResult = (state.activeResult + (e.key === "ArrowDown" ? 1 : -1) + n) % n;
      renderResults();
    } else if (e.key === "Enter") {
      e.preventDefault();
      pickResult(state.activeResult >= 0 ? state.activeResult : 0);
    } else if (e.key === "Escape") {
      closeResults();
    }
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".search-wrap")) closeResults();
  });
  $("#search-results").addEventListener("click", (e) => {
    const li = e.target.closest("li[data-idx]");
    if (li) pickResult(Number(li.dataset.idx));
  });

  $("#add-unit").addEventListener("change", updateAddHint);
  $("#add-amount").addEventListener("input", updateAddHint);
  $("#add-btn").addEventListener("click", async () => {
    if (!state.selected) return toast("Pick something first", true);
    if (!state.personId) return toast("Create a person first", true);
    const amount = Number($("#add-amount").value);
    if (!amount || amount <= 0) return toast("Enter an amount", true);
    const unit = $("#add-unit").value;
    try {
      await api("/api/entries", {
        method: "POST",
        body: JSON.stringify({
          person_id: state.personId,
          food_id: state.selected.id,
          date: state.date,
          meal: state.config.meals.includes($("#add-meal").value)
            ? $("#add-meal").value : "snack",
          amount,
          unit,
        }),
      });
      toast(`Logged ${amount} ${unit === "piece" && state.selected.kind === "activity"
        ? "session" : unit} of ${state.selected.name} for ${personName()}`);
      $("#add-amount").value = "";
      updateAddHint();
      await refreshDay();
    } catch (err) {
      toast(err.message, true);
    }
  });

  $("#entry-list").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-entry]");
    if (!btn) return;
    await api(`/api/entries/${btn.dataset.entry}`, { method: "DELETE" });
    toast("Removed");
    await refreshDay();
  });

  $("#food-form").addEventListener("submit", submitFood);
  $("#f-reset").addEventListener("click", resetFoodForm);
  $("#f-base-amount").addEventListener("input", updateBasisNote);
  $("#derive-energy").addEventListener("click", deriveEnergy);
  $("#food-list-search").addEventListener("input", renderFoodTable);
  $("#food-table").addEventListener("click", (e) => {
    const edit = e.target.closest("button[data-edit]");
    const del = e.target.closest("button[data-remove]");
    if (edit) fillFoodForm(edit.dataset.edit);
    if (del) deleteFood(del.dataset.remove);
  });

  $("#w-add").addEventListener("click", () => addWeight().catch((e) => toast(e.message, true)));
  $("#weight-table").addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-del-weight]");
    if (!b) return;
    await api(`/api/weights/${b.dataset.delWeight}`, { method: "DELETE" });
    await Promise.all([loadWeights(), loadMetrics()]);
  });

  $("#fc-refresh").addEventListener("click", () => loadForecast());
  $("#fc-save").addEventListener("click", async () => {
    try {
      await api(`/api/persons/${state.personId}/forecast?save=true`);
      toast("Forecast frozen — it will be scored against reality later");
      await loadScorecard();
    } catch (err) {
      toast(err.message, true);
    }
  });
  $("#forecast-table").addEventListener("click", async (e) => {
    const del = e.target.closest("button[data-del-fc]");
    if (del) {
      await api(`/api/forecasts/${del.dataset.delFc}`, { method: "DELETE" });
      await loadScorecard();
      return;
    }
    const box = e.target.closest("input[data-show-fc]");
    if (!box) return;
    const id = Number(box.dataset.showFc);
    if (box.checked) state.showForecasts.add(id);
    else state.showForecasts.delete(id);
    renderForecast();
  });

  $("#save-targets").addEventListener("click", () => saveTargets());
  $("#save-model").addEventListener("click", () => saveModelSettings());
  $$("a[data-goto]").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    showView(a.dataset.goto);
  }));

  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement.tagName !== "INPUT") {
      e.preventDefault();
      showView("log");
      $("#food-search").focus();
    }
  });
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      renderWeightChart();
      renderForecast();
    }, 200);
  });

  $("#add-meal").innerHTML = state.config.meals
    .map((m) => `<option value="${m}">${m[0].toUpperCase() + m.slice(1)}</option>`).join("");
  $("#w-date").value = todayISO();
  syncUnitButtons();
  setKind("food");
  loadModelSettings();
  showView("log");
  if (state.personId) fillPersonForm(state.personId);
  else resetPersonForm();
}

init().catch((err) => toast(err.message, true));
