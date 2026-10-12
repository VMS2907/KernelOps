// Documents: a real store on the real disk. Edit, save, browse history, and crash the writer for real.
import { h, fill, api, clear, pill, fmt } from "./util.js";

export class DocsView {
  constructor(root, app) {
    this.root = root;
    this.app = app;
    this.key = "inventory.csv";
    this.loaded = false;
    this.build();
  }

  build() {
    this.elList = h("div", { class: "doclist" });
    this.elInfo = h("div", { class: "muted small" });
    this.elKey = h("input", { type: "text", value: this.key, "aria-label": "Document key", style: { flex: "1" } });
    this.elText = h("textarea", { spellcheck: "false", "aria-label": "Document content" });
    this.elAt = h("select", { "aria-label": "Crash point" });
    this.elMsg = h("div", { class: "small", style: { minHeight: "20px" } });
    this.elHist = h("div", { class: "hist" });
    this.elCrash = h("div");
    this.root.append(
      h("div", { class: "grid", style: { gridTemplateColumns: "minmax(0, 3fr) minmax(0, 9fr)" } },
        h("div", { class: "card" },
          h("h3", {}, "Real store"), this.elInfo,
          h("div", { style: { marginTop: "10px" } }, this.elList),
          h("button", { class: "btn", style: { marginTop: "10px" }, onclick: () => this.select("new-document.txt", "") }, "+ New document"),
          h("button", { class: "btn", style: { marginTop: "6px" }, onclick: () => this.verify() }, "Verify all checksums"),
        ),
        h("div", { class: "stack" },
          h("div", { class: "card" },
            h("div", { class: "row" }, h("h3", {}, "Edit"), this.elKey),
            h("div", { style: { marginTop: "10px" } }, this.elText),
            h("div", { class: "row", style: { marginTop: "10px" } },
              h("button", { class: "btn primary", onclick: () => this.save() }, "Save (atomic + durable)"),
              h("div", { class: "spacer" }),
              h("span", { class: "muted small" }, "kill the writer at"), this.elAt,
              h("button", { class: "btn danger", onclick: () => this.crash() }, "⚡ Save & kill -9 mid-save"),
            ),
            this.elMsg,
          ),
          this.elCrash,
          h("div", { class: "card" }, h("h3", {}, "Version history (copy-on-write keeps old generations)"),
            h("p", { class: "muted small", style: { margin: "4px 0 8px" } }, "Old blocks are never overwritten, so earlier versions stay readable until garbage collection reclaims them."),
            this.elHist),
        ),
      ),
    );
    this.elAt.append(h("option", { value: "0" }, "a random syscall"), ...[1, 2, 3, 4, 5].map((n) => h("option", { value: String(n) }, `syscall #${n}`)));
  }

  async activate() {
    if (!this.loaded) { this.loaded = true; await this.refresh(); await this.select(this.key); }
  }

  async refresh(summary) {
    const s = summary || await api("/api/store");
    fill(this.elInfo, h("span", {}, `${s.engine} engine · generation ${s.generation ?? "–"}`), h("br"), h("span", { class: "mono" }, s.path));
    clear(this.elList);
    for (const d of s.documents) {
      this.elList.append(h("button", { class: d.key === this.key ? "sel" : "", onclick: () => this.select(d.key) },
        h("span", { class: "mono" }, d.key), d.ok ? h("span", { class: "muted small" }, fmt.int(d.len) + " B") : pill("bad", "damaged")));
    }
  }

  async select(key, text) {
    this.key = key;
    this.elKey.value = key;
    if (text !== undefined) { this.elText.value = text; clear(this.elHist); this.refresh(); return; }
    const d = await api("/api/store/doc?key=" + encodeURIComponent(key));
    this.elText.value = d.text ?? "";
    this.renderHistory(d.history);
    this.refresh();
  }

