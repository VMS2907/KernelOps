// SafeWrite launch film: a deterministic timeline. renderAt(t) draws frame t.
// Every visual beat also emits a sound cue (window.TIMELINE.cues) so the score
// generator can place each hit, whoosh and power-down exactly on the cut.
"use strict";

const W = 1920, H = 1080;
const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
const lerp = (a, b, t) => a + (b - a) * t;
const E = {
  out: (t) => 1 - Math.pow(1 - t, 3),
  in: (t) => t * t * t,
  io: (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2),
  back: (t) => { const c1 = 1.70158, c3 = c1 + 1; return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2); },
};
const P = (lt, a, d) => clamp((lt - a) / d);

function el(tag, cls, html, style) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined && html !== null) n.innerHTML = html;
  if (style) Object.assign(n.style, style);
  return n;
}
function appear(n, p, o = {}) {
  const { dy = 36, s0 = 1, blur = 0, dx = 0 } = o;
  n.style.opacity = p;
  n.style.transform = `translate(${(1 - p) * dx}px, ${(1 - p) * dy}px) scale(${s0 + (1 - s0) * p})`;
  if (blur) n.style.filter = `blur(${(1 - p) * blur}px)`;
}
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// ------------------------------------------------------------------ data
let DATA = { cli: null, footage: {} };
const pending = [];

function frameAt(shot, ft) {
  const fr = DATA.footage[shot].frames;
  let lo = 0, hi = fr.length - 1;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (fr[mid].t <= ft) lo = mid; else hi = mid - 1; }
  return `build/footage/${shot}/${fr[lo].file}`;
}

// A browser window showing real footage, with a camera (focus point + zoom).
function browserWindow(root, shot, box, url) {
  const win = el("div", "window", null, { left: box.x + "px", top: box.y + "px", width: box.w + "px", height: box.h + "px" });
  const bar = el("div", "bar", `<i style="background:#ff5f57"></i><i style="background:#febc2e"></i><i style="background:#28c840"></i><span class="url">${url || "127.0.0.1:8765 · SafeWrite Crash Lab"}</span>`);
  const view = el("div", "view");
  const cam = el("div", "cam");
  const img = el("img");
  cam.append(img);
  view.append(cam);
  win.append(bar, view);
  root.append(win);
  const meta = DATA.footage[shot];
  return { win, img, cam, view, shot, fw: meta.viewport.width, fh: meta.viewport.height, vw: box.w, vh: box.h - 46, last: "" };
}
function setFootage(w, ft, cx, cy, z) {
  const src = frameAt(w.shot, ft);
  if (src !== w.last) {
    w.img.src = src;
    w.last = src;
    pending.push(w.img.decode().catch(() => {}));
  }
  const base = w.vw / w.fw;
  const s = base * z;
  let tx = w.vw / 2 - cx * s, ty = w.vh / 2 - cy * s;
  tx = clamp(tx, w.vw - w.fw * s, 0);
  ty = clamp(ty, Math.min(0, w.vh - w.fh * s), 0);
  w.cam.style.transform = `translate(${tx}px, ${ty}px) scale(${s})`;
}
function camAt(keys, lt) {
  // keys: [[t, cx, cy, z], ...] eased between keyframes
  if (lt <= keys[0][0]) return keys[0].slice(1);
  for (let i = 0; i < keys.length - 1; i++) {
    const [t0, ...a] = keys[i], [t1, ...b] = keys[i + 1];
    if (lt <= t1) { const k = E.io((lt - t0) / (t1 - t0)); return a.map((v, j) => lerp(v, b[j], k)); }
  }
  return keys[keys.length - 1].slice(1);
}

// ------------------------------------------------------------------ cues
const CUES = [];
const cue = (t, kind, extra = {}) => CUES.push({ t: +t.toFixed(3), kind, ...extra });

// ------------------------------------------------------------------ scenes
const SCENES = [];
const scene = (id, start, end, build, update) => SCENES.push({ id, start, end, build, update });

// 1 · cold open ------------------------------------------------------------
const L1 = "You just pressed Save.";
scene("cold", 0, 5.4, (r, c) => {
  c.box = el("div", "center");
  c.line = el("div", "h1", "", { position: "relative" });
  c.q = el("div", "sub", "Did it actually save?", { marginTop: "34px", fontSize: "48px" });
  c.box.append(c.line, c.q);
  r.append(c.box);
}, (r, c, lt) => {
  const n = clamp(Math.floor((lt - 0.6) / 0.058), 0, L1.length);
  const blink = Math.floor(lt / 0.53) % 2 === 0 || (lt > 0.6 && lt < 2.0);
  c.line.innerHTML = esc(L1.slice(0, n)) + `<span style="display:inline-block;width:0.06em;height:0.9em;margin-left:6px;vertical-align:-0.08em;background:${blink ? "#f5f5f2" : "transparent"}"></span>`;
  appear(c.q, E.out(P(lt, 3.0, 0.6)), { dy: 20 });
  c.box.style.opacity = 1 - 0.25 * P(lt, 4.9, 0.5);
});
for (let i = 0; i < L1.length; i++) if (L1[i] !== " ") cue(0.6 + i * 0.058, "key");
[0.15, 1.35, 2.55, 3.75, 4.95].forEach((t) => cue(t, "heart"));

