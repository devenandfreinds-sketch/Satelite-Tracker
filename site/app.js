/* Satelite Tracker dashboard. Static: reads data/index.json and data/sat/<norad>.json,
   written by scripts/build_site.py from publishable (CelesTrak-origin) data only.
   All data strings go into the DOM via textContent. */
(() => {
  "use strict";

  const LABELS = {
    geo_ew_stationkeeping: "GEO east-west station-keeping",
    geo_ns_stationkeeping: "GEO north-south station-keeping",
    geo_drift_start: "GEO drift start",
    geo_drift_stop: "GEO drift stop",
    geo_drift_change: "GEO drift change",
    geo_relocation: "GEO relocation",
    geo_plane_change: "GEO plane change",
    geo_maneuver: "GEO maneuver",
    altitude_maintenance: "Altitude maintenance",
    orbit_raise: "Orbit raise",
    orbit_lowering: "Orbit lowering",
    circularization: "Circularization",
    eccentricity_raise: "Eccentricity raise",
    plane_change: "Plane change",
  };
  const CATEGORIES = {
    geo_inspector: "GEO inspectors",
    leo_shadowing: "LEO close approaches",
    validation: "Validation (ISS)",
    starlink_sample: "Starlink sample",
  };
  const METHODS = {
    colocated: "Same GEO longitude slot",
    coplanar: "Same orbit plane",
    crossing: "Crossing approach",
  };
  const RE_KM = 6378.135;
  const DAY = 86400;

  let D = null;            // index.json
  const satCache = {};     // norad -> series
  const charts = [];       // live uPlot instances (destroyed on navigation)
  const state = { q: "", cat: "_watch", label: "", routine: false, preset: "all", from: "", to: "", minDv: "",
                  sort: { key: "window_end_utc", dir: -1 } };

  // ---------- helpers ----------
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toFixed(d));
  const signed = (v, d = 2) => (v === null || v === undefined ? "–" : (v > 0 ? "+" : "") + Number(v).toFixed(d));
  const dateOf = (iso) => (iso ? iso.slice(0, 10) : "–");
  const dt = (iso) => (iso ? iso.slice(0, 16).replace("T", " ") + "Z" : "–");
  const ts = (iso) => Date.parse(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z") / 1000;
  const sat = (id) => D.satellites.find((s) => s.norad_id === id);
  const satName = (id) => (sat(id) ? sat(id).name : String(id));
  const labelName = (l) => LABELS[l] || l || "–";
  const evHref = (key) => "#/event/" + encodeURIComponent(key);
  const dvText = (e) => `${fmt(e.dv_total_ms, e.dv_total_ms < 1 ? 3 : 2)} ± ${fmt(e.dv_sigma_ms, e.dv_sigma_ms < 1 ? 3 : 2)}`;
  const attached = (key) => (D.neighbors[key] || []).find((n) => n.max_range_km !== null && n.max_range_km <= 1);
  const nearestText = (key, withName) => {
    const a = attached(key);
    if (a) return `attached: ${a.neighbor_name || a.neighbor_norad}`;
    const n = nearest(key);
    if (!n) return "–";
    const km = fmt(n.min_range_km, n.min_range_km < 10 ? 1 : 0);
    return withName ? `${n.neighbor_name || n.neighbor_norad}, ${km} km` : km;
  };
  const nearest = (key) => {
    const rows = (D.neighbors[key] || []).filter((n) => n.max_range_km === null || n.max_range_km > 1);
    return rows.length ? rows.reduce((a, b) => (a.min_range_km <= b.min_range_km ? a : b)) : null;
  };

  function clearCharts() { while (charts.length) charts.pop().destroy(); }

  async function loadSat(id) {
    if (!satCache[id]) {
      const r = await fetch(`data/sat/${id}.json`);
      if (!r.ok) throw new Error(`no history for ${id}`);
      satCache[id] = await r.json();
    }
    return satCache[id];
  }

  function chip(e) {
    const kids = [h("span", { class: "chip" + (e.routine ? "" : " notable") }, labelName(e.label))];
    if (e.status === "provisional") kids.push(" ", h("span", { class: "chip prov", title: "Next element set not in yet" }, "provisional"));
    return kids;
  }

  // ---------- sortable table ----------
  function table(cols, rows, { onRow, sort, onSort, empty = "Nothing matches these filters." } = {}) {
    const thead = h("tr", {}, cols.map((c) => {
      const s = sort && sort.key === c.key ? (sort.dir > 0 ? "ascending" : "descending") : null;
      return h("th", { class: c.right ? "r" : null, "aria-sort": s, scope: "col" },
        c.sortable === false || !onSort ? c.title : h("button", { onclick: () => onSort(c.key) }, c.title));
    }));
    const body = rows.length ? rows.map((r) => {
      const tr = h("tr", { class: onRow ? "click" : null }, cols.map((c) =>
        h("td", { class: [c.right ? "r num" : "", c.wrap ? "wrap-cell" : ""].join(" ").trim() || null }, c.render ? c.render(r) : r[c.key] ?? "–")));
      if (onRow) {
        tr.tabIndex = 0;
        tr.addEventListener("click", () => onRow(r));
        tr.addEventListener("keydown", (ev) => { if (ev.key === "Enter") onRow(r); });
      }
      return tr;
    }) : [h("tr", {}, h("td", { colspan: cols.length, class: "empty" }, empty))];
    return h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, thead), h("tbody", {}, body)));
  }

  function sortRows(rows, { key, dir }) {
    return rows.slice().sort((a, b) => {
      const x = a[key], y = b[key];
      if (x === y) return 0;
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (x > y ? 1 : -1) * dir;
    });
  }

  // ---------- chart with burn bands ----------
  function bandsPlugin(events, onPick) {
    return {
      hooks: {
        drawClear: (u) => {
          const ctx = u.ctx;
          ctx.save();
          for (const e of events) {
            const x0 = u.valToPos(ts(e.window_start_utc), "x", true);
            const x1 = u.valToPos(ts(e.window_end_utc), "x", true);
            if (x1 < u.bbox.left || x0 > u.bbox.left + u.bbox.width) continue;
            ctx.fillStyle = e.routine ? css("--routine-band") : css("--notable-band");
            const left = Math.max(x0, u.bbox.left);
            const w = Math.max(Math.min(x1, u.bbox.left + u.bbox.width) - left, 2 * devicePixelRatio);
            ctx.fillRect(left, u.bbox.top, w, u.bbox.height);
          }
          ctx.restore();
        },
        ready: (u) => {
          u.over.addEventListener("click", () => {
            const t = u.posToVal(u.cursor.left, "x");
            const hit = events.find((e) => t >= ts(e.window_start_utc) - 3600 * 6 && t <= ts(e.window_end_utc) + 3600 * 6);
            if (hit) onPick(hit);
          });
        },
      },
    };
  }

  function timeChart(host, { title, t, y, unit, digits = 3, events = [], range = null }) {
    const HINT = "Hover the chart for values. Shaded bands are burn windows; click one to open it.";
    const readout = h("div", { class: "readout" }, HINT);
    const box = h("div", {});
    const card = h("div", { class: "chart" }, h("div", { class: "ctitle" }, title), box, readout);
    host.append(card);
    const width = () => Math.max(280, card.clientWidth - 16);
    const opts = {
      width: width(), height: 230,
      scales: { x: { time: true, ...(range ? { min: range[0], max: range[1] } : {}) } },
      series: [{}, { label: title, stroke: css("--series"), width: 1.5, points: { show: t.length < 200, size: 4 } }],
      axes: [
        { stroke: css("--muted"), grid: { stroke: css("--grid"), width: 1 }, ticks: { stroke: css("--axis") } },
        { stroke: css("--muted"), grid: { stroke: css("--grid"), width: 1 }, ticks: { stroke: css("--axis") }, size: 64,
          values: (u, vals) => vals.map((v) => v.toFixed(Math.max(0, Math.min(4, digits - 1)))) },
      ],
      legend: { show: false },
      cursor: { y: false, points: { size: 7 } },
      plugins: [bandsPlugin(events, (e) => { location.hash = evHref(e.key); })],
      hooks: {
        setCursor: [(u) => {
          const i = u.cursor.idx;
          readout.textContent = "";
          if (i === null || i === undefined) { readout.textContent = HINT; return; }
          const at = u.data[0][i];
          const inside = events.find((e) => at >= ts(e.window_start_utc) && at <= ts(e.window_end_utc));
          readout.append(h("strong", {}, `${fmt(u.data[1][i], digits)} ${unit}`), "  ",
            new Date(at * 1000).toISOString().slice(0, 16).replace("T", " ") + "Z",
            inside ? `  ·  ${labelName(inside.label)}, ${dvText(inside)} m/s` : "");
        }],
      },
    };
    const u = new uPlot(opts, [t, y], box);
    charts.push(u);
    new ResizeObserver(() => u.setSize({ width: width(), height: 230 })).observe(card);
    return u;
  }

  function bandLegend() {
    return h("div", { class: "legend" },
      h("span", {}, h("i", { style: "background:var(--notable-band)" }), "Notable burn window"),
      h("span", {}, h("i", { style: "background:var(--routine-band)" }), "Routine (station-keeping, altitude upkeep)"));
  }

  async function orbitCharts(host, id, events, range = null) {
    let s;
    try { s = await loadSat(id); } catch { host.append(h("p", { class: "muted" }, "No public history for this object yet.")); return; }
    host.append(bandLegend());
    if (s.regime === "geo") {
      timeChart(host, { title: "Mean longitude (deg east)", t: s.t, y: s.lon_deg, unit: "°E", digits: 3, events, range });
    } else {
      timeChart(host, { title: "Mean altitude (km above equatorial radius)", t: s.t, y: s.sma_km.map((a) => a - RE_KM), unit: "km", digits: 2, events, range });
    }
    timeChart(host, { title: "Inclination (deg)", t: s.t, y: s.inc_deg, unit: "°", digits: 4, events, range });
  }

  // ---------- views ----------
  function filteredEvents() {
    const now = Date.now() / 1000;
    const span = { "30": 30, "90": 90, "365": 365 }[state.preset];
    const from = state.preset === "custom" && state.from ? Date.parse(state.from) / 1000 : span ? now - span * DAY : null;
    const to = state.preset === "custom" && state.to ? Date.parse(state.to) / 1000 + DAY : null;
    const q = state.q.trim().toLowerCase();
    return D.events.filter((e) => {
      const s = sat(e.norad_id);
      if (!state.routine && e.routine) return false;
      if (state.cat === "_watch") { if (s && s.category === "starlink_sample") return false; }
      else if (state.cat && (!s || s.category !== state.cat)) return false;
      if (state.label && e.label !== state.label) return false;
      if (state.minDv && !(e.dv_total_ms >= Number(state.minDv))) return false;
      const t = ts(e.window_end_utc);
      if (from && t < from) return false;
      if (to && t > to) return false;
      if (q && !(`${satName(e.norad_id)} ${e.norad_id}`.toLowerCase().includes(q))) return false;
      return true;
    }).map((e) => ({ ...e, sat_name: satName(e.norad_id),
      nearest_km: attached(e.key) ? 0 : (nearest(e.key) || {}).min_range_km ?? null }));
  }

  function viewEvents(app) {
    const total = D.events.length, notable = D.events.filter((e) => !e.routine).length;
    app.append(h("div", { class: "tiles" },
      tile(total.toLocaleString(), "maneuvers detected, all 73 objects"),
      tile(notable.toLocaleString(), "not routine upkeep"),
      tile(D.campaigns.length.toLocaleString(), "multi-burn campaigns"),
      tile(`${D.validation.detected}/${D.validation.total}`, "NASA-reported ISS burns detected")));

    const labels = [...new Set(D.events.map((e) => e.label))].sort();
    const presetBtn = (v, t) => h("button", { type: "button", "aria-pressed": String(state.preset === v),
      onclick: () => { state.preset = v; route(); } }, t);
    const filters = h("div", { class: "filters", role: "search" },
      h("div", { class: "presets", role: "group", "aria-label": "Date range" },
        presetBtn("all", "All"), presetBtn("30", "30 days"), presetBtn("90", "90 days"), presetBtn("365", "1 year"), presetBtn("custom", "Custom")),
      state.preset === "custom" ? [
        h("label", {}, "From", h("input", { type: "date", value: state.from, onchange: (ev) => { state.from = ev.target.value; route(); } })),
        h("label", {}, "To", h("input", { type: "date", value: state.to, onchange: (ev) => { state.to = ev.target.value; route(); } }))] : null,
      h("label", {}, "Satellite", h("input", { type: "search", placeholder: "Name or NORAD ID", value: state.q,
        oninput: (ev) => { state.q = ev.target.value; renderTable(); } })),
      h("label", {}, "Group", select([["_watch", "Watch targets (no Starlink sample)"], ...Object.entries(CATEGORIES)], state.cat, "All groups", (v) => { state.cat = v; renderTable(); })),
      h("label", {}, "Type", select(labels.map((l) => [l, labelName(l)]), state.label, "All types", (v) => { state.label = v; renderTable(); })),
      h("label", {}, "Min Δv (m/s)", h("input", { type: "number", min: "0", step: "0.1", value: state.minDv, style: "width:96px",
        oninput: (ev) => { state.minDv = ev.target.value; renderTable(); } })),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: state.routine,
        onchange: (ev) => { state.routine = ev.target.checked; renderTable(); } }), "Include routine upkeep"));
    app.append(filters);
    const count = h("p", { class: "muted", style: "margin:4px 0 8px" });
    const holder = h("div", {});
    app.append(count, holder);

    function renderTable() {
      const rows = sortRows(filteredEvents(), state.sort);
      count.textContent = `${rows.length.toLocaleString()} events` + (state.routine ? "" : " (routine upkeep hidden)");
      holder.replaceChildren(table([
        { key: "window_end_utc", title: "Detected", render: (e) => dateOf(e.window_end_utc) },
        { key: "sat_name", title: "Satellite", render: (e) => e.sat_name },
        { key: "label", title: "Type", render: chip },
        { key: "dv_total_ms", title: "Δv (m/s, ±1σ)", right: true, render: dvText },
        { key: "d_sma_km", title: "ΔSMA (km)", right: true, render: (e) => signed(e.d_sma_km, 2) },
        { key: "plane_change_deg", title: "Plane Δ (°)", right: true, render: (e) => (e.plane_change ? fmt(e.plane_change_deg, 3) : "–") },
        { key: "nearest_km", title: "Nearest object (km)", right: true, render: (e) => nearestText(e.key, false) },
      ], rows, { onRow: (e) => { location.hash = evHref(e.key); }, sort: state.sort,
        onSort: (k) => { state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : -1 }; renderTable(); } }));
    }
    renderTable();
  }

  function tile(v, k) { return h("div", { class: "tile" }, h("div", { class: "v num" }, v), h("div", { class: "k" }, k)); }
  function select(opts, value, allText, onchange) {
    return h("select", { onchange: (ev) => onchange(ev.target.value) },
      h("option", { value: "" }, allText), opts.map(([v, t]) => h("option", { value: v, selected: v === value }, t)));
  }

  async function viewEvent(app, key) {
    const e = D.events.find((x) => x.key === key);
    if (!e) { app.append(h("p", {}, "Event not found. "), h("a", { href: "#/events" }, "Back to events")); return; }
    const s = sat(e.norad_id);
    app.append(h("a", { class: "back", href: "#/events" }, "← All events"));
    app.append(h("h2", {}, `${s ? s.name : e.norad_id}: ${labelName(e.label)}`));
    app.append(h("p", { class: "ink2", style: "margin-top:0" }, `Burn between ${dt(e.window_start_utc)} and ${dt(e.window_end_utc)}. `,
      e.status === "confirmed" ? `Confirmed by the set at ${dt(e.confirmed_at_utc)}.` : "Provisional: the next element set has not arrived yet.",
      e.campaign_id ? [" Part of ", h("a", { href: "#/campaigns" }, "a multi-burn campaign"), "."] : ""));

    const geo = e.regime === "geo";
    app.append(h("div", { class: "grid2" },
      h("div", { class: "card" }, h("h3", { style: "margin-top:0" }, "Burn"), h("dl", { class: "kv" },
        h("dt", {}, "Δv, minimum impulsive"), h("dd", {}, `${dvText(e)} m/s`),
        h("dt", {}, "In-plane part"), h("dd", {}, `${fmt(e.dv_inplane_ms, 3)} m/s`),
        h("dt", {}, "Plane change"), h("dd", {}, e.plane_change ? `${fmt(e.plane_change_deg, 3)}° (${fmt(e.dv_plane_ms, 2)} m/s)` : "not significant"),
        h("dt", {}, "Change in mean SMA"), h("dd", {}, `${signed(e.d_sma_km, 3)} km`),
        h("dt", {}, "Detection strength"), h("dd", {}, `element z ${fmt(e.max_z, 0)}, position z ${fmt(e.z_pos, 0)}`),
        h("dt", {}, "Element sets spanned"), h("dd", {}, String(e.n_pairs)))),
      h("div", { class: "card" }, h("h3", { style: "margin-top:0" }, "Orbit before → after"), h("dl", { class: "kv" },
        h("dt", {}, "Perigee altitude"), h("dd", {}, `${fmt(e.before_perigee_km, 1)} → ${fmt(e.after_perigee_km, 1)} km`),
        h("dt", {}, "Apogee altitude"), h("dd", {}, `${fmt(e.before_apogee_km, 1)} → ${fmt(e.after_apogee_km, 1)} km`),
        h("dt", {}, "Inclination"), h("dd", {}, `${fmt(e.before_inc_deg, 4)} → ${fmt(e.after_inc_deg, 4)}°`),
        geo ? [h("dt", {}, "Longitude"), h("dd", {}, `${fmt(e.lon_before_deg, 3)} → ${fmt(e.lon_after_deg, 3)}°E`),
               h("dt", {}, "Drift rate"), h("dd", {}, `${signed(e.drift_before_deg_day, 3)} → ${signed(e.drift_after_deg_day, 3)} °/day`)] : null))));

    const nb = D.neighbors[key] || [];
    app.append(h("h3", {}, "Closest objects in the 24 h after the burn"));
    if (!nb.length) {
      app.append(h("p", { class: "muted" }, e.routine && !(s && s.category === "leo_shadowing")
        ? "Not screened: routine upkeep events are skipped." : "No similar-orbit object found in the public catalog."));
    } else {
      app.append(table([
        { key: "neighbor_name", title: "Object", render: (n) => [n.neighbor_name || String(n.neighbor_norad), " ",
          h("span", { class: "muted" }, `#${n.neighbor_norad}`)] },
        { key: "method", title: "Why it's listed", render: (n) => METHODS[n.method] || n.method },
        { key: "min_range_km", title: "Closest (km)", right: true, render: (n) => (n.max_range_km !== null && n.max_range_km < 1 ? "attached" : fmt(n.min_range_km, n.min_range_km < 10 ? 1 : 0)) },
        { key: "tca_utc", title: "When", render: (n) => dt(n.tca_utc) },
        geo ? { key: "lon_sep_deg", title: "Longitude gap (°)", right: true, render: (n) => fmt(n.lon_sep_deg, 3) } : null,
        { key: "elements_age_h", title: "Set age (h)", right: true, render: (n) => fmt(n.elements_age_h, 0) },
      ].filter(Boolean), nb));
      app.append(h("p", { class: "caveat" }, "Ranges come from public element sets and carry their errors: about a kilometre for fresh LEO sets, tens of km in GEO, more as the neighbour's set ages. "
        + "“Attached” means the two stayed within 1 km for the whole day (docked, or catalogued as one). LEO screening covers active payloads only; debris and rocket bodies are not in a public LEO list."));
    }

    app.append(h("h3", {}, "Orbit history around the burn"));
    const mine = D.events.filter((x) => x.norad_id === e.norad_id);
    const mid = (ts(e.window_start_utc) + ts(e.window_end_utc)) / 2;
    await orbitCharts(app, e.norad_id, mine, [mid - 20 * DAY, mid + 20 * DAY]);
    app.append(h("p", {}, h("a", { href: `#/sat/${e.norad_id}` }, `Full history for ${s ? s.name : e.norad_id} →`)));
  }

  function viewSatellites(app) {
    app.append(h("h2", {}, "Watchlist"));
    for (const [cat, title] of Object.entries(CATEGORIES)) {
      const list = D.satellites.filter((s) => s.category === cat);
      if (!list.length) continue;
      app.append(h("h3", {}, `${title} (${list.length})`));
      app.append(h("div", { class: "sat-grid" }, list.map((s) => h("a", { class: "sat-card", href: `#/sat/${s.norad_id}` },
        h("div", { class: "n" }, s.name), h("div", { class: "m" }, `#${s.norad_id} · ${s.operator || "–"}`),
        h("div", { class: "m" }, s.has_series ? `${s.n_events} maneuvers, ${s.n_notable} not routine` : "No public history yet")))));
    }
  }

  async function viewSat(app, id) {
    const s = sat(id);
    if (!s) { app.append(h("p", {}, "Satellite not on the watchlist.")); return; }
    app.append(h("a", { class: "back", href: "#/satellites" }, "← Watchlist"));
    app.append(h("h2", {}, s.name));
    app.append(h("p", { class: "ink2", style: "margin-top:0" }, `NORAD ${s.norad_id} · ${s.cospar_id} · ${s.operator || "–"} · ${CATEGORIES[s.category] || s.category}`));
    if (s.notes) app.append(h("p", { class: "caveat" }, s.notes));
    const mine = D.events.filter((e) => e.norad_id === id);
    await orbitCharts(app, id, mine);
    app.append(h("h3", {}, `Maneuvers (${mine.length})`));
    app.append(table([
      { key: "window_end_utc", title: "Detected", render: (e) => dateOf(e.window_end_utc) },
      { key: "label", title: "Type", render: chip },
      { key: "dv_total_ms", title: "Δv (m/s, ±1σ)", right: true, render: dvText },
      { key: "d_sma_km", title: "ΔSMA (km)", right: true, render: (e) => signed(e.d_sma_km, 2) },
      { key: "nearest", title: "Nearest object", render: (e) => nearestText(e.key, true) },
    ], sortRows(mine, { key: "window_end_utc", dir: -1 }), { onRow: (e) => { location.hash = evHref(e.key); }, empty: "No maneuvers detected." }));
  }

  function viewCampaigns(app) {
    app.append(h("h2", {}, "Multi-burn campaigns"));
    app.append(h("p", { class: "ink2 prose" }, "A run of non-routine burns on one object, no more than three days apart, or one burn sequence the detector merged because it spanned several element sets."));
    const rows = sortRows(D.campaigns.map((c) => ({ ...c, sat_name: satName(c.norad_id) })), { key: "total_dv_ms", dir: -1 });
    app.append(table([
      { key: "sat_name", title: "Satellite", render: (c) => c.sat_name },
      { key: "start_utc", title: "Start", render: (c) => dateOf(c.start_utc) },
      { key: "span_days", title: "Days", right: true, render: (c) => fmt(c.span_days, 1) },
      { key: "n_events", title: "Burns", right: true },
      { key: "total_dv_ms", title: "Total Δv (m/s)", right: true, render: (c) => `${fmt(c.total_dv_ms, 1)} ± ${fmt(c.total_dv_sigma_ms, 1)}` },
      { key: "net_d_sma_km", title: "Net ΔSMA (km)", right: true, render: (c) => signed(c.net_d_sma_km, 1) },
      { key: "labels", title: "Burn types", wrap: true, render: (c) => c.labels.split(",").map(labelName).join(", ") },
    ], rows, { onRow: (c) => {
      const first = D.events.find((e) => e.campaign_id === c.campaign_id);
      if (first) location.hash = evHref(first.key);
    }, empty: "No campaigns detected." }));
  }

  function viewValidation(app) {
    const v = D.validation;
    app.append(h("h2", {}, "Validation: ISS reboosts"));
    app.append(h("p", { class: "ink2 prose" }, `NASA publishes ISS reboosts, so the ISS is the test the detector must pass before it is trusted elsewhere. `
      + `The ground truth below was compiled from NASA's Space Station blog independently of the detector. `
      + `${v.detected} of ${v.total} burns were detected, with ${v.unmatched_event_keys.length} events NASA does not list. `
      + `This is an in-sample result: the method was designed on this same window, and new reboosts are the real test.`));
    app.append(h("div", { class: "tiles" },
      tile(`${v.detected}/${v.total}`, "NASA burns detected"),
      tile(String(v.unmatched_event_keys.length), "events not in NASA's list"),
      tile(`${fmt(median(v.burns.map((b) => b.lag_h).filter((x) => x !== null)), 0)} h`, "median burn date to first set showing it")));
    app.append(table([
      { key: "burn_date", title: "NASA burn date" },
      { key: "vehicle", title: "Vehicle" },
      { key: "duration", title: "Duration" },
      { key: "event_key", title: "Detected", render: (b) => (b.event_key ? h("a", { href: evHref(b.event_key) }, "yes") : "missed") },
      { key: "d_sma_km", title: "Detector ΔSMA (km)", right: true, render: (b) => signed(b.d_sma_km, 2) },
      { key: "nasa_mean_alt_change_km", title: "NASA stated (km)", right: true, render: (b) => (b.nasa_mean_alt_change_km === null ? "–" : signed(b.nasa_mean_alt_change_km, 2)) },
      { key: "dv_total_ms", title: "Δv (m/s)", right: true, render: (b) => (b.dv_total_ms === null ? "–" : `${fmt(b.dv_total_ms, 2)} ± ${fmt(b.dv_sigma_ms, 2)}`) },
      { key: "source_url", title: "Source", render: (b) => h("a", { href: b.source_url, rel: "noopener" }, "NASA") },
    ], v.burns));
    app.append(h("h3", {}, "Detector vs NASA, full window"));
    app.append(h("img", { class: "fig", src: "img/iss_detector_overview_publishable.png", alt: "ISS mean semi-major axis, position-residual z and element-step z over 17 months, with NASA burns and detector events marked", loading: "lazy" }));
  }

  function median(xs) { if (!xs.length) return null; const s = xs.slice().sort((a, b) => a - b); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; }

  function viewAbout(app) {
    app.append(h("div", { class: "prose" },
      h("h2", {}, "How this works"),
      h("ol", {},
        h("li", {}, "Every day we store the newest public element set (TLE/GP) for each watchlist object from CelesTrak, and keep all past ones."),
        h("li", {}, "Each new set is compared with the previous one: the old set is propagated with SGP4 to the new epoch, and the mean orbital elements are differenced."),
        h("li", {}, "Each object gets its own noise model from its recent history. A burn is flagged only when an element change and the position miss it should cause are both at least 10 times that noise, and the change persists into the next set."),
        h("li", {}, "Δv is the minimum impulsive burn consistent with the change, with a 1σ uncertainty. On the ISS it agrees with NASA's stated orbit changes to about 0.1 km."),
        h("li", {}, "Each notable burn is screened against the public catalog for objects in the same GEO slot or orbit plane, using element sets from the time of the burn.")),
      h("h3", {}, "Limits"),
      h("ul", {},
        h("li", {}, "Public element sets are accurate to about a kilometre in LEO and tens of km in GEO, and arrive a day or more after a burn."),
        h("li", {}, "Some satellites, including classified US payloads such as USA 314 and USA 338, have no public element sets, so approaches to them cannot be measured here."),
        h("li", {}, "“Routine” labels describe the orbit change, not intent. Every event is a lead until confirmed by other sources.")),
      h("p", {}, "Full method and sources: ", h("a", { href: "https://github.com/devenandfreinds-sketch/Satelite-Tracker/blob/main/docs/METHOD.md" }, "docs/METHOD.md"), ".")));
  }

  // ---------- routing ----------
  async function route() {
    clearCharts();
    const app = document.getElementById("app");
    app.replaceChildren();
    const [, tab = "events", arg] = (location.hash || "#/events").split("/");
    const active = { event: "events", sat: "satellites" }[tab] || tab;
    document.querySelectorAll("nav a").forEach((a) => a.setAttribute("aria-current", a.dataset.tab === active ? "page" : "false"));
    try {
      if (tab === "event") await viewEvent(app, decodeURIComponent(arg || ""));
      else if (tab === "sat") await viewSat(app, Number(arg));
      else if (tab === "satellites") viewSatellites(app);
      else if (tab === "campaigns") viewCampaigns(app);
      else if (tab === "validation") viewValidation(app);
      else if (tab === "about") viewAbout(app);
      else viewEvents(app);
    } catch (err) {
      app.append(h("p", {}, `Something went wrong: ${err.message}`));
    }
    if (tab === "event" || tab === "sat") window.scrollTo(0, 0);
  }

  async function init() {
    try {
      const r = await fetch("data/index.json");
      D = await r.json();
    } catch {
      document.getElementById("app").replaceChildren(h("p", {}, "Could not load data/index.json. Run scripts/build_site.py, then serve this folder."));
      return;
    }
    document.getElementById("generated").textContent = `Data generated ${dt(D.generated_utc)} · ${D.events.length.toLocaleString()} events`;
    window.addEventListener("hashchange", route);
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", route);
    route();
  }
  init();
})();
