"""
jobs.py - the two long operations, each of which runs in its own worker thread:

    run_baseline()  - optical baseline check (progress is reported so the display can show a progress bar)
    run_pad()       - one gauze pad:  blank -> mix with the motor -> settle -> sample -> compute

They are plain functions that BLOCK until finished.  The controller starts them in a thread so the UART and
the STAT heartbeat keep running while the motor spins.  A threading.Event ("cancel") stops them quickly.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional

from . import calc
from .config import Config
from .hardware import Hardware
from .sensor import Cancelled, OpticalError, OpticalReader, Reading


class JobError(Exception):                              # an error with a protocol ERR code
    """A job failed.  `code` is the protocol ERR code that goes to the display."""

    def __init__(self, code: int, message: str) -> None:  # message text and ERR code
        super().__init__(message)                       # keep the text in the Exception
        self.code = code                                # the protocol error code (1-4)
        self.message = message                          # readable explanation for the log


# ============================================================================================ baseline
@dataclass                                              # a dataclass gets __init__ from its fields
class BaselineResult:                                   # result of the baseline
    """Outcome of the baseline check."""
    ok: bool                                             # True = sensor and reagent look healthy
    i540: int                                            # blank intensity at 540 nm, rounded (goes into BDON and the case file)
    i850: int                                            # blank intensity at IR
    message: str                                         # human readable explanation for the log
    reading: Optional[Reading] = None                    # the full reading (kept as the reference for later pad blanks)


def run_baseline(hw: Hardware, cfg: Config, cancel: threading.Event,  # run_baseline(): arguments: hardware, settings, cancel flag,
                 progress: Callable[[int], None]) -> BaselineResult:  # and a function to report progress in percent
    """Measure the clear-reagent light levels and decide whether the optics are healthy."""
    reader = OpticalReader(hw, cfg, cancel)              # helper that flashes LEDs and averages the ADC
    try:                                                # everything that can go wrong is handled below
        # Convert the reader's 0..1 progress into whole percent for BPRG frames.
        reading = reader.read_set("blank", lambda f: progress(int(f * 100)))  # turn the reader's 0..1 progress into 0..100 percent
    except OpticalError as exc:                          # light levels unacceptable: this is a *failed baseline*, not a crash
        return BaselineResult(False, 0, 0, exc.message)  # BDON,0 will be sent
    except OSError as exc:                               # the ADC stopped answering: real hardware fault
        raise JobError(2, "ADC communication failed: %s" % exc)  # report the ADC problem as ERR 2
    finally:                                            # always executed, even after an error:
        hw.leds_off()                                    # always leave the LEDs off
    return BaselineResult(True, int(round(reading.net540)), int(round(reading.net850)), "baseline OK", reading)  # ok: store the intensities as whole numbers (counts)


# ============================================================================================ one pad
@dataclass                                              # dataclass: progress of one pad
class PadContext:                                       # remembers what is already done for this pad
    """What has already been done for the pad that is being measured.

    If a pad fails halfway (for example a noisy sample reading) the operator can press START again and the job
    RESUMES here instead of starting over - a new blank after mixing would be wrong because the blood is
    already in the reagent.
    """
    blank: Optional[Reading] = None                      # blank reading of this pad (CAL-1), or None if not yet taken
    mixed: bool = False                                  # True after the motor has mixed and the solution has settled


@dataclass                                              # dataclass: all results of one pad
class PadOutcome:                                       # everything produced by one pad
    """Everything produced by one pad measurement."""
    blank: Reading                                      # blank reading (clear reagent)
    sample: Reading                                     # sample reading (mixed solution)
    result: calc.PadResult                              # calculated absorbances and millilitres


def _mix(hw: Hardware, cfg: Config, cancel: threading.Event) -> None:  # _mix(): run the motor
    """Run the motor: ramp up, hold, ramp down, then wait for the solution to settle."""
    steps = 10                                           # number of little speed steps in a ramp
    try:                                                # the motor is switched off afterwards, whatever happens
        for i in range(1, steps + 1):                    # ramp UP in 10 steps
            hw.motor_set(cfg.motor_speed * i / steps)    # 10 %, 20 % ... 100 % of the mixing speed
            if cancel.wait(cfg.motor_ramp_s / steps):    # wait one step (returns True if cancelled)
                raise Cancelled()                       # stop immediately when cancelled
        if cancel.wait(cfg.mix_time_s):                  # hold the mixing speed for the mixing time
            raise Cancelled()                           # stop immediately when cancelled
        for i in range(steps - 1, -1, -1):               # ramp DOWN: 90 % ... 0 %
            hw.motor_set(cfg.motor_speed * i / steps)   # 10 %, 20 % ... 0 % of the mixing speed
            if cancel.wait(cfg.motor_ramp_s / steps):   # wait one step; True = cancelled
                raise Cancelled()                       # abort the job
    finally:                                            # always executed: motor off
        hw.motor_stop()                                  # whatever happened, the motor MUST be off afterwards
    if cancel.wait(cfg.settle_after_mix_s):              # bubbles and fibres settle with the motor stopped
        raise Cancelled()                               # abort the job


def run_pad(hw: Hardware, cfg: Config, cancel: threading.Event, ctx: PadContext,  # run_pad(): arguments: hardware, settings, cancel flag, progress so far,
            hb_patient: float, baseline_ref: Optional[Reading]) -> PadOutcome:  # patient Hb and the baseline reference
    """Measure one pad.  Raises JobError (hardware / reagent faults) or Cancelled."""
    reader = OpticalReader(hw, cfg, cancel)              # optics helper
    try:                                                # optical problems and ADC errors are translated below
        # ---- step 1: BLANK (requirement CAL-1: every pad has its own blank) ------------------------------
        if ctx.blank is None:                            # not done yet (or this is a fresh pad)
            ctx.blank = reader.read_set("blank")         # clear reagent, no blood mixed in yet
            if baseline_ref is not None:                 # we have a baseline to compare with
                deviation = abs(ctx.blank.net540 - baseline_ref.net540) / baseline_ref.net540 * 100.0  # how far (in %) the new blank is from the baseline
                if deviation > cfg.blank_tolerance_pct:  # far from the baseline: reagent missing, wrong, or cuvette dirty
                    ctx.blank = None                     # this blank is not trusted, take a new one next time
                    raise JobError(3, "reagent problem: blank differs %.0f %% from the baseline" % deviation)  # report a reagent problem
        # ---- step 2: MIX ----------------------------------------------------------------------------------
        if not ctx.mixed:                                # the motor has not run for this pad yet
            _mix(hw, cfg, cancel)                        # mix and settle
            ctx.mixed = True                             # remember it so a retry does not mix twice
        # ---- step 3: SAMPLE -------------------------------------------------------------------------------
        sample = reader.read_set("sample")               # light through the mixed solution
        # ---- step 4: COMPUTE ------------------------------------------------------------------------------
        blank = ctx.blank                                # the blank that belongs to this pad
        if sample.net540 > blank.net540 * 1.05 or sample.net850 > blank.net850 * 1.05:  # brighter than the blank is physically impossible
            # the mixture became BRIGHTER than the clear reagent: impossible physically -> something is wrong
            raise JobError(2, "sample brighter than blank (check blank / lamp drift)")  # report a sensor fault
        result = calc.pad_volume(blank.net540, sample.net540, blank.net850, sample.net850, hb_patient, cfg)  # apply Beer-Lambert and the calibration
        if result.a540 > cfg.max_absorbance:             # beyond the range where Beer-Lambert is reliable
            raise OpticalError(4, "optical over-range: A540 = %.2f" % result.a540)  # beyond the range where Beer-Lambert is reliable
        return PadOutcome(blank=blank, sample=sample, result=result)  # everything went well
    except OpticalError as exc:                          # translate optical problems into protocol errors
        raise JobError(exc.code, exc.message)           # translate the optical error into a protocol ERR code
    except OSError as exc:                               # I2C failure
        raise JobError(2, "ADC communication failed: %s" % exc)  # I2C failure: report as sensor fault
    finally:                                            # always executed:
        hw.leds_off()                                    # safety: LEDs off
        hw.motor_stop()                                  # safety: motor off
