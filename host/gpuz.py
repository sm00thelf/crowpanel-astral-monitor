#!/usr/bin/env python3
"""ASUS GPU Tweak III / GPU-Z panel for Linux.

Reproduces the data model documented in ASUSGPUZ-RE.md on native Linux sources:
nvidia-smi (NVML) for the GPU, the driver's Linux NVAPI (libnvidia-api.so.1) for what
NVML lacks, the board MCU on the GPU's I2C bus for the ASTRAL extras, sysfs for PCI
topology, hwmon for board sensors, and the vendor's own InitData_Card1.ini when it
is present next to this script.

Monitor rows GPU Tweak III itself leaves empty on this card are listed, not hidden --
see section 7.2 of ASUSGPUZ-RE.md.

  ./gpuz.py              static panel + live sensors, once
  ./gpuz.py --watch      refresh every 2 s
  ./gpuz.py --selftest   assert every field we claim is obtainable resolves
"""

import argparse
import configparser
import ctypes
import os
import re
import fcntl
import math
import glob
import shutil
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
NA = "N/A"
NO_SOURCE = "not read here; most are empty in GPU Tweak III too on this card (RE doc 7.2)"

# GB202 / RTX 5090 constants. Public specification values, NOT read from the card:
# nothing in Linux exposes die size or transistor count. Edit per card.
ASIC = {
    "codename": "GB202",
    "process_nm": 5,
    "die_mm2": 750,
    "transistors_m": 92200,
    "shaders": 21760,
    "tmus": 680,
    "rops": 176,
}

SMI_FIELDS = [
    "index", "name", "pci.bus_id", "pci.device_id", "pci.sub_device_id",
    "driver_version", "vbios_version", "memory.total", "memory.used",
    "temperature.gpu", "temperature.memory",
    "clocks.current.graphics", "clocks.current.sm", "clocks.current.memory",
    "clocks.current.video", "clocks.max.graphics", "clocks.max.memory",
    "power.draw", "power.limit", "power.max_limit", "fan.speed",
    "utilization.gpu", "utilization.memory", "utilization.encoder",
    "utilization.decoder", "pstate", "pcie.link.gen.current",
    "pcie.link.width.current", "clocks_throttle_reasons.active",
]

# GT/s -> PCIe generation, for the "Bus Interface" row.
PCIE_GEN = {"2.5": "1.1", "5.0": "2.0", "8.0": "3.0", "16.0": "4.0", "32.0": "5.0"}


class _FanRpm(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint), ("fan", ctypes.c_uint),
                ("speed", ctypes.c_uint)]


# Temperature thresholds, NVML_TEMPERATURE_THRESHOLD_*.
_THRESHOLDS = (("GPU Max", 0), ("GPU Slowdown", 1), ("Memory Max", 3))


def nvml():
    """Per-fan speed/RPM and temperature thresholds.

    nvidia-smi only reports one aggregate fan percentage and no thresholds, so
    these have to come from libnvidia-ml directly.
    """
    out = {"fans": [], "thresholds": [], "temp": None}
    try:
        lib = ctypes.CDLL("libnvidia-ml.so.1")
    except OSError:
        return out
    if lib.nvmlInit_v2() != 0:
        return out
    try:
        h = ctypes.c_void_p()
        if lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)) != 0:
            return out
        n = ctypes.c_uint()
        if lib.nvmlDeviceGetNumFans(h, ctypes.byref(n)) == 0:
            for i in range(n.value):
                pct = ctypes.c_uint()
                ok_pct = lib.nvmlDeviceGetFanSpeed_v2(h, i, ctypes.byref(pct)) == 0
                s = _FanRpm(version=ctypes.sizeof(_FanRpm) | (1 << 24), fan=i)
                ok_rpm = lib.nvmlDeviceGetFanSpeedRPM(h, ctypes.byref(s)) == 0
                out["fans"].append((i,
                                    pct.value if ok_pct else None,
                                    s.speed if ok_rpm else None))
        t = ctypes.c_uint()
        # ASUS reads NV_GPU_THERMAL_SETTINGS.sensor[0].currentTemp, an integer; see
        # ASUSGPUZ-RE.md 9.3. nvidia-smi reports a fractional value, so for parity with
        # the Windows tool take NVML's integer instead.
        if lib.nvmlDeviceGetTemperature(h, 0, ctypes.byref(t)) == 0:
            out["temp"] = t.value
        for label, tid in _THRESHOLDS:
            t = ctypes.c_uint()
            if lib.nvmlDeviceGetTemperatureThreshold(h, tid, ctypes.byref(t)) == 0:
                out["thresholds"].append((label, t.value))
        return out
    finally:
        lib.nvmlShutdown()


