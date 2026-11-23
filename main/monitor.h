#pragma once

#include "esp_lcd_panel_ops.h"

// Builds the GPU monitor screen and starts receiving JSON lines on UART0 (see HARDWARE.md, host/feed.py).
void monitor_start(esp_lcd_panel_handle_t lcd);
