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
cmdline="root=UUID=12ca4052-2e4c-46be-a446-02952bb56f0f ro console=ttyS0,115200n8 systemd.log_level=info"
cmdline+=" systemd.mask=apt-daily.service systemd.mask=apt-daily.timer"
cmdline+=" systemd.mask=apt-daily-upgrade.service systemd.mask=apt-daily-upgrade.timer"
cmdline+=" systemd.mask=packagekit.service systemd.mask=package-data-download.service"
cmdline+=" systemd.mask=snapd.service systemd.mask=snapd.socket systemd.mask=snapd.seeded.service"
cmdline+=" systemd.mask=unattended-upgrades.service systemd.mask=fwupd.service"
cmdline+=" systemd.mask=networkd-dispatcher.service systemd.mask=ModemManager.service"
cmdline+=" systemd.mask=systemd-update-done.service"
cmdline+=" systemd.mask=boot-efi.mount"
cmdline+=" systemd.mask=systemd-timesyncd.service"
cmdline+=" systemd.mask=grub-common.service systemd.mask=grub-initrd-fallback.service"
cmdline+=" systemd.mask=logrotate.service systemd.mask=snapd.apparmor.service"
cmdline+=" systemd.mask=fwupd-refresh.timer systemd.mask=apt-news.service"

exec nice -n -20 qemu-system-x86_64 "${base_args[@]}" \
  -kernel /boot-assets/vmlinuz \
  -initrd /boot-assets/initrd \
  -append "$cmdline"
