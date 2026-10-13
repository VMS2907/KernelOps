# Crash Arena: build brief (next task, not started yet)

> **Notes for the session that builds this** (added after the brief was written; the brief
> itself follows unchanged):
>
> * Work on top of branch `claude/confident-sagan-o9suhm`. The repository's default branch and
>   `main` do **not** contain the live-challenge work (`safewrite/challenges.py`,
>   `safewrite/errors.py`, `safewrite/vfs/faulty.py`, `docs/CHALLENGES.md`).
> * The suite now has **78** tests, not 54 (24 were added in `tests/test_challenges.py`). Where the
>   brief says "all 54 existing tests must still pass", read "all 78".
> * New since the brief: `python -m safewrite challenge` (C1–C5), the save error contract
>   (`CommitAborted` / `VisibleNotDurable`), `FileLock` with shared/exclusive modes in
>   `safewrite/concurrency.py`, and a multi-writer mode for the shadow engine. CI also runs
>   `python -m safewrite challenge`.
> * Lesson from CI: `time.perf_counter()` is per-process on macOS and on Windows with Python 3.9.
>   Never compare it across processes; `safewrite/challenges.py` has a system-wide `_now()`.

---

# Task: build "Crash Arena" — the whole audience crashes SafeWrite live from their phones

## Context
This repo is SafeWrite (OSH021, Kernel Ops hackathon final round). It already has:
- 5 engines (naive, shadow, undo, wal, cow) behind SafeStore
- safewrite/torture.py: real kill -9 loop (child_main writer, _spawn, _kill, _judge, content_for/version_of)
- safewrite/server/app.py: stdlib ThreadingHTTPServer, ROUTES dict, SSE helpers (sse_start/sse), static files in server/static/
- 54 passing tests, CI on Linux/macOS/Windows, ZERO third-party dependencies

Read README.md, docs/DESIGN.md, torture.py and server/app.py fully before writing code.

## Goal
On stage, we put a QR code on the projector. Every student opens it on their phone and gets one giant
red ⚡ button. Each tap REALLY kills a process mid-save (SIGKILL / TerminateProcess). The projector
shows two live counters side by side:
- LEFT  "Normal save" (naive/trunc = what open(path, "w") does): corrupted files climbing
- RIGHT "SafeWrite" (cow): crashes climbing, corruptions stuck at 0
The audience does the proof themselves. It must be understandable to a non-OS person in 5 seconds.

## Hard rules
1. HONESTY IS EVERYTHING. Every number shown must come from a real kill + real recovery + real
   _judge() verdict. No simulated, interpolated, or animated-up counts. If taps outpace kills, show
   BOTH "taps received" and "real crashes performed" — never pretend a tap was a crash.
2. FAIRNESS: one tap = kill one naive writer AND one cow writer at the same moment, same random
   delay distribution. This answers "did you rig the naive side?"
3. Zero third-party Python deps. Stdlib only. For the QR code, vendor a small MIT-licensed JS QR
   generator (e.g. qrcode-generator by Kazuhiko Arase) into server/static/vendor/ with its license
   file. Must work fully offline (phone hotspot, no internet).
4. Do NOT modify engine code, the crash simulator, or existing behaviour. All 54 existing tests must
   still pass on all CI platforms.
5. Never block the HTTP server threads on a kill/recover cycle.

## Backend: safewrite/arena.py
- ArenaEngine class managing a POOL of K pre-warmed writer processes per side (default K=4, each
  with its own temp store dir), reusing torture.py's writer/judge logic (refactor shared helpers
  into importable functions if needed, without changing torture's behaviour or output).
- A tap enqueues a crash token. A worker thread per side pops tokens, picks a ready writer, waits a
  small random delay (same distribution both sides), kills it, opens the store (runs real
  recovery), calls _judge, records the verdict, and respawns that writer in the background.
- Bounded queue (e.g. max 500 pending). Excess taps are counted as "taps received" but dropped,
  and the UI says so honestly.
- Per-device rate limit (cookie/random client id): max ~8 taps/sec per phone. Nicknames: max 16
  chars, strip HTML, simple profanity filter.
