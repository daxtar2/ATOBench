#!/usr/bin/env bash
# Bootstrap a fresh Cursor cloud-agent VM (or any bare Ubuntu 24.04 box) for
# running the ATOBench Harbor tasks: Docker (vfs storage, no containerd
# snapshotter — unprivileged kernels lack overlayfs), the legacy-iptables
# FORWARD fix, and the harbor CLI.
#
# Usage: sudo bash harbor/scripts/dev_vm_bootstrap.sh   (then re-login or
#         run: newgrp docker)
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 python3-pip >/dev/null

# vfs works without overlayfs privileges; disable the containerd snapshotter
# so the storage-driver setting is honored.
mkdir -p /etc/docker
printf '{"storage-driver":"vfs","features":{"containerd-snapshotter":false}}' \
    > /etc/docker/daemon.json

# Stale iptables-legacy rules (DROP policy on FORWARD) shadow docker's nft
# rules in nested/unprivileged environments — allow forwarding there too.
iptables-legacy -P FORWARD ACCEPT || true

# Start dockerd in the background if it is not already running.
if ! docker info >/dev/null 2>&1; then
    nohup dockerd >/var/log/dockerd-bootstrap.log 2>&1 &
    for _ in $(seq 1 30); do
        docker info >/dev/null 2>&1 && break
        sleep 1
    done
fi

# Passwordless docker for the calling user (dev VMs only).
if [ -n "${SUDO_USER:-}" ]; then
    usermod -aG docker "$SUDO_USER" || true
fi
chmod 666 /var/run/docker.sock || true

# harbor CLI for the calling user.
sudo -u "${SUDO_USER:-ubuntu}" pip install --quiet --user harbor 2>/dev/null || \
    pip install --quiet harbor

docker run --rm hello-world >/dev/null && echo "docker: OK"
sudo -u "${SUDO_USER:-ubuntu}" "$HOME/.local/bin/harbor" --version 2>/dev/null || \
    echo "harbor: installed (ensure ~/.local/bin is on PATH)"
echo "bootstrap complete"
