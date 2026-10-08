#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PC Hardware Test - bootable hardware diagnostic (runs on SystemRescue live USB).

* Works without an internal disk and without any installed operating system.
* READ-ONLY for the computer's own drives: nothing is ever written to them.
* Reports (HTML + text + raw logs) are saved on the USB stick in PC-Reports/.

Usage:  pctest                 -> menu
        pctest --mode quick -y -> quick test, no menu
"""
import argparse
import configparser
import datetime
import glob
import html
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback

VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
BOOTMNT = "/run/archiso/bootmnt"

PASS, WARN, FAIL, INFO, SKIP = "PASS", "WARN", "FAIL", "INFO", "SKIP"
RANK = {SKIP: 0, INFO: 0, PASS: 1, WARN: 2, FAIL: 3}

DEFAULT_CONFIG = {
    "report": {
        "company_name": "",
        "technician": "",
    },
    "menu": {
        "default_choice": "1",
        "timeout_seconds": "20",
    },
    "tests": {
        "cpu_stress_quick_seconds": "60",
        "cpu_stress_full_seconds": "600",
        "ram_quick_seconds": "60",
        "ram_full_minutes": "30",
        "disk_read_quick_mb": "1024",
        "disk_read_full_mb": "2048",
        "disk_selftest_in_full": "yes",
        "disk_selftest_max_minutes": "6",
    },
}

# --------------------------------------------------------------------------
# Console helpers
# --------------------------------------------------------------------------
USE_COLOR = sys.stdout.isatty()
COLORS = {PASS: "1;32", WARN: "1;33", FAIL: "1;31", INFO: "1;36", SKIP: "0;37"}


def col(text, code):
    if not USE_COLOR:
        return text
    return "\033[%sm%s\033[0m" % (code, text)


def badge(status):
    return col("[%s]" % status.center(4), COLORS.get(status, "0"))


def say(text=""):
    print(text, flush=True)


def ask(prompt, timeout=None, default=""):
    """Read a line from the console. Returns default on timeout/EOF/no tty."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        if not sys.stdin or not sys.stdin.isatty():
            say()
            return default
        if timeout:
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
            if not ready:
                say()
                return default
        line = sys.stdin.readline()
        if not line:
            return default
        return line.strip()
    except (OSError, ValueError):
        return default


def ask_yes_no(prompt):
    """Returns True / False, or None when the user typed 's' (skip)."""
    while True:
        a = ask(prompt + " [y = yes / n = no / s = skip]: ").lower()
        if a in ("y", "yes"):
            return True
        if a in ("n", "no"):
            return False
        if a in ("s", "skip", ""):
            return None


