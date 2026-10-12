"""The proof: every crash state of every safe engine recovers correctly, and
every removed safeguard is caught by at least one counter-example."""
import os
import shutil
import tempfile
import unittest

from safewrite.crashsim import DEFAULT_SCENARIOS, MODELS, explore
from safewrite.crashsim.explorer import crash_points, get_recording, recover_and_judge, window
from safewrite.crashsim.model import build_image, enumerate_states
from safewrite.engines import ENGINES, all_specs, make, spec_info
from safewrite.vfs import OsVFS

SAFE = ["shadow", "undo", "wal", "cow"]


class SafeEnginesNeverViolate(unittest.TestCase):
    def test_all_safe_engines_all_scenarios_all_models(self):
        total = 0
        for spec in SAFE:
            for sc in DEFAULT_SCENARIOS:
                for model in MODELS:
                    with self.subTest(engine=spec, scenario=sc, model=model):
                        r = explore(spec, sc, model)
                        total += r["states"] + r["recovery_crash_states"]
                        self.assertEqual(r["record_error"], "")
                        self.assertGreater(r["states"], 0)
                        if spec == "shadow" and sc == "transfer":
                            # documented: shadow is atomic per document only
                            self.assertEqual(r["verdict"], "LIMITATION", r["examples"])
                            continue
                        self.assertEqual(r["violations"], 0, r["examples"])
                        self.assertEqual(r["recovery_violations"], 0, r["examples"])
                        self.assertEqual(r["verdict"], "PASS")
        self.assertGreater(total, 8000)

    def test_crashes_during_recovery_are_explored(self):
        for spec in ("undo", "wal"):
            r = explore(spec, "update", "power")
            self.assertGreater(r["recovery_crash_states"], 0)


class EveryAblationIsCaught(unittest.TestCase):
    def test_each_removed_safeguard_has_a_counter_example(self):
        for spec in all_specs():
            if spec_info(spec)["expected_safe"]:
                continue
            with self.subTest(ablation=spec):
                found = []
                for sc in DEFAULT_SCENARIOS:
                    for model in ("power", "paranoid"):
                        r = explore(spec, sc, model, recovery_crashes=False)
                        if r["violations"]:
                            found.append((sc, model, r["examples"][0]["code"]))
                self.assertTrue(found, f"{spec}: the checker found no counter-example")

    def test_power_loss_bugs_are_invisible_to_kill9_testing(self):
        # The point of the power model: missing-fsync bugs pass process-crash tests.
        for spec in ("shadow/no-fsync", "shadow/no-dirsync", "wal/early-ack", "cow/no-barrier"):
            with self.subTest(ablation=spec):
                proc = explore(spec, "update", "process", recovery_crashes=False)
                power = explore(spec, "update", "power", recovery_crashes=False)
                self.assertEqual(proc["violations"], 0)
                self.assertGreater(power["violations"], 0)

    def test_naive_corrupts_even_under_kill9(self):
        r = explore("naive", "update", "process", recovery_crashes=False)
        self.assertIn("CORRUPT", r["outcomes"])


class SimulatedImagesMatchRealDisk(unittest.TestCase):
    """Materialise crash images as real directories and recover them with real
    system calls: the outcome must match the in-memory replay."""

    def test_real_disk_replay(self):
        tmp = tempfile.mkdtemp()
        try:
            for spec in ("shadow", "undo", "wal", "cow", "naive"):
                rec = get_recording(spec, "update")
                cls = ENGINES[spec.split("/")[0]]
                for point in crash_points(rec.trace)[::2]:
                    states, _ = enumerate_states(rec.trace, rec.base, point, MODELS["power"])
                    a, b = window(rec.trace, point)
                    for st in states[:6]:
                        img = build_image(rec.trace, rec.base, st)
                        mem_out, _, _ = recover_and_judge(rec, img.files, img.dirs, a, b, check_idempotent=False)
                        d = os.path.join(tmp, f"{spec}-{point}-{id(st)}")
                        os.makedirs(d)
                        for sub in img.dirs:
                            os.makedirs(os.path.join(d, sub), exist_ok=True)
                        for p, data in img.files.items():
                            os.makedirs(os.path.dirname(os.path.join(d, p)), exist_ok=True)
                            with open(os.path.join(d, p), "wb") as f:
                                f.write(data)
                        eng = cls(OsVFS(d), **rec.options)
                        try:
                            eng.open()
                            got = {k: eng.get(k) for k in rec.scenario.keys}
                        except Exception as e:  # noqa: BLE001
                            got = repr(type(e))
                        finally:
                            eng.close()       # Windows cannot delete files that are still open
                        if mem_out.state is not None and mem_out.code in ("OK", "CORRUPT", "LOST"):
                            self.assertEqual(got, mem_out.state, f"{spec} point {point}")
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