- Record for each crash: id, timestamp, nickname, side, verdict (OLD/NEW/CORRUPT/LOST/EMPTY/TRUNCATED),
  recovered version, and for corruptions the human-readable description from
  crashsim.provenance.describe_mixture (e.g. "empty file (0 bytes)", "bytes 0-16384 from new,
  rest from old").
- Stats: taps received, crashes performed per side, corruptions per side, per-nickname crash counts,
  crashes/sec.
- States: LOBBY (QR shown, counters 0), LIVE, FROZEN (final summary). Host-only controls.
- Clean shutdown: kill all children, delete temp dirs, on Ctrl+C and on exit.

## Server routes (add to ROUTES in app.py)
- GET  /tap                 phone page
- POST /api/arena/tap       {client_id, nickname} -> {accepted, your_crashes}
- GET  /api/arena/stream    SSE: stats every ~200ms + crash feed events
- GET  /arena               projector page
- POST /api/arena/control   {action: start|freeze|reset|autotap_on|autotap_off}, requires a host
                            token printed in the terminal at startup (phones must not control it)

## CLI: `python -m safewrite arena`
- Binds 0.0.0.0 (default port 8765), detects the LAN IP, prints the phone URL, the host URL with
  token, and an ASCII QR in the terminal if feasible (optional).
- Flags: --pool K, --naive-variant (default naive/trunc), --safe-engine (default cow), --port.
- Prints a clear Windows firewall hint if on Windows.

## Phone page (/tap) — mobile-first, works on cheap Android Chrome
- First screen: "Enter your name" -> then a full-screen giant red ⚡ button labelled
  "PULL THE PLUG".
- Haptic vibration on tap (navigator.vibrate, guarded), button press animation.
- Below: "You crashed N saves" and the latest verdict for your taps ("SafeWrite recovered ✓" /
  "Normal save: file destroyed ✗").
- No zoom, no text selection on the button, touch-action: manipulation.
- Handles server disconnect gracefully (retry, small "reconnecting" note).

## Projector page (/arena) — designed for a classroom projector, readable from the back row
- LOBBY: huge QR code + the URL as text + "Scan and pull the plug", live count of joined phones.
- LIVE: split screen.
  LEFT (red): "Normal save — what most apps do". Big numbers: crashes, CORRUPTED FILES.
  A "corruption gallery" of the last few broken files with the plain-English damage
  ("Priya's crash: file is EMPTY — the assignment is gone").
  RIGHT (green): "SafeWrite". Big numbers: crashes, corrupted files = 0 (counter pulses on each
  crash but stays 0). Feed: "Rahul crashed save #482 → recovered version 481 ✓".
- Top bar: taps received, real crashes/sec, top 5 crashers leaderboard.
- The document is named "Final_Assignment.txt" in all UI copy (relatable framing).
- FROZEN: final summary slide: "N students. X real crashes. Normal save destroyed Y files.
  SafeWrite lost 0." plus one line: "kill -9 is the easy case. Power loss is harder — that's what
  our simulator proves." with a button that opens the existing Explorer tab.
- Host keyboard shortcuts on the projector page: S start, F freeze, R reset, A toggle auto-tap.
- AUTO-TAP fallback: server-side generator of crash tokens at a steady rate, for when the network
  fails or phones won't connect. Clearly labelled on screen as "auto" in the feed so it's honest.
- Match the existing Crash Lab's visual style (styles.css). Large type, high contrast, works in a
  bright room.

## Tests (tests/test_arena.py)
- 50 enqueued taps -> both sides perform crashes; crashes performed == verdicts recorded;
  cow corruptions == 0.
- Fairness: each processed tap produced exactly one crash per side.
- Rate limit and bounded-queue behaviour; taps received >= crashes performed always.
- Control endpoint rejects requests without the host token.
- Clean shutdown leaves no child processes and no temp dirs.
- Keep the runtime short enough for CI; skip heavy parts with a clear reason where needed.

## Load test: tools/arena_load.py
Simulates 60 phones tapping at random rates for 60s using threads + urllib. Reports taps sent,
accepted, crashes performed, p95 tap latency, and confirms cow corruptions = 0. Run it and include
the output in docs.

## Docs
- docs/ARENA.md: stage runbook — laptop on charger, phone hotspot setup, firewall, opening the two
  pages, the exact 90-second script, fallback plan (auto-tap), what to say if it breaks.
- Add 3-4 likely judge questions to docs/JUDGE_QA.md: "Is the naive side rigged?",
  "Is a phone tap really a kill -9?", "Why does kill -9 not test durability?",
  "What if two taps arrive at once?" — with crisp answers.
- README: add an Arena row to the Results table (with load-test numbers) and to the Web UI table.

## Working method
1. First, show me a short plan: files to add/change, how you'll refactor torture.py helpers, and the
   process/threading model. Wait for my OK.
2. Build backend + tests, run the full test suite.
3. Build the two pages, then run the load test and fix whatever breaks.
4. At the end, give me: what changed, test results, load-test numbers, and anything you could NOT
   verify (e.g. Windows behaviour) stated plainly.
