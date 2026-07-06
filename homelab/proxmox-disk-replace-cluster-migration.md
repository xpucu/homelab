# Proxmox Disk Replacement & Cluster Join Runbook — `proxmox.local`

> Real-world runbook from an actual disk swap + cluster join, with the gotchas we hit along the way left in. IPs replaced with placeholders (`<this-node-ip>`, `<synology-nas-ip>`, `<unas-ip>`, etc.) — hostnames, container/VM names, and internal paths are left as-is since they're just examples, not sensitive info.

Backing up the containers + qBittorrent VM on **proxmox.local** (currently standalone), replacing its disk, doing a clean Proxmox VE install (same hostname), joining it into a cluster with your other node, then restoring everything.

## Your Inventory (captured before wipe)

| Type | Original VMID | Name | Status | Restored As |
|---|---|---|---|---|
| LXC | 100 | plexy | running | **200** (conflicts with `pve`'s `grocy`) |
| LXC | 104 | unbound | running | **201** (conflicts with `pve`'s `bazarr`) |
| VM | 103 | qbit | running (2048MB, 35GB disk) | **202** (conflicts with `pve`'s `sonarr`) |

`pve` already runs a full arr-stack (100–111: grocy, prowlarr, radarr, sonarr, bazarr, lidarr, readarr, recyclarr, sabnzbd, notifiarr, adsb, librenms), which is why all three original IDs collide once clustered — VMIDs must be unique across the whole cluster, not just per-node.

**Storage:**
| Name | Type | Total | Used | Free | %Used |
|---|---|---|---|---|---|
| local | dir | ~38.7 GB | ~14.2 GB | ~22.5 GB | 36.78% |
| local-lvm | lvmthin | ~54.2 GB | ~49.2 GB | ~5.0 GB | **90.77%** |

⚠️ `local-lvm` is almost full — **do not** `vzdump` to local storage. Dump straight to the other node or NFS/CIFS (Phase 1, Option A/B below). There isn't enough free local space to hold backups of all three guests safely.

**Network (`/etc/network/interfaces`):**
```
auto lo
iface lo inet loopback
iface nic0 inet manual
auto vmbr0
iface vmbr0 inet dhcp
        bridge-ports nic0
        bridge-stp off
        bridge-fd 0
iface nic1 inet manual
source /etc/network/interfaces.d/*
```
`vmbr0` bridges `nic0`, gets its address via DHCP. `nic1` is defined but unused (manual, no bridge). Recreate this exact config on the fresh install — note whether your DHCP server has a reservation for this MAC so it comes back on the same IP (important, see NFS note below).

**Current cluster status:** standalone — `/etc/pve/corosync.conf` does not exist. This is a first-time cluster join, not a rejoin.

**NFS mounts in use (reattach after restore):**
| Mount point | Source | Notes |
|---|---|---|
| `/mnt/movies` | `<synology-nas-ip>:/volume1/Movies` (NFSv4.1) | Synology, clientaddr <this-node-ip> |
| `/mnt/tv` | `<synology-nas-ip>:/volume2/Media/TV` (NFSv4.1) | Synology, clientaddr <this-node-ip> |
| `/mnt/unas` | `<unas-ip>:/var/nfs/shared/Shared_Drive` (NFSv3, `_netdev`) | Generic NFS server |

Confirmed from `pct config`: these are mounted at the **host** level and bind-mounted straight into `plexy` (100) as host-directory mounts:

```
mp0: /mnt/movies,mp=/mnt/movies
mp1: /mnt/tv,mp=/mnt/tv
mp2: /mnt/unas,mp=/mnt/unas
```

`unbound` (104) has no mount points — it's a plain container, nothing extra to reattach.

Because `mp0`/`mp1`/`mp2` are stored inside `plexy`'s own config file (`/etc/pve/lxc/100.conf`), they'll come back automatically when you `pct restore` — you don't need to re-add them by hand. But the **host-level NFS mounts have to exist and be mounted before you start `plexy`**, or the container will start with empty bind-mount directories (or fail to start, depending on `mp` strictness). So `/etc/fstab` (or equivalent mount step) must be recreated on the host first, then the container started.

`plexy` also has GPU passthrough for hardware transcoding:
```
lxc.cgroup2.devices.allow: c 226:0 rwm
lxc.cgroup2.devices.allow: c 226:128 rwm
lxc.mount.entry: /dev/dri dev/dri none bind,optional,create=dir
```
These lines are also stored in the container config and will be restored automatically. However, `/dev/dri` has to actually exist on the **host** after reinstall — that means the same GPU driver (Intel/AMD iGPU, whichever this is) needs to be present on the fresh Proxmox install. A stock Proxmox VE install doesn't include VA-API/iGPU driver packages by default; you'll likely need to reinstall those on the host before `plexy` can use hardware transcoding again (e.g. `intel-media-va-driver`, `vainfo` for Intel Quick Sync).

```bash
# Run this before wiping, as a final sanity check
pct config 100
pct config 104
cat /etc/fstab
```

---

## On Reusing the Hostname `proxmox.local`

Since the new install will keep the same hostname **and** (assuming a DHCP reservation) the same IP, most things — NFS mounts, DNS, any static references in Home Assistant/LibreNMS — should just work without changes. Two things to handle explicitly:

1. **SSH host key mismatch.** A fresh install generates new SSH host keys. Any machine that has previously SSH'd to `proxmox.local` (including the other Proxmox node, which you'll SSH from during `pvecm add`) will have the *old* key cached and will refuse to connect. Clear it first:
   ```bash
   # run on the OTHER node, before joining
   ssh-keygen -R proxmox.local
   ssh-keygen -R <this-node-ip>   # or whatever its IP turns out to be
   ```
2. **DHCP reservation.** Confirm your router/DHCP server has a reservation for this host's MAC address so it comes back on `<this-node-ip>` after reinstall — this keeps the NFS `clientaddr` and any hardcoded references (Bouncie, HA, LibreNMS monitoring) valid without edits.

---

## Phase 0: Final Pre-Wipe Capture

```bash
# Confirm nothing changed since inventory above
pct list
qm list
pvesm status

# Capture exact container configs (passthrough, mounts, network)
mkdir -p /mnt/unas/NAS_Backup/proxmox
pct config 100 > /mnt/unas/NAS_Backup/proxmox/plexy-config.txt
pct config 104 > /mnt/unas/NAS_Backup/proxmox/unbound-config.txt
qm config 103 > /mnt/unas/NAS_Backup/proxmox/qbit-config.txt
```

---

## Phase 1: Back Up Every Container and VM

`local-lvm` is at 90.77% — back up **directly to remote storage**, don't stage locally first.

### Option A — Add the other node (or a share) as backup storage via GUI
Datacenter → Storage → Add → Directory/NFS/CIFS → content type "VZDump backup file". Point it at the other node's `/var/lib/vz/dump` (over NFS/SSHFS) or a NAS share you already have mounted (`/mnt/unas` is a candidate, given it's already connected).

### Option B — vzdump straight to an existing mount

```bash
# dedicated backup directory on the NAS
mkdir -p /mnt/unas/NAS_Backup/proxmox

vzdump 100 --mode snapshot --compress zstd --dumpdir /mnt/unas/NAS_Backup/proxmox
vzdump 104 --mode snapshot --compress zstd --dumpdir /mnt/unas/NAS_Backup/proxmox
vzdump 103 --mode snapshot --compress zstd --dumpdir /mnt/unas/NAS_Backup/proxmox

# or all three at once
vzdump 100 104 103 --mode snapshot --compress zstd --dumpdir /mnt/unas/NAS_Backup/proxmox
```

Verify each file landed and isn't 0 bytes:
```bash
ls -lh /mnt/unas/NAS_Backup/proxmox
```

If you'd rather not depend on `/mnt/unas` staying mounted through the wipe/reinstall, also copy the finished backup files to the other Proxmox node:
```bash
scp /mnt/unas/NAS_Backup/proxmox/*.zst root@<other-node-ip>:/var/lib/vz/dump/
```

---

## Phase 2: Back Up Host Configuration

```bash
tar czf /mnt/unas/NAS_Backup/proxmox/host-config-backup.tar.gz \
  /etc/network/interfaces \
  /etc/pve/storage.cfg \
  /etc/hosts \
  /etc/resolv.conf \
  /etc/hostname \
  /etc/fstab \
  /mnt/unas/NAS_Backup/proxmox/plexy-config.txt \
  /mnt/unas/NAS_Backup/proxmox/unbound-config.txt \
  /mnt/unas/NAS_Backup/proxmox/qbit-config.txt \
  /etc/pve/firewall \
  /etc/pve/qemu-server \
  /etc/pve/lxc
```

---

## Phase 3: Verify Backups Before Touching the Disk

```bash
ls -lh /mnt/unas/NAS_Backup/proxmox
ssh root@<other-node-ip> "ls -lh /var/lib/vz/dump/"
```

Confirm all three guests (100, 103, 104) plus the config tarball are present and non-empty on storage that is **not** the disk you're about to replace, before proceeding.

---

## Phase 4: Replace the Disk & Reinstall Proxmox VE

1. `shutdown -h now`
2. Swap the physical disk
3. Boot the Proxmox VE installer ISO
4. Install with:
   - Hostname: `proxmox.local` (same as before)
   - Same network config as captured in Phase 0 (vmbr0 on nic0, DHCP) — confirm the DHCP reservation is in place first so the IP comes back the same
5. After first boot:
   ```bash
   apt update && apt full-upgrade -y
   ```

   If this fails with `401 Unauthorized` on `enterprise.proxmox.com` (expected on a fresh install without a subscription key), disable the enterprise repos and switch to the free no-subscription repo:

   ```bash
   # These repos have no "Enabled:" line by default, so they're enabled unless you add one —
   # sed find/replace won't work here since there's nothing to replace, just append the line:
   echo "Enabled: false" >> /etc/apt/sources.list.d/pve-enterprise.sources
   echo "Enabled: false" >> /etc/apt/sources.list.d/ceph.sources

   # Add the no-subscription repo
   cat > /etc/apt/sources.list.d/pve-no-subscription.sources << 'REPOEOF'
   Types: deb
   URIs: http://download.proxmox.com/debian/pve
   Suites: trixie
   Components: pve-no-subscription
   Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
   REPOEOF

   apt update && apt full-upgrade -y
   ```
6. Confirm version compatibility with the other node:
   ```bash
   pveversion
   ```

---

## Phase 5: Join the Cluster

**On the other node**, clear the stale SSH host key for the reinstalled machine first:
```bash
ssh-keygen -R proxmox.local
```

Check if the other node is already clustered:
```bash
pvecm status
```
- Not clustered → create one: `pvecm create <cluster-name>`
- Already clustered → skip

**On the reinstalled `proxmox.local`**, join:
```bash
pvecm add <ip-of-other-node>
```

Verify from either side:
```bash
pvecm status
pvecm nodes
```

⚠️ **Confirmed conflict.** `pve` already has VMID 100 (`grocy`), 103 (`sonarr`), and 104 (`bazarr`) — all three collide with `plexy`/`qbit`/`unbound`. Since VMIDs are unique cluster-wide, restore using new IDs instead:

| Original | New VMID | Guest |
|---|---|---|
| 100 | **200** | plexy |
| 104 | **201** | unbound |
| 103 | **202** | qbit |

`proxmox` itself is currently empty (`pct list`/`qm list` returned nothing after the reinstall, as expected), so there's no conflict on that side — just the collision with `pve`'s existing guests.

---

## Phase 6: Restore Containers and the VM

Pull backups back to the rejoined node (or restore straight from `/mnt/unas` / the other node's `dump` folder, since it's reachable). Using the new VMIDs (200/201/202) to avoid the conflicts with `pve`'s existing guests:

```bash
pct restore 200 /mnt/unas/NAS_Backup/proxmox/vzdump-lxc-100-*.tar.zst --storage local-lvm
pct restore 201 /mnt/unas/NAS_Backup/proxmox/vzdump-lxc-104-*.tar.zst --storage local-lvm
qmrestore /mnt/unas/NAS_Backup/proxmox/vzdump-qemu-103-*.vma.zst 202 --storage local-lvm
```

⚠️ **`qbit` (202) may fail to start** with `volume 'local:iso/...' does not exist` — this happens if the VM's config still references an installer ISO mounted as a virtual CD-ROM (leftover from initial VM creation; ISOs aren't included in `vzdump` backups). Check and remove it:

