#!/usr/bin/env bash
set -euo pipefail

if command -v convert >/dev/null 2>&1; then
  convert -version
  exit 0
fi

# Avoid the unreliable Azure mirror on GitHub-hosted Ubuntu runners.
# Keep the archive.ubuntu.com and security.ubuntu.com fallback entries.
if [[ -f /etc/apt/apt-mirrors.txt ]]; then
  sudo sed -i '/azure\.archive\.ubuntu\.com/d' /etc/apt/apt-mirrors.txt
fi

apt_options=(
  -o Acquire::Retries=3
  -o Acquire::http::Timeout=20
  -o Acquire::https::Timeout=20
  -o DPkg::Lock::Timeout=120
)

# Fail on an incomplete index refresh instead of installing from stale indexes.
sudo apt-get "${apt_options[@]}" -o APT::Update::Error-Mode=any update
sudo apt-get "${apt_options[@]}" install -y --no-install-recommends imagemagick
convert -version