def nvapi():
    """What NVML lacks, through the same NVAPI calls Vender.dll makes (RE doc 7.2).

    Struct versions are (version << 16 | size); found by probing, NVIDIA publishes none.
    """
    out = {}
    try:
        lib = ctypes.CDLL("libnvidia-api.so.1")
    except OSError:
        return out
    qi = lib.nvapi_QueryInterface
    qi.restype, qi.argtypes = ctypes.c_void_p, [ctypes.c_uint32]

    def fn(iid, *args):
        ptr = qi(iid)
        return ctypes.CFUNCTYPE(ctypes.c_int, *args)(ptr) if ptr else None

    init = fn(0x0150E828)
    enum = fn(0xE5AC921F, ctypes.c_void_p, ctypes.c_void_p)
    handles, n = (ctypes.c_void_p * 64)(), ctypes.c_uint32()
    if not init or not enum or init() != 0 or enum(handles, ctypes.byref(n)) != 0 \
            or not n.value:
        return out

    def call(iid, size, ver, word1=0, head=b""):
        f = fn(iid, ctypes.c_void_p, ctypes.c_void_p)
        buf = ctypes.create_string_buffer(size)
        struct.pack_into("<II", buf, 0, size | ver << 16, word1)
        buf[4:4 + len(head)] = head
        return buf if f and f(handles[0], buf) == 0 else None

    # Thermal sensors, s32 / 256 at +40. Only slots 1 (core) and 2 (VRAM) exist on
    # GB202; every other slot reads 255, so there is no hotspot sensor to report.
    b = call(0x65FE3AAD, 0xA8, 2, 0b110)
    if b:
        core, vram = struct.unpack_from("<2i", b, 44)
        out["core_temp"], out["vram_temp"] = core / 256.0, vram / 256.0
    b = call(0x465F9BCF, 0x4C, 1)
    if b:
        out["voltage_mv"] = struct.unpack_from("<I", b, 0x28)[0] / 1000.0
    # ClientPowerTopologyGetStatus: {domain, _, percent*1000, _} of the default limit.
    b = call(0xEDCF624E, 0x48, 1)
    if b:
        out["topology_pct"] = {
            struct.unpack_from("<I", b, 8 + 16 * i)[0]:
            struct.unpack_from("<I", b, 16 + 16 * i)[0] / 1000.0
            for i in range(min(struct.unpack_from("<I", b, 4)[0], 4))}
    # Power monitor with GPU-Z's struct sizes (RE doc 12.3). Info: 15-word channel records
    # from +0x30, rail type at word 2. Status: 11-word records from +0x1c:
    # mW, _, _, mA, uV. Channel 0 is the total and always equals 1 + 2.
    info = call(0xC12EB19E, 0xAA8, 1)
    b = info and call(0xF40238EF, 0x59C, 1, struct.unpack_from("<I", info, 4)[0])
    if b:
        out["power"] = []
        for i in range(32):
            if not struct.unpack_from("<I", b, 4)[0] >> i & 1:
                continue
            mw, _, _, ma, uv = struct.unpack_from("<5I", b, 0x1C + 0x2C * i)
            out["power"].append((struct.unpack_from("<I", info, 0x38 + 0x3C * i)[0],
                                 mw / 1000.0, ma / 1000.0, uv / 1e6))
    # ClientPowerPolicies: min / default / max at words 5, 8, 11 and the current limit,
    # all in 1/1000 %. Matches InitData_Card1.ini PowerTargetMin/Def/Max.
    info, b = call(0x34206D86, 0xB8, 1), call(0x70916171, 0x48, 1)
    if info and b:
        out["power_target_pct"] = tuple(
            v / 1000.0 for v in struct.unpack_from("<I8xI8xI", info, 20)
            + struct.unpack_from("<I", b, 16))
    # ClientThermalPolicies: word 1 is the policy count; 0 = no temperature target.
    b = call(0x0D258BB5, 0x58, 1)
    if b:
        out["thermal_policies"] = struct.unpack_from("<I", b, 4)[0]
    # GetClockBoostRanges: 0x48-byte records from +0x2c: type (0 GPU, 4 memory),
    # max / min offset in kHz at +0x24 / +0x28, first / last V/F point index as two
    # bytes at +0x2c (how GPU-Z assigns points to a domain, RE doc 12.3).
    r = call(0x64B43A6A, 0x928, 1)
    domains = {}
    if r:
        out["clock_ranges_mhz"] = {}
        for i in range(struct.unpack_from("<I", r, 4)[0]):
            typ, hi, lo, first, last = struct.unpack_from("<i32x2i2B", r, 0x2C + 0x48 * i)
            if hi or lo:
                out["clock_ranges_mhz"][typ] = (lo / 1000.0, hi / 1000.0)
                domains[typ] = range(first, last + 1)
    # V/F curve: GetClockBoostMask gives a 256-bit point mask, which the status call
    # takes back; status has (kHz, uV) from +0x48 in 0x1c-byte records.
    m = call(0x507B4B59, 0x182C, 1)
    b = m and call(0x21537AD4, 0x1C28, 1, head=m.raw[4:36])
    if b:
        mask = int.from_bytes(m.raw[4:36], "little")
        out["vf"] = {typ: [(khz / 1000.0, uv / 1000.0) for khz, uv in (
            struct.unpack_from("<2I", b, 0x48 + 0x1C * i) for i in idx if mask >> i & 1)]
            for typ, idx in domains.items()}
    return out