// 2 · saving isn't one step -------------------------------------------------
const LAUNCH = [6.0, 7.2, 8.4, 9.6], FLIGHT = 0.9;
function buildChunks(r, c) {
  c.title = el("div", "h2 abs", "Saving isn't one step.", { left: "0", right: "0", top: "110px", textAlign: "center" });
  c.note = el("div", "sub abs", "Your file is written to the disk in pieces.", { left: "0", right: "0", top: "212px", textAlign: "center" });
  r.append(c.title, c.note);
  // document card
  c.doc = el("div", "abs", null, { left: "300px", top: "360px", width: "330px", height: "430px", borderRadius: "22px",
    background: "#13151a", border: "1px solid #2a2d34", boxShadow: "0 30px 80px rgba(0,0,0,.5)" });
  c.doc.append(el("div", "mono", "inventory.csv", { position: "absolute", left: "26px", top: "22px", fontSize: "22px", color: "#c8c8c2" }));
  r.append(c.doc);
  // disk
  c.disk = el("div", "abs", null, { left: "1240px", top: "330px", width: "380px", height: "500px", borderRadius: "28px",
    background: "linear-gradient(180deg,#15171d,#0d0e12)", border: "1px solid #2a2d34", boxShadow: "0 30px 80px rgba(0,0,0,.6)" });
  c.disk.append(el("div", null, "DISK", { position: "absolute", left: "30px", top: "24px", fontWeight: "700", letterSpacing: ".2em", color: "#7c7c77", fontSize: "20px" }));
  c.disk.append(el("div", "mono", "inventory.csv", { position: "absolute", left: "30px", top: "62px", fontSize: "20px", color: "#9a9a95" }));
  r.append(c.disk);
  c.slots = [];
  c.chunks = [];
  for (let i = 0; i < 4; i++) {
    const slot = el("div", "block", null, { left: `${1240 + 30}px`, top: `${330 + 110 + i * 92}px`, width: "320px", height: "78px",
      background: "var(--old)", boxShadow: "inset 0 0 0 1px rgba(255,255,255,.12)" });
    slot.append(el("div", "mono", "old", { position: "absolute", right: "16px", top: "24px", fontSize: "20px", color: "rgba(255,255,255,.75)" }));
    r.append(slot);
    c.slots.push(slot);
    const ch = el("div", "block", null, { left: "0", top: "0", width: "282px", height: "78px", background: "var(--new)",
      boxShadow: "0 18px 40px rgba(255,122,26,.25)" });
    ch.append(el("div", "mono", `piece ${i + 1}/4`, { position: "absolute", left: "18px", top: "24px", fontSize: "20px", color: "#1a0d02", fontWeight: "700" }));
    r.append(ch);
    c.chunks.push(ch);
  }
}
function updateChunks(c, t) {
  appear(c.title, E.out(clamp((t - 5.5) / 0.5)), { dy: 24 });
  appear(c.note, E.out(clamp((t - 6.1) / 0.5)), { dy: 18 });
  for (let i = 0; i < 4; i++) {
    const sx = 324, sy = 430 + i * 88, ex = 1270, ey = 440 + i * 92;
    const p = clamp((t - LAUNCH[i]) / FLIGHT);
    const k = E.io(p);
    const x = lerp(sx, ex, k), y = lerp(sy, ey, k) - Math.sin(Math.PI * k) * 140;
    c.chunks[i].style.transform = `translate(${x}px, ${y}px) rotate(${Math.sin(Math.PI * k) * -6}deg) scale(${1 + 0.06 * Math.sin(Math.PI * k)})`;
    c.chunks[i].style.opacity = p >= 1 ? 0 : 1;
    const landed = p >= 1;
    c.slots[i].style.background = landed ? "var(--new)" : "var(--old)";
    c.slots[i].firstChild.textContent = landed ? "new" : "old";
    c.slots[i].firstChild.style.color = landed ? "#1a0d02" : "rgba(255,255,255,.75)";
  }
}
scene("chunks", 5.4, 10.2, buildChunks, (r, c, lt, t) => updateChunks(c, t));
LAUNCH.forEach((t, i) => { cue(t, "whoosh"); if (i < 3) cue(t + FLIGHT, "tick"); });
cue(5.4, "drone_start");

// 3 · power cut ---------------------------------------------------------------
scene("cut", 10.2, 11.4, (r, c) => { c.inner = el("div", "scene"); r.append(c.inner); buildChunks(c.inner, c); },
  (r, c, lt, t) => {
    updateChunks(c, 10.2 + Math.min(lt, 0.05));
    const off = P(lt, 0.06, 0.2), off2 = P(lt, 0.26, 0.1);
    c.inner.style.transformOrigin = "50% 50%";
    c.inner.style.transform = `scale(${1 - off2}, ${Math.max(0.004, 1 - E.in(off))})`;
    c.inner.style.filter = `brightness(${1 + 3 * off})`;
    c.inner.style.opacity = lt > 0.42 ? 0 : 1;
    document.getElementById("flash").style.opacity = lt < 0.06 ? 0.85 * (1 - lt / 0.06) : 0;
  });
cue(10.2, "powerdown");

