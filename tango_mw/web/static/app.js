"use strict";
// ---------------------------------------------------------------- helpers
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false) el.append(c instanceof Node ? c : String(c));
  return el;
}
async function api(method, url, body) {
  const r = await fetch(url, { method, headers: body !== undefined ? { "Content-Type": "application/json" } : {}, body: body !== undefined ? JSON.stringify(body) : undefined });
  const txt = await r.text();
  let data; try { data = txt ? JSON.parse(txt) : null; } catch { data = txt; }
  if (!r.ok) {
    let msg = data && data.detail !== undefined ? data.detail : data;
    if (Array.isArray(msg)) msg = msg.map(e => `${e.loc}: ${e.msg}`).join("\n");
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}
const clone = o => JSON.parse(JSON.stringify(o));
const fmt = (v, d = 3) => (v === null || v === undefined || Number.isNaN(v)) ? "–" : (typeof v === "number" ? (Math.abs(v) < 0.5 * 10 ** -d ? 0 : v).toFixed(d) : String(v));
const deg = r => r * 180 / Math.PI;
function toast(msg, bad) { console[bad ? "error" : "log"](msg); alert(msg); }

// ---------------------------------------------------------------- state
let schema = null, serverCfg = null, draft = null, dirty = false;
let controllers = { controllers: [], errors: {}, files: [] };
let status = null, procedures = null;
let currentTab = "dash";

function markDirty() { dirty = true; $("#savebar").classList.remove("hidden"); }
function clearDirty() { dirty = false; $("#savebar").classList.add("hidden"); }

// ---------------------------------------------------------------- JSON-schema forms
function resolve(s) {
  if (!s) return {};
  if (s.$ref) { const name = s.$ref.split("/").pop(); const { $ref, ...rest } = s; return resolve({ ...schema.$defs[name], ...rest }); }
  if (s.allOf && s.allOf.length === 1) { const { allOf, ...rest } = s; return resolve({ ...allOf[0], ...rest }); }
  return s;
}
function unwrapNullable(s) {
  s = resolve(s);
  if (s.anyOf) {
    const nonNull = s.anyOf.filter(x => x.type !== "null");
    if (nonNull.length === 1) return [{ ...resolve(nonNull[0]), description: s.description, title: s.title }, true];
  }
  return [s, false];
}
function labelFor(key, s) { return h("label", {}, key, s.description ? h("small", {}, s.description) : null); }

function jsonField(obj, key, nullable) {
  const ta = h("textarea", { class: "json", spellcheck: "false" });
  ta.value = obj[key] === null || obj[key] === undefined ? "" : JSON.stringify(obj[key]);
  ta.addEventListener("input", () => {
    const t = ta.value.trim();
    if (!t && nullable) { obj[key] = null; ta.classList.remove("invalid"); markDirty(); return; }
    try { obj[key] = JSON.parse(t); ta.classList.remove("invalid"); markDirty(); } catch { ta.classList.add("invalid"); }
  });
  return ta;
}

function inputFor(s, obj, key, nullable) {
  const v = obj[key];
  if (s.enum) {
    const sel = h("select", {}, nullable ? h("option", { value: "__null" }, "—") : null,
      s.enum.map(e => h("option", { value: JSON.stringify(e), selected: JSON.stringify(e) === JSON.stringify(v) }, String(e))));
    if (v === null && nullable) sel.value = "__null";
    sel.addEventListener("change", () => { obj[key] = sel.value === "__null" ? null : JSON.parse(sel.value); markDirty(); });
    return sel;
  }
  if (s.type === "boolean") {
    if (nullable) {
      const sel = h("select", {}, h("option", { value: "null" }, "— (défaut driver)"), h("option", { value: "true" }, "oui"), h("option", { value: "false" }, "non"));
      sel.value = JSON.stringify(v ?? null);
      sel.addEventListener("change", () => { obj[key] = JSON.parse(sel.value); markDirty(); });
      return sel;
    }
    const cb = h("input", { type: "checkbox" }); cb.checked = !!v;
    cb.addEventListener("change", () => { obj[key] = cb.checked; markDirty(); });
    return cb;
  }
  if (s.type === "integer" || s.type === "number") {
    const inp = h("input", { type: "number", step: s.type === "integer" ? "1" : "any" });
    inp.value = v ?? "";
    inp.addEventListener("input", () => {
      if (inp.value === "") { if (nullable) { obj[key] = null; markDirty(); } return; }
      const n = Number(inp.value); if (Number.isNaN(n)) return;
      obj[key] = s.type === "integer" ? Math.round(n) : n; markDirty();
    });
    return inp;
  }
  if (s.type === "string") {
    const inp = h("input", { type: "text", size: 28 }); inp.value = v ?? "";
    inp.addEventListener("input", () => { obj[key] = (inp.value === "" && nullable) ? null : inp.value; markDirty(); });
    return inp;
  }
  return null;
}

function arrayTable(itemSchema, arr) {
  const props = Object.entries(itemSchema.properties);
  const wrap = h("div");
  const render = () => {
    wrap.innerHTML = "";
    const tbl = h("table", { class: "edit" }, h("tr", {}, props.map(([k]) => h("th", {}, k)), h("th")));
    arr.forEach((row, i) => {
      tbl.append(h("tr", {}, props.map(([k, ps]) => {
        const [inner, nullable] = unwrapNullable(ps);
        return h("td", {}, inputFor(inner, row, k, nullable) || jsonField(row, k, nullable));
      }), h("td", {}, h("button", { class: "ghost", onclick: () => { arr.splice(i, 1); markDirty(); render(); } }, "✕"))));
    });
    wrap.append(tbl, h("button", { class: "ghost", onclick: () => {
      const row = {}; for (const [k, ps] of props) { const [inner] = unwrapNullable(ps); row[k] = ps.default ?? (inner.type === "string" ? "" : inner.type === "boolean" ? false : 0); }
      if ("id" in row && arr.length) row.id = Math.max(...arr.map(r => r.id || 0)) + 1;
      arr.push(row); markDirty(); render();
    } }, "+ ajouter"));
  };
  render();
  return wrap;
}

function buildForm(s, obj, opts = {}) {
  s = resolve(s);
  const box = h("div");
  for (const [key, ps0] of Object.entries(s.properties || {})) {
    if (opts.skip && opts.skip.includes(key)) continue;
    const [ps, nullable] = unwrapNullable(ps0);
    if (ps.type === "object" && ps.properties) {
      if (obj[key] == null) obj[key] = {};
      const det = h("details", { open: opts.openNested ? true : null }, h("summary", {}, key + (ps.description ? ` — ${ps.description}` : "")), buildForm(ps, obj[key], {}));
      box.append(det);
      continue;
    }
    if (ps.type === "array" && ps.items && resolve(ps.items).properties) {
      if (!Array.isArray(obj[key])) obj[key] = [];
      box.append(h("fieldset", {}, h("legend", {}, key), arrayTable(resolve(ps.items), obj[key])));
      continue;
    }
    let input = inputFor(ps, obj, key, nullable);
    if (!input) input = jsonField(obj, key, nullable);
    box.append(h("div", { class: "field" }, labelFor(key, ps0.description ? ps0 : ps), input));
  }
  return box;
}

function renderSection(id, section, opts) {
  const el = $(id); el.innerHTML = "";
  el.append(buildForm(schema.properties[section], draft[section], opts));
}

// ---------------------------------------------------------------- config loading
async function loadConfig(force) {
  const cfg = await api("GET", "/api/config");
  if (!force && dirty) return;
  if (!force && serverCfg && JSON.stringify(cfg) === JSON.stringify(serverCfg)) return;
  serverCfg = cfg; draft = clone(cfg); clearDirty();
  renderForms();
}
function renderForms() {
  renderSection("#form-camera", "camera", { openNested: false });
  renderSection("#form-detector", "detector");
  renderSection("#form-world", "world");
  renderSection("#form-logical", "logical");
  renderSection("#form-ros", "ros");
  renderSection("#form-safety", "safety");
  renderSection("#form-plugins", "plugins");
  renderSection("#form-web", "web");
  renderRobots();
}
async function applyDraft(save) {
  try {
    await api("PUT", "/api/config" + (save ? "?save=true" : ""), draft);
    serverCfg = clone(draft); clearDirty();
    await loadConfig(true);
  } catch (e) { toast("Configuration refusée :\n" + e.message, true); }
}

// ---------------------------------------------------------------- robots tab
function ctrlDesc(name) { return controllers.controllers.find(c => c.name === name); }

function paramInput(name, spec, params) {
  const v = params[name] ?? spec.default;
  if (spec.choices) {
    const sel = h("select", {}, spec.choices.map(c => h("option", { value: JSON.stringify(c), selected: c === v }, String(c))));
    sel.addEventListener("change", () => { params[name] = JSON.parse(sel.value); markDirty(); });
    return sel;
  }
  if (spec.type === "bool") {
    const cb = h("input", { type: "checkbox" }); cb.checked = !!v;
    cb.addEventListener("change", () => { params[name] = cb.checked; markDirty(); }); return cb;
  }
  if (spec.type === "int" || spec.type === "float") {
    const inp = h("input", { type: "number", step: spec.step ?? (spec.type === "int" ? 1 : "any"), min: spec.min ?? undefined, max: spec.max ?? undefined });
    inp.value = v;
    inp.addEventListener("input", () => { if (inp.value !== "") { params[name] = Number(inp.value); markDirty(); } });
    return inp;
  }
  const inp = h("input", { type: "text" }); inp.value = typeof v === "string" ? v : JSON.stringify(v);
  inp.addEventListener("input", () => { let x = inp.value; try { x = JSON.parse(x); } catch {} params[name] = x; markDirty(); });
  return inp;
}

const driveTimers = new Set();
function renderRobots() {
  // re-rendering destroys the "stop" buttons: never leave a manual drive running
  for (const t of driveTimers) clearInterval(t); driveTimers.clear();
  const box = $("#robotcards"); box.innerHTML = "";
  const rs = resolve(schema.$defs.RobotConfig);
  draft.robots.forEach((r, idx) => {
    const desc = ctrlDesc(r.controller);
    const ctrlSel = h("select", {}, controllers.controllers.map(c => h("option", { value: c.name, selected: c.name === r.controller }, c.name)));
    if (!desc) ctrlSel.prepend(h("option", { value: r.controller, selected: true }, r.controller + " (introuvable)"));
    ctrlSel.addEventListener("change", () => { r.controller = ctrlSel.value; r.controller_params = {}; markDirty(); renderRobots(); });
    const params = h("div");
    if (desc) {
      params.append(h("p", { class: "muted small" }, desc.description || ""));
      for (const [pn, spec] of Object.entries(desc.params)) {
        params.append(h("div", { class: "field" }, h("label", {}, pn, spec.description ? h("small", {}, spec.description) : null), paramInput(pn, spec, r.controller_params)));
      }
      params.append(h("button", { class: "ghost small", onclick: () => { r.controller_params = {}; markDirty(); renderRobots(); } }, "Valeurs par défaut"));
    }
    // calibration
    const procSel = h("select", {}, Object.entries(procedures?.procedures || {}).map(([k, d]) => h("option", { value: k, title: d }, `${k} — ${d}`)));
    const optsTa = h("textarea", { class: "json", spellcheck: "false" }); optsTa.value = JSON.stringify(procedures?.default_options || {}, null, 0);
    const jobBox = h("div", { class: "small", "data-job": r.name });
    // manual drive
    const unit = draft.logical.heading_unit;
    const spd = h("input", { type: "number", step: "any", value: "5" });
    const hdg = h("input", { type: "number", step: "any", value: "0" });
    const frameSel = h("select", {}, h("option", { value: "logical" }, "logique (GAMA)"), h("option", { value: "physical" }, "physique (m/s, rad)"));
    let timer = null;
    const driveBtn = h("button", {}, "Envoyer en continu");
    const sendOnce = () => api("POST", `/api/robots/${r.name}/request`, { speed: Number(spd.value), heading: Number(hdg.value), frame: frameSel.value }).catch(e => { stopDrive(); toast(e.message, true); });
    const stopDrive = () => { if (timer) { clearInterval(timer); driveTimers.delete(timer); } timer = null; driveBtn.textContent = "Envoyer en continu"; };
    driveBtn.addEventListener("click", () => {
      if (timer) { stopDrive(); api("POST", `/api/robots/${r.name}/request`, { stop: true }); return; }
      sendOnce(); timer = setInterval(sendOnce, 200); driveTimers.add(timer); driveBtn.textContent = "Arrêter";
    });

    const card = h("div", { class: "card robotcard" },
      h("div", { class: "head" },
        h("h3", {}, r.name, " ", h("span", { class: "muted small" }, r.tag_id >= 0 ? `tag #${r.tag_id}` : "tag non associé (onglet Déploiement)")),
        h("span", { class: "small", "data-rstatus": r.name }),
        h("button", { class: "danger ghost", onclick: () => { if (confirm(`Supprimer ${r.name} ?`)) { draft.robots.splice(idx, 1); markDirty(); renderRobots(); } } }, "Supprimer")),
      h("div", { class: "cols3" },
        h("div", {}, h("h4", {}, "Identité & sortie"), buildForm(rs, r, { skip: ["controller", "controller_params", "calibration"], openNested: true })),
        h("div", {}, h("h4", {}, "Contrôleur"), h("div", { class: "field" }, h("label", {}, "controller"), ctrlSel), params),
        h("div", {}, h("h4", {}, "Calibration"),
          h("div", { class: "row" }, procSel),
          h("details", {}, h("summary", { class: "small" }, "options de la procédure"), optsTa),
          h("div", { class: "row" },
            h("button", { class: "primary", onclick: async () => {
              if (dirty && !confirm("Des modifications non appliquées seront ignorées par la calibration. Continuer ?")) return;
              let options = {}; try { options = JSON.parse(optsTa.value || "{}"); } catch { return toast("options JSON invalides", true); }
              try { await api("POST", `/api/robots/${r.name}/calibrate`, { procedure: procSel.value, options }); } catch (e) { toast(e.message, true); }
            } }, "Lancer"),
            h("button", { class: "ghost", onclick: () => api("DELETE", `/api/robots/${r.name}/calibrate`) }, "Annuler")),
          jobBox,
          h("details", {}, h("summary", { class: "small" }, "paramètres de calibration (modifiables)"), buildForm(schema.$defs.CalibrationData, r.calibration)),
          h("h4", {}, "Pilotage manuel (test)"),
          h("div", { class: "field" }, h("label", {}, "repère"), frameSel),
          h("div", { class: "field" }, h("label", {}, "vitesse"), spd),
          h("div", { class: "field" }, h("label", {}, `heading (${unit} en logique)`), hdg),
          h("div", { class: "row" }, driveBtn))));
    box.append(card);
  });
  updateRobotLive();
}

$("#addrobot").addEventListener("click", () => {
  const used = new Set(draft.robots.map(r => r.tag_id)); let t = 10; while (used.has(t)) t++;
  let n = draft.robots.length + 1; while (draft.robots.some(r => r.name === "robot" + n)) n++;
  draft.robots.push({ name: "robot" + n, tag_id: t, enabled: true, controller: "heading_pid", controller_params: {},
    output: { type: "twist", topic: "/{name}/cmd_vel" }, calibration: clone(serverCfg.robots[0]?.calibration ?? defaultCalibration()) });
  markDirty(); renderRobots();
});
function defaultCalibration() {
  const o = {}; for (const [k, s] of Object.entries(resolve(schema.$defs.CalibrationData).properties)) o[k] = s.default ?? null; return o;
}

function updateRobotLive() {
  if (!status) return;
  for (const el of $$("[data-rstatus]")) {
    const n = el.dataset.rstatus; const c = status.control.robots[n]; const w = status.world.robots[n]; const p = status.presence[n];
    if (!c) { el.textContent = "(non appliqué)"; continue; }
    el.innerHTML = "";
    el.append(h("span", { class: "dot " + (w && w.visible ? "ok" : "bad") }), w && w.visible ? "visible" : "invisible", " · ", c.status);
    if (p && p.known) el.append(" · ", h("span", { class: p.online ? "ok" : "bad" }, p.online ? "joignable" : "aucun abonné"));
  }
  for (const el of $$("[data-job]")) {
    const c = status.control.robots[el.dataset.job]; const j = c && c.calibration_job;
    el.innerHTML = "";
    if (!j) { el.append(h("span", { class: "muted" }, "aucune calibration lancée")); continue; }
    const cls = j.status === "done" ? "ok" : j.status === "running" ? "warn" : "bad";
    el.append(h("div", {}, h("b", { class: cls }, j.status), ` ${j.procedure} — phase ${j.phase}`));
    if (j.error) el.append(h("div", { class: "bad" }, j.error));
    el.append(h("pre", { class: "logs" }, (j.log || []).join("\n")));
  }
}

// ---------------------------------------------------------------- dashboard
function badge(text, cls, title) { return h("span", { class: "badge " + cls, title: title || "" }, text); }
function renderStatus() {
  const s = status; if (!s) return;
  const hl = s.health, w = s.world;
  const b = $("#badges"); b.innerHTML = "";
  b.append(
    badge(`caméra ${hl.camera_ok ? fmt(hl.fps, 1) + " fps" : "HS"}`, hl.camera_ok ? "ok" : "bad", s.camera.error || ""),
    badge(`homographie ${w.valid ? "ok" : "absente"}`, w.valid ? "ok" : "bad"),
    badge(`bus ${s.bus.kind}`, s.bus.kind === "ros2" ? "ok" : "warn", s.bus.error || ""),
    badge(`GAMA ${s.gama.last_request_age !== null && s.gama.last_request_age < 2 ? "actif" : "silencieux"}`, s.gama.last_request_age !== null && s.gama.last_request_age < 2 ? "ok" : "warn"),
  );
  if (Object.keys(s.registry_errors).length) b.append(badge("erreur contrôleur", "bad"));
  const es = $("#estop"); es.classList.toggle("on", hl.estop); es.textContent = hl.estop ? "ARRÊT ACTIF — relâcher" : "ARRÊT D'URGENCE";
  $("#unsaved").classList.toggle("hidden", !s.config_dirty || dirty);
  $("#cfgpath").textContent = s.config_path ? "Fichier : " + s.config_path : "Pas de fichier de configuration (sera créé dans config/config.yaml)";

  const wi = $("#worldinfo"); wi.innerHTML = "";
  const kv = (k, v, cls) => wi.append(h("div", {}, k), h("div", { class: cls || "" }, v));
  kv("Homographie", w.valid ? (w.homography_locked ? "verrouillée" : "continue") : "non calculée", w.valid ? "ok" : "bad");
  kv("Erreur de reprojection", w.reprojection_error !== null ? fmt(w.reprojection_error * 1000, 2) + " mm" : "–");
  const missing = w.world_tags_expected.filter(i => !w.world_tags_visible.includes(i));
  kv("Tags de monde visibles", `${w.world_tags_visible.join(", ") || "aucun"}${missing.length ? " (manquants : " + missing.join(", ") + ")" : ""}`, missing.length ? "warn" : "ok");
  kv("Tags inconnus", w.unknown_tags.join(", ") || "aucun");
  kv("Image", w.image_size.join(" × "));
  kv("Repère logique", `${w.logical.width} × ${w.logical.height}, origine ${w.logical.origin}, heading en ${w.logical.heading_unit}`);
  const gi = $("#gamainfo"); gi.innerHTML = "";
  gi.append(h("div", {}, "Publieurs sur /requests"), h("div", {}, s.gama.publishers_on_requests ?? "n/a (pas de ROS)"),
    h("div", {}, "Dernière requête"), h("div", {}, s.gama.last_request_age !== null ? fmt(s.gama.last_request_age, 1) + " s" : "jamais"));

  const t = $("#robottable"); t.innerHTML = "";
  t.append(h("tr", {}, ["Robot", "Tag", "Vu", "x, y (m)", "θ (°)", "v (m/s)", "Logique (x, y, heading)", "État", "Requête", "Commande", "Réseau"].map(x => h("th", {}, x))));
  for (const [n, c] of Object.entries(s.control.robots)) {
    const r = w.robots[n] || {}; const p = s.presence[n] || {};
    const cmd = c.command || {};
    const cmdTxt = cmd.wheels ? `G ${fmt(cmd.wheels[0], 2)} D ${fmt(cmd.wheels[1], 2)}` : `lin ${fmt(cmd.linear, 2)} ang ${fmt(cmd.angular, 2)}`;
    t.append(h("tr", {},
      h("td", {}, h("b", {}, n)), h("td", {}, r.tag_id),
      h("td", {}, h("span", { class: "dot " + (r.visible ? "ok" : "bad") }), r.visible ? "oui" : (r.age != null ? `il y a ${fmt(r.age, 1)} s` : "jamais")),
      h("td", {}, r.ever_seen ? `${fmt(r.x)}, ${fmt(r.y)}` : "–"), h("td", {}, r.ever_seen ? fmt(deg(r.theta), 1) : "–"),
      h("td", {}, fmt(r.speed, 3)),
      h("td", {}, r.ever_seen ? `${fmt(r.logical.x, 1)}, ${fmt(r.logical.y, 1)}, ${fmt(r.logical.heading, 1)}` : "–"),
      h("td", { class: c.status === "actif" ? "ok" : c.controller_error ? "bad" : "" }, c.status, c.controller_error ? h("pre", { class: "err" }, c.controller_error) : null),
      h("td", {}, c.request ? `${fmt(c.request.speed, 3)} m/s @ ${fmt(deg(c.request.heading), 0)}° (${c.request.source})` : "–"),
      h("td", {}, cmdTxt),
      h("td", {}, p.known ? h("span", { class: p.online ? "ok" : "bad" }, p.online ? p.subscribers.join(", ") : "aucun abonné") : h("span", { class: "muted" }, "n/a"))));
  }
  updateRobotLive();
}
$("#estop").addEventListener("click", () => api("POST", "/api/estop", { on: !(status && status.health.estop) }));

// ---------------------------------------------------------------- controllers tab
async function loadControllers() {
  controllers = await api("GET", "/api/controllers");
  const list = $("#ctrllist"); list.innerHTML = "";
  for (const c of controllers.controllers) {
    list.append(h("details", {}, h("summary", {}, c.name, " ", h("span", { class: "muted small" }, c.origin === "builtin" ? "intégré" : c.origin)),
      h("p", { class: "small" }, c.description),
      h("table", {}, h("tr", {}, h("th", {}, "paramètre"), h("th", {}, "défaut"), h("th", {}, "bornes"), h("th", {}, "description")),
        Object.entries(c.params).map(([k, p]) => h("tr", {}, h("td", {}, k), h("td", {}, JSON.stringify(p.default)),
          h("td", {}, p.choices ? p.choices.join(" | ") : `${p.min ?? ""} … ${p.max ?? ""}`), h("td", {}, p.description))))));
  }
  const errs = $("#ctrlerrors"); errs.innerHTML = "";
  const e = Object.entries(controllers.errors);
  if (!e.length) errs.textContent = "aucune";
  for (const [f, tb] of e) errs.append(h("div", {}, h("b", {}, f), h("pre", { class: "err" }, tb)));
  const sel = $("#ctrlfiles"); const prev = sel.value; sel.innerHTML = "";
  for (const f of controllers.files) { const name = f.split(/[\\/]/).pop(); sel.append(h("option", { value: name, selected: name === prev }, name)); }
  for (const b of ["#ctrlopen", "#ctrlnew", "#ctrlsave"]) $(b).disabled = !controllers.allow_code_edit;
  if (draft) renderRobots();
}
let openFile = null;
$("#ctrlopen").addEventListener("click", async () => {
  const f = $("#ctrlfiles").value; if (!f) return;
  try { const r = await api("GET", "/api/controllers/source/" + f); openFile = f; $("#ctrlcode").value = r.code; $("#ctrlmsg").textContent = f; } catch (e) { toast(e.message, true); }
});
$("#ctrlnew").addEventListener("click", async () => {
  const n = $("#ctrlnewname").value.trim(); if (!n) return;
  try { const r = await api("POST", "/api/controllers/new", { name: n }); await loadControllers(); $("#ctrlfiles").value = r.filename; $("#ctrlopen").click(); } catch (e) { toast(e.message, true); }
});
$("#ctrlsave").addEventListener("click", async () => {
  if (!openFile) return toast("Aucun fichier ouvert", true);
  try {
    const r = await api("PUT", "/api/controllers/source/" + openFile, { code: $("#ctrlcode").value });
    $("#ctrlmsg").innerHTML = ""; $("#ctrlmsg").append(r.errors ? h("span", { class: "bad" }, "enregistré mais erreur au chargement (voir à gauche)") : h("span", { class: "ok" }, "enregistré et rechargé ✓"));
    await loadControllers();
  } catch (e) { $("#ctrlmsg").innerHTML = ""; $("#ctrlmsg").append(h("span", { class: "bad" }, e.message)); }
});
$("#ctrlcode").addEventListener("keydown", ev => {
  if (ev.key === "Tab") { ev.preventDefault(); const t = ev.target, s = t.selectionStart; t.setRangeText("    ", s, t.selectionEnd, "end"); }
  if ((ev.ctrlKey || ev.metaKey) && ev.key === "s") { ev.preventDefault(); $("#ctrlsave").click(); }
});
$("#ctrlreload").addEventListener("click", async () => { await api("POST", "/api/controllers/reload"); await loadControllers(); });

// ---------------------------------------------------------------- network tab
async function loadNetwork() {
  const n = await api("GET", "/api/network");
  const info = n.info || {};
  const env = $("#rosenv"); env.innerHTML = "";
  const kv = (k, v, cls) => env.append(h("div", {}, k), h("div", { class: cls || "" }, v ?? "–"));
  kv("Bus", n.bus, n.bus === "ros2" ? "ok" : "warn");
  if (n.bus_error) kv("Erreur", n.bus_error, "bad");
  if (!info.available) kv("ROS 2", info.reason || "indisponible (rclpy non trouvé — sourcez l'installation ROS 2)", "warn");
  kv("Nœud", info.node); kv("RMW", info.rmw); kv("Domain ID", info.domain_id);
  for (const [k, v] of Object.entries(info.env || {})) kv(k, v === null ? "(non défini)" : v, v === null ? "muted" : "");

  const pres = $("#presence"); pres.innerHTML = "";
  const tbl = h("table", {}, h("tr", {}, ["Robot", "Topic de commande", "Abonnés", "Caméra"].map(x => h("th", {}, x))));
  for (const r of serverCfg.robots) {
    const p = status?.presence?.[r.name] || {}; const w = status?.world?.robots?.[r.name] || {};
    tbl.append(h("tr", {}, h("td", {}, r.name), h("td", {}, h("code", {}, p.topic || r.output.topic.replace("{name}", r.name))),
      h("td", {}, p.known ? h("span", { class: p.online ? "ok" : "bad" }, h("span", { class: "dot " + (p.online ? "ok" : "bad") }), p.online ? p.subscribers.join(", ") : "aucun nœud n'écoute") : h("span", { class: "muted" }, "n/a")),
      h("td", {}, h("span", { class: "dot " + (w.visible ? "ok" : "bad") }), w.visible ? "tag visible" : "tag non visible")));
  }
  pres.append(tbl);

  const topics = info.topics || [];
  const byName = Object.fromEntries(topics.map(t => [t.name, t]));
  const tm = $("#topicmap"); tm.innerHTML = "";
  tm.append(h("tr", {}, ["Topic", "Type", "Sens", "Rôle", "En face"].map(x => h("th", {}, x))));
  for (const r of n.topic_map) {
    const g = byName[r.topic]; let other = h("span", { class: "muted" }, info.available ? "absent du graphe" : "n/a");
    if (g) {
      const cnt = r.dir === "pub" ? g.subscribers : g.publishers;
      const label = r.dir === "pub" ? `${g.subscribers} abonné(s)` : `${g.publishers} publieur(s)`;
      other = h("span", { class: cnt > 0 ? "ok" : "warn" }, label);
    }
    tm.append(h("tr", {}, h("td", {}, h("code", {}, r.topic)), h("td", {}, r.type), h("td", {}, r.dir === "pub" ? "→ publie" : "← écoute"), h("td", {}, r.doc), h("td", {}, other)));
  }
  $("#nodecount").textContent = info.nodes ? `(${info.nodes.length})` : "";
  const nl = $("#nodes"); nl.innerHTML = ""; (info.nodes || []).forEach(x => nl.append(h("li", {}, x)));
  const filt = $("#topicfilter").value.toLowerCase();
  const tt = $("#topics"); tt.innerHTML = "";
  tt.append(h("tr", {}, ["Topic", "Type", "Pub", "Sub"].map(x => h("th", {}, x))));
  topics.filter(t => !filt || t.name.toLowerCase().includes(filt)).forEach(t => tt.append(h("tr", {}, h("td", {}, h("code", {}, t.name)), h("td", { class: "small" }, t.types.join(", ")), h("td", {}, t.publishers), h("td", {}, t.subscribers))));
}
$("#topicfilter").addEventListener("input", () => loadNetwork());

// ---------------------------------------------------------------- deployment tab
let lastScan = null;
function identOptions() {
  const o = { motion: $("#identmotion").value };
  if ($("#identraw").value !== "") o.raw = Number($("#identraw").value);
  return o;
}
function prefillWorldDims() {
  if (!draft) return;
  if (!$("#wwidth").value) {
    const xs = draft.world.tags.map(t => t.x), ys = draft.world.tags.map(t => t.y);
    if (xs.length) { $("#wwidth").value = Math.max(...xs) - Math.min(...xs); $("#wheight").value = Math.max(...ys) - Math.min(...ys); }
  }
  if (!$("#disctpl").value) $("#disctpl").value = draft.robots[0]?.output.topic || "/{name}/cmd_vel";
}
function nextUnassigned() { return Math.min(0, ...draft.robots.map(r => r.tag_id)) - 1; }
function newRobot(name, tag) {
  let n = name, k = 2; while (draft.robots.some(r => r.name === n)) n = `${name}_${k++}`;
  const tpl = draft.robots[0]?.output || { type: "twist", topic: "/{name}/cmd_vel" };
  return { name: n, tag_id: tag, enabled: true, controller: draft.robots[0]?.controller || "heading_pid", controller_params: {},
    output: clone(tpl), calibration: defaultCalibration() };
}
function renderScan() {
  const t = $("#scantable"); t.innerHTML = "";
  if (!lastScan) return;
  $("#scaninfo").textContent = `${lastScan.tags.length} tag(s) sur ${lastScan.frames} image(s)` + (lastScan.homography_valid ? "" : " — homographie non valide");
  t.append(h("tr", {}, ["Tag", "Rôle actuel", "Vu", "Image (px)", "Monde (m)", "Action (brouillon)"].map(x => h("th", {}, x))));
  for (const g of lastScan.tags) {
    const inDraftWorld = draft.world.tags.some(w => w.id === g.id);
    const draftRobot = draft.robots.find(r => r.tag_id === g.id);
    const role = g.role === "world" ? h("span", { class: "ok" }, "repère monde")
      : g.role === "robot" ? h("span", { class: "ok" }, "robot " + g.robot)
      : inDraftWorld ? h("span", { class: "warn" }, "repère monde (brouillon)")
      : draftRobot ? h("span", { class: "warn" }, `robot ${draftRobot.name} (brouillon)`)
      : h("span", { class: "warn" }, "inconnu");
    let action = "";
    if (g.role === "unknown" && !inDraftWorld && !draftRobot) {
      const sel = h("select", {}, h("option", { value: "" }, "assigner à…"),
        draft.robots.map(r => h("option", { value: "r:" + r.name }, "robot " + r.name + (r.tag_id < 0 ? " (non associé)" : ` (#${r.tag_id})`))),
        h("option", { value: "new" }, "nouveau robot"),
        g.world ? h("option", { value: "world" }, "repère de monde (position mesurée)") : null);
      sel.addEventListener("change", () => {
        const v = sel.value; if (!v) return;
        if (v === "world") draft.world.tags.push({ id: g.id, x: +g.world[0].toFixed(4), y: +g.world[1].toFixed(4) });
        else if (v === "new") draft.robots.push(newRobot("robot_" + g.id, g.id));
        else draft.robots.find(x => x.name === v.slice(2)).tag_id = g.id;
        markDirty(); renderForms(); renderScan();
      });
      action = sel;
    }
    t.append(h("tr", {}, h("td", {}, h("b", {}, "#" + g.id)), h("td", {}, role), h("td", {}, Math.round(g.seen * 100) + " %"),
      h("td", {}, `${Math.round(g.u)}, ${Math.round(g.v)}`), h("td", {}, g.world ? `${fmt(g.world[0])}, ${fmt(g.world[1])}` : "–"), h("td", {}, action)));
  }
}
$("#scanbtn").addEventListener("click", async () => {
  $("#scaninfo").textContent = "scan…";
  try { lastScan = await api("GET", "/api/setup/scan?duration=" + $("#scandur").value); renderScan(); } catch (e) { toast(e.message, true); }
});
$("#proposeworld").addEventListener("click", async () => {
  const out = $("#worldproposal"); out.textContent = "scan…";
  try {
    const r = await api("POST", "/api/setup/propose_world", { width: Number($("#wwidth").value), height: Number($("#wheight").value),
      rotation: Number($("#wrot").value), mirror: $("#wmirror").checked, duration: Number($("#scandur").value) });
    lastScan = r.scan; renderScan();
    out.innerHTML = "";
    const keepBox = h("input", { type: "checkbox" });
    out.append(h("table", {}, h("tr", {}, h("th", {}, "Tag"), h("th", {}, "x (m)"), h("th", {}, "y (m)")),
      r.tags.map(t => h("tr", {}, h("td", {}, "#" + t.id), h("td", {}, t.x), h("td", {}, t.y)))),
      h("label", { class: "small" }, keepBox, " conserver aussi les autres repères existants"),
      h("div", { class: "row" }, h("button", { class: "primary", onclick: () => {
        const keep = keepBox.checked ? draft.world.tags.filter(t => !r.tags.some(n => n.id === t.id)) : [];
        draft.world.tags = [...r.tags, ...keep];
        draft.robots.forEach(rb => { if (r.tags.some(t => t.id === rb.tag_id)) rb.tag_id = nextUnassigned(); });
        markDirty(); renderForms(); renderScan();
        out.append(h("div", { class: "ok small" }, "Proposition copiée dans le brouillon — vérifiez puis cliquez « Appliquer »."));
      } }, "Utiliser (brouillon)")));
  } catch (e) { out.innerHTML = ""; out.append(h("div", { class: "bad" }, e.message)); }
});
$("#measureextra").addEventListener("click", async () => {
  try {
    const r = await api("POST", "/api/setup/measure_tags", { duration: Number($("#scandur").value) });
    lastScan = r.scan; renderScan();
    const cand = r.tags.filter(t => !draft.world.tags.some(w => w.id === t.id) && !draft.robots.some(rb => rb.tag_id === t.id));
    const info = $("#measureinfo"); info.innerHTML = "";
    if (!cand.length) { info.textContent = "aucun tag inconnu à ajouter"; return; }
    // unchecked by default: an unknown tag may be on a robot that is not configured yet
    const boxes = cand.map(t => [h("input", { type: "checkbox" }), t]);
    info.append(h("div", { class: "warn" }, "Cochez uniquement les tags fixés au sol (pas ceux des robots) :"),
      h("table", {}, boxes.map(([cb, t]) => h("tr", {}, h("td", {}, cb), h("td", {}, "#" + t.id), h("td", {}, `x ${t.x}`), h("td", {}, `y ${t.y}`)))),
      h("button", { class: "ghost", onclick: () => {
        const add = boxes.filter(([cb]) => cb.checked).map(([, t]) => t);
        draft.world.tags.push(...add);
        if (add.length) { markDirty(); renderForms(); renderScan(); }
        info.textContent = add.length ? `${add.length} repère(s) ajouté(s) au brouillon : ${add.map(t => "#" + t.id).join(", ")}` : "rien ajouté";
      } }, "Ajouter la sélection (brouillon)"));
  } catch (e) { toast(e.message, true); }
});
$("#robotsfromtags").addEventListener("click", () => {
  if (!lastScan) return toast("Lancez d'abord un scan", true);
  const unk = lastScan.tags.filter(t => t.role === "unknown" && !draft.robots.some(r => r.tag_id === t.id) && !draft.world.tags.some(w => w.id === t.id));
  unk.forEach(t => draft.robots.push(newRobot("robot_" + t.id, t.id)));
  if (unk.length) { markDirty(); renderForms(); renderScan(); }
  toast(unk.length ? `${unk.length} robot(s) ajouté(s) au brouillon (topics à vérifier dans l'onglet Robots).` : "Aucun tag inconnu.");
});
$("#discover").addEventListener("click", async () => {
  const out = $("#discoverout"); out.textContent = "recherche…";
  const tpl = $("#disctpl").value || "/{name}/cmd_vel";
  try {
    const r = await api("GET", "/api/setup/discover_robots?template=" + encodeURIComponent(tpl));
    out.innerHTML = "";
    if (!r.available) { out.append(h("div", { class: "warn small" }, r.reason)); return; }
    if (!r.robots.length) { out.append(h("div", { class: "muted small" }, `aucun topic ne correspond à ${r.template}`)); return; }
    const boxes = [];
    out.append(h("table", {}, h("tr", {}, ["", "Robot", "Topic", "Type", "Abonnés"].map(x => h("th", {}, x))),
      r.robots.map(x => {
        const known = x.configured || draft.robots.some(d => d.name === x.name);
        const cb = h("input", { type: "checkbox" }); cb.checked = !known; cb.disabled = known; boxes.push([cb, x]);
        return h("tr", {}, h("td", {}, cb), h("td", {}, x.name, known ? h("span", { class: "muted small" }, " (déjà configuré)") : ""),
          h("td", {}, h("code", {}, x.topic)), h("td", { class: "small" }, x.types.join(", ")),
          h("td", { class: x.subscribers ? "ok" : "warn" }, x.subscribers));
      })),
      h("button", { class: "ghost", onclick: () => {
        let n = 0;
        for (const [cb, x] of boxes) if (cb.checked && !draft.robots.some(d => d.name === x.name)) {
          const rb = newRobot(x.name, nextUnassigned()); rb.output.topic = tpl;
          rb.output.type = x.types.some(t => t.endsWith("Float64MultiArray")) ? "wheels" : x.types.some(t => t.endsWith("/String")) ? "json" : "twist";
          draft.robots.push(rb); n++;
        }
        if (n) { markDirty(); renderForms(); }
        toast(`${n} robot(s) ajouté(s) au brouillon. Cliquez « Appliquer » puis « Identifier » pour associer leurs tags.`);
      } }, "Ajouter la sélection (brouillon)"));
  } catch (e) { out.textContent = e.message; }
});
function renderIdentTable() {
  const t = $("#identtable"); if (!serverCfg) return;
  if (t.contains(document.activeElement)) return; // don't disturb manual typing
  t.innerHTML = "";
  t.append(h("tr", {}, ["Robot", "Tag", "Vu", "", "Saisie manuelle", "Dernière identification"].map(x => h("th", {}, x))));
  for (const r of serverCfg.robots) {
    const c = status?.control?.robots?.[r.name]; const w = status?.world?.robots?.[r.name];
    const j = c?.calibration_job; const isIdent = j && j.procedure === "identify";
    const inp = h("input", { type: "number", style: "width:70px" }); inp.value = r.tag_id >= 0 ? r.tag_id : "";
    let last = "";
    if (isIdent) last = h("span", { class: j.status === "done" ? "ok" : j.status === "running" ? "warn" : "bad" },
      j.status === "running" ? `en cours (${j.phase})` : j.status === "done" ? `#${j.result.tag_id} ✓` : (j.error || j.status));
    t.append(h("tr", {},
      h("td", {}, h("b", {}, r.name)),
      h("td", {}, r.tag_id >= 0 ? "#" + r.tag_id : h("span", { class: "warn" }, "non associé")),
      h("td", {}, h("span", { class: "dot " + (w?.visible ? "ok" : "bad") })),
      h("td", {}, h("button", { class: "ghost", onclick: () => startIdentify(r.name).catch(e => toast(e.message, true)) }, "Identifier")),
      h("td", {}, inp, " ", h("button", { class: "ghost", onclick: async () => {
        if (inp.value === "") return;
        try { await api("POST", `/api/robots/${r.name}/assign_tag`, { tag_id: Number(inp.value) }); await loadConfig(true); } catch (e) { toast(e.message, true); }
      } }, "OK")),
      h("td", { class: "small" }, last)));
  }
}
async function startIdentify(name) {
  if (dirty) throw new Error("Appliquez d'abord les modifications en cours.");
  await api("POST", `/api/robots/${name}/identify`, { options: identOptions() });
  waitJob(name).then(() => loadConfig(false)).catch(() => {});
}
async function waitJob(name) {
  for (;;) {
    await new Promise(r => setTimeout(r, 300));
    const s = await api("GET", "/api/status"); const j = s.control.robots[name]?.calibration_job;
    if (j && j.procedure === "identify" && j.status !== "running") return j;
  }
}
$("#identifyall").addEventListener("click", async () => {
  if (dirty) return toast("Appliquez d'abord les modifications en cours.", true);
  const todo = serverCfg.robots.filter(r => r.tag_id < 0 || !(status?.world?.robots?.[r.name]?.visible));
  if (!todo.length) return toast("Tous les robots ont un tag visible.");
  const btn = $("#identifyall"); btn.disabled = true;
  const report = [];
  try {
    for (const r of todo) {
      try { await startIdentify(r.name); const j = await waitJob(r.name); report.push(`${r.name} : ${j.status === "done" ? "#" + j.result.tag_id : j.error}`); }
      catch (e) { report.push(`${r.name} : ${e.message}`); }
    }
  } finally { btn.disabled = false; await loadConfig(true); }
  toast("Identification terminée :\n" + report.join("\n"));
});

// ---------------------------------------------------------------- misc
$("#probe").addEventListener("click", async () => {
  $("#probeout").textContent = "recherche…";
  const cams = await api("GET", "/api/cameras");
  $("#probeout").textContent = cams.length ? cams.map(c => `#${c.index} (${c.width}×${c.height})`).join(", ") + " — renseignez 'source' avec l'index" : "aucune caméra locale trouvée";
});
$("#relock").addEventListener("click", () => api("POST", "/api/world/relock"));
const updTag = () => { $("#taglink").href = `/api/tags/${$("#tagid").value}.png?size=${$("#tagsize").value}`; };
$("#tagid").addEventListener("input", updTag); $("#tagsize").addEventListener("input", updTag); updTag();
$("#apply").addEventListener("click", () => applyDraft(false));
$("#applysave").addEventListener("click", () => applyDraft(true));
$("#revert").addEventListener("click", async () => {
  await loadConfig(true);
  if (typeof renderScan === "function") { renderScan(); $("#measureinfo").textContent = ""; $("#worldproposal").innerHTML = ""; }
});
$("#savecfg").addEventListener("click", async () => { await api("POST", "/api/config/save"); });

let lastLog = 0;
async function pollLogs() {
  try {
    const recs = await api("GET", "/api/logs?after=" + lastLog);
    const box = $("#logs"); const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 5;
    for (const r of recs) {
      lastLog = r.id;
      box.append(h("div", { class: r.level }, `${new Date(r.t * 1000).toLocaleTimeString()} ${r.level.padEnd(7)} ${r.name}: ${r.msg}`));
    }
    while (box.childElementCount > 500) box.firstChild.remove();
    if (atBottom) box.scrollTop = box.scrollHeight;
  } catch {}
}

// tabs + streams (only one MJPEG stream open at a time)
function setTab(name) {
  currentTab = name;
  $$("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach(t => t.classList.toggle("active", t.id === "tab-" + name));
  $("#stream").src = name === "dash" ? "/api/stream.mjpg" : "";
  $("#stream2").src = name === "camera" ? "/api/stream.mjpg" : "";
  $("#stream3").src = name === "deploy" ? "/api/stream.mjpg" : "";
  if (name === "deploy") { prefillWorldDims(); renderIdentTable(); }
  if (name === "network") loadNetwork().catch(() => {});
  if (name === "controllers") loadControllers().catch(() => {});
  try { localStorage.setItem("tab", name); } catch {}
}
$$("#tabs button").forEach(b => b.addEventListener("click", () => setTab(b.dataset.tab)));

async function pollStatus() {
  try { status = await api("GET", "/api/status"); renderStatus(); if (currentTab === "deploy") renderIdentTable(); } catch {}
}

(async function init() {
  schema = await api("GET", "/api/config/schema");
  procedures = await api("GET", "/api/calibration/procedures");
  controllers = await api("GET", "/api/controllers");
  await loadConfig(true);
  await loadControllers();
  let t = "dash"; try { t = localStorage.getItem("tab") || "dash"; } catch {}
  setTab(t);
  pollStatus(); setInterval(pollStatus, 500);
  setInterval(pollLogs, 1000); pollLogs();
  setInterval(() => { if (!dirty && !document.activeElement?.matches("input, textarea, select")) loadConfig(false).catch(() => {}); }, 3000);
  setInterval(() => { if (currentTab === "network") loadNetwork().catch(() => {}); }, 2000);
})();
