/*
 * `zapp state`: rxrbench's state line (docs/PLUGINS.md "The state line"), one line a bench reads
 * with no code of its own:
 *
 *     ~S{"v":1,"family":"zephyr_settings","app":"zapp","fw":"3.0","target":"rpi_pico2/rp2350a/m33/w","id":"0123456789abcdef"}*1a2b
 *
 * The checksum is CRC-16/XMODEM of the JSON text as printed (crc16_itu_t with seed 0).
 */
#include <stdio.h>
#include <string.h>

#include <zephyr/drivers/hwinfo.h>
#include <zephyr/shell/shell.h>
#include <zephyr/sys/crc.h>

static int cmd_state(const struct shell *sh, size_t argc, char **argv)
{
	uint8_t raw[16];
	char id[2 * sizeof(raw) + 1] = "";
	char json[256];
	ssize_t n = hwinfo_get_device_id(raw, sizeof(raw));

	for (ssize_t i = 0; i < n; i++) {
		snprintf(&id[2 * i], 3, "%02x", raw[i]);
	}
	int len = snprintf(json, sizeof(json),
			   "{\"v\":1,\"family\":\"zephyr_settings\",\"app\":\"zapp\",\"fw\":\"%s\","
			   "\"target\":\"%s\",\"id\":\"%s\"}",
			   CONFIG_ZAPP_VERSION, CONFIG_BOARD_TARGET, id);

	if (len < 0 || len >= (int)sizeof(json)) {
		shell_error(sh, "state: too long");
		return -ENOMEM;
	}
	shell_print(sh, "~S%s*%04x", json, crc16_itu_t(0, (const uint8_t *)json, len));
	return 0;
}

SHELL_SUBCMD_ADD((zapp), state, NULL, "rxrbench's state line", cmd_state, 1, 0);