// 4 · half new, half old -------------------------------------------------------
scene("after", 11.4, 16.2, (r, c) => {
  c.wrap = el("div", "scene");
  r.append(c.wrap);
  c.label = el("div", "mono abs", "inventory.csv  ·  after the crash", { left: "260px", top: "300px", fontSize: "26px", color: "#9a9a95" });
  c.strip = el("div", "abs", null, { left: "260px", top: "350px", width: "1400px", height: "84px", borderRadius: "14px", overflow: "hidden", display: "flex" });
  c.strip.append(el("div", null, null, { width: "74%", background: "var(--new)" }), el("div", null, null, { width: "26%", background: "var(--old)" }));
  c.hl = el("div", "h1 abs", `Half <span style="color:var(--new)">new</span>. Half <span style="color:var(--old)">old</span>.`, { left: 0, right: 0, top: "520px", textAlign: "center" });
  c.sub = el("div", "h2 abs", "And nothing tells you.", { left: 0, right: 0, top: "690px", textAlign: "center", color: "#cfcfca" });
  c.ok = el("div", "chip abs", `<span style="color:#9ad79a">✓</span> file opens fine · no error`, { left: "760px", top: "850px" });
  c.wrap.append(c.label, c.strip, c.hl, c.sub, c.ok);
}, (r, c, lt) => {
  const on = E.out(P(lt, 0, 0.25));
  c.wrap.style.transform = `scale(1, ${Math.max(0.005, on)})`;
  c.wrap.style.filter = `brightness(${1 + 2 * (1 - on)})`;
  appear(c.label, P(lt, 0.2, 0.4), { dy: 10 });
  appear(c.strip, P(lt, 0.2, 0.4), { dy: 10 });
  const g = [0.1, 1.2, 2.6, 3.9].some((x) => lt > x && lt < x + 0.14);
  c.strip.style.boxShadow = g ? "-14px 0 0 rgba(255,0,80,.55), 14px 0 0 rgba(0,200,255,.55)" : "none";
  c.strip.style.transform = g ? `translateX(${(Math.sin(lt * 90) * 18).toFixed(1)}px)` : "none";
  appear(c.hl, E.out(P(lt, 0.5, 0.5)), { dy: 30 });
  if (g) c.hl.style.transform += ` translateX(${(Math.cos(lt * 70) * 10).toFixed(1)}px)`;
  appear(c.sub, E.out(P(lt, 2.2, 0.5)), { dy: 24 });
  appear(c.ok, E.out(P(lt, 3.2, 0.4)), { dy: 14 });
});
[11.5, 12.6, 14.0, 15.3].forEach((t) => cue(t, "glitch"));
cue(11.4, "crt_on");
cue(11.4, "eerie_start");

// 5 · real terminal + real file ---------------------------------------------------
scene("terminal", 16.2, 22.0, (r, c) => {
  const step = DATA.cli.steps[2];
  c.cmd = step.cmd;
  c.out = step.out.split("\n");
  c.chip = el("div", "chip abs", `<span class="dot"></span> real program · real file · killed mid-save`, { left: "160px", top: "70px" });
  r.append(c.chip);
  c.term = el("div", "window", null, { left: "160px", top: "150px", width: "1600px", height: "560px" });
  c.term.append(el("div", "bar", `<i style="background:#ff5f57"></i><i style="background:#febc2e"></i><i style="background:#28c840"></i><span class="url">terminal — ~/demo</span>`));
  c.tbody = el("div", "term");
  c.term.append(c.tbody);
  r.append(c.term);
  // file viewer
  const lines = DATA.cli.file_lines, first = DATA.cli.first_a_line;
  c.file = el("div", "window", null, { left: "160px", top: "150px", width: "1600px", height: "860px", opacity: 0 });
  c.file.append(el("div", "bar", `<i style="background:#ff5f57"></i><i style="background:#febc2e"></i><i style="background:#28c840"></i><span class="url">inventory.csv — the real file the killed program left behind</span>`));
  const body = el("div", "term", null, { fontSize: "27px", lineHeight: "1.55", padding: "18px 0" });
  for (let n = first - 8; n <= first + 7; n++) {
    const text = lines[n - 1] || "";
    const isA = n >= first;
    const row = el("div", null, `<span style="display:inline-block;width:90px;text-align:right;color:#5d5f66;margin-right:26px">${n}</span>${esc(text)}`,
      { padding: "0 34px", background: isA ? "rgba(61,139,255,.16)" : "rgba(255,122,26,.16)" });
    if (n === first) row.style.borderTop = "3px solid #ffffff";
    body.append(row);
  }
  c.file.append(body);
  c.callout = el("div", "chip abs", `line ${first}: new version ends, <b style="color:#fff">old version begins</b>`, { left: "980px", top: "566px", background: "rgba(0,0,0,.75)", fontSize: "21px" });
  c.cap = el("div", "sub abs", `Top: <span style="color:var(--new)">today's inventory</span>. Bottom: <span style="color:var(--old)">yesterday's</span>.`, { left: 0, right: 0, top: "1022px", textAlign: "center", fontSize: "34px" });
  r.append(c.file, c.callout, c.cap);
}, (r, c, lt) => {
  appear(c.chip, E.out(P(lt, 0.1, 0.4)), { dy: 10 });
  const prompt = `<span style="color:#7bd88f">~/demo</span> <span style="color:#8d8d88">$</span> `;
  const typed = c.cmd.slice(0, clamp(Math.floor((lt - 0.3) / 0.016), 0, c.cmd.length));
  let html = prompt + esc(typed);
  const outStart = 0.3 + c.cmd.length * 0.016 + 0.25;
  if (lt > outStart) {
    const shown = clamp(Math.floor((lt - outStart) / 0.07), 0, c.out.length);
    html += "\n" + c.out.slice(0, shown).map((l) => {
      if (l.includes("CRASH")) return `<span style="color:#ff5c5c;font-weight:700">${esc(l)}</span>`;
      if (l.includes("pwrite")) return `<span style="color:#9ec1ff">${esc(l)}</span>`;
      return `<span style="color:#9a9a95">${esc(l)}</span>`;
    }).join("\n");
  } else if (Math.floor(lt / 0.5) % 2 === 0) html += "▌";
  c.tbody.innerHTML = html;
  const sw = E.io(P(lt, 3.0, 0.6));
  c.term.style.opacity = 1 - sw;
  c.term.style.transform = `translateY(${-40 * sw}px) scale(${1 - 0.04 * sw})`;
  c.file.style.opacity = sw;
  c.file.style.transform = `translateY(${60 * (1 - sw)}px)`;
  appear(c.callout, E.out(P(lt, 3.7, 0.4)), { dx: 30, dy: 0 });
  appear(c.cap, E.out(P(lt, 4.1, 0.5)), { dy: 12 });
});
(() => {
  // keyboard clicks while the command types, a zap when the CRASH line prints
  const cmdLen = 75;
  for (let i = 0; i < cmdLen; i += 3) cue(16.2 + 0.3 + i * 0.016, "key", { soft: true });
})();
cue(16.2, "pulse_start");

