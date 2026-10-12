// Render the film: drive scenes.js frame by frame and pipe JPEG frames into ffmpeg.
//   node video/render.js                      -> video/build/film_video.mp4 (+ timeline.json)
//   node video/render.js --stills 3,12,30     -> PNG stills for review
//   node video/render.js --from 40 --to 52    -> partial render (preview)
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");
const { chromium } = require("playwright");

const ROOT = __dirname;
const BUILD = path.join(ROOT, "build");
const args = process.argv.slice(2);
const opt = (name, dflt) => { const i = args.indexOf("--" + name); return i >= 0 ? args[i + 1] : dflt; };

function loadData() {
  const cli = JSON.parse(fs.readFileSync(path.join(BUILD, "cli.json"), "utf8"));
  const footage = {};
  for (const shot of fs.readdirSync(path.join(BUILD, "footage"))) {
    const f = path.join(BUILD, "footage", shot, "index.json");
    if (fs.existsSync(f)) footage[shot] = JSON.parse(fs.readFileSync(f, "utf8"));
  }
  return { cli, footage };
}

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
  page.on("pageerror", (e) => console.error("pageerror:", String(e)));
  await page.goto("file://" + path.join(ROOT, "scenes.html"));
  await page.evaluate((d) => window.init(d), loadData());
  const timeline = await page.evaluate(() => window.TIMELINE);
  fs.writeFileSync(path.join(BUILD, "timeline.json"), JSON.stringify(timeline, null, 1));
  const fps = timeline.fps;
  const stage = await page.$("#stage");

  const stills = opt("stills", null);
  if (stills) {
    fs.mkdirSync(path.join(BUILD, "stills"), { recursive: true });
    for (const t of stills.split(",").map(Number)) {
      await page.evaluate((tt) => window.renderAt(tt), t);
      await stage.screenshot({ path: path.join(BUILD, "stills", `t${t.toFixed(2).padStart(6, "0")}.png`) });
    }
    console.log("stills written");
    await browser.close();
    return;
  }

  const from = Number(opt("from", 0)), to = Number(opt("to", timeline.duration));
  const out = opt("out", path.join(BUILD, from === 0 && to === timeline.duration ? "film_video.mp4" : `preview_${from}_${to}.mp4`));
  const ff = spawn("ffmpeg", ["-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", String(fps), "-c:v", "mjpeg", "-i", "-",
    "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out], { stdio: ["pipe", "inherit", "inherit"] });
  const n0 = Math.round(from * fps), n1 = Math.round(to * fps);
  const t0 = Date.now();
  for (let n = n0; n < n1; n++) {
    await page.evaluate((tt) => window.renderAt(tt), n / fps);
    const buf = await stage.screenshot({ type: "jpeg", quality: 95 });
    if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once("drain", r));
    if (n % 150 === 0) console.log(`frame ${n}/${n1}  (${((Date.now() - t0) / 1000).toFixed(0)} s)`);
  }
  ff.stdin.end();
  await new Promise((r) => ff.on("close", r));
  console.log("wrote", out);
  await browser.close();
})();
