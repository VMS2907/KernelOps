"""Crash Arena: real kills, honest counters, fairness, limits, access control, clean shutdown."""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from safewrite.arena import (SIDES, ArenaEngine, ArenaFull, DocumentScenario, FairQueue, RateLimiter, _Token,
                             clean_nickname)
from safewrite.store import SafeStore
from safewrite.torture import KEY, _spawn, _wait_ready, content_for


def cid(i):
    return f"phone-{i:06d}"


def accounted(s):
    """accepted taps = processed + failed + stale + discarded + pending + in flight."""
    q = s["queue"]
    return s["processed"] + s["failed"] + s["stale"] + s["discarded"] + q["pending"] + q["in_flight"]


class RealCrashes(unittest.TestCase):
    """One warm engine for the class: starting writers is the expensive part on CI."""

    @classmethod
    def setUpClass(cls):
        cls.spawned = []
        cls.arena = ArenaEngine(pool=3, size=16 * 1024, delay_max=0.002, seed=7, max_phones=1000)
        cls.arena.on_spawn = cls.spawned.append
        cls.arena.start()
        if not cls.arena.wait_warm(120):
            raise AssertionError(f"writers did not warm up: {cls.arena.snapshot()['pool']}")

    @classmethod
    def tearDownClass(cls):
        cls.arena.shutdown()

    def setUp(self):
        self.arena.control("reset")
        self.arena.control("start")

    def test_50_taps_crash_both_sides_for_real(self):
        a = self.arena
        for i in range(50):
            self.assertTrue(a.tap(cid(i), f"Student {i}")["accepted"])
        self.assertTrue(a.wait_idle(240), a.snapshot())
        s = a.snapshot()
        self.assertEqual(s["failed"], 0, s["pool"])
        self.assertEqual(s["processed"], 50)
        for side in SIDES:
            st = s["sides"][side]
            self.assertEqual(st["crashes"], 50)
            self.assertEqual(sum(st["verdicts"].values()), 50, "every crash has exactly one verdict")
        self.assertEqual(s["sides"]["safe"]["corrupted"], 0, s["sides"]["safe"]["verdicts"])
        self.assertEqual(set(s["sides"]["safe"]["verdicts"]) - {"OLD", "NEW"}, set())
        # naive/trunc destroys the file in roughly half of all kills; 0 of 50 would mean no real kill landed
        self.assertGreater(s["sides"]["naive"]["corrupted"], 0, s["sides"]["naive"]["verdicts"])
        self.assertGreaterEqual(s["taps"]["phone"]["received"], s["processed"])
        self.assertEqual(s["accepted"], accounted(s))
        # each writer really died: far more processes were started than the pool holds
        self.assertGreaterEqual(len(self.spawned), 2 * 50)

    def test_fairness_one_crash_per_side_per_tap(self):
        a = self.arena
        for i in range(12):
            a.tap(cid(1000 + i), "fair")
        self.assertTrue(a.wait_idle(120))
        pairs = a.export()["pairs"]
        self.assertEqual(len(pairs), 12)
        self.assertEqual(len({p["tap"] for p in pairs}), 12, "one record per tap")
        ids = []
        for p in pairs:
            self.assertEqual(set(p["sides"]), set(SIDES), "each processed tap crashed both sides")
            ids += [p["sides"][s]["id"] for s in SIDES]
            for s in SIDES:
                v = p["sides"][s]
                self.assertIn(v["verdict"], DocumentScenario.VERDICTS)
                self.assertGreaterEqual(v["acked"], 0, "the writer was mid-way through its save loop")
            self.assertLessEqual(p["delay_ms"], 2.0 + 1e-6, "one delay drawn per tap, shared by both kills")
        self.assertEqual(len(set(ids)), 24)
        self.assertEqual({p["first_killed"] for p in pairs}, set(SIDES), "who dies first alternates")

    def test_rate_limit_counts_but_does_not_queue(self):
        a = self.arena
        results = [a.tap(cid(2000), "speedy") for _ in range(30)]
        reasons = [r["reason"] for r in results]
        passed = 30 - reasons.count("rate_limited")
        self.assertGreaterEqual(passed, 8)
        self.assertLessEqual(passed, 10)            # burst of 8 plus what refilled during the loop
        self.assertEqual(set(reasons) - {"queued", "rate_limited", "backlog"}, set())
        accepted = reasons.count("queued")
        self.assertGreaterEqual(accepted, 3)        # at most 3 waiting per phone; lanes may take some at once
        self.assertTrue(a.wait_idle(120))
        s = a.snapshot()
        tp = s["taps"]["phone"]
        self.assertEqual(tp["received"], 30)
        self.assertEqual((tp["accepted"], tp["rate_limited"], tp["backlog"]),
                         (accepted, 30 - passed, reasons.count("backlog")))
        self.assertEqual(s["processed"], accepted)
        self.assertGreaterEqual(tp["received"], s["sides"]["naive"]["crashes"])

    def test_taps_are_not_crashes_outside_live(self):
        a = self.arena
        a.control("freeze")
        r = a.tap(cid(3000), "late")
        self.assertFalse(r["accepted"])
        self.assertEqual(r["reason"], "not_live")
        a.control("reset")
        self.assertEqual(a.tap(cid(3000), "early")["reason"], "not_live")
        s = a.snapshot()
        self.assertEqual((s["state"], s["processed"], s["taps"]["phone"]["not_live"]), ("LOBBY", 0, 1))

    def test_phone_sees_its_own_verdicts(self):
        a = self.arena
        a.join(cid(4000), "<b>Priya</b>")
        a.tap(cid(4000))
        self.assertTrue(a.wait_idle(120))
        me = a.me(cid(4000))
        self.assertEqual((me["nickname"], me["your_crashes"]), ("Priya", 1))
        self.assertEqual(set(me["last"]["sides"]), set(SIDES))
        self.assertEqual(a.snapshot()["leaderboard"][0], {"nickname": "Priya", "crashes": 1})


