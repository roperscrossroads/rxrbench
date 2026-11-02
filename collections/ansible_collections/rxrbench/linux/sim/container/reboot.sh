#!/bin/sh
# A reboot: the ssh server (this container's first process) ends, and the container is
# started again by its restart policy. Its files persist, as a device's do.
kill 1
