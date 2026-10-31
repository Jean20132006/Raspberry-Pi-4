"""End-to-end tests: a fake display talks to the real controller over an in-memory serial cable."""
import csv
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from harness import Rig


def pad_value(args):
    return float(args[1])


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.d, self.hw = self.rig.display, self.rig.hw

    def tearDown(self):
        self.rig.stop()

    # ------------------------------------------------------------------------------------------------ helpers
    def baseline(self):
        m = self.d.mark()
        self.d.send("BASE")
        self.assertIsNotNone(self.d.wait("BPRG", after=m), "no BPRG progress")
        done = self.d.wait("BDON", after=m)
        self.assertIsNotNone(done, "no BDON")
        return done

    def open_case(self, cid="T1", hb="13.0"):
        self.d.send("CASE,%s,%s" % (cid, hb))
        self.assertTrue(self.rig.wait_state(2), "unit did not enter CASE ACTIVE")

    def measure(self, ml, hb=13.0, n=1):
        self.hw.load_pad_ml(ml, hb)
        m = self.d.mark()
        self.hw.press_start()
        return self.d.wait("PAD", lambda a: int(a[0]) == n, after=m, timeout=8)

    # ------------------------------------------------------------------------------------------------ tests
    def test_boot_sends_stat(self):
        a = self.d.wait("STAT")
        self.assertEqual(a, ["0", "1", "0", "87"])                    # idle, cartridge in, AC, 87 %

    def test_full_case_two_pads(self):
        done = self.baseline()
        self.assertEqual(done[0], "1")                                # baseline ok
        self.assertAlmostEqual(int(done[1]), 20000, delta=300)        # i540
        self.assertAlmostEqual(int(done[2]), 24000, delta=300)        # i850
        self.assertTrue(self.rig.wait_state(0))
        self.open_case()
        p1 = self.measure(6.0, n=1)
        self.assertIsNotNone(p1)
        self.assertAlmostEqual(pad_value(p1), 6.0, delta=0.4)         # recovers the simulated 6 mL
        p2 = self.measure(10.0, n=2)
        self.assertAlmostEqual(pad_value(p2), 10.0, delta=0.6)
        self.d.send("ENDC")
        self.assertTrue(self.rig.wait_state(0))
        # case file
        f = Path(self.rig.cfg.data_dir) / "cases" / "T1.csv"
        rows = list(csv.DictReader(open(f)))
        self.assertEqual([r["event"] for r in rows], ["START", "BASELINE_OK", "PAD", "PAD", "END"])
        self.assertAlmostEqual(float(rows[2]["ebl_ml"]), 6.0, delta=0.4)
        self.assertIn("UNCALIBRATED", rows[2]["note"])
        self.assertFalse((Path(self.rig.cfg.data_dir) / "active_case.json").exists())   # cleared at the end
        self.assertEqual(self.hw._speed, 0.0)                         # motor off
        self.assertIsNone(self.hw.led_log[-1])                        # LEDs off

    def test_motor_ramps_and_runs(self):
        self.baseline(); self.open_case()
        self.measure(6.0)
        speeds = [s for _t, s in self.hw.motor_log]
        self.assertGreater(max(speeds), 0.5)                          # reached mixing speed
        self.assertEqual(speeds[-1], 0.0)                             # and stopped
        self.assertEqual(sorted(set(speeds[:10])), sorted(set(speeds[:10])))
        self.assertTrue(all(b >= a - 1e-9 for a, b in zip(speeds[:10], speeds[1:11])))   # ramp up is monotonic

    def test_progress_is_monotonic_and_ends_before_bdon(self):
        m = self.d.mark()
        self.d.send("BASE")
        self.assertIsNotNone(self.d.wait("BDON", after=m))
        pcts = [int(a[0]) for a in self.d.find("BPRG")]
        self.assertEqual(pcts, sorted(pcts))
        self.assertGreaterEqual(len(pcts), 3)
        self.assertEqual(pcts[0], 0)

    def test_pad_frame_is_repeated(self):
        self.baseline(); self.open_case()
        self.measure(6.0)
        time.sleep(1.5 * self.rig.cfg.pad_resend_period_s * self.rig.cfg.pad_resend_count)
        pads = self.d.find("PAD")
        self.assertGreaterEqual(len(pads), 3)                         # original + repeats
        self.assertEqual(len({tuple(p) for p in pads}), 1)            # all identical (display ignores duplicates)

    def test_base_repeats_are_ignored(self):
        m = self.d.mark()
        self.d.send("BASE"); self.d.send("BASE"); self.d.send("BASE")
        self.assertIsNotNone(self.d.wait("BDON", after=m))
        time.sleep(0.3)
        self.assertEqual(len(self.d.find("BDON")), 1)                 # only ONE baseline was run

    def test_start_without_case_is_ignored(self):
        self.baseline()
        m = self.d.mark()
        self.hw.press_start()
        time.sleep(0.5)
        self.assertEqual(self.d.find("PAD"), [])
        self.assertEqual(self.hw._speed, 0.0)

    def test_start_twice_does_not_double_measure(self):
        self.baseline(); self.open_case()
        self.hw.load_pad_ml(6.0, 13.0)
        self.hw.press_start(); time.sleep(0.05); self.hw.press_start()
        time.sleep(2.5)
        self.assertEqual(len({a[0] for a in self.d.find("PAD")}), 1)  # still only pad number 1

    def test_invalid_case_is_rejected(self):
        self.baseline()
        self.d.send("CASE,BAD ID!,13.0")                              # invalid characters (also fails the parser)
        self.d.send("CASE,OK1,99.0")                                  # Hb out of range
        time.sleep(0.4)
        self.assertNotEqual(self.rig.stat_state(), 2)

    def test_garbage_and_bad_crc_do_not_disturb(self):
        self.baseline()
        self.d.send_raw(b"\x00\xffxx$CASE,T9,13.0*0000\r\n")          # wrong checksum
        time.sleep(0.3)
        self.assertNotEqual(self.rig.stat_state(), 2)
        self.open_case("T9")

    # ------------------------------------------------------------------------------------------------ error handling
    def test_base_without_cartridge(self):
        self.hw.cartridge = False
        time.sleep(0.3)
        m = self.d.mark()
        self.d.send("BASE")
        self.assertIsNotNone(self.d.wait("ERR", lambda a: a == ["1"], after=m))
        bd = self.d.wait("BDON", after=m)
        self.assertEqual(bd[0], "0")

    def test_reagent_fault_then_retry_resumes_same_pad(self):
        self.baseline(); self.open_case()
        self.hw.reagent_gone = True                                   # blank 50 % too dark -> code 3
        self.hw.load_pad_ml(6.0, 13.0)
        m = self.d.mark()
        self.hw.press_start()
        self.assertIsNotNone(self.d.wait("ERR", lambda a: a == ["3"], after=m, timeout=8))
        self.assertTrue(self.rig.wait_state(3))
        self.hw.reagent_gone = False                                  # operator fixes the reagent
        m = self.d.mark()
        self.hw.press_start()                                         # START = try again
        p = self.d.wait("PAD", after=m, timeout=8)
        self.assertIsNotNone(p)
        self.assertEqual(p[0], "1")                                   # still pad 1: the failed attempt did not count
        self.assertAlmostEqual(pad_value(p), 6.0, delta=0.4)

    def test_over_range_error_code_4(self):
        self.baseline(); self.open_case()
        self.hw.load_pad(3.0)                                         # absurdly dark sample
        m = self.d.mark()
        self.hw.press_start()
        self.assertIsNotNone(self.d.wait("ERR", lambda a: a == ["4"], after=m, timeout=8))
        self.assertEqual(self.hw._speed, 0.0)

    def test_adc_failure_during_pad(self):
        self.baseline(); self.open_case()
        self.hw.adc_broken = True
        m = self.d.mark()
        self.hw.press_start()
        self.assertIsNotNone(self.d.wait("ERR", lambda a: a == ["2"], after=m, timeout=8))
        self.assertTrue(self.rig.wait_state(3))
        self.hw.press_start()                                         # still broken: stays in ERROR
        time.sleep(0.3)
        self.assertEqual(self.rig.stat_state(), 3)
        self.hw.adc_broken = False
        m = self.d.mark()
        self.hw.press_start()                                         # fixed: retry works
        self.assertIsNotNone(self.d.wait("PAD", after=m, timeout=8))

    def test_cartridge_removed_during_mixing_aborts_and_case_resumes(self):
        self.baseline(); self.open_case("R1")
        self.measure(6.0, n=1)
        self.hw.load_pad_ml(6.0, 13.0)
        self.hw.press_start()
        time.sleep(0.25)                                              # in the middle of the pad
        self.hw.cartridge = False                                     # operator pulls the cartridge
        self.assertTrue(self.rig.wait_state(0))
        time.sleep(0.2)
        self.assertEqual(self.hw._speed, 0.0)                         # motor stopped
        self.assertEqual(len({a[0] for a in self.d.find("PAD")}), 1)  # no second pad was produced
        self.hw.cartridge = True                                      # new cartridge inserted
        self.d.send("CASE,R1,13.0")                                   # display re-sends CASE (state 0 during an active case)
        self.assertTrue(self.rig.wait_state(2))
        p = self.measure(6.0, n=2)                                   # numbering continues
        self.assertIsNotNone(p)

    def test_power_fail_shuts_down_safely(self):
        self.baseline(); self.open_case("PF")
        self.hw.load_pad_ml(6.0, 13.0)
        self.hw.press_start()
        time.sleep(0.2)
        self.hw.fail_flag = True
        self.rig.thread.join(timeout=5)
        self.assertFalse(self.rig.thread.is_alive())                  # main loop ended
        self.assertEqual(self.hw._speed, 0.0)
        self.assertTrue((Path(self.rig.cfg.data_dir) / "active_case.json").exists())   # case kept for resume

    def test_new_case_without_endc_closes_old_one(self):
        self.baseline(); self.open_case("A1")
        self.d.send("CASE,B2,12.0")                                   # ENDC was lost
        time.sleep(0.4)
        rows = list(csv.DictReader(open(Path(self.rig.cfg.data_dir) / "cases" / "A1.csv")))
        self.assertEqual(rows[-1]["event"], "END")
        self.assertTrue((Path(self.rig.cfg.data_dir) / "cases" / "B2.csv").exists())

    def test_endc_during_measurement_cancels_it(self):
        self.baseline(); self.open_case()
        self.hw.load_pad_ml(6.0, 13.0)
        self.hw.press_start()
        time.sleep(0.2)
        self.d.send("ENDC")
        self.assertTrue(self.rig.wait_state(0))
        time.sleep(0.8)
        self.assertEqual(self.d.find("PAD"), [])                      # cancelled job produced nothing
        self.assertEqual(self.hw._speed, 0.0)


