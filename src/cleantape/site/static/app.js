/* Clean Tape — small vanilla JS: terminal chrome (clock, F-keys, ticker box), Plotly charts themed
   from CSS tokens, Markets picker, Health filter. Everything degrades: the pages work without JS. */
(function () {
  "use strict";
  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const $ = (id) => document.getElementById(id);
  const readJSON = (id) => { const el = $(id); return el ? JSON.parse(el.textContent) : null; };

  function layout(extra) {
    const ink2 = css("--ink-2"), grid = css("--grid"), axis = css("--axis");
    return Object.assign({
      paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
      font: { family: "IBM Plex Mono, ui-monospace, Menlo, monospace", size: 11, color: ink2 },
      margin: { l: 48, r: 12, t: 8, b: 32 }, hovermode: "x unified", showlegend: false,
      xaxis: { gridcolor: grid, linecolor: axis, zeroline: false },
      yaxis: { gridcolor: grid, linecolor: axis, zeroline: false },
      hoverlabel: { bgcolor: "#000", bordercolor: css("--amber"), font: { color: css("--ink"), family: "IBM Plex Mono, monospace" } },
    }, extra || {});
  }
  const config = { displaylogo: false, responsive: true, modeBarButtonsToRemove: ["lasso2d", "select2d"] };
  const plot = (id, data, extra) => { if ($(id) && window.Plotly) Plotly.newPlot(id, data, layout(extra), config); };
  // Line with a soft area fill down to the series minimum (not to zero, which would squash
  // yields/prices). Implemented as an invisible baseline trace + fill: "tonexty".
  const hexA = (hex, a) => { const n = parseInt(hex.replace("#", ""), 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`; };
  function area(x, y, name, color, extra) {
    const vals = y.filter((v) => v != null);
    const lo = vals.length ? Math.min(...vals) : 0;
    const base = { x, y: x.map(() => lo), type: "scatter", mode: "lines", line: { width: 0 },
      hoverinfo: "skip", showlegend: false };
    return [base, line(x, y, name, color, Object.assign({ fill: "tonexty", fillcolor: hexA(color, 0.14) }, extra || {}))];
  }
  const line = (x, y, name, color, extra) => Object.assign(
    { x, y, name, type: "scatter", mode: "lines", line: { color, width: 2 }, connectgaps: false }, extra || {});

  // ---------------------------------------------------------------- Rates
  function rates() {
    const curve = readJSON("curve-data");
    if (curve && curve.curves) {
      const colors = [css("--s1"), css("--s2"), css("--s3")];
      const data = curve.curves.filter((c) => c.y).map((c, i) => line(curve.x, c.y, `${c.label} (${c.date})`, colors[i],
        { mode: "lines+markers", marker: { size: 8, line: { color: css("--surface"), width: 2 } },
          hovertemplate: "%{y:.2f}%<extra>%{fullData.name}</extra>" }));
      plot("curve", data, { showlegend: true, legend: { orientation: "h", y: -0.15 }, hovermode: "closest",
        yaxis: { ticksuffix: "%", gridcolor: css("--grid") }, xaxis: { type: "category", gridcolor: css("--grid"), linecolor: css("--axis") } });
    }
    const hist = readJSON("history-data") || {};
    Object.values(hist).forEach((s) => {
      const y = s.units === "bp_spread" ? s.v : s.v;
      plot(`h-${s.id}`, area(s.d, y, s.name, css("--s1"),
        { hovertemplate: s.units === "bp_spread" ? "%{y:.0f}bp<extra></extra>" : "%{y:.2f}%<extra></extra>" }),
        { yaxis: { ticksuffix: s.units === "bp_spread" ? "" : "%", gridcolor: css("--grid") } });
    });
  }

  // ---------------------------------------------------------------- Markets
  function markets() {
    const pick = $("pick");
    if (!pick) return;
    let current = null, range = "1Y";
    const fmtMove = (m, mode) => m == null ? "n/a" : (mode === "bp" ? `${m >= 0 ? "+" : ""}${m.toFixed(1)}bp` : `${m >= 0 ? "+" : ""}${m.toFixed(2)}%`);
    function startFor(lastISO) {
      const d = new Date(lastISO + "T00:00:00Z");
      const back = { "1M": 1, "3M": 3, "1Y": 12 }[range];
      if (range === "Max") return null;
      if (range === "YTD") return `${d.getUTCFullYear()}-01-01`;
      d.setUTCMonth(d.getUTCMonth() - back);
      return d.toISOString().slice(0, 10);
    }
    function draw() {
      const s = current; if (!s) return;
      const last = s.d[s.d.length - 1];
      const start = startFor(last);
      const unit = s.units === "pct_yield" ? "%" : s.units === "bp_spread" ? "bp" : "";
      $("series-title").textContent = `${s.name} — last ${last}`;
      plot("series-chart", area(s.d, s.v, s.name, css("--s1"), { hovertemplate: `%{y}${unit}<extra></extra>` }),
        { xaxis: { range: start ? [start, last] : undefined, gridcolor: css("--grid") }, yaxis: { ticksuffix: unit, gridcolor: css("--grid"), autorange: true } });
      if (start && window.Plotly) {  // rescale y to the visible window
        const ys = s.v.filter((v, i) => s.d[i] >= start && v != null);
        if (ys.length) { const lo = Math.min(...ys), hi = Math.max(...ys), pad = (hi - lo) * 0.05 || 1;
          Plotly.relayout("series-chart", { "yaxis.range": [lo - pad, hi + pad] }); }
      }
      const moves = s.m.filter((m) => m != null);
      const today = s.m[s.m.length - 1];
      $("hist-note").textContent = `${moves.length} daily moves since ${s.d[0]}; today ${fmtMove(today, s.mode)}`;
      const data = [{ x: moves, type: "histogram", nbinsx: 60, marker: { color: css("--s1"), line: { color: css("--surface"), width: 1 } },
        hovertemplate: "%{x}: %{y} days<extra></extra>" }];
      const shapes = today == null ? [] : [{ type: "line", x0: today, x1: today, yref: "paper", y0: 0, y1: 1, line: { color: css("--ink"), width: 2, dash: "dot" } }];
      const ann = today == null ? [] : [{ x: today, yref: "paper", y: 1, text: `today ${fmtMove(today, s.mode)}`, showarrow: false, yanchor: "bottom", font: { color: css("--ink") } }];
      plot("moves-hist", data, { shapes, annotations: ann, hovermode: "closest", bargap: 0.02,
        xaxis: { ticksuffix: s.mode === "bp" ? "bp" : "%", gridcolor: css("--grid") }, margin: { l: 48, r: 12, t: 20, b: 32 } });
    }
    function load(id) {
      fetch(`data/series/${encodeURIComponent(id)}.json`).then((r) => r.json()).then((s) => {
        current = s; draw();
        if (location.hash.slice(1) !== id) history.replaceState(null, "", `#${id}`);
      }).catch(() => { $("series-title").textContent = `Could not load ${id} (open via a web server, e.g. make serve).`; });
    }
    document.querySelectorAll(".ranges button").forEach((b) => b.addEventListener("click", () => {
      range = b.dataset.range;
      document.querySelectorAll(".ranges button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
      draw();
    }));
    pick.addEventListener("change", () => load(pick.value));
    window.addEventListener("hashchange", () => {
      const id = location.hash.slice(1);
      if (id && id !== pick.value && [...pick.options].some((o) => o.value === id)) { pick.value = id; load(id); }
    });
    const fromHash = location.hash.slice(1);
    if (fromHash && [...pick.options].some((o) => o.value === fromHash)) pick.value = fromHash;
    load(pick.value);
  }

  // ---------------------------------------------------------------- Portfolio
  function portfolio() {
    const h = readJSON("pnl-data");
    if (!h) return;
    plot("cum-pnl", [line(h.d, h.cum, "Cumulative P&L", css("--s1"),
      { hovertemplate: "%{y:,.0f} USD<extra></extra>", mode: "lines+markers", marker: { size: 6 } })],
      { yaxis: { gridcolor: css("--grid"), zeroline: true, zerolinecolor: css("--axis") } });
  }

  // ---------------------------------------------------------------- Health
  function health() {
    const boxes = document.querySelectorAll("input[data-sev]");
    const apply = () => {
      const on = new Set([...boxes].filter((b) => b.checked).map((b) => b.dataset.sev));
      document.querySelectorAll("#checks tbody tr[data-sev]").forEach((tr) => { tr.hidden = !on.has(tr.dataset.sev); });
    };
    boxes.forEach((b) => b.addEventListener("change", apply));
    if (boxes.length) apply();
    const h = readJSON("check-history-data");
    if (h && h.d.length) {
      const total = h.warn.concat(h.fail).reduce((a, b) => a + b, 0);
      plot("check-history", [
        { x: h.d, y: h.warn, name: "⚠ WARN", type: "bar", marker: { color: css("--warning") }, hovertemplate: "%{x}: %{y} WARN<extra></extra>" },
        { x: h.d, y: h.fail, name: "✕ FAIL", type: "bar", marker: { color: css("--critical") }, hovertemplate: "%{x}: %{y} FAIL<extra></extra>" },
      ], { barmode: "stack", showlegend: true, legend: { orientation: "h", y: -0.25 }, bargap: 0.3,
        xaxis: { type: "category", gridcolor: css("--grid") },  // one bar per run-day, never a time axis
        yaxis: { rangemode: "tozero", range: total ? undefined : [0, 1], dtick: total ? undefined : 1, gridcolor: css("--grid") },
        annotations: total ? [] : [{ xref: "paper", yref: "paper", x: 0.5, y: 0.55, showarrow: false,
          text: `✓ No warnings or failures in the last ${h.d.length} day${h.d.length > 1 ? "s" : ""}`,
          font: { color: css("--good"), size: 13 } }] });
    }
  }

  // ---------------------------------------------------------------- Terminal chrome
  function chrome() {
    const clock = $("clock");
    if (clock) {
      const fmt = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour12: false,
        hour: "2-digit", minute: "2-digit", second: "2-digit" });
      const tick = () => { clock.textContent = fmt.format(new Date()); };
      tick(); setInterval(tick, 1000);
    }
    const keys = {};
    document.querySelectorAll(".fkeys a[data-key]").forEach((a) => { keys[a.dataset.key] = a.href; });
    const input = $("cmd-input"), form = $("cmd");
    const options = input ? [...document.querySelectorAll("#cmd-list option")].map((o) => [o.value, o.textContent]) : [];
    const resolve = (q) => {
      q = q.trim().toUpperCase().replace(/<?GO>?$/, "").trim();
      if (!q) return null;
      const exact = options.find(([id, name]) => id === q || name.toUpperCase() === q);
      if (exact) return exact[0];
      const part = options.find(([id, name]) => id.startsWith(q) || name.toUpperCase().includes(q));
      return part ? part[0] : null;
    };
    if (form) form.addEventListener("submit", (e) => {
      e.preventDefault();
      const id = resolve(input.value);
      if (id) location.href = `markets.html#${id}`;
      else { input.value = ""; input.placeholder = "NOT FOUND"; setTimeout(() => { input.placeholder = "TICKER"; }, 1500); }
    });
    document.addEventListener("keydown", (e) => {
      const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
      if (e.key === "/" && !typing && input) { e.preventDefault(); input.focus(); return; }
      if (e.key === "Escape" && document.activeElement === input) { input.blur(); return; }
      if (keys[e.key] && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); location.href = keys[e.key]; }
    });
  }

  // ---------------------------------------------------------------- Fit to screen (Overview)
  // Binary-search the largest font size (--fs, which every size inside <main> is relative to)
  // at which the Overview fits the window with no page scroll and no clipped table columns.
  function fitToScreen() {
    if (document.body.dataset.page !== "overview") return;
    const main = document.querySelector("main");
    const MIN = 9, MAX = 17, COMPACT_BELOW = 11.5;
    const eligible = () => window.innerWidth >= 1000 && window.innerHeight >= 560;
    const fits = () => {
      if (main.scrollHeight > main.clientHeight + 1) return false;
      for (const c of document.querySelectorAll(".col")) if (c.scrollHeight > c.clientHeight + 1) return false;
      for (const s of main.querySelectorAll(".scroll")) if (s.scrollWidth > s.clientWidth + 1) return false;
      return true;
    };
    // Largest --fs that fits with the current layout (MIN if nothing fits).
    function search() {
      let lo = MIN, hi = MAX;
      main.style.setProperty("--fs", `${hi}px`);
      if (fits()) return hi;
      for (let i = 0; i < 8; i++) {
        const mid = (lo + hi) / 2;
        main.style.setProperty("--fs", `${mid}px`);
        if (fits()) lo = mid; else hi = mid;
      }
      main.style.setProperty("--fs", `${lo}px`);  // apply the best size found, not the last probe
      return lo;
    }
    function fit() {
      const body = document.body;
      if (!eligible()) {
        body.classList.remove("fit", "compact");
        main.style.removeProperty("--fs");
        return;
      }
      body.classList.add("fit");
      body.classList.remove("compact");
      const full = search();
      if (full < COMPACT_BELOW) {
        // Short screen: drop the hero sparklines and keep whichever layout gives bigger text.
        body.classList.add("compact");
        const compact = search();
        if (compact <= full) { body.classList.remove("compact"); search(); }
      }
      // Even the minimum doesn't fit (very short window): let it scroll rather than clip.
      if (!fits()) {
        body.classList.remove("fit", "compact");
        main.style.removeProperty("--fs");
      }
    }
    let raf = 0;
    const schedule = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(fit); };
    window.addEventListener("resize", schedule);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(schedule);
    fit();
  }

  function init() { chrome(); fitToScreen(); rates(); markets(); portfolio(); health(); }
  if (window.Plotly || !document.querySelector('script[src*="plotly"]')) init();
  else window.addEventListener("load", init);
})();
