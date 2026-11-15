/*
 * `zapp locate on|off|blink [seconds]`: the board's LED, to find the board on a bench. A plain LED
 * (the devicetree's led0) or the first pixel of an addressable strip (led-strip: the WS2812 on a
 * KB2040 or a Pro Micro RP2040), whichever the board has. `blink` with no seconds, or 0, blinks
 * until `off`. Prints `locate <what>`.
 */
#include <stdlib.h>
#include <string.h>

#include <zephyr/device.h>
#include <zephyr/kernel.h>
#include <zephyr/shell/shell.h>

#if DT_NODE_EXISTS(DT_ALIAS(led_strip))
#include <zephyr/drivers/led_strip.h>

static const struct device *const strip = DEVICE_DT_GET(DT_ALIAS(led_strip));

static void light(bool on)
{
	struct led_rgb px = {.r = on ? 0x30 : 0, .g = on ? 0x30 : 0, .b = on ? 0x30 : 0};

	led_strip_update_rgb(strip, &px, 1);
}

static bool ready(void)
{
	return device_is_ready(strip);
}
#else
#include <zephyr/drivers/gpio.h>

static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);

static void light(bool on)
{
	gpio_pin_set_dt(&led, on);
}

static bool ready(void)
{
	return gpio_is_ready_dt(&led) && gpio_pin_configure_dt(&led, GPIO_OUTPUT_INACTIVE) == 0;
}
#endif

static bool lit;
static int64_t until;   /* uptime in ms when blinking stops; 0 blinks until off */
static void blink(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(blinker, blink);

static void blink(struct k_work *work)
{
	if (until && k_uptime_get() >= until) {
		lit = false;
		light(false);
		return;
	}
	lit = !lit;
	light(lit);
	k_work_reschedule(&blinker, K_MSEC(250));
}

static int cmd_locate(const struct shell *sh, size_t argc, char **argv)
{
	if (!ready()) {
		shell_error(sh, "locate: the LED is not ready");
		return -ENODEV;
	}
	k_work_cancel_delayable(&blinker);
	if (strcmp(argv[1], "on") == 0 || strcmp(argv[1], "off") == 0) {
		lit = strcmp(argv[1], "on") == 0;
		light(lit);
		shell_print(sh, "locate %s", argv[1]);
		return 0;
	}
	if (strcmp(argv[1], "blink") == 0) {
		long s = argc > 2 ? strtol(argv[2], NULL, 10) : 0;

		until = s > 0 ? k_uptime_get() + s * 1000 : 0;
		k_work_reschedule(&blinker, K_NO_WAIT);
		shell_print(sh, "locate blink %ld", s > 0 ? s : 0);
		return 0;
	}
	shell_error(sh, "locate: on, off or blink [seconds]");
	return -EINVAL;
}

SHELL_SUBCMD_ADD((zapp), locate, NULL, "on|off|blink [seconds]: the board's LED", cmd_locate, 2, 1);
