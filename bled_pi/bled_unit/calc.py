"""
calc.py - the maths: from light intensities to millilitres of blood.  Pure functions, no hardware.

Beer-Lambert law:   A = log10(I0 / I)
    I0 = light that reaches the photodiode when only reagent is in the cuvette   (the BLANK of this pad)
    I  = light that reaches it after the blood is mixed into the reagent         (the SAMPLE)
    A  = absorbance; it grows in proportion to the haemoglobin concentration.

Every intensity is first corrected with the DARK reading (photodiode signal with all LEDs off), which removes
stray light and the electronic offset.
"""
from __future__ import annotations

import math                                              # math.log10
from dataclasses import dataclass, field                 # small result containers
from typing import List

from .config import Config                               # the calibration numbers live there


@dataclass
class PadResult:
    """Everything we learned from one gauze pad (all of it is written to the case file)."""
    a540: float                                          # absorbance at 540 nm (haemoglobin + turbidity)
    a850: float                                          # absorbance at 850-880 nm (turbidity only)
    net_a: float                                         # a540 - k * a850 : the turbidity-corrected absorbance
    ebl_ml: float                                        # estimated blood volume of this pad in mL
    flags: List[str] = field(default_factory=list)       # warnings such as "NEG_CLAMPED" or "OVER_MAX"

#Calculate the absorbance
def absorbance(i0_net: float, i_net: float) -> float:
    """A = log10(I0/I) using intensities that already have the dark level subtracted."""
    if i0_net <= 0 or i_net <= 0:                        # log of zero/negative is undefined: caller must reject first
        raise ValueError("intensity must be positive")   # stops immediately
    return math.log10(i0_net / i_net)                    # the Beer-Lambert absorbance


def pad_volume(blank540: float, sample540: float, blank850: float, sample850: float,
               hb_patient: float, cfg: Config) -> PadResult:
    """Compute the blood volume of one pad.  All four intensities are dark-corrected counts."""
    flags: List[str] = []                                # warnings collected on the way
    a540 = absorbance(blank540, sample540)               # absorbance at the haemoglobin wavelength
    a850 = absorbance(blank850, sample850)               # absorbance at the turbidity wavelength
    net_a = a540 - cfg.turbidity_k * a850                # remove the turbidity part (bubbles, fibres, fat)
    # Linear calibration gives mL for blood with the calibration haemoglobin (cal_hb_ref).
    ml_at_ref_hb = cfg.cal_slope_ml_per_a * net_a + cfg.cal_intercept_ml
    # A patient with LESS haemoglobin loses MORE blood for the same haemoglobin mass in the reagent:
    ebl = ml_at_ref_hb * (cfg.cal_hb_ref / hb_patient)   # scale by the patient's own baseline haemoglobin
    if ebl < 0:                                          # tiny negative values are just noise around zero
        flags.append("NEG_CLAMPED")                      # remember that we corrected it
        ebl = 0.0                                        # blood volume can not be negative
    if ebl > cfg.pad_max_ml:                             # the display ignores more than 500 mL for one pad
        flags.append("OVER_MAX")                         # we still report it so the display can warn the operator
    if not cfg.calibration_confirmed:                    # constants are still the placeholders
        flags.append("UNCALIBRATED")                     # every row in the case file carries this warning
    return PadResult(a540=a540, a850=a850, net_a=net_a, ebl_ml=ebl, flags=flags)
