// Crash Arena phone page: one big button, one real crash pair per accepted tap.
// Plain ES2017 (no modules, no optional chaining) so it runs on old Android Chrome.
// Every number shown here comes from the server; nothing is counted locally.
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var store = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };

  function newId() {
    var bytes = new Uint8Array(12);
    if (window.crypto && crypto.getRandomValues) crypto.getRandomValues(bytes);
    else for (var i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
    return "p" + Array.prototype.map.call(bytes, function (b) { return (b < 16 ? "0" : "") + b.toString(16); }).join("");
  }

  var clientId = store.get("sw-arena-id") || newId();
  store.set("sw-arena-id", clientId);
  var nickname = store.get("sw-arena-name") || "";
  var state = "", crashes = 0, inflight = 0, failures = 0, pollTimer = null, noteTimer = null;

  function post(path, body) {
    return fetch(path, {
      method: "POST", cache: "no-store", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json().then(function (d) {
        if (!r.ok) { var e = new Error(d.error || ("HTTP " + r.status)); e.status = r.status; throw e; }
        return d;
      });
    });
  }

  function show(id) {
    $("join").classList.toggle("on", id === "join");
    $("play").classList.toggle("on", id === "play");
  }

  function note(text, ms) {
    var el = $("note");
    clearTimeout(noteTimer);
    if (!text) { el.hidden = true; return; }
    el.textContent = text;
    el.hidden = false;
    if (ms) noteTimer = setTimeout(function () { el.hidden = true; }, ms);
  }


  // ------------------------------------------------------------- the tap sound: "FAAAH"
  // Every tap plays a sound. If the laptop was started with --tap-sound FILE, the phones
  // download that clip once (GET /tap-sound) and play it. Otherwise they synthesize a
  // "FAAAH" with Web Audio: an "f" hiss, then a voice-like "aaah" (a buzzing source
  // through the three formants of the vowel /a/) that slides down in pitch with a wobble.
  // No sound file is needed and it works offline.
  var noiseBuf = null;
  function noise(ctx) {
    if (!noiseBuf || noiseBuf.sampleRate !== ctx.sampleRate) {
      noiseBuf = ctx.createBuffer(1, ctx.sampleRate, ctx.sampleRate);
      var d = noiseBuf.getChannelData(0);
      for (var i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
    }
    var n = ctx.createBufferSource();
    n.buffer = noiseBuf;
    n.loop = true;
    return n;
  }

  function env(param, points) {             // [[time, value], ...], exponential between points
    param.setValueAtTime(points[0][1], points[0][0]);
    for (var i = 1; i < points.length; i++) param.exponentialRampToValueAtTime(points[i][1], points[i][0]);
  }

  // Returns the time the sound ends.
  function faah(ctx, out, t, pitch) {
    var k = pitch || 1, end = t + 1.25;
    // "F": breath through the teeth
    var hiss = noise(ctx), hp = ctx.createBiquadFilter(), hg = ctx.createGain();
    hp.type = "highpass"; hp.frequency.value = 2200;
    env(hg.gain, [[t, 0.001], [t + 0.03, 0.32], [t + 0.12, 0.28], [t + 0.2, 0.001]]);
    hiss.connect(hp); hp.connect(hg); hg.connect(out);
    hiss.start(t); hiss.stop(t + 0.22);
    // "AAAH": a voice. Two buzzing oscillators plus some breath, shaped by the vowel's formants.
    var src = ctx.createGain(), voice = ctx.createGain(), vg = ctx.createGain();
    src.gain.value = 1;
    var lfo = ctx.createOscillator(), lfoDepth = ctx.createGain();
    lfo.frequency.value = 5.5;
    env(lfoDepth.gain, [[t + 0.1, 0.01], [t + 0.45, 0.01], [t + 0.75, 9 * k]]);
    lfo.connect(lfoDepth);
    [["sawtooth", 1], ["sawtooth", 1.006]].forEach(function (spec) {
      var o = ctx.createOscillator(), f = o.frequency, m = spec[1] * k;
      o.type = spec[0];
      env(f, [[t + 0.1, 240 * m], [t + 0.25, 290 * m], [t + 0.55, 250 * m], [end, 150 * m]]);
      lfoDepth.connect(f);
      o.connect(src);
      o.start(t + 0.1);
      o.stop(end + 0.05);
    });
    var breath = noise(ctx), bg = ctx.createGain();
    bg.gain.value = 0.18;
    breath.connect(bg); bg.connect(src);
    breath.start(t + 0.1); breath.stop(end + 0.05);
    [[780, 7, 1.0], [1180, 9, 0.55], [2550, 11, 0.22]].forEach(function (fm) {   // vowel /a/
      var bp = ctx.createBiquadFilter(), g = ctx.createGain();
      bp.type = "bandpass"; bp.frequency.value = fm[0]; bp.Q.value = fm[1];
      g.gain.value = fm[2];
      src.connect(bp); bp.connect(g); g.connect(voice);
    });
    voice.connect(vg);
    env(vg.gain, [[t + 0.1, 0.001], [t + 0.17, 1.5], [t + 0.5, 1.35], [end - 0.25, 0.9], [end, 0.001]]);
    vg.connect(out);
    lfo.start(t + 0.1); lfo.stop(end + 0.05);
    return end;
  }
  window.SafeWriteFaah = faah;              // lets a test render it offline

  var audio = { ctx: null, muted: store.get("sw-arena-mute") === "1", voices: [], played: 0,
                clip: null, clipBytes: null, custom: false };

  // A custom clip (--tap-sound) is fetched right away and decoded once audio is unlocked.
  fetch("/tap-sound", { cache: "force-cache" }).then(function (r) {
    return r.ok ? r.arrayBuffer() : null;
  }).then(function (buf) {
    if (buf && buf.byteLength) { audio.clipBytes = buf; audio.custom = true; decodeClip(); }
  }).catch(function () { /* no custom sound: synthesize */ });

  function decodeClip() {
    if (!audio.ctx || !audio.clipBytes || audio.clip) return;
    var bytes = audio.clipBytes;
    audio.clipBytes = null;
    try {
      audio.ctx.decodeAudioData(bytes, function (b) { audio.clip = b; }, function () { audio.custom = false; });
    } catch (e) { audio.custom = false; }
  }

  function audioCtx() {
    if (!audio.ctx) {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      try { audio.ctx = new AC(); } catch (e) { return null; }
    }
    if (audio.ctx.state === "suspended" && audio.ctx.resume) {
      try { var p = audio.ctx.resume(); if (p && p.catch) p.catch(function () {}); } catch (e) { /* no gesture yet */ }
    }
    decodeClip();
    return audio.ctx;
  }

  // Every tap makes a sound. At most 3 play at once per phone: a new tap fades out the oldest.
  function release(master) {
    var i = audio.voices.indexOf(master);
    if (i < 0) return;
    audio.voices.splice(i, 1);
    try {
      master.gain.setTargetAtTime(0, audio.ctx.currentTime, 0.015);
      setTimeout(function () { master.disconnect(); }, 120);
    } catch (e) { /* already gone */ }
  }

  function honk() {
    if (audio.muted) return;
    var ctx = audioCtx();
    if (!ctx) return;
    var play = function () {
      if (audio.voices.length >= 3) release(audio.voices[0]);
      audio.played++;
      var master = ctx.createGain(), end, t = ctx.currentTime + 0.005, rate = 0.94 + Math.random() * 0.12;
      master.connect(ctx.destination);
      audio.voices.push(master);
      if (audio.clip) {
        var b = ctx.createBufferSource();
        b.buffer = audio.clip;
        b.playbackRate.value = rate;
        b.connect(master);
        b.start(t);
        end = t + audio.clip.duration / rate;
      } else {
        master.gain.value = 0.9;
        end = faah(ctx, master, t, rate);
      }
      setTimeout(function () { release(master); }, (end - ctx.currentTime) * 1000 + 50);
    };
    if (ctx.state === "running") play();
    else if (ctx.resume) { try { ctx.resume().then(play, function () {}); } catch (e) { /* ignore */ } }
  }
  window.SafeWriteHornStats = function () {
    return { played: audio.played, state: audio.ctx && audio.ctx.state, custom: !!audio.clip };
  };

  function renderSound() {
    var b = $("sound");
    b.textContent = audio.muted ? "🔇" : "🔊";
    b.setAttribute("aria-pressed", audio.muted ? "false" : "true");
  }
  $("sound").addEventListener("click", function () {
    audio.muted = !audio.muted;
    store.set("sw-arena-mute", audio.muted ? "1" : "0");
    renderSound();
    if (!audio.muted) honk();
  });
  // Browsers only allow sound after a real tap or click: unlock on the first one.
  ["click", "touchend", "keydown"].forEach(function (type) {
    document.addEventListener(type, function () { if (!audio.muted) audioCtx(); }, { passive: true });
  });
  renderSound();

  // ------------------------------------------------------------- join
  function join(name) {
    $("join-btn").disabled = true;
    return post("/api/arena/join", { client_id: clientId, nickname: name }).then(function (d) {
      nickname = d.nickname;
      store.set("sw-arena-name", nickname);
      $("who").textContent = nickname;
      show("play");
      setState(d.state);
      poll(0);
    }).catch(function (e) {
      var n = $("join-note");
      if (e.status === 409) {
        n.textContent = "The arena is full: " + e.message.replace(/^the arena is full: /, "") +
          ". Watch the big screen, or try again in a minute: a seat frees up when someone leaves.";
        clearTimeout(pollTimer);
        show("join");
      } else {
        n.textContent = "Could not join (" + e.message + "). Are you on the same Wi-Fi as the projector laptop?";
      }
      n.hidden = false;
    }).then(function () { $("join-btn").disabled = false; });
  }

  $("join-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var v = $("name").value.trim();
    if (!audio.muted) audioCtx();                  // this click is the gesture that unlocks sound
    if (v) join(v);
  });
  $("rename").addEventListener("click", function () {
    $("name").value = nickname;
    show("join");
    $("name").focus();
  });

  // ------------------------------------------------------------- state
  function setState(s) {
    if (s === state) return;
    state = s;
    var pill = $("state"), btn = $("plug"), label = $("plug-label");
    pill.className = "pill" + (s === "LIVE" ? " live" : s === "FROZEN" ? " frozen" : "");
    pill.textContent = s === "LIVE" ? "LIVE" : s === "FROZEN" ? "round over" : "waiting for host";
    btn.disabled = s !== "LIVE";
    label.textContent = s === "LIVE" ? "Press for MARKS" : s === "FROZEN" ? "LOOK UP ↑" : "WAITING…";
  }

  function setCrashes(n) {
    crashes = Math.max(0, n | 0);
    $("crashes").textContent = crashes.toLocaleString("en-US");
    $("saves").textContent = crashes === 1 ? "save" : "saves";
  }

  function setTaps(n) {
    $("taps").textContent = (n | 0).toLocaleString("en-US") + (n === 1 ? " tap" : " taps") +
      " received · each crash killed 2 real programs mid-save (" + (2 * crashes).toLocaleString("en-US") + " so far)";
  }

  function okText(v) {
    return v.verdict === "NEW"
      ? "save #" + v.inflight + " finished just before the kill: version " + v.recovered + " is intact"
      : "save #" + v.inflight + " was cut off: recovered version " + v.recovered;
  }

  function verdict(el, v, isSafe) {
    var d = el.querySelector(".d"), b = el.querySelector("b");
    el.className = "v " + (v.ok ? "good" : "bad");
    if (isSafe) b.textContent = v.ok ? "SafeWrite recovered ✓" : "SafeWrite: " + v.verdict + " ✗";
    else b.textContent = v.ok ? "Normal save: survived this one" : "Normal save: file destroyed ✗";
    d.textContent = v.ok ? okText(v) : v.detail;
  }

  function renderLast(last) {
    if (!last || !last.sides) return;
    verdict($("v-naive"), last.sides.naive, false);
    verdict($("v-safe"), last.sides.safe, true);
  }

  // ------------------------------------------------------------- poll (state + my verdicts)
  function poll(delay) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(function () {
      fetch("/api/arena/me?client_id=" + encodeURIComponent(clientId), { cache: "no-store" })
        .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
        .then(function (d) {
          if (failures) note("");
          failures = 0;
          if (!d.known && nickname) { join(nickname); return; }      // the server restarted
          setState(d.state);
          setCrashes(d.your_crashes);
          setTaps(d.your_taps);
          renderLast(d.last);
          poll(document.hidden ? 5000 : 1000);
        })
        .catch(function () {
          failures++;
          note("Reconnecting to the arena…");
          poll(Math.min(5000, 500 * Math.pow(2, failures)));
        });
    }, delay);
  }

  // ------------------------------------------------------------- the button
  var REASONS = {
    rate_limited: "Easy! Max 8 taps a second per phone: that tap did not count.",
    busy: "The laptop is busy crashing: that tap was counted, but no writer was free for it.",
    backlog: "You already have crashes lined up: that tap was counted, wait for those first.",
    not_live: "Not live right now: watch the projector.",
  };

  function press(ev) {
    if (ev.cancelable) ev.preventDefault();
    var btn = $("plug");
    if (state !== "LIVE") return;
    btn.classList.remove("press");
    void btn.offsetWidth;                                  // restart the press animation
    btn.classList.add("press");
    setTimeout(function () { btn.classList.remove("press"); }, 90);
    if (navigator.vibrate) { try { navigator.vibrate(25); } catch (e) { /* not allowed */ } }
    honk();
    if (inflight >= 6) { note("Your phone is still sending taps: give it a moment.", 1500); return; }
    inflight++;
    post("/api/arena/tap", { client_id: clientId, nickname: nickname }).then(function (d) {
      setCrashes(Math.max(crashes, d.your_crashes));
      setTaps(d.your_taps);
      setState(d.state);
      if (!d.accepted) note(REASONS[d.reason] || d.reason, 2000);
      else if (failures === 0) note("");
    }).catch(function (e) {
      if (e.status === 409) { join(nickname); return; }      // lost our seat while away
      note("That tap did not reach the laptop. Reconnecting…", 2500);
      poll(300);
    }).then(function () { inflight--; });
  }

  var plug = $("plug");
  if (window.PointerEvent) plug.addEventListener("pointerdown", press);
  else plug.addEventListener("touchstart", press, { passive: false });
  plug.addEventListener("contextmenu", function (ev) { ev.preventDefault(); });
  document.addEventListener("visibilitychange", function () { if (!document.hidden && nickname) poll(0); });

  // ------------------------------------------------------------- start
  if (nickname) {
    $("name").value = nickname;
    join(nickname);
  }
})();
