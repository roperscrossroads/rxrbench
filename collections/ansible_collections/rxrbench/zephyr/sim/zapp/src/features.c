/*
 * `zapp version` and `zapp features`: which build this is and what it provides, as the build
 * listed it (CMakeLists.txt writes the same list to zapp-features.txt for the catalog).
 *
 *     version zapp 3.0
 *     features bootsel digest reboot selftest state
 */
#include <zephyr/shell/shell.h>

static int cmd_version(const struct shell *sh, size_t argc, char **argv)
{
	shell_print(sh, "version zapp %s", CONFIG_ZAPP_VERSION);
	return 0;
}

static int cmd_features(const struct shell *sh, size_t argc, char **argv)
{
	shell_print(sh, "features %s", ZAPP_FEATURES);
	return 0;
}

SHELL_SUBCMD_ADD((zapp), version, NULL, "this build's version", cmd_version, 1, 0);
SHELL_SUBCMD_ADD((zapp), features, NULL, "what this build provides", cmd_features, 1, 0);
