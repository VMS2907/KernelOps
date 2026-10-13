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

Crash Arena (``python -m safewrite arena``; see safewrite/arena.py):

  /tap                              phone page          /arena   projector page
  POST /api/arena/join {client_id,nickname}
  POST /api/arena/tap  {client_id,nickname}   -> {accepted, your_crashes}
  /api/arena/me?client_id           the phone's state and its latest verdicts
  /api/arena/stream                 SSE: stats every 200 ms + crash pairs
  POST /api/arena/control {action}  start|freeze|reset|autotap_on|autotap_off (host token)
  /api/arena/export                 every crash record as JSON (host token)
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from http.cookies import CookieError, SimpleCookie
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
    arena: Any = None          # an ArenaEngine, only on servers made by make_arena_server
    host_token: str = ""
    tap_sound: Optional[str] = None   # an audio file the phones play on every tap (--tap-sound)
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

    # ------------------------------------------------------- crash arena
    # Every handler here only reads counters or enqueues a tap: the kills and
    # recoveries run on the arena's own threads, never on a request thread.
    def _arena(self) -> Any:
        if self.arena is None:
            self.error(503, "the arena is not running: start it with  python -m safewrite arena")
        return self.arena

    def _small_body(self, limit: int = 4096) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("request body too large")
        b = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(b, dict):
            raise ValueError("expected a JSON object")
        return b

    def _token_ok(self) -> bool:
        tok = self.headers.get("X-Arena-Token", "")
        if not tok:
            try:
                morsel = SimpleCookie(self.headers.get("Cookie", "")).get("sw_host")
                tok = morsel.value if morsel else ""
            except CookieError:
                tok = ""
        return bool(self.host_token and tok) and hmac.compare_digest(tok.encode(), self.host_token.encode())

    def _page(self, name: str, extra: Optional[Dict[str, str]] = None) -> None:
        with open(os.path.join(STATIC, name), "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def page_tap(self) -> None:
        self._page("tap.html")

    def tap_sound_file(self) -> None:
        if not self.tap_sound:
            self.error(404, "no custom tap sound: the phones synthesize one")
            return
        with open(self.tap_sound, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", TAP_SOUND_TYPES[os.path.splitext(self.tap_sound)[1].lower()])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def page_arena(self) -> None:
        tok = self.q().get("host", "")
        extra = {}
        if tok and self.host_token and hmac.compare_digest(tok.encode(), self.host_token.encode()):
            # The page then removes ?host= from the address bar; the cookie keeps it the host.
            extra["Set-Cookie"] = f"sw_host={tok}; Path=/; SameSite=Strict; HttpOnly"
        self._page("arena.html", extra)

    def _seated(self, fn: Callable[[], Any]) -> None:
        from ..arena import ArenaFull
        try:
            self.json(fn())
        except ArenaFull as e:
            self.json({"error": str(e), "full": True}, 409)

    def api_arena_join(self) -> None:
        a = self._arena()
        if a is not None:
            b = self._small_body()
            self._seated(lambda: a.join(str(b.get("client_id", "")), b.get("nickname")))

    def api_arena_tap(self) -> None:
        a = self._arena()
        if a is not None:
            b = self._small_body()
            self._seated(lambda: a.tap(str(b.get("client_id", "")), b.get("nickname")))

    def api_arena_me(self) -> None:
        a = self._arena()
        if a is not None:
            self.json(a.me(self.q().get("client_id", "")))

    def api_arena_control(self) -> None:
        a = self._arena()
        if a is None:
            return
        if not self._token_ok():
            self.error(403, "host token required")
            return
        self.json(a.control(str(self._small_body().get("action", ""))))

    def api_arena_export(self) -> None:
        a = self._arena()
        if a is None:
            return
        if not self._token_ok():
            self.error(403, "host token required")
            return
        self.json(a.export())

    def api_arena_stream(self) -> None:
        a = self._arena()
        if a is None:
            return
        with _SSE_MU:
            if _SSE["clients"] >= 64:
                self.error(503, "too many live viewers")
                return
            _SSE["clients"] += 1
        try:
            self.sse_start()
            self.sse("hello", {**a.meta(), "is_host": self._token_ok()})
            seq, epoch = max(0, a.processed - 12), None
            while not a.closed:
                snap = a.snapshot()
                if snap["epoch"] != epoch:
                    seq = seq if epoch is None else 0
                    epoch = snap["epoch"]
                pairs, seq = a.feed_since(seq)
                if pairs:
                    self.sse("crashes", pairs)
                self.sse("stats", snap)
                time.sleep(0.2)
        finally:
            with _SSE_MU:
                _SSE["clients"] -= 1


#: The default tap sound shipped with the arena ("faah"); --tap-sound replaces it, "synth" disables it.
DEFAULT_TAP_SOUND = os.path.join(STATIC, "sounds", "faah.mp3")
TAP_SOUND_TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".ogg": "audio/ogg",
                   ".oga": "audio/ogg", ".opus": "audio/ogg", ".wav": "audio/wav", ".webm": "audio/webm"}
_SSE_MU = threading.Lock()
_SSE = {"clients": 0}


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
    ("GET", "/tap"): Handler.page_tap,
    ("GET", "/arena"): Handler.page_arena,
    ("GET", "/tap-sound"): Handler.tap_sound_file,
    ("POST", "/api/arena/join"): Handler.api_arena_join,
    ("POST", "/api/arena/tap"): Handler.api_arena_tap,
    ("GET", "/api/arena/me"): Handler.api_arena_me,
    ("GET", "/api/arena/stream"): Handler.api_arena_stream,
    ("POST", "/api/arena/control"): Handler.api_arena_control,
    ("GET", "/api/arena/export"): Handler.api_arena_export,
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


# ----------------------------------------------------------------------------
# Crash Arena server: the Crash Lab on the LAN, locked down to the arena
# ----------------------------------------------------------------------------
#: What a phone on the LAN may reach.  control/export check the host token themselves.
ARENA_PUBLIC = {("GET", "/tap"), ("GET", "/arena"), ("GET", "/tap-sound"), ("POST", "/api/arena/join"), ("POST", "/api/arena/tap"),
                ("GET", "/api/arena/me"), ("GET", "/api/arena/stream"), ("POST", "/api/arena/control"),
                ("GET", "/api/arena/export")}
ARENA_STATIC = {"/tap.js", "/arena.js", "/arena.css", "/styles.css", "/util.js", "/vendor/qrcode.js"}
LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


class ArenaHandler(Handler):
    """Phones get the arena only.  The full Crash Lab answers on this laptop
    (loopback) or to the host's browser (token cookie), so nobody in the
    audience can start an exploration or a torture run mid-demo."""

    loopback = LOOPBACK

    def is_host(self) -> bool:
        return self.client_address[0] in self.loopback or self._token_ok()

    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path
        if (method, path) in ARENA_PUBLIC or (method == "GET" and path in ARENA_STATIC) or self.is_host():
            super()._dispatch(method)
        elif method == "GET" and path in ("", "/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/tap")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.error(403, "during the arena the Crash Lab is only available on the presenter's laptop")


class ArenaHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256       # socketserver's default of 5 drops SYNs when a room taps at once


def check_tap_sound(path: str) -> str:
    """Validate a --tap-sound file; return its absolute path."""
    full = os.path.abspath(path)
    ext = os.path.splitext(full)[1].lower()
    if ext not in TAP_SOUND_TYPES:
        raise ValueError(f"--tap-sound must be one of {', '.join(sorted(TAP_SOUND_TYPES))}, not {ext or 'no extension'}")
    if not os.path.isfile(full):
        raise ValueError(f"--tap-sound: no such file {full}")
    if os.path.getsize(full) > 5 << 20:
        raise ValueError("--tap-sound: keep the clip under 5 MB (every phone downloads it once)")
    return full


def make_arena_server(host: str, port: int, arena: Any, token: str,
                      store_dir: str = "demo-store", tap_sound: Optional[str] = None) -> ArenaHTTPServer:
    """A server whose handler class is bound to this arena (no global state)."""
    handler = type("BoundArenaHandler", (ArenaHandler,),
                   {"state": State(store_dir), "arena": arena, "host_token": token,
                    "tap_sound": check_tap_sound(tap_sound) if tap_sound else None})
    return ArenaHTTPServer((host, port), handler)


def serve_arena(host: str = "0.0.0.0", port: int = 8765, pool: int = 4, naive: str = "naive/trunc",
                safe: str = "cow", size: int = 65536, queue_max: int = 500, per_phone: int = 3,
                max_phones: int = 30, rate: float = 8.0,
                autotap_rate: float = 10.0, lanes: Optional[int] = None, url_host: Optional[str] = None,
                token: Optional[str] = None, store_dir: str = "demo-store", open_browser: bool = True,
                tap_sound: Optional[str] = None) -> int:
    import secrets
    from ..arena import ArenaEngine, lan_addresses

    token = token or secrets.token_urlsafe(9)
    if tap_sound is None and os.path.isfile(DEFAULT_TAP_SOUND):
        tap_sound = DEFAULT_TAP_SOUND
    elif tap_sound == "synth":
        tap_sound = None
    if tap_sound:
        try:
            tap_sound = check_tap_sound(tap_sound)
        except ValueError as e:
            print(e)
            return 2
    ips = lan_addresses()
    phone_host = url_host or (ips[0] if ips else "127.0.0.1")
    phone_url = f"http://{phone_host}:{port}/tap"
    host_url = f"http://127.0.0.1:{port}/arena?host={token}"
    arena = ArenaEngine(naive=naive, safe=safe, pool=pool, lanes=lanes, size=size, queue_max=queue_max,
                        per_phone=per_phone, max_phones=max_phones, rate=rate, autotap_rate=autotap_rate, public_url=phone_url)
    try:
        httpd = make_arena_server(host, port, arena, token, store_dir, tap_sound)
    except OSError as e:
        print(f"cannot listen on port {port}: {e}\n"
              f"  is the Crash Lab or another arena already running?  try  --port {port + 1}")
        return 2

    def _term(*_: Any) -> None:
        raise KeyboardInterrupt

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _term)
    try:
        print("SafeWrite Crash Arena: warming up writers ...", flush=True)
        arena.start()
        arena.wait_warm(60)
        m = arena.meta()
        print(f"""
  PHONES open:       {phone_url}
  PROJECTOR (host):  {host_url}
  host token:        {token}    (projector keys: S start · F freeze · R reset · A auto-tap)
  phones:            up to {arena.max_phones} (a phone idle for a minute gives its seat to a newcomer)
  tap sound:         {os.path.basename(tap_sound) if tap_sound else 'synthesized "FAAAH" in the browser'}

  left  (normal save): {naive:<12}  right (SafeWrite): {safe}
  writers:           {pool} per side, warm  ·  {size // 1024} KB document  ·  kill delay 0-{m['delay_max_ms']} ms
  queue:             {queue_max} taps max, {per_phone or 'no limit'} waiting per phone  ·  {rate:g} taps/s per phone  ·  auto-tap {autotap_rate:g}/s
""")
        if not ips:
            print("  ! no network address found: join the hotspot/router first and restart, or pass --url-host")
        elif len(ips) > 1 and not url_host:
            print(f"  other addresses:  {', '.join(ips[1:])}")
            print("  (if phones cannot reach the PHONES address, restart with --url-host <one of these>)")
        if sys.platform == "win32":
            print("""
  Windows: when the firewall asks, tick *Private networks* and click Allow.  If phones still
  cannot connect, set the Wi-Fi network to Private and run once in an admin PowerShell:
    New-NetFirewallRule -DisplayName "SafeWrite Arena" -Direction Inbound -Protocol TCP -LocalPort %d -Action Allow -Profile Private""" % port)
        elif sys.platform == "darwin":
            print("  macOS: if asked whether Python may accept incoming connections, click Allow.")
        print("\n  Ctrl+C stops the arena, kills every writer and deletes its temp stores.\n", flush=True)
        if open_browser:
            threading.Timer(0.5, lambda: webbrowser.open(host_url)).start()
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping: killing writers, deleting temp stores")
    finally:
        arena.shutdown()
        httpd.server_close()
        httpd.RequestHandlerClass.state.close_store()  # type: ignore[attr-defined]
    return 0
