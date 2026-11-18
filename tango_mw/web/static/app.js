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
const pretty = k => k.replace(/_/g, " ");
function labelFor(key, s) { return h("label", { title: key }, pretty(key), s.description ? h("small", {}, s.description) : null); }

// Fields shown by default; everything else is folded under "Advanced settings".
const BASIC = {
  camera: ["source", "width", "height", "fps", "rotate", "flip"],
  detector: ["family"],
  world: ["tags", "homography_mode"],
  logical: ["width", "height", "origin", "heading_unit"],
  ros: ["bus", "domain_id", "namespace"],
  safety: ["request_timeout", "tag_lost_timeout", "stop_out_of_bounds"],
  goto: ["speed_fraction", "clearance", "timeout"],
  plugins: ["dirs", "auto_reload"],
  web: ["host", "port", "allow_code_edit"],
  robot: ["name", "tag_id", "enabled", "output"],
};

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
      const sel = h("select", {}, h("option", { value: "null" }, "— (driver default)"), h("option", { value: "true" }, "yes"), h("option", { value: "false" }, "no"));
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
    } }, "+ add"));
  };
  render();
  return wrap;
}

function buildForm(s, obj, opts = {}) {
  s = resolve(s);
  const outer = h("div");
  const adv = h("div");
  for (const [key, ps0] of Object.entries(s.properties || {})) {
    if (opts.skip && opts.skip.includes(key)) continue;
    const box = opts.basic && !opts.basic.includes(key) ? adv : outer;
    const [ps, nullable] = unwrapNullable(ps0);
    if (ps.type === "object" && ps.properties) {
      if (obj[key] == null) obj[key] = {};
      const det = h("details", { open: opts.openNested ? true : null }, h("summary", {}, pretty(key) + (ps.description ? ` — ${ps.description}` : "")), buildForm(ps, obj[key], {}));
      box.append(det);
      continue;
    }
    if (ps.type === "array" && ps.items && resolve(ps.items).properties) {
      if (!Array.isArray(obj[key])) obj[key] = [];
      box.append(h("fieldset", {}, h("legend", {}, pretty(key)), arrayTable(resolve(ps.items), obj[key])));
      continue;
    }
    let input = inputFor(ps, obj, key, nullable);
    if (!input) input = jsonField(obj, key, nullable);
    box.append(h("div", { class: "field" }, labelFor(key, ps0.description ? ps0 : ps), input));
  }
  if (adv.childElementCount) outer.append(h("details", { class: "advanced" }, h("summary", {}, `Advanced settings (${adv.childElementCount})`), adv));
  return outer;
}

