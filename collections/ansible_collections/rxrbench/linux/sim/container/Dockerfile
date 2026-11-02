# A simulated Linux device for rxrbench's tests: a BusyBox userland and an ssh server, and
# nothing else. No Python, no init system, no package the tool could lean on by accident:
# what works against this works against a small embedded image.
FROM alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc
RUN apk add --no-cache openssh-server \
    && rm -f /etc/ssh/ssh_host_* \
    && mkdir -p /root/.ssh /run/sshd && chmod 700 /root/.ssh
COPY entrypoint.sh reboot.sh /usr/local/sbin/
RUN chmod 755 /usr/local/sbin/entrypoint.sh /usr/local/sbin/reboot.sh \
    && ln -sf /usr/local/sbin/reboot.sh /sbin/reboot
EXPOSE 22
ENTRYPOINT ["/usr/local/sbin/entrypoint.sh"]