  renderHistory(hist) {
    clear(this.elHist);
    if (!hist.length) { this.elHist.append(h("div", { class: "muted small" }, "No versions yet.")); return; }
    hist.forEach((v, i) => {
      const when = v.timestamp ? new Date(v.timestamp * 1000).toLocaleTimeString() : "";
      this.elHist.append(h("div", { class: "h" },
        h("span", {}, v.deleted ? pill("bad", "deleted") : h("b", {}, `v${v.version}`), " ", h("span", { class: "muted" }, when)),
        v.deleted ? null : h("span", { class: "mono muted" }, `${fmt.int(v.length)} B · ${v.sha256.slice(0, 12)}`),
        i === 0 || v.deleted ? (i === 0 ? pill("neutral", "current") : null)
          : h("button", { class: "btn", onclick: () => this.restore(v.version) }, "Restore")));
    });
  }

  async save() {
    const r = await api("/api/store/doc", { key: this.elKey.value, text: this.elText.value });
    this.elMsg.replaceChildren(pill("good", `✓ saved in ${fmt.ms(r.ms)}: committed and fsynced`));
    await this.select(this.elKey.value);
    this.refresh(r.summary);
  }

  async restore(version) {
    const r = await api("/api/store/restore", { key: this.key, version });
    this.elMsg.replaceChildren(pill("good", `✓ restored v${version} as a new version`));
    await this.select(this.key);
    this.refresh(r.summary);
  }

  async verify() {
    const s = await api("/api/store");
    const bad = s.documents.filter((d) => !d.ok);
    this.elMsg.replaceChildren(bad.length ? pill("bad", `✗ ${bad.length} damaged`) : pill("good", `✓ all ${s.documents.length} documents verified`));
  }

  async crash() {
    this.elMsg.replaceChildren(h("span", { class: "spin" }), " saving in a child process… then killing it…");
    const r = await api("/api/store/crash", { key: this.elKey.value, text: this.elText.value, at: Number(this.elAt.value) });
    const c = r.crash;
    const good = c.verdict === "old" || c.verdict === "new" || c.verdict === "absent";
    const what = { old: "the PREVIOUS version (the save never reached its commit point)", new: "the NEW version (the commit point was passed before the kill)",
      absent: "no document (it never existed before)", damaged: "a damaged document" }[c.verdict];
    const icons = { ok: "✓", replay: "↻", rollback: "↶", discard: "✂", select: "➜", warn: "!", error: "✗", info: "·" };
    const plan = c.plan.filter((o) => o.kind !== "note");
    fill(this.elCrash, h("div", { class: "card flash" },
      h("div", { class: "verdict " + (good ? "good" : "bad") }, h("div", { class: "big" }, good ? "✓" : "✗"),
        h("div", {}, h("h3", {}, `Writer killed at syscall #${c.at} of ${c.syscalls} (exit code ${c.exit_code})`),
          h("div", {}, `After reopening, recovery (${fmt.ms(c.recovery_ms)}) found ${what}.`))),
      h("div", { class: "grid g2", style: { marginTop: "12px" } },
        h("div", {}, h("h3", {}, "The save's syscalls"),
          h("div", { class: "oplist", style: { marginTop: "6px" } }, plan.map((o) => h("div", { class: "o" + (o.n >= c.at ? " lost" : "") },
            h("span", { class: "n" }, "#" + o.n), o.n === c.at ? pill("bad", "⚡ killed here") : o.n < c.at ? pill("neutral", "✓ done") : pill("neutral", "never ran"),
            h("span", { class: "t" }, o.text))))),
        h("div", {}, h("h3", {}, "Recovery log"),
          h("ul", { class: "events", style: { marginTop: "6px" } }, c.recovery.events.map((ev) => h("li", {}, h("span", { class: "ic " + ev.kind }, icons[ev.kind] || "·"), h("span", {}, ev.msg))))),
      ),
      h("details", { style: { marginTop: "10px" } }, h("summary", { class: "muted small" }, "child process output"), h("div", { class: "crash-log" }, c.child_output + (c.child_error ? "\n" + c.child_error : ""))),
    ));
    this.elMsg.replaceChildren();
    await this.select(this.elKey.value);
    this.refresh(r.summary);
  }
}
