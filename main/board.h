// CrowPanel 4.3" (ESP32-S3, 480x272 RGB, XPT2046). See HARDWARE.md.
#pragma once

#define LCD_H_RES 480
#define LCD_V_RES 272

// Refresh rate = LCD_PCLK_HZ / (H_TOTAL * V_TOTAL) = LCD_PCLK_HZ / 158360.
// 9.5 MHz -> 60 Hz, 12 MHz -> 76 Hz, 15 MHz -> 95 Hz. LVGL renders ~90 FPS, so the panel is the limit:
// raise this until the picture drifts or flickers, then step back.
#define LCD_PCLK_HZ 12000000
#define LCD_HSYNC_PULSE 4
#define LCD_HSYNC_BACK 43
#define LCD_HSYNC_FRONT 8
#define LCD_VSYNC_PULSE 4
#define LCD_VSYNC_BACK 12
#define LCD_VSYNC_FRONT 8
#define LCD_H_TOTAL (LCD_H_RES + LCD_HSYNC_PULSE + LCD_HSYNC_BACK + LCD_HSYNC_FRONT)
#define LCD_V_TOTAL (LCD_V_RES + LCD_VSYNC_PULSE + LCD_VSYNC_BACK + LCD_VSYNC_FRONT)

// Height of LVGL's draw buffer in internal RAM (LCD_H_RES * LCD_DRAW_LINES * 2 bytes).
#define LCD_DRAW_LINES 20

#define LCD_PIN_DE 40
#define LCD_PIN_VSYNC 41
#define LCD_PIN_HSYNC 39
#define LCD_PIN_PCLK 42
#define LCD_PIN_BACKLIGHT 2
// RGB565, LSB first: B0..B4, G0..G5, R0..R4
#define LCD_PINS_DATA {8, 3, 46, 9, 1, 5, 6, 7, 15, 16, 4, 45, 48, 47, 21, 14}

#define TOUCH_SPI_HOST SPI2_HOST
#define TOUCH_PIN_MOSI 11
#define TOUCH_PIN_MISO 13
#define TOUCH_PIN_SCK 12
#define TOUCH_PIN_CS 0

// Raw 12-bit ADC value at each screen edge, measured on this unit (2026-11-23) by tapping targets 30 px in
// from each corner and extrapolating. X runs left to right, the opposite of Elecrow's defaults.
#define TOUCH_RAW_X_LEFT 172
#define TOUCH_RAW_X_RIGHT 3883
#define TOUCH_RAW_Y_TOP 325
#define TOUCH_RAW_Y_BOTTOM 3800
