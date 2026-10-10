# OSWorld evaluator runtime

The authoritative dependency lock is the pinned upstream `uv.lock` with SHA-256
`123c8837f7901b6b1aa34836f49e00fcca0f7e77de6f4839bd9cd6fa2ea4f44e`.
The OSWorld source commit is `091f5ef1d5544bc74953c77875d5feb5bed30108`.

The VM and provider are evaluator assets, never Builder package content. `vm_manifest.json`
is generated only after the commit-qualified VM download passes exact-size, SHA-256, ZIP,
qcow2, boot, screenshot, action, and cleanup checks. Provider execution is pinned to:

```text
happysixd/osworld-docker@sha256:0e6497a9295647cf05bf2b2af522fdd79bdeba2737595259cab310a3bcf6baa9
```

The runtime needs evaluator-owned Docker access and host networking. The provider's default UEFI
path stalls in the guest bootloader on this host. The frozen provider instead uses verified KVM
direct-kernel boot with the kernel and initrd extracted from the same checksum-locked qcow2. A
fresh probe produced a visible 1920x1080 desktop; the TCG fallback booted Ubuntu but its virtio
scanout remained black. The provider receives `/dev/kvm`, `/dev/net/tun`, and `/dev/vhost-net`;
Builder and Candidate jobs receive neither Docker nor VM devices.
