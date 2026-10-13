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


  // ------------------------------------------------------------- the bhopu (bulb horn)
  // Synthesized with Web Audio, so there is no sound file and it works offline: two
  // squeezes of a rubber-bulb reed horn, "bhoo-bhoo". A sawtooth and a slightly detuned
  // square go through a soft clipper (the brassy rasp) and a nasal band-pass. The pitch
  // scoops up as the bulb is squeezed and sags as it lets go.
  var HONK_CURVE = (function () {
    var n = 1024, c = new Float32Array(n);
    for (var i = 0; i < n; i++) { var x = (i * 2) / n - 1; c[i] = Math.tanh(3 * x); }
    return c;
  })();

  function bhoo(ctx, out, t, f0, len) {
    var mix = ctx.createGain(), shaper = ctx.createWaveShaper(), g = ctx.createGain();
    var nasal = ctx.createBiquadFilter(), body = ctx.createBiquadFilter();
    mix.gain.value = 0.45;
    shaper.curve = HONK_CURVE;
    nasal.type = "bandpass"; nasal.frequency.value = f0 * 2.7; nasal.Q.value = 1.3;
    body.type = "lowpass"; body.frequency.value = 2200;
    [["sawtooth", 1], ["square", 1.008]].forEach(function (spec) {
      var o = ctx.createOscillator(), f = o.frequency, k = spec[1];
      o.type = spec[0];
      f.setValueAtTime(f0 * 0.8 * k, t);
      f.exponentialRampToValueAtTime(f0 * k, t + 0.04);
      f.setValueAtTime(f0 * k, t + len * 0.65);
      f.exponentialRampToValueAtTime(f0 * 0.86 * k, t + len);
      o.connect(mix);
      o.start(t);
      o.stop(t + len + 0.03);
    });
    mix.connect(shaper);
    shaper.connect(nasal); shaper.connect(body);
    nasal.connect(g); body.connect(g);
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.3, t + 0.025);
    g.gain.setValueAtTime(0.27, t + len - 0.06);
    g.gain.exponentialRampToValueAtTime(0.0001, t + len);
    g.connect(out);
  }

  // "bhoo-bhoo": returns the time the horn falls silent.
  function bhopu(ctx, out, t, pitch) {
    var f0 = 300 * (pitch || 1);
    bhoo(ctx, out, t, f0, 0.16);
    bhoo(ctx, out, t + 0.22, f0 * 0.93, 0.24);
    return t + 0.48;
  }
  window.SafeWriteBhopu = bhopu;            // lets a test render it offline

  var audio = { ctx: null, muted: store.get("sw-arena-mute") === "1", voices: 0, played: 0 };

  function audioCtx() {
    if (!audio.ctx) {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      try { audio.ctx = new AC(); } catch (e) { return null; }
    }
    if (audio.ctx.state === "suspended" && audio.ctx.resume) {
      try { var p = audio.ctx.resume(); if (p && p.catch) p.catch(function () {}); } catch (e) { /* no gesture yet */ }
    }
    return audio.ctx;
  }

  function honk() {
    if (audio.muted || audio.voices >= 3) return;   // at most 3 horns at once per phone
    var ctx = audioCtx();
    if (!ctx) return;
    var play = function () {
      audio.voices++;
      audio.played++;
      var master = ctx.createGain();
      master.gain.value = 0.9;
      master.connect(ctx.destination);
      var end = bhopu(ctx, master, ctx.currentTime + 0.005, 0.92 + Math.random() * 0.16);
      setTimeout(function () { audio.voices--; master.disconnect(); }, (end - ctx.currentTime) * 1000 + 120);
    };
    if (ctx.state === "running") play();
    else if (ctx.resume) { try { ctx.resume().then(play, function () {}); } catch (e) { /* ignore */ } }
  }
  window.SafeWriteHornStats = function () { return { played: audio.played, state: audio.ctx && audio.ctx.state }; };

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
    label.textContent = s === "LIVE" ? "PULL THE PLUG" : s === "FROZEN" ? "LOOK UP ↑" : "WAITING…";
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
