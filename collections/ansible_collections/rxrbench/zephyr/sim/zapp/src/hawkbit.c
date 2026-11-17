/*
 * `zapp hawkbit`: updates from an Eclipse hawkBit server the device polls (Kconfig ZAPP_HAWKBIT).
 *
 * One thread runs Zephyr's hawkBit client: ask the server, download what it offers into the
 * second slot, reboot into it, and tell the server afterwards. Which server, under which name
 * and with which token are settings, written like any other and read before each poll:
 *
 *     zhb/server    the server's address          zhb/port     its port (8080 when not set)
 *     zhb/id        this target's controller id   zhb/token    its security token (a secret)
 *
 * It asks only with an address, a server and a name set, and with a CONFIRMED image: an image
 * on its test boot is confirmed here after ZAPP_HAWKBIT_CONFIRM_SEC of uptime, so the server
 * hears of an update only from an image that came up and stayed up.
 *
 *     zapp hawkbit probe      ask now
 *     zapp hawkbit status     hawkbit image <v> <confirmed|unconfirmed> server <addr:port|none>
 *                             id <name|none> token <set|none> net <up|down> action <n>
 *                             probes <n> last <result>
 */
#include <errno.h>
#include <stdlib.h>
#include <string.h>

#include <zephyr/dfu/mcuboot.h>
#include <zephyr/kernel.h>
#include <zephyr/mgmt/hawkbit/config.h>
#include <zephyr/mgmt/hawkbit/hawkbit.h>
#include <zephyr/net/net_event.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_mgmt.h>
#include <zephyr/settings/settings.h>
#include <zephyr/shell/shell.h>
#include <zephyr/storage/flash_map.h>

/* Zephyr before 4.5 names it FIXED_PARTITION_ID */
#ifndef PARTITION_ID
#define PARTITION_ID(label) FIXED_PARTITION_ID(label)
#endif

static K_SEM_DEFINE(kick, 0, 1);
static struct net_mgmt_event_callback ipv4_cb;
static enum hawkbit_response last = HAWKBIT_NO_RESPONSE;
static uint32_t probes;
static char target_id[65];
static char server[65];
static char port[6];
static char token[33];   /* a hawkBit security token is 32 characters */

static int zhb_set(const char *name, size_t len, settings_read_cb read_cb, void *cb_arg)
{
	const char *next;
	char *dst;
	size_t cap;

	if (settings_name_steq(name, "id", &next) && !next) {
		dst = target_id, cap = sizeof(target_id);
	} else if (settings_name_steq(name, "server", &next) && !next) {
		dst = server, cap = sizeof(server);
	} else if (settings_name_steq(name, "port", &next) && !next) {
		dst = port, cap = sizeof(port);
	} else if (settings_name_steq(name, "token", &next) && !next) {
		dst = token, cap = sizeof(token);
	} else {
		return -ENOENT;
	}
	if (len >= cap) {
		return -EINVAL;
	}
	ssize_t n = read_cb(cb_arg, dst, len);

	if (n < 0) {
		return (int)n;
	}
	dst[n] = '\0';
	return 0;
}

SETTINGS_STATIC_HANDLER_DEFINE(zapp_hawkbit, "zhb", NULL, zhb_set, NULL, NULL);

/* The settings as they stand now, given to the client where they differ from what it holds. */
static void configure(void)
{
	const char *have;
	uint16_t want_port;

	settings_load_subtree("zhb");   /* settings written since boot */
	want_port = port[0] ? (uint16_t)strtoul(port, NULL, 10) : 8080;
	have = hawkbit_get_server_addr();
	if (server[0] && (have == NULL || strcmp(have, server) != 0)) {
		(void)hawkbit_set_server_addr(server);
	}
	if (hawkbit_get_server_port() != want_port) {
		(void)hawkbit_set_server_port(want_port);
	}
	have = hawkbit_get_ddi_security_token();
	if (token[0] && (have == NULL || strcmp(have, token) != 0)) {
		(void)hawkbit_set_ddi_security_token(token);
	}
}

static const char *result(enum hawkbit_response r)
{
	switch (r) {
	case HAWKBIT_NO_RESPONSE: return "none";
	case HAWKBIT_UPDATE_INSTALLED: return "update-installed";
	case HAWKBIT_NO_UPDATE: return "no-update";
	case HAWKBIT_NETWORKING_ERROR: return "networking-error";
	case HAWKBIT_UNCONFIRMED_IMAGE: return "image-unconfirmed";
	case HAWKBIT_PERMISSION_ERROR: return "permission-error";
	case HAWKBIT_METADATA_ERROR: return "metadata-error";
	case HAWKBIT_DOWNLOAD_ERROR: return "download-error";
	case HAWKBIT_ALLOC_ERROR: return "allocation-error";
	case HAWKBIT_NOT_INITIALIZED: return "not-initialized";
	case HAWKBIT_PROBE_IN_PROGRESS: return "probe-in-progress";
	}
	return "?";
}