// 6 · stakes ---------------------------------------------------------------------------
const STAKES = [[22.0, "Bank balances."], [23.2, "Hospital records."], [24.4, "Exam marks."], [25.6, "Your phone's update."]];
scene("stakes", 22.0, 28.0, (r, c) => {
  c.cards = STAKES.map(([, txt]) => { const n = el("div", "center"); n.append(el("div", "h1", txt, { fontSize: "150px" })); r.append(n); return n; });
  c.last = el("div", "center");
  c.l1 = el("div", "h1", "One crash.", { fontSize: "140px" });
  c.l2 = el("div", "h1", "Silent corruption.", { fontSize: "140px", color: "var(--bad)" });
  c.last.append(c.l1, c.l2);
  r.append(c.last);
}, (r, c, lt, t) => {
  c.cards.forEach((n, i) => {
    const a = STAKES[i][0], b = i < 3 ? STAKES[i + 1][0] : 26.8;
    const on = t >= a && t < b;
    n.style.opacity = on ? 1 : 0;
    n.style.transform = `scale(${1.08 - 0.08 * E.out(clamp((t - a) / 1.2))})`;
  });
  c.last.style.opacity = t >= 26.8 ? 1 : 0;
  c.last.style.transform = `scale(${1.06 - 0.06 * E.out(clamp((t - 26.8) / 1.2))})`;
  c.l2.style.opacity = t >= 27.2 ? 1 : 0;
});
STAKES.forEach(([t]) => cue(t, "hit", { gain: 0.8 }));
cue(26.8, "hit", { gain: 1.0 });
cue(27.2, "hit", { gain: 0.7, low: true });
cue(27.4, "riser", { dur: 1.8 });

// 7 · reveal -------------------------------------------------------------------------------
function logo(size) {
  const m = el("div", null, null, { width: size + "px", height: size + "px", borderRadius: size * 0.24 + "px",
    background: "linear-gradient(135deg, var(--brand-a), #ff5e3a 45%, var(--brand-b))", display: "grid", placeItems: "center",
    boxShadow: `0 0 ${size * 0.6}px rgba(247,148,29,.35)` });
  m.innerHTML = `<svg width="${size * 0.56}" height="${size * 0.56}" viewBox="0 0 32 32"><path d="M18 3 7 18h7l-2 11 11-15h-7z" fill="#fff"/></svg>`;
  return m;
}
scene("reveal", 28.0, 34.0, (r, c) => {
  c.glow = el("div", "abs", null, { inset: 0, background: "radial-gradient(circle at 50% 46%, rgba(247,148,29,.22), rgba(61,139,255,.10) 32%, rgba(0,0,0,0) 60%)" });
  c.row = el("div", "abs", null, { left: 0, right: 0, top: "330px", display: "flex", justifyContent: "center", alignItems: "center", gap: "44px" });
  c.mark = logo(190);
  c.word = el("div", "h1", "SafeWrite", { fontSize: "170px", fontWeight: 900 });
  c.row.append(c.mark, c.word);
  c.s1 = el("div", "sub abs", "Crash-consistent document storage.", { left: 0, right: 0, top: "600px", textAlign: "center", fontSize: "50px" });
  c.s2 = el("div", "h2 abs", `<span class="grad">Proven, not promised.</span>`, { left: 0, right: 0, top: "700px", textAlign: "center", fontSize: "92px" });
  r.append(c.glow, c.row, c.s1, c.s2);
}, (r, c, lt) => {
  const on = lt >= 1.2;
  c.glow.style.opacity = on ? 0.6 + 0.4 * E.out(P(lt, 1.2, 1.5)) : 0;
  const pm = P(lt, 1.2, 0.7);
  c.mark.style.opacity = on ? 1 : 0;
  c.mark.style.transform = `scale(${on ? 0.6 + 0.4 * E.back(pm) : 0.6}) rotate(${(1 - E.out(pm)) * -12}deg)`;
  appear(c.word, on ? E.out(P(lt, 1.45, 0.6)) : 0, { dx: -40, dy: 0, blur: 10 });
  appear(c.s1, E.out(P(lt, 2.6, 0.6)), { dy: 20 });
  appear(c.s2, E.out(P(lt, 3.8, 0.7)), { dy: 26, blur: 8 });
  document.getElementById("flash").style.opacity = lt >= 1.2 && lt < 1.5 ? 0.9 * (1 - (lt - 1.2) / 0.3) : 0;
  const breathe = 1 + 0.012 * Math.sin(lt * 1.3);
  c.row.style.transform = `scale(${breathe})`;
});
cue(29.2, "boom");
cue(31.8, "shimmer");

// 8 · how: copy-on-write -------------------------------------------------------------------
const HOW = [[34.0, "Never overwrite."], [35.8, "Write the new version somewhere else."], [38.2, "Then flip one switch."],
  [40.0, "Crash before the flip: <span style='color:var(--old)'>old</span>. After: <span style='color:var(--new)'>new</span>."]];
