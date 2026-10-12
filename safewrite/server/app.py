"""Crash Lab: a zero-dependency web server (stdlib ``http.server``).

``python -m safewrite serve`` then open http://127.0.0.1:8765

JSON API (all GET unless noted):

  /api/meta                         engines, variants, scenarios, models
  /api/trace?spec&scenario&model    recorded syscall trace + crash points
  /api/inspect?...&point&index      one crash state: disk image, recovery log, verdict
  /api/plug?...&want=violation      pick a crash state ("pull the plug")
  /api/explore/stream?specs&scenarios&models   SSE: one event per matrix cell
  /api/explore/cached               the last full matrix, if any
  /api/torture/stream?engines&trials&size      SSE: one event per real kill -9
  /api/bench?run=1                  benchmark results (cached unless run=1)
  /api/store                        the real demo store: documents + last recovery
  /api/store/doc?key                content + history
  POST /api/store/doc {key,text}    save
  POST /api/store/delete {key}
  POST /api/store/restore {key,version}
  POST /api/store/crash {key,text,at}   save in a child process that is killed for
                                         real at syscall ``at``, then recover
"""

from __future__ import annotations

import json
import mimetypes
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..crashsim import DEFAULT_SCENARIOS, MODELS, SCENARIOS, explore
from ..crashsim.explorer import find_state, inspect, trace_view
from ..engines import all_specs, spec_info
from ..format import IntegrityError, sha256
from ..store import SafeStore, StoreError

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESULTS = os.path.join(ROOT, "results")


class State:
    """Process-wide state shared by request threads."""

    def __init__(self, store_dir: str) -> None:
        self.store_dir = os.path.abspath(store_dir)
        self.store: Optional[SafeStore] = None
        self.store_mu = threading.Lock()
        self.explore_cache: Dict[str, Dict[str, Any]] = {}
        self.bench: Optional[Dict[str, Any]] = None
        self.bench_mu = threading.Lock()
        self.last_crash: Optional[Dict[str, Any]] = None
        self._load_results()

    def _load_results(self) -> None:
        for name, attr in (("explore.json", None), ("bench.json", "bench")):
            path = os.path.join(RESULTS, name)
            if not os.path.exists(path):
                continue
            try:
                with open(path) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            if attr == "bench":
                self.bench = data
            else:
                for r in data.get("results", []):
                    self.explore_cache[f"{r['spec']}|{r['scenario']}|{r['model']}"] = r

    # ----------------------------------------------------------- demo store
    def open_store(self) -> SafeStore:
        if self.store is None:
            if not os.path.exists(os.path.join(self.store_dir, "SAFEWRITE.json")):
                s = SafeStore.create(self.store_dir, engine="cow")
                s.put("inventory.csv", SCENARIOS["update"].initial["inventory.csv"])  # type: ignore[arg-type]
                s.put("README.txt", b"This is a real SafeWrite store on your disk.\n"
                                    b"Edit a document, then use 'Save & crash' to kill the writer for real.\n")
                self.store = s
            else:
                self.store = SafeStore.open(self.store_dir)
        return self.store

    def close_store(self) -> None:
        if self.store is not None:
            self.store.close()
            self.store = None


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    raise TypeError(type(obj))


