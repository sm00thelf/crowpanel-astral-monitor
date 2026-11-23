#include "monitor.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "board.h"
#include "cJSON.h"
#include "driver/uart.h"
#include "esp_heap_caps.h"
#include "esp_lcd_panel_rgb.h"
#include "esp_lvgl_port.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"

#define RX_LINE_MAX 8192   // host/feed.py checks its frames against the same number
#define STALE_MS 5000
// GPU Tweak III's Power Detector+ (aseng.xml and a screenshot of the tool): per pin "Pin N : 7.33A" with a light,
// "Green: less than 9.2A. Red: More than 9.2A, or no detected amperage."; bad rows are #FF5555.
#define PIN_MAX_A 9.2
#define PD_GREEN lv_color_hex(0x55FF55)
#define PD_RED lv_color_hex(0xFF5555)
#define DEG "\xC2\xB0"

#define RED lv_color_hex(0xE0564F)
#define ORANGE lv_color_hex(0xF0A040)
#define GREY lv_color_hex(0x808088)
#define TABLE_ROWS 12
#define PAGES 7
#define VF_POINTS 32     // host/feed.py sends at most as many
#define VF_W 468
#define VF_H 100
#define SAG_POINTS 168   // hourly samples, one week of uptime; host/feed.py keeps as many
#define SAG_MAX 500      // top of the sag chart in 1/100 degrees

static lv_obj_t *util_chart, *mem_chart, *temp_chart, *mem_max, *stat[2], *fan_label[2], *warn_label, *zone_label[4],
    *card_table, *info_table, *pd_icon, *pin_light[6], *pin_label[6], *stale_label;
static lv_chart_series_t *util_ser, *mem_ser, *temp_ser, *vram_ser, *limit_ser, *vf_ser, *sag_ser;
// Tables on pages 4 to 6, each filled from the frame's key of the same index.
static const char *const extra_key[] = {"conn", "therm", "sys", "lim"};
static lv_obj_t *extra_table[4], *vf_chart, *vf_dot, *vf_range, *sag_chart, *sag_now;
static uint32_t last_rx;
static esp_lcd_panel_handle_t panel;

static double num(const cJSON *v)
{
    return cJSON_IsNumber(v) ? v->valuedouble : NAN;
}

static void pd_show(const cJSON *pins);

// The chart draws a series as one polyline and has no area fill, so shade below it as the line is queued.
static void fill_cb(lv_event_t *e)
{
    lv_draw_task_t *task = lv_event_get_draw_task(e);
    lv_draw_line_dsc_t *line = lv_draw_task_get_draw_dsc(task);
    if (lv_draw_task_get_type(task) != LV_DRAW_TASK_TYPE_LINE || line->base.part != LV_PART_ITEMS || !line->points ||
        lv_color_eq(line->color, GREY)) {   // grey = the slowdown limit, a bare line
        return;
    }
    lv_area_t box;
    lv_obj_get_coords(lv_event_get_target_obj(e), &box);
    lv_draw_triangle_dsc_t tri;
    lv_draw_triangle_dsc_init(&tri);
    tri.color = line->color;
    tri.opa = LV_OPA_40;
    lv_draw_rect_dsc_t rect;
    lv_draw_rect_dsc_init(&rect);
    rect.bg_color = line->color;
    rect.bg_opa = LV_OPA_40;
    for (int i = 1; i < line->point_cnt; i++) {
        lv_point_precise_t a = line->points[i - 1], b = line->points[i];
        if (a.y == LV_DRAW_LINE_POINT_NONE || b.y == LV_DRAW_LINE_POINT_NONE) {
            continue;
        }
        int32_t low = LV_MAX(a.y, b.y);
        if (a.y != b.y) {
            tri.p[0] = a;
            tri.p[1] = b;
            tri.p[2].x = a.y < b.y ? a.x : b.x;
            tri.p[2].y = low;
            lv_draw_triangle(line->base.layer, &tri);
        }
        lv_area_t below = {a.x, low, b.x - 1, box.y2};
        lv_draw_rect(line->base.layer, &rect, &below);
    }
}

