# Plex GPU Hardware Transcoding — Intel Quick Sync → Unprivileged LXC

Enabling Intel Quick Sync hardware transcoding for Plex running in an **unprivileged
Proxmox LXC**, including the one JasperLake-specific gotcha that makes or breaks it.

> **TL;DR:** GPU passthrough to an LXC is easy (no VFIO/IOMMU — just expose the render
> node). The trap on this box is that JasperLake's **HEVC hardware *encoder* is broken**
> (`hevc_vaapi: Failed to map output buffers: 24`). Fix = **disable HEVC video encoding**
> in Plex so it uses the rock-solid `h264_vaapi` encoder instead.

---

## 1. Environment

| Item | Value |
| --- | --- |
| Node | `proxmox` (node B) |
| Guest | **CT 200** `plexy` — unprivileged LXC |
| GPU | Intel **JasperLake [UHD Graphics]** `8086:4e55`, host driver `i915` |
| Plex | Plex Media Server (Helper-Script LXC), **Plex Pass required** for HW transcode |
| Client that triggered this | Echo Show (Plex Android app) — low-power **SDR** device |

**Why bother:** the Echo Show can't direct-play a 4K HEVC/HDR/TrueHD remux, so Plex must
transcode. On CPU a 4K transcode stalls → the client shows *"An error occurred… check your
connection"* (or `s1001`). On the iGPU it's easy. Quick Sync also does **HW HDR→SDR
tone-mapping** so HDR sources don't look washed-out on the SDR screen.

---

## 2. Host side (Proxmox node)

The `i915` driver exposes the render node on the host — verify it's there:

```bash
lspci -nnk | grep -iEA3 'vga|display|3d'    # expect JasperLake, "Kernel driver in use: i915"
ls -l /dev/dri                              # expect card1 (226:1) + renderD128 (226:128)
lsmod | grep i915                           # i915 loaded
```

Nothing to install on the host — the kernel `i915` module is all that's needed for the
render node. (Only the **render** node `renderD128` matters for headless transcoding; the
`card1` node is for display and is not required.)

---

## 3. LXC passthrough config

Add to `/etc/pve/lxc/200.conf` on the `proxmox` node (already present on this box):

```conf
lxc.cgroup2.devices.allow: c 226:0 rwm
lxc.cgroup2.devices.allow: c 226:128 rwm
lxc.mount.entry: /dev/dri dev/dri none bind,optional,create=dir
```

Plus `features: nesting=1,keyctl=1` (set via `pct set 200 -features nesting=1,keyctl=1`),
which also applies the nesting-aware AppArmor profile automatically.

Then `pct reboot 200`. What each line does:
- `lxc.mount.entry: /dev/dri …` — bind-mounts the host's `/dev/dri` (card + render nodes)
  into the container.
- `lxc.cgroup2.devices.allow: c 226:128 rwm` — grants the container access to the **render**
  node (major 226, minor 128). **This is the one that matters.**
- `lxc.cgroup2.devices.allow: c 226:0 rwm` — grants minor 0 (`card0`). Note: on this host
  the card node is actually **`card1` (226:1)**, not `card0`, so this line targets a
  non-existent minor. **Harmless** — Plex only uses the render node — but if you ever need
  the card node inside the container, change it to `c 226:1 rwm`.
- **AppArmor: do NOT set `lxc.apparmor.profile: unconfined`.** It isn't needed — device
  access is governed by the cgroup allow above (not AppArmor), and the `/dev/dri` bind is set
  up by the LXC runtime *outside* the container's profile, so the nesting-aware default
  profile (from `features: nesting=1`) works fine for Quick Sync. Worse, an explicit
  `unconfined` line triggers a noisy warning on every start —
  `explicitly configured lxc.apparmor.profile overrides the following settings:
  features:nesting` — and drops the container to *no* confinement. **Verified:** HW transcode
  still works after removing it. Omit it.

