/* 3D globe. Positions are precomputed by scripts/build_site.py with SGP4 from
   publishable element sets (TEME frame, Earth radii); this file only
   interpolates and draws them. Earth turns at the sidereal rate (GMST), so
   orbits stay fixed in space and GEO satellites sit over their longitude. */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const TEX = "https://cdn.jsdelivr.net/npm/three-globe@2.31.0/example/img/";
const RE_KM = 6378.137;
const MU = 398600.4418;
const GROUP_NAMES = { geo_inspector: "GEO inspector", leo_shadowing: "LEO close approach", validation: "Validation (ISS)", starlink_sample: "Starlink sample" };
const LABELS = {
  geo_ew_stationkeeping: "GEO east-west station-keeping", geo_ns_stationkeeping: "GEO north-south station-keeping",
  geo_drift_start: "GEO drift start", geo_drift_stop: "GEO drift stop", geo_drift_change: "GEO drift change",
  geo_relocation: "GEO relocation", geo_plane_change: "GEO plane change", geo_maneuver: "GEO maneuver",
  altitude_maintenance: "Altitude maintenance", orbit_raise: "Orbit raise", orbit_lowering: "Orbit lowering",
  circularization: "Circularization", eccentricity_raise: "Eccentricity raise", plane_change: "Plane change",
};
const COLORS = { "United States": 0x3987e5, China: 0xd95926, Russia: 0x199e70, "NASA/Roscosmos": 0xf0efec, SpaceX: 0x898781 };
const colorOf = (o) => (o.category === "starlink_sample" ? 0x898781 : COLORS[o.operator] ?? 0xc3c2b7);
const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  for (const k of kids.flat()) if (k !== null && k !== undefined) n.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return n;
};
const fmt = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toFixed(d));
const kmText = (km) => (km < 10 ? `${fmt(km, 2)} km` : km < 1000 ? `${fmt(km, 0)} km` : `${(km / 1000).toFixed(1)}k km`);

// ---------- time & frames ----------
function gmst(unix) {
  // IAU 1982 GMST (radians); plenty for orienting a texture.
  const jd = unix / 86400 + 2440587.5, t = (jd - 2451545.0) / 36525;
  let s = 67310.54841 + (876600 * 3600 + 8640184.812866) * t + 0.093104 * t * t - 6.2e-6 * t * t * t;
  s = ((s % 86400) + 86400) % 86400;
  return (s / 86400) * 2 * Math.PI;
}
function sunDir(unix) {
  // Low-precision solar position (about 0.01 deg), as a TEME-ish unit vector.
  const n = unix / 86400 + 2440587.5 - 2451545.0;
  const L = (280.46 + 0.9856474 * n) * Math.PI / 180, g = (357.528 + 0.9856003 * n) * Math.PI / 180;
  const lam = L + (1.915 * Math.sin(g) + 0.02 * Math.sin(2 * g)) * Math.PI / 180, eps = 23.439 * Math.PI / 180;
  return [Math.cos(lam), Math.cos(eps) * Math.sin(lam), Math.sin(eps) * Math.sin(lam)];
}
// TEME (x, y, z north) -> three.js (x, y up, z): (x, z, -y). Matches the
// sphere texture's longitude layout, so Earth rotation is just rotation.y = GMST.
const toScene = (p, out = new THREE.Vector3()) => out.set(p[0], p[2], -p[1]);
const iso = (unix) => new Date(unix * 1000).toISOString().replace("T", " ").slice(0, 19) + "Z";

// ---------- scene ----------
const canvas = $("scene");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, logarithmicDepthBuffer: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(40, 1, 1e-6, 2000);
camera.position.set(0, 5.5, 17);
const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.minDistance = 1.15;
controls.maxDistance = 60;

scene.add(new THREE.AmbientLight(0xffffff, 0.12));
const sun = new THREE.DirectionalLight(0xffffff, 2.2);
scene.add(sun);

const loader = new THREE.TextureLoader();
const earthTex = loader.load(TEX + "earth-blue-marble.jpg");
earthTex.colorSpace = THREE.SRGBColorSpace;
earthTex.anisotropy = renderer.capabilities.getMaxAnisotropy();
const earth = new THREE.Mesh(new THREE.SphereGeometry(1, 128, 96),
  new THREE.MeshPhongMaterial({ map: earthTex, bumpMap: loader.load(TEX + "earth-topology.png"), bumpScale: 0.004, shininess: 6 }));
scene.add(earth);