static lv_obj_t *label(lv_obj_t *parent, const char *text, int x, int y)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_label_set_text(l, text);
    lv_obj_set_pos(l, x, y);
    return l;
}

// Two text columns without the theme's cell borders; rows come from the host as [label, text] pairs.
static lv_obj_t *table(lv_obj_t *parent, int x, int y, int w0, int w1)
{
    lv_obj_t *t = lv_table_create(parent);
    lv_obj_set_pos(t, x, y);
    lv_table_set_column_count(t, 2);
    lv_table_set_column_width(t, 0, w0);
    lv_table_set_column_width(t, 1, w1);
    lv_table_set_cell_value(t, 0, 0, "--");
    lv_obj_set_style_bg_opa(t, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(t, 0, 0);
    lv_obj_set_style_bg_opa(t, LV_OPA_TRANSP, LV_PART_ITEMS);
    lv_obj_set_style_border_width(t, 0, LV_PART_ITEMS);
    lv_obj_set_style_pad_all(t, 4, LV_PART_ITEMS);
    return t;
}

static void rows(lv_obj_t *t, const cJSON *arr)
{
    int n = LV_MIN(cJSON_GetArraySize(arr), TABLE_ROWS);
    if (n <= 0) {
        return;
    }
    lv_table_set_row_count(t, n);
    for (int i = 0; i < n; i++) {
        const cJSON *row = cJSON_GetArrayItem(arr, i);
        for (int c = 0; c < 2; c++) {
            const char *s = cJSON_GetStringValue(cJSON_GetArrayItem(row, c));
            lv_table_set_cell_value(t, i, c, s ? s : "");
        }
    }
}

// One minute of history at the feed's one frame per second, 0..100 (% or degrees C).
static lv_obj_t *chart(lv_obj_t *parent, int y, int w, int h, lv_chart_series_t **ser)
{
    lv_obj_t *c = lv_chart_create(parent);
    lv_obj_set_pos(c, 6, y);
    lv_obj_set_size(c, w, h);
    lv_chart_set_point_count(c, 60);
    lv_chart_set_div_line_count(c, 5, 12);
    lv_obj_set_style_pad_all(c, 0, 0);
    lv_obj_set_style_radius(c, 4, 0);
    lv_obj_set_style_bg_color(c, lv_color_hex(0x1B1B1F), 0);
    lv_obj_set_style_border_color(c, RED, 0);
    lv_obj_set_style_border_width(c, 1, 0);
    lv_obj_set_style_line_color(c, lv_color_hex(0x3A3A40), 0);
    lv_obj_set_style_line_width(c, 1, LV_PART_ITEMS);
    lv_obj_set_style_size(c, 0, 0, LV_PART_INDICATOR);   // no point markers
    *ser = lv_chart_add_series(c, RED, LV_CHART_AXIS_PRIMARY_Y);
    lv_obj_add_event_cb(c, fill_cb, LV_EVENT_DRAW_TASK_ADDED, NULL);
    lv_obj_set_send_draw_task_events(c, true);
    return c;
}

static void stale_cb(lv_timer_t *timer)
{
    if (lv_tick_elaps(last_rx) > STALE_MS) {
        lv_obj_set_hidden(stale_label, false);
    }
}

static lv_obj_t *page[PAGES];
static int page_now;

// Pages switch at once on a sideways swipe. Dragging a scrolling tileview was tried first: redrawing whole pages
// was laggy, and the resistive panel's noisy last sample made quick swipes bounce back or go the wrong way.
static void show_page(int next)
{
    if (next < 0 || next >= PAGES || next == page_now) {
        return;
    }
    lv_obj_set_hidden(page[page_now], true);
    lv_obj_set_hidden(page[next], false);
    page_now = next;
}

static void swipe_cb(lv_event_t *e)
{
    lv_dir_t dir = lv_indev_get_gesture_dir(lv_indev_active());
    show_page(page_now + (dir == LV_DIR_LEFT) - (dir == LV_DIR_RIGHT));
}

static void flip_store(bool write, uint8_t *flipped)
{
    nvs_handle_t h;
    if (nvs_open("ui", NVS_READWRITE, &h) != ESP_OK) {
        return;   // without storage the flip still works, it just is not remembered
    }
    if (write) {
        nvs_set_u8(h, "flip", *flipped);
        nvs_commit(h);
    } else {
        nvs_get_u8(h, "flip", flipped);
    }
    nvs_close(h);
}

// Holding a finger still for a second turns the picture upside down, or back. Touch follows: LVGL rotates the
// touch points with the display.
static void hold_cb(lv_event_t *e)
{
    static lv_point_t pressed_at;
    lv_point_t p;
    lv_indev_get_point(lv_indev_active(), &p);
    if (lv_event_get_code(e) == LV_EVENT_PRESSED) {
        pressed_at = p;
        return;
    }
    if (LV_ABS(p.x - pressed_at.x) > 20 || LV_ABS(p.y - pressed_at.y) > 20) {
        return;   // a slow swipe, not a hold
    }
    uint8_t flipped = lv_display_get_rotation(NULL) == LV_DISPLAY_ROTATION_0;
    lv_display_set_rotation(NULL, flipped ? LV_DISPLAY_ROTATION_180 : LV_DISPLAY_ROTATION_0);
    flip_store(true, &flipped);
    lv_indev_wait_release(lv_indev_active());
}

// Seven pages, swiped sideways. Page 1 follows Mission Center's GPU page: history charts left, caption/value
// pairs right, Power Detector+ below. Page 2: temperatures, thermal map, power split. Page 3: peaks and card info.
// Page 4: connector and board thermistors. Pages 5 and 6: system and limits tables. Page 7: V/F curve and sag.
static void ui_create(void)
{
    lv_obj_set_style_bg_color(lv_screen_active(), lv_color_hex(0x222226), 0);
    lv_obj_set_scrollable(lv_screen_active(), false);
    lv_obj_add_event_cb(lv_screen_active(), swipe_cb, LV_EVENT_GESTURE, NULL);   // bubbles up from the widgets
    // Touch is read about 85 times a second, so the default minimum of 3 px per read rejected every drag slower
    // than about 250 px/s (measured). 0 = any 50 px of net sideways movement in one press counts.
    lv_indev_set_gesture_min_velocity(lv_indev_get_next(NULL), 0);
    lv_indev_set_long_press_time(lv_indev_get_next(NULL), 1000);
    lv_indev_add_event_cb(lv_indev_get_next(NULL), hold_cb, LV_EVENT_PRESSED, NULL);
    lv_indev_add_event_cb(lv_indev_get_next(NULL), hold_cb, LV_EVENT_LONG_PRESSED, NULL);
    uint8_t flipped = 0;
    flip_store(false, &flipped);
    if (flipped) {
        lv_display_set_rotation(NULL, LV_DISPLAY_ROTATION_180);
    }
    for (int i = 0; i < PAGES; i++) {
        page[i] = lv_obj_create(lv_screen_active());
        lv_obj_remove_style_all(page[i]);
        lv_obj_set_size(page[i], lv_pct(100), lv_pct(100));
        lv_obj_set_scrollable(page[i], false);
        lv_obj_set_hidden(page[i], i != 0);
    }
    lv_obj_t *scr = page[0], *p2 = page[1], *p3 = page[2];

    label(p2, "Temperature over 1 minute", 6, 4);
    lv_obj_t *legend = label(p2, "VRAM", 0, 0);
    lv_obj_set_style_text_color(legend, ORANGE, 0);
    lv_obj_align(legend, LV_ALIGN_TOP_RIGHT, -224, 4);
    temp_chart = chart(p2, 22, 250, 130, &temp_ser);
    vram_ser = lv_chart_add_series(temp_chart, ORANGE, LV_CHART_AXIS_PRIMARY_Y);
    limit_ser = lv_chart_add_series(temp_chart, GREY, LV_CHART_AXIS_PRIMARY_Y);   // the GPU's slowdown temperature
    label(p2, "Thermal map", 6, 160);
    for (int i = 0; i < 4; i++) {
        zone_label[i] = label(p2, "--", 6 + 126 * (i % 2), 180 + 30 * (i / 2));
        lv_obj_set_style_text_font(zone_label[i], &lv_font_montserrat_20, 0);
    }
    card_table = table(p2, 262, 0, 64, 150);
    info_table = table(p3, 2, 0, 200, 272);

    lv_obj_t *p4 = page[3], *p7 = page[6];
    label(p4, "12V-2x6 connector", 6, 4);
    label(p4, "Board thermistors", 286, 4);
    extra_table[0] = table(p4, 2, 20, 100, 178);
    extra_table[1] = table(p4, 282, 20, 80, 110);
    extra_table[2] = table(page[4], 2, 0, 250, 222);
    extra_table[3] = table(page[5], 2, 0, 220, 252);

    // Voltage to the right, clock upwards; the dot is where the card is now. The axes follow the curve sent.
    label(p7, "V/F curve", 6, 4);
    vf_range = label(p7, "", 0, 0);
    lv_obj_align(vf_range, LV_ALIGN_TOP_RIGHT, -6, 4);
    vf_chart = chart(p7, 22, VF_W, VF_H, &vf_ser);
    lv_chart_set_type(vf_chart, LV_CHART_TYPE_SCATTER);
    lv_chart_set_point_count(vf_chart, VF_POINTS);
    lv_obj_set_style_line_width(vf_chart, 2, LV_PART_ITEMS);
    vf_dot = lv_obj_create(vf_chart);
    lv_obj_set_size(vf_dot, 9, 9);
    lv_obj_set_style_radius(vf_dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_border_width(vf_dot, 0, 0);
    lv_obj_set_style_bg_color(vf_dot, PD_GREEN, 0);
    lv_obj_set_hidden(vf_dot, true);
    label(p7, "Card sag, hourly", 6, 130);
    sag_now = label(p7, "", 0, 0);
    lv_obj_align(sag_now, LV_ALIGN_TOP_RIGHT, -6, 130);
    sag_chart = chart(p7, 148, VF_W, 118, &sag_ser);
    lv_chart_set_point_count(sag_chart, SAG_POINTS);
    lv_chart_set_axis_range(sag_chart, LV_CHART_AXIS_PRIMARY_Y, 0, SAG_MAX);
    lv_chart_set_all_values(sag_chart, sag_ser, LV_CHART_POINT_NONE);

    label(scr, "Utilization over 1 minute", 6, 4);
    lv_obj_align(label(scr, "100%", 0, 0), LV_ALIGN_TOP_RIGHT, -188, 4);
    util_chart = chart(scr, 22, 286, 104, &util_ser);
    label(scr, "Memory usage over 1 minute", 6, 130);
    mem_max = label(scr, "", 0, 0);
    lv_obj_align(mem_max, LV_ALIGN_TOP_RIGHT, -188, 130);
    mem_chart = chart(scr, 148, 286, 66, &mem_ser);

    // Right column as on GPU Tweak III's "Thermal map & Power Detector +" page: the pin list with a light per
    // pin, and the two fan speeds right below it. This is the LC card, so the fans are radiator and blower.
    pd_icon = label(scr, LV_SYMBOL_OK, 300, 4);
    label(scr, "Power Detector +", 324, 4);
    for (int i = 0; i < 6; i++) {
        int y = 28 + 20 * i;
        pin_light[i] = lv_obj_create(scr);
        lv_obj_set_pos(pin_light[i], 304, y + 3);
        lv_obj_set_size(pin_light[i], 10, 10);
        lv_obj_set_style_radius(pin_light[i], LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_border_width(pin_light[i], 0, 0);
        pin_label[i] = label(scr, "", 324, y);
    }
    fan_label[0] = label(scr, "", 300, 156);
    fan_label[1] = label(scr, "", 300, 178);

    // Bottom strip: power and temperature, then throttle and off-angle warnings (empty when there are none).
    static const char *const captions[] = {"Power Draw", "Temp GPU / VRAM"};
    for (int i = 0; i < 2; i++) {
        label(scr, captions[i], 6 + 196 * i, 222);
        stat[i] = label(scr, "--", 6 + 196 * i, 240);
        lv_obj_set_style_text_font(stat[i], &lv_font_montserrat_20, 0);
    }
    warn_label = label(scr, "", 330, 226);
    lv_obj_set_width(warn_label, 146);
    lv_obj_set_style_text_color(warn_label, PD_RED, 0);
    pd_show(NULL);

    stale_label = lv_label_create(lv_layer_top());
    lv_label_set_text(stale_label, "no data");
    lv_obj_set_style_text_color(stale_label, RED, 0);
    lv_obj_align(stale_label, LV_ALIGN_BOTTOM_RIGHT, -4, -2);
    lv_timer_create(stale_cb, 1000, NULL);
}

// LVGL's own printf has no float support here, so format with libc. A NaN argument means "not sent": show "--".
static void show(lv_obj_t *l, double a, double b, const char *fmt)
{
    char buf[48] = "--";
    if (!isnan(a + b)) {
        snprintf(buf, sizeof buf, fmt, a, b);
    }
    lv_label_set_text(l, buf);
}

// pins: the "pins" array of six amps, or NULL when the host sent none (then everything is grey).
static void pd_show(const cJSON *pins)
{
    bool known = false, alert = false;
    for (int i = 0; i < 6; i++) {
        double a = num(cJSON_GetArrayItem(pins, i));
        bool bad = a > PIN_MAX_A || a <= 0;   // "no detected amperage" taken as a zero reading
        char buf[24];
        if (isnan(a)) {
            snprintf(buf, sizeof buf, "Pin %d : --", i + 1);
        } else {
            snprintf(buf, sizeof buf, "Pin %d : %.2fA", i + 1, a);
            known = true;
            alert |= bad;
        }
        lv_label_set_text(pin_label[i], buf);
        lv_color_t grey = lv_color_hex(0x808088);
        lv_obj_set_style_text_color(pin_label[i], isnan(a) ? grey : bad ? PD_RED : lv_color_white(), 0);
        lv_obj_set_style_bg_color(pin_light[i], isnan(a) ? grey : bad ? PD_RED : PD_GREEN, 0);
    }
    lv_label_set_text(pd_icon, alert ? LV_SYMBOL_WARNING : LV_SYMBOL_OK);
    lv_obj_set_style_text_color(pd_icon, !known ? lv_color_hex(0x808088) : alert ? PD_RED : PD_GREEN, 0);
}

static void set_red(lv_obj_t *l, bool red)
{
    if (red) {
        lv_obj_set_style_text_color(l, RED, 0);
    } else {
        lv_obj_remove_local_style_prop(l, LV_STYLE_TEXT_COLOR, 0);
    }
}

// Every lookup tolerates a missing or non-numeric item: cJSON returns NULL, num() gives NaN, the widget shows "--".
static void apply(const cJSON *root)
{
    const cJSON *g = cJSON_GetObjectItem(root, "gpu"), *fans = cJSON_GetObjectItem(g, "fans");
    double load = num(cJSON_GetObjectItem(g, "load")), temp = num(cJSON_GetObjectItem(g, "temp"));
    double mem = num(cJSON_GetObjectItem(g, "mem")) / 1024, total = num(cJSON_GetObjectItem(g, "mem_total")) / 1024;

    show(stat[0], num(cJSON_GetObjectItem(g, "power")), num(cJSON_GetObjectItem(g, "limit")), "%.1f W / %.0f W");
    show(stat[1], temp, num(cJSON_GetObjectItem(g, "vram")), "%.0f / %.0f " DEG "C");
    set_red(stat[1], temp >= num(cJSON_GetObjectItem(g, "slowdown")));   // false when either is NaN
    show(fan_label[0], num(cJSON_GetArrayItem(fans, 0)), 0, "Rad. fans : %.0f rpm");
    show(fan_label[1], num(cJSON_GetArrayItem(fans, 1)), 0, "Blower : %.0f rpm");
    show(mem_max, total, 0, "%.1f GiB");
    lv_obj_align(mem_max, LV_ALIGN_TOP_RIGHT, -188, 130);

    lv_chart_set_next_value(util_chart, util_ser, isnan(load) ? LV_CHART_POINT_NONE : (int32_t)load);
    lv_chart_set_next_value(mem_chart, mem_ser, isnan(mem / total) ? LV_CHART_POINT_NONE : (int32_t)(100 * mem / total));

    pd_show(cJSON_GetObjectItem(root, "pins"));

    char buf[96] = "";
    const cJSON *w;
    cJSON_ArrayForEach(w, cJSON_GetObjectItem(root, "warn")) {
        const char *s = cJSON_GetStringValue(w);
        size_t used = strlen(buf);
        if (s) {
            snprintf(buf + used, sizeof buf - used, "%s" LV_SYMBOL_WARNING " %s", used ? "\n" : "", s);
        }
    }
    lv_label_set_text(warn_label, buf);

    double vram = num(cJSON_GetObjectItem(g, "vram"));
    lv_chart_set_next_value(temp_chart, temp_ser, isnan(temp) ? LV_CHART_POINT_NONE : (int32_t)temp);
    lv_chart_set_next_value(temp_chart, vram_ser, isnan(vram) ? LV_CHART_POINT_NONE : (int32_t)vram);
    const cJSON *zones = cJSON_GetObjectItem(root, "zones");
    for (int i = 0; i < 4; i++) {
        snprintf(buf, sizeof buf, "Z%d  %%.1f " DEG "C", i + 1);
        show(zone_label[i], num(cJSON_GetArrayItem(zones, i)), 0, buf);
    }
    rows(card_table, cJSON_GetObjectItem(root, "card"));
    rows(info_table, cJSON_GetObjectItem(root, "info"));
    for (int i = 0; i < 4; i++) {
        rows(extra_table[i], cJSON_GetObjectItem(root, extra_key[i]));
    }
    double slow = num(cJSON_GetObjectItem(g, "slowdown"));
    lv_chart_set_all_values(temp_chart, limit_ser, isnan(slow) ? LV_CHART_POINT_NONE : (int32_t)slow);

    // V/F curve: [mV, MHz] points, and "vf_now" in the same units.
    const cJSON *vf = cJSON_GetObjectItem(root, "vf"), *now = cJSON_GetObjectItem(root, "vf_now");
    double x0 = INFINITY, x1 = -INFINITY, y0 = INFINITY, y1 = -INFINITY;
    for (int i = 0; i < VF_POINTS; i++) {
        const cJSON *pt = cJSON_GetArrayItem(vf, i);   // NULL past the end
        double x = num(cJSON_GetArrayItem(pt, 0)), y = num(cJSON_GetArrayItem(pt, 1));
        bool ok = !isnan(x) && !isnan(y);
        lv_chart_set_series_value_by_id2(vf_chart, vf_ser, i, ok ? (int32_t)x : 0, ok ? (int32_t)y : LV_CHART_POINT_NONE);
        if (ok) {
            x0 = fmin(x0, x), x1 = fmax(x1, x), y0 = fmin(y0, y), y1 = fmax(y1, y);
        }
    }
    double nx = num(cJSON_GetArrayItem(now, 0)), ny = num(cJSON_GetArrayItem(now, 1));
    bool drawn = x1 > x0 && y1 > y0;
    lv_obj_set_hidden(vf_dot, !drawn || isnan(nx) || isnan(ny));
    if (drawn) {
        lv_chart_set_axis_range(vf_chart, LV_CHART_AXIS_PRIMARY_X, (int32_t)x0, (int32_t)x1);
        lv_chart_set_axis_range(vf_chart, LV_CHART_AXIS_PRIMARY_Y, (int32_t)y0, (int32_t)y1);
        if (!isnan(nx) && !isnan(ny)) {
            lv_obj_set_pos(vf_dot, (int32_t)((nx - x0) * (VF_W - 3) / (x1 - x0)) - 4,
                           (int32_t)((y1 - ny) * (VF_H - 3) / (y1 - y0)) - 4);
        }
        snprintf(buf, sizeof buf, "%.0f-%.0f mV, %.0f-%.0f MHz", x0, x1, y0, y1);
        lv_label_set_text(vf_range, buf);
        lv_obj_align(vf_range, LV_ALIGN_TOP_RIGHT, -6, 4);
    }
    lv_chart_refresh(vf_chart);

    // Sag: the newest sample at the right edge, 1/100 degrees.
    const cJSON *sag = cJSON_GetObjectItem(root, "sag");
    int have = cJSON_GetArraySize(sag);
    for (int i = 0; i < SAG_POINTS; i++) {
        double v = num(cJSON_GetArrayItem(sag, i - (SAG_POINTS - have)));   // NULL for a negative index
        lv_chart_set_series_value_by_id(sag_chart, sag_ser, i, isnan(v) ? LV_CHART_POINT_NONE : (int32_t)v);
    }
    lv_chart_refresh(sag_chart);
    show(sag_now, num(cJSON_GetArrayItem(sag, have - 1)) / 100, SAG_MAX / 100, "now %.2f" DEG ", top %.0f" DEG);
    lv_obj_align(sag_now, LV_ALIGN_TOP_RIGHT, -6, 130);
}

static void handle_line(const char *line)
{
    cJSON *root = cJSON_Parse(line);   // a truncated or garbled line fails here and is dropped
    if (!cJSON_IsObject(root)) {
        cJSON_Delete(root);
        return;
    }
    if (lvgl_port_lock(1000)) {
        apply(root);
        last_rx = lv_tick_get();
        lv_obj_set_hidden(stale_label, true);
        lvgl_port_unlock();
    }
    cJSON_Delete(root);
}

// Answer to a "shot" line, for host/shot.py: a header line, then the framebuffer as it is on the panel (RGB565,
// little-endian, upside down when flipped). Sending takes 23 s at 115200 baud; frames arriving meanwhile are lost.
static void screenshot(void)
{
    void *fb;
    size_t size = LCD_H_RES * LCD_V_RES * 2;
    uint8_t *copy = heap_caps_malloc(size, MALLOC_CAP_SPIRAM);   // internal RAM has no room for a second frame
    if (!copy || esp_lcd_rgb_panel_get_frame_buffer(panel, 1, &fb) != ESP_OK || !lvgl_port_lock(1000)) {
        free(copy);
        return;
    }
    memcpy(copy, fb, size);   // under the lock, so not halfway through a redraw
    int flipped = lv_display_get_rotation(NULL) == LV_DISPLAY_ROTATION_180;
    lvgl_port_unlock();
    char hdr[32];
    int n = snprintf(hdr, sizeof hdr, "\nSHOT %d %d %d\n", LCD_H_RES, LCD_V_RES, flipped);
    uart_write_bytes(UART_NUM_0, hdr, n);
    uart_write_bytes(UART_NUM_0, copy, size);
    free(copy);
}

static void rx_task(void *arg)
{
    static char line[RX_LINE_MAX];
    uint8_t chunk[128];
    size_t len = 0;
    for (;;) {
        int n = uart_read_bytes(UART_NUM_0, chunk, sizeof chunk, pdMS_TO_TICKS(20));
        for (int i = 0; i < n; i++) {
            if (chunk[i] != '\n') {
                if (len < sizeof line - 1) {
                    line[len++] = chunk[i];
                }
                continue;
            }
            line[len] = 0;
            len = 0;
            if (line[0] == '{') {
                handle_line(line);
            } else if (!strcmp(line, "shot")) {
                screenshot();
            } else if (!strncmp(line, "page ", 5) && lvgl_port_lock(1000)) {   // "page 4", as host/shot.py --page
                show_page(atoi(line + 5) - 1);
                lvgl_port_unlock();
            }
        }
    }
}

void monitor_start(esp_lcd_panel_handle_t lcd)
{
    panel = lcd;
    esp_err_t err = nvs_flash_init();   // remembers the screen flip
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    if (lvgl_port_lock(0)) {
        ui_create();
        lvgl_port_unlock();
    }
    // UART0 is the console (CH340, 115200 already set); log output keeps working, the driver adds buffered RX.
    ESP_ERROR_CHECK(uart_driver_install(UART_NUM_0, RX_LINE_MAX, 0, 0, NULL, 0));
    // A feed that is already sending overflows the hardware FIFO during boot, before the driver is there to
    // empty it, and reception stays broken afterwards (seen: no frame ever accepted). Start from an empty FIFO.
    uart_flush_input(UART_NUM_0);
    xTaskCreate(rx_task, "monitor_rx", 6144, NULL, 3, NULL);
}
