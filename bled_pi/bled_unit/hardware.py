"""
hardware.py - the ONLY file that touches the real Raspberry Pi pins and the I2C bus.

All other files talk to a "Hardware" object with the methods below.  That makes it possible to run the whole
program on a normal PC with fake_hardware.FakeHardware, and to test the logic before the electronics exist.

Pin and device summary (numbers are in config.py):
    LED 540 nm, LED IR     -> two GPIO outputs, each switching a constant-current LED driver (NEVER a bare GPIO!)
    Motor                  -> one PWM pin (speed) + one ENABLE pin on the motor driver (a 40 W motor needs a driver)
    OPT101 photodiode      -> its analog output is read by an ADS1115 16-bit ADC on I2C
    START button           -> GPIO input, pressed = connects the pin to GND
    Cartridge switch       -> GPIO input, cartridge inserted = connects the pin to GND
    AC present, power fail -> GPIO inputs from the power-management board
"""
from __future__ import annotations

import time                                              # short pauses
from typing import Callable, Optional                    # type hints

from .config import Config                               # pin numbers and I2C addresses


class Hardware:                                         # the interface every hardware class must implement
    """The interface every hardware implementation (real or fake) must provide."""

    def set_start_callback(self, callback: Callable[[], None]) -> None:  # who to call when START is pressed
        """Register a function that is called (from another thread) every time START is pressed."""
        raise NotImplementedError                        # real classes override this

    def led_on(self, wavelength: str) -> None:          # switch exactly one LED on
        """Switch ONE LED on and the other off.  wavelength is '540' or 'IR'."""
        raise NotImplementedError                       # implemented by the subclasses below

    def leds_off(self) -> None:                         # both LEDs off
        """Switch both LEDs off (needed for the dark reading and for safety)."""
        raise NotImplementedError                       # implemented by the subclasses

    def motor_set(self, speed: float) -> None:          # set the motor speed 0..1
        """Set the mixing motor speed 0.0 (stopped) to 1.0 (full)."""
        raise NotImplementedError                       # implemented by the subclasses

    def motor_stop(self) -> None:                       # stop the motor now
        """Stop the motor immediately and disable its driver."""
        raise NotImplementedError                       # implemented by the subclasses

    def read_adc(self) -> int:                          # one raw ADC reading
        """Return ONE raw signed 16-bit ADC reading of the photodiode (bigger = more light)."""
        raise NotImplementedError                       # implemented by the subclasses

    def cartridge_present(self) -> bool:                # is the cartridge inserted?
        """True while a cartridge is inserted."""
        raise NotImplementedError                       # implemented by the subclasses

    def on_battery(self) -> bool:                       # running from the battery?
        """True when the unit runs on the battery (AC adapter missing)."""
        raise NotImplementedError                       # implemented by the subclasses

    def battery_percent(self) -> int:                   # battery charge in percent
        """Battery charge 0-100, or -1 when unknown."""
        raise NotImplementedError                       # implemented by the subclasses

    def power_fail(self) -> bool:                       # is the supply about to fail?
        """True when the power manager warns that the supply is about to fail."""
        raise NotImplementedError                       # implemented by the subclasses

    def self_test(self) -> bool:                        # start-up check of the ADC
        """Quick check at start-up: can we talk to the ADC?  True = OK."""
        raise NotImplementedError                       # implemented by the subclasses

    def close(self) -> None:                            # release the hardware
        """Release every pin (called once when the program ends).  Outputs go to a safe (off) state."""
        raise NotImplementedError                       # implemented by the subclasses