// thin atmosphere rim
scene.add(new THREE.Mesh(new THREE.SphereGeometry(1.025, 96, 64), new THREE.ShaderMaterial({
  transparent: true, depthWrite: false, side: THREE.BackSide, blending: THREE.AdditiveBlending,
  vertexShader: "varying vec3 n; void main(){ n = normalize(normalMatrix * normal); gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }",
  fragmentShader: "varying vec3 n; void main(){ float i = pow(0.72 - dot(n, vec3(0.0,0.0,1.0)), 3.0); gl_FragColor = vec4(0.35,0.6,1.0,1.0) * i; }",
})));

// stars
{
  const g = new THREE.BufferGeometry(), N = 2500, a = new Float32Array(N * 3);
  for (let i = 0; i < N; i++) {
    const u = Math.random() * 2 - 1, t = Math.random() * 2 * Math.PI, r = 400, s = Math.sqrt(1 - u * u);
    a.set([r * s * Math.cos(t), r * u, r * s * Math.sin(t)], i * 3);
  }
  g.setAttribute("position", new THREE.BufferAttribute(a, 3));
  scene.add(new THREE.Points(g, new THREE.PointsMaterial({ color: 0x9aa0a6, size: 1.2, sizeAttenuation: false })));
}

// equator ring + GEO belt guide
function ring(radius, color, opacity) {
  const pts = [];
  for (let i = 0; i <= 256; i++) { const t = (i / 256) * 2 * Math.PI; pts.push(new THREE.Vector3(radius * Math.cos(t), 0, radius * Math.sin(t))); }
  return new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({ color, transparent: true, opacity }));
}
const geoRing = ring(42164 / RE_KM, 0x7d8a99, 0.32);
scene.add(geoRing);

// ---------- state ----------
const S = {
  mode: "live", data: null, objects: [], t: 0, t0: 0, t1: 0, speed: 60, playing: true,
  selected: null, hovered: null, groups: new Set(["geo_inspector", "leo_shadowing", "validation", "starlink_sample"]),
  orbitMode: "selected", follow: true, replay: null, replays: [], events: {}, sats: {},
};
if (new URLSearchParams(location.search).has("debug")) window.__globe = S;  // console inspection only
let points = null, orbitLines = [], extraLines = [], neighbourLines = [];
const tmp = new THREE.Vector3();

function posAt(o, t) {
  const d = S.data, f = (t - d.t0) / d.step_s, i = Math.floor(f);
  if (i < 0 || i >= d.n - 1) return null;
  // a replay target carries its pre-burn and post-burn tracks separately
  const track = o.pos_after && t >= d.burn_t ? o.pos_after : o.pos;
  const a = track[i], b = track[i + 1];
  if (!a || !b) return a || b || null;
  const w = f - i;
  return [a[0] + (b[0] - a[0]) * w, a[1] + (b[1] - a[1]) * w, a[2] + (b[2] - a[2]) * w];
}

function line(pts, color, opacity, dashed = false) {
  const geo = new THREE.BufferGeometry().setFromPoints(pts.map((p) => toScene(p)));
  const mat = dashed ? new THREE.LineDashedMaterial({ color, transparent: true, opacity, dashSize: 0.04, gapSize: 0.03 })
    : new THREE.LineBasicMaterial({ color, transparent: true, opacity });
  const l = new THREE.Line(geo, mat);
  if (dashed) l.computeLineDistances();
  return l;
}

function clearScene() {
  for (const l of [...orbitLines, ...extraLines, ...neighbourLines]) { scene.remove(l); l.geometry.dispose(); }
  orbitLines = []; extraLines = []; neighbourLines = [];
  if (points) { scene.remove(points); points.geometry.dispose(); points = null; }
}

function build(objects) {
  clearScene();
  S.objects = objects;
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(objects.length * 3), 3));
  const col = new Float32Array(objects.length * 3);
  objects.forEach((o, i) => { const c = new THREE.Color(colorOf(o)); col.set([c.r, c.g, c.b], i * 3); });
  g.setAttribute("color", new THREE.BufferAttribute(col, 3));
  points = new THREE.Points(g, new THREE.PointsMaterial({ size: 7, sizeAttenuation: false, vertexColors: true, depthWrite: false,
    map: dotTexture(), alphaTest: 0.4, transparent: true }));
  points.renderOrder = 2;
  scene.add(points);
  for (const o of objects) {
    const path = o.orbit || o.orbit_after;
    const l = path && path.length ? line(path, colorOf(o), 0.35) : null;
    o.orbitLine = l;
    if (l) { scene.add(l); orbitLines.push(l); }
  }
  applyVisibility();
}

function dotTexture() {
  const c = document.createElement("canvas"); c.width = c.height = 64;
  const x = c.getContext("2d");
  x.fillStyle = "#050506"; x.beginPath(); x.arc(32, 32, 30, 0, 2 * Math.PI); x.fill();
  x.fillStyle = "#ffffff"; x.beginPath(); x.arc(32, 32, 22, 0, 2 * Math.PI); x.fill();
  return new THREE.CanvasTexture(c);
}