scene("how", 34.0, 43.6, (r, c) => {
  c.titles = HOW.map(([, txt]) => { const n = el("div", "h2 abs", txt, { left: 0, right: 0, top: "120px", textAlign: "center" }); r.append(n); return n; });
  c.dia = el("div", "abs", null, { left: 0, top: 0, width: "1920px", height: "1080px", transformOrigin: "50% 40%" });
  r.append(c.dia);
  const x0 = 960 - (10 * 130 + 9 * 22) / 2, y0 = 560;
  c.slots = [];
  for (let i = 0; i < 10; i++) {
    const x = x0 + i * 152;
    const s = el("div", "block", null, { left: x + "px", top: y0 + "px", width: "130px", height: "130px",
      border: "2px dashed #34363d", boxSizing: "border-box" });
    c.dia.append(s);
    c.slots.push({ x, s });
  }
  c.old = [0, 1, 2, 3].map((i) => {
    const b = el("div", "block", `<div class="mono" style="position:absolute;inset:0;display:grid;place-items:center;font-size:26px;color:rgba(255,255,255,.85)">v1</div>`,
      { left: c.slots[i].x + "px", top: y0 + "px", width: "130px", height: "130px", background: "var(--old)" });
    c.dia.append(b);
    return b;
  });
  c.neu = [5, 6, 7, 8].map((i) => {
    const b = el("div", "block", `<div class="mono" style="position:absolute;inset:0;display:grid;place-items:center;font-size:26px;color:#1a0d02;font-weight:700">v2</div>`,
      { left: c.slots[i].x + "px", top: y0 + "px", width: "130px", height: "130px", background: "var(--new)", opacity: 0 });
    c.dia.append(b);
    return b;
  });
  c.sb = el("div", "abs", "SUPERBLOCK", { left: "840px", top: "330px", width: "240px", height: "64px", borderRadius: "14px",
    background: "#1b1d23", border: "2px solid #4a4d55", display: "grid", placeItems: "center", fontWeight: 800, letterSpacing: ".14em", fontSize: "22px" });
  c.dia.append(c.sb);
  c.arrow = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  c.arrow.setAttribute("width", "1920"); c.arrow.setAttribute("height", "1080");
  Object.assign(c.arrow.style, { position: "absolute", left: 0, top: 0 });
  c.arrow.innerHTML = `<defs><marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#f5f5f2"/></marker></defs><path id="ap" stroke="#f5f5f2" stroke-width="6" fill="none" marker-end="url(#ah)"/>`;
  c.dia.append(c.arrow);
  c.commit = el("div", "chip abs", "★ commit point", { left: "1110px", top: "336px", background: "rgba(34,197,94,.16)", color: "#7ee2a1", borderColor: "rgba(34,197,94,.4)" });
  c.dia.append(c.commit);
  c.oldGroup = (c.slots[0].x + c.slots[3].x + 130) / 2;
  c.newGroup = (c.slots[5].x + c.slots[8].x + 130) / 2;
  // outcome cards
  c.cards = el("div", "abs", null, { left: "260px", right: "260px", top: "800px", display: "flex", gap: "40px", justifyContent: "center" });
  const card = (head, color, txt) => el("div", null, `<div style="font-size:26px;color:#9a9a95;margin-bottom:10px">⚡ ${head}</div><div style="font-size:44px;font-weight:800;color:${color}">${txt} ✓</div>`,
    { flex: 1, padding: "26px 34px", borderRadius: "20px", background: "#121318", border: "1px solid #2a2c33" });
  c.cardA = card("crash before the flip", "var(--old)", "old version");
  c.cardB = card("crash after the flip", "var(--new)", "new version");
  c.cards.append(c.cardA, c.cardB);
  r.append(c.cards);
  c.final = el("div", "center"); c.final.append(el("div", "h1 grad", "Never a mix.", { fontSize: "170px" }));
  r.append(c.final);
}, (r, c, lt, t) => {
  HOW.forEach(([a], i) => {
    const b = i < HOW.length - 1 ? HOW[i + 1][0] : 42.4;
    const p = t >= a && t < b ? E.out(clamp((t - a) / 0.35)) : 0;
    appear(c.titles[i], p, { dy: 20 });
  });
  c.neu.forEach((b, i) => {
    const p = E.back(clamp((t - (35.9 + i * 0.4)) / 0.3));
    b.style.opacity = clamp((t - (35.9 + i * 0.4)) / 0.15);
    b.style.transform = `scale(${0.4 + 0.6 * p})`;
  });
  const flip = E.io(clamp((t - 38.6) / 0.35));
  const tx = lerp(c.oldGroup, c.newGroup, flip);
  c.arrow.querySelector("#ap").setAttribute("d", `M 960 394 C 960 470, ${tx} 470, ${tx} 548`);
  c.sb.style.borderColor = t >= 38.95 && t < 39.6 ? "#7ee2a1" : "#4a4d55";
  c.sb.style.boxShadow = t >= 38.95 && t < 39.6 ? "0 0 40px rgba(34,197,94,.5)" : "none";
  appear(c.commit, E.out(clamp((t - 39.0) / 0.3)), { dx: -20, dy: 0 });
  const dz = E.io(clamp((t - 40.0) / 0.5));
  c.dia.style.transform = `translateY(${-70 * dz}px) scale(${1 - 0.18 * dz})`;
  c.dia.style.opacity = t >= 42.4 ? 1 - E.out(clamp((t - 42.4) / 0.3)) : 1;
  appear(c.cardA, t < 42.4 ? E.out(clamp((t - 40.3) / 0.4)) : 0, { dy: 30 });
  appear(c.cardB, t < 42.4 ? E.out(clamp((t - 40.8) / 0.4)) : 0, { dy: 30 });
  appear(c.final, E.back(clamp((t - 42.45) / 0.4)), { dy: 0, s0: 0.85 });
});
[35.9, 36.3, 36.7, 37.1].forEach((t) => cue(t, "pop"));
cue(34.0, "groove_start");
cue(38.6, "flip");
cue(40.3, "tick"); cue(40.8, "tick");
cue(42.4, "hit", { gain: 0.9 });

