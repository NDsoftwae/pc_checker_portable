# PC Hardware Test - bootable USB diagnostic

Boot any PC from this USB stick and it automatically tests the hardware, then saves a report
(HTML + text) **on the USB stick**. The computer does **not** need a hard disk or an installed
operating system. Everything runs from the USB stick and RAM.

Internal disks are only **read**. Nothing on the tested computer is ever changed.

## What is tested

| Part | Checks |
|---|---|
| System | Manufacturer, model, serial number, motherboard, BIOS version, chassis type |
| CPU | Model, cores, stress test on all cores with result verification, peak temperature, thermal throttling, clock speed under load |
| RAM | Size, every slot (size / type / speed / brand / part no.), memory error test, ECC counters |
| Disks (HDD / SSD / NVMe / eMMC) | SMART health, bad / pending sectors, SSD life left, power-on hours, temperature, read speed test, built-in drive self-test (full mode), "no disk detected" warning |
| Graphics & screen | GPU and driver, built-in screen and connected monitors (name, size, resolution) |
| Network | Ethernet link and speed, Wi-Fi adapter + scan for networks, internet, Bluetooth, radio switch |
| Battery | Health (wear %), charge, cycle count, charger detected, "not charging" problem |
| Temperatures & fans | All sensors, fan speeds |
| Audio | Sound cards and codecs |
| Devices | Keyboard, touchpad, webcam, card reader, USB buses and devices |
| System log | Hardware errors: CPU machine-check, ECC, disk I/O, SATA/NVMe resets, PCIe, GPU hangs, overheating |
| Interactive (optional) | Keyboard keys, screen dead-pixel colour test, speakers L/R, microphone, every USB port |

Each item is graded **PASS / WARN / FAIL**, and the report gives an overall verdict.

## One-time setup of the USB stick (Windows)

You need a USB stick of at least 2 GB. **It will be erased.**

1. Download the **SystemRescue** ISO (free): <https://www.system-rescue.org/Download/>
2. Download **Rufus**: <https://rufus.ie>
3. In Rufus select the USB stick and the SystemRescue ISO.
   - Partition scheme: **MBR** (boots on both old BIOS and UEFI PCs)
   - File system: **FAT32**
   - Click START and, when asked, choose **"Write in ISO Image mode"** (not DD mode), so the stick
     stays writable for reports.
4. Copy the test program onto the stick (replace `E` with your USB drive letter):

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\Copy-To-USB.ps1 -Drive E
   ```

   You can also copy the three folders inside `usb\` (`autorun`, `pctest`, `sysrescue.d`) to the
   root of the stick by hand.

The stick then looks like this:

```
E:\
├── autorun\autorun0          <- starts the test automatically at boot
├── pctest\pctest.py          <- the test program
├── pctest\pctest.conf        <- settings (company name, test durations)
├── sysrescue.d\500-pctest.yaml
├── PC-Reports\               <- reports are saved here
└── (SystemRescue files...)
```

## Using it

1. Plug in the stick and switch on the PC. Open the boot menu (usually **F12**, **F11**, **F8**,
   **Esc** or **F9**) and pick the USB stick.
2. In the SystemRescue menu just press **Enter** (or wait).
3. The test menu appears:
   - **1 Quick test** (~4 min). Starts automatically after 20 seconds, so you can walk away.
   - **2 Quick + interactive checks** (keyboard, screen, speakers, mic, USB ports)
   - **3 Full test** (~45 min: 10 min CPU stress, up to 30 min RAM test, disk self-test)
   - **4 Full + interactive**
   - **5 Shell**
4. When finished it shows the results and saves the report. You can then view it, open it in a
   browser (`g`), reboot or power off.
5. On any Windows PC open `PC-Reports\<date>_<model>_<serial>\report.html`. It prints well
   (Ctrl+P) for a customer, and has a technician notes / signature box.
   `PC-Reports\index.csv` keeps one line per tested computer (open with Excel).

Press **Ctrl+C** during a long test to skip just that test. Type `pctest` in the shell to run again.

## Settings - `pctest\pctest.conf`

Edit with Notepad: company name on the report, technician name, default menu choice and
timeout, CPU / RAM / disk test durations.

## Tips

- **PC won't boot the stick:** disable *Secure Boot* in BIOS/UEFI setup, or try the other
  USB port / Legacy mode.
- **Disk installed but "NO DISK DETECTED":** set SATA mode to **AHCI** in BIOS. Intel RST /
  RAID / VMD mode hides NVMe drives from Linux.
- **Deep RAM test:** in the SystemRescue boot menu choose **Memtest86+** and let it run 1 or more passes.
- **Report could not be saved:** the stick was written in DD mode (read-only). Redo step 3 in
  ISO mode, or plug in a second FAT32 USB stick before the test ends. It is used automatically.
- Interactive checks need someone at the keyboard. Unattended quick tests need nobody.
- The time in the report comes from the computer's clock, which can be wrong on old machines.

## Files in this folder

```
Copy-To-USB.ps1              copies the program to a SystemRescue USB stick
usb\autorun\autorun0         boot-time launcher (bash)
usb\pctest\pctest.py         the diagnostic program (Python 3, no extra modules)
usb\pctest\pctest.conf       settings
usb\sysrescue.d\500-pctest.yaml  SystemRescue options (keep stick mounted, autorun)
```