function visible(o) { return S.mode === "replay" || S.groups.has(o.category); }

function applyVisibility() {
  for (const o of S.objects) {
    if (!o.orbitLine) continue;
    const show = visible(o) && (S.orbitMode === "all" || (S.orbitMode === "selected" && (o === S.selected || S.mode === "replay")));
    o.orbitLine.visible = show;
    o.orbitLine.material.opacity = o === S.selected ? 0.75 : 0.28;
  }
}

// ---------- labels ----------
const labelHost = $("labels");
function drawLabels(positions) {
  labelHost.replaceChildren();
  const placed = [];  // [x, y] of labels drawn so far, to stack ones that would overlap
  const w = canvas.clientWidth, h = canvas.clientHeight;
  const tgt = S.mode === "replay" ? S.objects[0] : S.selected;
  const tgtPos = tgt ? positions.get(tgt) : null;
  for (const o of S.objects) {
    const show = o === S.selected || o === S.hovered || S.mode === "replay";
    const p = positions.get(o);
    if (!show || !p || !visible(o)) continue;
    toScene(p, tmp).project(camera);
    if (tmp.z > 1 || tmp.z < -1) continue;
    // hide labels for satellites behind the Earth
    const world = toScene(p);
    if (occluded(world)) continue;
    const div = el("div", { class: "lbl" + (o === S.selected ? " sel" : "") }, o.name || String(o.norad_id));
    if (S.mode === "replay" && o.role === "neighbor" && tgtPos) {
      const d = Math.hypot(p[0] - tgtPos[0], p[1] - tgtPos[1], p[2] - tgtPos[2]) * RE_KM;
      div.append(el("span", { class: "d" }, kmText(d)));
    }
    const x = ((tmp.x + 1) / 2) * w;
    let y = ((1 - tmp.y) / 2) * h;
    while (placed.some(([px, py]) => Math.abs(px - x) < 160 && Math.abs(py - y) < 16)) y += 16;
    placed.push([x, y]);
    div.style.left = `${x}px`;
    div.style.top = `${y}px`;
    labelHost.append(div);
  }
}
const ray = new THREE.Ray();
function occluded(world) {
  ray.origin.copy(camera.position);
  ray.direction.copy(world).sub(camera.position);
  const dist = ray.direction.length();
  ray.direction.normalize();
  const hit = ray.intersectSphere(new THREE.Sphere(new THREE.Vector3(), 0.999), tmp2);
  return hit && camera.position.distanceTo(hit) < dist;
}
const tmp2 = new THREE.Vector3();

// ---------- picking ----------
const raycaster = new THREE.Raycaster();
raycaster.params.Points.threshold = 0.04;
const mouse = new THREE.Vector2();
let downAt = null;
function pick(ev) {
  const r = canvas.getBoundingClientRect();
  mouse.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
  raycaster.setFromCamera(mouse, camera);
  raycaster.params.Points.threshold = 0.012 * camera.position.distanceTo(controls.target);
  const hits = points ? raycaster.intersectObject(points) : [];
  const hit = hits.find((x) => visible(S.objects[x.index]) && !occluded(x.point));
  return hit ? S.objects[hit.index] : null;
}
canvas.addEventListener("pointermove", (ev) => { S.hovered = pick(ev); canvas.style.cursor = S.hovered ? "pointer" : ""; });
canvas.addEventListener("pointerdown", (ev) => { downAt = [ev.clientX, ev.clientY]; });
canvas.addEventListener("pointerup", (ev) => {
  if (!downAt || Math.hypot(ev.clientX - downAt[0], ev.clientY - downAt[1]) > 4) return;  // a drag, not a click
  const o = pick(ev);
  if (o) { select(o, true); if (S.mode === "live") filterToSat(o); }
});

// ---------- selection & info ----------
function select(o, fly) {
  S.selected = o;
  $("pick").value = o && S.mode === "live" ? String(o.norad_id) : "";
  applyVisibility();
  renderInfo();
  if (o && fly) flyTo(o);
}

