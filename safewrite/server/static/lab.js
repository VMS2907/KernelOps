// The Lab: one engine, one scenario, one crash. Scrub the syscalls, pull the plug, watch recovery.
import { h, fill, api, clear, tooltip, OUTCOME, pill, fmt, esc } from "./util.js";

const KIND = { write: "pwrite", trunc: "ftruncate", fsync: "fsync", fsync_dir: "fsync dir", rename: "rename",
  create: "creat", unlink: "unlink", mkdir: "mkdir" };

const STATUS = {
  barrier: ["barrier", "▮ barrier"],
  durable: ["neutral", "✓ durable"],
  persisted: ["neutral", "✓ persisted"],
  survived: ["lucky", "~ survived by luck"],
  lost: ["bad", "✗ lost"],
  torn: ["torn", "⚠ torn"],
};

function groupedEngineSelect(engines, value, onchange) {
  const sel = h("select", { onchange: (e) => onchange(e.target.value), "aria-label": "Engine" });
  const groups = [
    ["Crash-consistent engines", engines.filter((e) => e.expected_safe)],
    ["Unsafe baseline", engines.filter((e) => !e.safe && !e.variant)],
    ["Ablations: one safeguard removed", engines.filter((e) => e.variant && e.safe)],
    ["Baseline variants", engines.filter((e) => e.variant && !e.safe)],
  ];
  for (const [label, list] of groups) {
    if (!list.length) continue;
    const g = h("optgroup", { label });
    for (const e of list) g.append(h("option", { value: e.spec, selected: e.spec === value }, `${e.spec}: ${e.title}`));
    sel.append(g);
  }
  return sel;
}

export class LabView {
  constructor(root, app) {
    this.root = root;
    this.app = app;
    this.s = { spec: "naive", scenario: "update", model: "power", point: null, index: 0 };
    this.trace = null;
    this.busy = false;
    this.build();
  }

  activate() {
    if (!this.trace) this.load({});
  }

  build() {
    const m = this.app.meta;
    this.elSpec = h("div");
    this.elScenario = h("select", { "aria-label": "Scenario", onchange: (e) => this.load({ scenario: e.target.value, point: null, index: 0 }) },
      m.scenarios.map((s) => h("option", { value: s.name }, s.title)));
    this.elModel = h("div", { class: "seg", role: "group", "aria-label": "Crash model" },
      m.models.map((md) => h("button", { "data-m": md.name, title: md.title, onclick: () => this.load({ model: md.name, index: 0 }) }, md.name)));
    this.elEngine = h("div", { class: "card engine-card" });
    this.elStory = h("div", { class: "card" });
    this.elTimeline = h("div", { class: "timeline" });
    this.elCrashBar = h("div", { class: "crash-bar" });
    this.elOps = h("div", { class: "card" });
    this.elDisk = h("div", { class: "card" });
    this.elRecovery = h("div", { class: "card" });
    this.elVerdict = h("div");

    const title = (n, text, sub) => h("div", { class: "section-title" }, h("span", { class: "num" }, n), h("h2", {}, text), sub ? h("span", { class: "muted small" }, sub) : null);

    this.root.append(
      h("div", { class: "controls" },
        h("label", { class: "field" }, h("span", {}, "Engine"), this.elSpec),
        h("label", { class: "field" }, h("span", {}, "Scenario"), this.elScenario),
        h("div", { class: "field" }, h("span", {}, "Crash model"), this.elModel),
        h("div", { class: "spacer" }),
        h("button", { class: "btn danger", onclick: () => this.plug("random") }, "⚡ Pull the plug"),
        h("button", { class: "btn", onclick: () => this.plug("violation") }, "🎯 Find a violation"),
      ),
      h("div", { class: "grid g-lab", style: { marginTop: "14px" } }, this.elEngine, this.elStory),
      title(1, "The save, syscall by syscall", "click any syscall to cut the power right after it"),
      h("div", { class: "card" }, this.elTimeline, this.elCrashBar),
      title(2, "What reached the disk", "the page cache is gone; only this survived"),
      h("div", { class: "grid g2" }, this.elOps, this.elDisk),
      title(3, "Reboot: recovery runs", "the engine's real recovery code, on that exact disk image"),
      this.elRecovery,
      title(4, "Verdict", "old version, new version, or corruption?"),
      this.elVerdict,
    );
  }

