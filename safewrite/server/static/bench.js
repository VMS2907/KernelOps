// Trade-offs: what each crash-consistency technique costs, measured on this machine.
import { h, fill, svg, api, clear, tooltip, fmt, pill } from "./util.js";

const S1 = "var(--old)";        // categorical slot 1
const S2 = "var(--new)";        // categorical slot 2
const RAMP = ["var(--ramp-1)", "var(--ramp-2)", "var(--ramp-3)"];   // ordinal blue ramp (threads: few -> many)

export class BenchView {
  constructor(root, app) {
    this.root = root;
    this.app = app;
    this.loaded = false;
    this.build();
  }

  build() {
    this.elBody = h("div");
    this.btn = h("button", { class: "btn", onclick: () => this.load(true) }, "↻ Re-run benchmarks");
    this.root.append(
      h("div", { class: "card" },
        h("div", { class: "controls" },
          h("div", {}, h("h2", {}, "The cost of crash consistency, measured"),
            h("p", { class: "muted small", style: { margin: "2px 0 0" } },
              "Real durable saves on this machine's file system. Absolute numbers depend on the disk; the ratios between engines are the point.")),
          h("div", { class: "spacer" }), this.btn)),
      this.elBody);
  }

  activate() { if (!this.loaded) { this.loaded = true; this.load(false); } }

  async load(run) {
    fill(this.elBody, h("div", { class: "card empty", style: { marginTop: "14px" } }, h("span", { class: "spin" }), run ? " running benchmarks (about 20 s)…" : " loading…"));
    this.btn.disabled = true;
    try {
      const b = await api("/api/bench" + (run ? "?run=1" : ""));
      this.render(b);
    } catch (e) {
      fill(this.elBody, h("div", { class: "card" }, "Benchmark failed: " + e.message));
    } finally {
      this.btn.disabled = false;
    }
  }

  render(b) {
    const R = b.results;
    const names = R.map((r) => r.engine);
    const cows = R.find((r) => r.engine === "cow"), wal = R.find((r) => r.engine === "wal");
    const grid = h("div", { class: "grid g2", style: { marginTop: "14px" } },
      chartCard("Durable saves per second", `${b.n} saves of ${fmt.int(b.size)} bytes · higher is better`,
        hbars(names, [{ name: "durable (fsync on)", color: S1, values: R.map((r) => r.saves_per_s) },
                      { name: "fsync disabled (not crash-safe)", color: S2, values: R.map((r) => r.no_fsync_saves_per_s) }], (v) => fmt.compact(Math.round(v)) + "/s")),
      chartCard("fsync barriers per commit", "the dominant cost on real disks · lower is better",
        hbars(names, [{ name: "fsyncs", color: S1, values: R.map((r) => r.fsyncs_per_commit) }], (v) => v.toFixed(2))),
      chartCard("Write amplification", "bytes written to disk ÷ bytes of document saved · lower is better",
        hbars(names, [{ name: "amplification", color: S1, values: R.map((r) => r.write_amplification) }], (v) => v.toFixed(2) + "×")),
      chartCard("Recovery time vs. un-checkpointed work", "WAL must replay its log; COW just picks a superblock",
        lines(Object.keys(b.recovery_scaling.wal), [
          { name: "wal (redo replay)", color: S1, values: Object.values(b.recovery_scaling.wal) },
          { name: "cow (pick valid superblock)", color: S2, values: Object.values(b.recovery_scaling.cow) }], (v) => fmt.ms(v))),
      chartCard("Concurrency: commits per second", "WAL group commit lets N committers share one fsync",
        hbars(names, b.threads.map((t, i) => ({ name: `${t} thread${t > 1 ? "s" : ""}`, color: RAMP[i % RAMP.length],
          values: R.map((r) => r.throughput[String(t)]?.commits_per_s ?? 0) })), (v) => fmt.compact(Math.round(v)) + "/s")),
      h("div", { class: "card" },
        h("h3", {}, "Why copy-on-write is our default"),
        h("ul", { style: { margin: "8px 0 0", paddingLeft: "18px", lineHeight: "1.7" } },
          h("li", {}, h("b", {}, "Recovery is O(1): "), `${fmt.ms(cows?.recovery_ms ?? 0)} after an unclean shutdown, flat as history grows. WAL replay grows with the log.`),
          h("li", {}, h("b", {}, "Old or new by construction: "), "live blocks are never overwritten, so there is no replay or rollback logic to get wrong."),
          h("li", {}, h("b", {}, "Free version history: "), "previous generations stay readable (see the Documents tab)."),
          h("li", {}, h("b", {}, "Readers never block: "), "MVCC snapshots pin a generation while a writer prepares the next."),
          h("li", {}, h("b", {}, "The cost: "), `2 fsyncs per commit vs. WAL's ~1 (WAL wins small-write throughput, ${fmt.compact(Math.round(wal?.saves_per_s ?? 0))}/s), plus garbage collection.`)),
        h("p", { class: "muted small", style: { marginTop: "8px" } }, "Undo journaling costs 3 fsyncs and writes every byte twice. Shadow files can't make a multi-document save atomic.")),
    );
    const table = h("table", { class: "data" },
      h("thead", {}, h("tr", {}, ["engine", "p50 ms", "p99 ms", "saves/s", "fsync/commit", "syscalls/commit", "write amp", "recovery ms", "no-fsync saves/s"].map((x) => h("th", {}, x)))),
      h("tbody", {}, R.map((r) => h("tr", {}, [r.engine, r.p50_ms, r.p99_ms, r.saves_per_s, r.fsyncs_per_commit, r.syscalls_per_commit, r.write_amplification, r.recovery_ms, r.no_fsync_saves_per_s].map((x) => h("td", {}, x))))));
    fill(this.elBody, grid,
      h("div", { class: "card", style: { marginTop: "14px" } }, h("h3", {}, "All numbers"), h("p", { class: "muted small" }, `measured ${b.generated} on ${b.platform}`), table));
  }
}