function flyTo(o) {
  const p = posAt(o, S.t);
  if (!p) return;
  const world = toScene(p);
  const r = world.length();
  const dist = S.mode === "replay" ? replayZoom() : Math.max(0.6, r * 0.45);
  const up = world.clone().normalize();
  let dir = up.clone().add(new THREE.Vector3(0, 0.35, 0)).normalize();
  if (S.mode === "replay") {
    // Look down on the orbit plane: co-orbital neighbours separate in-plane
    // (along-track and radially), which a view from straight above shows best.
    const p2 = posAt(o, S.t + S.data.step_s);
    if (p2) {
      const normal = world.clone().cross(toScene(p2).sub(world)).normalize();
      dir = normal.multiplyScalar(0.94).add(up.multiplyScalar(0.34)).normalize();
    }
  }
  controls.target.copy(world);
  camera.position.copy(world).add(dir.multiplyScalar(dist));
  controls.minDistance = S.mode === "replay" ? 1e-5 : 0.05;
  controls.update();
}

function orbitFacts(o, p, t) {
  // Speed from a 60 s difference, never straddling the replay's switch from
  // the pre-burn to the post-burn element set (that jump is not motion).
  const burn = S.mode === "replay" ? S.data.burn_t : null;
  const dt = burn !== null && t < burn && t + 60 >= burn ? -60 : 60;
  const p2 = posAt(o, t + dt);
  const r = Math.hypot(...p) * RE_KM;
  const v = p2 ? Math.hypot(p2[0] - p[0], p2[1] - p[1], p2[2] - p[2]) * RE_KM / 60 : null;
  const lon = ((Math.atan2(p[1], p[0]) - gmst(t)) * 180 / Math.PI + 540) % 360 - 180;
  const lat = Math.asin(p[2] / Math.hypot(...p)) * 180 / Math.PI;
  const a = v ? 1 / (2 / r - (v * v) / MU) : null;
  return { alt: r - RE_KM, v, lon, lat, period: a ? 2 * Math.PI * Math.sqrt(a ** 3 / MU) / 60 : null };
}

function renderInfo() {
  const box = $("info");
  const o = S.selected;
  if (!o) { box.hidden = true; return; }
  box.hidden = false;
  const p = posAt(o, S.t);
  const f = p ? orbitFacts(o, p, S.t) : null;
  const evs = S.events[o.norad_id] || [];
  box.replaceChildren(
    el("h3", {}, o.name || String(o.norad_id)),
    el("div", { class: "small muted" }, `NORAD ${o.norad_id}` + (o.category ? ` · ${GROUP_NAMES[o.category] || o.category}` : "") + (o.operator ? ` · ${o.operator}` : "")),
    el("dl", {},
      el("dt", {}, "Altitude"), el("dd", { id: "i-alt" }, f ? `${fmt(f.alt, 0)} km` : "–"),
      el("dt", {}, "Speed"), el("dd", { id: "i-v" }, f && f.v ? `${fmt(f.v, 2)} km/s` : "–"),
      el("dt", {}, "Over"), el("dd", { id: "i-ll" }, f ? `${fmt(f.lat, 1)}°, ${fmt(f.lon, 1)}°E` : "–"),
      el("dt", {}, "Period"), el("dd", {}, f && f.period ? `${fmt(f.period, 0)} min` : "–"),
      S.mode === "live" ? [el("dt", {}, "Elements from"), el("dd", {}, (o.epoch_utc || "").slice(0, 16).replace("T", " ") + "Z")] : null,
      el("dt", {}, "Maneuvers"), el("dd", {}, `${evs.length} detected, ${evs.filter((e) => !e.routine).length} not routine`)),
    S.sats[o.norad_id] ? el("a", { href: `dashboard.html#/sat/${o.norad_id}` }, "Orbit history and maneuvers →") : null);
}

function updateInfoLive(positions) {
  const o = S.selected;
  if (!o) return;
  const p = positions.get(o);
  if (!p || !$("i-alt")) return;
  const f = orbitFacts(o, p, S.t);
  $("i-alt").textContent = `${fmt(f.alt, 0)} km`;
  $("i-v").textContent = f.v ? `${fmt(f.v, 2)} km/s` : "–";
  $("i-ll").textContent = `${fmt(f.lat, 1)}°, ${fmt(f.lon, 1)}°E`;
}

// ---------- modes ----------
async function loadJSON(path) { const r = await fetch(path); if (!r.ok) throw new Error(path); return r.json(); }

async function startLive() {
  S.mode = "live";
  S.data = S.live || (S.live = await loadJSON("data/globe/live.json"));
  S.t0 = S.data.t0; S.t1 = S.data.t0 + S.data.step_s * (S.data.n - 1);
  S.t = Math.min(Math.max(Date.now() / 1000, S.t0), S.t1);
  S.speed = 60; S.playing = true;
  build(S.data.objects);
  $("live-controls").hidden = false; $("replay-controls").hidden = true; $("banner").hidden = true;
  $("mode-live").setAttribute("aria-selected", "true"); $("mode-replay").setAttribute("aria-selected", "false");
  geoRing.visible = true;
  select(null);
  controls.target.set(0, 0, 0); controls.minDistance = 1.15;
  camera.position.set(0, 5.5, 17);
  syncButtons();
}