// footage scenes share a caption + window layout
function footageScene(id, start, end, shot, caption, chipText, box, mapT, camKeys, extra) {
  scene(id, start, end, (r, c) => {
    c.bg = el("div", "abs", null, { inset: 0, background: "radial-gradient(circle at 70% 60%, rgba(61,139,255,.10), rgba(0,0,0,0) 55%), radial-gradient(circle at 20% 30%, rgba(247,148,29,.10), rgba(0,0,0,0) 50%)" });
    r.append(c.bg);
    c.cap = el("div", "h2 abs", caption, { left: box.x + "px", top: "52px", fontSize: "64px" });
    c.chip = el("div", "chip abs", `<span class="dot" style="background:#ff3b3b"></span> ${chipText}`, { right: (1920 - box.x - box.w) + "px", top: "68px" });
    r.append(c.cap, c.chip);
    c.w = browserWindow(r, shot, box);
    if (extra && extra.build) extra.build(r, c);
  }, (r, c, lt, t) => {
    appear(c.cap, E.out(P(lt, 0.05, 0.45)), { dy: 18 });
    appear(c.chip, E.out(P(lt, 0.3, 0.4)), { dy: 10 });
    const winIn = E.out(P(lt, 0, 0.5));
    c.w.win.style.opacity = winIn;
    c.w.win.style.transform = `translateY(${(1 - winIn) * 50}px)`;
    const [cx, cy, z] = camAt(camKeys, lt);
    setFootage(c.w, mapT(lt), cx, cy, z);
    if (extra && extra.update) extra.update(r, c, lt, t);
  });
}

// 9 · lab footage ------------------------------------------------------------------------------
footageScene("lab", 43.6, 52.0, "lab_cow", "Pull the plug anywhere.", "real recording · crash lab",
  { x: 160, y: 170, w: 1600, h: 870 }, (lt) => 0.4 + lt * 1.06,
  [[0, 960, 560, 1.0], [1.0, 960, 560, 1.0], [1.6, 1060, 420, 1.3], [2.2, 1060, 420, 1.3], [2.9, 960, 1700, 1.15],
   [3.2, 960, 1700, 1.15], [3.8, 960, 860, 1.15], [4.6, 960, 860, 1.15], [5.2, 960, 1700, 1.15], [6.2, 960, 1700, 1.15],
   [7.0, 960, 1000, 1.0], [8.4, 960, 1000, 1.0]]);

// 10 · four techniques ------------------------------------------------------------------------------
const FOUR = [["Temp file + rename", "write a copy, swap names"], ["Undo journal", "keep the old bytes"], ["Redo log", "promise first, then do"], ["Copy-on-write", "never overwrite · our default"]];
scene("four", 52.0, 55.6, (r, c) => {
  c.grid = el("div", "abs", null, { left: "210px", right: "210px", top: "250px", display: "grid", gridTemplateColumns: "1fr 1fr", gap: "34px" });
  c.cards = FOUR.map(([a, b], i) => {
    const n = el("div", null, `<div style="font-size:54px;font-weight:800;letter-spacing:-.02em">${a}</div><div style="font-size:30px;color:#9a9a95;margin-top:8px">${b}</div>`,
      { padding: "34px 40px", borderRadius: "24px", background: i === 3 ? "linear-gradient(135deg, rgba(247,148,29,.22), rgba(61,139,255,.18))" : "#121318",
        border: i === 3 ? "2px solid rgba(247,148,29,.7)" : "1px solid #2a2c33" });
    c.grid.append(n);
    return n;
  });
  c.t = el("div", "h2 abs", "We built all four.", { left: 0, right: 0, top: "800px", textAlign: "center" });
  r.append(c.grid, c.t);
}, (r, c, lt) => {
  c.cards.forEach((n, i) => appear(n, E.back(P(lt, i * 0.6, 0.35)), { dy: 40, s0: 0.92 }));
  appear(c.t, E.out(P(lt, 2.4, 0.4)), { dy: 20 });
});
[52.0, 52.6, 53.2, 53.8].forEach((t, i) => cue(t, "pluck", { note: i }));
cue(54.4, "tick");

// 11 · most people test 3 ------------------------------------------------------------------------
scene("three", 55.6, 58.0, (r, c) => {
  c.l1 = el("div", "h2 abs", `Most people test <span id="three" style="position:relative;display:inline-block;font-size:110px;color:#fff">3<span id="strike" style="position:absolute;left:-10%;top:50%;height:10px;width:0;background:var(--bad);border-radius:6px"></span></span> crashes.`,
    { left: 0, right: 0, top: "300px", textAlign: "center" });
  c.l2 = el("div", "h1 abs", "We tested every possible one.", { left: 0, right: 0, top: "560px", textAlign: "center" });
  r.append(c.l1, c.l2);
}, (r, c, lt) => {
  appear(c.l1, E.out(P(lt, 0, 0.35)), { dy: 20 });
  c.l1.querySelector("#strike").style.width = (120 * E.out(P(lt, 0.8, 0.25))) + "%";
  appear(c.l2, E.out(P(lt, 1.2, 0.45)), { dy: 30, blur: 8 });
});
cue(56.4, "scratch");
cue(57.2, "riser", { dur: 0.8 });

