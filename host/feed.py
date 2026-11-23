#!/usr/bin/env python3
"""Send this PC's GPU sensors to the CrowPanel, one JSON line per second.

  ./feed.py              write to /dev/ttyUSB0
  ./feed.py --dry-run    print the lines instead
  ./feed.py --selftest   build one frame and check it fits the firmware

The wire format is described in HARDWARE.md; main/monitor.c is the receiver.
"""

import argparse
import json
import math
import os
import sys
import tempfile
import termios
import time

import gpuz

LINE_MAX = 8192   # line buffer in main/monitor.c


# Throttle reasons worth a warning; the idle and clock-setting bits are normal operation.
THROTTLE_WARN = {0x004: "power cap", 0x008: "HW slowdown", 0x020: "thermal",
                 0x040: "HW thermal", 0x080: "power brake"}
# shortcut: our own limit in degrees off level, ASUS's off-angle threshold is unknown;
# take theirs if the main exe gets decompiled. Assumes a horizontally mounted card.
TILT_MAX = 3.0
VF_POINTS = 32    # points of the V/F curve sent; main/monitor.c takes as many
SAG_HOURS = 168   # hourly samples of the card's off-level angle kept; main/monitor.c charts as many
STATE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "crowpanel-feed.json")
PRICE = None      # per kWh, from --price
peaks = {}        # highest values since the feed started
sag = None        # [[hour, degrees], ...], loaded from STATE on first use
energy_wh, energy_t = 0.0, None


def peak(key, v):
    if v is not None and (key not in peaks or v > peaks[key]):
        peaks[key] = v
    return peaks.get(key)


def fmt(f, *vals):
    return "--" if None in vals else f % vals


def num(v, digits=2):
    """A finite float, or None: cJSON cannot parse NaN/Infinity and gpuz reports "N/A"."""
    try:
        v = round(float(v), digits)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def sag_history(deg, hour):
    """The off-level angle once per hour of uptime, kept across restarts. Returns 1/100 degrees for the chart."""
    global sag
    if sag is None:
        try:
            with open(STATE) as f:
                sag = [[int(h), float(d)] for h, d in json.load(f)["sag"]]
        except (OSError, ValueError, KeyError, TypeError):
            sag = []
    if not sag or sag[-1][0] != hour:
        sag = (sag + [[hour, round(deg, 2)]])[-SAG_HOURS:]
        try:
            os.makedirs(os.path.dirname(STATE), exist_ok=True)
            with open(STATE + ".tmp", "w") as f:
                json.dump({"sag": sag}, f)
            os.replace(STATE + ".tmp", STATE)
        except OSError:
            pass                           # the history still works, it just is not remembered
    return [round(d * 100) for _, d in sag]


def vram_top(index, n=3):
    """The n programs holding the most memory on GPU `index`: [(name, MiB), ...]."""
    use = {}
    for line in gpuz.run(["nvidia-smi", "pmon", "-c", "1", "-s", "m"]).splitlines():
        p = line.split()
        if len(p) >= 6 and p[0] == index and p[3].isdigit():
            use[p[5]] = use.get(p[5], 0) + int(p[3])
    return sorted(use.items(), key=lambda kv: -kv[1])[:n]


def pci_dir(bus_id):
    return "/sys/bus/pci/devices/0000:" + bus_id.split(":", 1)[1].lower()


def aer(bus_id):
    """Corrected / non-fatal / fatal PCIe error totals from the kernel; the total is each file's last number."""
    out = []
    for kind in ("correctable", "nonfatal", "fatal"):
        try:
            with open("%s/aer_dev_%s" % (pci_dir(bus_id), kind)) as f:
                out.append(f.read().split()[-1])
        except (OSError, IndexError):
            out.append("--")
    return " / ".join(out)


def gen(speed):
    return gpuz.PCIE_GEN.get(speed.split()[0], "?").split(".")[0]


