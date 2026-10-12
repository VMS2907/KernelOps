// Torture: real processes, real files, real SIGKILL. Live.
import { h, clear, stream, pill, fmt, tooltip, esc } from "./util.js";

const DEFAULT = ["naive", "naive/trunc", "shadow", "undo", "wal", "cow"];

export class TortureView {
  constructor(root, app) {
    this.root = root;
    this.app = app;
    this.es = null;
    this.cards = {};
    this.build();
  }

  activate() {}

  build() {
    const engines = this.app.meta.engines.filter((e) => !e.variant || e.name === "naive");
    this.checks = h("div", { class: "checks" }, engines.map((e) =>
      h("label", {}, h("input", { type: "checkbox", value: e.spec, checked: DEFAULT.includes(e.spec) }), e.spec)));
    this.trials = h("input", { type: "number", value: 60, min: 1, max: 2000 });
    this.size = h("select", {}, [["4096", "4 KB"], ["65536", "64 KB"], ["1048576", "1 MB"]].map(([v, l]) => h("option", { value: v, selected: v === "65536" }, l)));
    this.btn = h("button", { class: "btn danger", onclick: () => this.toggle() }, "⚡ Start killing");
    this.grid = h("div", { class: "grid g3", style: { marginTop: "14px" } });
    this.root.append(
      h("div", { class: "card" },
        h("h2", {}, "kill -9 torture test"),
        h("p", { class: "muted", style: { marginTop: "4px" } },
          "A separate writer process saves version n+1, n+2, … of a document and reports each version after save() returns. ",
          "We SIGKILL it at a random moment: no cleanup, no flush, no goodbye. Then we open the store (running recovery) on the files it left, ",
          "and check the document is byte-identical to the last acknowledged version or to the one in flight."),
        h("div", { class: "controls", style: { marginTop: "10px" } },
          h("div", { class: "field" }, h("span", {}, "Engines"), this.checks),
          h("label", { class: "field" }, h("span", {}, "Kills per engine"), this.trials),
          h("label", { class: "field" }, h("span", {}, "Document size"), this.size),
          h("div", { class: "spacer" }), this.btn),
        h("div", { class: "legend" },
          h("span", {}, h("i", { style: { background: "var(--old)", borderRadius: "50%" } }), "recovered last acknowledged version"),
          h("span", {}, h("i", { style: { background: "var(--new)", borderRadius: "50%" } }), "recovered in-flight version (committed just before its ack)"),
          h("span", {}, h("i", { style: { background: "var(--bad)", transform: "rotate(45deg) scale(.9)" } }), "✗ corrupt / lost / unreadable"),
        ),
      ),
      this.grid,
      h("p", { class: "muted small", style: { marginTop: "12px" } },
        "A kill -9 keeps the kernel's page cache, so this proves atomicity on a real file system. Power loss, where the cache is lost, ",
        "is covered by the Explorer's simulator. Note how the unsafe baseline fails here too: it does not even survive a process crash."),
    );
  }

  toggle() {
    if (this.es) { this.stop(); return; }
    const engines = [...this.checks.querySelectorAll("input:checked")].map((i) => i.value);
    if (!engines.length) return;
    clear(this.grid);
    this.cards = {};
    for (const e of engines) this.grid.append(this.card(e));
    const q = new URLSearchParams({ engines: engines.join(","), trials: this.trials.value, size: this.size.value });
    this.btn.textContent = "■ Stop";
    this.es = stream("/api/torture/stream?" + q, {
      engine: (ev) => this.cards[ev.engine]?.status("running…"),
      kill: (ev) => this.onKill(ev),
      result: (r) => this.cards[r.engine]?.finish(r),
      done: () => this.stop(),
      error: () => this.stop(),
    });
  }

  stop() {
    if (this.es) this.es.close();
    this.es = null;
    this.btn.textContent = "⚡ Start killing";
  }

  card(engine) {
    const info = this.app.meta.engines.find((e) => e.spec === engine);
    const dots = h("div", { class: "dots" });
    const nOld = h("div", { class: "n" }, "0"), nNew = h("div", { class: "n" }, "0"), nBad = h("div", { class: "n" }, "0");
    const ticker = h("div", { class: "ticker" }, "waiting…");
    const badge = h("span", {}, info.expected_safe ? pill("neutral", "crash-consistent") : pill("neutral", "unsafe baseline"));
    const el = h("div", { class: "card t-card" },
      h("h3", {}, h("span", { class: "mono" }, engine), badge),
      h("div", { class: "muted small" }, info.title),
      h("div", { class: "counts" },
        h("div", {}, nOld, h("div", { class: "l" }, "✓ old version")),
        h("div", {}, nNew, h("div", { class: "l" }, "✓ new version")),
        h("div", {}, nBad, h("div", { class: "l" }, "✗ corrupt / lost"))),
      dots, ticker);
    const c = { old: 0, neu: 0, bad: 0 };
    this.cards[engine] = {
      status: (t) => { ticker.textContent = t; },
      kill: (ev) => {
        const code = ev.code;
        const kind = code === "OK_OLD" ? "old" : code === "OK_NEW" ? "new" : "bad";
        if (kind === "old") c.old++; else if (kind === "new") c.neu++; else c.bad++;
        nOld.textContent = c.old; nNew.textContent = c.neu; nBad.textContent = c.bad;
        const dot = h("i", { class: kind });
        tooltip(dot, `kill #${ev.trial} after ${ev.delay_ms} ms<br>${esc(ev.detail)}`);
        dots.append(dot);
        ticker.textContent = `kill #${ev.trial}/${ev.trials} after ${ev.delay_ms} ms → ${ev.detail}`;
        if (kind === "bad") el.classList.add("flash");
      },
      finish: (r) => {
        badge.replaceChildren(r.violations ? pill("bad", `✗ ${r.violations} violations`) : pill("good", `✓ ${r.trials} kills, 0 violations`));
        ticker.textContent = `${r.trials} real crashes in ${r.seconds}s · ${fmt.int(Math.round(r.puts_per_sec))} durable saves/s`;
      },
    };
    return el;
  }

  onKill(ev) { this.cards[ev.engine]?.kill(ev); }
}
