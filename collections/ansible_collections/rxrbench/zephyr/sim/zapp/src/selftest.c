/*
 * `zapp test <name>`: checks of the board's own hardware, one line each, for a layout's device
 * tests (`bench_tests`: a command and the line it expects):
 *
 *     test flash pass 64 bytes written, read back and deleted
 *     test temp fail no sensor
 *
 * flash    a setting written to the storage partition, read back, compared and deleted
 * entropy  two draws from the random source, not equal and not all zero
 * temp     the die temperature, between 0 and 90 C (on a chip with a sensor: the RP2040's)
 * wifi     the link up with an address (in a build with WiFi)
 * all      each of the above this build has; `zapp test list` names them
 */
#include <errno.h>
#include <string.h>

#include <zephyr/device.h>
#include <zephyr/drivers/sensor.h>
#include <zephyr/kernel.h>
#include <zephyr/random/random.h>
#include <zephyr/settings/settings.h>
#include <zephyr/shell/shell.h>

#include "zapp.h"

#define SCRATCH "test/scratch"
#define DIE_TEMP DT_ALIAS(die_temp0)
#define HAS_TEMP (DT_NODE_HAS_STATUS(DIE_TEMP, okay) && IS_ENABLED(CONFIG_SENSOR))

struct readback {
	uint8_t buf[64];
	ssize_t len;
};

static int read_cb(const char *key, size_t len, settings_read_cb rd, void *cb_arg, void *param)
{
	struct readback *r = param;

	if (settings_name_next(key, NULL) != 0) {
		return 0;
	}
	r->len = len > sizeof(r->buf) ? -EFBIG : rd(cb_arg, r->buf, len);
	return 0;
}

static bool t_flash(const struct shell *sh)
{
	uint8_t want[64];
	struct readback got = {.len = -ENOENT};

	sys_rand_get(want, sizeof(want));
	int rc = settings_save_one(SCRATCH, want, sizeof(want));

	if (!rc) {
		rc = settings_load_subtree_direct(SCRATCH, read_cb, &got);
	}
	settings_delete(SCRATCH);
	if (rc || got.len != sizeof(want) || memcmp(got.buf, want, sizeof(want))) {
		shell_print(sh, "test flash fail rc %d, read %d bytes", rc, (int)got.len);
		return false;
	}
	shell_print(sh, "test flash pass %d bytes written, read back and deleted", (int)sizeof(want));
	return true;
}

static bool t_entropy(const struct shell *sh)
{
	uint8_t a[32], b[32], zero[32] = {0};

	sys_rand_get(a, sizeof(a));
	sys_rand_get(b, sizeof(b));
	if (!memcmp(a, b, sizeof(a)) || !memcmp(a, zero, sizeof(a))) {
		shell_print(sh, "test entropy fail two draws equal or zero");
		return false;
	}
	shell_print(sh, "test entropy pass two 32-byte draws differ");
	return true;
}

static bool t_temp(const struct shell *sh)
{
#if HAS_TEMP
	const struct device *dev = DEVICE_DT_GET(DIE_TEMP);
	struct sensor_value v;

	if (!device_is_ready(dev) || sensor_sample_fetch(dev) ||
	    sensor_channel_get(dev, SENSOR_CHAN_DIE_TEMP, &v)) {
		shell_print(sh, "test temp fail the sensor did not answer");
		return false;
	}
	if (v.val1 < 0 || v.val1 > 90) {
		shell_print(sh, "test temp fail %d C", v.val1);
		return false;
	}
	shell_print(sh, "test temp pass %d C", v.val1);
	return true;
#else
	shell_print(sh, "test temp fail no sensor");
	return false;
#endif
}

static bool t_wifi(const struct shell *sh)
{
#ifdef CONFIG_ZAPP_WIFI
	char addr[16];

	if (zapp_wifi_up(addr, sizeof(addr))) {
		shell_print(sh, "test wifi pass up at %s", addr);
		return true;
	}
	shell_print(sh, "test wifi fail not up (zapp wifi status)");
	return false;
#else
	shell_print(sh, "test wifi fail no WiFi in this build");
	return false;
#endif
}

static const struct {
	const char *name;
	bool (*run)(const struct shell *sh);
	bool here;
} tests[] = {
	{"flash", t_flash, true},
	{"entropy", t_entropy, true},
	{"temp", t_temp, HAS_TEMP},
	{"wifi", t_wifi, IS_ENABLED(CONFIG_ZAPP_WIFI)},
};

static int cmd_test(const struct shell *sh, size_t argc, char **argv)
{
	bool all = strcmp(argv[1], "all") == 0, list = strcmp(argv[1], "list") == 0;
	int failed = 0, ran = 0;

	for (size_t i = 0; i < ARRAY_SIZE(tests); i++) {
		if (list) {
			if (tests[i].here) {
				shell_print(sh, "test %s", tests[i].name);
			}
		} else if ((all && tests[i].here) || strcmp(argv[1], tests[i].name) == 0) {
			ran++;
			failed += !tests[i].run(sh);
		}
	}
	if (list) {
		return 0;
	}
	if (!ran) {
		shell_print(sh, "test %s fail no such test (zapp test list)", argv[1]);
		return -EINVAL;
	}
	if (all) {
		shell_print(sh, "test all %s %d of %d", failed ? "fail" : "pass", ran - failed, ran);
	}
	return failed ? -EIO : 0;
}

SHELL_SUBCMD_ADD((zapp), test, NULL, "<name>|all|list: check the board's hardware", cmd_test, 2, 0);
