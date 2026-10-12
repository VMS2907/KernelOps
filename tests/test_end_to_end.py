"""Real processes, real files: the CLI, real os._exit crashes, kill -9 torture, the web API."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request

from safewrite import SafeStore
from safewrite.torture import torture

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = dict(os.environ, PYTHONPATH=ROOT, NO_COLOR="1", PYTHONIOENCODING="utf-8")


def cli(*args, check=True):
    p = subprocess.run([sys.executable, "-m", "safewrite", *args], capture_output=True, encoding="utf-8",
                       errors="replace", env=ENV, timeout=120)
    if check and p.returncode != 0:
        raise AssertionError(f"{args} -> {p.returncode}\n{p.stdout}\n{p.stderr}")
    return p


class RealCrashes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_os_exit_at_every_syscall_recovers_old_or_new(self):
        A = "item,qty\nwater,120\n" * 40
        B = "item,qty\nwater,100\nfood,95\n" * 30
        for engine in ("shadow", "undo", "wal", "cow"):
            store = os.path.join(self.tmp, engine)
            cli("init", store, "--engine", engine)
            cli("put", store, "doc", "--text", A)
            plan = cli("crash", store, "doc", "--text", B, "--list").stdout
            n = len(re.findall(r"^\s+\[\s*\d+\]", plan, flags=re.M))
            self.assertGreater(n, 0)
            for at in range(1, n + 1):
                with self.subTest(engine=engine, syscall=at):
                    p = cli("crash", store, "doc", "--text", B, "--at", str(at), check=False)
                    self.assertEqual(p.returncode, 77, p.stdout + p.stderr)
                    out = os.path.join(self.tmp, "out")
                    cli("recover", store, "--key", "doc", "--out", out)
                    with open(out, encoding="utf-8", newline="") as f:
                        got = f.read()
                    self.assertIn(got, (A, B))
                    with SafeStore.open(store) as s:          # reset to A for the next crash
                        s.put("doc", A.encode())

    def test_kill9_torture_safe_engines(self):
        for engine in ("shadow", "wal", "cow"):
            with self.subTest(engine=engine):
                r = torture(engine, trials=8, size=16 * 1024, directory=self.tmp)
                self.assertEqual(r.violations, 0, r.examples)
                self.assertEqual(r.trials, 8)


class CLI(unittest.TestCase):
    def test_store_commands(self):
        tmp = tempfile.mkdtemp()
        try:
            s = os.path.join(tmp, "s")
            cli("init", s, "--engine", "cow")
            cli("put", s, "k", "--text", "v1")
            cli("put", s, "k", "--text", "v2")
            self.assertEqual(cli("get", s, "k").stdout, "v2")
            self.assertIn("v", cli("history", s, "k").stdout)
            self.assertIn("verified", cli("verify", s).stdout)
            self.assertEqual(json.loads(cli("info", s).stdout)["engine"], "cow")
            cli("rm", s, "k")
            self.assertEqual(cli("get", s, "k", check=False).returncode, 1)
        finally:
            shutil.rmtree(tmp)

    def test_explore_and_trace(self):
        p = cli("explore", "--engines", "cow,shadow/no-fsync", "--scenarios", "update", "--models", "power")
        self.assertIn("safe engines", p.stdout)
        self.assertIn("COMMIT POINT", cli("trace", "wal").stdout)


class WebAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from safewrite.server.app import make_server
        cls.tmp = tempfile.mkdtemp()
        cls.httpd = make_server("127.0.0.1", 0, os.path.join(cls.tmp, "store"))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        from safewrite.server.app import Handler
        Handler.state.close_store()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=60) as r:
            return json.loads(r.read())

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    def test_lab_endpoints(self):
        meta = self.get("/api/meta")
        self.assertEqual(len(meta["engines"]), 13)
        t = self.get("/api/trace?spec=naive&scenario=update&model=power")
        p = self.get("/api/plug?spec=naive&scenario=update&model=power&want=violation")
        self.assertTrue(p["violation"])
        d = self.get(f"/api/inspect?spec=naive&scenario=update&model=power&point={p['point']}&index={p['index']}")
        self.assertNotEqual(d["outcome"]["code"], "OK")
        self.assertTrue(t["trace"])

    def test_static_files(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as r:
            self.assertIn(b"Crash Lab", r.read())
        for f in ("app.js", "lab.js", "explorer.js", "styles.css"):
            with urllib.request.urlopen(self.base + "/" + f, timeout=10) as r:
                self.assertEqual(r.status, 200)

    def test_crash_as_the_very_first_action(self):
        from safewrite.server.app import Handler
        old_dir = Handler.state.store_dir
        Handler.state.close_store()
        Handler.state.store_dir = os.path.join(self.tmp, "fresh-store")
        try:
            r = self.post("/api/store/crash", {"key": "big", "text": "line\n" * 20000, "at": 2})
            self.assertEqual(r["crash"]["exit_code"], 77)
            self.assertIn(r["crash"]["verdict"], ("absent", "new"))
        finally:
            Handler.state.close_store()
            Handler.state.store_dir = old_dir

    def test_real_store_and_real_crash(self):
        s = self.post("/api/store/doc", {"key": "note", "text": "first"})
        self.assertTrue(s["ok"])
        r = self.post("/api/store/crash", {"key": "note", "text": "second" * 100, "at": 1})
        self.assertEqual(r["crash"]["exit_code"], 77)
        self.assertEqual(r["crash"]["verdict"], "old")
        d = self.get("/api/store/doc?key=note")
        self.assertEqual(d["text"], "first")


if __name__ == "__main__":
    unittest.main()