class _Smbus(ctypes.Union):
    _fields_ = [("byte", ctypes.c_uint8), ("block", ctypes.c_uint8 * 34)]


class _SmbusIoctl(ctypes.Structure):
    _fields_ = [("rw", ctypes.c_uint8), ("cmd", ctypes.c_uint8),
                ("size", ctypes.c_uint32), ("data", ctypes.POINTER(_Smbus))]


def astral_mcu():
    """ASUS's IT8915 board MCU: 7-bit 0x2B on GPU I2C port 1 (RE doc 7.2). Read-only.

    Uses SMBus transfers: the NVIDIA adapter splits a plain I2C write+read into two
    transactions, and the MCU then answers zeros.
    """
    for name in glob.glob("/sys/class/i2c-dev/i2c-*/name"):
        try:
            with open(name) as fh:
                if not fh.read().startswith("NVIDIA i2c adapter 1 "):
                    continue
            fd = os.open("/dev/" + name.split("/")[-2], os.O_RDWR)
        except OSError:
            continue
        try:
            fcntl.ioctl(fd, 0x0703, 0x2B)                      # I2C_SLAVE

            def rd(reg, n=0):
                d = _Smbus()
                d.block[0] = n
                fcntl.ioctl(fd, 0x0720,                        # I2C_SMBUS, read
                            _SmbusIoctl(1, reg, 8 if n else 2, ctypes.pointer(d)))
                return bytes(d.block[1:1 + n]) if n else d.byte

            if (rd(0x20), rd(0x21)) != (0x15, 0x89):           # ASUS's own ID check
                continue
            be = lambda raw: [int.from_bytes(raw[i:i + 2], "big")
                              for i in range(0, len(raw), 2)]
            pins = be(rd(0x80, 24))
            return {
                "ext_fan": rd(0x41),
                "ext_rpm": (rd(0x44) * 30, rd(0x48) * 30),
                "pins": [(pins[i] / 1000.0, pins[i + 1] / 1000.0)
                         for i in range(0, 12, 2)],
                "board_temps": [v / 10.0 for v in be(rd(0x60, 16))],
                # ExpanModule.dll (RE doc 12.1): accelerometer BE s16 x/y/z, 4096 = 1 g
                "accel": struct.unpack(">3h", bytes(rd(r) for r in range(0x70, 0x76))),
                # three exclusive seconds counters: idle / light / load
                "mileage_s": struct.unpack(">3I", rd(0xc0, 12)),
                "fw": rd(0x1f) << 8 | rd(0x1e),
                "features": rd(0xfd),          # valid from fw 0x510 (RE doc 12.1)
            }
        except OSError:
            continue
        finally:
            os.close(fd)
    return {}


