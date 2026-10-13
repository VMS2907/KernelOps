// Record real footage of the running Crash Lab (python -m safewrite serve) with
// Chrome's screencast API: every repaint becomes a JPEG frame with a timestamp.
//
//   node video/capture_footage.js http://127.0.0.1:8799 video/build/footage
//
// A visible cursor (with click ripples) is injected, because headless Chrome
// does not draw one and launch videos look dead without it.
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const BASE = process.argv[2] || "http://127.0.0.1:8799";
const OUT = process.argv[3] || "video/build/footage";
const ONLY = process.argv[4] || "";
const SCALE = 4 / 3; // the UI is laid out as if 1440 px wide, rendered crisp at 1920 px
const V = (w, h) => ({ width: Math.round(w * SCALE), height: Math.round(h * SCALE) });

const CURSOR_JS = `
(() => {
  if (window.__cursor || !document.documentElement) return;
  const c = document.createElement('div');
  c.innerHTML = '<svg width="28" height="28" viewBox="0 0 28 28"><path d="M4 2 L4 22 L9.5 17 L13 25 L16.5 23.5 L13 15.8 L20.5 15.8 Z" fill="#fff" stroke="#111" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  Object.assign(c.style, {position:'fixed', left:'0px', top:'0px', zIndex: 2147483647, pointerEvents:'none',
    transition:'none', filter:'drop-shadow(0 2px 3px rgba(0,0,0,.35))'});
  document.documentElement.appendChild(c);
  window.__cursor = c;
  addEventListener('mousemove', e => { c.style.left = (e.clientX - 4) + 'px'; c.style.top = (e.clientY - 2) + 'px'; }, true);
  addEventListener('mousedown', e => {
    const r = document.createElement('div');
    Object.assign(r.style, {position:'fixed', left:(e.clientX-18)+'px', top:(e.clientY-18)+'px', width:'36px', height:'36px',
      borderRadius:'50%', border:'3px solid rgba(247,148,29,.95)', zIndex:2147483646, pointerEvents:'none',
      transform:'scale(.3)', opacity:'1', transition:'transform .45s ease-out, opacity .45s ease-out'});
    document.documentElement.appendChild(r);
    requestAnimationFrame(() => { r.style.transform = 'scale(1.6)'; r.style.opacity = '0'; });
    setTimeout(() => r.remove(), 600);
  }, true);
})();`;