async function startReplay(id) {
  const meta = S.replays.find((r) => r.id === id) || S.replays[0];
  if (!meta) return;
  $("replay-pick").value = meta.id;
  const d = await loadJSON(`data/globe/${meta.id}.json`);
  S.mode = "replay";  // only now: frames in between still draw the previous data
  d.objects.forEach((o) => {
    const s = S.sats[o.norad_id];
    o.name = o.name || (s && s.name) || String(o.norad_id);
    o.category = s ? s.category : null;
    o.operator = s ? s.operator : null;
  });
  S.data = d; S.replay = meta;
  S.t0 = d.t0; S.t1 = d.t0 + d.step_s * (d.n - 1);
  S.t = Math.max(S.t0, d.burn_t - 3 * 3600);
  const geo = (S.events[meta.norad_id] || []).find((e) => e.key === meta.key)?.regime === "geo";
  S.speed = geo ? 600 : 60; S.playing = true;
  build(d.objects);
  const target = d.objects[0];
  // before-burn orbit as a dashed grey line, after-burn orbit in the target's colour
  if (target.orbit_before) { const l = line(target.orbit_before, 0xc3c2b7, 0.5, true); scene.add(l); extraLines.push(l); }
  for (const n of d.objects.slice(1)) {
    const l = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]),
      new THREE.LineBasicMaterial({ color: 0xf0efec, transparent: true, opacity: 0.55 }));
    l.userData.n = n; scene.add(l); neighbourLines.push(l);
  }
  $("live-controls").hidden = true; $("replay-controls").hidden = false;
  $("mode-live").setAttribute("aria-selected", "false"); $("mode-replay").setAttribute("aria-selected", "true");
  geoRing.visible = geo;
  const ev = (S.events[meta.norad_id] || []).find((e) => e.key === meta.key);
  const tca = meta.tca_utc ? Date.parse(meta.tca_utc) / 1000 : null;
  $("jump-tca").disabled = !tca;
  $("replay-note").replaceChildren(
    `${LABELS[meta.label] || meta.label}, ${fmt(meta.dv_total_ms, meta.dv_total_ms < 1 ? 3 : 2)} m/s. `,
    `Closest: ${meta.closest_name || "neighbour"} at ${kmText(meta.closest_km)}`, tca ? `, ${iso(tca).slice(0, 16)}Z. ` : ". ",
    "Grey dashed line: orbit before the burn. ",
    ev ? el("a", { href: `dashboard.html#/event/${encodeURIComponent(meta.key)}` }, "Event details →") : null);
  select(target, true);
  syncButtons();
}

function replayZoom() {
  // Frame the closest neighbour at the current time, not the farthest: the interesting
  // object is usually a few km away while others in the slot are 1000+ km.
  const d = S.data, tgt = posAt(d.objects[0], S.t);
  let near = Infinity;
  for (const n of d.objects.slice(1)) {
    const p = posAt(n, S.t);
    if (p && tgt) near = Math.min(near, Math.hypot(p[0] - tgt[0], p[1] - tgt[1], p[2] - tgt[2]));
  }
  if (!Number.isFinite(near)) near = 200 / RE_KM;
  return Math.min(Math.max(near * 6, 15 / RE_KM), 3);
}

