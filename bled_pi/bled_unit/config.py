"""
config.py - every number you may want to change lives in this one file.

Nothing else in the program contains a "magic number".  When you build the hardware you will mostly
edit THIS file: pin numbers, timings, thresholds and the calibration constants.

Values marked  [TUNE]  are sensible starting guesses that MUST be confirmed on your real hardware.
"""
from __future__ import annotations                       # lets us use modern type hints on older Python 3.7+

from dataclasses import dataclass, replace               # dataclass = a class that is just a bag of settings
                                                         # replace() creates a copy of a dataclass object with one or more fields changed.
from pathlib import Path                                 # Path = a convenient way to handle folder/file names


@dataclass
class Config:
    """One object that holds all settings.  Create it with Config() and pass it around."""

    # ------------------------------------------------------------------ serial link to the display
    serial_port: str = "/dev/serial0"      # Pi 4 hardware UART (GPIO14 = TX, GPIO15 = RX). 
    baud: int = 115200                     # must equal the display firmware (115200, 8N1)

    # ------------------------------------------------------------------ GPIO pins (BCM numbering!)
    pin_led_540: int = 17                  # output  -> driver of the 540 nm LED  (via a constant-current driver)
    pin_led_ir: int = 27                   # output  -> driver of the 850-880 nm LED
    pin_motor_pwm: int = 18                # output  -> motor driver PWM input (speed)
    pin_motor_enable: int = 23             # output  -> motor driver ENABLE input (1 = driver on)
    pin_start_button: int = 26             # input   <- START push button to GND (internal pull-up used)
    pin_cartridge: int = 5                 # input   <- cartridge micro-switch to GND (closed = cartridge present)
    pin_ac_present: int = 6                # input   <- 1 when the AC adapter powers the unit, 0 on battery
    pin_power_fail: int = 16               # input   <- power manager pulls LOW when the supply is about to fail

    # ------------------------------------------------------------------ I2C devices
    i2c_bus: int = 1                       # Pi header pins 3 (SDA) and 5 (SCL) are bus 1
    adc_address: int = 0x48                # ADS1115 with ADDR pin to GND
    has_fuel_gauge: bool = False           # set True if you fit a MAX17048 battery fuel gauge
    fuel_gauge_address: int = 0x36         # MAX17048 address

    # ------------------------------------------------------------------ ADC (ADS1115) settings
    adc_sample_period_s: float = 0.009     # ADS1115 runs at 128 samples/s = one result every 7.8 ms; wait a bit more
    adc_full_scale: int = 32767            # biggest positive value of a signed 16-bit ADC reading
    adc_max_ok: int = 30000                # [TUNE] above this the ADC is nearly saturated -> "over-range"

    # ------------------------------------------------------------------ optical reading (see slide "Reading the sensor reliably")
    dark_settle_s: float = 0.50            # [TUNE] wait after all LEDs are off, before reading the dark level
    led_settle_s: float = 0.50             # [TUNE] wait after switching an LED on (LED + photodiode settle)
    samples_per_average: int = 16          # [TUNE] ADC samples averaged for one value (N in the slide)
    warmup_rounds: int = 2                 # [TUNE] first rounds are thrown away (LED warm-up)
    kept_rounds: int = 5                   # [TUNE] rounds that are kept and averaged
    max_spread_pct: float = 2.0            # [TUNE] (max-min)/mean of the kept rounds must be below this (CAL-3)
    min_signal: float = 800.0              # [TUNE] smallest acceptable (light - dark) in ADC counts
    read_retries: int = 2                  # a noisy reading set is repeated this many times before it is a fault

    # ------------------------------------------------------------------ baseline / blank checks
    blank_tolerance_pct: float = 35.0      # [TUNE] a pad blank may differ this much from the baseline (else reagent fault)

    # ------------------------------------------------------------------ motor (mixing reagent + blood)
    motor_speed: float = 0.60              # [TUNE] mixing speed, 0.0 to 1.0 of full PWM
    motor_pwm_hz: int = 1000               # [TUNE] PWM frequency (use pigpio hardware PWM 20 kHz for a quiet 40 W motor)
    motor_ramp_s: float = 2.0              # [TUNE] seconds to ramp the speed up (and down) - protects gears and gauze
    mix_time_s: float = 1000.0               # [TUNE] seconds at full mixing speed (needs an experiment: when is A540 stable?)
    settle_after_mix_s: float = 8.0        # [TUNE] wait for bubbles and fibres to settle before the sample reading

    # ------------------------------------------------------------------ calibration (Beer-Lambert -> millilitres)
    # Pad volume = (cal_slope * NetAbsorbance + cal_intercept) * (cal_hb_ref / patient_Hb)
    #   NetAbsorbance = A540 - turbidity_k * A_IR
    # The three numbers below come from your calibration experiment (known blood volumes on gauze).
    cal_slope_ml_per_a: float = 9.5744      # [TUNE] PLACEHOLDER from the old prototype - it does NOT transfer to new hardware
    cal_intercept_ml: float = -0.09993      # [TUNE] PLACEHOLDER (same warning)
    cal_hb_ref: float = 13.0                # [TUNE] Hb (g/dL) of the blood used for the calibration experiment
    turbidity_k: float = 1.0                # [TUNE] how much of the IR absorbance is subtracted (1.0 = all of it)
    calibration_confirmed: bool = True     # set True ONLY after you calibrated on the real unit (removes the warning)
    max_absorbance: float = 2.0             # above this the sample is too dark to read -> "optical over-range"
    pad_max_ml: float = 500.0               # the display ignores values above this (protocol limit)

    # ------------------------------------------------------------------ communication behaviour
    stat_period_s: float = 1.0              # STAT frame at least once per second (protocol)
    pad_resend_count: int = 3               # PAD frame is repeated this many extra times (display ignores duplicates)
    pad_resend_period_s: float = 1.0        # seconds between those repeats
    error_repeat_period_s: float = 4.0      # while in ERROR the ERR frame is repeated this often
    base_dup_window_s: float = 3.0          # a BASE that arrives this soon after a baseline ended is a stale repeat
    poll_period_s: float = 0.25             # how often inputs (cartridge, power) are checked

    # ------------------------------------------------------------------ safety / housekeeping
    power_fail_shutdown_os: bool = False    # True = also run "sudo shutdown -h now" after a power-fail warning
    data_dir: Path = Path.home() / "bled_data"   # case files, active-case state and the log file go here

    # ------------------------------------------------------------------ helpers
    def fast(self) -> "Config":
        """Return a copy with all waiting times made ~50x shorter. Used for tests and the PC simulator only."""
        return replace(                                  # replace() copies the dataclass and changes some fields
            self,
            dark_settle_s=0.002, led_settle_s=0.004,      # tiny settle times
            adc_sample_period_s=0.0005,                   # tiny ADC period (the fake ADC answers instantly)
            samples_per_average=6, warmup_rounds=1, kept_rounds=3,   # fewer samples and rounds
            motor_ramp_s=0.1, mix_time_s=0.3, settle_after_mix_s=0.1,  # short mixing
            pad_resend_period_s=0.3, error_repeat_period_s=0.6,      # quicker repeats
            base_dup_window_s=0.5, poll_period_s=0.05,
        )
