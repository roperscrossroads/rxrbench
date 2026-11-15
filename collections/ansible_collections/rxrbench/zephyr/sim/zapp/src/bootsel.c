/*
 * `rp2 bootsel` (and `zapp bootsel`): into the RP2040's or RP2350's ROM bootloader, its drive and
 * picotool's interface, so a bench writes the next image with no one pressing a button. The
 * picoboot flasher asks for it by the first name (a layout's bench_picoboot_enter).
 */
#include <zephyr/kernel.h>
#include <zephyr/shell/shell.h>
#include <pico/bootrom.h>

static int cmd_bootsel(const struct shell *sh, size_t argc, char **argv)
{
	shell_print(sh, "rebooting into the ROM bootloader");
	k_msleep(100);   /* the line reaches the host before USB goes */
	reset_usb_boot(0, 0);
	return 0;
}

SHELL_STATIC_SUBCMD_SET_CREATE(rp2_cmds,
	SHELL_CMD(bootsel, NULL, "reboot into the ROM bootloader (BOOTSEL)", cmd_bootsel),
	SHELL_SUBCMD_SET_END);
SHELL_CMD_REGISTER(rp2, &rp2_cmds, "RP2040 / RP2350", NULL);
SHELL_SUBCMD_ADD((zapp), bootsel, NULL, "reboot into the ROM bootloader (BOOTSEL)", cmd_bootsel, 1, 0);
