/* What zapp's feature files offer one another. */
#ifndef ZAPP_H
#define ZAPP_H

#include <stdbool.h>
#include <stddef.h>

/* wifi.c: join the network the settings name (-ENOENT without one); whether it is up, and its address */
int zapp_wifi_connect(void);
bool zapp_wifi_up(char *addr, size_t len);
void zapp_wifi_start(void);

#endif