class Limits(unittest.TestCase):
    def test_bounded_queue_drops_and_says_so(self):
        a = ArenaEngine(queue_max=5)      # never started: nothing drains the queue
        try:
            a.control("start")
            got = [a.tap(cid(i), "x") for i in range(12)]
            self.assertEqual(sum(r["accepted"] for r in got), 5)
            self.assertEqual({r["reason"] for r in got[5:]}, {"busy"})
            s = a.snapshot()
            self.assertEqual(s["taps"]["phone"], {"received": 12, "accepted": 5, "rate_limited": 0,
                                                  "queue_full": 7, "backlog": 0, "not_live": 0})
            self.assertEqual(s["queue"]["pending"], 5)
            a.control("freeze")
            s = a.snapshot()
            self.assertEqual((s["discarded"], s["queue"]["pending"], s["processed"]), (5, 0, 0))
            self.assertEqual(s["accepted"], accounted(s))
        finally:
            a.shutdown()

    def test_at_most_60_phones_and_closed_tabs_give_their_seat_back(self):
        a = ArenaEngine()                         # default cap: 60 phones
        try:
            self.assertEqual(a.max_phones, 60)
            for i in range(60):
                a.join(cid(i), f"p{i}")
            with self.assertRaises(ArenaFull):
                a.join(cid(60), "61st")
            with self.assertRaises(ArenaFull):
                a.tap(cid(60))                    # tapping without a seat is refused too
            a.join(cid(7), "renamed")             # a seated phone can always come back
            self.assertEqual(a.snapshot()["phones"]["joined"], 60)
            a.clients[cid(3)].seen -= 120         # this phone closed its tab two minutes ago
            self.assertEqual(a.join(cid(60), "61st")["nickname"], "61st")
            self.assertNotIn(cid(3), a.clients)
            ph = a.snapshot()["phones"]
            self.assertEqual((ph["joined"], ph["max"], ph["full"]), (60, 60, True))
            self.assertEqual(a.snapshot()["taps"]["phone"]["received"], 0, "refused phones never count as taps")
        finally:
            a.shutdown()

    def test_rate_limiter_and_fair_queue(self):
        rl = RateLimiter(8)
        self.assertEqual(sum(rl.allow("p", 100.0) for _ in range(20)), 8)
        self.assertEqual(sum(rl.allow("p", 100.5) for _ in range(20)), 4)
        self.assertTrue(rl.allow("other", 100.5))
        q = FairQueue(10)
        for i in range(5):
            self.assertIsNone(q.put("fast", _Token(i, "fast", "", "phone", 0.0, 0)))
        q.put("slow", _Token(99, "slow", "", "phone", 0.0, 0))
        order = [q.get(0).client for _ in range(6)]
        self.assertEqual(order[:2], ["fast", "slow"], "a fast tapper cannot push others to the back")
        self.assertIsNone(q.get(0))
        capped = FairQueue(10, per_client=3)
        got = [capped.put("p", _Token(i, "p", "", "phone", 0.0, 0)) for i in range(5)]
        self.assertEqual(got, [None, None, None, FairQueue.BACKLOG, FairQueue.BACKLOG])
        self.assertIsNone(capped.put("q", _Token(9, "q", "", "phone", 0.0, 0)), "other phones still get in")

    def test_nicknames(self):
        self.assertEqual(clean_nickname("<script>alert(1)</script>Rahul"), "alert(1)Rahul")
        self.assertEqual(clean_nickname("  Priya   S  "), "Priya S")
        self.assertEqual(len(clean_nickname("x" * 100)), 16)
        self.assertEqual(clean_nickname("Kshitij"), "Kshitij")       # not a substring false positive
        self.assertEqual(clean_nickname("f.u.c.k"), "Anonymous")
        self.assertEqual(clean_nickname("<<>>"), "")
        with self.assertRaises(ValueError):
            ArenaEngine(safe="naive")                                # the right side must be crash-safe