def frame():
    global energy_wh, energy_t
    out = {}
    gpus = gpuz.smi()
    if gpus:
        g, nv, api = gpus[0], gpuz.nvml(), gpuz.nvapi()
        out["gpu"] = {
            "temp": num(g["temperature.gpu"] if nv["temp"] is None else nv["temp"]),
            "vram": num(api.get("vram_temp"), 1),
            "load": num(g["utilization.gpu"]),
            "mem": num(g["memory.used"]),
            "mem_total": num(g["memory.total"]),
            "power": num(g["power.draw"], 1),
            "limit": num(g["power.limit"], 0),
            "fans": [num(rpm) for _, _, rpm in nv["fans"]],           # radiator, blower on this LC card; both stop at idle
            "slowdown": num(dict(nv["thresholds"]).get("GPU Slowdown")),
        }
        gp, warn, card, info = out["gpu"], [], [], []
        try:
            bits = int(g["clocks_throttle_reasons.active"], 16)
        except ValueError:
            bits = 0
        warn += ["Throttle: " + n for b, n in THROTTLE_WARN.items() if bits & b]
        rails = {r: (w, a) for r, w, a, _ in api.get("power", [])}
        for name, rail in (("Total", 0xF5), ("16-pin", 0xDA), ("Slot", 0xFF)):
            if rail in rails:
                card.append([name, "%.1f W  %.2f A" % rails[rail]])
        info.append(["Peak GPU temp", fmt("%.0f \u00b0C", peak("temp", gp["temp"]))])
        info.append(["Peak VRAM temp", fmt("%.0f \u00b0C", peak("vram", gp["vram"]))])
        info.append(["Peak power", fmt("%.1f W", peak("power", gp["power"]))])
        conn, sysrows, lim = [], [], []
        ps = gpuz.sysfs(g["pci.bus_id"])
        if gpuz.NA not in ps.values():
            sysrows.append(["PCIe link", "Gen %s x%s (max Gen %s x%s)" % (
                gen(ps["current_link_speed"]), ps["current_link_width"],
                gen(ps["max_link_speed"]), ps["max_link_width"])])
            if ps["current_link_width"] != ps["max_link_width"]:     # speed drops at idle, width should not
                warn.append("PCIe link x%s of x%s" % (ps["current_link_width"], ps["max_link_width"]))
        sysrows.append(["PCIe errors corr. / non-fatal / fatal", aer(g["pci.bus_id"])])
        sysrows.append(["Encoder / decoder", "%s %% / %s %%" % (g["utilization.encoder"], g["utilization.decoder"])])
        sysrows.append(["Memory controller", "%s %%" % g["utilization.memory"]])
        now = time.monotonic()
        if energy_t is not None and gp["power"] is not None:
            energy_wh += gp["power"] * min(now - energy_t, 5.0) / 3600      # capped: a stalled feed is not a load
        energy_t = now
        sysrows.append(["Energy since feed start", "%.1f Wh" % energy_wh + (
            "" if PRICE is None else "  (%.2f at %.2f/kWh)" % (energy_wh / 1000 * PRICE, PRICE))])
        cpu_temp = [v for name, v, _ in gpuz.board_sensors()
                    if name.startswith(("coretemp/Package", "k10temp/Tctl"))]
        sysrows.append(["CPU load / temp", "%.0f %% / %s" % (
            gpuz.cpu_utilization(), fmt("%.0f \u00b0C", cpu_temp[0] if cpu_temp else None))])
        lim.append(["Power target min / def / max", fmt("%.0f / %.0f / %.0f %%  (now %.0f)",
                                                       *api.get("power_target_pct", [None]))])
        lim.append(["Highest power limit", fmt("%.0f W", num(g["power.max_limit"]))])
        for typ, name in ((0, "GPU clock offset"), (4, "Memory clock offset")):
            lim.append([name, fmt("%+.0f .. %+.0f MHz", *api.get("clock_ranges_mhz", {}).get(typ, [None]))])
        lim += [[label + " temp limit", "%d \u00b0C" % t] for label, t in nv["thresholds"]]
        lim += [["VRAM #%d" % (i + 1), "%s  %d MiB" % nm] for i, nm in enumerate(vram_top(g["index"]))]
        curve = api.get("vf", {}).get(0, [])
        if curve:                          # [mV, MHz], thinned out but with the last point kept
            step = -(-len(curve) // VF_POINTS)
            out["vf"] = [[round(mv), round(mhz)] for mhz, mv in curve[:-1:step][:VF_POINTS - 1] + curve[-1:]]
            out["vf_now"] = [num(api.get("voltage_mv"), 0), num(g["clocks.current.graphics"], 0)]
        mcu = gpuz.astral_mcu()            # empty without rw access to the GPU's /dev/i2c-N
        if mcu:
            # Amps per pin as GPU Tweak's GetPowerStatus reports them (RE doc 12.1):
            # reverse register order, and a reading over 50 A becomes 0.01.
            pins = out["pins"] = [0.01 if a > 50 else num(a) for _, a in reversed(mcu["pins"])]
            card.append(["Pins sum", "%.2f A" % sum(pins)])
            conn += [["Pin %d" % (i + 1), "%.2f V  %.2f A" % (v, a)]
                     for i, ((v, _), a) in enumerate(zip(reversed(mcu["pins"]), pins))]
            # A pin carrying much more or less than the others points at a bad contact.
            spread = max(pins) - min(pins)
            share = (spread, 100 * spread * len(pins) / sum(pins) if sum(pins) else 0.0)
            conn.append(["Spread", "%.2f A  (%.0f %% of mean)" % share])
            conn.append(["Peak spread", "%.2f A  (%.0f %% of mean)" % peak("spread", share)])
            out["therm"] = [["Board T%d" % (i + 1), "%.1f \u00b0C" % v] for i, v in enumerate(mcu["board_temps"])]
            sysrows.append(["Ext. fan header", "%d / %d rpm" % mcu["ext_rpm"]])
            sysrows.append(["Board controller firmware", "%#06x" % mcu["fw"]])
            top = peak("pin", (max(pins), pins.index(max(pins)) + 1))
            info.append(["Peak pin current", "%.2f A (Pin%d)" % top])
            t = mcu["board_temps"]         # the four zones of ASUS's thermal map, as in gpuz.live_sensors
            out["zones"] = [num(z, 1) for z in (
                (t[2] + t[5]) / 2, (t[1] + t[3]) / 2, (t[0] + t[7]) / 2, t[4])]
            info.append(["Peak zone temp", "%.1f \u00b0C" % peak("zone", max(out["zones"]))])
            x, y, z = (max(-4096, min(4096, v)) for v in mcu["accel"])
            n = math.sqrt(x * x + y * y + z * z)
            if n:
                ax, ay = math.degrees(math.asin(x / n)), math.degrees(math.asin(y / n))
                info.append(["Tilt X / Y / Z", "%.1f / %.1f / %.1f \u00b0"
                             % (ax, ay, math.degrees(math.acos(z / n)))])
                if max(abs(ax), abs(ay)) > TILT_MAX:
                    warn.append("Off-angle installation")
                off = math.degrees(math.acos(abs(z) / n))             # angle between the board and level
                out["sag"] = sag_history(off, int(time.time() // 3600))
            info.append(["Hours idle / light / load",
                         "%.0f / %.0f / %.0f" % tuple(s / 3600 for s in mcu["mileage_s"])])
        card.append(["Core", fmt("%.0f mV", num(api.get("voltage_mv")))])
        card.append(["Clock", fmt("%.0f MHz", num(g["clocks.current.graphics"]))])
        card.append(["Fans", " / ".join(fmt("%d %%", pct) for _, pct, _ in nv["fans"]) or "--"])
        info.append(["Throttle reasons", gpuz.throttle_str(g["clocks_throttle_reasons.active"])])
        info.append(["Performance state", g["pstate"]])
        out.update(warn=warn, card=card, info=info, conn=conn, sys=sysrows, lim=lim)
    return out


def encode(f):
    return json.dumps(f, separators=(",", ":")) + "\n"


def open_port(path, mode=os.O_WRONLY):
    fd = os.open(path, mode | os.O_NOCTTY)
    a = termios.tcgetattr(fd)
    a[0] = a[1] = a[3] = 0                                    # raw
    a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL       # HUPCL off: no reset on close
    a[4] = a[5] = termios.B115200
    termios.tcsetattr(fd, termios.TCSANOW, a)
    return fd


def selftest():
    global STATE, sag
    with tempfile.TemporaryDirectory() as d:       # not the real history
        STATE = os.path.join(d, "sub", "state.json")
        assert sag_history(1.234, 7) == [123] and sag_history(9, 7) == [123] and sag_history(2, 8) == [123, 200]
        sag = None                                 # as after a restart
        assert sag_history(3, 8) == [123, 200], "history not read back"
        sag = [[h, 0.0] for h in range(SAG_HOURS)]
        assert len(sag_history(1, SAG_HOURS)) == SAG_HOURS
        sag = None
        _selftest_frame()
    assert gen("32.0 GT/s PCIe") == "5" and gen("weird") == "?"


def _selftest_frame():
    assert peak("t", 5) == 5 and peak("t", 3) == 5 and peak("t", 7) == 7 and peak("none", None) is None
    peaks.clear()
    assert fmt("%d W", None) == "--" and fmt("%d W", 3) == "3 W"
    assert num("N/A") is None and num(float("nan")) is None and num("54") == 54.0
    f = frame()
    line = encode(f)
    assert json.loads(line) == f, "frame does not round-trip"
    assert len(line) < LINE_MAX, "frame is %d bytes, firmware takes %d" % (len(line), LINE_MAX)
    assert f.get("gpu", {}).get("temp"), "no GPU temperature"
    assert len(f.get("vf", [])) <= VF_POINTS and all(len(f[k]) <= 12 for k in ("card", "info", "conn", "sys", "lim"))
    print("selftest OK: %d bytes" % len(line))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--price", type=float, help="electricity price per kWh, to show what the card has cost")
    a = ap.parse_args()
    global PRICE
    PRICE = a.price
    if a.selftest:
        selftest()
        return
    fd = err = None
    try:
        while True:
            t0 = time.monotonic()
            line = encode(frame())
            if a.dry_run:
                sys.stdout.write(line)
                sys.stdout.flush()
            else:
                try:                       # the panel may be unplugged or reflashed: keep trying
                    if fd is None:
                        fd = open_port(a.port)
                    data = line.encode()
                    while data:
                        data = data[os.write(fd, data):]
                    err = None
                except (OSError, termios.error) as e:
                    if str(e) != err:      # once per outage, not once per second in the journal
                        err = str(e)
                        print("%s: %s" % (a.port, e), file=sys.stderr, flush=True)
                    if fd is not None:
                        os.close(fd)
                        fd = None
            time.sleep(max(0.0, 1.0 - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
