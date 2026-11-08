Author: Jean Eric Tsanga
# Steps to run the BLED on Raspberry Pi4
# Step 1: install packages
```bash
sudo apt update
sudo apt install -y python3-gpiozero python3-lgpio python3-smbus2 python3-serial
```
# Step 2: enable I2C and the serial port (This part is interactive)
```bash
sudo raspi-config
```
This opens a menu. Go to Interface Options:

I2C: Enable.
Serial Port: “login shell over serial?” `No`, then “serial hardware enabled?” `Yes`.
Choose Finish, but you can skip the reboot for now.
# Step 3: edit the boot config (manual)
sudo nano /boot/firmware/config.txt
```bash
sudo nano /boot/firmware/config.txt
```
# Step 4: add yourself to the device groups
```bash
sudo usermod -aG dialout,gpio,i2c $USER
```
Replace $USER by the USER name that you have in `bled-unit.service` file (In my case, I replace it by eric)
These group changes take effect after the next login, so reboot now:
```bash
sudo reboot

```
# Step 5 Check the username and groups(Optional)
```bash
whoami          # should print: eric
groups eric     # after the reboot, should list dialout gpio i2c

```
# Step 5: check the hardware, then run the project
After the reboot, with the ADS1115 wired:
```bash
sudo apt install -y i2c-tools
i2cdetect -y 1        # you should see 48
ls -l /dev/serial0    # should point to ttyAMA0
cd ~/Raspberry-Pi-4/bled_pi          # use the folder where your project actually is (In my case bled_pi folder is inside Raspberry-Pi-4 folder)
python3 -m bled_unit.main -v # must be run from the folder that        contains bled_unit/.

```
# To start automatically at boot (optional)
Do it once the program runs reliably by hand:
```bash
cd ~/Raspberry-Pi-4/bled_pi
sudo cp deploy/bled-unit.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bled-unit

```
After that the program starts each time the Pi boots. 
# Useful commands:
```bash

sudo systemctl status bled-unit     # is it running?
journalctl -u bled-unit -f          # live log
sudo systemctl stop bled-unit       # stop it (needed before running it by hand again)

```
# Shut the Pi down properly
Don’t just pull the power. Run ```bash sudo shutdown -h now``` and wait until the green LED stops blinking. Cutting power while it’s writing can corrupt the SD card.
