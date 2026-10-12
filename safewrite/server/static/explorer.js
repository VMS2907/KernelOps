// Explorer: every engine x every scenario x every crash model, every crash state. The proof table.
import { h, fill, api, clear, stream, tooltip, pill, fmt, esc, OUTCOME } from "./util.js";

const VERDICT = {
  PASS: { cls: "pass", sym: "✓", tip: "every crash state recovered correctly" },
  "BROKEN-AS-EXPECTED": { cls: "broken", sym: "✗", tip: "safeguard removed: counter-examples found (expected)" },
  LIMITATION: { cls: "limit", sym: "½", tip: "documented limitation: this engine is atomic per document, not per transaction" },
  "UNEXPECTED-PASS": { cls: "idle", sym: "·", tip: "this scenario does not exercise the removed safeguard" },
  FAIL: { cls: "fail", sym: "!!", tip: "a supposedly safe engine violated the spec" },
};

export class ExplorerView {
  constructor(root, app) {
    this.root = root;
    this.app = app;
    this.model = "power";
    this.cells = new Map();         // "spec|scenario|model" -> result
    this.selected = null;
    this.running = false;
    this.started = false;
    this.build();
  }

  key(spec, sc, model) { return `${spec}|${sc}|${model}`; }

  build() {
    this.elStats = h("div", { class: "grid g4" });
    this.elModel = h("div", { class: "seg", role: "group", "aria-label": "Crash model" },
      this.app.meta.models.map((m) => h("button", { "data-m": m.name, title: m.title, onclick: () => { this.model = m.name; this.render(); this.ensure(); } }, m.name)));
    this.elProgress = h("div", { class: "progress" }, h("div"));
    this.elStatus = h("span", { class: "muted small" });
    this.elMatrix = h("div", { class: "matrix-wrap" });
    this.elDrawer = h("div", { class: "drawer" });
    this.root.append(
      this.elStats,
      h("div", { class: "card", style: { marginTop: "14px" } },
        h("div", { class: "controls" },
          h("div", {}, h("h2", {}, "Crash-state exploration matrix"),
            h("p", { class: "muted small", style: { margin: "2px 0 0" } },
              "Each cell: the engine is crashed after every syscall of the scenario, every disk state the model allows is rebuilt, recovery runs, and the result is checked.")),
          h("div", { class: "spacer" }),
          h("div", { class: "field" }, h("span", {}, "Crash model"), this.elModel),
          h("button", { class: "btn", onclick: () => this.runAll() }, "Run all models"),
          h("button", { class: "btn", onclick: () => this.ensure(true) }, "↻ Re-run fresh"),
        ),
        h("div", { style: { margin: "12px 0 6px" } }, this.elProgress),
        this.elStatus,
        this.elMatrix,
        h("div", { class: "legend", style: { marginTop: "12px" } },
          Object.entries(VERDICT).map(([k, v]) => h("span", {}, h("span", { class: "cell " + v.cls, style: { display: "inline-flex", minWidth: 0, padding: "0 6px", cursor: "default" } }, v.sym), " ", v.tip))),
      ),
      this.elDrawer,
    );
  }

  async activate() {
    if (this.started) return;
    this.started = true;
    const cached = await api("/api/explore/cached");
    for (const r of cached.results) this.cells.set(this.key(r.spec, r.scenario, r.model), r);
    this.render();
    this.ensure();
  }

  specs() { return this.app.meta.engines.map((e) => e.spec); }
  scenarios() { return this.app.meta.scenarios.map((s) => s.name); }

  ensure(fresh = false, models = [this.model]) {
    if (this.running) return;
    const missing = fresh || this.specs().some((s) => this.scenarios().some((c) => models.some((m) => !this.cells.has(this.key(s, c, m)))));
    if (!missing) { this.render(); return; }
    this.running = true;
    if (fresh) for (const m of models) for (const s of this.specs()) for (const c of this.scenarios()) this.cells.delete(this.key(s, c, m));
    this.render();
    const q = new URLSearchParams({ models: models.join(","), fresh: fresh ? "1" : "0" });
    const t0 = performance.now();
    stream("/api/explore/stream?" + q, {
      cell: (ev) => {
        const r = ev.result;
        this.cells.set(this.key(r.spec, r.scenario, r.model), r);
        this.elProgress.firstChild.style.width = (100 * ev.done / ev.total) + "%";
        this.elStatus.textContent = `exploring… ${ev.done}/${ev.total} cells`;
        this.renderCell(r);
        this.renderStats();
      },
      done: (ev) => {
        this.running = false;
        this.elStatus.textContent = `done in ${((performance.now() - t0) / 1000).toFixed(1)} s`;
        this.render();
      },
      error: () => { this.running = false; },
    });
  }