class Shutdown(unittest.TestCase):
    def test_clean_shutdown_leaves_no_processes_and_no_files(self):
        procs = []
        a = ArenaEngine(pool=2, size=4096, delay_max=0.001)
        a.on_spawn = procs.append
        base = a.base
        a.start()
        self.assertTrue(a.wait_warm(120))
        a.control("start")
        for i in range(6):
            a.tap(cid(i), "bye")
        time.sleep(0.05)                      # shut down with crashes in flight
        a.shutdown()
        a.shutdown()                          # idempotent
        self.assertGreater(len(procs), 0)
        self.assertTrue(all(p.poll() is not None for p in procs), "every writer is dead and reaped")
        if hasattr(signal, "SIGKILL"):        # POSIX: the pids are gone, not zombies
            for p in procs:
                with self.assertRaises(ProcessLookupError):
                    os.kill(p.pid, 0)
        self.assertFalse(os.path.exists(base), "temp stores deleted")
        alive = [t.name for t in threading.enumerate() if t.name.startswith("arena-")]
        self.assertEqual(alive, [])

    def test_gated_writer_exits_when_its_parent_goes_away(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "s")
            with SafeStore.create(path, engine="cow") as s:
                s.put(KEY, content_for(0, 4096))
            p = _spawn(path, 0, 4096, gated=True)
            _wait_ready(p)
            p.stdin.close()                   # what the OS does when the arena dies
            self.assertEqual(p.wait(timeout=30), 0)
            p.stdout.close()
            p.stderr.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Pluggable(unittest.TestCase):
    def test_engine_only_sees_verdicts(self):
        """A scenario with its own invariant plugs in without touching the engine."""
        class Strict(DocumentScenario):
            name = "strict"

            def judge(self, path, state, acked):
                v = super().judge(path, state, acked)
                v.code, v.ok = ("KEPT" if v.code == "OLD" else "MOVED"), v.code == "OLD"
                return v

        a = ArenaEngine(pool=1, size=4096, delay_max=0.001, scenario=Strict(4096))
        try:
            a.start()
            self.assertTrue(a.wait_warm(120))
            a.control("start")
            for i in range(4):
                a.tap(cid(i), "s")
            self.assertTrue(a.wait_idle(120))
            st = a.snapshot()["sides"]["safe"]
            self.assertEqual(sum(st["verdicts"].values()), 4)
            self.assertLessEqual(set(st["verdicts"]), {"KEPT", "MOVED"})
            self.assertEqual(st["corrupted"], st["verdicts"].get("MOVED", 0))
            self.assertEqual(a.meta()["scenario"]["name"], "strict")
        finally:
            a.shutdown()