async function shot(browser, name, viewport, fn) {
  if (ONLY && !ONLY.split(",").includes(name)) return;
  const dir = path.join(OUT, name);
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
  const ctx = await browser.newContext({ viewport, colorScheme: "dark" });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => console.log("pageerror", name, String(e)));
  await page.addInitScript(CURSOR_JS);
  const cdp = await ctx.newCDPSession(page);
  const frames = [];
  let t0 = null;
  cdp.on("Page.screencastFrame", async ({ data, metadata, sessionId }) => {
    const t = metadata.timestamp;
    if (t0 === null) t0 = t;
    const file = `f${String(frames.length).padStart(5, "0")}.jpg`;
    fs.writeFileSync(path.join(dir, file), Buffer.from(data, "base64"));
    frames.push({ t: +(t - t0).toFixed(4), file });
    try { await cdp.send("Page.screencastFrameAck", { sessionId }); } catch (_) {}
  });
  const helpers = {
    page,
    wait: (ms) => page.waitForTimeout(ms),
    async zoom() {
      await page.evaluate((z) => { document.body.style.zoom = String(z); }, SCALE);
      await page.waitForTimeout(250);
    },
    async start() {
      await page.evaluate(CURSOR_JS);
      await cdp.send("Page.startScreencast", { format: "jpeg", quality: 93, everyNthFrame: 1 });
    },
    async move(selector, steps = 28) {
      const box = await page.locator(selector).first().boundingBox();
      if (!box) throw new Error("not found: " + selector);
      const x = box.x + box.width / 2, y = box.y + box.height / 2;
      await page.mouse.move(x, y, { steps });
      return { x, y };
    },
    async click(selector, steps = 28) {
      await this.move(selector, steps);
      await page.waitForTimeout(180);
      await page.mouse.down();
      await page.waitForTimeout(70);
      await page.mouse.up();
    },
  };
  await fn(helpers);
  await cdp.send("Page.stopScreencast");
  await page.waitForTimeout(200);
  fs.writeFileSync(path.join(dir, "index.json"), JSON.stringify({ name, scale: SCALE, viewport, frames }, null, 0));
  console.log(`${name}: ${frames.length} frames, ${frames.length ? frames[frames.length - 1].t.toFixed(1) : 0}s`);
  await ctx.close();
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();

  // Lab with copy-on-write: pull the plug again and again.
  await shot(browser, "lab_cow", V(1440, 1350), async (h) => {
    const p = h.page;
    await p.goto(BASE + "/#lab", { waitUntil: "networkidle" });
    await h.zoom();
    await p.selectOption('#view-lab select[aria-label="Engine"]', "cow");
    await p.waitForTimeout(1200);
    await p.mouse.move(1200, 400);
    await h.start();
    await h.wait(700);
    for (let i = 0; i < 4; i++) {
      await h.click('button:has-text("Pull the plug")');
      await h.wait(1500);
    }
    await h.wait(600);
  });

  // Explorer: the proof matrix streams in live.
  await shot(browser, "explorer", V(1440, 1080), async (h) => {
    const p = h.page;
    await p.goto(BASE + "/#explorer", { waitUntil: "networkidle" });
    await h.zoom();
    await p.waitForTimeout(1500);
    await p.mouse.move(930, 800);
    await h.start();
    await h.wait(500);
    await h.click('button:has-text("Re-run fresh")');
    await p.waitForFunction(() => /done in/.test(document.querySelector("#view-explorer .muted.small")?.textContent || ""), null, { timeout: 120000 }).catch(() => {});
    await h.wait(1200);
  });

  // Torture: real kill -9 against real files.
  await shot(browser, "torture", V(1440, 900), async (h) => {
    const p = h.page;
    await p.goto(BASE + "/#torture", { waitUntil: "networkidle" });
    await h.zoom();
    for (const box of await p.$$("#view-torture .checks input")) {
      const v = await box.getAttribute("value");
      const want = ["naive/trunc", "wal", "cow"].includes(v);
      if ((await box.isChecked()) !== want) await box.click();
    }
    await p.fill('#view-torture input[type="number"]', "24");
    await p.mouse.move(930, 660);
    await h.start();
    await h.wait(500);
    await h.click('button:has-text("Start killing")');
    await p.waitForFunction(() => [...document.querySelectorAll("#view-torture .t-card .ticker")]
      .filter((n) => /real crashes in/.test(n.textContent)).length >= 3, null, { timeout: 240000 }).catch(() => {});
    await h.wait(1500);
  });

  // Documents: edit a real document, save it, then kill the writer mid-save.
  await shot(browser, "docs", V(1440, 980), async (h) => {
    const p = h.page;
    await p.goto(BASE + "/#docs", { waitUntil: "networkidle" });
    await h.zoom();
    await p.waitForTimeout(1200);
    await p.mouse.move(1060, 400);
    await h.start();
    await h.wait(400);
    await h.click("#view-docs textarea", 22);
    await p.keyboard.press("Control+End");
    await p.keyboard.type("\nwater bottles #61,999,shelf-1  <- urgent restock", { delay: 38 });
    await h.wait(300);
    await p.selectOption('#view-docs select[aria-label="Crash point"]', "3");
    await h.click('button:has-text("Save & kill -9")');
    await p.waitForSelector("#view-docs .verdict", { timeout: 60000 });
    await h.wait(400);
    await p.evaluate(() => document.querySelector("#view-docs .verdict").scrollIntoView({ behavior: "smooth", block: "center" }));
    await h.wait(2600);
  });

  await browser.close();
})();