  runAll() { this.ensure(false, this.app.meta.models.map((m) => m.name)); }

  // ------------------------------------------------------------------ stats
  renderStats() {
    const rs = [...this.cells.values()];
    const states = rs.reduce((a, r) => a + r.states + (r.recovery_crash_states || 0), 0);
    const safe = rs.filter((r) => r.expected_safe);
    const safeViol = safe.filter((r) => r.verdict === "FAIL").reduce((a, r) => a + r.violations + r.recovery_violations, 0);
    const safeStates = safe.reduce((a, r) => a + r.states + (r.recovery_crash_states || 0), 0);
    const ablations = new Set(this.app.meta.engines.filter((e) => !e.expected_safe).map((e) => e.spec));
    const caught = new Set(rs.filter((r) => r.verdict === "BROKEN-AS-EXPECTED").map((r) => r.spec));
    const models = new Set(rs.map((r) => r.model));
    const recov = rs.reduce((a, r) => a + (r.recovery_crash_states || 0), 0);
    fill(this.elStats, 
      h("div", { class: "card stat" }, h("div", { class: "label" }, "Crash states explored"),
        h("div", { class: "value hero" }, fmt.int(states)),
        h("div", { class: "sub" }, `${models.size} crash model(s) · incl. ${fmt.int(recov)} crashes during recovery`)),
      h("div", { class: "card stat" }, h("div", { class: "label" }, "Violations in the safe engines"),
        h("div", { class: "value" }, fmt.int(safeViol), " ", safeViol ? pill("bad", "✗ bug") : pill("good", "✓ none")),
        h("div", { class: "sub" }, `across ${fmt.int(safeStates)} crash states of shadow, undo, wal, cow`)),
      h("div", { class: "card stat" }, h("div", { class: "label" }, "Removed safeguards caught"),
        h("div", { class: "value" }, `${[...caught].filter((s) => ablations.has(s)).length} / ${ablations.size}`),
        h("div", { class: "sub" }, "every ablation must produce a counter-example, or the checker is too weak")),
      h("div", { class: "card stat" }, h("div", { class: "label" }, "What a crash can do"),
        h("div", { class: "sub", style: { marginTop: "6px" } }, "lose unflushed writes · reorder them · tear sectors · zero-fill extents · crash again during recovery")),
    );
  }