// 12 · explorer footage + counter ---------------------------------------------------------------------
footageScene("explorer", 58.0, 66.4, "explorer", "Every crash. Tested.", "real recording · explorer",
  { x: 660, y: 170, w: 1160, h: 870 },
  // the click at real speed, the quiet first seconds of the run skipped fast, the matrix filling at ~1.2x
  (lt) => lt < 1.5 ? 0.6 + lt * 1.33 : lt < 3.0 ? 2.6 + (lt - 1.5) * 4.6 : Math.min(9.5 + (lt - 3.0) * 1.22, 14.46),
  [[0, 960, 720, 1.0], [2.5, 960, 760, 1.0], [3.4, 760, 980, 1.45], [6.2, 820, 1080, 1.45], [7.6, 960, 760, 1.04], [8.4, 960, 760, 1.04]],
  {
    build(r, c) {
      c.num = el("div", "h1 abs", "0", { left: "100px", top: "330px", fontSize: "136px", fontWeight: 900, fontVariantNumeric: "tabular-nums" });
      c.lab = el("div", "sub abs", "crash states explored", { left: "106px", top: "490px" });
      c.zero = el("div", "abs", `<div style="font-family:var(--display);font-size:150px;font-weight:900;color:var(--good);letter-spacing:-.03em">0</div><div class="sub" style="margin-top:-10px">failures in the<br>safe engines</div>`,
        { left: "100px", top: "600px" });
      r.append(c.num, c.lab, c.zero);
    },
    update(r, c, lt) {
      const k = E.io(P(lt, 0.3, 6.2));
      c.num.textContent = Math.round(63673 * k).toLocaleString("en-US");
      appear(c.lab, E.out(P(lt, 0.4, 0.4)), { dy: 10 });
      appear(c.zero, E.back(P(lt, 6.6, 0.35)), { dy: 0, s0: 1.3 });
    },
  });
cue(58.4, "arp_rise", { dur: 6.2 });
cue(64.6, "stamp");

// 13 · we broke it on purpose ------------------------------------------------------------------------
const BROKEN = ["naive", "naive/trunc", "shadow/no-fsync", "shadow/no-dirsync", "undo/no-journal-sync", "wal/no-checksum", "wal/early-ack", "cow/no-barrier", "cow/one-super"];
scene("broke", 66.4, 71.2, (r, c) => {
  c.t = el("div", "h2 abs", "Then we broke it on purpose.", { left: 0, right: 0, top: "90px", textAlign: "center" });
  c.grid = el("div", "abs", null, { left: "140px", right: "140px", top: "250px", display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "22px" });
  c.chips = BROKEN.map((b) => {
    const n = el("div", null, `<span class="mono" style="font-size:25px">${b}</span><span style="color:var(--bad);font-weight:800;font-size:24px">✗ caught</span>`,
      { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "22px 26px", borderRadius: "16px",
        background: "rgba(255,77,77,.10)", border: "1px solid rgba(255,77,77,.35)" });
    c.grid.append(n);
    return n;
  });
  c.big = el("div", "h1 abs", `9 safeguards removed. <span style="color:var(--good)">9 caught.</span>`, { left: 0, right: 0, top: "720px", textAlign: "center", fontSize: "96px" });
  r.append(c.t, c.grid, c.big);
}, (r, c, lt) => {
  appear(c.t, E.out(P(lt, 0, 0.35)), { dy: 20 });
  c.chips.forEach((n, i) => appear(n, E.back(P(lt, 0.6 + i * 0.17, 0.25)), { dy: 0, s0: 0.85 }));
  appear(c.big, E.out(P(lt, 2.8, 0.4)), { dy: 24 });
});
BROKEN.forEach((_, i) => cue(67.0 + i * 0.17, "tick", { gain: 0.6 }));
cue(69.2, "hit", { gain: 0.9 });

// 14 · torture footage -----------------------------------------------------------------------------------
footageScene("torture", 71.2, 79.6, "torture", "Real kill -9. Real files.", "real recording · real time",
  { x: 160, y: 170, w: 1600, h: 870 }, (lt) => Math.min(lt * 1.0, 99),
  [[0, 960, 600, 1.0], [2.0, 960, 560, 1.15], [6.0, 960, 560, 1.15], [8.4, 960, 600, 1.05]],
  {
    build(r, c) {
      const big = { fontSize: "30px", padding: "16px 28px", background: "rgba(8,9,12,.92)", letterSpacing: ".04em" };
      c.bad = el("div", "chip abs", `<span style="color:var(--bad)">✗</span> ordinary save: corrupted files`, { ...big, left: "300px", top: "940px", borderColor: "rgba(255,77,77,.5)" });
      c.good = el("div", "chip abs", `<span style="color:var(--good)">✓</span> SafeWrite: 0 corruptions`, { ...big, right: "300px", top: "940px", borderColor: "rgba(34,197,94,.5)" });
      r.append(c.bad, c.good);
    },
    update(r, c, lt) {
      appear(c.bad, E.out(P(lt, 6.2, 0.4)), { dy: 14 });
      appear(c.good, E.out(P(lt, 6.6, 0.4)), { dy: 14 });
    },
  });
cue(71.2, "hats_fast");
cue(77.4, "tick"); cue(77.8, "tick");

// 15 · one more thing ----------------------------------------------------------------------------------
scene("onemore", 79.6, 84.4, (r, c) => {
  c.a = el("div", "center"); c.a.append(el("div", "sub", "One more thing.", { fontSize: "54px" }));
  c.b = el("div", "center"); c.b.append(el("div", "h2", "Killing a program can't find<br>the deadliest bug.", { fontSize: "88px" }));
  c.c = el("div", "center"); c.c.append(el("div", "h2", `So we built a<br><span class="grad">power-cut simulator.</span>`, { fontSize: "100px" }));
  r.append(c.a, c.b, c.c);
}, (r, c, lt) => {
  c.a.style.opacity = lt < 1.0 ? E.out(P(lt, 0.1, 0.4)) : 0;
  appear(c.b, lt >= 1.0 && lt < 2.8 ? E.out(P(lt, 1.0, 0.4)) : 0, { dy: 18 });
  appear(c.c, lt >= 2.8 ? E.out(P(lt, 2.8, 0.5)) : 0, { dy: 24, blur: 8 });
});
cue(79.6, "drop_out");
cue(82.4, "hit", { gain: 0.6, low: true });

