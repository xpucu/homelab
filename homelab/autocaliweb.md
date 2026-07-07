# Autocaliweb (ACW) — Install & Config on Proxmox

Ebook server replacing the old **CWA (Calibre-Web-Automated)** that ran in Docker on the
Windows host. CWA is Docker-only with no native Proxmox install; **Autocaliweb** is an
actively-maintained fork of CWA that added first-class non-Docker install, which is why
it has a maintained Proxmox Helper-Script (and CWA doesn't).

**Decision: fresh install, NOT migrating old CWA settings.**

- Target node: **`proxmox`** (not pve)
- Library lives on: **UNAS** at `/mnt/unas/Media/Books` (NFS)
- Web UI: port **8083**  ·  default login `admin` / `admin123` (change immediately)

---

## 0. Prerequisite — confirm `/mnt/unas` exists on the proxmox node

The install CT is **unprivileged** and can't mount NFS itself — it relies on a host mount
bound in (same pattern as SAB/Lidarr/Readarr). The `pve` node had `/mnt/unas`; the
**proxmox** node must have it too. Check first:

```bash
# on the proxmox node
mount | grep /mnt/unas
ls /mnt/unas/Media/Books
```

If it's **missing**, mount it on the proxmox host before proceeding:
```bash
# install client if needed
apt install -y nfs-common
# mount (UNAS is NFSv3 only)
mkdir -p /mnt/unas
mount -t nfs -o vers=3 unas:/var/nfs/shared/Shared_Drive /mnt/unas
ls /mnt/unas/Media/Books   # confirm books visible
# persist in fstab
echo 'unas:/var/nfs/shared/Shared_Drive /mnt/unas nfs vers=3,_netdev,nofail 0 0' >> /etc/fstab
```
(UNAS whitelist must include the proxmox node's IP for `<LAN_CIDR>` — it should already,
from the arr-stack build.)

---

## 1. Run the Helper-Script (on the proxmox node)

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/autocaliweb.sh)"
```

Prompts:
- **Mode:** Default (or Advanced to set CTID/IP explicitly)
- **CTID:** pick a free ID on the proxmox node (it currently has 100/101/103/104 — 101
  will be destroyed after the SAB move; pick e.g. **105** or another free number)
- **Disk:** bump to **~8 GB** (Calibre conversion binaries are chunky; the proxmox pool
  has room again once SAB's 88 GB is reclaimed)
- **Resources:** 2 vCPU / 2 GB RAM defaults are fine
- Builds Debian 13 + ACW + Calibre binaries (pulled from Codeberg)

**Record the CTID and IP** it prints at the end — both needed next.

---

## 2. Post-install — bind the UNAS into the CT (REQUIRED)

```bash
# on the proxmox host — replace <ctid>
echo 'mp0: /mnt/unas,mp=/mnt/unas' >> /etc/pve/lxc/<ctid>.conf
pct reboot <ctid>

# verify ACW's container sees the books
pct enter <ctid>
ls /mnt/unas/Media/Books        # should list your ebooks / library
```

If the CT needs nesting/keyctl for anything (usually not for ACW), the pattern is
`pct set <ctid> -features nesting=1,keyctl=1`.

---

## 3. First-run setup (web UI)

Open `http://<ip>:8083` — login `admin` / `admin123`.

1. **Change the admin password** immediately.
2. **Set the Calibre library location** → `/mnt/unas/Media/Books`
   - If a Calibre library (a `metadata.db`) already exists there → ACW adopts it.
   - If it's loose ebook files or empty → ACW auto-creates a fresh library.
3. **Ingest folder + auto-convert:** set the ingest/drop folder and the auto-conversion
   target format. ⚠️ Files dropped in the ingest folder are **deleted after processing** —
   it's a drop-zone, not storage.
4. **Providers:** enable metadata providers (Google Books etc.) for cover/metadata fetch.

---

## 4. Kindle Oasis integration (optional)

- **Send-to-Kindle:** configure SMTP in ACW settings; add ACW's sender address to your
  Amazon "approved personal document email" list. Then send books straight to the Oasis.
- Or download converted **AZW3/MOBI** and sideload via USB.
- ACW converts formats via Calibre binaries, so EPUB→Kindle-friendly is handled for you.

---

## 5. Known risks / gotchas

- **⚠️ SQLite `metadata.db` over NFS** — the whole Calibre-Web family can hit
  "database is locked" when the library's `metadata.db` sits on NFS/SMB (weak file
  locking). Watch for it. If it bites: move the library (or at least `metadata.db`) to
  local container storage, or ensure ONLY ACW touches the db (no parallel Calibre desktop
  writing to the same file).
- **Codeberg dependency** — install/updates pull from Codeberg (project moved off GitHub).
  If Codeberg is down, install/update fails; retry later.
- **Updates:** re-run the same script → it offers Update; it backs up config/env to a tar
  first. The community script itself may not support in-place updates for every release —
  check the script page notes.
- **Unprivileged + NFS squash:** files ACW writes to the UNAS show as the mapped anon
  user (cosmetic); writes work given the share's permissions.

---

## 6. Feature parity vs the old CWA (for reference)

Equal: ingest automation, auto-convert (Calibre binaries), metadata download + write to
ebook files, OPDS, Kobo/Kindle formats, shelves, user management.
ACW adds: OIDC/SSO, native (non-Docker) install.
CWA-only extras (not needed here): Hardcover metadata provider, KOReader sync, Magic
Shelves, split-library support. Standard metadata providers ARE in ACW — only the
Hardcover-specific source is absent.

---

## 7. Status

- [ ] Confirm `/mnt/unas` on proxmox node
- [ ] Run Helper-Script, record CTID + IP
- [ ] Bind UNAS into CT, reboot, verify books visible
- [ ] First-run: change password, set library `/mnt/unas/Media/Books`, ingest, providers
- [ ] Kindle send-to-device (optional)
- [ ] Retire old CWA on the Windows host → **last Windows-Docker holdout gone**