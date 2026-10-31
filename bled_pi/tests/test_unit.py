"""Unit tests: protocol, maths, optics.   Run:  python3 -m unittest discover -s tests -v"""
import os
import random
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bled_unit import calc, protocol as proto
from bled_unit.config import Config
from bled_unit.fake_hardware import FakeHardware
from bled_unit.sensor import OpticalError, OpticalReader


class ProtocolTests(unittest.TestCase):
    def test_crc_check_value(self):
        self.assertEqual(proto.crc16(b"123456789"), 0x29B1)          # CRC-16/CCITT-FALSE standard check value

    def test_frame_matches_display_example(self):
        self.assertEqual(proto.frame("PAD,3,41.2"), b"$PAD,3,41.2*33E9\r\n")   # example from BLED_LINK_PROTOCOL.md

    def test_parser_roundtrip_and_noise(self):
        p = proto.Parser()
        data = b"garbage\x00\xff" + proto.frame("CASE,A-1,13.2") + b"noise" + proto.frame("ENDC")
        msgs = p.feed(data)
        self.assertEqual([(m.type, m.args) for m in msgs], [("CASE", ["A-1", "13.2"]), ("ENDC", [])])

    def test_parser_split_across_chunks(self):
        p, out = proto.Parser(), []
        for b in proto.frame("BASE"):
            out += p.feed(bytes([b]))                                 # one byte at a time
        self.assertEqual(len(out), 1)

    def test_bad_crc_and_lowercase_rejected(self):
        p = proto.Parser()
        bad = bytearray(proto.frame("PAD,1,5.0")); bad[6] ^= 1
        self.assertEqual(p.feed(bytes(bad)), [])
        self.assertEqual(p.crc_errors, 1)
        lower = proto.frame("PAD,1,5.0").decode().lower().replace("pad", "PAD").replace("$", "$").encode()
        if lower != proto.frame("PAD,1,5.0"):                         # only meaningful when the hex has letters
            self.assertEqual(p.feed(lower), [])

    def test_fuzz_no_corrupted_frame_accepted(self):
        rnd, p, escaped = random.Random(1), proto.Parser(), 0
        good = proto.frame("PAD,12,41.2")
        for _ in range(20000):
            f = bytearray(good)
            for _k in range(rnd.choice([1, 2, 3])):                   # flip 1-3 random bits
                i = rnd.randrange(len(f) - 2)                         # keep the final \r\n so the frame ends
                f[i] ^= 1 << rnd.randrange(8)
            for m in p.feed(bytes(f) + b"\n"):
                if (m.type, m.args) != ("PAD", ["12", "41.2"]):
                    escaped += 1
        self.assertEqual(escaped, 0)

    def test_builders(self):
        self.assertEqual(proto.stat(2, 1, 0, 87), "STAT,2,1,0,87")
        self.assertEqual(proto.bdon(True, 48213, 51877), "BDON,1,48213,51877")
        self.assertEqual(proto.pad(3, 41.23), "PAD,3,41.2")
        self.assertEqual(proto.bprg(140), "BPRG,100")
        self.assertEqual(proto.err(4), "ERR,4")


class CalcTests(unittest.TestCase):
    def test_absorbance(self):
        self.assertAlmostEqual(calc.absorbance(1000, 100), 1.0)       # 10x dimmer = A of 1

    def test_pad_volume_scales_with_patient_hb(self):
        cfg = Config()
        a = calc.pad_volume(20000, 10000, 20000, 20000, 13.0, cfg)    # A540 = 0.301
        b = calc.pad_volume(20000, 10000, 20000, 20000, 6.5, cfg)     # same light, patient has half the Hb
        self.assertAlmostEqual(b.ebl_ml, 2 * a.ebl_ml, places=6)      # -> twice the blood volume

    def test_turbidity_is_subtracted(self):
        cfg = Config()
        clean = calc.pad_volume(20000, 10000, 20000, 20000, 13.0, cfg)
        dirty = calc.pad_volume(20000, 10000, 20000, 15000, 13.0, cfg)   # IR also dimmer: turbid sample
        self.assertLess(dirty.ebl_ml, clean.ebl_ml)

    def test_negative_clamped_and_flags(self):
        cfg = Config()
        r = calc.pad_volume(20000, 20000, 20000, 20000, 13.0, cfg)    # no absorbance at all
        self.assertEqual(r.ebl_ml, 0.0)
        self.assertIn("NEG_CLAMPED", r.flags)
        self.assertIn("UNCALIBRATED", r.flags)


class SensorTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config().fast()
        self.hw = FakeHardware(self.cfg)
        self.reader = OpticalReader(self.hw, self.cfg, threading.Event())

    def test_blank_reading_is_sane(self):
        r = self.reader.read_set("blank")
        self.assertAlmostEqual(r.net540, 20000, delta=200)
        self.assertAlmostEqual(r.net850, 24000, delta=200)
        self.assertAlmostEqual(r.dark, 120, delta=10)

    def test_leds_end_off_and_never_two_at_once(self):
        self.reader.read_set("blank")
        self.assertIsNone(self.hw.led_log[-1])                         # last action: LEDs off
        self.assertTrue(all(x in (None, "540", "IR") for x in self.hw.led_log))

    def test_saturation_is_over_range(self):
        self.hw.saturate = True
        with self.assertRaises(OpticalError) as cm:
            self.reader.read_set("blank")
        self.assertEqual(cm.exception.code, 4)

    def test_dead_led_is_sensor_fault(self):
        self.hw.full_540 = 100                                         # almost no light
        with self.assertRaises(OpticalError) as cm:
            self.reader.read_set("blank")
        self.assertEqual(cm.exception.code, 2)

    def test_dark_sample_is_over_range(self):
        self.hw.full_540 = 20000
        self.hw.load_pad(3.0)                                          # A = 3 -> far too dark
        self.hw._motor_seconds = 10                                    # pretend it is fully mixed
        with self.assertRaises(OpticalError) as cm:
            self.reader.read_set("sample")
        self.assertEqual(cm.exception.code, 4)

    def test_adc_failure_raises_oserror(self):
        self.hw.adc_broken = True
        with self.assertRaises(OSError):
            self.reader.read_set("blank")

    def test_noise_rejected(self):
        self.hw.noise = 3000.0                                         # terrible noise
        with self.assertRaises(OpticalError):
            self.reader.read_set("blank")

    def test_cancel_stops_quickly(self):
        cancel = threading.Event()
        cancel.set()
        reader = OpticalReader(self.hw, self.cfg, cancel)
        from bled_unit.sensor import Cancelled
        with self.assertRaises(Cancelled):
            reader.read_set("blank")
        self.assertIsNone(self.hw.led_log[-1] if self.hw.led_log else None)


if __name__ == "__main__":
    unittest.main()
