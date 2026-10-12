// Tiny helpers: DOM building, API calls, server-sent events, tooltips.

export function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

export function svg(tag, attrs = {}, ...kids) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

// Replace an element's children, skipping null/false (DOM append would print "null").
export function fill(el, ...kids) {
  clear(el);
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

export async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

export function stream(path, handlers) {
  const es = new EventSource(path);
  for (const [name, fn] of Object.entries(handlers)) {
    if (name === "error") continue;           // handled by onerror below (no JSON payload)
    es.addEventListener(name, (ev) => fn(JSON.parse(ev.data)));
  }
  es.onerror = () => { es.close(); handlers.error && handlers.error(); };
  return es;
}

export const fmt = {
  int: (n) => (n ?? 0).toLocaleString("en-US"),
  compact: (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e4 ? (n / 1e3).toFixed(1) + "K" : (n ?? 0).toLocaleString("en-US"),
  ms: (n) => n < 1 ? n.toFixed(2) + " ms" : n < 100 ? n.toFixed(1) + " ms" : Math.round(n) + " ms",
};

const tip = () => document.getElementById("tooltip");
export function tooltip(el, html) {
  el.addEventListener("mouseenter", () => { const t = tip(); t.innerHTML = typeof html === "function" ? html() : html; t.style.display = "block"; });
  el.addEventListener("mousemove", (e) => {
    const t = tip();
    const x = Math.min(e.clientX + 14, window.innerWidth - t.offsetWidth - 8);
    const y = e.clientY + 16 + t.offsetHeight > window.innerHeight ? e.clientY - t.offsetHeight - 12 : e.clientY + 16;
    t.style.left = x + "px"; t.style.top = y + "px";
  });
  el.addEventListener("mouseleave", () => { tip().style.display = "none"; });
  return el;
}

export function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

export const OUTCOME = {
  OK: { cls: "good", sym: "✓", text: "correct" },
  LOST: { cls: "bad", sym: "✗", text: "acknowledged write lost" },
  TORN_TX: { cls: "bad", sym: "✗", text: "transaction half-applied" },
  CORRUPT: { cls: "bad", sym: "✗", text: "silent corruption" },
  DETECTED: { cls: "bad", sym: "✗", text: "damage detected, data lost" },
  UNRECOVERABLE: { cls: "bad", sym: "✗", text: "store unreadable" },
  NONIDEMPOTENT: { cls: "bad", sym: "✗", text: "recovery not idempotent" },
};

export function pill(cls, text) { return h("span", { class: "pill " + cls }, text); }
