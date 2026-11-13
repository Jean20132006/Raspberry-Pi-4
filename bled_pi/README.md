# BLED measurement unit - Raspberry Pi 4 software

Python 3 program that runs on the Raspberry Pi 4 inside the BLED.  It

* fires the two LEDs (540 nm and 850-880 nm), **one at a time**, and reads the OPT101 photodiode through an ADS1115 ADC,
* runs the motor that mixes the reagent with the blood from the gauze,
* computes the blood volume of every pad (Beer-Lambert, turbidity correction, patient haemoglobin),
* talks to the display over the UART with the frames you already have (`$PAD,3,41.2*33E9`),
* writes every pad to a CSV file and survives power cuts.

## How a pad is measured

```
operator inserts gauze  ->  presses START  (the display must have an active case)
 1. BLANK   dark reading + 540 nm / IR readings of the clear reagent         (CAL-1: every pad has its own blank)
 2. MIX     motor ramps up, runs mix_time_s, ramps down, solution settles
 3. SAMPLE  dark reading + 540 nm / IR readings of the mixed solution
 4. A540 = log10(blank540/sample540)   A850 = log10(blank850/sample850)
    netA = A540 - k * A850                                  (turbidity correction)
    mL   = (slope * netA + intercept) * (Hb_calibration / Hb_patient)
 5. send  $PAD,n,mL   (repeated 3 times, the display ignores duplicates)
```

## Files

| File | What it does |
|---|---|
| `bled_unit/config.py` | **all** settings: pins, timings, thresholds, calibration. Edit this first |
| `bled_unit/protocol.py` | frame encoder / parser / CRC-16 (same rules as `bled_proto.c`) |
| `bled_unit/hardware.py` | real Raspberry Pi: GPIO (gpiozero) + ADS1115 (smbus2) |
| `bled_unit/sensor.py` | one reliable reading set (dark, warm-up rounds, averaging, checks) |
| `bled_unit/calc.py` | the maths: absorbance and millilitres |
| `bled_unit/jobs.py` | the two long operations: baseline and one pad (blank, mix, sample) |
| `bled_unit/storage.py` | case CSV file and the "active case" file (power-cut recovery) |
| `bled_unit/link.py` | serial port + background receive thread |
| `bled_unit/controller.py` | the state machine: IDLE, BASELINE, READY, CASE_ACTIVE, ERROR, SHUTDOWN |
| `bled_unit/main.py` | starts everything |
| `deploy/bled-unit.service` | start automatically at boot, restart if frozen |

## 1. Raspberry Pi set-up

```bash
sudo apt update
sudo apt install -y python3-gpiozero python3-lgpio python3-smbus2 python3-serial
sudo raspi-config        # Interface Options: I2C = Enable;  Serial Port: login shell = NO, serial hardware = YES
# add to /boot/firmware/config.txt (or /boot/config.txt on older systems), then reboot:
#     dtoverlay=disable-bt          <- gives the stable hardware UART to /dev/serial0
sudo usermod -aG dialout,gpio,i2c pi
python3 -m bled_unit.main -v        # run by hand once
```
Automatic start: copy the folder to `/home/pi/bled_pi`, then
`sudo cp deploy/bled-unit.service /etc/systemd/system/ && sudo systemctl enable --now bled-unit`.
Logs: `journalctl -u bled-unit -f` and `~/bled_data/bled_unit.log`.  Case files: `~/bled_data/cases/<case id>.csv`.

## 2. Wiring (BCM numbers are in `config.py`)

| Function | Pi pin | Connects to |
|---|---|---|
| UART TX (GPIO14) | 8 | display **RX = IO44** |
| UART RX (GPIO15) | 10 | display **TX = IO43** |
| GND | 6 | display GND (TX and RX are **crossed**, GND is shared) |
| I2C SDA / SCL (GPIO2 / GPIO3) | 3 / 5 | ADS1115 SDA / SCL (VDD 3.3 V, ADDR to GND = 0x48) |
| LED 540 nm (GPIO17) | 11 | input of an LED **constant-current driver** |
| LED IR (GPIO27) | 13 | input of the second LED driver |
| Motor PWM (GPIO18) | 12 | PWM input of the motor driver |
| Motor enable (GPIO23) | 16 | ENABLE input of the motor driver |
| START button (GPIO26) | 37 | button to GND (internal pull-up used) |
| Cartridge switch (GPIO5) | 29 | micro-switch to GND, **closed = cartridge present** |
| AC present (GPIO6) | 31 | HIGH when the AC adapter feeds the unit |
| Power-fail (GPIO16) | 36 | power manager pulls **LOW** shortly before the supply fails |
| OPT101 output | - | ADS1115 **AIN0**, power the OPT101 from 3.3 V (its output then stays below ~2 V) |

Never drive an LED or a motor straight from a GPIO pin (max about 16 mA per pin).  The Pi only sends logic signals.

## 3. Calibrate before you trust a number

The log says `CALIBRATION IS NOT CONFIRMED` and every case-file row says `UNCALIBRATED` until you do this:

1. **Mixing time**: run a pad and record A540 against time with the motor on.  Set `mix_time_s` to when the curve is flat.
2. **Timing / thresholds**: with a clean cartridge look at the spread of the readings, then set `led_settle_s`,
   `samples_per_average`, `max_spread_pct`, `min_signal`.
3. **Calibration line**: put known blood volumes (0, 1, 2, 5, 10 mL ...) on gauze using blood of known haemoglobin,
   record `net_a` from the case file, fit a straight line, then enter `cal_slope_ml_per_a`, `cal_intercept_ml`,
   `cal_hb_ref`.  The numbers in `config.py` are placeholders from the old prototype.  Set `calibration_confirmed = True`.
4. **Range**: after each baseline the log prints `usable range: ... about N mL per pad`.  A pad bigger than that gives
   `ERR 4`.  Choose the reagent volume and LED current so that the biggest expected pad fits.

## 4. How the system protects itself

* **One controller, one queue**: no race conditions; UART, button, timers and worker threads only post events.
* **Never two LEDs on together**; LEDs and motor are forced OFF after every job, error, cancel, shutdown.
* **Per-pad blank** (CAL-1) and blank-versus-baseline check -> `ERR 3` when the reagent is missing or wrong.
* **Resume, do not restart**: if a pad fails after mixing, START continues with the same blank (a new blank would be wrong).
* **Lost-frame protection**: each `PAD` frame is repeated 3 times; the display ignores duplicate numbers.
* **Power cut**: every row is `fsync`ed; the pad counter is saved atomically; after a reboot the next `CASE` with the same id
  continues with the next pad number.  A power-fail input triggers a clean shutdown.
* **Cartridge removed** during a measurement: motor and LEDs stop at once, the case stays open, `CASE` resumes it.
* **Lost ENDC**: a CASE with a different id closes the old case first.
* **systemd watchdog**: restarts the program if the controller freezes.

## 5. Error codes sent to the display

| Code | Meaning | When |
|---|---|---|
| 1 | no cartridge | BASE / START without cartridge |
| 2 | sensor fault | ADC does not answer, LED/IR signal too low, readings unstable, sample brighter than blank |
| 3 | reagent problem | the blank of a pad differs more than `blank_tolerance_pct` from the baseline |
| 4 | optical over-range | ADC saturated, or the sample is too dark (pad holds more blood than the optics can measure) |

After an error the display shows it and the unit waits.  Fix the cause and press **START** (or send BASE / ENDC).
