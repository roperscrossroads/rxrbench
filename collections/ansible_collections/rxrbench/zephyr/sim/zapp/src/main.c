/*
 * zapp: a Zephyr application that keeps what a device keeps. At boot it loads its settings
 * from flash; a device that has no key yet makes one (32 random bytes, as hex) and saves it
 * under app/key, so the key is the device's own, made once and kept across reboots and
 * updates until the storage is erased. Its version goes to app/version at every boot, so a
 * bench can read which build runs. Everything else (a pre-shared key, a certificate, any
 * setting a bench declares) is written and read through the settings shell.
 */
#include <errno.h>
#include <stdio.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/random/random.h>
#include <zephyr/settings/settings.h>

#define KEY_BYTES 32

static char device_key[KEY_BYTES * 2 + 1];

static int app_set(const char *name, size_t len, settings_read_cb read_cb, void *cb_arg)
{
	const char *next;

	if (settings_name_steq(name, "key", &next) && !next) {
		if (len >= sizeof(device_key)) {
			return -EINVAL;
		}
		ssize_t n = read_cb(cb_arg, device_key, len);

		if (n < 0) {
			return (int)n;
		}
		device_key[n] = '\0';
		return 0;
	}
	/* any other setting under app/ is somebody else's to read (the shell reads it) */
	return 0;
}

SETTINGS_STATIC_HANDLER_DEFINE(zapp, "app", NULL, app_set, NULL, NULL);

int main(void)
{
	int rc = settings_subsys_init();

	if (rc) {
		printk("zapp: settings init failed (%d)\n", rc);
		return rc;
	}
	settings_load();
	if (device_key[0] == '\0') {
		uint8_t raw[KEY_BYTES];

		sys_rand_get(raw, sizeof(raw));
		for (int i = 0; i < KEY_BYTES; i++) {
			snprintf(&device_key[2 * i], 3, "%02x", raw[i]);
		}
		settings_save_one("app/key", device_key, strlen(device_key));
		printk("zapp: a key was made\n");
	}
	settings_save_one("app/version", CONFIG_ZAPP_VERSION, strlen(CONFIG_ZAPP_VERSION));
	printk("zapp %s: up\n", CONFIG_ZAPP_VERSION);
	return 0;
}
