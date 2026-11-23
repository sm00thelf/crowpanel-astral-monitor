# Hardware and design notes

## Board

A **CrowPanel 4.3" ESP32-S3 HMI display**: 480×272 RGB LCD with XPT2046 resistive touch.
The model was identified from the chip as read by esptool plus the display and touch setup in the boot log of the
firmware the board came with; no datasheet was consulted. The 5" and 7" CrowPanels use 800×480 panels with
capacitive touch, so they don't match.

| Part | Value | Source |
| --- | --- | --- |
| USB bridge | QinHeng CH340, ID `1a86:7523`, `/dev/ttyUSB0` | lsusb |
| Chip | ESP32-S3 (QFN56), revision v0.2 | esptool |
| CPU | Dual core + LP core, 240 MHz, Wi-Fi + BT 5 (LE) | esptool |
| Crystal | 40 MHz | esptool |
| Flash | 4 MB, quad (QIO), 3.3 V, manufacturer `5e`, device `4016` | esptool |
| PSRAM | 2 MB embedded, AP_3v3, 80 MHz | esptool, boot log |
| Display | RGB LCD, 480×272, 12 MHz pixel clock (76 Hz) | `main/board.h` |
| Backlight | GPIO 2 | boot log |
| Touch | XPT2046 resistive, CS=0, INT=36 | boot log |
| SPI bus | MOSI=11, MISO=13, SCK=12 | boot log |
| Console UART | GPIO 43/44 | boot log |

The LCD pins and timings are in `main/board.h`.

## Framebuffer and refresh rate

The framebuffer (261 KB) is in internal RAM, leaving about 82 KB of internal RAM free (largest block 31 KB);
PSRAM is free for the application.
A PSRAM framebuffer was tried first and rejected: at 60 Hz the driver's bounce-buffer refill took up to 1060 µs
per 16-line chunk against a 901 µs budget whenever LVGL was drawing, and the picture jumped by 16 lines.
That is the likely reason the firmware the board came with ran at 6 MHz (38 Hz).

Measured over serial with LVGL's perf monitor (internal framebuffer): about 90 FPS rendered, 3 ms per frame,
at 9.5, 12 and 15 MHz alike, so the pixel clock sets the refresh rate.
At 12 MHz (76 Hz) the picture is stable and colours are correct, confirmed by eye. 15 MHz is untested by eye.

## Touch

Calibrated on one unit: four targets 30 px from the corners read back within 5 px.
X was mirrored against the Elecrow defaults. The constants are in `main/board.h`; the README describes how to
measure them on another unit.

## Monitor

The firmware shows the GPU on three dark pages.

- A sideways swipe of 50 px or more switches page at once, without a sliding animation: dragging pages was laggy
  and unreliable on the resistive panel.
- Holding a finger still for one second turns the picture upside down or back; the choice is kept in NVS across
  reboots.
- Page 1 is laid out like Mission Center's GPU page: one-minute history charts for utilization and memory on the
  left, a clone of GPU Tweak III's Power Detector+ pin list on the right with the radiator and blower fan speeds
  right below it, as in the vendor tool, and power, temperatures and any throttle or off-angle warnings along the
  bottom.
- Page 2 has a GPU/VRAM temperature history, the card's four thermal-map zones, the power split between 16-pin
  and slot, pin current sum, core voltage, clock and fan duty.
- Page 3 lists peak values since the feed started, card tilt, the mileage counters, throttle reasons and
  performance state.
- Page 4 has volts and amps per 12V-2x6 pin, the spread between the highest and lowest pin (now and peak), and
  the eight board thermistors that the thermal map averages into four zones.
- Page 5 is a table of system values: PCIe link (from sysfs; the speed drops at idle, the width should not),
  the kernel's PCIe error totals, encoder, decoder and memory controller load, energy since the feed started,
  CPU load and temperature, the card's external fan header and its board controller firmware.
- Page 6 is a table of limits: power target range, clock offset ranges, the driver's temperature limits, and
  the three programs holding the most video memory.
- Page 7 draws the driver's voltage/frequency curve for the GPU clock, voltage to the right and clock upwards,
  with a dot at the current core voltage and clock. Below it is the card's sag: the angle between the board and
  level from the accelerometer, one sample per hour of uptime, 168 samples. `host/feed.py` keeps them in
  `~/.local/state/crowpanel-feed.json`, so the chart survives restarts of the feed and the panel.

The grey line in the temperature chart on page 2 is the GPU's slowdown temperature.

Both fans read 0 rpm at idle because the card stops them; under load they report normally
(checked: about 1360 and 1425 rpm at 440 W).

### Wire format

The PC sends the readings over the same USB serial port used for flashing:
one JSON object per line at 115200 baud.
`main/monitor.c` ignores lines that do not start with `{`, shows `--` for any missing key, and shows "no data"
when no frame arrived for 5 s.

```
{"gpu":{"temp":40,"vram":52,"load":9,"mem":1999,"mem_total":32607,"power":39.3,"limit":600,"fans":[0,0],"slowdown":93},
 "pins":[A x6], "zones":[C x4], "warn":["text", ...], "card":[["label","text"], ...], "info":[["label","text"], ...]}
```

`card` and `info` (up to 12 rows each) fill the tables on pages 2 and 3 as sent; peaks, warnings and their
thresholds are decided in `host/feed.py`. Pages 4 to 7 take more keys of the same kind:

```
 "conn":[["label","text"], ...], "therm":[...], "sys":[...], "lim":[...],   tables on pages 4 (two), 5 and 6
 "vf":[[mV,MHz] x up to 32], "vf_now":[mV,MHz],                             page 7, curve and dot
 "sag":[1/100 degree x up to 168]                                           page 7, oldest first
```

A line may be up to 8192 bytes.
The off-angle warning fires above 3 degrees off level on X or Y, which is our own limit: ASUS's threshold is not
known.

Besides frames the panel acts on two lines. `page N` switches to page N, counted from 1. `shot` answers with `SHOT <width> <height> <flipped>` on a line of
its own, followed by the framebuffer as raw RGB565, little-endian, 2 bytes per pixel (261,120 bytes, 23 s).
The picture is sent as it is on the panel, so upside down when `flipped` is 1; `host/shot.py` turns it back.

### Power Detector+

GPU temperature turns red at `slowdown`. Power Detector+ follows GPU Tweak III: pins in its order (reverse of the
MCU register), text `Pin N : 7.33A`, a green light below 9.2 A, and a red light with `#FF5555` text above 9.2 A or
at 0 A ("no detected amperage"). The icon on the left is a green check, or a red warning sign when any pin is red.

The rule and colours are from the vendor's strings; the list-with-fans arrangement follows a screenshot of the
vendor tool. Which of the driver's two fans is the radiator and which the blower is assumed from the vendor's
label order (first = radiator).