class Handler(BaseHTTPRequestHandler):
    state: State
    server_version = f"SafeWriteCrashLab/{__version__}"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        if os.environ.get("SAFEWRITE_HTTP_LOG"):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------ plumbing
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=_jsonable).encode(), "application/json")

    def error(self, code: int, msg: str) -> None:
        self.json({"error": msg}, code)

    def q(self) -> Dict[str, str]:
        return {k: v[-1] for k, v in parse_qs(urlparse(self.path).query).items()}

    def body(self) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

    def sse(self, event: str, data: Any) -> None:
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data, default=_jsonable)}\n\n".encode())
        self.wfile.flush()

    # ------------------------------------------------------------- routing
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path
        route = ROUTES.get((method, path))
        try:
            if route is not None:
                route(self)
            elif method == "GET":
                self.static(path)
            else:
                self.error(404, "no such endpoint")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, KeyError, StoreError, IntegrityError) as e:
            try:
                self.error(400, f"{type(e).__name__}: {e}")
            except Exception:
                pass
        except Exception as e:  # pragma: no cover
            traceback.print_exc()
            try:
                self.error(500, f"{type(e).__name__}: {e}")
            except Exception:
                pass

    def static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.normpath(os.path.join(STATIC, path.lstrip("/")))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            self.error(404, "not found")
            return
        with open(full, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(200, data, ctype)

    # ----------------------------------------------------------- endpoints
    def api_meta(self) -> None:
        self.json({
            "version": __version__,
            "engines": [spec_info(s) for s in all_specs()],
            "scenarios": [SCENARIOS[s].to_json() for s in DEFAULT_SCENARIOS],
            "models": [m.to_json() for m in MODELS.values()],
            "store_dir": self.state.store_dir,
            "cached_cells": len(self.state.explore_cache),
        })

    def _lab_args(self) -> Dict[str, str]:
        q = self.q()
        spec = q.get("spec", "cow")
        scenario = q.get("scenario", "update")
        model = q.get("model", "power")
        spec_info(spec)
        if scenario not in SCENARIOS or model not in MODELS:
            raise ValueError("unknown scenario or model")
        return {"spec": spec, "scenario": scenario, "model": model, **{k: v for k, v in q.items()
                                                                        if k not in ("spec", "scenario", "model")}}

    def api_trace(self) -> None:
        a = self._lab_args()
        self.json(trace_view(a["spec"], a["scenario"], a["model"]))

    def api_inspect(self) -> None:
        a = self._lab_args()
        self.json(inspect(a["spec"], a["scenario"], a["model"], int(a.get("point", 0)), int(a.get("index", 0))))

    def api_plug(self) -> None:
        a = self._lab_args()
        seed = random.randrange(1 << 30)
        point, index, found = find_state(a["spec"], a["scenario"], a["model"], a.get("want", "random"), seed)
        self.json({"point": point, "index": index, "violation": found})

    def api_explore_cached(self) -> None:
        self.json({"results": list(self.state.explore_cache.values())})

    def api_explore_stream(self) -> None:
        q = self.q()
        specs = [s for s in q.get("specs", "").split(",") if s] or all_specs()
        scenarios = [s for s in q.get("scenarios", "").split(",") if s] or DEFAULT_SCENARIOS
        models = [m for m in q.get("models", "").split(",") if m] or ["power"]
        fresh = q.get("fresh") == "1"
        self.sse_start()
        total = len(specs) * len(scenarios) * len(models)
        done = 0
        t0 = time.perf_counter()
        for model in models:
            for spec in specs:
                for sc in scenarios:
                    key = f"{spec}|{sc}|{model}"
                    r = None if fresh else self.state.explore_cache.get(key)
                    if r is None:
                        r = explore(spec, sc, model)
                        self.state.explore_cache[key] = r
                    done += 1
                    self.sse("cell", {"done": done, "total": total, "result": r})
        self.sse("done", {"seconds": round(time.perf_counter() - t0, 2)})

    def api_torture_stream(self) -> None:
        from ..torture import torture
        q = self.q()
        engines = [e for e in q.get("engines", "naive,shadow,wal,cow").split(",") if e]
        trials = max(1, min(2000, int(q.get("trials", 50))))
        size = max(256, min(4 << 20, int(q.get("size", 65536))))
        for e in engines:
            spec_info(e)
        self.sse_start()
        stopped = {"v": False}

        for eng in engines:
            self.sse("engine", {"engine": eng, "info": spec_info(eng)})

            def tick(ev: Dict[str, Any], eng: str = eng) -> None:
                try:
                    self.sse("kill", {"engine": eng, **ev})
                except (BrokenPipeError, ConnectionResetError, OSError):
                    stopped["v"] = True

            res = torture(eng, trials=trials, size=size, progress=tick, stop=lambda: stopped["v"])
            if stopped["v"]:
                return
            self.sse("result", res.to_json())
        self.sse("done", {})

    def api_bench(self) -> None:
        from ..bench import run_bench
        q = self.q()
        with self.state.bench_mu:
            if q.get("run") == "1" or self.state.bench is None:
                self.state.bench = run_bench(n=int(q.get("n", 150)), size=int(q.get("size", 4096)))
            self.json(self.state.bench)

    # ------------------------------------------------------- real store
    def _store_summary(self, s: SafeStore) -> Dict[str, Any]:
        docs = []
        for k in s.keys():
            try:
                v = s.get(k) or b""
                docs.append({"key": k, "len": len(v), "sha256": sha256(v)[:12], "ok": True})
            except IntegrityError as e:
                docs.append({"key": k, "ok": False, "error": str(e)})
        return {"path": s.path, "engine": s.spec, "info": s.info(), "documents": docs,
                "generation": getattr(s.engine, "generation", None),
                "last_crash": self.state.last_crash}

    def api_store(self) -> None:
        with self.state.store_mu:
            self.json(self._store_summary(self.state.open_store()))

    def api_store_doc_get(self) -> None:
        key = self.q()["key"]
        with self.state.store_mu:
            s = self.state.open_store()
            try:
                v = s.get(key)
                err = None
            except IntegrityError as e:
                v, err = None, str(e)
            hist = [h.to_json() for h in s.history(key)]
        self.json({"key": key, "exists": v is not None, "text": None if v is None else v.decode("utf-8", "replace"),
                   "len": None if v is None else len(v), "error": err, "history": hist})

    def api_store_doc_post(self) -> None:
        b = self.body()
        with self.state.store_mu:
            s = self.state.open_store()
            t0 = time.perf_counter()
            s.put(b["key"], b["text"].encode())
            ms = (time.perf_counter() - t0) * 1e3
            self.json({"ok": True, "ms": round(ms, 3), "summary": self._store_summary(s)})

    def api_store_delete(self) -> None:
        b = self.body()
        with self.state.store_mu:
            s = self.state.open_store()
            s.delete(b["key"])
            self.json({"ok": True, "summary": self._store_summary(s)})

    def api_store_restore(self) -> None:
        b = self.body()
        with self.state.store_mu:
            s = self.state.open_store()
            data = s.engine.get_version(b["key"], int(b["version"]))  # type: ignore[attr-defined]
            s.put(b["key"], data or b"")
            self.json({"ok": True, "summary": self._store_summary(s)})

    def api_store_crash(self) -> None:
        """Save in a *separate process* and kill it for real mid-save, then recover here."""
        b = self.body()
        key, text = b["key"], b["text"]
        with self.state.store_mu:
            self.state.open_store()                  # make sure the store exists (first-ever action)
            self.state.close_store()                 # release our lock: the child must own the store
            plan = self._plan(key, text)
            at = int(b.get("at") or 0) or random.randint(1, max(1, plan["syscalls"]))
            env = dict(os.environ, PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""),
                       NO_COLOR="1", PYTHONIOENCODING="utf-8")
            # The new text travels in a temp file: command lines are size-limited (32K on Windows).
            fd, src = tempfile.mkstemp(prefix="safewrite-crash-", suffix=".txt")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(text.encode())
                proc = subprocess.run([sys.executable, "-m", "safewrite", "crash", self.state.store_dir, key,
                                       "--file", src, "--at", str(at)],
                                      capture_output=True, encoding="utf-8", errors="replace", env=env, timeout=60)
            finally:
                try:
                    os.unlink(src)
                except OSError:
                    pass
            t0 = time.perf_counter()
            s = self.state.open_store()               # <- recovery runs here, on the dead child's files
            rec_ms = (time.perf_counter() - t0) * 1e3
            try:
                now = s.get(key)
                err = None
            except IntegrityError as e:
                now, err = None, str(e)
            new = text.encode()
            verdict = ("new" if now == new else "old" if now is not None else "absent") if not err else "damaged"
            self.state.last_crash = {
                "key": key, "at": at, "syscalls": plan["syscalls"], "exit_code": proc.returncode,
                "child_output": proc.stdout[-4000:], "child_error": proc.stderr[-2000:],
                "recovery": s.recovery.to_json(), "recovery_ms": round(rec_ms, 3),
                "verdict": verdict, "error": err,
                "result_sha256": sha256(now)[:12] if now is not None else None,
                "plan": plan["trace"], "time": time.strftime("%H:%M:%S"),
            }
            self.json({"crash": self.state.last_crash, "summary": self._store_summary(s)})

    def _plan(self, key: str, text: str) -> Dict[str, Any]:
        from ..engines import make
        from ..vfs import MemVFS, OsVFS, RecordingVFS
        osv = OsVFS(self.state.store_dir)
        snap = osv.snapshot()
        snap.pop("LOCK", None)
        rec = RecordingVFS(MemVFS(snap, osv.walk_dirs()))
        with open(os.path.join(self.state.store_dir, "SAFEWRITE.json")) as f:
            meta = json.load(f)
        eng = make(meta["engine"], rec, **meta.get("options", {}))
        eng.open()
        rec.reset()
        eng.commit({key: text.encode()})
        return {"syscalls": rec.syscalls, "trace": [op.to_json() for op in rec.trace]}


