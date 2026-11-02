# The image `just ci` runs the GitHub Actions jobs in, with act: the act runner image (pinned by
# digest), with what a GitHub-hosted runner has and it lacks: the jobs run as a non-root user,
# `runner`, with passwordless sudo. Its uid is the caller's, so the worktree act binds in is
# writable by it; the host's Docker socket group is added when the container starts.
FROM catthehacker/ubuntu:act-24.04@sha256:c58e2b364da03b0c804c7d660f2ecbedf2f221a382b9baa0b344b0144780ff43
ARG UID=1000
RUN if getent passwd "$UID" >/dev/null; then userdel -r "$(getent passwd "$UID" | cut -d: -f1)" || true; fi \
    && useradd -m -u "$UID" -s /bin/bash runner \
    && echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner && chmod 0440 /etc/sudoers.d/runner \
    && chown -R runner /opt/hostedtoolcache
ENV HOME=/home/runner USER=runner LOGNAME=runner
USER runner