// ---------- UI wiring ----------
function syncButtons() {
  $("play").textContent = S.playing ? "Pause" : "Play";
  $("play").setAttribute("aria-pressed", String(S.playing));
  document.querySelectorAll("[data-speed]").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.speed) === S.speed)));
  $("speed-label").textContent = S.speed === 1 ? "real time" : `${S.speed}× real time`;
}
$("play").addEventListener("click", () => { S.playing = !S.playing; syncButtons(); });
document.querySelectorAll("[data-speed]").forEach((b) => b.addEventListener("click", () => { S.speed = Number(b.dataset.speed); S.playing = true; syncButtons(); }));
$("scrub").addEventListener("input", (ev) => { S.t = S.t0 + (S.t1 - S.t0) * (ev.target.value / 1000); });
$("mode-live").addEventListener("click", () => { EV.active = null; history.replaceState(null, "", location.pathname); startLive().then(renderEvents); });
function jumpTo(t) {
  S.t = Math.min(Math.max(t, S.t0), S.t1);
  if (S.selected) { lastFollow = null; flyTo(S.selected); }
}
$("jump-burn").addEventListener("click", () => jumpTo(S.data.burn_t - 1800));
$("jump-tca").addEventListener("click", () => { if (S.replay && S.replay.tca_utc) jumpTo(Date.parse(S.replay.tca_utc) / 1000 - 1800); });
const replayFromPicker = async (id) => {
  await startReplay(id);
  EV.active = S.replay ? S.replay.key : null;
  renderEvents();
};
$("mode-replay").addEventListener("click", () => replayFromPicker($("replay-pick").value));
$("replay-pick").addEventListener("change", (ev) => replayFromPicker(ev.target.value));
$("pick").addEventListener("change", (ev) => {
  const o = S.objects.find((x) => String(x.norad_id) === ev.target.value);
  select(o || null, true);
  filterToSat(o || null);
});
$("orbits").addEventListener("change", (ev) => { S.orbitMode = ev.target.value; applyVisibility(); });
$("follow").addEventListener("change", (ev) => { S.follow = ev.target.checked; });
document.querySelectorAll("[data-group]").forEach((c) => c.addEventListener("change", () => {
  c.checked ? S.groups.add(c.dataset.group) : S.groups.delete(c.dataset.group);
  if (S.selected && !visible(S.selected)) select(null);
  applyVisibility();
}));
$("collapse").addEventListener("click", () => {
  const p = $("panel"); p.classList.toggle("collapsed");
  $("collapse").setAttribute("aria-expanded", String(!p.classList.contains("collapsed")));
});

// ---------- event list ----------
// Beside the globe: every detected maneuver, newest first. Picking one plays its
// close-approach replay when there is one, otherwise flies to the satellite now.
const EV = { show: "notable", q: "", group: "_watch", sat: null, active: null, limit: 150, neighbors: {} };
const hex = (c) => "#" + c.toString(16).padStart(6, "0");
const dvText = (e) => `${fmt(e.dv_total_ms, e.dv_total_ms < 1 ? 3 : 2)} ± ${fmt(e.dv_sigma_ms, e.dv_sigma_ms < 1 ? 3 : 2)} m/s`;
const replayOf = (key) => S.replays.find((r) => r.key === key);

function nearestText(key) {
  const rows = EV.neighbors[key] || [];
  const docked = rows.find((n) => n.max_range_km !== null && n.max_range_km <= 1);
  if (docked) return `attached to ${docked.neighbor_name || docked.neighbor_norad}`;
  const free = rows.filter((n) => n.max_range_km === null || n.max_range_km > 1);
  if (!free.length) return null;
  const n = free.reduce((a, b) => (a.min_range_km <= b.min_range_km ? a : b));
  return `${kmText(n.min_range_km)} from ${n.neighbor_name || n.neighbor_norad}`;
}

function eventRows() {
  const q = EV.q.trim().toLowerCase();
  return S.allEvents.filter((e) => {
    const s = S.sats[e.norad_id];
    if (EV.sat !== null) return e.norad_id === EV.sat;
    if (EV.show === "notable" && e.routine) return false;
    if (EV.show === "replay" && !replayOf(e.key)) return false;
    if (EV.group === "_watch") { if (s && s.category === "starlink_sample") return false; }
    else if (EV.group && (!s || s.category !== EV.group)) return false;
    if (q && !`${s ? s.name : ""} ${e.norad_id}`.toLowerCase().includes(q)) return false;
    return true;
  });
}

function renderEvents() {
  const rows = eventRows();
  const s = EV.sat !== null ? S.sats[EV.sat] : null;
  const count = $("ev-count");
  count.replaceChildren(`${rows.length.toLocaleString()} maneuver${rows.length === 1 ? "" : "s"}`);
  if (s) {
    count.append(` for ${s.name} `, el("button", { type: "button", class: "linkish", id: "ev-clear" }, "show all"));
    $("ev-clear").addEventListener("click", () => { EV.sat = null; renderEvents(); });
  }
  document.querySelectorAll("[data-show]").forEach((b) => b.setAttribute("aria-pressed", String(EV.sat === null && b.dataset.show === EV.show)));
  const list = $("ev-list");
  list.replaceChildren(...rows.slice(0, EV.limit).map(eventItem));
  if (!rows.length) list.append(el("li", { class: "empty small muted" }, "Nothing matches."));
  if (rows.length > EV.limit) {
    const more = el("button", { type: "button", class: "more" }, `Show ${Math.min(150, rows.length - EV.limit)} more`);
    more.addEventListener("click", () => { EV.limit += 150; renderEvents(); });
    list.append(el("li", {}, more));
  }
}

