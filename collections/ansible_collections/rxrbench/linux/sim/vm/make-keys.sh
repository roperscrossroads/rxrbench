#!/bin/sh
# The two things the simulated A/B device's image is built with and this repository does not
# track: the certificate its bundles are verified against, and the ssh key that opens it.
# Both are throwaway and this checkout's own. The private halves stay on this machine, in
# ~/.cache/rxrbench/simvm/ (mode 700): a build host is given the certificate and the public
# key only, and bundles are signed here.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
state="$HOME/.cache/rxrbench/simvm"
layer="$here/meta-rxrbench-sim/recipes-core"
mkdir -p "$state" "$layer/rauc/files" "$layer/rxrbench-sim-access/files"
chmod 700 "$state"
if [ ! -f "$state/ca.key.pem" ]; then
    openssl req -x509 -newkey rsa:4096 -nodes -days 3650 \
        -keyout "$state/ca.key.pem" -out "$state/ca.cert.pem" \
        -subj "/CN=rxrbench simulated device - TEST ONLY" 2>/dev/null
    chmod 600 "$state/ca.key.pem"
fi
[ -f "$state/id_ed25519" ] || ssh-keygen -q -t ed25519 -N "" -C rxrbench-simvm -f "$state/id_ed25519"
cp "$state/ca.cert.pem" "$layer/rauc/files/rxrbench-sim.cert.pem"
cp "$state/id_ed25519.pub" "$layer/rxrbench-sim-access/files/authorized_keys"
echo "certificate and ssh key written under $layer (private halves in $state)"
