# Homelab

Notes, runbooks, and configs from my self-hosted homelab. Everything here runs on
**Proxmox VE** across two nodes, with media and services in a mix of LXC containers
and VMs, backed by NFS/CIFS storage on two NAS units.

These are real-world write-ups from actual builds — the gotchas and dead-ends are
left in on purpose, because they're usually the useful part.

> **No Docker anymore.** The stack used to run in Docker on a Windows host; it's now
> native on Proxmox. The old Docker-on-Windows material is kept for reference under
> [`archived/`](archived/).

---

## Architecture at a glance

### Nodes
| Host | Role | Notable guests |
| --- | --- | --- |
| `pve` | Proxmox node A | arr stack (Prowlarr, Radarr, Sonarr, Bazarr, Lidarr, Readarr, Recyclarr), SABnzbd, Notifiarr, ADS-B receiver, LibreNMS, grocy — all LXC |
| `proxmox` | Proxmox node B | Plex (LXC, iGPU passthrough), Unbound (LXC), qBittorrent (VM behind VPN) |

The two nodes are joined into a single Proxmox **cluster** (VMIDs are unique
cluster-wide — see the disk-replace/cluster runbook for how that collision was handled).

### Storage
| NAS | Protocol | Holds |
| --- | --- | --- |
| Synology (`synology`) | NFS | Movies, TV |
| UniFi UNAS Pro (`unas`) | NFS (v3 only) | Music, Books |

NFS is mounted on the Proxmox **host** and bind-mounted into unprivileged LXCs; VMs
mount NFS/CIFS directly. The guiding rule everywhere is **identical paths across every
app** (download client and *arr app see the same path), so no remote-path mappings are
needed and imports are same-filesystem moves.

### Network
- Flat `/22` LAN, a single subnet.
- The qBittorrent VM routes all torrent traffic through an L2TP/IPSec VPN to a MikroTik
  router, with an iptables kill switch so a dropped tunnel can't leak.

---

## What's in here

| File | What it covers |
| --- | --- |
| [`arr-stack-storage-rebuild.md`](arr-stack-storage-rebuild.md) | The main one — migrating the full *arr media stack (Radarr/Sonarr/Lidarr/Readarr, SABnzbd, qBittorrent, Prowlarr, Recyclarr) from Docker-on-Windows to Proxmox LXCs/VMs with NFS-backed shared storage. NAS exports, host mounts, LXC bind mounts, path-consistency design, and clean app config. |
| [`Setting_up_qbit.md`](Setting_up_qbit.md) | qBittorrent in a Proxmox VM behind an L2TP/IPSec VPN (MikroTik), with a kill switch, auto-reconnect monitor, and CIFS mount to the NAS. |
| [`adsb-proxmox-lxc-install.md`](adsb-proxmox-lxc-install.md) | ADS-B receiver (RTL-SDR Blog V4) in a Proxmox LXC with USB passthrough — dump1090-fa/PiAware, feeding FlightAware, Flightradar24, and ADSB-Exchange from one dongle. |
| [`autocaliweb.md`](autocaliweb.md) | Autocaliweb (Calibre-Web-Automated fork) ebook server on Proxmox via Helper-Script, with the Books library on the UNAS over NFS. |
| [`proxmox-disk-replace-cluster-migration.md`](proxmox-disk-replace-cluster-migration.md) | Runbook for swapping a node's disk, reinstalling Proxmox with the same hostname, joining it into the cluster, and restoring guests (handling the VMID collisions, GPU passthrough, and NFS mounts). |
| [`recyclarr.yml`](recyclarr.yml) | Recyclarr config (TRaSH-guide-backed quality profiles and custom formats) referenced by the arr-stack guide. |
| [`archived/`](archived/) | Retired Docker-on-Windows setups — CWA ebook server (`docker-compose.yaml` + Synology-NFS-in-Docker guide) and a Portainer compose. Kept for reference; no longer in use. |

---

## Conventions

- **No secrets in these files.** API keys, passwords, PSKs, and credentials are shown as
  `<PLACEHOLDER>` tokens — swap in real values at runtime and keep them in a local
  untracked file.
- **IPs are genericized.** Real addresses are replaced with hostnames (`synology`,
  `unas`) or placeholders (`<LAN_CIDR>`, `<container-ip>`); internal hostnames and paths
  are left as-is since they're generic examples.
- Written for **Proxmox VE 8.x+** on Debian 13 (Trixie) guests unless a doc says otherwise.

---

## Credits

The ADS-B build stands on the work of **abcd567**
([@abcd567a](https://github.com/abcd567a), active on the
[FlightAware forum](https://discussions.flightaware.com/)) and the
[Proxmox VE Helper-Scripts](https://github.com/community-scripts/ProxmoxVE) community.
Quality profiles come from the [TRaSH Guides](https://trash-guides.info/) via Recyclarr.
And a big thank-you to [**Biblioman** (biblioman.chitanka.info)](https://biblioman.chitanka.info/)
for opening their source code and book database — the custom Bulgarian metadata provider
would not exist without it.