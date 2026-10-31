"""
fake_hardware.py - a pretend Raspberry Pi + optics, so the whole program can run on a laptop.

It simulates the physics well enough to test everything:
  * LEDs on  -> the fake photodiode returns a big number; LEDs off -> only a small dark offset.
  * A pad is "loaded" with a known blood volume.  Once the motor has run long enough the solution is "mixed"
    and the light at 540 nm drops according to Beer-Lambert (the IR light drops only by the turbidity).
  * You can inject failures (ADC not answering, saturated ADC, cartridge removed, ...) to test the error logic.
"""
from __future__ import annotations

import random                                            # measurement noise
import threading                                         # the start callback is called like a real GPIO thread
import time                                              # clock for the mixing simulation
from typing import Callable, Optional

from .config import Config
from .hardware import Hardware


class FakeHardware(Hardware):
    """Drop-in replacement for PiHardware."""

    def __init__(self, cfg: Config, noise: float = 4.0) -> None:
        self.cfg = cfg                                   # settings (we need the calibration to invent realistic light levels)
        self.noise = noise                               # standard deviation of the fake ADC noise, in counts
        self.dark_counts = 120                           # photodiode reading with all LEDs off
        self.full_540 = 20000                            # light level (counts) of the clear reagent at 540 nm
        self.full_ir = 24000                             # light level (counts) of the clear reagent at IR
        self.cartridge = True                            # cartridge inserted
        self.battery_on = False                          # running from AC
        self.battery = 87                                # battery percent
        self.fail_flag = False                           # power-fail warning
        self.adc_broken = False                          # make read_adc raise OSError
        self.saturate = False                            # make read_adc return a saturated value
        self.reagent_gone = False                        # simulates an empty reagent chamber (very different blank)
        self.turbidity_a = 0.02                          # absorbance of bubbles/fibres (same at both wavelengths)
        self.pad_net_a = 0.0                             # net absorbance the loaded pad will produce once mixed
        self.mix_needed_s = 0.10                         # motor run time after which the pad is fully mixed
        self._led = None                                 # which LED is on: None, "540" or "IR"
        self._speed = 0.0                                # motor speed now
        self._t_speed = time.monotonic()                 # when the speed last changed
        self._motor_seconds = 0.0                        # accumulated motor run time for the current pad
        self._cb: Optional[Callable[[], None]] = None    # start-button callback
        self.motor_log: list = []                        # (time, speed) history so tests can verify the motor was used
        self.led_log: list = []                          # history of LED states so tests can verify "one LED at a time"

    # ------------------------------------------------------------------------------- scenario controls (tests / simulator)
    def load_pad(self, net_absorbance: float) -> None:
        """Pretend a new gauze pad with this net absorbance was inserted (unmixed)."""
        self._motor_seconds = 0.0                        # a fresh pad has not been mixed yet
        self.pad_net_a = net_absorbance                  # remember what it will measure once mixed

    def load_pad_ml(self, ml: float, hb_patient: float) -> None:
        """Pretend a pad holding `ml` mL of blood of a patient with `hb_patient` g/dL was inserted."""
        c = self.cfg                                     # shortcut
        ml_at_ref = ml * hb_patient / c.cal_hb_ref       # inverse of the volume formula in calc.pad_volume
        net_a = (ml_at_ref - c.cal_intercept_ml) / c.cal_slope_ml_per_a   # inverse of the linear calibration
        self.load_pad(net_a)                             # same as load_pad but described in mL

    def press_start(self) -> None:
        """Simulate a press of the START button (calls the registered callback in a new thread, like gpiozero)."""
        if self._cb is not None:
            threading.Thread(target=self._cb, daemon=True).start()

    # ------------------------------------------------------------------------------- Hardware interface
    def set_start_callback(self, callback: Callable[[], None]) -> None:
        self._cb = callback                              # store it

    def led_on(self, wavelength: str) -> None:
        self._led = wavelength                           # only one LED can be on in this model by construction
        self.led_log.append(wavelength)                  # record it

    def leds_off(self) -> None:
        self._led = None
        self.led_log.append(None)

    def _account_motor(self) -> None:
        """Add the time spent at the previous speed to the mixing total."""
        now = time.monotonic()                           # current time
        if self._speed > 0:                              # the motor was turning since the last change
            self._motor_seconds += (now - self._t_speed) * self._speed   # slower speed mixes less
        self._t_speed = now                              # restart the stopwatch

    def motor_set(self, speed: float) -> None:
        self._account_motor()                            # close the previous interval
        self._speed = max(0.0, min(1.0, speed))          # new speed
        self.motor_log.append((time.monotonic(), self._speed))

    def motor_stop(self) -> None:
        self.motor_set(0.0)                              # same as speed zero

    def read_adc(self) -> int:
        if self.adc_broken:                              # simulate a loose I2C wire
            raise OSError("fake I2C error")
        if self.saturate:                                # simulate too much light
            return 32767
        self._account_motor()                            # keep the mixing clock current while the motor runs
        mixed = min(1.0, self._motor_seconds / self.mix_needed_s)   # 0 = not mixed, 1 = fully mixed
        value = float(self.dark_counts)                  # start from the electronic offset
        if self._led == "540":                           # green LED on
            a = self.turbidity_a * mixed + self.pad_net_a * mixed   # absorbance grows while mixing
            level = self.full_540 * (0.5 if self.reagent_gone else 1.0)   # an empty chamber changes the blank a lot
            value += level * (10 ** (-a))                # Beer-Lambert: I = I0 * 10^-A
        elif self._led == "IR":                          # infrared LED on
            a = self.turbidity_a * mixed                 # only turbidity affects the IR light
            level = self.full_ir * (0.5 if self.reagent_gone else 1.0)
            value += level * (10 ** (-a))
        value += random.gauss(0.0, self.noise)           # measurement noise
        return int(max(0, min(32767, value)))            # a real ADC returns whole numbers within its range

    def cartridge_present(self) -> bool:
        return self.cartridge

    def on_battery(self) -> bool:
        return self.battery_on

    def battery_percent(self) -> int:
        return self.battery

    def power_fail(self) -> bool:
        return self.fail_flag

    def self_test(self) -> bool:
        return not self.adc_broken                       # the fake ADC answers unless we broke it

    def close(self) -> None:
        self.leds_off()                                  # leave everything off
        self.motor_stop()