  // ------------------------------------------------------------------ matrix
  render() {
    this.elModel.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.m === this.model));
    this.renderStats();
    const scs = this.app.meta.scenarios;
    const table = h("table", { class: "matrix" });
    table.append(h("thead", {}, h("tr", {}, h("th", {}, "engine"), scs.map((s) => h("th", {}, s.title)))));
    const body = h("tbody");
    let lastEngine = null;
    for (const e of this.app.meta.engines) {
      const tr = h("tr", { class: (e.variant ? "variant" : "") + (e.name !== lastEngine ? " group" : "") });
      lastEngine = e.name;
      tr.append(h("td", { class: "spec" }, e.variant ? "└ " + e.variant : e.spec, " ",
        !e.variant ? (e.expected_safe ? pill("good", "safe") : pill("bad", "unsafe")) : null));
      for (const s of scs) {
        const td = h("td", { id: `cell-${e.spec}|${s.name}` });
        tr.append(td);
      }
      body.append(tr);
    }
    table.append(body);
    fill(this.elMatrix, table);
    for (const e of this.app.meta.engines) for (const s of scs) {
      const r = this.cells.get(this.key(e.spec, s.name, this.model));
      if (r) this.renderCell(r);
      else document.getElementById(`cell-${e.spec}|${s.name}`)?.append(h("div", { class: "cell pending" }, this.running ? h("span", { class: "spin" }) : "–"));
    }
    if (this.selected) this.renderDrawer(this.selected);
  }

  renderCell(r) {
    if (r.model !== this.model) return;
    const td = document.getElementById(`cell-${r.spec}|${r.scenario}`);
    if (!td) return;
    const v = VERDICT[r.verdict] || VERDICT.FAIL;
    const n = r.states + (r.recovery_crash_states || 0);
    const bad = r.violations + (r.recovery_violations || 0);
    const cell = h("div", { class: "cell " + v.cls, onclick: () => { this.selected = r; this.renderDrawer(r); } },
      h("span", { class: "sym" }, v.sym),
      h("span", { class: "cnt" }, r.verdict === "PASS" || r.verdict === "UNEXPECTED-PASS" ? `${fmt.int(n)} states` : `${fmt.int(bad)} / ${fmt.int(n)}`));
    tooltip(cell, () => `<b>${esc(r.spec)} · ${esc(r.scenario)} · ${esc(r.model)}</b><br>${v.tip}<br>${fmt.int(r.states)} crash states at ${r.crash_points} crash points` +
      (r.recovery_crash_states ? `<br>+ ${fmt.int(r.recovery_crash_states)} crashes during recovery` : "") +
      `<br>${Object.entries(r.outcomes).map(([k, c]) => `${k}: ${c}`).join(" · ")}`);
    fill(td, cell);
  }

  // ------------------------------------------------------------------ details
  renderDrawer(r) {
    const v = VERDICT[r.verdict] || VERDICT.FAIL;
    const total = Object.values(r.outcomes).reduce((a, b) => a + b, 0) || 1;
    const bar = h("div", { class: "bar-mini" });
    for (const [k, c] of Object.entries(r.outcomes)) {
      const seg = h("span", { style: { width: (100 * c / total) + "%", background: k === "OK" ? "var(--good)" : "var(--bad)", opacity: k === "OK" ? ".75" : "1", borderRight: "2px solid var(--surface-1)" } });
      tooltip(seg, `${k}: ${c} (${(100 * c / total).toFixed(1)}%)`);
      bar.append(seg);
    }
    const spark = h("div", { class: "spark" });
    const tl = r.timeline || [];
    const max = Math.max(1, ...tl.map((p) => p.states));
    for (const p of tl) {
      const s = h("span", { class: p.violations ? "v" : "", style: { height: (8 + 92 * Math.sqrt(p.states / max)) + "%", cursor: "pointer" },
        onclick: () => this.app.openInLab({ spec: r.spec, scenario: r.scenario, model: r.model, point: p.point, index: 0 }) });
      tooltip(s, () => `after <b>${esc(p.op)}</b><br>phase ${esc(p.phase)} · ${p.states} states · ${p.violations ? `<b>${p.violations} violations</b>` : "all correct"}<br><i>click to open in the Lab</i>`);
      spark.append(s);
    }
    const ex = (r.examples || []).map((e) => h("div", { class: "example" },
      h("div", { class: "row" }, pill("bad", e.code), h("span", {}, e.detail), h("div", { class: "spacer" }),
        e.state_index !== undefined ? h("button", { class: "btn", onclick: () => this.app.openInLab({ spec: r.spec, scenario: r.scenario, model: r.model, point: e.point, index: e.state_index }) }, "Open in Lab →") : null),
      h("div", { class: "crash" }, `crash after “${e.phase}”: ${e.crash}`)));
    fill(this.elDrawer, h("div", { class: "card" },
      h("div", { class: "row" }, h("h2", { class: "mono" }, r.spec), h("span", { class: "muted" }, `${r.scenario} · ${r.model}`),
        h("span", { class: "cell " + v.cls, style: { minWidth: 0, display: "inline-flex", cursor: "default" } }, v.sym, " ", r.verdict.toLowerCase())),
      r.breaks ? h("div", { class: "ablation" }, h("b", {}, "Safeguard removed: "), r.breaks) : null,
      h("div", { class: "grid g4", style: { marginTop: "12px" } },
        stat("crash points", r.crash_points), stat("crash states", fmt.int(r.states), r.exhaustive ? "exhaustive" : "sampled"),
        stat("unique disk images", fmt.int(r.unique_images)), stat("crashes during recovery", fmt.int(r.recovery_crash_states || 0), `${r.recovery_violations || 0} violations`)),
      h("h3", { style: { marginTop: "14px" } }, "Outcomes"), bar,
      h("div", { class: "legend" }, Object.entries(r.outcomes).map(([k, c]) => h("span", {}, h("i", { style: { background: k === "OK" ? "var(--good)" : "var(--bad)" } }), `${k === "OK" ? "✓" : "✗"} ${k}: ${fmt.int(c)}`))),
      tl.length ? [h("h3", { style: { marginTop: "14px" } }, "Per crash point (height = number of possible disk states; red = violations)"), spark] : null,
      ex.length ? [h("h3", { style: { marginTop: "14px" } }, "Counter-examples"), ex] : h("p", { class: "muted", style: { marginTop: "12px" } }, "No counter-examples: every crash state recovered to a valid version."),
      h("div", { class: "row", style: { marginTop: "12px" } }, h("button", { class: "btn primary", onclick: () => this.app.openInLab({ spec: r.spec, scenario: r.scenario, model: r.model, point: null, index: 0 }) }, "Open this engine in the Lab →")),
    ));
    this.elDrawer.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function stat(label, value, sub) {
  return h("div", { class: "stat" }, h("div", { class: "label" }, label), h("div", { class: "value", style: { fontSize: "22px" } }, value), sub ? h("div", { class: "sub" }, sub) : null);
}
