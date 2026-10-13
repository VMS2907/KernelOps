// Crash Arena projector. Every number on this page is copied from the server's
// verdict counters: nothing is interpolated, animated upwards or estimated here.
import { h, svg, fill, fmt } from "./util.js";

const $ = (id) => document.getElementById(id);
const S = { meta: null, snap: null, isHost: false, epoch: null, feed: [], galleryKey: "", processed: -1, resetArmed: 0 };
const FEED_MAX = 9;

// The host token arrives once as ?host=...; the server answered with an HttpOnly
// cookie, so take the token out of the address bar before the room can read it.
if (location.search.includes("host=")) history.replaceState(null, "", location.pathname);

// ------------------------------------------------------------------ helpers
function qrSvg(text) {
  const q = window.qrcode(0, "M");
  q.addData(text);
  q.make();
  const n = q.getModuleCount(), m = 4, size = n + 2 * m;
  let d = "";
  for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) if (q.isDark(r, c)) d += `M${c + m} ${r + m}h1v1h-1z`;
  return svg("svg", { viewBox: `0 0 ${size} ${size}`, "shape-rendering": "crispEdges", role: "img", "aria-label": "QR code for " + text },
    svg("rect", { width: size, height: size, fill: "#fff" }), svg("path", { d, fill: "#000" }));
}

function toast(text, ms = 1800) {
  let t = document.querySelector(".toast");
  if (!t) { t = h("div", { class: "toast" }); document.body.append(t); }
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, ms);
}

function banner(text) {
  const b = $("banner");
  b.hidden = !text;
  if (text) b.textContent = text;
}

const pct = (a, b) => (b ? Math.round((100 * a) / b) + "%" : "");
const verdictInfo = (code) => (S.meta && S.meta.scenario.verdicts[code]) || { short: code, story: "" };
const who = (p) => [h("b", {}, p.nickname), p.source === "auto" ? [" ", h("span", { class: "auto" }, "AUTO")] : null];

function pulse(el) {
  el.classList.remove("pulse");
  void el.offsetWidth;
  el.classList.add("pulse");
}

// ------------------------------------------------------------------ hello (static facts of this run)
function onHello(m) {
  S.meta = m;
  S.isHost = m.is_host;
  $("host").hidden = !m.is_host;
  $("viewer").hidden = m.is_host;
  const url = m.public_url || location.origin + "/tap";
  fill($("url"), url);
  fill($("qr"), qrSvg(url));
  fill($("mini-qr"), qrSvg(url));
  const local = /\/\/(127\.|localhost)/.test(url);
  $("url-warn").hidden = !local;
  if (local) $("url-warn").textContent = "Phones cannot reach " + new URL(url).host +
    ". Join the hotspot/router, then restart with  --url-host <this laptop's Wi-Fi address>.";
  fill($("naive-sub"), `open(path, "w") · engine ${m.specs.naive}`);
  fill($("safe-sub"), `${m.titles.safe.toLowerCase()} · engine ${m.specs.safe}`);
  document.title = `Crash Arena · ${m.scenario.document}`;
}

