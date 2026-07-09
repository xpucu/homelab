# Arr Stack — Proxmox + NFS Storage Rebuild Guide

A reproducible record of migrating an *arr media stack (Radarr/Sonarr/Lidarr/Readarr,
SABnzbd, qBittorrent) from Docker-on-Windows to Proxmox LXCs/VMs, with NFS-backed
shared storage across two NAS units.

> **Secrets policy:** No API keys, passwords, or credentials in this file.
> Replace every `<PLACEHOLDER>` with your real values at runtime. Keep real
> values in a local untracked file (e.g. `.env`, `secrets.local`) — never commit them.

---

## 1. Architecture Overview

### Nodes
| Host | Role | Guests (ID — name) |
| --- | --- | --- |
| `pve` | Proxmox node A | 100 grocy · 101 prowlarr · 102 radarr · 103 sonarr · 104 bazarr · 105 lidarr · 106 readarr · 107 recyclarr (all LXC) |
| `proxmox` | Proxmox node B | 200 plexy · 101 sabnzbd · 114 unbound (LXC, DoT resolver :5335) · **103 qbit (VM)** |

> CT = LXC container, VM = full virtual machine. `pct list` shows containers only;
> `qm list` shows VMs. A guest missing from `pct list` is probably a VM.
> **VMIDs are per-node** — both nodes have a guest 100/101/103/104; they're unrelated.

### Storage
| NAS | Host | Protocol | Holds |
| --- | --- | --- | --- |
| Synology | `synology` | NFS | Movies (`/volume1/Movies`), TV (`/volume2/Media/TV`) |
| UniFi UNAS Pro | `unas` | NFS (v3 only) | Music & Books (`Shared_Drive/Media/...`) |

### Network
- Flat `/22` LAN, a single subnet (no inter-VLAN routing needed).
- Whitelist NAS NFS exports to the **whole subnet** `<LAN_CIDR>` to avoid
  per-IP whack-a-mole as guests are added.

---

## 2. Design Principles (the "why")

1. **Same path everywhere.** Every app that touches a file — download client *and*
   arr app — must see it at the **identical path**. Consistent paths = no Remote
   Path Mappings and instant same-filesystem imports.
2. **Downloads live on the same share as the library, in a subfolder** — never
   dumped into the library root. Enables fast same-filesystem moves and keeps
   "downloading" separate from "imported."
3. **Per-client download subfolders** (`downloads/sabnzbd`, `downloads/qbit`) so
   two clients grabbing the same title can't collide on the raw download path.
4. **Incomplete downloads stay on fast local storage**, never the NAS. Only the
   completed file lands on the NAS.
5. **Mount NFS on the Proxmox host, bind-mount into LXCs.** Unprivileged LXCs
   cannot reliably mount NFS themselves. VMs mount NFS directly (no bind needed).
6. **Clean app config over importing old DBs.** Prowlarr supplies indexers,
   Recyclarr supplies quality profiles/custom formats — so the old Radarr/Sonarr
   config is mostly stale. Migrate only **import lists** (manually) and re-adopt
   the existing library by scanning the root folder.

---

## 3. Target Directory Layout

```
Synology /volume1/Movies/              ← Radarr library + Plex
├── downloads/
│   ├── sabnzbd/                       ← SAB completed movies
│   └── qbit/                          ← qBit completed movies
└── <library folders>

Synology /volume2/Media/TV/            ← Sonarr library + Plex
├── downloads/
│   ├── sabnzbd/
│   └── qbit/
└── <library folders>

UNAS Shared_Drive/Media/Music/         ← Lidarr library
├── downloads/sabnzbd/
└── <library folders>

UNAS Shared_Drive/Media/Books/         ← Readarr library
├── downloads/sabnzbd/
└── <library folders>
```

---

## 4. NAS Configuration

### 4.1 Synology NFS (per share: Movies, TV)
Control Panel → Shared Folder → *share* → Edit → **NFS Permissions** → Create:

- **Hostname/IP:** `<LAN_CIDR>`
- **Privilege:** Read/Write
- **Squash:** Map all users to admin
- **Security:** sys
- Enable: asynchronous, allow non-privileged ports, allow access to mounted subfolders

Note the export path shown at the dialog bottom (e.g. `/volume1/Movies`). Tip:
**avoid spaces in share names** (renamed `TV Shows` → `TV`) to dodge escaping pain.

### 4.2 UNAS Pro NFS
- UniFi Drive → Settings → Services → **enable NFS**.
- Per shared drive, set **NFS squash mode**:
  - *Collaborative (All Squash)* — recommended; all clients map to one anon user.
  - *Isolated (No Root Squash)* — advanced; **irreversible**, disables SMB/UI access
    for that share. Avoid unless you know you need it.
- Add allowed clients: `<LAN_CIDR>` (covers all nodes/guests).
- **UNAS serves NFSv3 only.** The UI gives a clean v4-style mount path
  (`/var/nfs/shared/<Share>`), but mounts negotiate down to v3 (watch for
  `rpc-statd` starting). Use the UI-provided path.

---

## 5. Host-Side NFS Mounts (repeat on EACH Proxmox node)