// 16 · documents footage -------------------------------------------------------------------------------
footageScene("docs", 84.4, 90.4, "docs", "Your laptop. Your disk.", "real recording · real kill mid-save",
  { x: 160, y: 170, w: 1600, h: 870 },
  // typing plays at 1.5x, the kill and the recovery verdict at real speed, then the verdict holds
  (lt) => lt < 2.6 ? 0.6 + lt * 1.5 : lt < 3.6 ? 4.5 + (lt - 2.6) * 0.6 : Math.min(5.1 + (lt - 3.6) * 0.34, 5.92),
  [[0, 1180, 380, 1.3], [2.5, 1180, 380, 1.3], [3.3, 1300, 830, 1.25], [6.0, 1300, 840, 1.32]]);
cue(84.4, "groove_return");
cue(86.95, "tick", { gain: 0.8 }); cue(87.0, "glitch", { dur: 0.25 }); cue(87.8, "shimmer");

// 17 · final line ---------------------------------------------------------------------------------------
scene("final", 90.4, 96.4, (r, c) => {
  c.box = el("div", "center", null, { gap: "6px" });
  c.l1 = el("div", "h1", "Every crash.", { fontSize: "150px" });
  c.l2 = el("div", "h1", `<span style="color:var(--old)">Old</span> or <span style="color:var(--new)">new</span>.`, { fontSize: "150px" });
  c.l3 = el("div", "h1 grad", "Never a mix.", { fontSize: "170px" });
  c.box.append(c.l1, c.l2, c.l3);
  r.append(c.box);
}, (r, c, lt) => {
  appear(c.l1, E.out(P(lt, 0, 0.3)), { dy: 30 });
  appear(c.l2, E.out(P(lt, 1.2, 0.3)), { dy: 30 });
  appear(c.l3, E.back(P(lt, 2.4, 0.4)), { dy: 0, s0: 0.85, blur: 6 });
  c.box.style.transform = `scale(${1 + 0.03 * E.io(P(lt, 0, 6))})`;
});
cue(90.4, "hit", { gain: 0.8 }); cue(91.6, "hit", { gain: 0.85 }); cue(92.8, "boom", { gain: 1.0 });

// 18 · end card -------------------------------------------------------------------------------------------
scene("end", 96.4, 102.0, (r, c) => {
  c.glow = el("div", "abs", null, { inset: 0, background: "radial-gradient(circle at 50% 42%, rgba(247,148,29,.18), rgba(61,139,255,.08) 30%, rgba(0,0,0,0) 58%)" });
  c.row = el("div", "abs", null, { left: 0, right: 0, top: "290px", display: "flex", justifyContent: "center", alignItems: "center", gap: "40px" });
  c.row.append(logo(160), el("div", "h1", "SafeWrite", { fontSize: "150px", fontWeight: 900 }));
  c.tag = el("div", "h2 abs", `<span class="grad">Proven, not promised.</span>`, { left: 0, right: 0, top: "520px", textAlign: "center", fontSize: "72px" });
  c.meta = el("div", "abs", `<div class="sub" style="font-size:34px">Team VAT · Kernel Ops 2026</div>
     <div class="mono" style="font-size:30px;color:#d4d4cf;margin-top:26px">github.com/VMS2907/KernelOps</div>
     <div class="chip mono" style="margin-top:26px;text-transform:none;letter-spacing:0;font-size:26px">$ python -m safewrite serve</div>`,
    { left: 0, right: 0, top: "660px", textAlign: "center" });
  r.append(c.glow, c.row, c.tag, c.meta);
}, (r, c, lt) => {
  appear(c.row, E.out(P(lt, 0, 0.6)), { dy: 0, s0: 0.94, blur: 10 });
  appear(c.tag, E.out(P(lt, 0.5, 0.5)), { dy: 16 });
  appear(c.meta, E.out(P(lt, 1.0, 0.6)), { dy: 16 });
  r.style.opacity = 1 - P(lt, 4.6, 1.0);
});
cue(96.4, "end_chord");

// ------------------------------------------------------------------ engine
const DURATION = 102.0;
const SECTIONS = [
  { name: "intro", start: 0, end: 10.2 }, { name: "silence", start: 10.2, end: 11.4 },
  { name: "eerie", start: 11.4, end: 22.0 }, { name: "stakes", start: 22.0, end: 28.0 },
  { name: "reveal", start: 28.0, end: 34.0 }, { name: "groove", start: 34.0, end: 55.6 },
  { name: "build", start: 55.6, end: 58.0 }, { name: "proof", start: 58.0, end: 71.2 },
  { name: "drive", start: 71.2, end: 79.6 }, { name: "dropout", start: 79.6, end: 84.4 },
  { name: "finale", start: 84.4, end: 96.4 }, { name: "outro", start: 96.4, end: 102.0 },
];
window.TIMELINE = { duration: DURATION, fps: 30, bpm: 100, cues: CUES.sort((a, b) => a.t - b.t), sections: SECTIONS };

let current = null;
const layer = document.getElementById("layer");
window.init = (data) => { DATA = data; };
window.renderAt = async (t) => {
  const sc = SCENES.find((s) => t >= s.start && t < s.end) || SCENES[SCENES.length - 1];
  document.getElementById("flash").style.opacity = 0;
  if (current !== sc) {
    layer.innerHTML = "";
    layer.removeAttribute("style");
    sc.root = el("div", "scene");
    sc.ctx = {};
    layer.append(sc.root);
    sc.build(sc.root, sc.ctx);
    current = sc;
  }
  sc.update(sc.root, sc.ctx, t - sc.start, t);
  const g = document.getElementById("grain");
  g.style.transform = `translate(${-((t * 137) % 60)}px, ${-((t * 89) % 60)}px)`;
  const waits = pending.splice(0);
  await Promise.all(waits);
  await document.fonts.ready;
};