function eventItem(e) {
  const s = S.sats[e.norad_id];
  const rp = replayOf(e.key);
  const near = nearestText(e.key);
  const on = e.key === EV.active;
  const btn = el("button", { type: "button", class: "ev", "aria-current": String(on) },
    el("span", { class: "ev-top" },
      el("span", { class: "dot", style: `background:${hex(colorOf(s || {}))}` }),
      el("strong", {}, s ? s.name : String(e.norad_id)),
      el("span", { class: "ev-date num" }, e.window_end_utc.slice(0, 10))),
    el("span", { class: "ev-mid" },
      el("span", { class: "chip" + (e.routine ? "" : " notable") }, LABELS[e.label] || e.label),
      el("span", { class: "num" }, dvText(e)),
      rp ? el("span", { class: "badge" }, "▶ Replay") : null),
    near ? el("span", { class: "ev-near small ink2" }, `Nearest: ${near}`) : null);
  btn.addEventListener("click", () => openEvent(e));
  const li = el("li", {}, btn);
  if (on) {
    const live = S.mode === "live";
    li.append(el("div", { class: "ev-detail small" },
      el("p", {}, `Burn between ${e.window_start_utc.slice(0, 16).replace("T", " ")}Z and ${e.window_end_utc.slice(0, 16).replace("T", " ")}Z. `,
        e.d_sma_km !== null ? `Mean altitude ${e.d_sma_km >= 0 ? "up" : "down"} ${fmt(Math.abs(e.d_sma_km), 2)} km. ` : "",
        e.plane_change ? `Plane change ${fmt(e.plane_change_deg, 3)}°. ` : ""),
      live ? el("p", { class: "muted" }, "No replay for this one, so the globe shows where the satellite is now.") : null,
      el("a", { href: `dashboard.html#/event/${encodeURIComponent(e.key)}` }, "Event details and orbit charts →")));
  }
  return li;
}

async function openEvent(e, scroll = true) {
  EV.active = e.key;
  history.replaceState(null, "", `?event=${encodeURIComponent(e.key)}`);
  const rp = replayOf(e.key);
  if (rp) await startReplay(rp.id);
  else {
    if (S.mode !== "live") await startLive();
    const o = S.objects.find((x) => x.norad_id === e.norad_id);
    if (o) {
      if (!S.groups.has(o.category)) {  // make sure its group is shown
        S.groups.add(o.category);
        const box = document.querySelector(`[data-group="${o.category}"]`);
        if (box) box.checked = true;
      }
      select(o, true);
    }
  }
  renderEvents();
  // stacked layout: the list is under the globe, so bring the globe into view
  if (scroll && innerWidth < 1000) $("stage").scrollIntoView({ behavior: "smooth", block: "start" });
}

function filterToSat(o) {
  EV.sat = o ? o.norad_id : null;
  EV.limit = 150;
  renderEvents();
}

document.querySelectorAll("[data-show]").forEach((b) => b.addEventListener("click", () => {
  EV.show = b.dataset.show; EV.sat = null; EV.limit = 150; renderEvents();
}));
$("ev-q").addEventListener("input", (ev) => { EV.q = ev.target.value; EV.sat = null; EV.limit = 150; renderEvents(); });
$("ev-group").addEventListener("change", (ev) => { EV.group = ev.target.value; EV.sat = null; EV.limit = 150; renderEvents(); });

// ---------- loop ----------
let lastLayout = "";
function resize() {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  // On wide screens the control panel covers the left edge; shift the view's
  // centre right by half its width so the focused satellite isn't under it.
  const panel = $("panel");
  const side = w > 760 && !panel.classList.contains("collapsed") ? panel.offsetWidth + 12 : 0;
  const layout = `${w}x${h}:${side}`;
  if (layout === lastLayout) return;
  lastLayout = layout;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  if (side) camera.setViewOffset(w + side, h, 0, 0, w, h); else camera.clearViewOffset();
  camera.updateProjectionMatrix();
}