static bool identity(char *id, int id_max_len)
{
	if (target_id[0] == '\0') {
		return false;
	}
	strncpy(id, target_id, id_max_len - 1);
	id[id_max_len - 1] = '\0';
	return true;
}

/* An address of its own, asked of the interface each time: an event may have come and gone
 * before this thread was listening.
 */
static bool net_up(void)
{
	struct net_if *iface = net_if_get_default();

	return iface != NULL && net_if_ipv4_get_global_addr(iface, NET_ADDR_PREFERRED) != NULL;
}

/* A new address: ask now rather than at the next poll. */
static void ipv4_event(struct net_mgmt_event_callback *cb, uint64_t event, struct net_if *iface)
{
	k_sem_give(&kick);
}

static void poll_thread(void *a, void *b, void *c)
{
	uint32_t next = 5;
	const char *addr;

	net_mgmt_init_event_callback(&ipv4_cb, ipv4_event, NET_EVENT_IPV4_ADDR_ADD);
	net_mgmt_add_event_callback(&ipv4_cb);
	(void)hawkbit_set_device_identity_cb(identity);
	if (hawkbit_init() < 0) {
		printk("zapp: hawkbit init failed\n");
		return;
	}
	for (;;) {
		(void)k_sem_take(&kick, K_SECONDS(next));
		next = 5;
		if (!boot_is_img_confirmed()) {
			if (CONFIG_ZAPP_HAWKBIT_FAULT_SEC > 0 &&
			    k_uptime_get() / 1000 >= CONFIG_ZAPP_HAWKBIT_FAULT_SEC) {
				printk("zapp: FAULT TEST: this image faults on its test boot\n");
				k_sleep(K_MSEC(200));
				k_panic();
			}
			if (k_uptime_get() / 1000 < CONFIG_ZAPP_HAWKBIT_CONFIRM_SEC) {
				continue;
			}
			printk("zapp: still up after %d s: confirming the image (%d)\n",
			       CONFIG_ZAPP_HAWKBIT_CONFIRM_SEC, boot_write_img_confirmed());
		}
		configure();
		addr = hawkbit_get_server_addr();
		if (!net_up() || addr == NULL || addr[0] == '\0' || target_id[0] == '\0') {
			next = CONFIG_ZAPP_HAWKBIT_MIN_POLL_SEC;
			continue;
		}
		last = hawkbit_probe();
		probes++;
		if (last == HAWKBIT_UPDATE_INSTALLED) {
			printk("zapp: an update is in the second slot: rebooting into it\n");
			k_sleep(K_SECONDS(1));
			hawkbit_reboot();
		}
		/* the server says how often to ask; a server that did not answer is asked again soon */
		next = (last == HAWKBIT_NO_UPDATE)
			? MAX(hawkbit_get_poll_interval(), (uint32_t)CONFIG_ZAPP_HAWKBIT_MIN_POLL_SEC)
			: CONFIG_ZAPP_HAWKBIT_MIN_POLL_SEC;
	}
}

K_THREAD_DEFINE(zapp_hawkbit_thread, 8192, poll_thread, NULL, NULL, NULL, 12, 0, 3000);

static int cmd_status(const struct shell *sh, size_t argc, char **argv)
{
	struct mcuboot_img_header h;
	const char *addr = hawkbit_get_server_addr();
	const char *tok = hawkbit_get_ddi_security_token();
	bool known = boot_read_bank_header(PARTITION_ID(slot0_partition), &h, sizeof(h)) == 0 &&
		     h.mcuboot_version == 1;

	shell_fprintf(sh, SHELL_NORMAL, "hawkbit image %d.%d.%d %s server ",
		      known ? h.h.v1.sem_ver.major : 0, known ? h.h.v1.sem_ver.minor : 0,
		      known ? h.h.v1.sem_ver.revision : 0,
		      boot_is_img_confirmed() ? "confirmed" : "unconfirmed");
	if (addr && addr[0]) {
		shell_fprintf(sh, SHELL_NORMAL, "%s:%u", addr, hawkbit_get_server_port());
	} else {
		shell_fprintf(sh, SHELL_NORMAL, "none");
	}
	shell_print(sh, " id %s token %s net %s action %d probes %u last %s",
		    target_id[0] ? target_id : "none", (tok && tok[0]) ? "set" : "none",
		    net_up() ? "up" : "down", hawkbit_get_action_id(), probes, result(last));
	return 0;
}

static int cmd_probe(const struct shell *sh, size_t argc, char **argv)
{
	k_sem_give(&kick);
	shell_print(sh, "hawkbit probe requested");
	return 0;
}

SHELL_STATIC_SUBCMD_SET_CREATE(hawkbit_cmds,
	SHELL_CMD_ARG(status, NULL, "the image, the server, the last answer", cmd_status, 1, 0),
	SHELL_CMD_ARG(probe, NULL, "ask the server now", cmd_probe, 1, 0),
	SHELL_SUBCMD_SET_END);
SHELL_SUBCMD_ADD((zapp), hawkbit, &hawkbit_cmds, "updates from a hawkBit server", NULL, 1, 0);
