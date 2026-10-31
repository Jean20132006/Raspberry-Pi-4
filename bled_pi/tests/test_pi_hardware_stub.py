"""
Checks the REAL PiHardware class without a Raspberry Pi, by replacing gpiozero and smbus2 with tiny stand-ins.
It cannot prove the wiring works, but it proves the code uses the libraries correctly and the logic is right
(ADC byte decoding, one LED at a time, motor enable, input polarity, battery gauge).
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PINS = {}                                                # pin number -> the stand-in device created for it


class _Dev:
    def __init__(self, pin, *a, **kw):
        self.pin, self.kw, self.value, self.closed = pin, kw, kw.get("initial_value", 0), False
        self.is_active = False
        self.when_pressed = None
        PINS[pin] = self

    def on(self): self.value = 1
    def off(self): self.value = 0
    def close(self): self.closed = True


class _Bus:
    def __init__(self, n):
        self.regs, self.written, self.adc = {}, [], 0
        self.fail = False

    def write_i2c_block_data(self, addr, reg, data):
        if self.fail: raise OSError("no ack")
        self.written.append((addr, reg, list(data)))

    def read_i2c_block_data(self, addr, reg, n):
        if self.fail: raise OSError("no ack")
        if addr == 0x36: return [73, 0]                  # fuel gauge: 73 %
        v = self.adc & 0xFFFF
        return [v >> 8, v & 0xFF]

    def close(self): pass


def install_stubs():
    gz = types.ModuleType("gpiozero")
    gz.Button = type("Button", (_Dev,), {})
    gz.DigitalInputDevice = type("DigitalInputDevice", (_Dev,), {})
    gz.DigitalOutputDevice = type("DigitalOutputDevice", (_Dev,), {})
    gz.PWMOutputDevice = type("PWMOutputDevice", (_Dev,), {})
    sm = types.ModuleType("smbus2")
    sm.SMBus = _Bus
    sys.modules["gpiozero"], sys.modules["smbus2"] = gz, sm


class PiHardwareStubTests(unittest.TestCase):
    def setUp(self):
        PINS.clear()
        install_stubs()
        from bled_unit.config import Config
        from bled_unit.hardware import PiHardware
        self.cfg = Config()
        self.hw = PiHardware(self.cfg)

    def test_adc_configured_and_decoded(self):
        self.assertEqual(self.hw._bus.written[0], (0x48, 0x01, [0x44, 0x83]))     # continuous, AIN0, +-2.048 V, 128 SPS
        self.hw._bus.adc = 12345
        self.assertEqual(self.hw.read_adc(), 12345)
        self.hw._bus.adc = 0xFFFE                                                 # negative number in two's complement
        self.assertEqual(self.hw.read_adc(), -2)

    def test_self_test_and_i2c_failure(self):
        self.assertTrue(self.hw.self_test())
        self.hw._bus.fail = True
        self.assertFalse(self.hw.self_test())
        with self.assertRaises(OSError):
            self.hw.read_adc()

    def test_only_one_led_on(self):
        c = self.cfg
        self.hw.led_on("540")
        self.assertEqual((PINS[c.pin_led_540].value, PINS[c.pin_led_ir].value), (1, 0))
        self.hw.led_on("IR")
        self.assertEqual((PINS[c.pin_led_540].value, PINS[c.pin_led_ir].value), (0, 1))
        self.hw.leds_off()
        self.assertEqual((PINS[c.pin_led_540].value, PINS[c.pin_led_ir].value), (0, 0))
        with self.assertRaises(ValueError):
            self.hw.led_on("UV")

    def test_motor_enable_and_pwm(self):
        c = self.cfg
        self.hw.motor_set(0.6)
        self.assertEqual((PINS[c.pin_motor_en if hasattr(c, 'pin_motor_en') else c.pin_motor_enable].value, PINS[c.pin_motor_pwm].value), (1, 0.6))
        self.hw.motor_set(5.0)                                                    # clamped to 1.0
        self.assertEqual(PINS[c.pin_motor_pwm].value, 1.0)
        self.hw.motor_set(0)
        self.assertEqual((PINS[c.pin_motor_enable].value, PINS[c.pin_motor_pwm].value), (0, 0))

    def test_inputs_and_button(self):
        c = self.cfg
        PINS[c.pin_cartridge].is_active = True
        self.assertTrue(self.hw.cartridge_present())
        PINS[c.pin_ac_present].is_active = False
        self.assertTrue(self.hw.on_battery())
        PINS[c.pin_power_fail].is_active = True
        self.assertTrue(self.hw.power_fail())
        hits = []
        self.hw.set_start_callback(lambda: hits.append(1))
        PINS[c.pin_start_button].when_pressed()
        self.assertEqual(hits, [1])
        self.assertEqual(PINS[c.pin_start_button].kw["pull_up"], True)

    def test_battery_gauge_optional(self):
        self.assertEqual(self.hw.battery_percent(), -1)                           # not fitted by default
        self.hw._cfg.has_fuel_gauge = True
        self.assertEqual(self.hw.battery_percent(), 73)

    def test_close_releases_and_turns_off(self):
        self.hw.led_on("540"); self.hw.motor_set(0.5)
        self.hw.close()
        c = self.cfg
        self.assertEqual(PINS[c.pin_led_540].value, 0)
        self.assertTrue(PINS[c.pin_motor_pwm].closed)


if __name__ == "__main__":
    unittest.main()
