/*
 * WiFi from settings. wifi/ssid names the network and wifi/psk its key (a secret the layout
 * declares; no key means an open network). At boot, after the settings load, and on
 * `zapp wifi connect`, zapp joins it and takes an address by DHCP.
 *
 *     zapp wifi status   ->  wifi up <ssid> <address> | wifi joining <ssid> | wifi down <ssid> <why> | wifi none
 */
#include <errno.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/dhcpv4.h>
#include <zephyr/net/net_mgmt.h>
#include <zephyr/net/wifi_mgmt.h>
#include <zephyr/settings/settings.h>
#include <zephyr/shell/shell.h>

#include "zapp.h"

static char ssid[33];
static char psk[65];
static enum { NONE, JOINING, JOINED, DOWN } link;
static int last_error;
static struct net_mgmt_event_callback wifi_cb, ip_cb;

static int wifi_set(const char *name, size_t len, settings_read_cb read_cb, void *cb_arg)
{
	const char *next;
	char *dst = NULL;
	size_t cap = 0;

	if (settings_name_steq(name, "ssid", &next) && !next) {
		dst = ssid, cap = sizeof(ssid);
	} else if (settings_name_steq(name, "psk", &next) && !next) {
		dst = psk, cap = sizeof(psk);
	} else {
		return 0;
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

SETTINGS_STATIC_HANDLER_DEFINE(zapp_wifi, "wifi", NULL, wifi_set, NULL, NULL);

static void on_wifi(struct net_mgmt_event_callback *cb, uint64_t event, struct net_if *iface)
{
	const struct wifi_status *st = (const struct wifi_status *)cb->info;

	if (event == NET_EVENT_WIFI_CONNECT_RESULT) {
		if (st && st->status) {
			link = DOWN;
			last_error = st->status;
			return;
		}
		link = JOINING;   /* joined; up once DHCP gives an address */
		net_dhcpv4_start(iface);
	} else if (event == NET_EVENT_WIFI_DISCONNECT_RESULT) {
		link = DOWN;
		last_error = st ? st->status : 0;
	}
}

static void on_ip(struct net_mgmt_event_callback *cb, uint64_t event, struct net_if *iface)
{
	if (event == NET_EVENT_IPV4_ADDR_ADD) {
		link = JOINED;
	}
}

int zapp_wifi_connect(void)
{
	struct net_if *iface = net_if_get_wifi_sta();
	struct wifi_connect_req_params p = {0};

	if (!ssid[0]) {
		link = NONE;
		return -ENOENT;
	}
	if (!iface) {
		link = DOWN;
		last_error = -ENODEV;
		return -ENODEV;
	}
	p.ssid = (const uint8_t *)ssid;
	p.ssid_length = strlen(ssid);
	p.psk = (const uint8_t *)psk;
	p.psk_length = strlen(psk);
	p.security = psk[0] ? WIFI_SECURITY_TYPE_PSK : WIFI_SECURITY_TYPE_NONE;
	p.channel = WIFI_CHANNEL_ANY;
	p.band = WIFI_FREQ_BAND_2_4_GHZ;
	p.mfp = WIFI_MFP_OPTIONAL;
	p.timeout = SYS_FOREVER_MS;
	link = JOINING;
	int rc = net_mgmt(NET_REQUEST_WIFI_CONNECT, iface, &p, sizeof(p));

	if (rc) {
		link = DOWN;
		last_error = rc;
	}
	return rc;
}

bool zapp_wifi_up(char *addr, size_t len)
{
	struct net_if *iface = net_if_get_wifi_sta();
	struct net_in_addr *a = iface ? net_if_ipv4_get_global_addr(iface, NET_ADDR_PREFERRED) : NULL;

	if (link != JOINED || !a) {
		return false;
	}
	net_addr_ntop(NET_AF_INET, a, addr, len);
	return true;
}

void zapp_wifi_start(void)
{
	net_mgmt_init_event_callback(&wifi_cb, on_wifi,
				     NET_EVENT_WIFI_CONNECT_RESULT | NET_EVENT_WIFI_DISCONNECT_RESULT);
	net_mgmt_add_event_callback(&wifi_cb);
	net_mgmt_init_event_callback(&ip_cb, on_ip, NET_EVENT_IPV4_ADDR_ADD);
	net_mgmt_add_event_callback(&ip_cb);
	(void)zapp_wifi_connect();
}

static int cmd_status(const struct shell *sh, size_t argc, char **argv)
{
	char addr[NET_IPV4_ADDR_LEN];

	if (zapp_wifi_up(addr, sizeof(addr))) {
		shell_print(sh, "wifi up %s %s", ssid, addr);
	} else if (link == NONE || !ssid[0]) {
		shell_print(sh, "wifi none");
	} else if (link == JOINING || link == JOINED) {
		shell_print(sh, "wifi joining %s", ssid);
	} else {
		shell_print(sh, "wifi down %s %d", ssid, last_error);
	}
	return 0;
}

static int cmd_connect(const struct shell *sh, size_t argc, char **argv)
{
	settings_load_subtree("wifi");   /* settings written since boot */
	int rc = zapp_wifi_connect();

	if (rc == -ENOENT) {
		shell_print(sh, "wifi none");
		return 0;
	}
	shell_print(sh, rc ? "wifi down %s %d" : "wifi joining %s", ssid, rc);
	return rc;
}

SHELL_STATIC_SUBCMD_SET_CREATE(wifi_cmds,
	SHELL_CMD(status, NULL, "where the link is", cmd_status),
	SHELL_CMD(connect, NULL, "join the network the settings name", cmd_connect),
	SHELL_SUBCMD_SET_END);
SHELL_SUBCMD_ADD((zapp), wifi, &wifi_cmds, "WiFi from the settings wifi/ssid, wifi/psk", NULL, 1, 0);