function renderSection(id, section, opts = {}) {
  const el = $(id); el.innerHTML = "";
  el.append(buildForm(schema.properties[section], draft[section], { basic: BASIC[section], ...opts }));
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
  renderSection("#form-goto", "goto");
  renderRobots();
  renderPhoneControls();
  updateTagLinks();
}
async function applyDraft(save) {
  try {
    await api("PUT", "/api/config" + (save ? "?save=true" : ""), draft);
    serverCfg = clone(draft); clearDirty();
    await loadConfig(true);
  } catch (e) { toast("Configuration rejected:\n" + e.message, true); }
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
    if (!desc) ctrlSel.prepend(h("option", { value: r.controller, selected: true }, r.controller + " (not found)"));
    ctrlSel.addEventListener("change", () => { r.controller = ctrlSel.value; r.controller_params = {}; markDirty(); renderRobots(); });
    const params = h("div");
    if (desc) {
      params.append(h("p", { class: "muted small" }, desc.description || ""));
      for (const [pn, spec] of Object.entries(desc.params)) {
        params.append(h("div", { class: "field" }, h("label", {}, pn, spec.description ? h("small", {}, spec.description) : null), paramInput(pn, spec, r.controller_params)));
      }
      params.append(h("button", { class: "ghost small", onclick: () => { r.controller_params = {}; markDirty(); renderRobots(); } }, "Default values"));
    }
    // calibration
    const procSel = h("select", {}, Object.entries(procedures?.procedures || {}).map(([k, d]) => h("option", { value: k, title: d, selected: k === "full" }, `${k} — ${d}`)));
    const optsTa = h("textarea", { class: "json", spellcheck: "false" }); optsTa.value = JSON.stringify(procedures?.default_options || {}, null, 0);
    const jobBox = h("div", { class: "small", "data-job": r.name });
    // manual drive
    const unit = draft.logical.heading_unit;
    const spd = h("input", { type: "number", step: "any", value: "5" });
    const hdg = h("input", { type: "number", step: "any", value: "0" });
    const frameSel = h("select", {}, h("option", { value: "logical" }, "logical (GAMA)"), h("option", { value: "physical" }, "physical (m/s, rad)"));
    let timer = null;
    const driveBtn = h("button", {}, "Send continuously");
    const sendOnce = () => api("POST", `/api/robots/${r.name}/request`, { speed: Number(spd.value), heading: Number(hdg.value), frame: frameSel.value }).catch(e => { stopDrive(); toast(e.message, true); });
    const stopDrive = () => { if (timer) { clearInterval(timer); driveTimers.delete(timer); } timer = null; driveBtn.textContent = "Send continuously"; };
    driveBtn.addEventListener("click", () => {
      if (timer) { stopDrive(); api("POST", `/api/robots/${r.name}/request`, { stop: true }); return; }
      sendOnce(); timer = setInterval(sendOnce, 200); driveTimers.add(timer); driveBtn.textContent = "Stop";
    });

    const card = h("div", { class: "card robotcard" },
      h("div", { class: "head" },
        h("h3", {}, r.name, " ", h("span", { class: "muted small" }, r.tag_id >= 0 ? `tag #${r.tag_id}` : "no tag assigned (Deployment tab)")),
        h("span", { class: "small", "data-rstatus": r.name }),
        h("button", { class: "danger ghost", onclick: () => { if (confirm(`Delete ${r.name}?`)) { draft.robots.splice(idx, 1); markDirty(); renderRobots(); } } }, "Delete")),
      h("div", { class: "cols3" },
        h("div", {}, h("h4", {}, "Identity & output"), buildForm(rs, r, { skip: ["controller", "controller_params", "calibration", "home"], openNested: true, basic: BASIC.robot }),
          tagHeightEditor(r), homeEditor(r)),
        h("div", {}, h("h4", {}, "Controller"), h("div", { class: "field" }, h("label", {}, "controller"), ctrlSel), params),
        h("div", {}, h("h4", {}, "Calibration"),
          h("div", { class: "row" }, procSel),
          h("details", {}, h("summary", { class: "small" }, "procedure options"), optsTa),
          h("div", { class: "row" },
            h("button", { class: "primary", onclick: async () => {
              if (dirty && !confirm("Unapplied changes will be ignored by the calibration. Continue?")) return;
              let options = {}; try { options = JSON.parse(optsTa.value || "{}"); } catch { return toast("invalid JSON options", true); }
              try { await api("POST", `/api/robots/${r.name}/calibrate`, { procedure: procSel.value, options }); } catch (e) { toast(e.message, true); }
            } }, "Start"),
            h("button", { class: "ghost", onclick: () => api("DELETE", `/api/robots/${r.name}/calibrate`) }, "Cancel")),
          jobBox,
          h("details", {}, h("summary", { class: "small" }, "calibration parameters (editable)"), buildForm(schema.$defs.CalibrationData, r.calibration)),
          h("h4", {}, "Manual driving (test)"),
          h("div", { class: "field" }, h("label", {}, "frame"), frameSel),
          h("div", { class: "field" }, h("label", {}, "speed"), spd),
          h("div", { class: "field" }, h("label", {}, `heading (${unit} in logical)`), hdg),
          h("div", { class: "row" }, driveBtn),
          gotoEditor(r))));
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
// height of the robot's tag above the floor: needed to correct the parallax (oblique cameras)
function tagHeightEditor(r) {
  const i = h("input", { type: "number", step: "0.5", min: "0" });
  i.value = r.calibration && r.calibration.tag_height ? +(r.calibration.tag_height * 100).toFixed(1) : "";
  i.addEventListener("input", () => { r.calibration.tag_height = i.value === "" ? 0 : Number(i.value) / 100; markDirty(); });
  return h("div", { class: "field" },
    h("label", {}, "Tag height (cm) ", h("span", { class: "help", "data-help": "Height of the tag above the floor, measured with a ruler. Seen at an angle a raised tag appears away from its robot (several cm, much more with a camera at head height); the middleware corrects it with this height and the camera position it estimates from the boundary tags. 0 = tag on the floor." }, "?"),
      h("small", {}, "from the floor to the tag")), i);
}

// home position (GAMA units) and go-to, in the robot card
function homeEditor(r) {
  const unit = draft.logical.heading_unit;
  const box = h("div", {}, h("h4", {}, "Home position ", h("span", { class: "help", "data-help": "Where the robot starts a GAMA run, in GAMA coordinates. 'Place robots at home' (Start tab) or {\"place\": \"home\"} from GAMA drives every robot there, one after the other. Easiest: drive or put the robot where it should start, then click 'Use current position'." }, "?")));
  const mk = (k, v) => { const i = h("input", { type: "number", step: "any", placeholder: k }); i.value = v ?? ""; return i; };
  const hx = mk("x", r.home?.x), hy = mk("y", r.home?.y), hh = mk("heading", r.home?.heading);
  const upd = () => {
    if (hx.value === "" || hy.value === "") { r.home = null; } else r.home = { x: Number(hx.value), y: Number(hy.value), heading: hh.value === "" ? null : Number(hh.value) };
    markDirty();
  };
  [hx, hy, hh].forEach(i => i.addEventListener("input", upd));
  box.append(h("div", { class: "row" }, h("label", { class: "small" }, "x ", hx), h("label", { class: "small" }, "y ", hy), h("label", { class: "small" }, `heading (${unit}) `, hh)),
    h("div", { class: "row" },
      h("button", { class: "ghost", onclick: async () => {
        if (dirty) return toast("Apply the pending changes first.", true);
        try { await api("POST", `/api/robots/${r.name}/home_here`); await loadConfig(true); } catch (e) { toast(e.message, true); }
      } }, "Use current position"),
      h("button", { class: "ghost", onclick: () => { hx.value = hy.value = hh.value = ""; upd(); } }, "Clear")));
  return box;
}
function gotoEditor(r) {
  const unit = draft.logical.heading_unit;
  const mk = k => h("input", { type: "number", step: "any", placeholder: k });
  const gx = mk("x"), gy = mk("y"), gh = mk("heading");
  return h("div", {}, h("h4", {}, "Go to position (GAMA units) ", h("span", { class: "help", "data-help": "Drives the robot to a point with its calibration, then stops. Leave heading empty for any final heading. GAMA can do the same with {\"robot\": \"" + r.name + "\", \"goto\": {\"x\": …, \"y\": …}}." }, "?")),
    h("div", { class: "row" }, gx, gy, h("label", { class: "small" }, `(${unit}) `, gh),
      h("button", { onclick: async () => {
        if (gx.value === "" || gy.value === "") return toast("x and y are required", true);
        const body = { x: Number(gx.value), y: Number(gy.value) }; if (gh.value !== "") body.heading = Number(gh.value);
        try { await api("POST", `/api/robots/${r.name}/goto`, body); } catch (e) { toast(e.message, true); }
      } }, "Go"),
      h("button", { class: "ghost", onclick: () => api("DELETE", `/api/robots/${r.name}/goto`) }, "Cancel")));
}

function defaultCalibration() {
  const o = {}; for (const [k, s] of Object.entries(resolve(schema.$defs.CalibrationData).properties)) o[k] = s.default ?? null; return o;
}

function updateRobotLive() {
  if (!status) return;
  for (const el of $$("[data-rstatus]")) {
    const n = el.dataset.rstatus; const c = status.control.robots[n]; const w = status.world.robots[n]; const p = status.presence[n];
    if (!c) { el.textContent = "(not applied)"; continue; }
    el.innerHTML = "";
    el.append(h("span", { class: "dot " + (w && w.visible ? "ok" : "bad") }), w && w.visible ? "visible" : "not visible", " · ", c.status);
    if (p && p.known) el.append(" · ", h("span", { class: p.online ? "ok" : "bad" }, p.online ? "reachable" : "no subscriber"));
  }
  for (const el of $$("[data-job]")) {
    const c = status.control.robots[el.dataset.job]; const j = c && c.calibration_job;
    el.innerHTML = "";
    if (!j) { el.append(h("span", { class: "muted" }, "no calibration run yet")); continue; }
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
    badge(`camera ${hl.camera_ok ? fmt(hl.fps, 1) + " fps" : "down"}`, hl.camera_ok ? "ok" : "bad", s.camera.error || ""),
    badge(`homography ${w.valid ? "ok" : "missing"}`, w.valid ? "ok" : "bad"),
    badge(`bus ${s.bus.kind}`, s.bus.kind === "ros2" ? "ok" : "warn", s.bus.error || ""),
    badge(`GAMA ${s.gama.last_request_age !== null && s.gama.last_request_age < 2 ? "active" : "silent"}`, s.gama.last_request_age !== null && s.gama.last_request_age < 2 ? "ok" : "warn"),
  );
  if (Object.keys(s.registry_errors).length) b.append(badge("controller error", "bad"));
  const ov = s.checks ? s.checks.overall : "unknown";
  if (ov !== "unknown") b.append(badge(`setup ${ov === "ok" ? "ok" : ov === "warn" ? "to check" : "problem"}`, ov,
    (s.checks.checks || []).filter(c => c.status === ov && ov !== "ok").map(c => `${c.label}: ${c.value}`).join("\n")));
  renderChecks();
  const es = $("#estop"); es.classList.toggle("on", hl.estop); es.textContent = hl.estop ? "STOP ENGAGED — release" : "EMERGENCY STOP";
  $("#unsaved").classList.toggle("hidden", !s.config_dirty || dirty);
  $("#cfgpath").textContent = s.config_path ? "File: " + s.config_path : "No configuration file (will be created as config/config.yaml)";

  const wi = $("#worldinfo"); wi.innerHTML = "";
  const kv = (k, v, cls) => wi.append(h("div", {}, k), h("div", { class: cls || "" }, v));
  kv("Homography", w.valid ? (w.homography_locked ? "locked" : "continuous") : "not computed", w.valid ? "ok" : "bad");
  kv("Reprojection error", w.reprojection_error !== null ? fmt(w.reprojection_error * 1000, 2) + " mm" : "–");
  const missing = w.world_tags_expected.filter(i => !w.world_tags_visible.includes(i));
  kv("Visible world tags", `${w.world_tags_visible.join(", ") || "none"}${missing.length ? " (missing: " + missing.join(", ") + ")" : ""}`, missing.length ? "warn" : "ok");
  kv("Unknown tags", w.unknown_tags.join(", ") || "none");
  kv("Image", w.image_size.join(" × "));
  const cam = w.camera;
  kv("Camera position", cam ? `(${fmt(cam.x, 2)}, ${fmt(cam.y, 2)}) m, ${fmt(cam.height, 2)} m high, tilted ${fmt(cam.tilt_deg, 0)}° ` +
    `(${cam.source === "estimated" ? "estimated from the tags" : cam.source === "fov" ? "seen from above, typical field of view" : "lens intrinsics"})` : "–");
  kv("Logical frame", `${w.logical.width} × ${w.logical.height}, origin ${w.logical.origin}, heading in ${w.logical.heading_unit}`);
  const gi = $("#gamainfo"); gi.innerHTML = "";
  gi.append(h("div", {}, "Publishers on /requests"), h("div", {}, s.gama.publishers_on_requests ?? "n/a (no ROS)"),
    h("div", {}, "Last request"), h("div", {}, s.gama.last_request_age !== null ? fmt(s.gama.last_request_age, 1) + " s ago" : "never"));

  const t = $("#robottable"); t.innerHTML = "";
  t.append(h("tr", {}, ["Robot", "Tag", "Seen", "x, y (m)", "θ (°)", "v (m/s)", "Logical (x, y, heading)", "State", "Request", "Command", "Network"].map(x => h("th", {}, x))));
  for (const [n, c] of Object.entries(s.control.robots)) {
    const r = w.robots[n] || {}; const p = s.presence[n] || {};
    const cmd = c.command || {};
    const cmdTxt = cmd.wheels ? `L ${fmt(cmd.wheels[0], 2)} R ${fmt(cmd.wheels[1], 2)}` : `lin ${fmt(cmd.linear, 2)} ang ${fmt(cmd.angular, 2)}`;
    t.append(h("tr", {},
      h("td", {}, h("b", {}, n)), h("td", {}, r.tag_id),
      h("td", {}, h("span", { class: "dot " + (r.visible ? "ok" : "bad") }), r.visible ? "yes" : (r.age != null ? `${fmt(r.age, 1)} s ago` : "never")),
      h("td", {}, r.ever_seen ? `${fmt(r.x)}, ${fmt(r.y)}` : "–"), h("td", {}, r.ever_seen ? fmt(deg(r.theta), 1) : "–"),
      h("td", {}, fmt(r.speed, 3)),
      h("td", {}, r.ever_seen ? `${fmt(r.logical.x, 1)}, ${fmt(r.logical.y, 1)}, ${fmt(r.logical.heading, 1)}` : "–"),
      h("td", { class: c.status === "active" ? "ok" : c.controller_error ? "bad" : "" }, c.status, c.controller_error ? h("pre", { class: "err" }, c.controller_error) : null),
      h("td", {}, c.request ? `${fmt(c.request.speed, 3)} m/s @ ${fmt(deg(c.request.heading), 0)}° (${c.request.source})` : "–"),
      h("td", {}, cmdTxt),
      h("td", {}, p.known ? h("span", { class: p.online ? "ok" : "bad" }, p.online ? p.subscribers.join(", ") : "no subscriber") : h("span", { class: "muted" }, "n/a"))));
  }
  updateRobotLive();
}
$("#estop").addEventListener("click", () => api("POST", "/api/estop", { on: !(status && status.health.estop) }));

// ---------------------------------------------------------------- controllers tab
async function loadControllers() {
  controllers = await api("GET", "/api/controllers");
  const list = $("#ctrllist"); list.innerHTML = "";
  for (const c of controllers.controllers) {
    list.append(h("details", {}, h("summary", {}, c.name, " ", h("span", { class: "muted small" }, c.origin === "builtin" ? "built-in" : c.origin)),
      h("p", { class: "small" }, c.description),
      h("table", {}, h("tr", {}, h("th", {}, "parameter"), h("th", {}, "default"), h("th", {}, "bounds"), h("th", {}, "description")),
        Object.entries(c.params).map(([k, p]) => h("tr", {}, h("td", {}, k), h("td", {}, JSON.stringify(p.default)),
          h("td", {}, p.choices ? p.choices.join(" | ") : `${p.min ?? ""} … ${p.max ?? ""}`), h("td", {}, p.description))))));
  }
  const errs = $("#ctrlerrors"); errs.innerHTML = "";
  const e = Object.entries(controllers.errors);
  if (!e.length) errs.textContent = "none";
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
  if (!openFile) return toast("No file open", true);
  try {
    const r = await api("PUT", "/api/controllers/source/" + openFile, { code: $("#ctrlcode").value });
    $("#ctrlmsg").innerHTML = ""; $("#ctrlmsg").append(r.errors ? h("span", { class: "bad" }, "saved, but loading failed (see on the left)") : h("span", { class: "ok" }, "saved and reloaded ✓"));
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
  if (n.bus_error) kv("Error", n.bus_error, "bad");
  if (!info.available) kv("ROS 2", info.reason || "unavailable (rclpy not found — source the ROS 2 installation)", "warn");
  kv("Node", info.node); kv("RMW", info.rmw); kv("Domain ID", info.domain_id);
  for (const [k, v] of Object.entries(info.env || {})) kv(k, v === null ? "(not set)" : v, v === null ? "muted" : "");

  const pres = $("#presence"); pres.innerHTML = "";
  const tbl = h("table", {}, h("tr", {}, ["Robot", "Command topic", "Subscribers", "Camera"].map(x => h("th", {}, x))));
  for (const r of serverCfg.robots) {
    const p = status?.presence?.[r.name] || {}; const w = status?.world?.robots?.[r.name] || {};
    tbl.append(h("tr", {}, h("td", {}, r.name), h("td", {}, h("code", {}, p.topic || r.output.topic.replace("{name}", r.name))),
      h("td", {}, p.known ? h("span", { class: p.online ? "ok" : "bad" }, h("span", { class: "dot " + (p.online ? "ok" : "bad") }), p.online ? p.subscribers.join(", ") : "no node listening") : h("span", { class: "muted" }, "n/a")),
      h("td", {}, h("span", { class: "dot " + (w.visible ? "ok" : "bad") }), w.visible ? "tag visible" : "tag not visible")));
  }
  pres.append(tbl);

  const topics = info.topics || [];
  const byName = Object.fromEntries(topics.map(t => [t.name, t]));
  const tm = $("#topicmap"); tm.innerHTML = "";
  tm.append(h("tr", {}, ["Topic", "Type", "Dir.", "Role", "Other side"].map(x => h("th", {}, x))));
  for (const r of n.topic_map) {
    const g = byName[r.topic]; let other = h("span", { class: "muted" }, info.available ? "not in the graph" : "n/a");
    if (g) {
      const cnt = r.dir === "pub" ? g.subscribers : g.publishers;
      const label = r.dir === "pub" ? `${g.subscribers} subscriber(s)` : `${g.publishers} publisher(s)`;
      other = h("span", { class: cnt > 0 ? "ok" : "warn" }, label);
    }
    tm.append(h("tr", {}, h("td", {}, h("code", {}, r.topic)), h("td", {}, r.type), h("td", {}, r.dir === "pub" ? "→ publishes" : "← subscribes"), h("td", {}, r.doc), h("td", {}, other)));
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
  $("#scaninfo").textContent = `${lastScan.tags.length} tag(s) over ${lastScan.frames} frame(s)` + (lastScan.homography_valid ? "" : " — homography not valid");
  t.append(h("tr", {}, ["Tag", "Current role", "Seen", "Image (px)", "World (m)", "Action (draft)"].map(x => h("th", {}, x))));
  for (const g of lastScan.tags) {
    const inDraftWorld = draft.world.tags.some(w => w.id === g.id);
    const draftRobot = draft.robots.find(r => r.tag_id === g.id);
    const role = g.role === "world" ? h("span", { class: "ok" }, "world landmark")
      : g.role === "robot" ? h("span", { class: "ok" }, "robot " + g.robot)
      : inDraftWorld ? h("span", { class: "warn" }, "world landmark (draft)")
      : draftRobot ? h("span", { class: "warn" }, `robot ${draftRobot.name} (draft)`)
      : h("span", { class: "warn" }, "inconnu");
    let action = "";
    if (g.role === "unknown" && !inDraftWorld && !draftRobot) {
      const sel = h("select", {}, h("option", { value: "" }, "assign to…"),
        draft.robots.map(r => h("option", { value: "r:" + r.name }, "robot " + r.name + (r.tag_id < 0 ? " (unassigned)" : ` (#${r.tag_id})`))),
        h("option", { value: "new" }, "new robot"),
        g.world ? h("option", { value: "world" }, "world landmark (measured position)") : null);
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
    const r = await api("POST", "/api/setup/propose_world", { width: Number($("#wwidth").value) || null, height: Number($("#wheight").value) || null,
      rotation: Number($("#wrot").value), mirror: $("#wmirror").checked, duration: Number($("#scandur").value) });
    lastScan = r.scan; renderScan();
    out.innerHTML = "";
    if (r.estimated) out.append(h("p", { class: "small" }, h("b", {}, `Estimated from the printed tags (${fmt(r.estimated.tag_size * 1000, 0)} mm): `),
      `${fmt(r.estimated.width, 2)} m × ${fmt(r.estimated.height, 2)} m between tag centres (±2 %, fit ${fmt(r.estimated.rms_px, 2)} px). `,
      "Positions are relative to these 4 tags anyway: the size only sets the units (m/s, tolerances)."));
    const keepBox = h("input", { type: "checkbox" });
    out.append(h("table", {}, h("tr", {}, h("th", {}, "Tag"), h("th", {}, "x (m)"), h("th", {}, "y (m)")),
      r.tags.map(t => h("tr", {}, h("td", {}, "#" + t.id), h("td", {}, t.x), h("td", {}, t.y)))),
      h("label", { class: "small" }, keepBox, " also keep the other existing landmarks"),
      h("div", { class: "row" }, h("button", { class: "primary", onclick: () => {
        const keep = keepBox.checked ? draft.world.tags.filter(t => !r.tags.some(n => n.id === t.id)) : [];
        draft.world.tags = [...r.tags, ...keep];
        draft.robots.forEach(rb => { if (r.tags.some(t => t.id === rb.tag_id)) rb.tag_id = nextUnassigned(); });
        markDirty(); renderForms(); renderScan();
        out.append(h("div", { class: "ok small" }, "Proposal copied to the draft — check it, then click \"Apply\"."));
      } }, "Use (draft)")));
  } catch (e) { out.innerHTML = ""; out.append(h("div", { class: "bad" }, e.message)); }
});
$("#measureextra").addEventListener("click", async () => {
  try {
    const r = await api("POST", "/api/setup/measure_tags", { duration: Number($("#scandur").value) });
    lastScan = r.scan; renderScan();
    const cand = r.tags.filter(t => !draft.world.tags.some(w => w.id === t.id) && !draft.robots.some(rb => rb.tag_id === t.id));
    const info = $("#measureinfo"); info.innerHTML = "";
    if (!cand.length) { info.textContent = "no unknown tag to add"; return; }
    // unchecked by default: an unknown tag may be on a robot that is not configured yet
    const boxes = cand.map(t => [h("input", { type: "checkbox" }), t]);
    info.append(h("div", { class: "warn" }, "Only tick the tags fixed to the floor (not the robots' ones):"),
      h("table", {}, boxes.map(([cb, t]) => h("tr", {}, h("td", {}, cb), h("td", {}, "#" + t.id), h("td", {}, `x ${t.x}`), h("td", {}, `y ${t.y}`)))),
      h("button", { class: "ghost", onclick: () => {
        const add = boxes.filter(([cb]) => cb.checked).map(([, t]) => t);
        draft.world.tags.push(...add);
        if (add.length) { markDirty(); renderForms(); renderScan(); }
        info.textContent = add.length ? `${add.length} landmark(s) added to the draft: ${add.map(t => "#" + t.id).join(", ")}` : "nothing added";
      } }, "Add the selection (draft)"));
  } catch (e) { toast(e.message, true); }
});
$("#robotsfromtags").addEventListener("click", () => {
  if (!lastScan) return toast("Run a scan first", true);
  const unk = lastScan.tags.filter(t => t.role === "unknown" && !draft.robots.some(r => r.tag_id === t.id) && !draft.world.tags.some(w => w.id === t.id));
  unk.forEach(t => draft.robots.push(newRobot("robot_" + t.id, t.id)));
  if (unk.length) { markDirty(); renderForms(); renderScan(); }
  toast(unk.length ? `${unk.length} robot(s) added to the draft (check their topics in the Robots tab).` : "No unknown tag.");
});
$("#discover").addEventListener("click", async () => {
  const out = $("#discoverout"); out.textContent = "searching…";
  const tpl = $("#disctpl").value || "/{name}/cmd_vel";
  try {
    const r = await api("GET", "/api/setup/discover_robots?template=" + encodeURIComponent(tpl));
    out.innerHTML = "";
    if (!r.available) { out.append(h("div", { class: "warn small" }, r.reason)); return; }
    if (!r.robots.length) { out.append(h("div", { class: "muted small" }, `no topic matches ${r.template}`)); return; }
    const boxes = [];
    out.append(h("table", {}, h("tr", {}, ["", "Robot", "Topic", "Type", "Subscribers"].map(x => h("th", {}, x))),
      r.robots.map(x => {
        const known = x.configured || draft.robots.some(d => d.name === x.name);
        const cb = h("input", { type: "checkbox" }); cb.checked = !known; cb.disabled = known; boxes.push([cb, x]);
        return h("tr", {}, h("td", {}, cb), h("td", {}, x.name, known ? h("span", { class: "muted small" }, " (already configured)") : ""),
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
        toast(`${n} robot(s) added to the draft. Click "Apply", then "Identify" to assign their tags.`);
      } }, "Add the selection (draft)"));
  } catch (e) { out.textContent = e.message; }
});
function renderIdentTable() {
  const t = $("#identtable"); if (!serverCfg) return;
  if (t.contains(document.activeElement)) return; // don't disturb manual typing
  t.innerHTML = "";
  t.append(h("tr", {}, ["Robot", "Tag", "Seen", "", "Manual entry", "Last identification"].map(x => h("th", {}, x))));
  for (const r of serverCfg.robots) {
    const c = status?.control?.robots?.[r.name]; const w = status?.world?.robots?.[r.name];
    const j = c?.calibration_job; const isIdent = j && j.procedure === "identify";
    const inp = h("input", { type: "number", style: "width:70px" }); inp.value = r.tag_id >= 0 ? r.tag_id : "";
    let last = "";
    if (isIdent) last = h("span", { class: j.status === "done" ? "ok" : j.status === "running" ? "warn" : "bad" },
      j.status === "running" ? `running (${j.phase})` : j.status === "done" ? `#${j.result.tag_id} ✓` : (j.error || j.status));
    t.append(h("tr", {},
      h("td", {}, h("b", {}, r.name)),
      h("td", {}, r.tag_id >= 0 ? "#" + r.tag_id : h("span", { class: "warn" }, "unassigned")),
      h("td", {}, h("span", { class: "dot " + (w?.visible ? "ok" : "bad") })),
      h("td", {}, h("button", { class: "ghost", onclick: () => startIdentify(r.name).catch(e => toast(e.message, true)) }, "Identify")),
      h("td", {}, inp, " ", h("button", { class: "ghost", onclick: async () => {
        if (inp.value === "") return;
        try { await api("POST", `/api/robots/${r.name}/assign_tag`, { tag_id: Number(inp.value) }); await loadConfig(true); } catch (e) { toast(e.message, true); }
      } }, "OK")),
      h("td", { class: "small" }, last)));
  }
}
async function startIdentify(name) {
  if (dirty) throw new Error("Apply the pending changes first.");
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
  if (dirty) return toast("Apply the pending changes first.", true);
  const todo = serverCfg.robots.filter(r => r.tag_id < 0 || !(status?.world?.robots?.[r.name]?.visible));
  if (!todo.length) return toast("All robots have a visible tag.");
  const btn = $("#identifyall"); btn.disabled = true;
  const report = [];
  try {
    for (const r of todo) {
      try { await startIdentify(r.name); const j = await waitJob(r.name); report.push(`${r.name} : ${j.status === "done" ? "#" + j.result.tag_id : j.error}`); }
      catch (e) { report.push(`${r.name} : ${e.message}`); }
    }
  } finally { btn.disabled = false; await loadConfig(true); }
  toast("Identification finished:\n" + report.join("\n"));
});

// ---------------------------------------------------------------- misc
$("#probe").addEventListener("click", async () => {
  $("#probeout").textContent = "searching…";
  const cams = await api("GET", "/api/cameras");
  $("#probeout").textContent = cams.length ? cams.map(c => `#${c.index} (${c.width}×${c.height})`).join(", ") + " — set 'source' to the index" : "no local camera found";
});
$("#relock").addEventListener("click", () => api("POST", "/api/world/relock"));

// ---------------------------------------------------------------- start guide
let stepsKey = "";
function renderSteps() {
  const s = status; if (!s || !serverCfg) return;
  const hl = s.health, w = s.world, cfg = serverCfg;
  const robots = cfg.robots.filter(r => r.enabled);
  const missing = w.world_tags_expected.filter(i => !w.world_tags_visible.includes(i));
  const seen = robots.filter(r => w.robots[r.name] && w.robots[r.name].visible);
  const known = robots.filter(r => s.presence[r.name] && s.presence[r.name].known);
  const online = known.filter(r => s.presence[r.name].online);
  const calibrated = robots.filter(r => r.calibration && r.calibration.calibrated_at);
  const homed = robots.filter(r => r.home);
  const q = s.checks ? s.checks.overall : "unknown";
  const gamaOn = s.gama.last_request_age !== null && s.gama.last_request_age < 2;
  const st = (ok, warn) => ok ? "ok" : warn ? "warn" : "bad";
  const steps = [
    ["Camera streaming", "camera", st(hl.camera_ok), hl.camera_ok ? `${fmt(hl.fps, 0)} fps from ${cfg.camera.source}` : (hl.camera_error || "no image"),
      "Pick the camera source. For a phone: set source to 'mobile', open the Tango Camera app and type the pairing code."],
    ["Boundary tags in view", "world", st(w.valid && !missing.length, w.valid), w.valid ? (missing.length ? `missing: ${missing.join(", ")}` : `all ${w.world_tags_expected.length} visible, error ${fmt(w.reprojection_error * 1000, 1)} mm`) : "homography not computed",
      "Print the boundary tags (Field & tags), lay them flat at the corners, enter their positions (or use Scan & assign)."],
    ["Image quality", "camera", q === "unknown" ? "warn" : q, s.checks && s.checks.checks.filter(c => c.status === q && q !== "ok").map(c => c.label).join(", ") || (q === "ok" ? "lighting and tags look good" : "waiting for frames"),
      "Fix the red lines of the setup checks: light, glare, tag size, focus."],
    ["Robots seen by the camera", "deploy", st(robots.length && seen.length === robots.length, seen.length), `${seen.length}/${robots.length} visible`,
      "Stick a tag on each robot (FRONT arrow forward). Scan & assign → Identify finds which tag is on which robot."],
    ["Robots listening on ROS", "network", s.bus.kind !== "ros2" ? "warn" : st(known.length && online.length === known.length, online.length), s.bus.kind !== "ros2" ? "no ROS 2 (loopback bus)" : `${online.length}/${known.length} with a subscriber on their command topic`,
      "Start the robots' ROS nodes; same ROS_DOMAIN_ID as the middleware."],
    (() => { const p = s.checks && s.checks.checks.find(c => c.id === "parallax");
      const st2 = !p || p.status === "unknown" ? "warn" : p.status;
      return ["Robot tag heights (parallax)", "robots", st2, p ? p.value : "waiting for the camera position",
        "Robots tab → Tag height: measure the height of each robot's tag above the floor. Matters most with a camera at head height."]; })(),
    ["Robots calibrated", "robots", st(robots.length && calibrated.length === robots.length, calibrated.length), `${calibrated.length}/${robots.length}`,
      "Robots tab → Calibration → 'full' → Start, one robot at a time, with room around it."],
    ["Home positions set", "robots", st(robots.length && homed.length === robots.length, homed.length), `${homed.length}/${robots.length} (optional)`,
      "Robots tab → Home position: put the robot at its start spot and click 'Use current position'."],
    ["GAMA connected", "dash", st(gamaOn, s.gama.publishers_on_requests > 0), gamaOn ? "requests arriving" : (s.gama.publishers_on_requests ? "publisher present, no request yet" : "no request yet"),
      "Run the GAMA model (docs/gama/tango_demo.gaml): it publishes on /tango/requests."],
  ];
  const key = JSON.stringify(steps.map(x => [x[2], x[3]]));
  if (key === stepsKey) return;
  stepsKey = key;
  const ol = $("#steps"); ol.innerHTML = "";
  for (const [title, tab, state, detail, how] of steps) {
    ol.append(h("li", { class: "step " + state, onclick: () => setTab(tab), title: "Open " + tab },
      h("span", { class: "dot " + state }), h("b", {}, title), h("span", { class: "muted small" }, " — " + detail),
      state !== "ok" ? h("div", { class: "small how" }, how) : null));
  }
}
$("#placeall").addEventListener("click", async () => {
  try { const r = await api("POST", "/api/place", {}); $("#placeinfo").textContent = "placing " + r.placing.join(", "); }
  catch (e) { toast(e.message, true); }
});
$("#cancelplace").addEventListener("click", async () => {
  for (const r of serverCfg.robots) await api("DELETE", `/api/robots/${r.name}/goto`).catch(() => {});
  $("#placeinfo").textContent = "cancelled";
});

// ---------------------------------------------------------------- phone quick controls
function renderPhoneControls() {
  const box = $("#phonecontrols"); if (!box || !serverCfg) return;
  box.innerHTML = "";
  const cam = serverCfg.camera, m = cam.mobile;
  if ((cam.source || "").trim().toLowerCase() !== "mobile") return;
  const sel = (label, value, options, onchange, tip) => {
    const s = h("select", {}, options.map(([v, t]) => h("option", { value: JSON.stringify(v), selected: JSON.stringify(v) === JSON.stringify(value) }, t)));
    s.addEventListener("change", () => onchange(JSON.parse(s.value)));
    return h("label", { class: "small", title: tip || "" }, label, " ", s);
  };
  const set = async mut => {
    const c = clone(serverCfg.camera); mut(c);
    try { await api("PATCH", "/api/config/camera?save=true", c); serverCfg.camera = c; draft.camera = clone(c); renderSection("#form-camera", "camera"); renderPhoneControls(); }
    catch (e) { toast(e.message, true); }
  };
  box.append(h("div", { class: "row quick" },
    sel("Resolution", [cam.width, cam.height], [[[640, 480], "640×480 (4:3)"], [[1280, 720], "1280×720 (16:9)"], [[1280, 960], "1280×960 (4:3)"], [[1920, 1080], "1920×1080 (16:9)"], [[1920, 1440], "1920×1440 (4:3)"]],
      v => set(c => { c.width = v[0]; c.height = v[1]; }), "Higher = smaller tags still readable, but more Wi-Fi and CPU. 4:3 suits a square field"),
    sel("FPS", cam.fps, [[15, "15"], [30, "30"], [60, "60"]], v => set(c => { c.fps = v; })),
    sel("Max exposure", m.max_exposure_ms, [[null, "auto"], [20, "20 ms"], [10, "10 ms"], [5, "5 ms"]],
      v => set(c => { c.mobile.max_exposure_ms = v; }), "Shorter = less motion blur, needs more light. 10 / 20 ms avoid flicker under 50 Hz lights"),
    sel("Focus", m.focus, [["auto", "auto"], ["infinity", "fixed (far)"]], v => set(c => { c.mobile.focus = v; })),
    sel("Lens", m.lens, [["back", "back"], ["front", "front"]], v => set(c => { c.mobile.lens = v; })),
    sel("Torch", m.torch, [[false, "off"], [true, "on"]], v => set(c => { c.mobile.torch = v; })),
    sel("JPEG", m.jpeg_quality, [[60, "60"], [80, "80"], [90, "90"]], v => set(c => { c.mobile.jpeg_quality = v; }), "Image compression: lower = less Wi-Fi bandwidth")));
}

// ---------------------------------------------------------------- printable tags
function parseIds(t) {
  const out = [];
  for (const part of (t || "").split(/[,; ]+/)) {
    const m = part.match(/^(\d+)(?:-(\d+))?$/); if (!m) continue;
    const a = +m[1], b = m[2] ? +m[2] : a; for (let i = a; i <= Math.min(b, a + 200); i++) out.push(i);
  }
  return out;
}
function updateTagLinks() {
  if (!serverCfg) return;
  const ws = $("#tagsize").value || 100, rs = $("#rtagsize").value || 60;
  const q = `size_mm=${ws}&robot_size_mm=${rs}`;
  $("#printworld").href = `/print/tags?which=world&${q}`;
  $("#printrobots").href = `/print/tags?which=robots&size_mm=${rs}`;
  $("#printall").href = `/print/tags?which=all&${q}`;
  $("#ziptags").href = `/api/tags/archive.zip?which=all&${q}`;
  $("#printcustom").href = `/print/tags?which=custom&ids=${parseIds($("#tagids").value).join(",")}&size_mm=${ws}`;
  const g = $("#taggallery"); g.innerHTML = "";
  const add = (id, cap, front, mm) => g.append(h("figure", {},
    h("img", { src: `/api/tags/${id}.svg?size_mm=${mm}${front ? "&front=true" : ""}`, alt: "tag " + id }),
    h("figcaption", {}, cap, h("br"), h("a", { href: `/api/tags/${id}.svg?size_mm=${mm}${front ? "&front=true" : ""}`, download: `tag_${id}.svg` }, "SVG"), " · ",
      h("a", { href: `/api/tags/${id}.png?size=800`, download: `tag_${id}.png` }, "PNG"))));
  for (const t of serverCfg.world.tags) add(t.id, `#${t.id} boundary (${t.x}, ${t.y}) m`, false, ws);
  for (const r of serverCfg.robots) if (r.tag_id >= 0) add(r.tag_id, `#${r.tag_id} ${r.name}`, true, rs);
}
["#tagsize", "#rtagsize", "#tagids"].forEach(id => $(id).addEventListener("input", updateTagLinks));

// ---------------------------------------------------------------- setup checks
let checksKey = "";
function renderChecks() {
  const r = status && status.checks; if (!r) return;
  const key = JSON.stringify(r.checks.map(c => [c.status, c.value]));
  if (key === checksKey) return;
  checksKey = key;
  for (const t of $$("[data-checks]")) {
    t.innerHTML = "";
    if (!r.checks.length) { t.append(h("tr", {}, h("td", { class: "muted" }, "waiting for frames…"))); continue; }
    for (const c of r.checks) {
      t.append(h("tr", {},
        h("td", {}, h("span", { class: "dot " + (c.status === "unknown" ? "" : c.status) }), c.label),
        h("td", { class: c.status === "ok" ? "" : c.status }, c.value),
        h("td", { class: "muted small" }, c.advice || "")));
    }
  }
}

// ---------------------------------------------------------------- phone camera
let mobileKey = "";
function renderMobile() {
  const box = $("#mobilebox"); if (!box || !status) return;
  const m = status.mobile, src = (status.camera.source || "").trim().toLowerCase();
  const key = JSON.stringify([src, m && [m.running, m.error, m.https_error, m.pairing_code, m.require_code, m.web, m.app,
    m.client && [m.client.name, m.client.address, Math.round(m.client.fps), m.client.width, JSON.stringify(m.client.stats)]]]);
  if (key === mobileKey) return;
  mobileKey = key; box.innerHTML = "";
  if (src !== "mobile") {
    box.append(h("p", { class: "muted small" }, "An Android phone (Tango Camera app) or an iPhone (web page) can be the camera. ",
      "It is found automatically on the local network and reconnects on its own."),
      h("button", { onclick: async () => {
        if (dirty && !confirm("Unapplied changes will be saved too. Continue?")) return;
        draft.camera.source = "mobile"; await applyDraft(true);
      } }, "Use a phone as camera"));
    return;
  }
  if (!m) { box.append(h("p", { class: "bad" }, "Phone receiver stopped: " + (status.camera.error || "starting…"))); return; }
  if (m.error) box.append(h("p", { class: "bad" }, m.error));
  const c = m.client;
  const st = c ? c.stats || {} : {};
  box.append(h("div", { class: "kv" },
    h("span", {}, "State"), c ? h("span", { class: c.frame_age !== null && c.frame_age < 2 ? "ok" : "warn" },
      `${c.name} (${c.platform}) — ${fmt(c.fps, 1)} fps, ${c.width}×${c.height}`) : h("span", { class: "warn" }, "waiting for a phone…"),
    c && st.battery !== undefined ? [h("span", {}, "Phone"), h("span", { class: st.battery < 20 && !st.charging ? "bad" : "" },
      `battery ${st.battery} %${st.charging ? " (charging)" : ""}${st.temperature !== undefined ? `, ${fmt(st.temperature, 0)} °C` : ""}`
      + (st.camera_fps !== undefined ? ` — sensor ${fmt(st.camera_fps, 0)} fps, JPEG ${fmt(st.encode_ms, 1)} ms, ${fmt(st.kbps / 1000, 1)} Mbit/s` : "")
      + (st.exposure_ms ? `, exposure ${fmt(st.exposure_ms, 1)} ms ISO ${st.iso}` : "")
      + (st.screen_on === false ? ", screen off" : ""))] : null,
    h("span", {}, "Pairing code"), h("span", {}, m.require_code ? h("b", { style: "font-size:20px;letter-spacing:.15em" }, m.pairing_code) : "disabled (camera.mobile.require_code)"),
    h("span", {}, "Android app"), h("span", {}, m.app.length ? m.app.join(", ") : "–", h("small", { class: "muted" }, " (found automatically)")),
    h("span", {}, "Web page (iPhone)"), h("span", {}, m.web.length ? h("a", { href: m.web[0], target: "_blank" }, m.web[0].replace(/#.*/, "")) : "–"),
  ));
  if (m.https_error) box.append(h("p", { class: "warn small" }, m.https_error));
  if (m.web.length) {
    box.append(h("div", { class: "row" },
      h("img", { src: "/api/mobile/qr.png?data=" + encodeURIComponent(m.web[0]), alt: "QR code", style: "width:180px;height:180px;background:#fff;border-radius:6px" }),
      h("p", { class: "muted small", style: "flex:1;min-width:200px" },
        "Scan this QR code: with the iPhone Camera app it opens the camera page (accept the self-signed certificate); ",
        "with the Android app (\"Scan QR code\") it pairs directly. ",
        m.web.length > 1 ? `Other addresses: ${m.web.slice(1).map(u => u.replace(/#.*/, "")).join(", ")}` : "")));
  }
  box.append(h("div", { class: "row" },
    h("button", { class: "ghost", onclick: async () => { if (confirm("Phones already paired will have to pair again. Continue?")) { await api("POST", "/api/mobile/new_code"); await loadConfig(true); } } }, "New code"),
    c ? h("button", { class: "ghost", onclick: () => api("POST", "/api/mobile/disconnect") }, "Disconnect the phone") : null));
  box.append(h("p", { class: "muted small" }, "The quick settings above are applied to the phone at once; the rest is under Camera → Advanced settings → mobile. ",
    "Windows: allow Python through the firewall (private network) for ports ", `${m.port}${m.https_port ? " and " + m.https_port : ""} (TCP + UDP).`));
}
// help bubbles: hover on a desktop, tap on a phone / tablet
document.addEventListener("click", e => {
  const b = e.target.closest(".help");
  $$(".help.open").forEach(x => { if (x !== b) x.classList.remove("open"); });
  if (b) { b.classList.toggle("open"); e.stopPropagation(); }
}, true);
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
  $("#stream0").src = name === "start" ? "/api/stream.mjpg" : "";
  $("#stream").src = name === "dash" ? "/api/stream.mjpg" : "";
  $("#stream2").src = name === "camera" ? "/api/stream.mjpg" : "";
  $("#stream3").src = name === "deploy" ? "/api/stream.mjpg" : "";
  if (name === "deploy") { prefillWorldDims(); renderIdentTable(); }
  if (name === "network") loadNetwork().catch(() => {});
  if (name === "controllers") loadControllers().catch(() => {});
  try { localStorage.setItem("tab", name); } catch {}
  history.replaceState(null, "", "#tab=" + name);  // shareable link to a tab
}
$$("#tabs button").forEach(b => b.addEventListener("click", () => setTab(b.dataset.tab)));

async function pollStatus() {
  try { status = await api("GET", "/api/status"); renderStatus(); if (currentTab === "deploy") renderIdentTable(); if (currentTab === "camera") renderMobile(); if (currentTab === "start") renderSteps(); } catch {}
}

(async function init() {
  schema = await api("GET", "/api/config/schema");
  procedures = await api("GET", "/api/calibration/procedures");
  controllers = await api("GET", "/api/controllers");
  await loadConfig(true);
  await loadControllers();
  let t = "start"; try { t = localStorage.getItem("tab") || "start"; } catch {}
  const fromHash = location.hash.match(/tab=(\w+)/); if (fromHash) t = fromHash[1];
  if (!$(`#tab-${t}`)) t = "start";
  setTab(t);
  pollStatus(); setInterval(pollStatus, 500);
  setInterval(pollLogs, 1000); pollLogs();
  setInterval(() => { if (!dirty && !document.activeElement?.matches("input, textarea, select")) loadConfig(false).catch(() => {}); }, 3000);
  setInterval(() => { if (currentTab === "network") loadNetwork().catch(() => {}); }, 2000);
})();