ROUTES: Dict[Any, Callable[[Handler], None]] = {
    ("GET", "/api/meta"): Handler.api_meta,
    ("GET", "/api/trace"): Handler.api_trace,
    ("GET", "/api/inspect"): Handler.api_inspect,
    ("GET", "/api/plug"): Handler.api_plug,
    ("GET", "/api/explore/cached"): Handler.api_explore_cached,
    ("GET", "/api/explore/stream"): Handler.api_explore_stream,
    ("GET", "/api/torture/stream"): Handler.api_torture_stream,
    ("GET", "/api/bench"): Handler.api_bench,
    ("GET", "/api/store"): Handler.api_store,
    ("GET", "/api/store/doc"): Handler.api_store_doc_get,
    ("POST", "/api/store/doc"): Handler.api_store_doc_post,
    ("POST", "/api/store/delete"): Handler.api_store_delete,
    ("POST", "/api/store/restore"): Handler.api_store_restore,
    ("POST", "/api/store/crash"): Handler.api_store_crash,
}


def make_server(host: str, port: int, store_dir: str) -> ThreadingHTTPServer:
    Handler.state = State(store_dir)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
          store_dir: str = "demo-store") -> None:
    httpd = make_server(host, port, store_dir)
    url = f"http://{host}:{port}/"
    print(f"SafeWrite Crash Lab running at {url}  (Ctrl+C to stop)")
    print(f"real demo store: {os.path.abspath(store_dir)}")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        Handler.state.close_store()
        httpd.server_close()