// ------------------------------------------------------------------ stats (every 200 ms)
function onStats(s) {
  S.snap = s;
  if (S.epoch !== null && s.epoch !== S.epoch) { S.feed = []; renderFeed(); }
  S.epoch = s.epoch;
  document.body.className = "arena state-" + s.state;

  const tp = s.taps.phone, ta = s.taps.auto;
  const received = tp.accepted + tp.rate_limited + tp.queue_full + tp.backlog;   // taps while live
  const naive = s.sides.naive, safe = s.sides.safe;
  $("s-taps").textContent = fmt.int(received);
  $("s-crashes").textContent = fmt.int(s.processed);
  $("s-rate").textContent = s.rate >= 10 ? Math.round(s.rate) : s.rate.toFixed(1);
  $("s-phones").textContent = `${fmt.int(s.phones.joined)}/${fmt.int(s.phones.max)}`;

  const leaders = s.leaderboard.length
    ? s.leaderboard.map((l) => h("li", {}, h("span", {}, l.nickname), h("b", {}, fmt.int(l.crashes))))
    : [h("li", { class: "title" }, "nobody yet")];
  fill($("leaders"), h("li", { class: "title" }, "top crashers"), leaders);

  // the two sides
  for (const [side, st] of [["naive", naive], ["safe", safe]]) {
    $(side + "-crashes").textContent = fmt.int(st.crashes);
    $(side + "-bad").textContent = fmt.int(st.corrupted);
    $(side + "-pct").textContent = st.crashes ? pct(st.corrupted, st.crashes) + " of crashes" : "";
    const v = st.verdicts, total = st.crashes || 1;
    const bad = st.corrupted;
    fill($(side + "-mix"),
      h("span", { class: "m-bad", style: { width: (100 * bad) / total + "%" }, title: "corrupted" }),
      h("span", { class: "m-OLD", style: { width: (100 * (v.OLD || 0)) / total + "%" }, title: "previous version" }),
      h("span", { class: "m-NEW", style: { width: (100 * (v.NEW || 0)) / total + "%" }, title: "new version" }));
  }
  if (S.processed >= 0 && s.processed > S.processed) {
    pulse($("safe-bad").parentElement);
  }
  S.processed = s.processed;
  renderGallery(s.gallery.naive);

  // honesty line: every tap is accounted for
  const D = S.meta ? S.meta.delay_max_ms : 0;
  const kill = S.meta ? S.meta.kill : "SIGKILL";
  fill($("honest"),
    "Every number is a real process killed with ", h("b", {}, kill), ", recovered from disk and judged. Taps ",
    h("b", {}, fmt.int(received)), " → crashed ", h("b", {}, fmt.int(s.processed)),
    " · waiting ", h("b", {}, fmt.int(s.queue.pending + s.queue.in_flight)),
    " · dropped (writers busy) ", h("b", {}, fmt.int(tp.queue_full + tp.backlog)),
    " · over 8/s ", h("b", {}, fmt.int(tp.rate_limited)),
    s.discarded ? [" · not run (frozen) ", h("b", {}, fmt.int(s.discarded))] : null,
    s.failed ? [" · failed ", h("b", {}, fmt.int(s.failed))] : null,
    (ta.received || s.autotap) ? [" · ", h("b", {}, s.autotap ? "AUTO-TAP ON" : "auto-taps"), " ", h("b", {}, fmt.int(ta.accepted))] : null,
    ` · one tap = one kill on each side, same random 0–${D} ms delay`);

  // lobby
  $("joined").textContent = fmt.int(s.phones.joined);
  $("joined-l").textContent = `of ${fmt.int(s.phones.max)} phones joined` +
    (s.phones.full ? " · the arena is full (a seat frees up when a phone leaves)" : "");
  fill($("names"), s.phones.recent.map((n) => h("span", {}, n)));
  const ready = (x) => `${x.ready}/${x.pool}`;
  $("warm").textContent = `writer processes warm: normal save ${ready(naive)} · SafeWrite ${ready(safe)}` +
    (s.pool.errors ? ` · start-up errors: ${s.pool.errors}` : "");

  // frozen summary
  const students = s.phones.participants;
  fill($("f1"), h("b", {}, fmt.int(students)), students === 1 ? " student. " : " students. ",
    h("b", {}, fmt.int(naive.crashes + safe.crashes)), " real crashes.");
  fill($("f2"), "Normal save destroyed ", h("b", {}, fmt.int(naive.corrupted)), naive.corrupted === 1 ? " file." : " files.");
  fill($("f3"), "SafeWrite lost ", h("b", {}, fmt.int(safe.corrupted)), ".");
  const vs = (v) => Object.entries(v).sort((a, b) => b[1] - a[1]).map(([k, n]) => `${k} ${fmt.int(n)}`).join(" · ");
  fill($("f5"), `${fmt.int(s.processed)} taps crashed both sides (${fmt.int(received)} received). `,
    `Normal save: ${vs(naive.verdicts) || "-"}.  SafeWrite: ${vs(safe.verdicts) || "-"}.  `,
    s.latency_ms.p95 != null ? `Tap → verdict p95 ${fmt.int(Math.round(s.latency_ms.p95))} ms.` : "");

  const auto = $("auto-btn");
  auto.textContent = "A · Auto-tap " + (s.autotap ? "ON" : "off");
  auto.classList.toggle("on", s.autotap);
}

