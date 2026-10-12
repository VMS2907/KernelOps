"""Run the real terminal demo and save its exact output for the video.

The terminal scene in the video is typeset from this JSON: real commands,
real output, and the real bytes of the file a killed process left behind.
Nothing in it is invented.

    python video/capture_cli.py video/build/cli.json
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(cwd, *args):
    env = dict(os.environ, PYTHONPATH=ROOT, NO_COLOR="1", PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-m", "safewrite", *args], cwd=cwd, env=env,
                       capture_output=True, encoding="utf-8")
    return p.returncode, (p.stdout + p.stderr).rstrip("\n")


def main(out):
    tmp = tempfile.mkdtemp(prefix="sw-video-")
    try:
        shutil.copytree(os.path.join(ROOT, "examples"), os.path.join(tmp, "examples"))
        steps = []
        for args in (["init", "shop", "--engine", "naive"],
                     ["put", "shop", "inventory.csv", "--file", "examples/inventory_A.csv"],
                     ["crash", "shop", "inventory.csv", "--file", "examples/inventory_B.csv", "--at", "2"]):
            code, text = run(tmp, *args)
            text = text.replace(tmp + os.sep, "").replace(tmp, ".")
            steps.append({"cmd": "python -m safewrite " + " ".join(args), "exit": code, "out": text})
        with open(os.path.join(tmp, "shop", "docs", "inventory.csv"), encoding="utf-8") as f:
            broken = f.read().splitlines()
        first_a = next(i for i, line in enumerate(broken) if line.startswith("VERSION A"))
        data = {"steps": steps, "file_lines": broken, "first_a_line": first_a + 1,
                "lines_b": sum(1 for l in broken if l.startswith("VERSION B")),
                "lines_a": sum(1 for l in broken if l.startswith("VERSION A"))}
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        print(f"captured {len(steps)} commands; broken file switches to VERSION A at line {first_a + 1}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "video", "build", "cli.json"))
