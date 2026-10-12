// Crash Lab entry point: loads metadata, wires the tabs, owns cross-view navigation.
import { api } from "./util.js";
import { LabView } from "./lab.js";
import { ExplorerView } from "./explorer.js";
import { TortureView } from "./torture.js";
import { BenchView } from "./bench.js";
import { DocsView } from "./docs.js";
import { ConceptsView } from "./concepts.js";

const app = {
  meta: null,
  views: {},
  current: "lab",
  show(tab) {
    this.current = tab;
    document.querySelectorAll("nav.tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === tab));
    document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + tab));
    this.views[tab]?.activate?.();
    try { history.replaceState(null, "", "#" + tab); } catch (_) {}
  },
  // Explorer -> Lab: open one exact crash state.
  openInLab(args) {
    this.show("lab");
    this.views.lab.load(args);
  },
};

function initTheme() {
  const btn = document.getElementById("theme");
  let saved = null;
  try { saved = localStorage.getItem("sw-theme"); } catch (_) {}
  if (saved) document.documentElement.dataset.theme = saved;
  btn.addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("sw-theme", next); } catch (_) {}
  });
}

async function main() {
  initTheme();
  app.meta = await api("/api/meta");
  app.views = {
    lab: new LabView(document.getElementById("view-lab"), app),
    explorer: new ExplorerView(document.getElementById("view-explorer"), app),
    torture: new TortureView(document.getElementById("view-torture"), app),
    bench: new BenchView(document.getElementById("view-bench"), app),
    docs: new DocsView(document.getElementById("view-docs"), app),
    concepts: new ConceptsView(document.getElementById("view-concepts"), app),
  };
  document.querySelectorAll("nav.tabs button").forEach((b) => b.addEventListener("click", () => app.show(b.dataset.tab)));
  const initial = (location.hash || "#lab").slice(1);
  app.show(app.views[initial] ? initial : "lab");
  window.addEventListener("hashchange", () => {
    const tab = location.hash.slice(1);
    if (app.views[tab] && tab !== app.current) app.show(tab);
  });
}

main().catch((e) => {
  document.querySelector("main").innerHTML = `<div class="card">Could not start the Crash Lab: ${e.message}</div>`;
});
