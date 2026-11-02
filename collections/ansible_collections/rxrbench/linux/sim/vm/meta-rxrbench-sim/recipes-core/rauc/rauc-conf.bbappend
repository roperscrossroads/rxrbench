# The certificate this image verifies bundles against, and its own `compatible`.
# rxrbench-sim.cert.pem is written by make-keys.sh and is not tracked: a throwaway
# certificate of this checkout, whose private key stays on the workstation that signs.
FILESEXTRAPATHS:prepend := "${THISDIR}/files:"
SRC_URI += "file://rxrbench-sim.cert.pem"
SLOTGUARD_KEYRING = "rxrbench-sim.cert.pem"

do_install:append() {
    sed -i "s|^compatible=.*|compatible=rxrbench-sim-qemuarm|" ${D}${nonarch_libdir}/rauc/system.conf
    grep -q "^compatible=rxrbench-sim-qemuarm$" ${D}${nonarch_libdir}/rauc/system.conf \
        || bbfatal "rxrbench-sim: the RAUC compatible was not set"
}