let last = performance.now(), infoTick = 0, lastFollow = null;
function frame(now) {
  requestAnimationFrame(frame);  // first, so one bad frame can't stop the animation
  const dt = Math.min((now - last) / 1000, 0.1);
  last = now;
  if (S.data) {
    if (S.playing) {
      S.t += dt * S.speed;
      if (S.t > S.t1) S.t = S.mode === "replay" ? S.t0 : S.t1;
    }
    $("scrub").value = String(Math.round(((S.t - S.t0) / (S.t1 - S.t0)) * 1000));
    $("clock").textContent = iso(S.t);
    earth.rotation.y = gmst(S.t);
    const sd = sunDir(S.t);
    toScene(sd, sun.position).multiplyScalar(50);

    const positions = new Map();
    const arr = points.geometry.attributes.position.array;
    S.objects.forEach((o, i) => {
      const p = posAt(o, S.t);
      positions.set(o, p);
      if (p && visible(o)) toScene(p, tmp); else tmp.set(1e6, 1e6, 1e6);
      arr[i * 3] = tmp.x; arr[i * 3 + 1] = tmp.y; arr[i * 3 + 2] = tmp.z;
    });
    points.geometry.attributes.position.needsUpdate = true;
    points.geometry.computeBoundingSphere();

    // follow: move the camera with the selected satellite, keeping the user's view angle
    if (S.follow && S.selected && positions.get(S.selected)) {
      const w = toScene(positions.get(S.selected));
      if (lastFollow) camera.position.add(tmp2.copy(w).sub(lastFollow));
      controls.target.copy(w);
      lastFollow = w;
    } else lastFollow = null;

    if (S.mode === "replay") {
      const tp = positions.get(S.objects[0]);
      for (const l of neighbourLines) {
        const np = positions.get(l.userData.n);
        l.visible = !!(tp && np);
        if (l.visible) {
          const a = l.geometry.attributes.position.array;
          toScene(tp, tmp); a.set([tmp.x, tmp.y, tmp.z], 0);
          toScene(np, tmp); a.set([tmp.x, tmp.y, tmp.z], 3);
          l.geometry.attributes.position.needsUpdate = true;
        }
      }
      const burnShown = S.t >= S.data.burn_t;
      const banner = $("banner");
      banner.hidden = false;
      banner.textContent = burnShown
        ? `Burn detected: the first post-burn element set is from ${iso(S.data.burn_t)}. The orbit switches from the dashed line to the solid one.`
        : `Before the burn. Burn window ends ${iso(S.data.burn_t)}, in ${fmt((S.data.burn_t - S.t) / 3600, 1)} h.`;
    }
    // Labels must be projected with this frame's camera, after follow moved it;
    // a one-frame-stale matrix puts them ~30 km off at 600x in a 15 km view.
    resize();
    controls.update();
    camera.updateMatrixWorld();
    drawLabels(positions);
    if (++infoTick % 10 === 0) updateInfoLive(positions);
  } else {
    resize();
    controls.update();
  }
  renderer.render(scene, camera);
}

// ---------- boot ----------
if (innerWidth <= 760 || (innerWidth >= 1000 && innerWidth < 1280)) {
  // On phones the panel would cover the Earth; on mid-size screens the event list
  // already takes the right side. Start it folded in both.
  $("panel").classList.add("collapsed");
  $("collapse").setAttribute("aria-expanded", "false");
}
(async () => {
  try {
    const [index, replays] = await Promise.all([loadJSON("data/index.json"), loadJSON("data/globe/replays.json")]);
    for (const s of index.satellites) S.sats[s.norad_id] = s;
    for (const e of index.events) (S.events[e.norad_id] ||= []).push(e);
    S.allEvents = index.events.slice().sort((a, b) => (a.window_end_utc < b.window_end_utc ? 1 : -1));
    EV.neighbors = index.neighbors;
    S.replays = replays;
    const notable = index.events.filter((e) => !e.routine).length;
    $("ev-stats").textContent = `${index.events.length.toLocaleString()} found across ${index.satellites.length} satellites, ${notable} not routine upkeep, `
      + `${index.campaigns.length} multi-burn campaigns. Checked against NASA: ${index.validation.detected} of ${index.validation.total} ISS reboosts caught.`;
    $("generated").textContent = `Data generated ${index.generated_utc.slice(0, 16).replace("T", " ")}Z.`;
    const name = (id) => (S.sats[id] ? S.sats[id].name : String(id));
    $("replay-pick").replaceChildren(...replays.map((r) => el("option", { value: r.id },
      `${name(r.norad_id)} · ${r.window_end_utc.slice(0, 10)} · nearest ${kmText(r.closest_km)}`)));
    await startLive();
    const live = S.live.objects.slice().sort((a, b) => a.name.localeCompare(b.name));
    $("pick").replaceChildren(el("option", { value: "" }, "Choose…"), ...live.map((o) => el("option", { value: String(o.norad_id) }, o.name)));
    renderEvents();
    const params = new URLSearchParams(location.search);
    const wantEvent = params.get("event") && S.allEvents.find((e) => e.key === params.get("event"));
    if (wantEvent) {
      if (wantEvent.routine) EV.show = "all";
      await openEvent(wantEvent, false);
      document.querySelector('#ev-list [aria-current="true"]')?.scrollIntoView({ block: "nearest" });
    } else if (params.get("replay")) await startReplay(params.get("replay"));
    $("loading").hidden = true;
  } catch (err) {
    $("loading").textContent = `Could not load globe data (${err.message}). Run scripts/build_site.py.`;
  }
  requestAnimationFrame(frame);
})();