def progress(label, elapsed, total, extra=""):
    width = shutil.get_terminal_size((80, 24)).columns
    frac = min(1.0, elapsed / total) if total else 0
    bar_len = 20
    filled = int(bar_len * frac)
    left = max(0, int(total - elapsed))
    line = "  %s [%s%s] %3d%%  %d:%02d left  %s" % (
        label, "#" * filled, "-" * (bar_len - filled), frac * 100,
        left // 60, left % 60, extra)
    sys.stdout.write("\r" + line[:width - 1].ljust(width - 1))
    sys.stdout.flush()


def end_progress():
    sys.stdout.write("\n")
    sys.stdout.flush()


# --------------------------------------------------------------------------
# System helpers
# --------------------------------------------------------------------------
def have(tool):
    return shutil.which(tool) is not None


def run(cmd, timeout=60, input_text=None):
    """Run a command and return (returncode, combined output). Never raises."""
    shell = isinstance(cmd, str)
    if not shell and not have(cmd[0]):
        return 127, ""
    kwargs = dict(shell=shell, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                  timeout=timeout, universal_newlines=True, errors="replace")
    if input_text is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = input_text
    try:
        p = subprocess.run(cmd, **kwargs)
        return p.returncode, p.stdout or ""
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return 124, out
    except OSError as e:
        return 127, str(e)


def rd(path, default=None):
    try:
        with open(path, errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def rd_int(path, default=None):
    v = rd(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def dig(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d:
            d = d[k]
        elif isinstance(d, list) and isinstance(k, int) and -len(d) <= k < len(d):
            d = d[k]
        else:
            return default
    return d


def fmt_bytes(n, binary=False):
    if n is None:
        return "?"
    step = 1024.0 if binary else 1000.0
    units = ["B", "KiB", "MiB", "GiB", "TiB"] if binary else ["B", "KB", "MB", "GB", "TB"]
    n = float(n)
    for u in units:
        if n < step or u == units[-1]:
            return ("%.0f %s" if n >= 100 or u == "B" else "%.1f %s") % (n, u)
        n /= step


def fmt_hours(h):
    if h is None:
        return "?"
    return "%s h (~%.1f years of use)" % ("{:,}".format(int(h)), h / 8760.0)


JUNK_DMI = {"", "to be filled by o.e.m.", "default string", "system product name",
            "system manufacturer", "not specified", "none", "0123456789", "n/a",
            "not applicable", "system serial number", "chassis serial number", "123456789"}


def clean_dmi(v):
    v = (v or "").strip()
    return "" if v.lower() in JUNK_DMI else v


def strip_ctrl(text):
    """Remove the backspace/carriage-return progress noise some tools print."""
    text = re.sub(r".\x08", "", text)
    text = text.replace("\x08", "").replace("\r", "\n")
    return re.sub(r"\n{3,}", "\n\n", text)


# --------------------------------------------------------------------------
# Long-running process monitor (progress bar + temperature sampling)
# --------------------------------------------------------------------------
def run_monitored(cmds, expected_secs, label, sample=None, hard_limit=None):
    """Run several commands in parallel and show a progress bar.

    Returns (results, hit_limit) where results is a list of (rc, output).
    Ctrl+C stops the commands and re-raises KeyboardInterrupt.
    """
    procs, bufs, readers = [], [], []
    for cmd in cmds:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, universal_newlines=True,
                             errors="replace", start_new_session=True)
        buf = []
        t = threading.Thread(target=lambda p=p, buf=buf: buf.extend(iter(lambda: p.stdout.read(4096), "")),
                             daemon=True)
        t.start()
        procs.append(p)
        bufs.append(buf)
        readers.append(t)

    limit = hard_limit if hard_limit else expected_secs + 120
    start = time.time()
    hit_limit = False

    def kill_all():
        for p in procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except OSError:
                    pass
        deadline = time.time() + 10
        for p in procs:
            try:
                p.wait(max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                p.wait()

    try:
        while any(p.poll() is None for p in procs):
            elapsed = time.time() - start
            extra = ""
            if sample:
                try:
                    extra = sample() or ""
                except Exception:
                    extra = ""
            progress(label, elapsed, hard_limit or expected_secs, extra)
            if elapsed > limit:
                hit_limit = True
                kill_all()
                break
            time.sleep(2)
    except KeyboardInterrupt:
        kill_all()
        end_progress()
        raise
    end_progress()
    for t in readers:
        t.join(5)
    return [(p.returncode, "".join(b)) for p, b in zip(procs, bufs)], hit_limit


# --------------------------------------------------------------------------
# Sensors
# --------------------------------------------------------------------------
CPU_HWMON = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "soc_thermal", "cpu")


def hwmon_readings():
    temps, fans = [], []
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = rd(os.path.join(hw, "name"), "hwmon")
        for f in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
            v = rd_int(f)
            if v is None:
                continue
            t = v / 1000.0
            if t <= -40 or t >= 150 or t == 0:
                continue
            lbl = rd(f.replace("_input", "_label")) or os.path.basename(f).replace("_input", "")
            temps.append((name, lbl, t))
        for f in sorted(glob.glob(os.path.join(hw, "fan*_input"))):
            v = rd_int(f)
            if v is None:
                continue
            lbl = rd(f.replace("_input", "_label")) or os.path.basename(f).replace("_input", "")
            fans.append((name, lbl, v))
    return temps, fans


def thermal_zones():
    out = []
    for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        t = rd_int(os.path.join(z, "temp"))
        if t is None or t <= 0:
            continue
        out.append((rd(os.path.join(z, "type"), "zone"), t / 1000.0))
    return out


def cpu_temp():
    temps, _ = hwmon_readings()
    vals = [t for n, l, t in temps if n in CPU_HWMON]
    if not vals:
        vals = [t for n, t in thermal_zones() if n in ("x86_pkg_temp", "cpu-thermal", "cpu_thermal")]
    if not vals:
        vals = [t for n, t in thermal_zones() if n.startswith("acpitz")]
    return max(vals) if vals else None


def throttle_count():
    total = 0
    for f in glob.glob("/sys/devices/system/cpu/cpu*/thermal_throttle/*_throttle_count"):
        total += rd_int(f, 0) or 0
    return total


def cpu_freqs_mhz():
    vals = [rd_int(f) for f in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq")]
    vals = [v / 1000.0 for v in vals if v]
    if not vals:
        info = rd("/proc/cpuinfo", "")
        vals = [float(m) for m in re.findall(r"cpu MHz\s*:\s*([\d.]+)", info)]
    return vals


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
class Section:
    def __init__(self, key, title):
        self.key = key
        self.title = title
        self.items = []      # (label, value, status)
        self.notes = []
        self.tables = []     # (title, headers, rows)
        self.seconds = 0.0

    def add(self, label, value, status=INFO):
        self.items.append((label, str(value), status))
        say("  %s %s: %s" % (badge(status), label, value))
        return status

    def note(self, text):
        self.notes.append(text)

    def table(self, title, headers, rows):
        if rows:
            self.tables.append((title, headers, rows))

    @property
    def status(self):
        if not self.items:
            return SKIP
        r = max(RANK[s] for _, _, s in self.items)
        if r == 3:
            return FAIL
        if r == 2:
            return WARN
        if r == 1:
            return PASS
        return INFO if any(s == INFO for _, _, s in self.items) else SKIP


class Ctx:
    def __init__(self, cfg, mode, interactive):
        self.cfg = cfg
        self.mode = mode
        self.full = mode == "full"
        self.interactive = interactive
        self.sections = []
        self.raw = {}
        self.summary = {}
        self.boot_disks = set()
        self.selftests = {}          # /dev/xxx -> start time
        self.is_laptop = False
        self.started = datetime.datetime.now()

    def section(self, key, title):
        s = Section(key, title)
        self.sections.append(s)
        return s

    def num(self, key, default=0):
        try:
            return float(self.cfg.get("tests", key))
        except (ValueError, configparser.Error):
            return default


# --------------------------------------------------------------------------
# Disk helpers
# --------------------------------------------------------------------------
def lsblk_flat(cols):
    rc, out = run(["lsblk", "-J", "-l", "-p", "-o", cols])
    try:
        return json.loads(out).get("blockdevices", []) if rc == 0 else []
    except ValueError:
        return []


def detect_boot_disks():
    """Names (e.g. 'sdb') of the USB stick we booted from - never tested/touched."""
    names = set()
    for path in (BOOTMNT, HERE):
        rc, src = run(["findmnt", "-n", "-o", "SOURCE", "-T", path])
        src = src.strip().splitlines()[0] if src.strip() else ""
        if rc == 0 and src.startswith("/dev/"):
            rc, pk = run(["lsblk", "-n", "-o", "PKNAME", src])
            pk = pk.strip().splitlines()
            names.add(pk[0].strip() if pk and pk[0].strip() else os.path.basename(src))
    for row in lsblk_flat("NAME,PKNAME,LABEL,TYPE"):
        label = (row.get("label") or "").upper()
        if label.startswith("RESCUE") or label == "PCTEST":
            names.add(os.path.basename(row.get("pkname") or row.get("name") or ""))
    names.discard("")
    return names


def list_disks(ctx):
    rc, out = run(["lsblk", "-J", "-b", "-d", "-o", "NAME,SIZE,MODEL,SERIAL,TRAN,ROTA,TYPE,RM,VENDOR"])
    try:
        devs = json.loads(out).get("blockdevices", []) if rc == 0 else []
    except ValueError:
        devs = []
    disks = []
    for d in devs:
        name = d.get("name", "")
        if d.get("type") != "disk" or re.match(r"(loop|zram|ram|sr|fd|nbd|md|dm-)", name):
            continue
        if re.match(r"mmcblk\d+boot|mmcblk\d+rpmb", name):
            continue
        if name in ctx.boot_disks:
            continue
        try:
            size = int(d.get("size") or 0)
        except ValueError:
            size = 0
        if size == 0:
            continue
        d["size"] = size
        d["rota"] = str(d.get("rota")).lower() in ("1", "true")
        d["path"] = "/dev/" + name
        tran = (d.get("tran") or "").lower()
        if name.startswith("nvme"):
            d["kind"] = "NVMe SSD"
        elif name.startswith("mmcblk"):
            d["kind"] = "eMMC / SD"
        elif d["rota"]:
            d["kind"] = "HDD"
        else:
            d["kind"] = "SSD"
        if tran == "usb":
            d["kind"] += " (USB)"
        disks.append(d)
    return disks


SMART_CMD = ["smartctl", "-j", "-a", "-l", "selftest"]


def smart_json(dev):
    rc, out = run(SMART_CMD + [dev], timeout=60)
    try:
        return json.loads(out), out
    except ValueError:
        return None, out


# --------------------------------------------------------------------------
# Test sections
# --------------------------------------------------------------------------
CHASSIS = {3: "Desktop", 4: "Low Profile Desktop", 5: "Pizza Box", 6: "Mini Tower", 7: "Tower",
           8: "Portable", 9: "Laptop", 10: "Notebook", 11: "Hand Held", 13: "All-in-One",
           14: "Sub Notebook", 15: "Space-saving", 16: "Lunch Box", 17: "Server", 23: "Rack Mount",
           24: "Sealed-case PC", 30: "Tablet", 31: "Convertible", 32: "Detachable",
           35: "Mini PC", 36: "Stick PC"}
LAPTOP_CHASSIS = {8, 9, 10, 11, 14, 30, 31, 32}


def test_system(ctx):
    s = ctx.section("system", "System Information")

    def dmi(sysfs, key):
        v = clean_dmi(rd("/sys/class/dmi/id/" + sysfs))
        if not v:
            rc, out = run(["dmidecode", "-s", key])
            lines = [l.strip() for l in out.splitlines() if l.strip() and not l.startswith("#")]
            v = clean_dmi(lines[0]) if rc == 0 and lines else ""
        return v

    vendor = dmi("sys_vendor", "system-manufacturer")
    product = dmi("product_name", "system-product-name")
    version = dmi("product_version", "system-version")
    serial = dmi("product_serial", "system-serial-number")
    board = " ".join(x for x in (dmi("board_vendor", "baseboard-manufacturer"),
                                 dmi("board_name", "baseboard-product-name")) if x)
    bios = " ".join(x for x in (dmi("bios_vendor", "bios-vendor"), dmi("bios_version", "bios-version")) if x)
    bios_date = dmi("bios_date", "bios-release-date")
    chassis_num = rd_int("/sys/class/dmi/id/chassis_type", 0) or 0
    ctx.is_laptop = chassis_num in LAPTOP_CHASSIS or bool(glob.glob("/sys/class/power_supply/BAT*"))

    # Lenovo puts the friendly name ("ThinkPad T480") in product_version.
    model = product
    if version and vendor.lower().startswith("lenovo") and version.lower() not in product.lower():
        model = "%s (%s)" % (version, product)

    ctx.summary.update(manufacturer=vendor or "Unknown", model=model or "Unknown",
                       serial=serial or "Unknown")
    s.add("Manufacturer", vendor or "Unknown")
    s.add("Model", model or "Unknown")
    s.add("Serial number", serial or "Not set")
    if board:
        s.add("Motherboard", board)
    if bios:
        s.add("BIOS / UEFI firmware", "%s (%s)" % (bios, bios_date) if bios_date else bios)
    s.add("Chassis type", CHASSIS.get(chassis_num, "Unknown (%d)" % chassis_num))
    s.add("Boot mode", "UEFI" if os.path.isdir("/sys/firmware/efi") else "Legacy BIOS")
    s.add("Test mode", ctx.mode.capitalize() + (" + interactive checks" if ctx.interactive else ""))
    s.add("Test date / time", ctx.started.strftime("%Y-%m-%d %H:%M") + " (from computer clock)")

    ctx.raw["dmidecode"] = run(["dmidecode"])[1]
    ctx.raw["lspci"] = run(["lspci", "-nnk"])[1]
    ctx.raw["lsusb"] = run(["lsusb"])[1] + "\n" + run(["lsusb", "-t"])[1]
    ctx.raw["lsblk"] = run(["lsblk", "-o", "NAME,SIZE,TYPE,FSTYPE,LABEL,MODEL,SERIAL,TRAN,MOUNTPOINT"])[1]


def test_cpu(ctx):
    s = ctx.section("cpu", "Processor (CPU)")
    rc, out = run(["lscpu"])
    ctx.raw["lscpu"] = out
    info = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info.setdefault(k.strip(), v.strip())
    model = info.get("Model name")
    if not model:
        m = re.search(r"model name\s*:\s*(.+)", rd("/proc/cpuinfo", ""))
        model = m.group(1).strip() if m else ""
    model = re.sub(r"\s+", " ", model)
    threads = os.cpu_count() or 1
    try:
        cores = int(info.get("Core(s) per socket", "0")) * int(info.get("Socket(s)", "1"))
    except ValueError:
        cores = 0
    ctx.summary["cpu"] = model or "Unknown"
    s.add("Model", model or "Not detected", PASS if model else WARN)
    s.add("Cores / threads", "%s cores / %d threads" % (cores or "?", threads))
    max_mhz = None
    try:
        max_mhz = float(info.get("CPU max MHz", "0")) or None
    except ValueError:
        pass
    if not max_mhz:
        v = rd_int("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
        max_mhz = v / 1000.0 if v else None
    if max_mhz:
        s.add("Maximum clock", "%.0f MHz" % max_mhz)
    if info.get("Virtualization"):
        s.add("Virtualization", info["Virtualization"])

    t_idle = cpu_temp()
    if t_idle is not None:
        st = WARN if t_idle >= 75 else PASS
        s.add("Idle temperature", "%.0f °C" % t_idle, st)
    else:
        s.add("Temperature sensor", "Not available on this machine")

    secs = int(ctx.num("cpu_stress_full_seconds" if ctx.full else "cpu_stress_quick_seconds", 60))
    th0 = throttle_count()
    peak = [t_idle or 0.0]
    freq_samples = []

    def sample():
        f = cpu_freqs_mhz()
        if f:
            freq_samples.append(sum(f) / len(f))
        t = cpu_temp()
        if t is not None:
            peak[0] = max(peak[0], t)
            return "CPU %.0f°C  %s" % (t, ("%.0f MHz" % freq_samples[-1]) if f else "")
        return ("%.0f MHz" % freq_samples[-1]) if f else ""

    say("  Running stress test on all %d threads for %d s (Ctrl+C to skip)..." % (threads, secs))
    if have("stress-ng"):
        cmd = ["stress-ng", "--cpu", str(threads), "--cpu-method", "all", "--verify",
               "--timeout", "%ds" % secs, "--metrics-brief"]
        (res,), _ = run_monitored([cmd], secs, "CPU stress", sample)
        rc, out = res
        ctx.raw["stress-ng_cpu"] = out
        label = "Stress test (%d s, all threads, results verified)" % secs
        if rc == 0:
            s.add(label, "Passed - no calculation errors", PASS)
        elif rc == 2 or re.search(r"fail(ed)?: *[1-9]", out):
            s.add(label, "CALCULATION ERRORS - CPU is unstable (overheating, bad CPU or VRM)", FAIL)
        else:
            s.add(label, "Could not complete (stress-ng exit code %s)" % rc, WARN)
    else:
        rc, errors = python_cpu_burn(threads, secs, sample)
        label = "Stress test (%d s, all threads, results verified)" % secs
        if errors:
            s.add(label, "%d CALCULATION ERRORS - CPU is unstable" % errors, FAIL)
        else:
            s.add(label, "Passed - no calculation errors", PASS)

    if peak[0]:
        p = peak[0]
        st = FAIL if p >= 100 else WARN if p >= 92 else PASS
        s.add("Peak temperature under load", "%.0f °C" % p, st)
        if st != PASS:
            s.note("High CPU temperature: clean the fan/heatsink and replace thermal paste.")
    throttled = throttle_count() - th0
    if throttled > 0:
        s.add("Thermal throttling", "CPU slowed itself down %d times due to heat" % throttled, WARN)
    elif glob.glob("/sys/devices/system/cpu/cpu0/thermal_throttle"):
        s.add("Thermal throttling", "None", PASS)
    if freq_samples and max_mhz:
        loaded = freq_samples[len(freq_samples) // 3:] or freq_samples
        avg = sum(loaded) / len(loaded)
        st = WARN if avg < max_mhz * 0.35 else PASS
        s.add("Average clock under load", "%.0f MHz (max %.0f MHz)" % (avg, max_mhz), st)
        if st == WARN:
            s.note("CPU stays at low speed under load: power limit or overheating. On laptops this "
                   "is often caused by a dead battery or a charger that is not recognised.")


def _burn_worker(stop_at, conn):
    import hashlib
    data = b"pctest-burn" * 4096
    ref = hashlib.sha256(data).hexdigest()
    n = errs = 0
    while time.time() < stop_at:
        if hashlib.sha256(data).hexdigest() != ref:
            errs += 1
        n += 1
    conn.send((n, errs))
    conn.close()


def python_cpu_burn(threads, secs, sample):
    import multiprocessing
    stop_at = time.time() + secs
    procs = []
    for _ in range(threads):
        a, b = multiprocessing.Pipe(False)
        p = multiprocessing.Process(target=_burn_worker, args=(stop_at, b))
        p.start()
        procs.append((p, a))
    start = time.time()
    try:
        while any(p.is_alive() for p, _ in procs):
            progress("CPU stress", time.time() - start, secs, sample() or "")
            time.sleep(2)
    except KeyboardInterrupt:
        for p, _ in procs:
            p.terminate()
        end_progress()
        raise
    end_progress()
    errors = 0
    for p, a in procs:
        if a.poll(1):
            errors += a.recv()[1]
        p.join(1)
    return 0, errors


def parse_dmi_blocks(text, header):
    blocks, cur = [], None
    for line in text.splitlines():
        if line.strip() == header and not line.startswith("\t"):
            cur = {}
            blocks.append(cur)
            continue
        if cur is None:
            continue
        if not line.strip():
            cur = None
            continue
        if line.startswith("\t") and not line.startswith("\t\t") and ":" in line:
            k, v = line.strip().split(":", 1)
            cur[k.strip()] = v.strip()
    return blocks


def meminfo():
    out = {}
    for line in (rd("/proc/meminfo", "") or "").splitlines():
        m = re.match(r"(\w+):\s+(\d+)", line)
        if m:
            out[m.group(1)] = int(m.group(2)) * 1024
    return out


def test_memory(ctx):
    s = ctx.section("memory", "Memory (RAM)")
    mi = meminfo()
    total = mi.get("MemTotal", 0)
    rc, out = run(["dmidecode", "-t", "17"])
    modules = parse_dmi_blocks(out, "Memory Device") if rc == 0 else []
    installed = 0
    rows = []
    for m in modules:
        size = m.get("Size", "")
        empty = not size or "No Module" in size or size.startswith("0")
        mm = re.match(r"(\d+)\s*(MB|GB|TB)", size)
        if mm and not empty:
            installed += int(mm.group(1)) * {"MB": 1, "GB": 1024, "TB": 1024 * 1024}[mm.group(2)]
        speed = m.get("Configured Memory Speed") or m.get("Configured Clock Speed") or m.get("Speed", "")
        rows.append([m.get("Locator", "?"), "Empty" if empty else size,
                     "" if empty else m.get("Type", ""), "" if empty else speed,
                     "" if empty else clean_dmi(m.get("Manufacturer", "")),
                     "" if empty else clean_dmi(m.get("Part Number", ""))])
    s.table("Memory slots", ["Slot", "Size", "Type", "Speed", "Manufacturer", "Part number"], rows)

    ram_txt = fmt_bytes(installed * 1024 * 1024, True) if installed else fmt_bytes(total, True)
    ctx.summary["ram"] = ram_txt
    s.add("Installed memory", ram_txt, PASS if total else FAIL)
    s.add("Usable by system", fmt_bytes(total, True))
    if rows:
        used = sum(1 for r in rows if r[1] != "Empty")
        s.add("Slots used", "%d of %d" % (used, len(rows)))
        types = sorted(set(r[2] for r in rows if r[2] and r[2] != "Unknown"))
        if types:
            s.add("Memory type", ", ".join(types))

    ce = sum(rd_int(f, 0) or 0 for f in glob.glob("/sys/devices/system/edac/mc/mc*/ce_count"))
    ue = sum(rd_int(f, 0) or 0 for f in glob.glob("/sys/devices/system/edac/mc/mc*/ue_count"))
    if glob.glob("/sys/devices/system/edac/mc/mc*"):
        st = FAIL if ue else WARN if ce else PASS
        s.add("ECC error counters", "%d corrected, %d uncorrected" % (ce, ue), st)

    avail_mb = mi.get("MemAvailable", total) // (1024 * 1024)
    threads = os.cpu_count() or 1
    workers = max(1, min(threads, 4))
    if ctx.full and have("memtester"):
        minutes = ctx.num("ram_full_minutes", 30)
        target = max(128, int(avail_mb * 0.85) - 300)
        per = max(64, target // workers)
        limit = int(minutes * 60)
        say("  Testing %d MB of RAM with memtester (%d workers, up to %d min, Ctrl+C to skip)..."
            % (per * workers, workers, minutes))
        cmds = [["memtester", "%dM" % per, "1"] for _ in range(workers)]
        results, hit = run_monitored(cmds, limit, "RAM test", hard_limit=limit)
        text = "\n".join(strip_ctrl(o)[-6000:] for _, o in results)
        ctx.raw["memtester"] = text
        fails = sum(o.count("FAILURE") for _, o in results)
        label = "Memory pattern test (memtester, %d MB)" % (per * workers)
        if fails:
            s.add(label, "%d MEMORY ERRORS FOUND - faulty RAM module" % fails, FAIL)
            s.note("Test RAM modules one at a time to find the faulty one.")
        elif hit:
            s.add(label, "No errors in %d minutes (time limit reached before full pass)" % minutes, PASS)
        elif all(rc == 0 for rc, _ in results):
            s.add(label, "Passed - no errors", PASS)
        else:
            s.add(label, "Could not run fully (exit codes %s)" % [rc for rc, _ in results], WARN)
    elif have("stress-ng"):
        secs = int(ctx.num("ram_full_minutes", 30) * 60 if ctx.full else ctx.num("ram_quick_seconds", 60))
        target = max(128, int(avail_mb * (0.8 if ctx.full else 0.6)) - 200)
        per = max(64, target // workers)
        say("  Testing %d MB of RAM for %d s (Ctrl+C to skip)..." % (per * workers, secs))
        cmd = ["stress-ng", "--vm", str(workers), "--vm-bytes", "%dM" % per, "--vm-method", "all",
               "--verify", "--timeout", "%ds" % secs, "--metrics-brief"]
        (res,), _ = run_monitored([cmd], secs, "RAM test")
        rc, out = res
        ctx.raw["stress-ng_vm"] = out
        label = "Memory stress test (%d MB, %d s, verified)" % (per * workers, secs)
        if rc == 0:
            s.add(label, "Passed - no errors", PASS)
        elif rc == 2 or re.search(r"fail(ed)?: *[1-9]|verif\w* fail", out, re.I):
            s.add(label, "MEMORY ERRORS FOUND - faulty RAM", FAIL)
        else:
            s.add(label, "Could not complete (exit code %s)" % rc, WARN)
    elif have("memtester"):
        secs = int(ctx.num("ram_quick_seconds", 60))
        (res,), hit = run_monitored([["memtester", "256M", "1"]], secs, "RAM test", hard_limit=secs)
        fails = res[1].count("FAILURE")
        s.add("Memory test (256 MB)", "%d ERRORS" % fails if fails else "No errors",
              FAIL if fails else PASS)
    else:
        s.add("Memory test", "No memory test tool available", SKIP)
    if not ctx.full:
        s.note("Quick mode tests part of the RAM briefly. For a deep RAM test choose 'Full' or "
               "boot 'Memtest86+' from the USB boot menu.")


def start_disk_selftests(ctx):
    """Full mode: start SMART short self-tests early; they run inside the drives
    while the CPU/RAM tests run. Read-only - does not change data."""
    if not (ctx.full and have("smartctl")):
        return
    if ctx.cfg.get("tests", "disk_selftest_in_full", fallback="yes").lower() not in ("1", "yes", "true"):
        return
    for d in list_disks(ctx):
        rc, out = run(["smartctl", "-t", "short", d["path"]], timeout=30)
        if rc in (0, 4) and re.search(r"(Testing has begun|Self-test has begun|Self[- ]test.*(started|begun))", out, re.I):
            ctx.selftests[d["path"]] = time.time()


def selftest_state(j):
    """Returns ('running', pct) / ('passed', text) / ('failed', text) / (None, '')."""
    if dig(j, "device", "protocol") == "NVMe":
        op = dig(j, "nvme_self_test_log", "current_self_test_operation", "value", default=0)
        if op:
            return "running", dig(j, "nvme_self_test_log", "current_self_test_completion_percent", default=0)
        entry = dig(j, "nvme_self_test_log", "table", 0)
        if entry:
            val = dig(entry, "self_test_result", "value")
            text = dig(entry, "self_test_result", "string", default="")
            if val == 0:
                return "passed", text or "Completed without error"
            if val in (1, 2, 15):      # aborted / not used
                return None, text
            return "failed", text
        return None, ""
    st = dig(j, "ata_smart_data", "self_test", "status")
    if st:
        if (st.get("value", 0) >> 4) == 15:
            return "running", 100 - st.get("remaining_percent", 100)
        if st.get("passed") is True:
            return "passed", st.get("string", "Completed without error")
        if st.get("passed") is False:
            return "failed", st.get("string", "Failed")
    return None, ""


WEAR_ATTR = re.compile(r"Wear_Leveling|Life_Left|Wearout|Lifetime_Remain|Percent_Lifetime|SSD_Life", re.I)


def evaluate_smart(s, tag, j, disk):
    if j is None:
        s.add(tag + " SMART", "smartctl returned no data", INFO)
        return
    exit_status = dig(j, "smartctl", "exit_status", default=0) or 0
    if exit_status & 2 and not dig(j, "smart_status"):
        msgs = "; ".join(m.get("string", "") for m in dig(j, "smartctl", "messages", default=[]) or [])
        s.add(tag + " SMART", "Not available (%s)" % (msgs or "device does not support it / USB adapter"), INFO)
        return

    passed = dig(j, "smart_status", "passed")
    if passed is True:
        s.add(tag + " SMART overall health", "PASSED", PASS)
    elif passed is False:
        s.add(tag + " SMART overall health", "FAILING - drive predicts its own failure. Replace it!", FAIL)

    temp = dig(j, "temperature", "current")
    if temp:
        s.add(tag + " Temperature", "%d °C" % temp, WARN if temp >= 60 else PASS)
    hours = dig(j, "power_on_time", "hours")
    if hours is not None:
        s.add(tag + " Power-on time", fmt_hours(hours))
    cycles = dig(j, "power_cycle_count")
    if cycles is not None:
        s.add(tag + " Power cycles", "{:,}".format(cycles))

    nv = dig(j, "nvme_smart_health_information_log")
    if nv:
        cw = nv.get("critical_warning", 0)
        if cw:
            s.add(tag + " Critical warning", "0x%02x - drive reports a critical problem" % cw, FAIL)
        used = nv.get("percentage_used")
        if used is not None:
            health = max(0, 100 - used)
            st = FAIL if used >= 100 else WARN if used >= 80 else PASS
            s.add(tag + " Remaining life", "%d %%" % health, st)
            disk["health"] = "%d%%" % health
        spare, thr = nv.get("available_spare"), nv.get("available_spare_threshold")
        if spare is not None:
            st = FAIL if thr is not None and spare < thr else WARN if spare < 50 else PASS
            s.add(tag + " Available spare", "%d %% (threshold %s %%)" % (spare, thr), st)
        me = nv.get("media_errors")
        if me is not None:
            s.add(tag + " Media / data errors", me, FAIL if me > 0 else PASS)
        duw = nv.get("data_units_written")
        if duw:
            s.add(tag + " Total data written", fmt_bytes(duw * 512000))
        us = nv.get("unsafe_shutdowns")
        if us is not None:
            s.add(tag + " Unsafe shutdowns", "{:,}".format(us))

    attrs = dig(j, "ata_smart_attributes", "table", default=[]) or []
    by_id = {a.get("id"): a for a in attrs}

    def raw(i):
        a = by_id.get(i)
        if not a:
            return None
        v = dig(a, "raw", "value")
        # some drives pack several counters in the raw value; keep the low 32 bits
        return v & 0xFFFFFFFF if isinstance(v, int) else None

    checks = [
        (5, "Reallocated sectors", lambda v: FAIL if v > 100 else WARN if v > 0 else PASS),
        (197, "Pending (unstable) sectors", lambda v: FAIL if v > 0 else PASS),
        (198, "Uncorrectable sectors", lambda v: FAIL if v > 0 else PASS),
        (187, "Reported uncorrectable errors", lambda v: WARN if v > 0 else PASS),
        (10, "Spin retry count", lambda v: WARN if v > 0 else PASS),
        (199, "Cable / interface CRC errors", lambda v: WARN if v > 0 else PASS),
    ]
    for aid, name, judge in checks:
        v = raw(aid)
        if v is not None:
            st = judge(v)
            s.add("%s %s" % (tag, name), v, st)
            if aid == 199 and v > 0:
                s.note("%s CRC errors usually mean a bad SATA cable/connection, not a bad disk." % tag)
    for a in attrs:
        if (a.get("when_failed") or "") == "now":
            s.add("%s Attribute %s" % (tag, a.get("name")), "BELOW FAILURE THRESHOLD NOW", FAIL)
    wear = [a.get("value") for a in attrs if WEAR_ATTR.search(a.get("name", "")) and
            isinstance(a.get("value"), int) and 0 < a.get("value") <= 100]
    if wear and not disk.get("rota"):
        life = min(wear)
        s.add(tag + " Remaining life (SSD wear)", "%d %%" % life,
              FAIL if life < 10 else WARN if life < 30 else PASS)
        disk["health"] = "%d%%" % life
    errlog = dig(j, "ata_smart_error_log", "summary", "count") or \
        dig(j, "ata_smart_error_log", "extended", "count")
    if errlog:
        s.add(tag + " Logged drive errors", "%d errors in the drive's error log" % errlog, WARN)


def read_test(dev, size_mb, offset_mb):
    """Sequential READ of the raw device. Never writes. Returns (ok, mb_s, message)."""
    base = ["dd", "if=" + dev, "of=/dev/null", "bs=1M", "count=%d" % size_mb, "skip=%d" % offset_mb]
    timeout = int(size_mb / 5) + 30
    t = time.time()
    rc, out = run(base + ["iflag=direct", "status=none"], timeout=timeout)
    if rc != 0 and "Invalid argument" in out:
        t = time.time()
        rc, out = run(base + ["status=none"], timeout=timeout)
    el = max(0.001, time.time() - t)
    if rc == 124:
        return False, size_mb / el, "timed out (extremely slow read)"
    if rc != 0:
        return False, 0, (out.strip().splitlines() or ["read error"])[-1]
    return True, size_mb / el, ""


def test_storage(ctx):
    s = ctx.section("storage", "Storage (Disks)")
    disks = list_disks(ctx)
    ctx.raw["smart"] = ""
    if not disks:
        ctx.summary["storage"] = "None detected"
        s.add("Internal disks", "NO DISK DETECTED", WARN)
        s.note("No internal drive was found. If a drive is installed: check the cable / M.2 slot, "
               "and in BIOS set SATA mode to AHCI (Intel RST / RAID / VMD mode can hide NVMe drives).")
        return

    # wait for self-tests started at the beginning (full mode)
    if ctx.selftests:
        max_wait = ctx.num("disk_selftest_max_minutes", 6) * 60
        start = time.time()
        say("  Waiting for drive self-tests to finish (Ctrl+C to skip)...")
        try:
            while time.time() - start < max_wait:
                running = []
                for dev in ctx.selftests:
                    j, _ = smart_json(dev)
                    state, pct = selftest_state(j)
                    if state == "running":
                        running.append("%s %s%%" % (os.path.basename(dev), pct))
                if not running:
                    break
                progress("Disk self-test", time.time() - start, max_wait, " ".join(running))
                time.sleep(10)
        except KeyboardInterrupt:
            pass
        end_progress()

    summary = []
    rows = []
    read_mb = int(ctx.num("disk_read_full_mb" if ctx.full else "disk_read_quick_mb", 1024))
    for d in disks:
        dev = d["path"]
        tag = "[%s]" % d["name"]
        model = (d.get("model") or "").strip() or "Unknown model"
        size_txt = fmt_bytes(d["size"])
        say("  %s %s %s %s" % (col(tag, "1"), model, size_txt, d["kind"]))
        j, raw = smart_json(dev) if have("smartctl") else (None, "")
        ctx.raw["smart"] += "===== %s =====\n%s\n" % (dev, run(["smartctl", "-x", dev], timeout=60)[1])
        if j:
            model = j.get("model_name") or model
            serial = j.get("serial_number") or d.get("serial") or ""
            fw = j.get("firmware_version", "")
        else:
            serial, fw = d.get("serial") or "", ""
        s.add(tag + " Drive", "%s, %s, %s" % (model, size_txt, d["kind"]), PASS)
        if serial:
            s.add(tag + " Serial / firmware", "%s / %s" % (serial, fw or "?"))
        evaluate_smart(s, tag, j, d)

        if dev in ctx.selftests:
            j2, _ = smart_json(dev)
            state, text = selftest_state(j2)
            if state == "passed":
                s.add(tag + " Drive self-test (short)", "Passed", PASS)
            elif state == "failed":
                s.add(tag + " Drive self-test (short)", "FAILED: %s" % text, FAIL)
            elif state == "running":
                s.add(tag + " Drive self-test (short)", "Still running - result not available", INFO)
            else:
                s.add(tag + " Drive self-test (short)", "Result unknown %s" % text, INFO)

        # read tests (start / middle / end in full mode)
        size_mb = d["size"] // (1024 * 1024)
        chunk = max(16, min(read_mb, size_mb // 4))
        offsets = [0]
        if ctx.full and size_mb > chunk * 4:
            offsets += [size_mb // 2, size_mb - chunk - 8]
        speeds, errors = [], []
        try:
            for off in offsets:
                ok, mbs, msg = read_test(dev, chunk, off)
                if ok:
                    speeds.append(mbs)
                else:
                    errors.append("at %d MB: %s" % (off, msg))
        except KeyboardInterrupt:
            errors.append("skipped by user")
        if errors and errors != ["skipped by user"]:
            s.add(tag + " Read test", "READ ERRORS: " + "; ".join(errors), FAIL)
        elif speeds:
            spd = sum(speeds) / len(speeds)
            low = 250 if d["name"].startswith("nvme") else 40 if d["rota"] else 100
            st = WARN if (spd < low and "USB" not in d["kind"] and "eMMC" not in d["kind"]) else PASS
            s.add(tag + " Read test (%d MB x %d, read-only)" % (chunk, len(speeds)), "%.0f MB/s" % spd, st)
            d["speed"] = "%.0f MB/s" % spd
            if st == WARN:
                s.note("%s reads slower than expected for its type (worn drive, bad cable or "
                       "slow SATA mode)." % tag)

        rc, parts = run(["lsblk", "-n", "-o", "NAME,SIZE,FSTYPE,LABEL", dev])
        fs = set(re.findall(r"\b(ntfs|vfat|ext4|btrfs|xfs|exfat|LVM2_member|crypto_LUKS|BitLocker)\b", parts))
        if fs:
            hint = " - looks like Windows is installed" if "ntfs" in fs else ""
            s.add(tag + " Partitions / file systems", ", ".join(sorted(fs)) + hint)
        else:
            s.add(tag + " Partitions / file systems", "None (empty disk)")
        summary.append("%s %s" % (size_txt, d["kind"]))
        rows.append([d["name"], model, size_txt, d["kind"], d.get("health", "-"), d.get("speed", "-")])
    s.table("Detected drives", ["Device", "Model", "Size", "Type", "Life left", "Read speed"], rows)
    ctx.summary["storage"] = ", ".join(summary)


def lspci_devices():
    rc, out = run(["lspci", "-nnk"])
    devs, cur = [], None
    for line in out.splitlines():
        if line and not line[0].isspace():
            m = re.match(r"(\S+)\s+(.*?)\s\[([0-9a-f]{4})\]:\s+(.*)$", line)
            if m:
                cur = {"slot": m.group(1), "class": m.group(2), "classid": m.group(3),
                       "name": re.sub(r"\s*\[[0-9a-f]{4}:[0-9a-f]{4}\]", "", m.group(4)), "driver": None}
                devs.append(cur)
            else:
                cur = None
        elif cur and "Kernel driver in use:" in line:
            cur["driver"] = line.split(":", 1)[1].strip()
    return devs


def parse_edid(data):
    if len(data) < 128 or data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None
    mid = (data[8] << 8) | data[9]
    mfg = "".join(chr(((mid >> sh) & 0x1F) + 64) for sh in (10, 5, 0))
    name = ""
    for i in range(54, 126, 18):
        d = data[i:i + 18]
        if d[0] == 0 and d[1] == 0 and d[3] == 0xFC:
            name = d[5:18].split(b"\n")[0].decode("ascii", "replace").strip()
    res = ""
    d = data[54:72]
    if d[0] or d[1]:
        res = "%dx%d" % (d[2] | ((d[4] & 0xF0) << 4), d[5] | ((d[7] & 0xF0) << 4))
    size = ""
    if data[21] and data[22]:
        size = '%.1f"' % (((data[21] ** 2 + data[22] ** 2) ** 0.5) / 2.54)
    return mfg, name, res, size


def test_graphics(ctx):
    s = ctx.section("graphics", "Graphics & Display")
    gpus = [d for d in lspci_devices() if d["classid"].startswith("03")]
    names = []
    for g in gpus:
        names.append(g["name"])
        if g["driver"]:
            s.add("Graphics card", "%s (driver: %s)" % (g["name"], g["driver"]), PASS)
        else:
            s.add("Graphics card", "%s (no driver loaded)" % g["name"], INFO)
    if not gpus:
        s.add("Graphics card", "No PCI graphics device detected", WARN)
    ctx.summary["gpu"] = "; ".join(names) or "Unknown"

    rows = []
    for c in sorted(glob.glob("/sys/class/drm/card*-*")):
        conn = os.path.basename(c).split("-", 1)[1]
        status = rd(os.path.join(c, "status"), "unknown")
        edid_txt = ""
        try:
            with open(os.path.join(c, "edid"), "rb") as f:
                e = parse_edid(f.read())
            if e:
                edid_txt = " ".join(x for x in (e[0], e[1], e[3], e[2]) if x)
        except OSError:
            pass
        rows.append([conn, status, edid_txt])
        if status == "connected":
            internal = conn.startswith(("eDP", "LVDS", "DSI"))
            s.add("%s %s" % ("Built-in screen" if internal else "Display output", conn),
                  "connected" + (" - " + edid_txt if edid_txt else ""), PASS)
            if internal and not edid_txt:
                s.note("Built-in screen did not report its identity (EDID). Check the display cable.")
    s.table("Display connectors", ["Connector", "Status", "Monitor"], rows)
    if ctx.is_laptop and rows and not any(r[0].startswith(("eDP", "LVDS", "DSI")) and r[1] == "connected"
                                          for r in rows):
        s.add("Built-in screen", "Not detected (screen cable or panel fault, or lid closed)", WARN)


def test_network(ctx):
    s = ctx.section("network", "Network, Wi-Fi & Bluetooth")
    run(["rfkill", "unblock", "all"])
    rc, rf = run(["rfkill", "list"])
    ctx.raw["rfkill"] = rf
    if re.search(r"Hard blocked:\s*yes", rf):
        s.add("Radio switch", "Wireless is HARD-BLOCKED (physical switch / Fn key / BIOS)", WARN)

    ifaces = [os.path.basename(p) for p in sorted(glob.glob("/sys/class/net/*"))]
    ifaces = [i for i in ifaces if i != "lo" and os.path.exists("/sys/class/net/%s/device" % i)]
    if not ifaces:
        s.add("Network adapters", "No wired or wireless adapter detected", WARN)
    wired_up = False
    for i in ifaces:
        base = "/sys/class/net/" + i
        drv = os.path.basename(os.path.realpath(base + "/device/driver")) if os.path.exists(base + "/device/driver") else "?"
        mac = rd(base + "/address", "?")
        wireless = os.path.isdir(base + "/wireless") or os.path.isdir(base + "/phy80211")
        run(["ip", "link", "set", i, "up"])
        if wireless:
            s.add("Wi-Fi adapter %s" % i, "present (driver %s, MAC %s)" % (drv, mac), PASS)
            nets, best = {}, None
            if have("iw"):
                for attempt in range(3):
                    rc, out = run(["iw", "dev", i, "scan"], timeout=30)
                    if rc == 0 and "BSS" in out:
                        cur_sig = None
                        for line in out.splitlines():
                            m = re.search(r"signal:\s*(-?[\d.]+)", line)
                            if m:
                                cur_sig = float(m.group(1))
                            m = re.search(r"^\s*SSID:\s*(.*)$", line)
                            if m and m.group(1).strip():
                                nets[m.group(1).strip()] = cur_sig
                        break
                    time.sleep(3)
                sigs = [v for v in nets.values() if v is not None]
                best = max(sigs) if sigs else None
                if nets:
                    s.add("Wi-Fi scan %s" % i, "%d networks found (strongest %s dBm)" %
                          (len(nets), "%.0f" % best if best is not None else "?"), PASS)
                else:
                    s.add("Wi-Fi scan %s" % i, "No networks found (antenna/adapter problem, or no Wi-Fi nearby)", WARN)
        else:
            time.sleep(0.5)
            carrier = rd(base + "/carrier", "0") == "1"
            speed = rd_int(base + "/speed")
            if carrier:
                wired_up = True
                s.add("Ethernet %s" % i, "cable connected, link %s Mb/s (driver %s)" %
                      (speed if speed and speed > 0 else "?", drv), PASS)
            else:
                s.add("Ethernet %s" % i, "present (driver %s, MAC %s) - no cable connected" % (drv, mac), INFO)
    if wired_up or ifaces:
        rc, _ = run(["ping", "-c", "2", "-W", "3", "1.1.1.1"], timeout=15)
        if rc == 0:
            s.add("Internet connection", "Working", PASS)
        elif wired_up:
            time.sleep(5)       # DHCP may still be in progress
            rc, _ = run(["ping", "-c", "2", "-W", "3", "1.1.1.1"], timeout=15)
            s.add("Internet connection", "Working" if rc == 0 else "No internet (link is up)", PASS if rc == 0 else INFO)

    bt = sorted(glob.glob("/sys/class/bluetooth/hci*"))
    if bt:
        s.add("Bluetooth", "Adapter present (%s)" % ", ".join(os.path.basename(b) for b in bt), PASS)
    else:
        rc, out = run(["lsusb"])
        if re.search(r"bluetooth", out, re.I):
            s.add("Bluetooth", "Adapter found on USB (not initialised)", INFO)
        else:
            s.add("Bluetooth", "Not detected", INFO)


def test_battery(ctx):
    s = ctx.section("battery", "Battery & Power")
    found = False
    for ps in sorted(glob.glob("/sys/class/power_supply/*")):
        typ = rd(ps + "/type", "")
        name = os.path.basename(ps)
        if typ == "Mains":
            online = rd(ps + "/online", "?")
            s.add("Charger / AC adapter (%s)" % name, "connected" if online == "1" else "not connected", INFO)
        elif typ == "Battery" and rd(ps + "/scope", "") != "Device":
            found = True
            if rd(ps + "/present", "1") != "1":
                s.add("Battery %s" % name, "Slot present but NO BATTERY inserted", WARN)
                continue
            full = rd_int(ps + "/energy_full") or rd_int(ps + "/charge_full")
            design = rd_int(ps + "/energy_full_design") or rd_int(ps + "/charge_full_design")
            unit = "Wh" if rd_int(ps + "/energy_full") else "Ah"
            ident = " ".join(x for x in (rd(ps + "/manufacturer", ""), rd(ps + "/model_name", ""),
                                         rd(ps + "/technology", "")) if x)
            s.add("Battery %s" % name, ident or "present", PASS)
            if full and design:
                health = full * 100.0 / design
                st = FAIL if health < 50 else WARN if health < 75 else PASS
                s.add("Battery health (wear)", "%.0f %% of original capacity (%.1f / %.1f %s)" %
                      (health, full / 1e6, design / 1e6, unit), st)
                ctx.summary["battery"] = "%.0f%% health" % health
                if st != PASS:
                    s.note("Battery has lost a lot of capacity - consider replacing it.")
            cap = rd_int(ps + "/capacity")
            if cap is not None:
                s.add("Current charge", "%d %% (%s)" % (cap, rd(ps + "/status", "?")))
            cyc = rd_int(ps + "/cycle_count")
            if cyc:
                s.add("Charge cycles", cyc, WARN if cyc > 1000 else INFO)
            st = rd(ps + "/status", "")
            if st == "Not charging" and cap is not None and cap < 90:
                s.add("Charging", "Battery is NOT charging although below 90 %", WARN)
    if not found:
        if ctx.is_laptop:
            s.add("Battery", "No battery detected on a laptop", WARN)
        else:
            s.add("Battery", "None (desktop computer)", INFO)
            ctx.summary["battery"] = "n/a"


def test_sensors(ctx):
    s = ctx.section("sensors", "Temperatures & Fans")
    temps, fans = hwmon_readings()
    ctx.raw["sensors"] = run(["sensors"])[1]
    rows = [[n, l, "%.0f °C" % t] for n, l, t in temps]
    for n, t in thermal_zones():
        rows.append(["thermal zone", n, "%.0f °C" % t])
    s.table("Temperature sensors (now)", ["Sensor", "Label", "Value"], rows)
    hot = [r for r in temps if r[2] >= 90]
    if temps:
        mx = max(temps, key=lambda r: r[2])
        s.add("Temperature sensors", "%d readings, highest %.0f °C (%s %s)" % (len(temps), mx[2], mx[0], mx[1]),
              WARN if hot else PASS)
    else:
        s.add("Temperature sensors", "None readable", INFO)
    if fans:
        s.table("Fans", ["Sensor", "Fan", "RPM"], [[n, l, v] for n, l, v in fans])
        spinning = [f for f in fans if f[2] > 0]
        s.add("Fans", "%d detected, %d spinning (%s)" % (len(fans), len(spinning),
                                                        ", ".join("%s %d rpm" % (f[1], f[2]) for f in fans)),
              PASS if spinning else INFO)
        if not spinning:
            s.note("Fans report 0 RPM. Some fans stop when cool - check manually that the fan spins under load.")
    else:
        s.add("Fans", "No fan speed sensor readable (common on laptops)", INFO)


def test_audio(ctx):
    s = ctx.section("audio", "Audio")
    cards = rd("/proc/asound/cards", "") or ""
    found = re.findall(r"^\s*(\d+)\s+\[(.*?)\s*\]:\s*(.*)$", cards, re.M)
    if not found:
        s.add("Sound card", "No sound device detected", WARN)
        return
    for num, short, desc in found:
        s.add("Sound card %s" % num, desc.strip(), PASS)
    codecs = set()
    for f in glob.glob("/proc/asound/card*/codec#*"):
        m = re.search(r"^Codec:\s*(.+)$", rd(f, "") or "", re.M)
        if m:
            codecs.add(m.group(1).strip())
    if codecs:
        s.add("Audio codecs", ", ".join(sorted(codecs)))
    ctx.raw["aplay"] = run(["aplay", "-l"])[1] + "\n" + run(["arecord", "-l"])[1]


def test_devices(ctx):
    s = ctx.section("devices", "Keyboard, Touchpad, Camera & USB")
    text = rd("/proc/bus/input/devices", "") or ""
    kbd, pads, mice = [], [], []
    for block in text.split("\n\n"):
        m = re.search(r'N: Name="(.*)"', block)
        if not m:
            continue
        name = m.group(1)
        handlers = (re.search(r"H: Handlers=(.*)", block) or [None, ""])[1]
        ev = (re.search(r"B: EV=(\w+)", block) or [None, ""])[1]
        if re.search(r"touchpad|trackpad|synaptics|glidepoint|alps|elan.*(pad|0[0-9a-f]{3})", name, re.I):
            pads.append(name)
        elif "kbd" in handlers and ev in ("120013", "12001f", "100013") and \
                not re.search(r"button|video|hotkey|wmi|consumer|system control|speaker|headphone|mic", name, re.I):
            kbd.append(name)
        elif "mouse" in handlers:
            mice.append(name)
    s.add("Keyboard", ", ".join(kbd) if kbd else "None detected", PASS if kbd else (WARN if ctx.is_laptop else INFO))
    if ctx.is_laptop or pads:
        s.add("Touchpad", ", ".join(pads) if pads else "Not detected", PASS if pads else WARN)
    if mice:
        s.add("Mouse / pointing devices", ", ".join(mice))

    cams = []
    for v in sorted(glob.glob("/sys/class/video4linux/video*")):
        n = rd(v + "/name", "")
        if n and n not in cams:
            cams.append(n)
    if cams:
        s.add("Webcam", ", ".join(cams), PASS)
    else:
        s.add("Webcam", "Not detected" + (" (may be disabled in BIOS or by a privacy switch)" if ctx.is_laptop else ""),
              INFO)

    if glob.glob("/sys/class/mmc_host/*") or re.search(r"card reader", ctx.raw.get("lspci", "") +
                                                        ctx.raw.get("lsusb", ""), re.I):
        s.add("Card reader", "Present", PASS)

    rc, out = run(["lsusb"])
    usb_rows = []
    for line in out.splitlines():
        m = re.match(r"Bus (\d+) Device (\d+): ID (\S+)\s*(.*)", line)
        if m and not re.search(r"root hub", m.group(4), re.I):
            usb_rows.append([m.group(1), m.group(3), m.group(4)])
    hubs = len([l for l in out.splitlines() if "root hub" in l])
    s.add("USB", "%d USB buses, %d devices connected" % (hubs, len(usb_rows)), PASS if hubs else WARN)
    s.table("USB devices", ["Bus", "ID", "Device"], usb_rows)

    nodrv = [d for d in lspci_devices() if not d["driver"] and not d["classid"].startswith(("06", "08", "0c05"))]
    if nodrv:
        s.table("PCI devices without a Linux driver (usually harmless)", ["Slot", "Class", "Device"],
                [[d["slot"], d["class"], d["name"]] for d in nodrv])


KLOG_PATTERNS = [
    (FAIL, "CPU / hardware machine-check error (MCE)", r"\[Hardware Error\]|Machine check events logged|mce: .*error"),
    (FAIL, "Memory ECC error (EDAC)", r"EDAC .*(\bCE\b|\bUE\b|error)"),
    (WARN, "Disk I/O error", r"I/O error, dev (?!sr|loop|fd)|critical medium error|Unrecovered read error"),
    (WARN, "SATA link / command error", r"ata\d+(\.\d+)?: (failed command|exception Emask|COMRESET failed|hard resetting link)"),
    (WARN, "NVMe controller timeout / reset", r"nvme\d*.*(timeout|resetting controller|controller is down|Device not ready)"),
    (WARN, "PCIe bus error", r"PCIe Bus Error|AER: (Corrected|Uncorrected|Multiple)"),
    (WARN, "Graphics driver error / GPU hang", r"GPU HANG|gpu hang|ring \S+ timeout|amdgpu.*\*ERROR\*|i915.*\*ERROR\*|nouveau.*(fault|error)"),
    (WARN, "Overheating event", r"temperature above threshold|critical temperature reached|Package temperature.*throttled"),
    (WARN, "USB device errors", r"device descriptor read/\w+, error|device not accepting address|unable to enumerate USB device"),
]


def test_kernel_log(ctx):
    s = ctx.section("kernel", "System Error Log")
    rc, out = run(["dmesg"])
    if rc != 0 or not out:
        rc, out = run(["journalctl", "-k", "-b", "--no-pager"])
    ctx.raw["dmesg"] = out
    if not out:
        s.add("Kernel log", "Not readable", SKIP)
        return
    rows = []
    any_hit = False
    for status, label, pat in KLOG_PATTERNS:
        hits = [l for l in out.splitlines() if re.search(pat, l)]
        if hits:
            any_hit = True
            s.add(label, "%d messages" % len(hits), status)
            for h in hits[:5]:
                rows.append([label, h.strip()[:220]])
    if not any_hit:
        s.add("Hardware errors in system log", "None found", PASS)
    s.table("Example log lines", ["Problem", "Message"], rows)


# --------------------------------------------------------------------------
# Interactive tests (only when a technician is present)
# --------------------------------------------------------------------------
def test_interactive(ctx):
    s = ctx.section("interactive", "Interactive Checks (technician)")
    say("\a")
    say(col("  The automatic tests are done. Please answer the following checks.", "1;33"))

    # Keyboard
    target = "1234567890 qwertyuiop asdfghjkl zxcvbnm"
    say("\n  KEYBOARD: type exactly this line and press Enter (or just Enter to skip):")
    say("    " + col(target, "1"))
    typed = ask("    > ").lower()
    if typed:
        missing = sorted(set(target.replace(" ", "")) - set(typed))
        if missing:
            say("  Missing keys: %s - try again (Enter to skip)." % " ".join(missing))
            again = ask("    > ").lower()
            if again:
                missing = sorted(set(missing) - set(again))
        s.add("Keyboard keys (letters & numbers)", "Missing / not working: " + " ".join(missing) if missing
              else "All tested keys work", FAIL if missing else PASS)
    else:
        s.add("Keyboard keys", "Skipped", SKIP)

    # Screen
    if ask_yes_no("\n  SCREEN: show full-screen colours to look for dead pixels / lines?"):
        cols, rows = shutil.get_terminal_size((80, 25))
        for name, code in (("RED", "41"), ("GREEN", "42"), ("BLUE", "44"), ("WHITE", "47"), ("BLACK", "40")):
            sys.stdout.write("\033[2J\033[H")
            for r in range(rows - 1):
                sys.stdout.write("\033[%sm%s" % (code, " " * cols))
            sys.stdout.write("\033[0m")
            sys.stdout.flush()
            ask("\033[%d;1H\033[0m%s - press Enter for next colour" % (rows, name))
        sys.stdout.write("\033[0m\033[2J\033[H")
        ok = ask_yes_no("  Was the screen clean (no dead pixels, lines, spots or flicker)?")
        if ok is not None:
            s.add("Screen (colour test)", "No defects seen" if ok else "DEFECTS SEEN by technician", PASS if ok else FAIL)

    # Speakers
    if rd("/proc/asound/cards", "").strip() and have("speaker-test"):
        if ask_yes_no("\n  SPEAKERS: play a test sound (left / right voice)?"):
            for ctl in ("Master", "Speaker", "PCM", "Headphone", "Front"):
                run(["amixer", "-q", "sset", ctl, "85%", "unmute"])
            run(["speaker-test", "-c", "2", "-t", "wav", "-l", "1"], timeout=25)
            ok = ask_yes_no("  Did you hear 'Front Left' from the left and 'Front Right' from the right?")
            if ok is not None:
                s.add("Speakers", "Working" if ok else "NO SOUND / wrong channel", PASS if ok else FAIL)
        if have("arecord") and ask_yes_no("\n  MICROPHONE: record 4 seconds and play it back?"):
            run(["amixer", "-q", "sset", "Capture", "80%", "cap"])
            say("  Speak now...")
            run(["arecord", "-q", "-d", "4", "-f", "cd", "/tmp/pctest_mic.wav"], timeout=15)
            say("  Playing back...")
            run(["aplay", "-q", "/tmp/pctest_mic.wav"], timeout=15)
            ok = ask_yes_no("  Did you hear your recording?")
            if ok is not None:
                s.add("Microphone", "Working" if ok else "NOT WORKING", PASS if ok else FAIL)

    # USB ports
    if ask_yes_no("\n  USB PORTS: test ports by plugging a device (e.g. a mouse) into each one?"):
        def usb_set():
            out = {}
            for p in glob.glob("/sys/bus/usb/devices/*"):
                n = os.path.basename(p)
                if re.match(r"^\d+-[\d.]+$", n):
                    out[n] = rd(p + "/product", "") or rd(p + "/idProduct", "?")
            return out
        say("  Plug and unplug a device in every port. Each detection is shown. Press Enter when finished.")
        seen = usb_set()
        detected = []
        while True:
            ready, _, _ = select.select([sys.stdin], [], [], 0.5)
            if ready:
                sys.stdin.readline()
                break
            now = usb_set()
            for port, prod in now.items():
                if port not in seen:
                    say("    " + col("detected", "1;32") + " port %s: %s" % (port, prod))
                    detected.append(port)
            seen = now
        ports = sorted(set(detected))
        s.add("USB ports", "%d port(s) detected a device: %s" % (len(ports), ", ".join(ports)) if ports
              else "No device detected in any port", PASS if ports else WARN)
        ok = ask_yes_no("  Did every port you tested show 'detected'?")
        if ok is not None:
            s.add("USB ports (technician)", "All tested ports work" if ok else "SOME PORTS NOT WORKING",
                  PASS if ok else FAIL)

    if not s.items:
        s.add("Interactive checks", "Skipped", SKIP)


# --------------------------------------------------------------------------
# Report output
# --------------------------------------------------------------------------
def overall(ctx):
    r = max([RANK[s.status] for s in ctx.sections] or [0])
    return FAIL if r == 3 else WARN if r == 2 else PASS


VERDICT_TEXT = {
    PASS: "All tested hardware is working correctly.",
    WARN: "The computer works, but some items need attention (see warnings).",
    FAIL: "Hardware problems were found (see failed items).",
}


def find_output_root():
    """Find a writable place ON THE USB STICK (never on the computer's own disks)."""
    def writable(path):
        try:
            test = os.path.join(path, ".pctest_write_test")
            with open(test, "w") as f:
                f.write("ok")
            os.remove(test)
            return True
        except OSError:
            return False

    if os.path.ismount(BOOTMNT):
        run(["mount", "-o", "remount,rw", BOOTMNT])
        if writable(BOOTMNT):
            return BOOTMNT, True
    rows = lsblk_flat("NAME,PKNAME,FSTYPE,MOUNTPOINT,LABEL,TYPE,TRAN,RM")
    by_name = {r.get("name"): r for r in rows}
    boot = detect_boot_disks()
    for prefer_boot in (True, False):
        for r in rows:
            if r.get("type") != "part" or (r.get("fstype") or "") not in ("vfat", "exfat", "ntfs", "ntfs3"):
                continue
            parent = by_name.get(r.get("pkname"), {})
            is_boot = os.path.basename(r.get("pkname") or "") in boot
            removable = (parent.get("tran") == "usb") or str(parent.get("rm")).lower() in ("1", "true")
            if prefer_boot != is_boot or not (is_boot or removable):
                continue
            mp = r.get("mountpoint")
            if mp:
                run(["mount", "-o", "remount,rw", mp])
            else:
                mp = "/mnt/pctest_out_" + os.path.basename(r["name"])
                os.makedirs(mp, exist_ok=True)
                rc, _ = run(["mount", "-o", "rw", r["name"], mp])
                if rc != 0:
                    continue
            if writable(mp):
                return mp, True
    return "/root", False


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:40] or "unknown"


def write_reports(ctx, out_root):
    stamp = ctx.started.strftime("%Y%m%d-%H%M%S")
    folder = os.path.join(out_root, "PC-Reports", "%s_%s_%s" % (
        stamp, safe_name(ctx.summary.get("model", "PC")), safe_name(ctx.summary.get("serial", ""))))
    os.makedirs(os.path.join(folder, "raw"), exist_ok=True)
    for name, text in ctx.raw.items():
        if text:
            with open(os.path.join(folder, "raw", safe_name(name) + ".txt"), "w", errors="replace") as f:
                f.write(text)
    txt = text_report(ctx)
    with open(os.path.join(folder, "report.txt"), "w", errors="replace") as f:
        f.write(txt)
    with open(os.path.join(folder, "report.html"), "w", errors="replace") as f:
        f.write(html_report(ctx))
    index = os.path.join(out_root, "PC-Reports", "index.csv")
    new = not os.path.exists(index)
    with open(index, "a", errors="replace") as f:
        if new:
            f.write("date,result,manufacturer,model,serial,cpu,ram,storage,battery,folder\n")
        vals = [ctx.started.strftime("%Y-%m-%d %H:%M"), overall(ctx)] + \
               [ctx.summary.get(k, "") for k in ("manufacturer", "model", "serial", "cpu", "ram", "storage", "battery")] + \
               [os.path.basename(folder)]
        f.write(",".join('"%s"' % str(v).replace('"', "'") for v in vals) + "\n")
    run(["sync"])
    return folder


def text_report(ctx):
    L = []
    L.append("=" * 78)
    L.append("PC HARDWARE TEST REPORT".center(78))
    company = ctx.cfg.get("report", "company_name", fallback="")
    if company:
        L.append(company.center(78))
    L.append("=" * 78)
    for k, lbl in (("manufacturer", "Manufacturer"), ("model", "Model"), ("serial", "Serial"),
                   ("cpu", "CPU"), ("ram", "RAM"), ("storage", "Storage"), ("gpu", "Graphics"),
                   ("battery", "Battery")):
        if ctx.summary.get(k):
            L.append("%-13s %s" % (lbl + ":", ctx.summary[k]))
    L.append("%-13s %s" % ("Date:", ctx.started.strftime("%Y-%m-%d %H:%M")))
    L.append("")
    L.append("OVERALL RESULT: %s - %s" % (overall(ctx), VERDICT_TEXT[overall(ctx)]))
    L.append("")
    for s in ctx.sections:
        L.append("-" * 78)
        L.append("[%s] %s" % (s.status, s.title))
        L.append("-" * 78)
        for label, value, st in s.items:
            L.append("  [%-4s] %s: %s" % (st, label, value))
        for title, headers, rows in s.tables:
            L.append("")
            L.append("  " + title)
            widths = [max(len(str(x)) for x in [h] + [r[i] for r in rows]) for i, h in enumerate(headers)]
            widths = [min(w, 40) for w in widths]
            L.append("    " + "  ".join(str(h).ljust(w) for h, w in zip(headers, widths)))
            for r in rows:
                L.append("    " + "  ".join(str(x)[:40].ljust(w) for x, w in zip(r, widths)))
        for n in s.notes:
            L.append("  NOTE: " + n)
        L.append("")
    L.append("Generated by PC Hardware Test v%s" % VERSION)
    return "\n".join(L) + "\n"


HTML_CSS = """
:root{--bg:#f3f5f8;--card:#fff;--text:#1d2330;--muted:#5d6678;--line:#e4e8ef;--head:#1d2b45}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 "Segoe UI",Arial,Helvetica,sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:20px 16px}
header{background:var(--head);color:#fff;border-radius:12px;padding:20px 24px}
header h1{margin:0;font-size:22px}
header .co{opacity:.8;font-size:13px;margin-top:2px}
.ident{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:6px 24px;margin-top:14px;font-size:13px}
.ident b{display:block;color:#aab6cc;font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.verdict{border-radius:12px;padding:16px 22px;margin:16px 0;font-size:17px;font-weight:600;display:flex;gap:14px;align-items:center}
.verdict.PASS{background:#d9f4e2;color:#0f5e2c}.verdict.WARN{background:#fff0c7;color:#7a4f00}.verdict.FAIL{background:#fddcdc;color:#9a1717}
.verdict .big{font-size:26px;font-weight:800}
.card{background:var(--card);border-radius:12px;padding:16px 20px;margin:14px 0;box-shadow:0 1px 3px rgba(0,0,0,.07)}
.card h2{margin:0 0 10px;font-size:16px;display:flex;justify-content:space-between;align-items:center;gap:8px}
.badge{display:inline-block;min-width:46px;text-align:center;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700}
.PASS .badge,.badge.PASS{background:#d9f4e2;color:#0f5e2c}.badge.WARN{background:#fff0c7;color:#7a4f00}
.badge.FAIL{background:#fddcdc;color:#9a1717}.badge.INFO{background:#e2edfc;color:#1a4b94}.badge.SKIP{background:#eceef1;color:#666}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse}
td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600}
td.l{color:var(--muted);width:38%}
tr.FAIL td{background:#fff5f5}tr.WARN td{background:#fffbef}
h3{font-size:13px;margin:14px 0 4px;color:var(--muted)}
.note{background:#f6f8fb;border-left:3px solid #8aa2c8;padding:6px 10px;margin-top:8px;font-size:13px}
.summary td{border:none;padding:4px 8px}
footer{color:var(--muted);font-size:12px;text-align:center;margin:20px 0}
.sign{display:grid;grid-template-columns:1fr 1fr;gap:30px;margin-top:30px}
.sign div{border-top:1px solid #999;padding-top:4px;font-size:12px;color:var(--muted)}
@media print{body{background:#fff}.card{box-shadow:none;border:1px solid #ddd;break-inside:avoid}header{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
"""


def html_report(ctx):
    e = html.escape
    ov = overall(ctx)
    company = ctx.cfg.get("report", "company_name", fallback="")
    tech = ctx.cfg.get("report", "technician", fallback="")
    P = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width,initial-scale=1">',
         "<title>PC Test Report - %s</title><style>%s</style></head><body><div class=wrap>" %
         (e(ctx.summary.get("model", "")), HTML_CSS)]
    P.append("<header><h1>PC Hardware Test Report</h1>")
    if company:
        P.append("<div class=co>%s</div>" % e(company))
    P.append("<div class=ident>")
    for k, lbl in (("manufacturer", "Manufacturer"), ("model", "Model"), ("serial", "Serial number"),
                   ("cpu", "Processor"), ("ram", "Memory"), ("storage", "Storage"), ("gpu", "Graphics"),
                   ("battery", "Battery")):
        if ctx.summary.get(k):
            P.append("<div><b>%s</b>%s</div>" % (lbl, e(ctx.summary[k])))
    P.append("<div><b>Tested</b>%s (%s mode)</div></div></header>" %
             (ctx.started.strftime("%Y-%m-%d %H:%M"), e(ctx.mode)))
    P.append('<div class="verdict %s"><span class=big>%s</span><span>%s</span></div>' %
             (ov, {"PASS": "PASSED", "WARN": "ATTENTION", "FAIL": "FAILED"}[ov], e(VERDICT_TEXT[ov])))

    P.append("<div class=card><h2>Summary</h2><table class=summary>")
    for s in ctx.sections:
        P.append('<tr><td><span class="badge %s">%s</span></td><td><a href="#%s">%s</a></td></tr>' %
                 (s.status, s.status, s.key, e(s.title)))
    P.append("</table></div>")

    for s in ctx.sections:
        P.append('<div class=card id="%s"><h2>%s <span class="badge %s">%s</span></h2>' %
                 (s.key, e(s.title), s.status, s.status))
        if s.items:
            P.append("<div class=scroll><table>")
            for label, value, st in s.items:
                P.append('<tr class="%s"><td class=l>%s</td><td>%s</td><td style="width:60px">'
                         '<span class="badge %s">%s</span></td></tr>' % (st, e(label), e(value), st, st))
            P.append("</table></div>")
        for title, headers, rows in s.tables:
            P.append("<h3>%s</h3><div class=scroll><table><tr>%s</tr>" %
                     (e(title), "".join("<th>%s</th>" % e(str(h)) for h in headers)))
            for r in rows:
                P.append("<tr>%s</tr>" % "".join("<td>%s</td>" % e(str(x)) for x in r))
            P.append("</table></div>")
        for n in s.notes:
            P.append("<div class=note>%s</div>" % e(n))
        P.append("</div>")

    P.append('<div class=card><h2>Technician notes</h2><div style="min-height:60px"></div>'
             '<div class=sign><div>Technician: %s</div><div>Signature / date</div></div></div>' % e(tech))
    P.append("<footer>Generated by PC Hardware Test v%s &middot; raw logs are in the <code>raw</code> folder"
             "</footer></div></body></html>" % VERSION)
    return "\n".join(P)


# --------------------------------------------------------------------------
# Main flow
# --------------------------------------------------------------------------
TESTS = [
    ("System information", test_system),
    ("Processor", test_cpu),
    ("Memory", test_memory),
    ("Storage", test_storage),
    ("Graphics & display", test_graphics),
    ("Network", test_network),
    ("Battery", test_battery),
    ("Temperatures & fans", test_sensors),
    ("Audio", test_audio),
    ("Devices", test_devices),
    ("System error log", test_kernel_log),
]


def run_all(ctx):
    tests = list(TESTS)
    if ctx.interactive:
        tests.append(("Interactive checks", test_interactive))
    for i, (name, fn) in enumerate(tests, 1):
        say("\n" + col("=== [%d/%d] %s " % (i, len(tests), name), "1;34") + col("=" * max(0, 50 - len(name)), "1;34"))
        before = len(ctx.sections)
        t = time.time()
        try:
            if fn is test_cpu:
                start_disk_selftests(ctx)
            fn(ctx)
        except KeyboardInterrupt:
            end_progress()
            sec = ctx.sections[-1] if len(ctx.sections) > before else ctx.section(name.lower(), name)
            sec.add("Test", "Skipped by user (Ctrl+C)", SKIP)
            time.sleep(0.5)
        except Exception as ex:
            sec = ctx.sections[-1] if len(ctx.sections) > before else ctx.section(name.lower(), name)
            sec.add("Test error", "%s: %s" % (type(ex).__name__, ex), WARN)
            ctx.raw["error_" + name] = traceback.format_exc()
        if len(ctx.sections) > before:
            ctx.sections[-1].seconds = time.time() - t


def print_summary(ctx):
    say("\n" + col("=" * 70, "1"))
    say(col("  RESULTS", "1"))
    say(col("=" * 70, "1"))
    for s in ctx.sections:
        say("  %s %s" % (badge(s.status), s.title))
        for label, value, st in s.items:
            if st in (WARN, FAIL):
                say("        - %s: %s" % (label, value))
    ov = overall(ctx)
    say("")
    say("  OVERALL: %s  %s" % (badge(ov), VERDICT_TEXT[ov]))


def open_graphical(path):
    if not have("startx"):
        say("  Graphical desktop not available.")
        return
    browser = "firefox" if have("firefox") else ("chromium" if have("chromium") else None)
    if not browser:
        say("  No web browser available.")
        return
    autostart = os.path.expanduser("~/.config/autostart")
    os.makedirs(autostart, exist_ok=True)
    with open(os.path.join(autostart, "pctest-report.desktop"), "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=PC Test Report\n"
                "Exec=%s file://%s\n" % (browser, path))
    say("  Starting desktop... close the desktop (Log Out) to return here.")
    subprocess.call(["startx"])


def end_menu(ctx, folder, saved):
    report_txt = os.path.join(folder, "report.txt")
    report_html = os.path.join(folder, "report.html")
    while True:
        say("")
        say(col("  What next?", "1"))
        say("   [v] View the text report      [g] Open the HTML report (graphical desktop)")
        say("   [s] Exit to command shell     [r] Reboot     [p] Power off")
        c = ask("  Choice: ").lower()
        if c == "v":
            pager = "less" if have("less") else "more"
            subprocess.call([pager, "-R", report_txt] if pager == "less" else [pager, report_txt])
        elif c == "g":
            open_graphical(report_html)
        elif c == "s" or c == "":
            if not sys.stdin.isatty() or c == "s":
                say("  Type 'pctest' to run the test again.")
                return
        elif c == "r":
            run(["sync"])
            subprocess.call(["systemctl", "reboot"])
            return
        elif c == "p":
            run(["sync"])
            subprocess.call(["systemctl", "poweroff"])
            return


def load_config():
    cfg = configparser.ConfigParser()
    cfg.read_dict(DEFAULT_CONFIG)
    cfg.read(os.path.join(HERE, "pctest.conf"), encoding="utf-8")
    return cfg


def main():
    ap = argparse.ArgumentParser(description="PC Hardware Test")
    ap.add_argument("--mode", choices=["quick", "full"], help="test mode (skips the menu)")
    ap.add_argument("--interactive", action="store_true", help="include technician checks")
    ap.add_argument("-y", "--yes", action="store_true", help="no menus at all (for unattended use)")
    ap.add_argument("--out", help="directory to save reports in (default: the USB stick)")
    ap.add_argument("--cpu-seconds", type=int, help="override CPU stress duration")
    ap.add_argument("--ram-seconds", type=int, help="override quick RAM test duration")
    args = ap.parse_args()

    cfg = load_config()
    if args.cpu_seconds:
        cfg.set("tests", "cpu_stress_quick_seconds", str(args.cpu_seconds))
        cfg.set("tests", "cpu_stress_full_seconds", str(args.cpu_seconds))
    if args.ram_seconds:
        cfg.set("tests", "ram_quick_seconds", str(args.ram_seconds))

    say(col("\n  ================================================", "1;36"))
    say(col("        PC HARDWARE TEST  v%s" % VERSION, "1;36"))
    company = cfg.get("report", "company_name", fallback="")
    if company:
        say(col("        " + company, "1;36"))
    say(col("  ================================================", "1;36"))
    say("  Internal disks are only READ - nothing on this computer is changed.")
    if os.geteuid() != 0:
        say(col("  WARNING: not running as root - many tests will be limited.", "1;33"))

    mode, interactive = args.mode, args.interactive
    if not mode:
        try:
            timeout = int(cfg.get("menu", "timeout_seconds"))
        except ValueError:
            timeout = 20
        default = cfg.get("menu", "default_choice", fallback="1")
        say("")
        say("   [1] Quick test           (~4 minutes)")
        say("   [2] Quick test + interactive checks (keyboard, screen, speakers, USB)")
        say("   [3] Full test            (~45 minutes: long CPU/RAM test + disk self-test)")
        say("   [4] Full test + interactive checks")
        say("   [5] Exit to command shell")
        c = default if args.yes else ask("\n  Choose 1-5 (starts option %s automatically in %d s): " % (default, timeout),
                                        timeout=timeout, default=default)
        c = c or default
        if c == "5":
            say("  Type 'pctest' to start the test later.")
            return 0
        mode = "full" if c in ("3", "4") else "quick"
        interactive = c in ("2", "4")
    if args.yes or not sys.stdin.isatty():
        interactive = False

    ctx = Ctx(cfg, mode, interactive)
    ctx.boot_disks = detect_boot_disks()
    if ctx.boot_disks:
        say("  Boot USB stick: %s (excluded from disk tests)" % ", ".join(sorted(ctx.boot_disks)))

    run_all(ctx)
    print_summary(ctx)

    if args.out:
        os.makedirs(args.out, exist_ok=True)
        out_root, on_usb = args.out, True
    else:
        out_root, on_usb = find_output_root()
    try:
        folder = write_reports(ctx, out_root)
        say("\n  Report saved to: " + col(folder, "1"))
        if not on_usb:
            say(col("  WARNING: the USB stick is not writable - the report is only in RAM and will be "
                    "lost at power off.\n  Plug in a second (FAT32/exFAT) USB stick and run 'pctest' again, "
                    "or copy the folder now.", "1;33"))
        else:
            say("  On Windows open:  <USB drive>\\PC-Reports\\%s\\report.html" % os.path.basename(folder))
    except OSError as ex:
        say(col("  Could not save report: %s" % ex, "1;31"))
        folder = "/tmp"
    if not args.yes:
        end_menu(ctx, folder, on_usb)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\n  Aborted.")
        sys.exit(130)