function renderGallery(items) {
  const key = items.map((g) => g.id).join(",");
  if (key === S.galleryKey) return;
  S.galleryKey = key;
  if (!items.length) {
    fill($("gallery"), h("div", { class: "empty-l" }, "No broken files yet. Pull the plug!"));
    return;
  }
  fill($("gallery"), items.slice(0, 4).map((g) => {
    const info = verdictInfo(g.verdict);
    const showDetail = g.verdict !== "EMPTY";
    return h("div", { class: "g-card" },
      h("div", { class: "t" }, who(g), `'s crash · save #${fmt.int(g.inflight)}`),
      h("div", { class: "big" }, info.short, info.story ? h("span", { style: { fontWeight: 600 } }, " — " + info.story) : null),
      showDetail ? h("div", { class: "d" }, g.detail) : null);
  }));
}

// ------------------------------------------------------------------ crash pairs (the feed)
function onCrashes(pairs) {
  for (const p of pairs) S.feed.unshift(p);
  S.feed.length = Math.min(S.feed.length, FEED_MAX);
  renderFeed();
}

function renderFeed() {
  if (!S.feed.length) {
    fill($("feed"), h("div", { class: "empty-l" }, "Waiting for the first crash…"));
    return;
  }
  fill($("feed"), S.feed.map((p) => {
    const v = p.sides.safe;
    if (!v.ok) {
      return h("div", { class: "feed-l" }, h("span", { class: "no" }, "✗"),
        h("span", {}, who(p), ` crashed save #${fmt.int(v.inflight)} → ${v.verdict}: ${v.detail}`));
    }
    const tail = v.verdict === "NEW" ? h("span", { class: "why" }, "(it finished just before the kill)") : null;
    return h("div", { class: "feed-l" }, h("span", { class: "ok" }, "✓"),
      h("span", {}, who(p), ` crashed save #${fmt.int(v.inflight)} → recovered version ${fmt.int(v.recovered)} `, tail));
  }));
}

// ------------------------------------------------------------------ host controls
async function control(action) {
  try {
    const r = await fetch("/api/arena/control", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    onStats(d);
  } catch (e) {
    toast(`${action}: ${e.message}`, 3000);
  }
}

function act(name) {
  if (!S.isHost) { toast("View only: open the host URL printed in the terminal"); return; }
  const st = S.snap ? S.snap.state : "LOBBY";
  if (name === "start") control("start");
  else if (name === "freeze") control("freeze");
  else if (name === "autotap") control(S.snap && S.snap.autotap ? "autotap_off" : "autotap_on");
  else if (name === "reset") {
    if (Date.now() - S.resetArmed < 2500) { S.resetArmed = 0; control("reset"); toast("Reset: back to the lobby"); }
    else { S.resetArmed = Date.now(); toast(st === "LOBBY" ? "Press R again to reset" : "Press R again to reset all counters"); }
  }
}

document.addEventListener("keydown", (ev) => {
  if (ev.ctrlKey || ev.metaKey || ev.altKey || ev.repeat) return;
  const k = ev.key.toLowerCase();
  const map = { s: "start", f: "freeze", r: "reset", a: "autotap" };
  if (map[k]) { ev.preventDefault(); act(map[k]); }
  else if (k === "t") {
    const root = document.documentElement;
    root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
  }
});
document.querySelectorAll("#host [data-act]").forEach((b) => b.addEventListener("click", () => act(b.dataset.act)));

// ------------------------------------------------------------------ live connection
let retries = 0;
function connect() {
  const es = new EventSource("/api/arena/stream");
  const on = (name, fn) => es.addEventListener(name, (ev) => fn(JSON.parse(ev.data)));
  on("hello", (m) => { retries = 0; banner(null); onHello(m); });
  on("stats", onStats);
  on("crashes", onCrashes);
  es.onerror = () => {
    es.close();
    banner("Reconnecting to the arena server…");
    setTimeout(connect, Math.min(5000, 400 * 2 ** retries++));
  };
}
renderFeed();
connect();