```bash
apt update && apt install -y nfs-common
mkdir -p /mnt/movies /mnt/tv /mnt/unas

# test-mount before committing to fstab
mount -t nfs synology:/volume1/Movies /mnt/movies
mount -t nfs synology:/volume2/Media/TV /mnt/tv
mount -t nfs unas:/var/nfs/shared/Shared_Drive /mnt/unas

ls /mnt/movies /mnt/tv /mnt/unas/Media
touch /mnt/movies/.wt && echo OK && rm /mnt/movies/.wt   # write test
```

### `/etc/fstab` (host)
```fstab
synology:/volume1/Movies        /mnt/movies  nfs  rw,_netdev,nofail,soft  0  0
synology:/volume2/Media/TV      /mnt/tv      nfs  rw,_netdev,nofail,soft  0  0
unas:/var/nfs/shared/Shared_Drive  /mnt/unas  nfs  rw,_netdev,nofail,soft  0  0
```
Notes:
- `nofail` = a bad/unavailable mount won't hang boot.
- Paths with spaces need `\040` in fstab (avoided here by renaming the share).
- After editing fstab: `systemctl daemon-reload && umount <mp> && mount -a && ls <mp>`
  to validate without rebooting.

### Create download subfolders (once, on any host that has the mounts)
```bash
mkdir -p /mnt/movies/downloads/{sabnzbd,qbit}
mkdir -p /mnt/tv/downloads/{sabnzbd,qbit}
mkdir -p /mnt/unas/Media/Music/downloads/sabnzbd
mkdir -p /mnt/unas/Media/Books/downloads/sabnzbd
```

---

## 6. Bind Mounts into LXCs

Unprivileged LXCs can't mount NFS reliably — mount on host, bind in.
Edit `/etc/pve/lxc/<CTID>.conf` on the node hosting the container:

```conf
# --- pve node ---
# Radarr (CT 102) — movies only
mp0: /mnt/movies,mp=/mnt/movies

# Sonarr (CT 103) — TV only
mp0: /mnt/tv,mp=/mnt/tv

# Lidarr (CT 105) / Readarr (CT 106) — bind whole /mnt/unas (Option A, see below)
mp0: /mnt/unas,mp=/mnt/unas

# --- proxmox node ---
# SABnzbd (CT 101) — handles movies, TV, music, books
mp0: /mnt/movies,mp=/mnt/movies
mp1: /mnt/tv,mp=/mnt/tv
mp2: /mnt/unas,mp=/mnt/unas
```

Then:
```bash
pct reboot <CTID>
pct enter <CTID>
ls /mnt/movies/downloads && echo BIND_OK
exit
```

Gotchas:
- Bind mounts inside unprivileged LXCs show as `nobody:nogroup` (cosmetic UID
  translation). With `0777` perms on the NAS side, writes still succeed — verify
  with a `touch` test rather than trusting the ownership display.
- If a container warns about nesting on reboot or behaves oddly:
  `pct set <CTID> -features nesting=1,keyctl=1` (set **all** features you want in
  one command — `pct set -features` **replaces** the whole line, it doesn't append).

### Path-consistency rule for UNAS (Lidarr/Readarr) — Option A (chosen)
Bind the **whole `/mnt/unas`** into Lidarr/Readarr and set root folders to
`/mnt/unas/Media/Music` and `/mnt/unas/Media/Books`. This matches the path SAB
reports (`/mnt/unas/Media/Music/downloads/sabnzbd`), so **no Remote Path Mapping
needed**. (Binding only the subfolder as `/mnt/music` would create a path mismatch
requiring a mapping.)

> **When switching an existing container to this:** edit the `.conf` and **replace**
> the old subfolder line (e.g. `mp0: /mnt/unas/Media/Music,mp=/mnt/music`) with
> `mp0: /mnt/unas,mp=/mnt/unas` — don't add a second `mpN` line, or you'll have
> two overlapping binds. Verify with `grep mp /etc/pve/lxc/<CTID>.conf` (should
> show exactly one line). Reboot, then `ls /mnt/unas/Media/Music/downloads/sabnzbd`
> inside the container to confirm.

---

## 7. VM NFS Mounts (qBittorrent VM 103 on `proxmox`)

VMs mount NFS directly — no LXC restriction, no bind. The VM runs as a normal
user (e.g. `<user>`), so commands need **`sudo`** (unlike the root LXC shells).

```bash
# inside the VM
ping -c2 synology                 # confirm LAN reach FIRST (see VPN note)
sudo apt update && sudo apt install -y nfs-common
sudo mkdir -p /mnt/movies /mnt/tv
sudo mount -t nfs synology:/volume1/Movies /mnt/movies
sudo mount -t nfs synology:/volume2/Media/TV /mnt/tv
ls /mnt/movies/downloads && echo QBIT_MOUNT_OK
# write test (qBit must be able to write its download subfolder)
touch /mnt/movies/downloads/qbit/.wt && echo WRITE_OK && rm /mnt/movies/downloads/qbit/.wt
```
Add the same Synology fstab lines **inside the VM** (`sudo nano /etc/fstab`).

> **VPN caveat:** if the VM routes torrent traffic through a VPN, ensure NFS to
> `synology` goes out the **LAN** interface, not the tunnel (split routing).
> A clean `ping -c2 synology` (low latency, ttl 64) confirms the LAN route is
> intact and no static route is needed. If ping fails but internet works, the VPN
> is swallowing the LAN route — add a static route for `<LAN_CIDR>` via the LAN
> gateway.

qBit settings: keep **incomplete/temp on local fast disk** (`/home/qbittorrent/incomplete`);
move completed to `/mnt/movies/downloads/qbit` (category `radarr`) / `/mnt/tv/downloads/qbit`
(category `tv-sonarr`).

> **qBit VM disk-full fix (VM, not LXC — different from `pct resize`).** Symptom: VM
> root at 100% (`/dev/mapper/ubuntu--vg-ubuntu--lv`), qBit can't write, downloads stall.
> Ubuntu uses LVM-on-partition, so growing is multi-step:
> 1. Proxmox host: `qm resize 103 scsi0 +15G` (get disk id from `qm config 103`)
> 2. In VM: `sudo growpart /dev/sda 3` (partition 3 = LVM, NOT 1 which is BIOS boot)
> 3. `sudo pvresize /dev/sda3`
> 4. `sudo lvextend -l +100%FREE /dev/ubuntu-vg/ubuntu-lv`
> 5. `sudo resize2fs /dev/ubuntu-vg/ubuntu-lv`
> Ubuntu's installer often leaves free space in the VG already (claim with steps 4-5
> alone). Keep incomplete LOCAL once root is adequately sized — local is faster than
> CIFS-to-NAS for torrent random I/O; the NAS-incomplete idea was only a band-aid for
> the too-small disk. Note: this VM's thin pool is tight (see §11) — don't over-grow.