class HTTP(unittest.TestCase):
    TOKEN = "test-host-token"

    @classmethod
    def setUpClass(cls):
        from safewrite.server.app import make_arena_server
        cls.tmp = tempfile.mkdtemp()
        cls.arena = ArenaEngine(pool=1, size=4096, delay_max=0.001).start()
        cls.httpd = make_arena_server("127.0.0.1", 0, cls.arena, cls.TOKEN, os.path.join(cls.tmp, "lab"))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.arena.shutdown()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.httpd.RequestHandlerClass.state.close_store()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def tearDown(self):
        self.httpd.RequestHandlerClass.loopback = frozenset({"127.0.0.1", "::1"})

    def req(self, path, body=None, headers=None, method=None):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def test_pages(self):
        for path, text in (("/tap", "Press for MARKS".encode()), ("/tap", "😈".encode()), ("/tap.js", b"Press for MARKS"),
                           ("/arena", b"Final_Assignment.txt"), ("/arena.js", b"EventSource"),
                           ("/vendor/qrcode.js", b"Copyright (c) 2009 Kazuhiko Arase")):
            code, body, _ = self.req(path)
            self.assertEqual(code, 200, path)
            self.assertTrue(text in body, f"{path} should contain {text!r}")

    def test_custom_tap_sound(self):
        from safewrite.server.app import check_tap_sound
        self.assertEqual(self.req("/tap-sound")[0], 404)            # default: phones synthesize "faah"
        clip = os.path.join(self.tmp, "faah.wav")
        with open(clip, "wb") as f:
            f.write(b"RIFF" + b"\0" * 60)
        cls = self.httpd.RequestHandlerClass
        cls.tap_sound = check_tap_sound(clip)
        cls.loopback = frozenset()                                 # phones on the Wi-Fi may fetch it
        try:
            code, body, headers = self.req("/tap-sound")
            self.assertEqual((code, headers["Content-Type"], body[:4]), (200, "audio/wav", b"RIFF"))
        finally:
            cls.tap_sound = None
        with self.assertRaises(ValueError):
            check_tap_sound(os.path.join(self.tmp, "faah.exe"))
        with self.assertRaises(ValueError):
            check_tap_sound(os.path.join(self.tmp, "missing.mp3"))

    def test_control_requires_the_host_token(self):
        self.assertEqual(self.req("/api/arena/control", {"action": "start"})[0], 403)
        self.assertEqual(self.req("/api/arena/control", {"action": "start"}, {"X-Arena-Token": "nope"})[0], 403)
        self.assertEqual(self.req("/api/arena/control", {"action": "start"}, {"Cookie": "sw_host=nope"})[0], 403)
        self.assertEqual(self.req("/api/arena/export")[0], 403)
        self.assertEqual(self.arena.state, "LOBBY")
        code, body, _ = self.req("/api/arena/control", {"action": "start"}, {"X-Arena-Token": self.TOKEN})
        self.assertEqual((code, json.loads(body)["state"]), (200, "LIVE"))
        _, _, headers = self.req("/arena?host=" + self.TOKEN)        # the projector gets a cookie
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        code, body, _ = self.req("/api/arena/control", {"action": "freeze"}, {"Cookie": cookie})
        self.assertEqual((code, json.loads(body)["state"]), (200, "FROZEN"))
        self.assertEqual(self.req("/api/arena/control", {"action": "explode"}, {"X-Arena-Token": self.TOKEN})[0], 400)

    def test_phone_flow_and_stream(self):
        self.req("/api/arena/control", {"action": "reset"}, {"X-Arena-Token": self.TOKEN})
        self.req("/api/arena/control", {"action": "start"}, {"X-Arena-Token": self.TOKEN})
        code, body, _ = self.req("/api/arena/join", {"client_id": cid(1), "nickname": "Rahul"})
        self.assertEqual((code, json.loads(body)["nickname"]), (200, "Rahul"))
        code, body, _ = self.req("/api/arena/tap", {"client_id": cid(1)})
        self.assertEqual((code, json.loads(body)["accepted"]), (200, True))
        self.assertEqual(self.req("/api/arena/tap", {"client_id": "x"})[0], 400)
        self.assertEqual(self.req("/api/arena/tap", b"{" + b" " * 9000 + b"}")[0], 400)
        self.assertTrue(self.arena.wait_idle(60))
        me = json.loads(self.req(f"/api/arena/me?client_id={cid(1)}")[1])
        self.assertEqual(me["your_crashes"], 1)
        self.arena.max_phones = 1                 # the room is full: a new phone gets 409, not a seat
        try:
            code, body, _ = self.req("/api/arena/join", {"client_id": cid(2), "nickname": "late"})
            self.assertEqual((code, json.loads(body)["full"]), (409, True))
            self.assertEqual(self.req("/api/arena/tap", {"client_id": cid(2)})[0], 409)
            self.assertEqual(self.req("/api/arena/tap", {"client_id": cid(1)})[0], 200)
        finally:
            self.arena.max_phones = 60
        with urllib.request.urlopen(self.base + "/api/arena/stream", timeout=30) as r:
            events = []
            while len(events) < 3:
                line = r.readline().decode()
                if line.startswith("event: "):
                    events.append(line[7:].strip())
        self.assertEqual(events[0], "hello")
        self.assertIn("stats", events)
        self.assertIn("crashes", events)

    def test_lan_phones_get_the_arena_only(self):
        self.httpd.RequestHandlerClass.loopback = frozenset()     # pretend we are a phone on the Wi-Fi
        self.assertEqual(self.req("/api/meta")[0], 403)
        self.assertEqual(self.req("/api/store")[0], 403)
        self.assertEqual(self.req("/api/bench?run=1")[0], 403)
        opener = urllib.request.build_opener(type("NoRedirect", (urllib.request.HTTPRedirectHandler,),
                                                  {"redirect_request": lambda *a, **k: None}))
        try:
            opener.open(self.base + "/", timeout=10)
            self.fail("expected a redirect")
        except urllib.error.HTTPError as e:
            self.assertEqual((e.code, e.headers["Location"]), (302, "/tap"))
        self.assertEqual(self.req("/tap")[0], 200)
        self.assertEqual(self.req("/arena.js")[0], 200)
        self.assertEqual(self.req("/api/arena/me?client_id=" + cid(5))[0], 200)
        self.assertEqual(self.req("/api/meta", headers={"Cookie": "sw_host=" + self.TOKEN})[0], 200)


if __name__ == "__main__":
    unittest.main()