def cpu_utilization(interval=0.1):
    """IDS 90072. Windows takes it from CPUID SDK (RE doc 12.4); here /proc/stat."""
    def snap():
        with open("/proc/stat") as fh:
            v = [int(x) for x in fh.readline().split()[1:]]
        return sum(v), v[3] + v[4]                              # total, idle + iowait
    try:
        t0, i0 = snap()
        time.sleep(interval)
        t1, i1 = snap()
    except (OSError, ValueError, IndexError):
        return None
    return 100.0 * (1 - (i1 - i0) / (t1 - t0)) if t1 > t0 else None


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def smi():
    """One nvidia-smi call -> list of per-GPU dicts."""
    if not shutil.which("nvidia-smi"):
        return []
    out = run(["nvidia-smi", "--query-gpu=" + ",".join(SMI_FIELDS),
               "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.strip().splitlines():
        vals = [v.strip() for v in line.split(", ")]
        if len(vals) == len(SMI_FIELDS):
            gpus.append(dict(zip(SMI_FIELDS, vals)))
    return gpus


def sysfs(bus_id):
    """PCI attributes for a nvidia-smi bus id like 00000000:01:00.0."""
    bdf = bus_id.split(":", 1)[1] if bus_id.count(":") == 2 else bus_id
    d = "/sys/bus/pci/devices/0000:" + bdf
    out = {}
    for f in ("revision", "subsystem_device", "subsystem_vendor",
              "current_link_speed", "current_link_width",
              "max_link_speed", "max_link_width"):
        try:
            with open(os.path.join(d, f)) as fh:
                out[f] = fh.read().strip()
        except OSError:
            out[f] = NA
    return out


def init_ini():
    """Vendor card data, if the GPU Tweak III install sits next to us.

    The vendor writes a bare CR inside NV_BIOSString ("...AS49\rS"), which makes
    configparser choke on universal-newline translation; normalise before parsing.
    """
    for cand in (os.path.join(HERE, "windows", "InitData_Card1.ini"),
                 os.path.join(HERE, "InitData_Card1.ini")):
        if os.path.exists(cand):
            path = cand
            break
    else:
        return None
    try:
        with open(path, "r", encoding="latin-1", newline="") as fh:
            text = fh.read().replace("\r\n", "\n").replace("\r", "")
    except OSError:
        return None
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # keys are case-sensitive in the vendor file
    try:
        cp.read_string(text)
    except configparser.Error:
        return None
    return cp


def caps():
    """Capability rows: Vulkan / OpenGL / ray tracing / compute."""
    c = {}
    vk = run(["vulkaninfo"]) if shutil.which("vulkaninfo") else ""
    vers = re.findall(r"apiVersion\s*[=:]\s*([\d]+\.[\d]+\.[\d]+)", vk)
    c["Vulkan"] = max(vers, key=lambda v: tuple(map(int, v.split(".")))) if vers else NA
    c["Ray Tracing"] = "yes" if "VK_KHR_ray_tracing_pipeline" in vk else NA
    gl = run(["glxinfo", "-B"]) if shutil.which("glxinfo") else ""
    m = re.search(r"OpenGL core profile version string: ([\d.]+)", gl) or \
        re.search(r"OpenGL version string: ([\d.]+)", gl)
    c["OpenGL"] = m.group(1) if m else NA
    c["CUDA"] = "yes" if os.path.exists("/dev/nvidiactl") else NA
    c["OpenCL"] = "yes" if os.path.exists("/etc/OpenCL/vendors/nvidia.icd") else NA
    return c


# nvidia throttle bitmask -> name (NVML nvmlClocksThrottleReasons).
THROTTLE = [
    (0x001, "GpuIdle"), (0x002, "ApplicationsClocksSetting"),
    (0x004, "SwPowerCap"), (0x008, "HwSlowdown"), (0x010, "SyncBoost"),
    (0x020, "SwThermalSlowdown"), (0x040, "HwThermalSlowdown"),
    (0x080, "HwPowerBrakeSlowdown"), (0x100, "DisplayClockSetting"),
    (0x400, "ClocksEventReasonGpuIdle"),
]


def throttle_str(raw):
    try:
        bits = int(raw, 16) if str(raw).startswith("0x") else int(raw)
    except (TypeError, ValueError):
        return str(raw)
    if bits == 0:
        return "none"
    names = [n for b, n in THROTTLE if bits & b]
    return "%s (0x%x)" % (", ".join(names) or "unknown", bits)


def fnum(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def split_id(x):
    """nvidia-smi packs ids as 0xDDDDVVVV -> ('DDDD', 'VVVV') in GPU-Z order."""
    m = re.fullmatch(r"0x([0-9A-Fa-f]{4})([0-9A-Fa-f]{4})", x or "")
    return (m.group(1).upper(), m.group(2).upper()) if m else (x or NA, NA)


def bus_interface(ps):
    cur_s = ps["current_link_speed"].split()[0] if ps["current_link_speed"] != NA else NA
    max_s = ps["max_link_speed"].split()[0] if ps["max_link_speed"] != NA else NA
    gen, cgen = PCIE_GEN.get(max_s, "?"), PCIE_GEN.get(cur_s, "?")
    return "PCIe x%s %s @ x%s %s (%s GT/s)" % (
        ps["max_link_width"], gen, ps["current_link_width"], cgen, cur_s)


def static_panel(g, ps, ini, cp):
    """The 39 rows of the ASUSGPUZ 'Graphics Card' panel (see RE doc section 2.3)."""
    mem_rate = 2
    model = vbios_asus = None
    if ini:
        mem_rate = int(ini.get("clocksetting", "MemoryRate", fallback="2") or 2)
        model = ini.get("VGACardInfo", "ModelName", fallback=None)
        vbios_asus = ini.get("VGACardInfo", "NV_BIOSString", fallback=None)

    memclk = fnum(g["clocks.current.memory"])
    boost = fnum(g["clocks.max.graphics"])        # fillrates are rated, not live
    memmax = fnum(g["clocks.max.memory"])         # ditto for bandwidth
    width = 512 if ASIC["codename"] == "GB202" else None  # from the memory bus, spec
    eff = memclk * mem_rate if memclk else None
    eff_max = memmax * mem_rate if memmax else None
    dev, ven = split_id(g["pci.device_id"])
    sdev, sven = split_id(g["pci.sub_device_id"])

    rows = [
        ("Name", model or g["name"]),
        ("GPU", ASIC["codename"]),
        ("Revision", ps["revision"]),
        ("Technology", "%d nm (spec)" % ASIC["process_nm"]),
        ("Die Size", "%d mm² (spec)" % ASIC["die_mm2"]),
        ("Release Date", NA + " (not exposed on Linux)"),
        ("Transistors", "%d M (spec)" % ASIC["transistors_m"]),
        ("BIOS Version", vbios_asus or g["vbios_version"]),
        ("Subvendor", "ASUS (%s)" % sven),
        ("Device ID", "%s %s / %s %s" % (ven, dev, sven, sdev)),
        ("ROPs/TMUs", "%d / %d (spec)" % (ASIC["rops"], ASIC["tmus"])),
        ("Bus Interface", bus_interface(ps)),
        ("Shaders", "%d Unified (spec)" % ASIC["shaders"]),
        ("DirectX Support", "n/a on Linux (Vulkan %s)" % cp["Vulkan"]),
        ("Pixel Fillrate", "%.1f GPixel/s @ boost" % (ASIC["rops"] * boost / 1000)
         if boost else NA),
        ("Texture Fillrate", "%.1f GTexel/s @ boost" % (ASIC["tmus"] * boost / 1000)
         if boost else NA),
        ("Memory Type", "GDDR7 (spec)"),
        ("Bus Width", "%d Bit (spec)" % width if width else NA),
        ("Memory Size", "%s MB" % g["memory.total"]),
        ("Bandwidth", "%.1f GB/s @ rated" % (width / 8 * eff_max / 1000)
         if eff_max and width else NA),
        ("Driver Version", g["driver_version"]),
        ("Driver Date", NA + " (not exposed on Linux)"),
        ("Digital Signature", NA + " (Windows WHQL concept)"),
        ("GPU Clock", "%s MHz" % g["clocks.current.graphics"]),
        ("  Memory", "%s MHz (%s MHz effective)" % (g["clocks.current.memory"],
                                                    int(eff) if eff else NA)),
        ("  Boost", "%s MHz (max)" % g["clocks.max.graphics"]),
    ]
    if ini:
        rows += [
            ("Default Clock", "%s MHz" % ini.get("clocksetting", "GPUClock_3D_Def",
                                                 fallback=NA)),
            ("  Memory", "%s MHz effective" % ini.get("clocksetting",
                                                      "MemoryClock_3D_Def", fallback=NA)),
            ("  Boost", "%s MHz" % ini.get("SMTableSetting", "Def_Ref_GPUClock",
                                           fallback=NA)),
        ]
    rows += [
        ("NVIDIA SLI", "n/a (removed in Blackwell)"),
        ("Computing", "OpenCL %s, CUDA %s, DirectCompute n/a, DirectML n/a"
         % (cp["OpenCL"], cp["CUDA"])),
        ("Technologies", "Vulkan %s, Ray Tracing %s, OpenGL %s, PhysX %s"
         % (cp["Vulkan"], cp["Ray Tracing"], cp["OpenGL"],
            "SDK only (no GPU PhysX on Linux)")),
    ]
    return rows


def live_sensors(g, nv=None, api=None, mcu=None):
    """The Monitor catalogue (RE doc section 4.1), keyed by the vendor's IDS number."""
    nv = nv or {"fans": [], "thresholds": [], "temp": None}
    api, mcu = api or {}, mcu or {}
    gpu_temp = g["temperature.gpu"] if nv.get("temp") is None else nv["temp"]
    rows = [
        (90001, "GPU Clock", g["clocks.current.graphics"], "MHz"),
        (90050, "Processor Clock", g["clocks.current.sm"], "MHz"),
        (90004, "Memory Clock", g["clocks.current.memory"], "MHz"),
        (None, "Video Clock", g["clocks.current.video"], "MHz"),
        (90006, "GPU Temperature", gpu_temp, "C"),
        (90008, "Memory Temperature", api.get("vram_temp", NA), "C"),
        (90002, "GPU Voltage", api.get("voltage_mv", NA), "mV"),
        (90011, "GPU Usage", g["utilization.gpu"], "%"),
        (90025, "Memory Usage", g["utilization.memory"], "%"),
        (90049, "FB Usage", g["memory.used"], "MB"),
        (90012, "VID Usage", g["utilization.encoder"], "%"),
        (None, "Decoder Usage", g["utilization.decoder"], "%"),
        (90071, "Total Card Power", g["power.draw"], "W"),
        (90007, "Power Target Status", g["power.limit"], "W"),
        (90003, "GPU Fan Speed", g["fan.speed"], "%"),
        (None, "Performance State", g["pstate"], ""),
        (None, "Throttle Reasons",
         throttle_str(g["clocks_throttle_reasons.active"]), ""),
    ]
    # The vendor names the ASTRAL's two fans "Sides" (90022) and "Center" (90023).
    names = {0: (90022, "Fan Speed (Sides)"), 1: (90023, "Fan Speed (Center)")}
    for i, pct, rpm in nv["fans"]:
        ids, label = names.get(i, (None, "Fan %d Speed" % (i + 1)))
        val = NA if pct is None and rpm is None else "%s%% / %s" % (
            NA if pct is None else pct, NA if rpm is None else rpm)
        rows.append((ids, label, val, "" if val == NA else "RPM"))
    for label, val in nv["thresholds"]:
        rows.append((None, "Limit: %s Temp" % label, val, "C"))
    for dom, pct in sorted(api.get("topology_pct", {}).items()):
        rows.append((None, "Power Topology Domain %d" % dom, pct, "% of default limit"))
    for rail, w, a, v in api.get("power", []):    # rail names as GPU-Z has them
        rows.append((None, "Power Monitor %s" % RAILS.get(rail, "Rail %#x" % rail),
                     "%.3f W / %.2f A / %.3f V" % (w, a, v), ""))
    if "power_target_pct" in api:
        rows.append((None, "Power Target Range", "%.0f / %.0f / %.0f %% (now %.0f)"
                     % api["power_target_pct"], "min / default / max"))
    if "thermal_policies" in api:
        rows.append((None, "Temperature Target", "%d policies" % api["thermal_policies"]
                     if api["thermal_policies"] else "none on this card", ""))
    for typ, (lo, hi) in sorted(api.get("clock_ranges_mhz", {}).items()):
        rows.append((None, "%s Clock Offset Range" % DOMAINS.get(typ, "Domain %d" % typ),
                     "%+.0f .. %+.0f" % (lo, hi), "MHz"))
    for typ, vf in sorted(api.get("vf", {}).items()):
        if vf:
            rows.append((None, "%s V/F Curve" % DOMAINS.get(typ, "Domain %d" % typ),
                         "%d points, %.0f MHz @ %.0f mV .. %.0f MHz @ %.0f mV"
                         % ((len(vf),) + vf[0] + vf[-1]), "(--vf lists all)"))
    if mcu:
        rows.append((90035, "Ext. Fan Speed", mcu["ext_fan"], ""))
        rows.append((90036, "Ext. Fan 1 Speed", mcu["ext_rpm"][0], "RPM"))
        rows.append((90037, "Ext. Fan 2 Speed", mcu["ext_rpm"][1], "RPM"))
        for i, (v, a) in enumerate(mcu["pins"], 1):
            rows.append((None, "12V-2x6 Pin %d" % i, "%.3f V / %.2f A" % (v, a), ""))
        for i, t in enumerate(mcu["board_temps"], 1):
            rows.append((None, "Board Thermistor %d" % i, t, "C"))
        t = mcu["board_temps"]                 # the four zones ExpanModule.dll shows
        for i, z in enumerate(((t[2] + t[5]) / 2, (t[1] + t[3]) / 2, (t[0] + t[7]) / 2, t[4]), 1):
            rows.append((None, "Thermal Map Zone %d" % i, "%.1f" % z, "C"))
        feats = [n for bit, n in enumerate(("mileage", "power status", "thermal map"))
                 if mcu["fw"] < 0x510 or mcu["features"] >> bit & 1]
        rows.append((None, "Board MCU Firmware", "%#06x" % mcu["fw"], "(%s)" % ", ".join(feats)))
        x, y, z = (max(-4096, min(4096, v)) for v in mcu["accel"])
        n = math.sqrt(x * x + y * y + z * z)
        if n:                                                   # ASUS: asin(x), asin(y), acos(z)
            rows.append((None, "Card Tilt X / Y / Z", "%.1f / %.1f / %.1f" % (
                math.degrees(math.asin(x / n)), math.degrees(math.asin(y / n)),
                math.degrees(math.acos(z / n))), "deg"))
        for label, sec in zip(("Idle", "Light", "Load"), mcu["mileage_s"]):
            rows.append((None, "Mileage %s" % label, "%.1f" % (sec / 3600.0), "h"))
    cpu = cpu_utilization()
    if cpu is not None:
        rows.append((90072, "CPU Utilization", "%.0f" % cpu, "%"))
    khz = []
    for f in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
        try:
            with open(f) as fh:
                khz.append(int(fh.read()))
        except (OSError, ValueError):
            pass
    if khz:
        rows.append((None, "CPU Clock", "%.0f" % (max(khz) / 1000.0), "MHz (fastest core)"))
    return rows


DOMAINS = {0: "GPU", 4: "Memory"}          # NVAPI clock domain IDs
RAILS = {0xF5: "Total", 0xFF: "PCIe Slot", 0xDA: "16-Pin"}

UNAVAILABLE = [
    (90013, "Memory Current"), (90014, "Memory Voltage"),
    (90015, "PEX_VDD"), (90016, "VDDCI Voltage"), (90017, "VCC Voltage"),
    (90018, "PCIE12V Voltage"), (90019, "GPU Consumption"),
    (90020, "Memory Consumption"), (90021, "Total Board Consumption"),
    (90024, "Right Fan Speed"), (90026, "Load Line"), (90027, "VRM Clock"),
    (90028, "VDDCI Power Temperature"), (90029, "Memory Power Temperature"),
    (90030, "VDDCI Current"), (90031, "VDDC Current"), (90032, "0.95V Voltage"),
    (90033, "3.3V Voltage"), (90034, "1.8V Voltage"),
    (90038, "Normalized Total Power"), (90048, "GPU Current"),
    (90051, "VDDC Power Temperature"), (90055, "SOC VRM Temperature"),
    (90056, "Junction Temperature"), (90058, "GPU VRM Temperature"),
    (90059, "VDD VRM Temperature"), (90060, "Memory VRM 1 Temperature"),
    (90061, "Memory VRM 2 Temperature"), (90062, "Liquid Temperature"),
    (90063, "PLX Temperature"), (90064, "SOC Temperature"),
    (90009, "Power Temperature"), (90010, "Board Temperature"),
]


def board_sensors():
    """Motherboard EC / SIO sensors -- the Linux stand-in for ITECCTdll.dll."""
    out = []
    for h in sorted(os.listdir("/sys/class/hwmon")):
        d = os.path.join("/sys/class/hwmon", h)
        try:
            with open(os.path.join(d, "name")) as fh:
                chip = fh.read().strip()
        except OSError:
            continue
        for f in sorted(os.listdir(d)):
            if not f.endswith("_input"):
                continue
            try:
                with open(os.path.join(d, f)) as fh:
                    raw = int(fh.read().strip())
            except (OSError, ValueError):
                continue
            base = f[:-6]
            label = base
            try:
                with open(os.path.join(d, base + "_label")) as fh:
                    label = fh.read().strip() or base
            except OSError:
                pass
            if base.startswith("temp"):
                val, unit = raw / 1000.0, "C"
                if val < -50 or val > 150:   # disconnected probe
                    continue
            elif base.startswith("fan"):
                val, unit = raw, "RPM"
                if val == 0:
                    continue
            elif base.startswith("in"):
                val, unit = raw / 1000.0, "V"
            elif base.startswith("power"):
                val, unit = raw / 1e6, "W"
            else:
                continue
            out.append(("%s/%s" % (chip, label), val, unit))
    return out


_MEASURE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:%|[A-Za-z]+)?")


def _numbers(val):
    """The numbers of a row made only of measurements, e.g. "1.5 W / 2 A" -> [1.5, 2.0].

    Rows with other text (ranges, summaries, firmware) return [] and are not tracked.
    """
    parts = [_MEASURE.fullmatch(p.strip()) for p in str(val).split("/")]
    return [float(m.group(1)) for m in parts] if all(parts) else []


def show(gpus, with_board=True, stats=None):
    """stats: {label: [min, max]} kept across calls by --watch; None = single shot."""
    ini, cp, nv = init_ini(), caps(), nvml()
    for g in gpus:
        ps = sysfs(g["pci.bus_id"])
        print("=" * 72)
        print("Graphics Card #%s  --  %s" % (g["index"], g["name"]))
        print("=" * 72)
        for k, v in static_panel(g, ps, ini, cp):
            print("  %-20s %s" % (k, v))
        print("\n  Sensors")
        for ids, label, val, unit in live_sensors(g, nv, nvapi(), astral_mcu()):
            tag = "" if ids is None else " [%d]" % ids
            shown = val if val == NA else ("%s %s" % (val, unit)).strip()
            span = ""
            nums = _numbers(val)
            if stats is not None and nums:
                key = (g["index"], label)
                lo, hi = stats.get(key, (nums, nums))
                if len(lo) != len(nums):       # row changed shape: start over
                    lo, hi = nums, nums
                stats[key] = lo, hi = ([min(p) for p in zip(lo, nums)],
                                       [max(p) for p in zip(hi, nums)])
                span = "  min %s  max %s" % tuple(
                    " / ".join("%g" % v for v in side) for side in (lo, hi))
            print("    %-24s %-28s%s%s" % (label, shown, tag, span))
        print("\n  No source (%d sensors)" % len(UNAVAILABLE))
        print("    %s" % NO_SOURCE)
        for ids, label in UNAVAILABLE:
            print("      %-28s [%d]" % (label, ids))
    if with_board:
        print("\n" + "=" * 72)
        print("Board sensors (hwmon)")
        print("=" * 72)
        for label, val, unit in board_sensors():
            print("  %-34s %s %s" % (label, val, unit))


def selftest(gpus):
    """Fail loudly if a field the RE doc marks obtainable stops resolving."""
    assert gpus, "no NVIDIA GPU reported by nvidia-smi"
    g = gpus[0]
    must = ["name", "driver_version", "vbios_version", "memory.total", "memory.used",
            "temperature.gpu", "clocks.current.graphics", "clocks.current.memory",
            "clocks.max.graphics", "power.draw", "power.limit", "fan.speed",
            "utilization.gpu", "utilization.memory", "pstate",
            "pci.device_id", "pci.sub_device_id", "pcie.link.width.current"]
    bad = [k for k in must if g.get(k, NA) in (NA, "", None)]
    assert not bad, "nvidia-smi returned N/A for: %s" % ", ".join(bad)

    ps = sysfs(g["pci.bus_id"])
    bad = [k for k, v in ps.items() if v == NA]
    assert not bad, "sysfs missing: %s" % ", ".join(bad)

    assert float(g["temperature.gpu"]) > 0, "implausible GPU temperature"
    assert float(g["power.draw"]) > 0, "implausible power draw"
    assert board_sensors(), "no hwmon sensors readable"

    nv = nvml()
    assert nv["fans"], "NVML reported no fans (libnvidia-ml missing or too old?)"
    assert nv["thresholds"], "NVML reported no temperature thresholds"
    assert nv["temp"] is not None, "NVML gave no integer GPU temperature"
    # The integer must agree with nvidia-smi's fractional reading, or one is wrong.
    assert abs(nv["temp"] - float(g["temperature.gpu"])) < 2.0, (
        "NVML temp %s disagrees with nvidia-smi %s" % (nv["temp"], g["temperature.gpu"]))

    api, mcu = nvapi(), astral_mcu()
    assert 0 < api.get("vram_temp", 0) < 120, "NVAPI VRAM temperature missing"
    assert abs(api["core_temp"] - float(g["temperature.gpu"])) < 2.0, "NVAPI core temp off"
    assert 500 < api.get("voltage_mv", 0) < 1300, "NVAPI core voltage missing"
    pw = api.get("power")
    assert pw and [r for r, _, _, _ in pw] == [0xF5, 0xFF, 0xDA], "power rails changed"
    assert abs(pw[0][1] - pw[1][1] - pw[2][1]) < 0.01, "power monitor channels do not sum"
    assert all(10 < v < 13.5 and abs(w - a * v) < 0.5 for _, w, a, v in pw), "W != A*V"
    lo, de, hi, cur = api["power_target_pct"]
    assert 0 < lo <= cur <= hi and lo <= de <= hi, "power target range implausible"
    for typ, vf in api["vf"].items():
        assert vf and vf == sorted(vf) and 100 < vf[0][0] <= vf[-1][0] < 40000 \
            and 300 < vf[0][1] <= vf[-1][1] < 1400, "V/F curve %d implausible" % typ
    assert len(api["vf"][0]) > 50, "GPU V/F curve too short"
    assert api["clock_ranges_mhz"][0][0] < 0 < api["clock_ranges_mhz"][0][1], "clock range"
    if mcu:   # needs rw on the GPU's /dev/i2c-N; absent is not a failure
        assert all(10 < v < 13.5 for v, _ in mcu["pins"]), "implausible 12V pin voltage"
        assert 3000 < math.sqrt(sum(v * v for v in mcu["accel"])) < 5200, "accelerometer not ~1 g"
        assert all(mcu["mileage_s"]), "mileage counters empty"
        assert 0x149 <= mcu["fw"] < 0x1000, "MCU firmware version implausible"
    else:
        print("note: board MCU not readable (no access to the GPU I2C bus?)")

    assert _numbers("81.689 W / 6.82 A / 11.971 V") == [81.689, 6.82, 11.971]
    assert _numbers("0% / 0") == [0.0, 0.0] and _numbers(-0.7) == [-0.7]
    assert _numbers("67 / 100 / 100 % (now 100)") == [] and _numbers("0x0513") == []
    assert _numbers("P0") == [] and _numbers(NA) == []

    rows = static_panel(g, ps, init_ini(), caps())
    assert len(rows) >= 29, "static panel lost rows: %d" % len(rows)
    print("selftest OK: %d static rows, %d live sensors (%d fans via NVML), "
          "%d board sensors, %d gaps"
          % (len(rows), len(live_sensors(g, nv, api, mcu)), len(nv["fans"]),
             len(board_sensors()), len(UNAVAILABLE)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--watch", nargs="?", type=float, const=2.0, default=None,
                    metavar="SEC", help="refresh continuously")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--no-board", action="store_true", help="skip hwmon sensors")
    ap.add_argument("--vf", action="store_true", help="list the V/F curve points")
    a = ap.parse_args()

    gpus = smi()
    if not gpus:
        sys.exit("no NVIDIA GPU found (is nvidia-smi installed and the driver loaded?)")

    if a.selftest:
        selftest(gpus)
        return
    if a.vf:
        for typ, vf in sorted(nvapi().get("vf", {}).items()):
            print("%s V/F curve" % DOMAINS.get(typ, "Domain %d" % typ))
            for i, (mhz, mv) in enumerate(vf):
                print("  %3d  %7.1f MHz  %7.2f mV" % (i, mhz, mv))
        return
    if a.watch is None:
        show(gpus, not a.no_board)
        return
    stats = {}
    try:
        while True:
            os.write(1, b"\x1b[H\x1b[2J")
            show(smi(), not a.no_board, stats)
            time.sleep(a.watch)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