> **qBit stale-mount cleanup:** old pre-migration mounts (`/mnt/qbit/temp`,
> `/mnt/downloads`, `/mnt/qbit/radarr`, `/mnt/qbit/tv-sonarr`) can linger as runtime
> mounts even when absent from fstab (`mount -a` adds but never unmounts). Clean with
> `cd /; sudo umount <each>`. Current fstab should hold ONLY `/mnt/movies` + `/mnt/tv`
> (CIFS, credentials file). UNAS is intentionally ABSENT — qBit-for-music/books is
> deferred (needs kill-switch edit + the TV volume needs more space first).

---

## 7a. CIFS Credentials File (for the qBit VM)

Keep NAS passwords out of fstab and shell history:

```bash
sudo nano /root/.smbcred          # contents below
sudo chmod 600 /root/.smbcred
sudo chown root:root /root/.smbcred
```
`.smbcred` contents:
```
username=<NAS_USER>
password=<NAS_PASSWORD>
```
fstab lines reference it (note: CIFS uses **share names**, not `/volume1/...` paths;
TV share is `Media/TV` after the rename, no `\040` space):
```fstab
//synology/Movies    /mnt/movies  cifs  credentials=/root/.smbcred,vers=3.0,uid=qbittorrent,gid=qbittorrent,file_mode=0777,dir_mode=0777,_netdev,nofail  0  0
//synology/Media/TV  /mnt/tv      cifs  credentials=/root/.smbcred,vers=3.0,uid=qbittorrent,gid=qbittorrent,file_mode=0777,dir_mode=0777,_netdev,nofail  0  0
```
`uid=qbittorrent` forces ownership so the qBit service user can write. Always
`sudo mount -a` (the credentials file is root-only; a non-sudo `mount -a` fails
with `error 13 Permission denied` — which is the file being correctly locked down).

> **Kill-switch boot ordering:** CIFS at boot needs port 445, which the kill switch
> permits once it runs. With `nofail`, a too-early mount won't hang boot — `sudo mount -a`
> after the firewall is up will pick it up. Add a mount dependency on the killswitch
> service if boot-time auto-mount proves unreliable.

> ⚠️ **NEVER `iptables -F` with DROP policies set** — the kill switch defaults
> `INPUT/OUTPUT` to `DROP`, so flushing the *rules* leaves an all-blocking firewall
> and locks you out of SSH. Recovery via Proxmox **noVNC console** (not `qm terminal`
> if no serial configured): `iptables -P INPUT ACCEPT; iptables -P OUTPUT ACCEPT;
> iptables -P FORWARD ACCEPT; iptables -F` (policies to ACCEPT **first**, then flush).
> To open the firewall safely for maintenance: stop qBit, set policies ACCEPT, then
> flush; re-run `vpn-killswitch.sh` and verify before restarting qBit.

---

## 7b. SABnzbd Container Config (CT 101, no VPN)

SAB has **no VPN/kill switch**, so it uses NFS like the other LXCs — no CIFS needed.
Media paths come from the **host NFS binds** (`/mnt/movies`, `/mnt/tv`, `/mnt/unas`);
the container's own fstab should hold **no CIFS lines**.

**Cleanup performed (migrating off old CIFS config):**
- Removed all `//Syn/...` CIFS lines from the *container* `/etc/fstab` — they were
  stale and conflicting with the host NFS binds (the container fstab had CIFS entries
  for `/mnt/movies`, `/mnt/tv`, `/mnt/music`, `/mnt/downloads` that fought the binds).