  async load(args) {
    Object.assign(this.s, args);
    const m = this.app.meta;
    fill(this.elSpec, groupedEngineSelect(m.engines, this.s.spec, (v) => this.load({ spec: v, point: null, index: 0 })));
    this.elScenario.value = this.s.scenario;
    this.elModel.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.m === this.s.model));
    const q = new URLSearchParams({ spec: this.s.spec, scenario: this.s.scenario, model: this.s.model });
    this.trace = await api("/api/trace?" + q);
    const pts = this.trace.points.map((p) => p.point);
    if (this.s.point === null || !pts.includes(this.s.point)) {
      this.s.point = pts[Math.floor(pts.length / 2)];
      this.s.index = 0;
    }
    this.renderEngine();
    this.renderTimeline();
    await this.inspect();
  }

  pointInfo(p) { return this.trace.points.find((x) => x.point === p); }

  async inspect(flash) {
    const q = new URLSearchParams({ spec: this.s.spec, scenario: this.s.scenario, model: this.s.model,
      point: this.s.point, index: this.s.index });
    const d = await api("/api/inspect?" + q);
    this.d = d;
    this.s.index = d.index;
    this.renderTimeline();
    this.renderCrashBar();
    this.renderOps();
    this.renderDisk();
    this.renderRecovery();
    this.renderVerdict(flash);
  }

  async plug(want) {
    if (this.busy) return;
    this.busy = true;
    try {
      const q = new URLSearchParams({ spec: this.s.spec, scenario: this.s.scenario, model: this.s.model, want });
      const r = await api("/api/plug?" + q);
      this.s.point = r.point;
      this.s.index = r.index;
      this.noViolation = want === "violation" && !r.violation;
      await this.inspect(true);
    } finally {
      this.busy = false;
    }
  }

  // ------------------------------------------------------------------ cards
  renderEngine() {
    const i = this.trace.info;
    const fs = this.trace.trace.filter((o) => o.kind === "fsync" || o.kind === "fsync_dir").length;
    const sc = this.trace.trace.filter((o) => o.kind !== "note").length;
    fill(this.elEngine, 
      h("h3", {}, i.title, i.expected_safe ? pill("good", "✓ crash-consistent") : pill("bad", i.safe ? "✗ ablation" : "✗ unsafe baseline")),
      h("p", { class: "muted", style: { marginTop: "6px" } }, i.technique),
      h("dl", { class: "kv" },
        h("dt", {}, "commit point"), h("dd", {}, i.commit_point),
        h("dt", {}, "recovers to"), h("dd", {}, i.recovers_to),
        h("dt", {}, "multi-doc atomic"), h("dd", {}, i.atomic_tx ? "yes" : "no: one document at a time"),
        h("dt", {}, "this scenario"), h("dd", {}, `${sc} syscalls, ${fs} of them fsync barriers`),
      ),
      i.breaks ? h("div", { class: "ablation" }, h("b", {}, "Safeguard removed: "), i.breaks) : null,
    );
    const v = this.trace.versions;
    const sc2 = this.trace.scenario;
    const label = (j) => j === 0 ? "previous" : j === v.length - 1 ? "new" : `S${j}`;
    fill(this.elStory, 
      h("h3", {}, sc2.title),
      h("p", { class: "story", style: { marginTop: "6px" } }, sc2.story),
      h("div", { class: "row small" }, v.map((st, j) => h("span", { class: "pill " + (j === 0 ? "neutral" : "neutral") },
        h("i", { style: { width: "9px", height: "9px", borderRadius: "2px", display: "inline-block",
          background: j === 0 ? "var(--old)" : j === v.length - 1 ? "var(--new)" : "var(--lucky)" } }),
        `${label(j)}: ` + Object.entries(st).map(([k, x]) => x ? `${k} ${fmt.int(x.len)} B` : `${k} absent`).join(", ")))),
      h("p", { class: "muted small", style: { marginTop: "8px" } }, "Crash model: " + this.trace.model.title),
    );
  }

  renderTimeline() {
    const el = clear(this.elTimeline);
    const P = this.s.point;
    const pts = new Set(this.trace.points.map((p) => p.point));
    const startChip = h("button", { class: "op" + (P === 0 ? " crash-here" : ""), onclick: () => this.pick(0) }, h("span", { class: "k" }, "⏻"), "before any syscall");
    el.append(startChip);
    for (const op of this.trace.trace) {
      if (op.kind === "note") {
        if (op.label === "begin") el.append(h("span", { class: "flag begin" }, `BEGIN save ${op.info?.step ?? ""}`));
        else if (op.label === "ack") {
          const f = h("button", { class: "flag ack" + (P === op.idx + 1 ? " op crash-here" : ""), style: { cursor: "pointer", border: 0 },
            onclick: () => pts.has(op.idx + 1) && this.pick(op.idx + 1) }, `ACK ${op.info?.step ?? ""}: save() returned`);
          tooltip(f, "The caller was told the save is durable. A crash after this point must recover the new version.");
          el.append(f);
        } else if (op.label === "commit") {
          const f = h("span", { class: "flag commit" }, "★ COMMIT POINT");
          tooltip(f, esc(op.info?.detail || "the commit takes effect here"));
          el.append(f);
        } else el.append(h("span", { class: "tl-label" }, "# " + op.label));
        continue;
      }
      const cls = ["op", "k-" + op.kind];
      if (op.idx >= P) cls.push("after");
      if (op.idx + 1 === P) cls.push("crash-here");
      const chip = h("button", { class: cls.join(" "), onclick: () => this.pick(op.idx + 1) },
        h("span", { class: "muted" }, "#" + op.n), h("span", { class: "k" }, KIND[op.kind] || op.kind), shortText(op));
      const pi = this.pointInfo(op.idx + 1);
      tooltip(chip, () => `<b>${esc(op.text)}</b><br>cut power right after this call: ${pi ? pi.states + " possible disk state(s)" : ""}`);
      el.append(chip);
    }
  }

  pick(point) {
    this.s.point = point;
    this.s.index = 0;
    this.noViolation = false;
    this.inspect();
  }

  renderCrashBar() {
    const d = this.d;
    const last = this.trace.trace.slice(0, d.point).reverse().find((o) => o.kind !== "note");
    const step = (delta) => { this.s.index = (d.index + delta + d.count) % d.count; this.inspect(); };
    fill(this.elCrashBar, 
      h("span", { class: "bolt" }, "⚡"),
      h("span", {}, d.point === 0 ? "Power cut before the first syscall" : h("span", {}, "Power cut right after ", h("code", {}, `#${last?.n} ${last?.text}`))),
      h("span", { class: "muted" }, `phase “${d.phase}” · saves acknowledged: ${d.acked}`),
      h("div", { class: "spacer" }),
      h("span", { class: "muted small" }, `${fmt.int(d.count)} possible disk state${d.count === 1 ? "" : "s"}${d.exhaustive ? " (exhaustive)" : " (sampled)"}`),
      h("div", { class: "row" },
        h("button", { class: "btn", onclick: () => step(-1), "aria-label": "previous state" }, "◀"),
        h("span", { class: "mono" }, `state ${d.index + 1} / ${d.count}`),
        h("button", { class: "btn", onclick: () => step(1), "aria-label": "next state" }, "▶"),
      ),
    );
  }

  renderOps() {
    const d = this.d;
    const ops = this.trace.trace.filter((o) => o.kind !== "note" && (o.idx < d.point || d.op_status[o.idx] === "torn"));
    const list = h("div", { class: "oplist" });
    if (!ops.length) list.append(h("div", { class: "muted" }, "Nothing was issued yet: the disk still holds the starting state."));
    for (const o of ops) {
      const st = d.op_status[o.idx] || "persisted";
      const [cls, label] = STATUS[st] || ["neutral", st];
      list.append(h("div", { class: "o " + st }, h("span", { class: "n" }, "#" + o.n), pill(cls, label), h("span", { class: "t" }, o.text)));
    }
    fill(this.elOps, 
      h("h3", {}, "Which writes survived"),
      h("p", { class: "muted small", style: { margin: "4px 0 10px" } }, d.crash.text),
      list,
      h("p", { class: "muted small", style: { marginTop: "10px" } },
        "A barrier (fsync) guarantees everything before it on that file. Writes no barrier covers may be lost, reordered or torn."),
    );
  }

  renderDisk() {
    const d = this.d;
    const box = h("div");
    const opsByIdx = Object.fromEntries(this.trace.trace.map((o) => [o.idx, o]));
    for (const f of d.disk) {
      const sec = h("div", { class: "sectors" });
      const owners = f.owners.slice(0, 320);
      owners.forEach((o, i) => {
        let cls = "sector";
        const st = o >= 0 ? d.op_status[o] : null;
        if (st === "torn") cls += " t";
        else if (st === "survived") cls += " l";
        else if (o >= 0) cls += " w";
        const cell = h("span", { class: cls });
        tooltip(cell, () => o < 0 ? `sector ${i}: unchanged since before the save` : `sector ${i}: written by #${opsByIdx[o]?.n} ${esc(opsByIdx[o]?.text || "")}`);
        sec.append(cell);
      });
      if (f.owners.length > 320) sec.append(h("span", { class: "muted small" }, ` +${f.owners.length - 320} more`));
      if (!f.owners.length) sec.append(h("span", { class: "muted small" }, "(empty file)"));
      box.append(h("div", { class: "disk-file" }, h("div", { class: "name" }, h("span", {}, f.path), h("span", { class: "muted" }, fmt.int(f.size) + " B")), sec));
    }
    fill(this.elDisk, 
      h("h3", {}, "Disk image after the crash"),
      h("p", { class: "muted small", style: { margin: "4px 0 10px" } }, "One square per 512-byte sector."),
      box,
      h("div", { class: "legend" },
        h("span", {}, h("i", { style: { background: "var(--base)" } }), "unchanged"),
        h("span", {}, h("i", { style: { background: "var(--old)" } }), "written (durable)"),
        h("span", {}, h("i", { style: { background: "var(--lucky)" } }), "unforced write that happened to land"),
        h("span", {}, h("i", { style: { background: "repeating-linear-gradient(45deg, var(--new) 0 3px, transparent 3px 6px), var(--new-soft)" } }), "torn write"),
      ),
    );
  }

  renderRecovery() {
    const r = this.d.recovery;
    const icons = { ok: "✓", replay: "↻", rollback: "↶", discard: "✂", select: "➜", warn: "!", error: "✗", info: "·" };
    const list = h("ul", { class: "events" });
    for (const ev of r.events) list.append(h("li", {}, h("span", { class: "ic " + ev.kind }, icons[ev.kind] || "·"), h("span", {}, ev.msg)));
    if (!r.events.length) list.append(h("li", { class: "muted" }, "(no recovery log)"));
    fill(this.elRecovery, 
      h("div", { class: "row", style: { justifyContent: "space-between" } }, h("h3", {}, "Recovery log"),
        h("span", { class: "muted small" }, r.writes.length ? `recovery issued ${r.writes.length} write/sync syscall(s), and those are crash-tested too`
          : "recovery only read the disk: nothing to repair")),
      h("div", { style: { marginTop: "10px" } }, list),
    );
  }

  renderVerdict(flash) {
    const d = this.d;
    const o = d.outcome;
    const ok = o.code === "OK";
    const info = OUTCOME[o.code] || { text: o.code };
    const head = h("div", { class: "verdict " + (ok ? "good" : "bad") + (flash ? " flash" : "") },
      h("div", { class: "big" }, ok ? "✓" : "✗"),
      h("div", {},
        h("h3", {}, ok ? `Recovered the ${o.label}: byte-identical` : `${o.code.replace("_", " ")}: ${info.text}`),
        h("div", { class: "muted" }, ok
          ? (d.acked >= d.started && d.started > 0
            ? "The save had been acknowledged, so only the new version is allowed, and that is what came back."
            : d.started === 0 ? "Nothing had been written yet." : "The save was not acknowledged yet, so the previous or the new version is allowed. Never a mixture.")
          : o.detail),
        this.noViolation ? h("div", { class: "small", style: { marginTop: "4px" } }, "🎯 No violating crash state exists for this engine and scenario. Here is a random one instead.") : null,
      ));
    const docs = d.documents.map((doc) => this.renderDoc(doc));
    fill(this.elVerdict, h("div", { class: "card" }, head, docs));
  }

  renderDoc(doc) {
    const n = this.trace.versions.length - 1;
    const statusPill = {
      match: () => pill("good", "✓ " + doc.text),
      absent: () => pill(doc.match?.length ? "good" : "bad", (doc.match?.length ? "✓ " : "✗ ") + doc.text),
      corrupt: () => pill("bad", "✗ corrupt"),
      detected: () => pill("bad", "✗ checksum caught damage"),
      unrecoverable: () => pill("bad", "✗ unrecoverable"),
    }[doc.status]();
    const wrap = h("div", { class: "doc" }, h("div", { class: "doc-head" }, h("b", { class: "mono" }, doc.key), statusPill,
      doc.len !== undefined ? h("span", { class: "muted small" }, `${fmt.int(doc.len)} bytes · sha256 ${doc.sha256}`) : null));
    if (doc.status === "match") {
      const j = doc.match[doc.match.length - 1];
      wrap.append(h("div", { class: "strip" }, h("span", { class: j === 0 ? "s-old" : j === n ? "s-new" : "s-same", style: { width: "100%" } })));
    } else if (doc.provenance) {
      const strip = h("div", { class: "strip" });
      const total = doc.len || 1;
      const cls = { old: "s-old", new: "s-new", "=": "s-same", "0": "s-zero", "?": "s-junk" };
      const name = { old: "previous version", new: "new version", "=": "same in both", "0": "zeros", "?": "unrecognised bytes" };
      for (const sgm of doc.provenance) {
        const seg = h("span", { class: cls[sgm.src] || "s-same", style: { width: (100 * (sgm.end - sgm.start) / total) + "%" } });
        tooltip(seg, `bytes ${sgm.start}-${sgm.end}: ${name[sgm.src] || sgm.src}`);
        strip.append(seg);
      }
      wrap.append(h("p", { class: "small", style: { marginTop: "6px" } }, h("b", {}, "A corrupted mixture: "), doc.text), strip,
        h("div", { class: "legend" },
          h("span", {}, h("i", { style: { background: "var(--old)" } }), "byte from previous version"),
          h("span", {}, h("i", { style: { background: "var(--new)" } }), "byte from new version"),
          h("span", {}, h("i", { style: { background: "var(--axis)" } }), "same in both"),
          h("span", {}, h("i", { style: { background: "var(--zero)" } }), "zeros"),
          h("span", {}, h("i", { style: { background: "var(--junk)" } }), "unrecognised")));
    } else if (doc.text && doc.status !== "match") {
      wrap.append(h("p", { class: "small muted", style: { marginTop: "6px" } }, doc.text));
    }
    if (doc.preview !== undefined) wrap.append(doc.provenance ? colouredPreview(doc.preview, doc.provenance) : h("pre", { class: "preview" }, doc.preview));
    return wrap;
  }
}

// The preview text with each byte's background showing which version it came from.
function colouredPreview(text, segs) {
  const pre = h("pre", { class: "preview" });
  const bytes = new TextEncoder().encode(text);
  const cls = { old: "p-old", new: "p-new", "0": "p-zero", "?": "p-junk" };
  for (const s of segs) {
    if (s.start >= bytes.length) break;
    const part = new TextDecoder().decode(bytes.slice(s.start, Math.min(s.end, bytes.length)));
    pre.append(cls[s.src] ? h("span", { class: cls[s.src] }, part) : document.createTextNode(part));
  }
  return pre;
}

function shortText(op) {
  const t = op.text.replace(/^[a-z_]+\(/, "").replace(/\)$/, "");
  return t.length > 46 ? t.slice(0, 44) + "…" : t;
}
