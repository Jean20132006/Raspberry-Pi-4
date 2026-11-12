"""
sensor.py - takes ONE reliable set of light readings.
Return: - average of (i540 - dark) of N samples
        - average of (i850 - dark) of N samples
        - how spread the measurement was

One reading set =
    1. DARK:  all LEDs off, wait, average N ADC samples      -> stray light + electronic offset
    2. ROUNDS (warm-up rounds are thrown away):
         540 nm LED on, wait to settle, average N samples, LED off
         IR    LED on, wait to settle, average N samples, LED off      (never both LEDs together)
    3. DARK again, then average the two dark values (catches slow drift)
    4. Subtract the dark level, average the kept rounds, check the result before accepting it.
"""
from __future__ import annotations

import logging                                           # prints the readings to the terminal and the log file
import threading                                         # Event used to cancel a measurement
from dataclasses import dataclass                        # result container
from typing import Callable, List, Optional

from .config import Config
from .hardware import Hardware

log = logging.getLogger("bled.sensor")                    # messages from this file are tagged bled.sensor


class Cancelled(Exception):
    """Raised when the measurement is aborted on purpose (case ended, cartridge removed, shutdown)."""


class OpticalError(Exception):
    """A reading was taken but is not acceptable (too dark, too bright, too noisy).  Carries the ERR code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)                        # text for the log
        self.code = code                                 # protocol error code: 2 sensor fault, 4 optical over-range
        self.message = message


@dataclass
class Reading:
    """Averaged, dark-corrected light levels of one reading set (all in ADC counts)."""
    dark: float                                          # average dark level that was subtracted
    net540: float                                        # 540 nm light minus dark
    net850: float                                        # IR light minus dark
    spread540: float                                     # (max-min)/mean of the kept rounds, in percent
    spread850: float


class OpticalReader:
    """Knows how to flash the LEDs and average the ADC.  One object is used for all measurements."""

    def __init__(self, hw: Hardware, cfg: Config, cancel: threading.Event) -> None:
        self.hw = hw                                     # the hardware
        self.cfg = cfg                                   # timings and thresholds
        self.cancel = cancel                             # set() from another thread to abort

    # ---------------------------------------------------------------- small helpers
    def _wait(self, seconds: float) -> None:
        """Sleep, but wake up immediately (and abort) if the cancel flag is set."""
        if self.cancel.wait(seconds):                    # wait() returns True as soon as the flag is set
            raise Cancelled()                            # unwind all the way up to the job runner

    def _average(self) -> float:
        """Average N ADC samples (N = cfg.samples_per_average)."""
        total = 0.0                                      # running sum
        for _ in range(self.cfg.samples_per_average):    # take N samples
            total += self.hw.read_adc()                  # one conversion (may raise OSError if I2C fails)
            self._wait(self.cfg.adc_sample_period_s)     # let the ADC finish its next conversion
        return total / self.cfg.samples_per_average      # the average

    def _dark(self) -> float:
        """Dark level: every LED off, settle, average."""
        self.hw.leds_off()                               # no light at all
        self._wait(self.cfg.dark_settle_s)               # let the photodiode settle
        return self._average()

    def _lit(self, wavelength: str) -> float:
        """Light level with ONE LED on: switch on, settle, average, switch off again."""
        self.hw.led_on(wavelength)                       # exactly one LED
        try:
            self._wait(self.cfg.led_settle_s)            # LED brightness and photodiode settle
            return self._average()                       # take the samples
        finally:
            self.hw.leds_off()
                                       # LED off even if we were cancelled or an error occurred
    """how much the repeated measurement rounds disagree with each other """
    @staticmethod
    def _spread_pct(values: List[float]) -> float:
        """(max - min) / mean in percent: how much the rounds disagree."""
        mean = sum(values) / len(values)                 # average of the rounds
        if mean <= 0:                                    # avoid dividing by zero
            return 100.0
        return (max(values) - min(values)) / mean * 100.0

    # ---------------------------------------------------------------- the public measurement
    def read_set(self, kind: str, progress: Optional[Callable[[float], None]] = None) -> Reading:
        """Take a reading set.  kind = 'blank' (clear reagent) or 'sample' (after mixing).

        progress(fraction) is called with 0.0 ... 1.0 while measuring.
        Raises OpticalError for an unusable result and OSError if the ADC stops answering.
        """
        last_error: Optional[OpticalError] = None        # remember why the previous attempt failed
        for _attempt in range(self.cfg.read_retries + 1):   # first try + retries
            try:
                return self._one_attempt(kind, progress)  # success: done
            except OpticalError as exc:                  # noisy/odd reading
                last_error = exc                         # keep the reason
                if exc.code == 4:                        # over-range will not fix itself by repeating
                    break
        assert last_error is not None                    # we only get here after at least one failure
        raise last_error                                 # report the last reason

    def _one_attempt(self, kind: str, progress: Optional[Callable[[float], None]]) -> Reading:
        """One try of the full dark / rounds / dark sequence."""
        cfg = self.cfg                                   # shortcut
        total_rounds = cfg.warmup_rounds + cfg.kept_rounds   # rounds measured in total
        dark1 = self._dark()                             # dark level before the rounds
        v540: List[float] = []                           # raw 540 nm values of the KEPT rounds
        v850: List[float] = []                           # raw IR values of the KEPT rounds
        for r in range(total_rounds):                    # run all rounds
            a = self._lit("540")                         # 540 nm average of this round
            b = self._lit("IR")                          # IR average of this round
            if r >= cfg.warmup_rounds:                   # the first rounds are warm-up and are discarded
                v540.append(a)
                v850.append(b)
            if progress is not None:                     # report progress to the caller (baseline -> BPRG frames)
                progress((r + 1) / (total_rounds + 1))   # leave the last bit for the final dark reading
        dark2 = self._dark()                             # dark level after the rounds
        if progress is not None:
            progress(1.0)                                # finished
        dark = (dark1 + dark2) / 2.0                     # average dark level cancels slow drift
        net540 = [v - dark for v in v540]                # dark-corrected 540 nm rounds
        net850 = [v - dark for v in v850]                # dark-corrected IR rounds
        m540 = sum(net540) / len(net540)                 # mean 540 nm signal
        m850 = sum(net850) / len(net850)                 # mean IR signal
        s540, s850 = self._spread_pct(net540), self._spread_pct(net850)   # disagreement between rounds
        # --- print what was measured BEFORE any check, so the numbers are visible even when the reading is rejected
        log.info("%s reading: dark=%.0f (before %.0f, after %.0f) | 540 nm: raw mean=%.0f net mean=%.0f spread=%.2f %% | "
                 "IR: raw mean=%.0f net mean=%.0f spread=%.2f %% | limits: spread<=%.1f %%, net>=%.0f, raw<=%.0f",
                 kind, dark, dark1, dark2,
                 sum(v540) / len(v540), m540, s540,
                 sum(v850) / len(v850), m850, s850,
                 cfg.max_spread_pct, cfg.min_signal, cfg.adc_max_ok)
        # --- saturation check on the RAW values (before subtracting anything)
        if max(v540 + v850) > cfg.adc_max_ok:            # ADC (nearly) full: too much light
            raise OpticalError(4, "optical over-range: ADC saturated (%.0f counts)" % max(v540 + v850))
        # --- signal too small?
        if m850 < cfg.min_signal:                        # the IR light is not absorbed by blood, so a weak IR signal = hardware fault
            raise OpticalError(2, "sensor fault: IR signal too low (%.0f counts)" % m850)
        if m540 < cfg.min_signal:                        # weak 540 nm signal
            if kind == "blank":                          # a clear blank must be bright -> LED or sensor problem
                raise OpticalError(2, "sensor fault: 540 nm blank too low (%.0f counts)" % m540)
            raise OpticalError(4, "optical over-range: sample too dark (%.0f counts)" % m540)   # sample too concentrated
        # --- stability check
        if max(s540, s850) > cfg.max_spread_pct:         # too noisy to trust
            raise OpticalError(2, "unstable reading: spread %.2f %% / %.2f %%" % (s540, s850))
        return Reading(dark=dark, net540=m540, net850=m850, spread540=s540, spread850=s850)
