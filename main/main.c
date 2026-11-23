#include "board.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_rgb.h"
#include "esp_lcd_touch_xpt2046.h"
#include "esp_log.h"
#include "esp_lvgl_port.h"
#include "monitor.h"

static const char *TAG = "ui";

static esp_lcd_panel_handle_t lcd_init(void)
{
    esp_lcd_rgb_panel_config_t cfg = {
        .clk_src = LCD_CLK_SRC_DEFAULT,
        .timings = {
            .pclk_hz = LCD_PCLK_HZ,
            .h_res = LCD_H_RES,
            .v_res = LCD_V_RES,
            .hsync_pulse_width = LCD_HSYNC_PULSE,
            .hsync_back_porch = LCD_HSYNC_BACK,
            .hsync_front_porch = LCD_HSYNC_FRONT,
            .vsync_pulse_width = LCD_VSYNC_PULSE,
            .vsync_back_porch = LCD_VSYNC_BACK,
            .vsync_front_porch = LCD_VSYNC_FRONT,
            .flags.pclk_active_neg = true,
        },
        .data_width = 16,
        .bits_per_pixel = 16,
        // One framebuffer in internal RAM (flags.fb_in_psram left off). The 2 MB quad PSRAM is too slow to feed
        // the panel at 60 Hz: measured, a PSRAM framebuffer made the picture jump whenever LVGL was drawing.
        .num_fbs = 1,
        .hsync_gpio_num = LCD_PIN_HSYNC,
        .vsync_gpio_num = LCD_PIN_VSYNC,
        .de_gpio_num = LCD_PIN_DE,
        .pclk_gpio_num = LCD_PIN_PCLK,
        .disp_gpio_num = -1,
        .data_gpio_nums = LCD_PINS_DATA,
    };
    esp_lcd_panel_handle_t panel;
    ESP_ERROR_CHECK(esp_lcd_new_rgb_panel(&cfg, &panel));
    ESP_ERROR_CHECK(esp_lcd_panel_reset(panel));
    ESP_ERROR_CHECK(esp_lcd_panel_init(panel));
    return panel;
}

// XPT2046_CONVERT_ADC_TO_COORDS is off, so the driver hands over raw 12-bit ADC values; map them to pixels here.
static void touch_calibrate(esp_lcd_touch_handle_t tp, uint16_t *x, uint16_t *y, uint16_t *strength,
                            uint8_t *count, uint8_t max_count)
{
    for (int i = 0; i < *count; i++) {
        int px = ((int)x[i] - TOUCH_RAW_X_LEFT) * (LCD_H_RES - 1) / (TOUCH_RAW_X_RIGHT - TOUCH_RAW_X_LEFT);
        int py = ((int)y[i] - TOUCH_RAW_Y_TOP) * (LCD_V_RES - 1) / (TOUCH_RAW_Y_BOTTOM - TOUCH_RAW_Y_TOP);
        x[i] = px < 0 ? 0 : px >= LCD_H_RES ? LCD_H_RES - 1 : px;
        y[i] = py < 0 ? 0 : py >= LCD_V_RES ? LCD_V_RES - 1 : py;
    }
}

static esp_lcd_touch_handle_t touch_init(void)
{
    spi_bus_config_t bus = {
        .mosi_io_num = TOUCH_PIN_MOSI,
        .miso_io_num = TOUCH_PIN_MISO,
        .sclk_io_num = TOUCH_PIN_SCK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
    };
    ESP_ERROR_CHECK(spi_bus_initialize(TOUCH_SPI_HOST, &bus, SPI_DMA_CH_AUTO));

    esp_lcd_panel_io_handle_t io;
    esp_lcd_panel_io_spi_config_t io_cfg = ESP_LCD_TOUCH_IO_SPI_XPT2046_CONFIG(TOUCH_PIN_CS);
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(TOUCH_SPI_HOST, &io_cfg, &io));

    esp_lcd_touch_config_t cfg = {
        .x_max = LCD_H_RES,
        .y_max = LCD_V_RES,
        .rst_gpio_num = -1,
        .int_gpio_num = -1, // polled by LVGL; the IRQ line (GPIO 36) is not needed
        .process_coordinates = touch_calibrate,
    };
    esp_lcd_touch_handle_t tp;
    ESP_ERROR_CHECK(esp_lcd_touch_new_spi_xpt2046(io, &cfg, &tp));
    return tp;
}

void app_main(void)
{
    ESP_LOGI(TAG, "Panel refresh: %.1f Hz at %d Hz pixel clock",
             (double)LCD_PCLK_HZ / (LCD_H_TOTAL * LCD_V_TOTAL), LCD_PCLK_HZ);

    esp_lcd_panel_handle_t panel = lcd_init();

    const lvgl_port_cfg_t port_cfg = ESP_LVGL_PORT_INIT_CONFIG();
    ESP_ERROR_CHECK(lvgl_port_init(&port_cfg));

    const lvgl_port_display_cfg_t disp_cfg = {
        .panel_handle = panel,
        .buffer_size = LCD_H_RES * LCD_DRAW_LINES,
        .hres = LCD_H_RES,
        .vres = LCD_V_RES,
        .color_format = LV_COLOR_FORMAT_RGB565,
    };
    const lvgl_port_display_rgb_cfg_t rgb_cfg = {0};
    lv_display_t *disp = lvgl_port_add_disp_rgb(&disp_cfg, &rgb_cfg);
    assert(disp);

    const lvgl_port_touch_cfg_t touch_cfg = {
        .disp = disp,
        .handle = touch_init(),
    };
    assert(lvgl_port_add_touch(&touch_cfg));

    monitor_start(panel);

    // Backlight on only once the first frame is ready. shortcut: plain on/off, add LEDC PWM when dimming is needed.
    gpio_set_direction(LCD_PIN_BACKLIGHT, GPIO_MODE_OUTPUT);
    gpio_set_level(LCD_PIN_BACKLIGHT, 1);

    // The framebuffer takes most of the internal RAM; this line shows what is left for the application.
    ESP_LOGI(TAG, "Internal RAM free: %u bytes, largest block %u", (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
}
