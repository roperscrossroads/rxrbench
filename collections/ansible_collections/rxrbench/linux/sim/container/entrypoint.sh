#!/bin/sh
# What a device's first boot does: make a host key if there is none, then serve ssh. The
# device says what it is in /etc/os-release, from its environment; its LED is a directory
# with a `trigger` file, as under /sys/class/leds.
set -eu
cat > /etc/os-release <<OSR
ID=${SIM_OS_ID:-rxrbench-sim}
NAME="rxrbench simulated device"
VERSION_ID=${SIM_OS_VERSION:-1}
BUILD_ID=${SIM_OS_BUILD:-0}
PRETTY_NAME="rxrbench simulated device ${SIM_OS_VERSION:-1}"
OSR
hostname "${SIM_NAME:-sim}" 2>/dev/null || true
mkdir -p /run/leds/act
[ -f /run/leds/act/trigger ] || echo "[none] timer heartbeat" > /run/leds/act/trigger
[ -f /etc/ssh/ssh_host_ed25519_key ] || ssh-keygen -q -t ed25519 -N "" -f /etc/ssh/ssh_host_ed25519_key
if [ -n "${SIM_AUTHORIZED_KEY:-}" ]; then
    echo "$SIM_AUTHORIZED_KEY" > /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
fi
# root with a key only; the account has no password to guess
sed -i 's/^root:!/root:*/' /etc/shadow
exec /usr/sbin/sshd -D -e \
    -o PermitRootLogin=prohibit-password -o PasswordAuthentication=no \
    -o HostKey=/etc/ssh/ssh_host_ed25519_key
