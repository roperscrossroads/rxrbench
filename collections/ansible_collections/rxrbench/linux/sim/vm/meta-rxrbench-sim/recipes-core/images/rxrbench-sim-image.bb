# rxrbench's simulated A/B Linux device: meta-slotguard's qemuarm harness image, reachable
# over ssh. A test target, never a product: it trusts a throwaway certificate and a throwaway
# key, and its RAUC `compatible` is its own, so a bundle made from it is refused by any real
# device and a real device's bundle is refused by it.
require recipes-core/images/slotguard-image-qemu.bb

SUMMARY = "rxrbench's simulated A/B Linux device (qemuarm, RAUC, slotguard, ssh)"

# An ssh server with scp and sftp (a bundle is copied to the device), and what makes the
# device reachable and its host key last: rxrbench-sim-access.
IMAGE_INSTALL:append = " \
    openssh-sshd \
    openssh-sftp-server \
    openssh-scp \
    openssh-keygen \
    rxrbench-sim-access \
"