// Round axis ticks: 0 / 1K / 2K, never 0.77 / 1.54.
function niceScale(max, n = 4) {
  const raw = Math.max(max, 1e-9) / n;
  const p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * p).find((s) => s >= raw);
  return { step, max: Math.ceil(max / step) * step };
}

function chartCard(title, sub, chart) {
  return h("div", { class: "card chart" }, h("h3", {}, title), h("p", { class: "muted small", style: { margin: "2px 0 10px" } }, sub), chart);
}

function legend(series) {
  if (series.length < 2) return null;
  return h("div", { class: "legend", style: { marginTop: "6px" } }, series.map((s) => h("span", {}, h("i", { style: { background: s.color } }), s.name)));
}

// Horizontal bars, grouped by category. One axis, bars <= 24px, 4px rounded data end, square at baseline.
function hbars(cats, series, fmtv) {
  const W = 520, L = 64, R = 70, band = Math.max(20, Math.min(24, 60 / series.length));
  const gap = 2, groupGap = 12;
  const groupH = series.length * band + (series.length - 1) * gap;
  const H = cats.length * (groupH + groupGap) + 20;
  const sc = niceScale(Math.max(...series.flatMap((s) => s.values)));
  const max = sc.max;
  const x = (v) => L + (W - L - R) * (v / max);
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });
  for (let v = 0; v <= max + 1e-9; v += sc.step) {
    const gx = x(v);
    root.append(svg("line", { class: "grid", x1: gx, x2: gx, y1: 0, y2: H - 18 }),
      svg("text", { x: gx, y: H - 4, "text-anchor": "middle" }, tick(v)));
  }
  root.append(svg("line", { class: "axis", x1: L, x2: L, y1: 0, y2: H - 18 }));
  cats.forEach((c, i) => {
    const y0 = i * (groupH + groupGap) + 4;
    root.append(svg("text", { class: "lab", x: L - 8, y: y0 + groupH / 2 + 4, "text-anchor": "end" }, c));
    series.forEach((s, j) => {
      const v = s.values[i], y = y0 + j * (band + gap), w = Math.max(1, x(v) - L), r = Math.min(4, w);
      const d = `M${L},${y} h${w - r} a${r},${r} 0 0 1 ${r},${r} v${band - 2 * r} a${r},${r} 0 0 1 ${-r},${r} h${-(w - r)} z`;
      const bar = svg("path", { d, fill: s.color });
      const hit = svg("rect", { x: L, y, width: W - L - R, height: band, fill: "transparent" });
      tooltip(hit, `<b>${c}</b> · ${s.name}<br>${fmtv(v)}`);
      root.append(bar, svg("text", { class: "val", x: x(v) + 6, y: y + band / 2 + 4 }, fmtv(v)), hit);
    });
  });
  return h("div", {}, root, legend(series));
}

// Lines over ordinal x points. 2px lines, >= 8px end markers with a 2px surface ring.
function lines(xs, series, fmtv) {
  const W = 520, H = 220, L = 54, R = 110, T = 10, B = 28;
  const sc = niceScale(Math.max(...series.flatMap((s) => s.values)));
  const max = sc.max;
  const x = (i) => L + (W - L - R) * (xs.length === 1 ? 0.5 : i / (xs.length - 1));
  const y = (v) => T + (H - T - B) * (1 - v / max);
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });
  for (let v = 0; v <= max + 1e-9; v += sc.step) {
    const gy = y(v);
    root.append(svg("line", { class: "grid", x1: L, x2: W - R, y1: gy, y2: gy }),
      svg("text", { x: L - 6, y: gy + 4, "text-anchor": "end" }, tick(v) + " ms"));
  }
  root.append(svg("line", { class: "axis", x1: L, x2: W - R, y1: y(0), y2: y(0) }));
  xs.forEach((xv, i) => root.append(svg("text", { x: x(i), y: H - 8, "text-anchor": "middle" }, `${fmt.int(Number(xv))} commits`)));
  series.forEach((s) => {
    const pts = s.values.map((v, i) => `${x(i)},${y(v)}`).join(" ");
    root.append(svg("polyline", { points: pts, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    s.values.forEach((v, i) => {
      const dot = svg("circle", { cx: x(i), cy: y(v), r: 4.5, fill: s.color, stroke: "var(--surface-1)", "stroke-width": 2 });
      const hit = svg("circle", { cx: x(i), cy: y(v), r: 12, fill: "transparent" });
      tooltip(hit, `<b>${s.name}</b><br>${fmt.int(Number(xs[i]))} un-checkpointed commits → ${fmtv(v)}`);
      root.append(dot, hit);
    });
    const last = s.values.length - 1;
    root.append(svg("text", { class: "val", x: x(last) + 10, y: y(s.values[last]) + 4 }, `${s.name.split(" ")[0]} ${fmtv(s.values[last])}`));
  });
  return h("div", {}, root, legend(series));
}

function tick(v) {
  if (v >= 1000) return (v / 1000).toFixed(v % 1000 ? 1 : 0) + "K";
  return String(Math.round(v * 100) / 100);
}