> **DRM device numbering:** `card1`/`renderD128` (not `card0`/`renderD128`) is normal when
> the numbering starts at 1. Trust `ls -l /dev/dri` for the real minors rather than assuming
> `card0`.

---

## 4. Container permissions (the `plex` user must reach the device)

Inside the container the render node shows up owned by `root:<group>`, where the group name
is whatever the unprivileged-LXC GID translation lands on — here it displays as **`kvm`**,
not `render` (cosmetic; the numeric GID is what counts). The `plex` service user must be in
that group.

```bash
pct enter 200
ls -l /dev/dri        # note the group on renderD128 (here: kvm)
id plex               # must include that group
```

On this box `plex` is already covered on all the likely names:

```
uid=999(plex) gid=989(plex) groups=989(plex),44(video),993(kvm),992(render)
```

Because `plex` is in **`video` + `kvm` + `render`**, it has access no matter which name the
translation picks. If a rebuild leaves `plex` without it:

```bash
usermod -aG render,video,kvm plex     # cover all three; harmless if some don't exist
systemctl restart plexmediaserver
```

---

## 5. Verify the VAAPI stack

Plex bundles its **own** iHD VAAPI driver for the actual transcode (see the transcode
command's `LIBVA_DRIVERS_PATH=…/Cache/va-dri-linux-x86_64` and `driver=iHD`). For an
independent sanity check, install the system tools in the container:

```bash
pct exec 200 -- apt update
pct exec 200 -- apt install -y vainfo intel-media-va-driver-non-free
pct exec 200 -- vainfo --display drm --device /dev/dri/renderD128
```

A healthy result lists decode **and** encode entrypoints — on JasperLake:

```
VAProfileH264High     : VAEntrypointVLD          # H.264 decode
VAProfileH264High     : VAEntrypointEncSliceLP   # H.264 encode (low-power)
VAProfileHEVCMain     : VAEntrypointVLD          # HEVC decode
VAProfileHEVCMain     : VAEntrypointEncSliceLP   # HEVC encode (low-power) — present but flaky, see §7
VAProfileHEVCMain10   : VAEntrypointVLD          # HEVC 10-bit decode (needed for 4K HDR)
```

Note JasperLake only exposes the **Low-Power (`EncSliceLP`)** encode entrypoints.

---

## 6. Plex settings

Plex Web → **Settings → Transcoder** (Show Advanced):

- ✅ **Use hardware acceleration when available**
- ✅ **Use hardware-accelerated video encoding**
- ✅ **Enable HDR tone mapping** (works fine once §7 is applied)
- **Hardware transcoding device:** Auto (single iGPU — Auto picks `renderD128`)
- ⛔ **Enable HEVC video Encoding → Disabled** ← **the critical one, see §7**

---

## 7. The JasperLake gotcha — disable HEVC encoding ⚠️

**Symptom:** HW transcode initialises correctly (log shows `final decoder: vaapi, final
encoder: vaapi`, `zero-copy`), then dies the instant it encodes a frame:

```
[hevc_vaapi] Failed to map output buffers: 24 (internal encoding error).
[hevc_vaapi] Output failed: -5.
Error submitting video frame to the encoder
Nothing was written into output file …
Plex Transcoder exit code … 251 (failure)
```

Plex then falls back to **software** transcoding, which on a 4K source stalls at 0% → the
client errors.

**Cause:** JasperLake's **HEVC VAAPI *encoder*** (`hevc_vaapi`, low-power-only) is broken/
unreliable on this driver+chip. Decode is fine; H.264 encode is fine; **HEVC encode is not.**
Because the source remux is HEVC, Plex's default *"Enable HEVC video Encoding: HEVC Sources
Only"* makes it encode back **to** HEVC → straight into the broken encoder.

**Fix:** Settings → Transcoder → **Enable HEVC video Encoding → Disabled**. Plex then encodes
to **`h264_vaapi`**, which works perfectly:

```
Reached Decision … encoder=h264_vaapi width=3840 height=2160
TPU: hardware transcoding: final decoder: vaapi, final encoder: vaapi
# …plays, no "map output buffers" errors
```

Costs nothing — every Plex client (Echo Show included) plays H.264.

> **Isolation that proved it:** turning off HDR tone-mapping did **not** help (still
> `hevc_vaapi`, still crashed) — so the tone-mapper was innocent. Disabling HEVC encoding
> **did** fix it. HDR tone-mapping then works fine on top of `h264_vaapi`.

---

## 8. Verify it's actually on the GPU

Play a file that forces a transcode (e.g. the 4K remux to the Echo Show), then:

- **Plex → Settings → Dashboard** — the video line should read **`Transcode (hw)`**.
- **Live GPU load** from the host:
  ```bash
  pct exec 200 -- apt install -y intel-gpu-tools   # once
  pct exec 200 -- intel_gpu_top                     # Video + Render engines busy during playback
  ```
- **Log confirmation** (inside CT 200):
  ```bash
  grep -aiE "using hardware decode|final decoder: vaapi|hevc_vaapi|h264_vaapi" \
    "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Logs/Plex Media Server.log" | tail
  ```
  Want to see `h264_vaapi` and **no** `Failed to map output buffers`.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `hevc_vaapi: Failed to map output buffers: 24` → transcode exits 251 → client stalls at 0% | JasperLake HEVC hardware encoder is broken | **Disable HEVC video Encoding** in Plex (→ `h264_vaapi`) — §7 |
| `TPU: enabled, but no hardware decode accelerator found` | Device not visible / no perms in container, or Auto picked nothing | Check `ls -l /dev/dri` inside CT; ensure `plex` ∈ render/video/kvm; `pct reboot 200` |
| `vainfo` errors / `command not found` | VAAPI tools/driver missing | `apt install -y vainfo intel-media-va-driver-non-free` in CT |
| Dashboard shows `Transcode` (no `(hw)`) | HW accel toggles off, or HW init failing silently | Enable both HW-accel toggles; check log for the encoder error |
| Plays but buffers on 4K→4K | iGPU maxed re-encoding full 4K to a tiny client | Cap client Quality to 1080p (§10) |
| HDR content looks grey/washed-out | HDR tone-mapping disabled | Re-enable **Enable HDR tone mapping** (safe once HEVC encoding is off) |
| `explicitly configured lxc.apparmor.profile overrides … features:nesting` on CT start | An explicit `lxc.apparmor.profile: unconfined` line coexists with `features: nesting=1` | Delete the `lxc.apparmor.profile` line from `200.conf`; `features: nesting=1` applies the right profile and GPU still works (also drops you back to confined = more secure) |
| Scratch fills → `No space left on device` → `Conversion failed. The transcoder exited due to an error` | Transcode temp too small — default 20 GB root, or `/dev/shm` capped by RAM (overruns under 2 concurrent 4K + subtitle burn) | Point temp dir at `/mnt/unas/Media/plex-transcode` (9 TB); set **Max simultaneous transcodes = 2**; clear stale sessions left on the old path — §12 |

---

## 10. Known constraints & recommendations

- **JasperLake is a 1080p-transcode / 4K-direct-play chip.** H.264 HW transcode (incl.
  4K→1080p and HDR tone-mapping) is solid. Its HEVC *encoder* is not usable here — hence
  §7. HEVC/H.264 *decode* is fine.
- **Don't transcode 4K→4K.** For the Echo Show (tiny SDR screen) it's wasteful. Cap the
  app's streaming **Quality** to 1080p (or lower) to free GPU headroom and cut bandwidth.
- **Best UX for 4K remuxes:** watch them on a **4K direct-play client** (Apple TV / Nvidia
  Shield / 4K TV app) so the GPU never transcodes; keep a **1080p copy** (the Recyclarr
  1080p x264 profile already produces these) for casual/low-power clients like the Echo Show.
- **Optionally cap concurrent GPU transcodes** (Settings → Transcoder → *Maximum simultaneous
  GPU transcodes*) to 2–3 so a burst of streams doesn't overwhelm the little iGPU.

---

## 11. Status

| Piece | State |
| --- | --- |
| GPU passthrough into CT 200 | ✅ cgroup + mount in `200.conf`; render node accessible |
| `plex` device permissions | ✅ in `video` + `render` + `kvm` |
| VAAPI stack | ✅ iHD driver, HEVC/H.264 decode + H.264 encode verified via `vainfo` |
| Plex HW accel (decode + encode) | ✅ enabled |
| **HEVC encoding** | ⛔ **Disabled** (JasperLake `hevc_vaapi` broken) — forces `h264_vaapi` |
| HDR tone-mapping | ✅ enabled, works on `h264_vaapi` |
| Verified end-to-end | ✅ 4K HDR HEVC remux → Echo Show plays via hardware `h264_vaapi` + HDR tone-map |

---

## 12. Transcode temp directory (scratch location)

Live transcoding writes scratch segments to the **Transcoder temporary directory**
(Plex → Settings → Transcoder). The default is `…/Cache/Transcode` on the container's **20 GB
root disk** — far too small for 4K work, and the root can't be grown (thin pool is tight,
`arr-stack-storage-rebuild.md` §11). Two relocations were tried; the **NAS is the keeper**.