class PiHardware(Hardware):                             # the real Raspberry Pi 4 implementation
    """The real thing: Raspberry Pi 4 GPIO (gpiozero library) + ADS1115 on I2C (smbus2 library)."""

    # ADS1115 register addresses and the configuration word we use (see the ADS1115 data sheet)
    _REG_CONVERSION = 0x00                               # register that holds the latest conversion result
    _REG_CONFIG = 0x01                                   # register that selects channel, gain and speed
    # Config bits:  MUX = AIN0 vs GND (0x4000) | PGA = +-2.048 V (0x0400) | continuous mode (0x0000)
    #               | 128 samples per second (0x0080) | comparator off (0x0003)
    _CONFIG_WORD = 0x4000 | 0x0400 | 0x0000 | 0x0080 | 0x0003  # the 16-bit value written to the ADS1115 config register

    def __init__(self, cfg: Config) -> None:            # constructor: open every pin and the I2C bus
        # The libraries are imported HERE (not at the top of the file) so that a PC without them can still
        # import this module and run the simulator.
        from gpiozero import Button, DigitalInputDevice, DigitalOutputDevice, PWMOutputDevice
        import smbus2

        self._cfg = cfg                                  # keep the settings
        self._start_cb: Optional[Callable[[], None]] = None   # function to call on a START press (set later)

        # --- outputs: both LEDs start OFF (initial_value=False)
        self._led540 = DigitalOutputDevice(cfg.pin_led_540, initial_value=False)  # 540 nm LED driver input, starts OFF
        self._ledir = DigitalOutputDevice(cfg.pin_led_ir, initial_value=False)  # IR LED driver input, starts OFF
        # --- motor: PWM sets the speed, ENABLE switches the driver; both start OFF
        self._motor_pwm = PWMOutputDevice(cfg.pin_motor_pwm, frequency=cfg.motor_pwm_hz, initial_value=0)  # motor PWM (speed), starts at 0
        self._motor_en = DigitalOutputDevice(cfg.pin_motor_enable, initial_value=False)  # motor driver enable, starts OFF
        # --- inputs. pull_up=True means: the pin idles HIGH and a switch to GND makes it LOW.
        #     For such inputs gpiozero reports is_active == True while the pin is LOW (= switch closed).
        self._button = Button(cfg.pin_start_button, pull_up=True, bounce_time=0.05)   # 50 ms debounce in software
        self._button.when_pressed = self._button_pressed  # gpiozero calls this function in its own thread
        self._cart = DigitalInputDevice(cfg.pin_cartridge, pull_up=True)              # LOW = cartridge inserted
        self._ac = DigitalInputDevice(cfg.pin_ac_present, pull_up=False)              # HIGH = AC adapter present
        self._pfail = DigitalInputDevice(cfg.pin_power_fail, pull_up=True)            # LOW = power failing
        # --- I2C bus and ADC set-up
        self._bus = smbus2.SMBus(cfg.i2c_bus)            # open /dev/i2c-1
        self._adc_ok = False                             # becomes True when the ADC answered
        self._configure_adc()                            # write the configuration word

    # ----------------------------------------------------------------------------------------- ADC helpers
    def _configure_adc(self) -> None:                   # _configure_adc(): tell the ADC how to measure
        """Write the config word (big-endian: high byte first) so the ADC converts continuously."""
        try:                                            # I2C can fail if the wiring is wrong
            hi, lo = (self._CONFIG_WORD >> 8) & 0xFF, self._CONFIG_WORD & 0xFF   # split 16 bits into 2 bytes
            self._bus.write_i2c_block_data(self._cfg.adc_address, self._REG_CONFIG, [hi, lo])  # send to the ADC
            self._adc_ok = True                          # no exception -> the chip is there
        except OSError:                                  # wiring problem or wrong address
            self._adc_ok = False                        # remember that the chip did not answer

    def read_adc(self) -> int:                          # read_adc(): one conversion result
        """Read the conversion register (2 bytes, signed, big-endian).  Raises OSError if the I2C read fails."""
        data = self._bus.read_i2c_block_data(self._cfg.adc_address, self._REG_CONVERSION, 2)  # 2 bytes
        raw = (data[0] << 8) | data[1]                   # join the bytes into one 16-bit number
        if raw & 0x8000:                                 # the top bit set means a negative number
            raw -= 0x10000                               # convert from "two's complement" to a Python int
        return raw                                       # counts: 0 .. 32767 for 0 .. 2.048 V (0.0625 mV per count)

    def self_test(self) -> bool:                        # self_test(): is the ADC alive?
        """Re-configure the ADC and read it once; True when the chip answers."""
        self._configure_adc()                            # try again (the chip might have been powered late)
        if not self._adc_ok:                             # still silent
            return False                                # chip still silent
        try:                                            # try one real reading
            self.read_adc()                              # one test conversion
            return True                                  # the ADC works
        except OSError:                                 # I2C error
            return False                                # ADC not working

    # ----------------------------------------------------------------------------------------- LEDs
    def led_on(self, wavelength: str) -> None:          # led_on(): exactly one LED at a time
        """Only ONE LED may be on at any time, so each reading belongs to a single wavelength (OPT-4)."""
        if wavelength == "540":                          # green LED requested
            self._ledir.off()                            # make sure the other LED is off FIRST
            self._led540.on()                            # then switch the green LED on
        elif wavelength == "IR":                         # infrared LED requested
            self._led540.off()                          # the IR LED must be off first
            self._ledir.on()                            # now the IR LED on
        else:                                            # programming mistake
            raise ValueError("unknown wavelength %r" % wavelength)  # unknown wavelength name = programming mistake

    def leds_off(self) -> None:                         # leds_off(): both off
        """Both LEDs off."""
        self._led540.off()                              # 540 nm LED off
        self._ledir.off()                               # IR LED off

    # ----------------------------------------------------------------------------------------- motor
    def motor_set(self, speed: float) -> None:          # motor_set(): speed 0..1
        """Speed 0..1.  Enables the driver while the speed is above zero."""
        speed = max(0.0, min(1.0, speed))                # keep the value inside the legal range
        if speed > 0:                                    # we want it to turn
            self._motor_en.on()                          # driver ON
            self._motor_pwm.value = speed                # duty cycle = speed
        else:                                            # we want it to stop
            self.motor_stop()                           # speed 0 means stop

    def motor_stop(self) -> None:                       # motor_stop(): safe stop
        """Duty cycle 0 and driver disabled (so the motor is not held under power)."""
        self._motor_pwm.value = 0                        # no pulses
        self._motor_en.off()                             # driver OFF

    # ----------------------------------------------------------------------------------------- inputs
    def set_start_callback(self, callback: Callable[[], None]) -> None:  # remember the function to call
        """Remember who to call when START is pressed."""
        self._start_cb = callback                       # store it

    def _button_pressed(self) -> None:                  # called by gpiozero when the button is pressed
        """gpiozero calls this when the (debounced) button goes down; we forward it."""
        if self._start_cb is not None:                   # somebody is listening
            self._start_cb()                            # tell the controller

    def cartridge_present(self) -> bool:                # cartridge_present(): switch closed = LOW = is_active
        return bool(self._cart.is_active)                # LOW on the pin = switch closed = cartridge inserted

    def on_battery(self) -> bool:                       # on_battery(): AC pin LOW = battery
        return not bool(self._ac.is_active)              # AC pin LOW -> we run on the battery

    def power_fail(self) -> bool:                       # power_fail(): pin LOW = failing
        return bool(self._pfail.is_active)               # LOW on the pin = power is about to fail

    def battery_percent(self) -> int:                   # battery_percent(): from the fuel gauge
        """Read the MAX17048 fuel gauge (register 0x04, high byte = whole percent).  -1 if not fitted."""
        if not self._cfg.has_fuel_gauge:                 # no gauge in this build
            return -1                                   # no gauge fitted
        try:                                            # read the gauge register
            data = self._bus.read_i2c_block_data(self._cfg.fuel_gauge_address, 0x04, 2)  # state of charge
            return max(0, min(100, data[0]))             # high byte is already in percent
        except OSError:                                  # gauge not answering
            return -1                                   # gauge not answering

    # ----------------------------------------------------------------------------------------- cleanup
    def close(self) -> None:                            # close(): leave everything off and release the pins
        """Turn everything off and give the pins back to the operating system."""
        try:                                            # make outputs safe first
            self.leds_off()                              # LEDs off
            self.motor_stop()                            # motor off
        finally:                                         # even if the lines above failed, release the pins
            for dev in (self._led540, self._ledir, self._motor_pwm, self._motor_en,  # release every pin and device ...
                        self._button, self._cart, self._ac, self._pfail):  # ... one by one
                dev.close()                              # release one device
            self._bus.close()                            # close the I2C bus