```bash
qm config 202
# look for a line like: ide2: local:iso/<something>.iso,media=cdrom
qm set 202 --ide2 none,media=cdrom   # adjust the slot name to match what you see
qm start 202
```

`scsi0` (the actual disk) stays first in the boot order, so removing the CD-ROM reference doesn't affect anything else.

**Before starting `plexy`**, the host-level NFS mounts must exist — `mp0`/`mp1`/`mp2` and the GPU passthrough lines are already baked into `plexy`'s restored config, so you don't need to re-add them manually. You just need the host side ready first.

Using the same fstab lines as `pve` (kept consistent across both nodes intentionally):

```bash
mkdir -p /mnt/movies /mnt/tv /mnt/unas

cat >> /etc/fstab << 'FSTABEOF'
<unas-ip>:/var/nfs/shared/Shared_Drive  /mnt/unas  nfs  rw,_netdev,nofail,soft  0  0
<synology-nas-ip>:/volume1/Movies  /mnt/movies  nfs  rw,_netdev,nofail,soft  0  0
<synology-nas-ip>:/volume2/Media/TV      /mnt/tv      nfs  rw,_netdev,nofail,soft  0  0
FSTABEOF

mount -a
mount | grep /mnt

# Confirm /dev/dri exists on the host before starting plexy (GPU passthrough)
ls -l /dev/dri
# If missing, install the iGPU driver package first, e.g. for Intel Quick Sync:
apt install -y intel-media-va-driver vainfo
vainfo   # sanity check that hardware transcoding is available
```

