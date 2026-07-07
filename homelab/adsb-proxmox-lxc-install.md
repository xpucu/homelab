# ADS-B Receiver in a Proxmox LXC (RTL-SDR Blog V4)

How I set up a PiAware / dump1090-fa ADS-B receiver inside an unprivileged-free
LXC container on Proxmox VE, passing through an RTL-SDR Blog V4 USB dongle.

- **Host:** Proxmox VE node `pve`
- **Container:** CT 110, hostname `adsb`, Debian 13 (Trixie)
- **Dongle:** RTL-SDR Blog V4 (R828D / RTL2832U), USB ID `0bda:2838`
- **Install scripts:** https://github.com/abcd567a/piaware-ubuntu-debian-amd64
  (huge thanks to **abcd567** — see [Credits](#credits) below)

---

## Why the V4 needs special handling

The RTL-SDR Blog V4 uses an **R828D** tuner (not the older R820T2). The
`librtlsdr` shipped in distro repos is too old to drive it correctly, so the
**RTL-SDR Blog fork of the driver must be built from source**. This later
causes a build conflict with dump1090-fa (covered below).

---

## Part 1 — Proxmox host (`pve`)

### 1.1 Blacklist the kernel DVB modules

The host kernel will otherwise grab the dongle as a TV tuner, making it
unavailable to the container.

`/etc/modprobe.d/blacklist-rtlsdr.conf`:

```
blacklist dvb_core
blacklist dvb_usb_rtl2832u
blacklist dvb_usb_rtl28xxu
blacklist dvb_usb_v2
blacklist r820t
blacklist rtl2830
blacklist rtl2832
blacklist rtl2832_sdr
blacklist rtl2838

install dvb_core /bin/false
install dvb_usb_rtl2832u /bin/false
install dvb_usb_rtl28xxu /bin/false
install dvb_usb_v2 /bin/false
install r820t /bin/false
install rtl2830 /bin/false
install rtl2832 /bin/false
install rtl2832_sdr /bin/false
install rtl2838 /bin/false
```

### 1.2 Make the blacklist actually take effect

A reboot alone was **not** enough — the modules were cached in the initramfs
and kept loading. The fix was to rebuild the initramfs:

```bash
update-initramfs -u
reboot
```

After reboot, confirm the modules are gone (output must be empty):

```bash
lsmod | grep -iE 'dvb|rtl2832|r820'
```

### 1.3 Confirm the dongle is visible

```bash
lsusb | grep -i realtek
# Bus 001 Device 002: ID 0bda:2838 Realtek Semiconductor Corp. RTL2838 DVB-T
```

Note the **Bus** number (here: `001`). That whole bus gets passed to the
container.

### 1.4 udev rule for container access

Makes the device world read/writable so the container can open it.

`/etc/udev/rules.d/99-ads-receiver.rules`:

```
ACTION=="add", SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", MODE="0666"
```

Apply it:

```bash
udevadm control --reload-rules && udevadm trigger --action=add
ls -l /dev/bus/usb/001/002    # want crw-rw-rw-
```

> The device number (`002`) changes on every replug. That's why we pass the
> **whole bus** (`001`), not a single device node.

---

## Part 2 — Create the LXC

Created via the Proxmox web GUI:

- **CT ID:** 110, **Hostname:** `adsb`
- **Privileged** container (unchecked "Unprivileged container") — simplifies
  USB passthrough
- **Template:** `debian-13-standard_13.1-2_amd64`
- **Disk:** 8 GB, **Cores:** 2, **Memory:** 1024 MB, **Swap:** 512 MB
- **Network:** DHCP

### 2.1 Edit the container config

`/etc/pve/lxc/110.conf` — added USB passthrough, set MTU to 1500, enabled
nesting:

```
arch: amd64
cores: 2
hostname: adsb
memory: 1024
net0: name=eth0,bridge=vmbr0,firewall=1,hwaddr=BC:24:11:XX:XX:XX,ip=dhcp,type=veth,mtu=1500
ostype: debian
rootfs: local-lvm:vm-110-disk-0,size=8G
swap: 512
lxc.cgroup2.devices.allow: c 189:* rwm
lxc.mount.entry: /dev/bus/usb/001 dev/bus/usb/001 none bind,optional,create=dir
features: nesting=1
```

Key additions:
- `mtu=1500` on `net0` — **required**, or mlat-client throws
  `pack_into requires a buffer` errors (Proxmox bridges can negotiate MTU 9000).
- `lxc.cgroup2.devices.allow: c 189:* rwm` — allows USB character devices (major 189).
- `lxc.mount.entry` — bind-mounts USB bus 001 into the container.
- `features: nesting=1` — quiets the systemd 257 warning on Debian 13.

### 2.2 Start and enter

```bash
pct start 110
pct enter 110
```

---

## Part 3 — Inside the container

### 3.1 Base packages

```bash
apt update && apt install -y usbutils wget sudo
lsusb | grep -i realtek        # confirm dongle visible inside the container
```

### 3.2 Fix locale warnings (optional but cleans up output)

```bash
apt install -y locales
sed -i 's/# en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen
locale-gen
update-locale LANG=en_US.UTF-8
```

### 3.3 Build the RTL-SDR Blog V4 driver from source

```bash
apt install -y libusb-1.0-0-dev git cmake pkg-config build-essential
git clone https://github.com/rtlsdrblog/rtl-sdr-blog
cd rtl-sdr-blog
mkdir build && cd build
cmake ../ -DINSTALL_UDEV_RULES=ON
make && make install
ldconfig
```

### 3.4 Verify the dongle and V4 detection

```bash
echo 'export PATH=$PATH:/usr/local/bin' >> ~/.bashrc && export PATH=$PATH:/usr/local/bin
hash -r
rtl_test       # Ctrl+C after a few seconds
```

Expected output includes:

```
Found Rafael Micro R828D tuner
RTL-SDR Blog V4 Detected
```

and **no** `usb_claim_interface error -6` (that error means a DVB module is
still loaded on the host).

---

## Part 4 — Install dump1090-fa (and the build conflict)

### The problem

The install script builds dump1090-fa as a `.deb`. During the build,
`dpkg-shlibdeps` fails because dump1090-fa links against the **source-built**
`/usr/local/lib/librtlsdr.so.0`, which has no Debian package metadata:

```
dpkg-shlibdeps: error: no dependency information found for /usr/local/lib/librtlsdr.so.0
```

### The fix that worked

Temporarily move the source-built driver out of the linker path so the build
resolves against the apt-packaged `librtlsdr` (which has proper metadata), then
restore the V4 driver afterward. The V4 driver is what loads at runtime either
way, because `/usr/local/lib` takes precedence once restored.

```bash
# Make sure the packaged dev lib is present (provides build-time metadata)
apt install -y librtlsdr-dev rtl-sdr

# Hide the source-built driver during the build
mkdir -p /root/v4-driver-backup
mv /usr/local/lib/librtlsdr* /root/v4-driver-backup/
ldconfig
ldconfig -p | grep librtlsdr     # should show ONLY /lib/x86_64-linux-gnu paths
```

Run the install:

```bash
sudo bash -c "$(wget -O - https://raw.githubusercontent.com/abcd567a/piaware-ubuntu-debian-amd64/master/install-dump1090-fa.sh)"
```

Restore the V4 driver so it wins at runtime:

```bash
mv /root/v4-driver-backup/librtlsdr* /usr/local/lib/
ldconfig
ldconfig -p | grep librtlsdr     # /usr/local/lib should now be listed first
```

Reboot the container:

```bash
reboot
```

---

## Part 5 — Verify and configure

### 5.1 Confirm the service is running

```bash
systemctl status dump1090-fa --no-pager
```

Should show `active (running)`, plus in the log:

```
rtlsdr: using device #0: Generic RTL2832U OEM (RTLSDRBlog, Blog V4, SN 00000001)
Found Rafael Micro R828D tuner
RTL-SDR Blog V4 Detected
```

### 5.2 Confirm live aircraft

```bash
cat /run/dump1090-fa/aircraft.json | head -c 500; echo
```

Entries with `hex` codes and `lat`/`lon` = it's decoding real aircraft.

### 5.3 Set receiver location (fixes the map defaulting to Monaco)

Edit `/etc/default/dump1090-fa` and set:

```
RECEIVER_LAT=<your-latitude>
RECEIVER_LON=<your-longitude>
```

Then:

```bash
systemctl restart dump1090-fa
cat /run/dump1090-fa/receiver.json     # confirm lat/lon present
```

> **Note:** The SkyAware web map caches the last map center/zoom in browser
> local storage. After setting the location you must **hard refresh**
> (Ctrl+Shift+R) or open an incognito window, or it stays stuck on Monaco even
> though the backend is correct.

### 5.4 Web map

```bash
hostname -I        # get container IP
```

SkyAware map: `http://<container-ip>:8080`

---

## Gotchas summary

| Issue | Cause | Fix |
|-------|-------|-----|
| Modules reload after reboot | Blacklist cached in initramfs | `update-initramfs -u` then reboot |
| `rtl_test` command not found | `/usr/local/bin` not in PATH | add to PATH + `hash -r` |
| `usb_claim_interface error -6` | DVB module still loaded on host | recheck blacklist / `lsmod` |
| dump1090-fa build fails on `dpkg-shlibdeps` | Source-built librtlsdr has no pkg metadata | temporarily move it out, build, restore |
| Map stuck on Monaco | Browser local-storage cache | hard refresh / incognito |
| mlat buffer errors (later, with piaware) | MTU negotiated to 9000 | `mtu=1500` in LXC net config |
| Claim page loads blank / stuck | FlightAware auto-detect widget is broken | claim by feeder ID URL (see 6.2) |
| MLAT stuck "not synchronized / lat-lon not set" | feeder hadn't picked up the site location yet | `systemctl restart piaware` after setting location on site page |

---

## Part 6 — FlightAware (piaware)

### 6.1 Install piaware

```bash
sudo bash -c "$(wget -O - https://raw.githubusercontent.com/abcd567a/piaware-ubuntu-debian-amd64/master/install-piaware.sh)"
```

> piaware does **not** link librtlsdr directly, so it builds cleanly — no
> `dpkg-shlibdeps` move-out dance needed (unlike dump1090-fa).

Confirm it's running and feeding:

```bash
systemctl status piaware --no-pager
piaware-status
```

Look for:
```
logged in to FlightAware as user guest
piaware has successfully sent several msgs to FlightAware!
piaware is connected to FlightAware.
Your feeder ID is <your-feeder-id>
```

"user guest" = feeding but not yet linked to your account. That's what claiming fixes.

### 6.2 Claim the site

The public claim page (`https://flightaware.com/adsb/piaware/claim`) **renders
blank / stays stuck on "Loading"** — its auto-detect widget is broken. Bypass it
by claiming directly with the feeder ID in the URL (while logged in):

```
https://flightaware.com/adsb/piaware/claim/<your-feeder-id>
```

e.g. `https://flightaware.com/adsb/piaware/claim/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`

This linked the feeder to the account immediately. Feeding earns a free
FlightAware Enterprise account.

Stats page (live a few minutes after claiming):
`https://flightaware.com/adsb/stats/user/<username>`

### 6.3 Enable MLAT and auto-updates

```bash
piaware-config allow-mlat yes
piaware-config mlat-results yes
piaware-config allow-auto-updates yes
piaware-config allow-manual-updates yes
systemctl restart piaware
```

### 6.4 MLAT location + getting it to sync

MLAT needs a **precise antenna location** (within a few meters) or it won't
start. Set it on the **site's config page** on FlightAware (lat, lon, and
antenna height above ground).

> **Gotcha that bit us:** after setting the location, the site page still showed
> "MLAT disabled / not synchronized / Latitude Longitude not set" even though the
> lat/lon were visibly saved. The fix was simply **`systemctl restart piaware`** —
> the feeder picks up the location on reconnect and MLAT then synchronizes.

Watch MLAT come up:

```bash
journalctl -u piaware -f
# look for: mlat-client connected / synchronized with N nearby receivers
```

Confirm with:

```bash
piaware-status     # fa-mlat-client should now show "running"
```

> **Location consistency note:** the MLAT location set on the site can drift from
> the dump1090 map location set in 5.3 if you enter them separately — they should
> match the real antenna position. Worth reconciling — set dump1090's
> `RECEIVER_LAT/LON` to the true antenna location.

---

## Part 7 — piaware-web (FlightAware-branded status page)

```bash
sudo bash -c "$(wget -O - https://raw.githubusercontent.com/abcd567a/piaware-ubuntu-debian-amd64/master/install-piaware-web.sh)"
```

Builds cleanly (no librtlsdr link, so no `dpkg-shlibdeps` workaround needed).
Served on **port 80**:

```
http://<container-ip>
```

This is separate from the SkyAware map on `:8080` — piaware-web is the
FlightAware-branded status/overview page.

### Web endpoints summary

| URL | What |
|-----|------|
| `http://<container-ip>:8080` | SkyAware map (dump1090-fa) |
| `http://<container-ip>` | piaware-web status page (port 80) |
| `http://<container-ip>:8080/data/aircraft.json` | raw aircraft JSON (for scripts/integrations) |
| `http://<container-ip>:8754` | fr24feed status page (Flightradar24) |

---

## Part 8 — Flightradar24 (fr24feed) — second feed from the same dongle

You can feed FR24 **and** FlightAware from the same dump1090-fa simultaneously.
fr24feed runs as its own service and pulls from dump1090-fa over the network
(AVR/Beast on `127.0.0.1:30002`) — it does **not** touch the SDR directly.

Feeding earns a free FR24 Contributor/Business plan once actively feeding
(~24–48h; if it doesn't flip, log out and back into the FR24 account).

### 8.1 Pre-step

Create a free FR24 account first at flightradar24.com (Basic/feeder plan).

### 8.2 Trixie fix — installer expects `/etc/apt/sources.list`

The installer assumes the old single sources file, which doesn't exist on
Debian 13 (sources moved to `.sources` format under
`/etc/apt/sources.list.d/`). It fails with:

```
mv: cannot stat '/etc/apt/sources.list': No such file or directory
```

Fix — create the empty file it wants, then run the installer:

```bash
touch /etc/apt/sources.list
wget -qO- https://fr24.com/install.sh | sudo bash -s
```

### 8.3 Signup wizard answers

- **Email:** your FR24 account email
- **Sharing key:** leave blank (first feed)
- **MLAT:** **no** — FlightAware already does MLAT; running it from two feeders
  against one receiver causes conflicts. FR24's own docs say to disable MLAT when
  feeding multiple networks.
- **Lat/Lon:** your antenna coords
- **Altitude:** in feet, **no commas** (see gotcha below)
- **Autoconfig / reuse existing dump1090:** **yes**

On success you get a **sharing key** and **radar ID** (e.g. `T-XXXXnn`) — save both.

Check status:

```bash
fr24feed-status      # want: FR24 Link: connected, Receiver: connected
```

### 8.4 Gotchas

- **Comma in altitude:** entering `1,506` is parsed as `1` ft. Enter `1506`.
  Altitude is server-side (tied to the radar ID), only used for FR24 coverage
  modeling — it is **not** in `/etc/fr24feed.ini` and doesn't affect the feed.
- **The "My data sharing" account page is read-only** — no antenna-location
  editor there. To re-submit location you must re-run `fr24feed --signup`
  (stop the service first: `systemctl stop fr24feed`) and enter the **existing**
  sharing key to keep the same radar ID. Note: the signup verification step was
  hitting `SSL connect error / empty response` from FR24's endpoint — flaky and
  not worth chasing for a cosmetic altitude fix when the feed already works.
- **Installing FR24 can knock FlightAware MLAT loose** ("not synchronized / lat
  lon not set"). Fix: `systemctl restart piaware`, wait ~30s, recheck
  `piaware-status | grep -i mlat`. If it keeps reverting, set piaware's location
  explicitly so it doesn't depend on the shared receiver config.

`/etc/fr24feed.ini` (autoconfig result):

```
receiver="avr-tcp"
fr24key="<your-key>"
host="127.0.0.1:30002"
bs="no"
raw="no"
mlat="no"
mlat-without-gps="no"
```

---

## Part 9 — ADSB-Exchange (fourth feed, the unfiltered network)

ADSB-Exchange does **not** honor block lists — military and otherwise-filtered
aircraft show up here. No free premium-account perk (community/donation funded);
the payoff is unfiltered data + a personal stats/coverage page. Feeds from the
same dump1090-fa (Beast port 30005).

> **Difference from FR24:** ADSBx runs its **own MLAT client**
> (`adsbexchange-mlat`) and *wants* MLAT enabled. This means two MLAT clients run
> against the same receiver — FlightAware's and ADSBx's. That's fine: they feed
> **different** networks. On this setup FlightAware MLAT held after install
> (the earlier MLAT drops were FR24-install activity, not an ongoing conflict).
> Still — check `piaware-status | grep -i mlat` afterward and restart piaware if
> it dropped.

### 9.1 Install

`curl` isn't in the container by default:

```bash
apt install -y curl
```

Feed client (compiles a readsb-based client — takes a few min):

```bash
curl -L -o /tmp/axfeed.sh https://adsbexchange.com/feed.sh
sudo bash /tmp/axfeed.sh
```

Wizard asks for: a **feeder name**, and **lat / lon / altitude**.
**Altitude is in METERS here** (not feet like FR24). ~1506 ft ≈ **459 m**.
It auto-detects dump1090-fa on port 30005.

Stats package (stats page + feeder URL):

```bash
curl -L -o /tmp/axstats.sh https://adsbexchange.com/stats.sh
sudo bash /tmp/axstats.sh
```

### 9.2 Verify

```bash
systemctl status adsbexchange-feed --no-pager
systemctl status adsbexchange-mlat --no-pager
```

Feed service should show:
```
Beast TCP input: Connection established: 127.0.0.1 port 30005
BeastReduce TCP output: Connection established: feed1.adsbexchange...30004
```

> **MLAT startup race (harmless):** the mlat service may log
> `[Errno 111] refused` for `127.0.0.1:30154` if it starts before the feed
> service opens that port. It resolves once the feed service is up
> (`connection...established`). `0 positions/minute` at first is normal — MLAT
> needs overlapping coverage with nearby feeders to compute positions.

Feeder UUID is shown during install (e.g. `xxxxxxxx-...`). Check status after
~5 min:
- `https://adsbexchange.com/myip/`
- `https://map.adsbexchange.com/sync/`

Optional local tar1090-style web interface:

```bash
sudo bash /usr/local/share/adsbexchange/git/install-or-update-interface.sh
```

---

## Final state — feeding four networks from one dongle

| Network | What you get | MLAT |
|---------|--------------|------|
| SkyAware (local) | local map at `:8080` | n/a |
| FlightAware | feed + free Enterprise account | yes (fa-mlat-client) |
| Flightradar24 | feed + free Contributor/Business | no (FA does it) |
| ADSB-Exchange | unfiltered data (military visible) + stats page | yes (adsbexchange-mlat) |

---

## Still TODO

- [ ] Reconcile the three locations (home zone / MLAT site / dump1090 map) to the
      true antenna position
- [ ] **Home Assistant integration** — planes overhead / closest aircraft cards +
      military alerting (feed confirmed good at
      `http://<container-ip>:8080/data/aircraft.json`)

---

## Credits

Huge thank-you to **abcd567** ([@abcd567a on GitHub](https://github.com/abcd567a),
active on the [FlightAware discussions forum](https://discussions.flightaware.com/))
for the [`piaware-ubuntu-debian-amd64`](https://github.com/abcd567a/piaware-ubuntu-debian-amd64)
install scripts. They do the heavy lifting of building dump1090-fa, piaware, and
piaware-web on plain Debian/Ubuntu (amd64) — this whole LXC setup is built on top of
that work. Their long-running forum guides and troubleshooting posts were also the
reference for much of the RTL-SDR Blog V4 handling documented here.
