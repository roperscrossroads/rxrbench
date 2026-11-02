SUMMARY = "What makes rxrbench's simulated device reachable: a key, the network, a lasting host key"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

# authorized_keys is written by make-keys.sh and is not tracked: the key is this checkout's.
SRC_URI = " \
    file://authorized_keys \
    file://20-wired.network \
    file://ssh-host-keys-on-data.service \
"
S = "${UNPACKDIR}"

inherit systemd
SYSTEMD_SERVICE:${PN} = "ssh-host-keys-on-data.service"
SYSTEMD_AUTO_ENABLE = "enable"

python () {
    import os
    for entry in (d.getVar("FILESPATH") or "").split(":"):
        p = os.path.join(entry, "authorized_keys")
        if os.path.exists(p):
            if not open(p).read().strip():
                bb.fatal("rxrbench-sim-access: authorized_keys is empty: run make-keys.sh")
            return
    bb.fatal("rxrbench-sim-access: no authorized_keys beside this recipe: run make-keys.sh "
             "before the build (the simulated device would boot with no way in)")
}

do_install() {
    install -d -m 0700 ${D}${ROOT_HOME}/.ssh
    install -m 0600 ${S}/authorized_keys ${D}${ROOT_HOME}/.ssh/authorized_keys
    install -d ${D}${systemd_unitdir}/network
    install -m 0644 ${S}/20-wired.network ${D}${systemd_unitdir}/network/20-wired.network
    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${S}/ssh-host-keys-on-data.service ${D}${systemd_system_unitdir}/
}

FILES:${PN} += " \
    ${ROOT_HOME}/.ssh \
    ${systemd_unitdir}/network/20-wired.network \
"
