"""Build the SafeWrite launch film end to end.

    python video/build.py                   # everything: capture, render, score, mux
    python video/build.py --skip-capture    # reuse video/build/cli.json + footage
    python video/build.py --audio-only      # re-score and re-mux onto the rendered picture
    python video/build.py --vo voice.wav    # mix a recorded voiceover in (music ducks under it)

Needs Node with Playwright (Chromium) and ffmpeg on PATH. Output: video/build/SafeWrite_launch.mp4
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIDEO = os.path.join(ROOT, "video")
BUILD = os.path.join(VIDEO, "build")


def node_env():
    env = dict(os.environ)
    try:
        groot = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
        if groot:
            env["NODE_PATH"] = os.pathsep.join(p for p in (env.get("NODE_PATH"), groot) if p)
    except FileNotFoundError:
        pass
    return env


def run(*cmd, **kw):
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def capture():
    run(sys.executable, os.path.join(VIDEO, "capture_cli.py"), os.path.join(BUILD, "cli.json"))
    store = tempfile.mkdtemp(prefix="sw-film-store-")
    port = free_port()
    server = subprocess.Popen([sys.executable, "-m", "safewrite", "serve", "--port", str(port), "--no-browser",
                               "--store", store], cwd=ROOT)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        run("node", os.path.join(VIDEO, "capture_footage.js"), base, os.path.join(BUILD, "footage"), env=node_env())
    finally:
        server.terminate()
        server.wait(10)
        shutil.rmtree(store, ignore_errors=True)


def mux(vo=None):
    picture = os.path.join(BUILD, "film_video.mp4")
    score = os.path.join(BUILD, "score.wav")
    out = os.path.join(BUILD, "SafeWrite_launch.mp4")
    if vo:
        audio = ["-i", score, "-i", vo, "-filter_complex",
                 "[1:a]aresample=48000,pan=stereo|c0=c0|c1=c0,asplit=2[vo][key];"
                 "[0:a][key]sidechaincompress=threshold=0.02:ratio=8:attack=15:release=400[bed];"
                 "[bed][vo]amix=inputs=2:normalize=0,alimiter=limit=0.84[a]", "-map", "0:v", "-map", "[a]"]
        audio = ["-i", picture] + audio
        run("ffmpeg", "-y", "-loglevel", "error", *audio, "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
            "-shortest", "-movflags", "+faststart", out)
    else:
        run("ffmpeg", "-y", "-loglevel", "error", "-i", picture, "-i", score, "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-shortest", "-movflags", "+faststart", out)
    # a lighter copy for chat apps and slides
    run("ffmpeg", "-y", "-loglevel", "error", "-i", out, "-c:v", "libx264", "-preset", "slow", "-crf", "23",
        "-c:a", "copy", "-movflags", "+faststart", os.path.join(BUILD, "SafeWrite_launch_web.mp4"))
    print("wrote", out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-capture", action="store_true")
    ap.add_argument("--audio-only", action="store_true")
    ap.add_argument("--vo", help="voiceover recording (wav/m4a/mp3), timed to docs/VIDEO_PLAN.md")
    a = ap.parse_args()
    os.makedirs(BUILD, exist_ok=True)
    if not (a.skip_capture or a.audio_only or a.vo):
        capture()
    if not (a.audio_only or a.vo):
        run("node", os.path.join(VIDEO, "render.js"), env=node_env())
    run(sys.executable, os.path.join(VIDEO, "score.py"))
    mux(a.vo)


if __name__ == "__main__":
    main()