- Unmounted the leftover CIFS mounts `/mnt/downloads` and `/mnt/music`
  (stop SAB first — `umount` fails with "target is busy" while SAB holds the folder;
  `cd /` so your shell isn't inside the mount either).
- Container fstab now contains only `# UNCONFIGURED FSTAB FOR BASE SYSTEM`.

**SAB folder settings (Config → Folders):**
- **Temporary Download Folder (incomplete):** `incomplete` → resolves to local
  `/opt/sabnzbd/incomplete` (LOCAL, fast — never the NAS). Create it:
  `mkdir -p /opt/sabnzbd/incomplete && chown sabnzbd:sabnzbd /opt/sabnzbd/incomplete`
- **Completed Download Folder:** `/mnt/movies/downloads/sabnzbd` (base; per-category overrides it)

**SAB categories (Config → Categories)** — names standardized to match arr defaults:
`movies`, `tv`, `music`, `books` (renamed from SAB's default `audio`/`ebooks`),
each pointing at its `/mnt/.../downloads/sabnzbd` folder (see table in §8).

**Useful SAB defaults already set:** `unwanted_extensions = iso` with
`action_on_unwanted_extensions = 2` blocks ISO downloads (complements Recyclarr's
BR-DISK block).

> **Updating SAB (Helper-Script LXC):** run `ct/sabnzbd.sh` from the **host**, choose
> Update. Requires ≥2 vCPU / 2048 MB — under-provisioning (e.g. 1/512) causes the
> build to fail/interrupt. If a prior apt op was interrupted: `dpkg --configure -a`
> then `apt-get install -f` inside the container before retrying. HA-managed
> containers won't release the config lock for `pct set` — use
> `ha-manager set ct:<id> --state stopped`, resize, then `--state started`.

---

## 7c. Plex Container Config (CT 100, proxmox node)

Plex was the one component whose storage was NOT migrated during the main rebuild —
it kept its old per-share CIFS mounts and pointed at stale paths, so newly-imported
media was invisible in Plex even though Sonarr/Radarr placed files correctly.

**Symptom:** file present at `/mnt/tv/<show>/...` when checked from SAB/Sonarr, but
`ls /mnt/tv` inside the Plex container failed / showed a dead mount (`d?????????`).
Root cause: Plex's container fstab had its own CIFS mounts — and critically `/mnt/tv`
pointed at `//Syn/Media/TV Shows` (old name, with space) while Sonarr writes to the
renamed `Media/TV`. Different folder → Plex never saw new files.

**Fix (same host-mount-then-bind pattern as the other guests):**
```bash
# proxmox host — bind the NFS mounts into Plex
echo 'mp0: /mnt/movies,mp=/mnt/movies' >> /etc/pve/lxc/100.conf
echo 'mp1: /mnt/tv,mp=/mnt/tv'         >> /etc/pve/lxc/100.conf
echo 'mp2: /mnt/unas,mp=/mnt/unas'     >> /etc/pve/lxc/100.conf
pct set 100 -features nesting=1,keyctl=1
pct reboot 100
```
Then inside the container, **delete all `//Syn/...` CIFS lines from `/etc/fstab`**
(they conflict with the host binds). Verify: `ls /mnt/tv && ls /mnt/movies && ls /mnt/unas/Media/Music`.

**Plex library paths must be updated to match the new mounts:**
- Movies: old mount was `/mnt/movie` (singular!) → repoint library to `/mnt/movies` (plural)
- TV: `/mnt/tv` (same path, new NFS mount — just rescan)
- Music: was old CIFS `/mnt/music` → repoint to `/mnt/unas/Media/Music` (UNAS)
- Then **Scan Library Files** on each.

> **Still on CIFS / not yet migrated for Plex:** mp3 and photos libraries
> (`//Syn/Media/mp3`, `//Syn/Media/Pictures`). To finish: enable NFS on those Synology
> shares (whitelist `<LAN_CIDR>`), mount on host + fstab, bind into Plex. Deferred.

**Updating Plex (Helper-Script LXC):** run `ct/plex.sh` from the host → Update. Do NOT
use the "manual download" link in the Plex UI. Updated 1.43.1 → 1.43.2 this way.

> **GPU hardware transcoding** (Intel Quick Sync passthrough into this LXC, plus the
> JasperLake "disable HEVC encoding" gotcha) is documented separately in
> [`gpu-passthrough-plex.md`](gpu-passthrough-plex.md).

**Remote access (manual port-forward — UniFi has no NAT-PMP/UPnP).** Plex's auto-mapping
logs `NAT: PMP … Not Supported by gateway` and reports external port `0`, because UniFi
doesn't do NAT-PMP/UPnP by default. Set it up manually:
- **UniFi Network 9.x** (Zone-Based Firewall): the port-forward form moved to
  **Policy Engine → Policy Table → Create Policy → type = Port Forwarding** (or just use the
  Settings **search** box → "port forward"). Rule: **WAN TCP 32400 → `192.168.0.200:32400`**.
  Use the **"Select Device"** link to pick `plexy` — free-typing the IP can throw a false
  *"Please enter a valid IPv4 address"* error; selecting the device clears it.
- **Plex → Settings → Remote Access → "Manually specify public port" = 32400** → Apply → goes
  green. This also stops the NAT-PMP / `plex.direct:0` warnings (Plex no longer needs to
  auto-map).
- The UniFi **"WAN uses a dynamic IP → configure DDNS"** warning is **safe to ignore for
  Plex** — PMS reports its current public IP to plex.tv and clients connect via the
  `plex.direct` hostname, so a changing Cox WAN IP is handled automatically; no DDNS needed.
  (Confirmed not behind CGNAT/double-NAT: UniFi's WAN IP equals the real public IP.)
- **Pin the target:** set a **DHCP reservation** for `plexy` at `192.168.0.200` so the
  forward can't point at the wrong host after a lease change.

**Plex webhook / log-noise cleanup (done during a log review):**
- **Bazarr webhook** was two malformed entries (one missing the port → hit port 80; one
  missing the path → `405`). Correct single URL is
  `http://bazarr:6767/api/webhooks/plex?apikey=<BAZARR_API_KEY>&instance=Bazarr` (fix under
  **Plex → Settings → Webhooks**). Needs the `bazarr` name to resolve — see the static
  `/etc/hosts` bridge until internal DNS exists.
- **Custom server access URLs** had the field's *label text* pasted into the value
  (`Invalid connection URL 'Custom server access URLs: https://…'`). Under **Settings →
  Network**, set it to the bare `plex.direct` URL or leave it **empty** if not using a
  reverse proxy.
- Benign, ignorable log lines: `Unknown metadata type: folder`, `QueryParser: Invalid field`,
  transcode `ping/stop without valid session ID`, `[FFMPEG] No quality level set`,
  `CERT/OCSP … skipping stapling`, `Crash reporting disabled`. Watch only if repeating:
  `Invalid library metadata ID <n>` (usually a stale client / a collection pointing at a
  deleted item).

---

## 8. App Configuration (clean build)

### Root folders
| App | Root folder |
| --- | --- |
| Radarr | `/mnt/movies` |
| Sonarr | `/mnt/tv` |
| Lidarr | `/mnt/unas/Media/Music` |
| Readarr | `/mnt/unas/Media/Books` |

### Download clients (in each arr app: Settings → Download Clients)

**Standardized category names** (must match exactly on both the arr-app side and the
download-client side — the name is the contract between them):

| Arr app | Category | SAB folder | qBit folder |
| --- | --- | --- | --- |
| Radarr | `movies` | `/mnt/movies/downloads/sabnzbd` | `/mnt/movies/downloads/qbit` |
| Sonarr | `tv` | `/mnt/tv/downloads/sabnzbd` | `/mnt/tv/downloads/qbit` |
| Lidarr | `music` | `/mnt/unas/Media/Music/downloads/sabnzbd` | (qBit n/a — books/music via SAB) |
| Readarr | `books` | `/mnt/unas/Media/Books/downloads/sabnzbd` | (qBit n/a) |

> SAB ships default categories named `audio`/`ebooks` — **rename them to `music`/`books`**
> so they match Lidarr/Readarr defaults. The folder path matters less than the name
> matching; a mismatched name silently routes to the wrong/default folder.


- **SABnzbd:** host `<SAB_IP>:<PORT>`, API key `<SAB_API_KEY>`, category e.g. `movies`/`tv`.
  - In SAB → Config → Categories, point category folders at
    `/mnt/<cat>/downloads/sabnzbd`. Keep SAB's **incomplete dir on local disk**.
- **qBittorrent:** host `<QBIT_IP>:<PORT>`, user `<QBIT_USER>` / pass `<QBIT_PASS>`,
  category `radarr`/`tv-sonarr`. Save path `/mnt/<cat>/downloads/qbit`,
  incomplete on local temp.

Because all components reference identical paths, **no Remote Path Mappings**
should be required. Add one only if a client reports a path the arr app can't see.

### Indexers — via Prowlarr (don't add per-app)
Prowlarr → Settings → Apps → add each arr app (URL + API key). Prowlarr pushes
indexers to all of them. Add **FlareSolverr** for Cloudflare-protected indexers.

> **Migrating Prowlarr from old (Docker) instance:** the path of least resistance is
> **backup/restore**, NOT manual re-entry. Old Prowlarr UI → System → Backup → download
> zip → new Prowlarr → System → Backup → Restore → upload. Brings indexers *with* their
> API keys intact (the API/UI mask keys as `********`; only the `prowlarr.db` holds them
> unmasked). After restore, just re-point the 4 app connections to new IPs. Prowlarr
> config has no path/filesystem baggage, so a restore is safe even in a "clean rebuild".
> Restored download clients that test green (e.g. host `sabnzbd` resolving via local DNS)
> can be left as-is.

### Quality profiles / custom formats — via Recyclarr (don't hand-build)
See `recyclarr.yml` (separate file). Workflow:
```bash
recyclarr sync --preview   # review (non-destructive)
recyclarr sync             # apply
```

**Recyclarr v8.x syntax lessons (hard-won):**
- The modern, recommended approach is **guide-backed quality profiles**: list each
  profile by `trash_id` with `reset_unmatched_scores: enabled: true`, add a
  `quality_definition: {type: movie|series}`, and STOP. The profile auto-pulls its
  qualities, custom formats, scores, HDR/audio/unwanted groups. Don't hand-list CF IDs.
- Only add a `custom_formats:` block to *override* a score (e.g. extra-hard BR-DISK −10000).
- `custom_format_groups` is NOT a flat list — it takes `add:`/`skip:` with `- trash_id:`
  objects. But you rarely need it; default groups come with the profile.
- `replace_existing_custom_formats` was REMOVED in v8 — don't use it.
- Inline `# comments` on bare list items can break the YAML parser in some versions;
  when in doubt, strip comments.
- `recyclarr list quality-profiles <svc>` and `list custom-format-groups <svc>` give
  current trash_ids. `recyclarr config create` scaffolds the config path
  (`/root/.config/recyclarr/recyclarr.yml`). Scheduled via `/etc/cron.d/recyclarr`
  (NOT a systemd timer on this Helper-Script install).

**Profiles produced:** Radarr `HD Bluray + WEB` (1080p, no remux) + `UHD Bluray + WEB`
(2160p, no remux); Sonarr `WEB-1080p` + `WEB-2160p`. All exclude remux (Plex-transcode
friendly), block BR-DISK/ISO, prefer HDR (Infuse), score audio passthrough formats high.
Quality definitions set Max=Unlimited (fixes the restrictive default size cap).

> Keep movies/shows on the **1080p** profiles until 4K storage is sorted (thin pool
> can't hold 4K downloads). The UHD/2160p profiles exist but assign deliberately.

> **x265 (HD) is blocked at 1080p by TRaSH default** (−10000) — 1080p grabs will be
> x264, which is fine/better for Plex transcoding. Sonarr `Language: Not Original` is
> also −10000 (blocks non-original-language). Both are intentional TRaSH defaults.

### Library adoption
Adding a root folder does **not** auto-import. Use **Library Import**
(Movies → Library Import / "Import Existing Movies on Disk"), review matches,
assign profile. Best done *after* Recyclarr creates the profiles so you assign
the right one up front.

### Import lists
Re-add manually (Trakt/IMDb/etc.) — last step, once the core pipeline works.
Done this build: Trakt + Plex Watchlist in both Radarr + Sonarr.

### Downstream integrations to repoint after migration
Anything pointing at the OLD Windows arr instances needs updating to the new IPs/keys:
- **Home Assistant** — Sonarr integration repointed to new Sonarr ✅. Check Radarr
  integration + any SAB/qBit download-client sensors still aimed at old host IPs.
- **Connections (Settings → Connect) in Radarr + Sonarr** — NOT restored (no backup).
  Re-add: Plex Media Server (library-update-on-import — high value now that Plex mounts
  are fixed; points at CT 100), Notifiarr, Discord/Telegram, any webhooks. ⚠️ Custom
  scripts referenced Windows paths (`.bat`/`.ps1`) — those need rewriting for Linux
  containers, not copying. Read old instances' Settings → Connect (or the `Notifications`
  table in old `sonarr.db`/`radarr.db`) to see what to recreate.
- **Notifiarr (two-sided reconnect):** (1) in the Notifiarr dashboard/client, update each
  arr integration to the NEW container URL + API key (old Windows-host IPs are dead);
  (2) in each arr app, re-add Settings → Connect → Notifiarr with the Notifiarr API key
  and event triggers. If a `notifiarr` client container runs locally, its config also
  holds the arr URLs/keys — repoint those too.
- **Notifiarr CLIENT migration (was on the old Windows host):** ✅ moved to a dedicated
  LXC on pve (CT `notifiarr`). Build via Helper-Script. **Gotcha:** the Windows
  `notifiarr.conf` is stored **bzip2-compressed** — `pct push` copied it as-is and it
  looked scrambled in nano. Decompress first: `bunzip2 -c notifiarr.conf > conf.real`,
  fix line endings (`sed -i 's/\r$//'`), repoint `[[apps.*]]` URLs to new pve IPs, then
  install **uncompressed** as `/etc/notifiarr/notifiarr.conf` (Linux client reads plain
  text), `chmod 600`. The `DAEMON_OPTS unset` log line is harmless. API key also
  retrievable from the notifiarr.com account if all local copies are mangled.
  **Remember to STOP the Windows client** — two clients on one API key = duplicate
  notifications. ✅ DONE: old Windows client deleted, new pve client registered on
  notifiarr.com, all 9 integrations test green (Sonarr/Radarr/Readarr/Lidarr/Prowlarr/
  Plex/Tautulli/qBittorrent/SABnzbd).

> **Notifiarr free-tier rate limit (5 API hits/sec).** The Connect → Notifiarr passthrough
> (`/api/v1/notification/<app>`) posts **one API hit per event**; a burst over 5/s temporarily
> blocks the key (`limit exceeded for non subscriber account … Api key blocked`). Bursts come
> from **fan-out events** — a **season-pack import fires "On Import" once per episode file**
> (a 10-episode pack = 10 hits in a second), plus "On Grab" storms and mass renames. **Fix:**
> in each arr → **Settings → Connect → Notifiarr**, disable the high-volume triggers
> (**On File Import, On Grab, On Rename, On Health Issue/Restored, On Manual Interaction**)
> and keep only what you actually consume. Also confirm exactly **one** Notifiarr connection
> per app and **one** running client — a stray duplicate doubles the hit rate (see the
> "STOP the Windows client" note above). The block is a short cooldown, not a ban; it clears
> once you're back under 5/s. Patron tier raises the limit to 15/s, but sane triggers make
> that unnecessary.
- Any dashboards, Homepage/Homarr, Tautulli, Overseerr/Jellyseerr, notification
  webhooks, etc. that referenced the old Windows Sonarr/Radarr endpoints.

---

## 9. Migration Order (clean rebuild)

1. Install apps (Proxmox community Helper-Scripts).
2. NAS NFS exports + whitelist subnet.
3. Host NFS mounts + fstab (each node).
4. Download subfolders.
5. Bind mounts into LXCs / NFS inside VMs.
6. Root folders.
7. Download clients (SAB + qBit), categories, local incomplete dirs.
8. Prowlarr → apps (+ FlareSolverr).
9. Recyclarr → quality profiles/custom formats.
10. Library import (assign profiles).
11. Import lists (manual).

---

## 10. Troubleshooting Cheatsheet

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `mount.nfs: access denied by server` | Client IP not whitelisted on NAS export | Add IP or `<LAN_CIDR>` to the share's NFS allowed-clients |
| `mount.nfs: Operation not permitted` | Mounting NFS inside an unprivileged LXC | Mount on host, bind-mount in (don't mount in the CT) |
| `showmount -e` empty | NFS not enabled per-share, or client not in allow-list | Enable NFS on the share; add client |
| `showmount` path ≠ UI path | UNAS NFSv4 pseudo-path vs v3 real path | Use the path the official docs/UI give |
| Bind mount shows `nobody:nogroup` | Unprivileged-LXC UID translation | Cosmetic; verify with `touch` write test |
| `pct set -features` dropped a feature | `-features` replaces the whole line | Re-set with all features listed together |
| fstab edited but `mount -a` uses old version | systemd cache | `systemctl daemon-reload` |
| Slow imports / double disk usage | Cross-share copy instead of same-fs move | Put downloads + library on the same share/volume |
| Guest not in `pct list` | It's a VM, not an LXC | Use `qm list` |
| `WARN: Systemd 255 detected. You may need to enable nesting` on CT reboot | Container missing nesting feature | `pct set <CTID> -features nesting=1,keyctl=1` |
| `hostname.local` ping returns an unexpected IP | mDNS resolving to a mgmt/other interface | Trust `ip -4 addr` (the `vmbr0` LAN IP) for whitelisting, not the `.local` name |
| `apt: Permission denied / could not open lock file` | Running as non-root user in a VM | Prefix with `sudo` |
| `umount: target is busy` | App still using the mount, or shell `cd`'d inside it | Stop the app; `cd /`; then `fuser -m <mp>` to find holders |
| `dpkg was interrupted` / apt `exit code 100` | Half-finished package op (often from under-provisioned LXC) | `dpkg --configure -a` then `apt-get install -f` inside the CT |
| `can't lock file pve-config-<id>.lock` on `pct set` | Container is HA-managed | `ha-manager set ct:<id> --state stopped`, edit, then `--state started` |
| Helper-Script update fails on `jq` / under-provisioned | LXC below 2 vCPU / 2048 MB | Bump resources (`pct set <id> -cores 2 -memory 2048`), repair dpkg, retry |
| SAB "not writable with special character filenames" | Download folder is on CIFS | Move incomplete to local disk; use NFS for completed |
| Container `/mnt/x` is CIFS but should be NFS bind | Stale CIFS line in *container* fstab overriding intent | Remove CIFS lines from container fstab; media comes from host binds |
| `cp: failed to preserve ownership … Operation not permitted` copying **to** a share | Squashed NFS export (UNAS all-squash) maps every client to one anon user — you can't `chown` on it, so `cp -a`/`cp -p` fail the ownership step and return non-zero (data *is* copied, but a `set -e` script then aborts) | Copy **bytes only** to squashed shares: `cp --preserve=timestamps` (or plain `cp`), never `cp -a`/`-p`. Note `mv` across filesystems is fine — it treats can't-preserve-ownership as a non-fatal warning and still returns 0. (Bit `acw-db-mode.sh`'s backup step.) |
| Plex playback fails with **`s1001 (Network)`** on some files (esp. movies), others fine; log shows `MDE: video has neither a video stream nor an audio stream` → `Cannot make a decision because either the file is unplayable...` → HTTP 400 | **Corrupt media file** — full size but a leading zero-block (interrupted write / preallocated-but-incomplete download imported anyway). Not the mount/network/codec: healthy files Direct Play, the rotted one can't be analysed. The `Network` label is misleading | Probe the file with Plex's own ffmpeg: `FFMPEG_EXTERNAL_LIBS="…/Codecs/<build>-linux-x86_64/" "/usr/lib/plexmediaserver/Plex Transcoder" -i "<file>"` — `Invalid data … EBML` / no `Stream #` = corrupt. Confirm with `hexdump -C "<file>" | head` (leading `00`s). Scope both libraries with `plex-corrupt-scan.sh`. Fix: re-grab in Radarr/Sonarr (delete file → search) or restore a Synology snapshot; then Analyze the item. If **many** files across volumes are hit, suspect the NAS — check Storage Manager health + S.M.A.R.T. + run a Btrfs data scrub |

---

## 11. Known Constraints

- **UNAS = NFSv3 only.** No v4; expect `rpc-statd`. Cross-NAS hardlinks
  (Synology↔UNAS) are impossible — irrelevant here since music/books are small
  and "delete after import" is the chosen policy.
- **Squash modes** mean on-NAS ownership reflects the mapped anon/admin user, not
  the container's user. Fine for a single-admin homelab.
- **SAB incomplete must be local AND adequately sized.** First set on the container's
  8 GB root disk → one download filled it. Fixed via `pct resize 101 rootfs +80G`.
- **Thin-pool TRIM / `fstrim` setup.** On LVM-thin, `Data%` ≠ filesystem usage — thin
  volumes report blocks *ever written*, and deleted files don't return space to the pool
  without TRIM. Keep TRIM working so a busy download client doesn't slowly balloon pool
  usage over time:
  - **TRIM differs LXC vs VM.** LXCs: `pct fstrim <id>` from the host (the in-container
    `fstrim.timer` is skipped — `ConditionVirtualization=!container`). **VMs: `pct
    fstrim` does NOT work**; the VM disk also needs `discard=on,ssd=1` or trims never
    reach the pool:
    `qm set 103 -scsi0 local-lvm:vm-103-disk-0,iothread=1,discard=on,ssd=1,size=35G`
    then `qm reboot 103`, then inside the VM `sudo fstrim -av` +
    `sudo systemctl enable --now fstrim.timer` (VM systemd CAN run the timer).
  - **Automated safety net:** host cron `0 3 * * 0 for id in 100 101 104; do pct fstrim
    $id; done` (LXCs) + the qBit VM's own `fstrim.timer` = full coverage.
- **4K NOT safe on this thin pool** (~22 GB free). A single 40–80 GB 4K download would
  exhaust the pool mid-download before SAB moves/trims it. 4K needs more physical disk
  on the node or incomplete-on-NAS. 1080p focus is fine.
- **Timezone convention = `America/Phoenix`.** All guests are set to Phoenix local time
  (owner is in Phoenix; Arizona observes **no DST**, so local time carries no DST-ambiguity
  downside — logs read in wall-clock time *and* stay unambiguous). Guests defaulted to UTC,
  which made log-vs-wall-clock correlation painful. Set per container with:
  ```bash
  # inside the CT (unprivileged-safe; timedatectl may be refused, this always works)
  ln -sf /usr/share/zoneinfo/America/Phoenix /etc/localtime
  # fleet sweep from each node:
  for id in $(pct list | awk 'NR>1{print $1}'); do pct exec "$id" -- ln -sf /usr/share/zoneinfo/America/Phoenix /etc/localtime 2>/dev/null; done
  ```
  Gotchas: (1) **running services cache TZ at process start** — restart each (Plex, *arr,
  SAB) or reboot the CT for its logs to switch. (2) `pct exec` only touches **LXCs** — the
  qBit **VM** needs `sudo timedatectl set-timezone America/Phoenix` inside it. (3) The
  **Proxmox host nodes** are separate from their guests — set on each node if you want host
  logs on Phoenix too. (4) `ln -sf …/localtime` doesn't update `/etc/timezone`; add
  `echo 'America/Phoenix' > /etc/timezone` only if some app reads that for display.

## 12. Deployment Status (storage plumbing)

| Guest | Node | Mounts | Status |
| --- | --- | --- | --- |
| Radarr (102) | pve | `/mnt/movies` | ✅ bound, write-tested |
| Sonarr (103) | pve | `/mnt/tv` | ✅ bound |
| Lidarr (105) | pve | `/mnt/unas` (root folder `…/Media/Music`) | ✅ bound |
| Readarr (106) | pve | `/mnt/unas` (root folder `…/Media/Books`) | ✅ bound |
| SABnzbd (101) | proxmox | `/mnt/movies`, `/mnt/tv`, `/mnt/unas` (NFS binds) | ✅ bound; CIFS cruft removed; incomplete→local; cats standardized; v5.0.4; 2 vCPU/2GB |
| qBit (VM 103) | proxmox | `/mnt/movies`, `/mnt/tv` (**CIFS**, in-VM) | ✅ mounted, write-tested |
| Plex (CT 100) | proxmox | `/mnt/movies`, `/mnt/tv`, `/mnt/unas` (NFS binds) | ✅ migrated off old CIFS; v1.43.2; library paths fixed; mp3/photos deferred |

> **qBit uses CIFS, not NFS** — its kill switch firewall whitelists only SMB
> ports (445/139) to the NAS, and NFS (2049/111) would be blocked. CIFS needs no
> firewall change and qBit doesn't benefit from NFS hardlinks (delete-after-import).
> Paths still resolve to the same Synology folders at the same `/mnt/.../downloads/qbit`
> strings, so no Remote Path Mapping is needed despite the protocol difference.

Host fstab persistent on **both** nodes: `/mnt/movies`, `/mnt/tv`, `/mnt/unas`.

### Remaining (app config)
- [x] Root folders in each arr app
- [x] Download clients (SAB all 4; qBit on Radarr/Sonarr) with categories
- [x] Prowlarr → apps (restored from backup, 4 apps reconnected)
- [x] Recyclarr sync (profiles/custom formats) — 59 CF / 2 profiles / 14 sizes per service
- [x] Library import (Radarr + Sonarr adopted existing media)
- [x] End-to-end verified: Sonarr grab → SAB download → import to /mnt/tv → **visible in Plex**
- [x] Import lists — **Trakt + Plex Watchlist** re-created in both Radarr + Sonarr
      (verify each list's quality profile = 1080p, monitor setting, and root folder so
      Watchlist adds don't pull 4K or trigger unwanted searches)
- [ ] Plex mp3 + photos libraries — still on old CIFS; migrate to NFS (enable NFS on
      Synology mp3/Pictures shares, mount on host, bind into Plex 100)
- [ ] FlareSolverr — only if a Cloudflare-protected indexer needs it

### Known quirks accepted
- Radarr health warning "downloads in root folder" — **intentionally dismissed**.
  Downloads at `/mnt/movies/downloads/sabnzbd` are nested under root `/mnt/movies` by
  design (same-share = fast moves). Safe here: SAB writes only completed files there
  (incomplete is local), Radarr imports out promptly, delete-after-import. The alternative
  (downloads outside the library share) would break the same-filesystem move. Ignore it.
- Sonarr profiles are named **WEB-1080p / WEB-2160p** (not "HD Bluray + WEB" — that's
  Radarr-only). Assign WEB-1080p as the 1080p default.

