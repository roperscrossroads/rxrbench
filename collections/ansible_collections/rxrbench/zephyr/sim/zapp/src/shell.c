/*
 * The `zapp` command: each feature adds its subcommand from its own file (SHELL_SUBCMD_ADD), so
 * a build without a feature has no command for it.
 */
#include <zephyr/shell/shell.h>

SHELL_SUBCMD_SET_CREATE(zapp_cmds, (zapp));
SHELL_CMD_REGISTER(zapp, &zapp_cmds, "zapp's own commands", NULL);