### Final setup (reliable): temp dir on the 9 TB UNAS share
```bash
mkdir -p /mnt/unas/Media/plex-transcode && chmod 777 /mnt/unas/Media/plex-transcode
```
- Plex → Settings → Transcoder → **Transcoder temporary directory = `/mnt/unas/Media/plex-transcode`**
- Plex → Settings → Transcoder → **Maximum simultaneous video transcodes = 2** (was
  Unlimited). JasperLake can't do more than ~2 anyway; this caps scratch **and** GPU load.
- **Why the NAS:** 9 TB — can't run dry, holds multiple concurrent streams *and* full Convert
  jobs, and it *frees* container RAM instead of consuming it. Squash-NFS is fine (Plex writes
  as anon; `chmod 777`). Trade-off is NFS latency vs RAM — negligible for HLS segments on the
  LAN, and worth it for "doesn't die mid-movie."

### What NOT to use, and why — the `/dev/shm` detour
`/dev/shm` (RAM tmpfs) is the *fastest* for live transcode and self-clears on reboot, but
it's **capped by container RAM (~3.8 GB) and died mid-movie** under real load:
```
[ass @ …] Error submitting a packet to the muxer: No space left on device
Plex Transcoder exit code … 228  →  "Conversion failed. The transcoder exited due to an error."
```
**Two concurrent 4K→1080p transcodes + a burned-in ASS subtitle** overran the 3.8 GB tmpfs.
Raising RAM only lifts the ceiling — the NAS removes it. Only viable if you cap to a **single**
stream, and even then a subtitle-burn 4K job can still overflow. Not worth it.

### Gotchas (apply to any temp dir)
- **Changing the temp dir does NOT clean the old location.** Stale sessions on the *previous*
  path linger until deleted by hand — this masqueraded as a setting "not working" (12 GB of
  orphaned sessions left on root after a switch). Clear once:
  ```bash
  systemctl stop plexmediaserver
  rm -rf "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Cache/Transcode/"*
  systemctl start plexmediaserver
  ```
- **Confirm the setting persisted** (Plex sometimes silently drops a temp dir it dislikes):
  ```bash
  grep -o 'TranscoderTempDirectory="[^"]*"' \
    "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml"
  ```
- **Convert / Optimize / Sync jobs** convert whole files and write big output — keep them off
  this small-disk server (Settings → Status → Conversions).
- **Root cause of the whole saga:** transcoding an 83 GB 4K remux for a weak client (Echo
  Show). A **1080p copy** direct-plays / lightly transcodes — no scratch pressure, no GPU
  strain. Keep 1080p versions of anything watched on weak clients.