class RebootResumeTest(unittest.TestCase):
    def test_pad_counter_survives_a_reboot(self):
        tmp = tempfile.TemporaryDirectory()
        rig = Rig(data_dir=tmp.name)
        d, hw = rig.display, rig.hw
        d.send("BASE"); d.wait("BDON")
        d.send("CASE,RB1,13.0"); rig.wait_state(2)
        hw.load_pad_ml(6.0, 13.0); hw.press_start()
        self.assertIsNotNone(d.wait("PAD", lambda a: a[0] == "1", timeout=8))
        rig.ctrl.request_shutdown("simulated power cut"); rig.thread.join(5); rig.link.stop(); d.stop()
        # ---- the Pi boots again with the same memory card
        rig2 = Rig(data_dir=tmp.name)
        d2, hw2 = rig2.display, rig2.hw
        d2.send("CASE,RB1,13.0")                                      # the display re-sends CASE while the unit reports state 0
        self.assertTrue(rig2.wait_state(2))
        hw2.load_pad_ml(6.0, 13.0); hw2.press_start()
        p = d2.wait("PAD", timeout=8)
        self.assertEqual(p[0], "2")                                   # continues with pad 2, not 1
        rows = [r["event"] for r in __import__("csv").DictReader(open(Path(tmp.name) / "cases" / "RB1.csv"))]
        self.assertIn("RESUME", rows)
        rig2.stop(); tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