---

## Phase 7: Post-Restore Verification

```bash
pct start 200
pct start 201
qm start 202

pct list
qm list

pct exec 200 -- systemctl status plexmediaserver   # adjust to actual service name
pct exec 201 -- systemctl status unbound
```

Note: `pct start 201` may print `WARN: Systemd 252 detected. You may need to enable nesting.` — informational only, the container still starts fine. Silence it if you want:
```bash
pct set 201 --features nesting=1
```

If `unbound` shows `Active: inactive (dead)` inside the container, that's a separate issue from the container itself running — the service needs to be started/debugged independently:
```bash
pct exec 201 -- systemctl start unbound
pct exec 201 -- journalctl -u unbound -n 50 --no-pager   # if it won't start
```

Checklist:
- [ ] `plexy` (200) boots, Plex reachable, `/mnt/movies`, `/mnt/tv`, `/mnt/unas` visible inside the container
- [ ] Hardware transcoding works in Plex (Settings → Transcoder) — confirms `/dev/dri` passthrough survived the restore
- [ ] `unbound` (201) boots and resolving correctly
- [ ] `qbit` (202) boots, qBittorrent reachable, torrents/paths intact
- [ ] Any references to the old VMIDs (100/103/104) — bookmarks, monitoring dashboards, scripts — updated to the new IDs (200/201/202)
- [ ] `pvecm status` shows both nodes healthy
- [ ] DHCP reservation held and `proxmox.local` resolves to the expected IP
- [ ] Keep the `/mnt/unas/NAS_Backup/proxmox` files for a week or two before deleting

---

## Quick Reference

| Task | Command |
|---|---|
| List containers | `pct list` |
| List VMs | `qm list` |
| Backup one guest | `vzdump <ID> --mode snapshot --compress zstd --dumpdir /mnt/unas/NAS_Backup/proxmox` |
| Capture container config | `pct config <CTID>` / `qm config <VMID>` |
| Clear stale SSH key | `ssh-keygen -R proxmox.local` |
| Check cluster status | `pvecm status` |
| Create cluster | `pvecm create <name>` |
| Join cluster | `pvecm add <existing-node-ip>` |
| Restore container | `pct restore <CTID> <file> --storage local-lvm` |
| Restore VM | `qmrestore <file> <VMID> --storage local-lvm` |
