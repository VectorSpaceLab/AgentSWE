#!/usr/bin/env bash
set -Eeuo pipefail

APP="QEMU"
SUPPORT="https://github.com/qemus/qemu-docker"

cd /run
. reset.sh
. install.sh
. disk.sh
. display.sh
. network.sh
. boot.sh
. proc.sh
. config.sh

read -r -a base_args <<< "$ARGS"
exec nice -n -20 qemu-system-x86_64 "${base_args[@]}"
