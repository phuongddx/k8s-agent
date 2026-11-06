#!/usr/bin/env bash
# Installs a checksum-pinned kustomize (linux/amd64). Version and SHA256 copied from
# phuongddx/vps192-k8s-config scripts/install-tools.sh, so the agent renders exactly like CI there.
set -euo pipefail

kustomize_url="https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv5.7.1/kustomize_v5.7.1_linux_amd64.tar.gz"
kustomize_sha256="ea375e7372f9aa029129d4b2d16c66b7750b7f1213c4f66f910d981c895818d8"

dest="${1:-/usr/local/bin}"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$dest"

curl --fail --silent --show-error --location --retry 5 --retry-delay 5 --retry-all-errors \
  --output "$work/kustomize.tar.gz" "$kustomize_url"
printf '%s  %s\n' "$kustomize_sha256" "$work/kustomize.tar.gz" | sha256sum --check
tar -xzf "$work/kustomize.tar.gz" -C "$work" kustomize
install -m 0755 "$work/kustomize" "$dest/kustomize"
"$dest/kustomize" version
