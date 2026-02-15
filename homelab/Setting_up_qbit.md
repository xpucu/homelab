# qBittorrent with L2TP/IPSec VPN on Proxmox

Complete guide for setting up qBittorrent in a Proxmox VM with L2TP/IPSec VPN connection to a MikroTik router, including kill switch protection.

## Table of Contents
- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [VM Setup](#vm-setup)
- [VPN Configuration](#vpn-configuration)
- [qBittorrent Installation](#qbittorrent-installation)
- [Kill Switch Setup](#kill-switch-setup)
- [NAS Mount Configuration](#nas-mount-configuration)
- [Testing & Verification](#testing--verification)
- [Troubleshooting](#troubleshooting)

## Overview

This setup creates a secure torrenting environment where:
- qBittorrent runs in an isolated VM
- All torrent traffic routes through L2TP/IPSec VPN
- Kill switch prevents IP leaks if VPN disconnects
- Downloads save to NAS via CIFS/SMB mount
- Web UI accessible on local network

**Security Features:**
- Firewall rules ensure only VPN traffic allowed
- qBittorrent bound to VPN interface only
- Auto-reconnect on VPN drops
- Network interface binding prevents leaks

## Prerequisites

### Hardware/Software
- Proxmox VE 8.x or newer
- Ubuntu 24.04 Server ISO downloaded to Proxmox
- 2GB RAM, 2 CPU cores, 20GB disk for VM
- Network access to NAS (Synology or similar)

### VPN Requirements
- L2TP/IPSec VPN server (MikroTik router)
- VPN server IP/hostname
- Pre-shared key (PSK)
- Username and password

### Network Information
- Local network subnet (e.g., 192.168.0.0/22)
- NAS IP address or hostname
- NAS share credentials

## VM Setup

### 1. Create Ubuntu VM in Proxmox

1. Download Ubuntu ISO to Proxmox:
   ```bash
   cd /var/lib/vz/template/iso
   wget https://releases.ubuntu.com/24.04.1/ubuntu-24.04.1-live-server-amd64.iso
   ```

2. Create VM via Proxmox Web UI:
   - General: Name = `qbittorrent`, VM ID = `103`
   - OS: Select Ubuntu 24.04 ISO
   - System: Default settings
   - Disks: 20GB, VirtIO SCSI
   - CPU: 2 cores
   - Memory: 2048MB
   - Network: Bridge = vmbr0, VirtIO

3. Start VM and install Ubuntu:
   - Follow standard Ubuntu Server installation
   - Configure network with DHCP
   - Create admin user
   - Install OpenSSH server (optional but recommended)

### 2. Initial System Configuration

SSH into the VM or use Proxmox console:

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install required packages
sudo apt install qbittorrent-nox xl2tpd strongswan network-manager-l2tp cifs-utils -y
```

## VPN Configuration

### 1. Configure IPSec (strongSwan)

Edit `/etc/ipsec.conf`:

```bash
sudo nano /etc/ipsec.conf
```

Content:
```
config setup
    charondebug="all"
    uniqueids=yes

conn myvpn
    type=transport
    authby=secret
    left=%defaultroute
    leftprotoport=17/1701
    right=VPN_SERVER_IP
    rightprotoport=17/1701
    ike=aes256-sha1-modp1024,aes128-sha1-modp1024,3des-sha1-modp1024!
    esp=aes256-sha1,aes128-sha1,3des-sha1!
    keyexchange=ikev1
    ikelifetime=8h
    lifetime=1h
    keyingtries=%forever
    auto=start
```

Replace `VPN_SERVER_IP` with your MikroTik's public IP.

### 2. Configure Pre-Shared Key

Edit `/etc/ipsec.secrets`:

```bash
sudo nano /etc/ipsec.secrets
```

Content:
```
: PSK "YOUR_PRESHARED_KEY"
```

Replace `YOUR_PRESHARED_KEY` with your actual PSK.

### 3. Configure L2TP

Edit `/etc/xl2tpd/xl2tpd.conf`:

```bash
sudo nano /etc/xl2tpd/xl2tpd.conf
```

Content:
```
[lac myvpn]
lns = VPN_SERVER_IP
ppp debug = yes
pppoptfile = /etc/ppp/options.l2tpd.client
length bit = yes
```

Replace `VPN_SERVER_IP` with your MikroTik's IP.

### 4. Configure PPP Options

Edit `/etc/ppp/options.l2tpd.client`:

```bash
sudo nano /etc/ppp/options.l2tpd.client
```

Content:
```
ipcp-accept-local
ipcp-accept-remote
refuse-eap
require-mschap-v2
noccp
noauth
idle 1800
mtu 1410
mru 1410
defaultroute
usepeerdns
connect-delay 5000
name YOUR_VPN_USERNAME
password YOUR_VPN_PASSWORD
```

Replace `YOUR_VPN_USERNAME` and `YOUR_VPN_PASSWORD` with your credentials.

### 5. Enable and Start VPN Services

```bash
# Enable services
sudo systemctl enable strongswan-starter
sudo systemctl enable xl2tpd

# Start services
sudo systemctl restart strongswan-starter
sudo systemctl restart xl2tpd

# Wait for IPSec to establish
sleep 5

# Connect L2TP tunnel
sudo bash -c 'echo "c myvpn" > /var/run/xl2tpd/l2tp-control'

# Wait for connection
sleep 10

# Verify VPN is connected
ip addr show ppp0
```

You should see a `ppp0` interface with an IP like `192.168.250.30`.

### 6. Verify VPN Connection

```bash
# Check your public IP through VPN
curl --interface ppp0 ifconfig.me
```

This should show your VPN server's public IP, not your real IP.

### 7. Auto-Connect VPN on Boot

Create `/etc/systemd/system/vpn-connect.service`:

```bash
sudo nano /etc/systemd/system/vpn-connect.service
```

Content:
```ini
[Unit]
Description=Connect L2TP VPN
After=network.target strongswan-starter.service xl2tpd.service
Requires=strongswan-starter.service xl2tpd.service

[Service]
Type=oneshot
ExecStartPre=/bin/sleep 10
ExecStart=/bin/bash -c 'echo "c myvpn" > /var/run/xl2tpd/l2tp-control'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

Enable the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable vpn-connect
```

## qBittorrent Installation

### 1. Create qBittorrent User

```bash
sudo useradd -r -s /bin/false qbittorrent
```

### 2. Create Required Directories

```bash
sudo mkdir -p /home/qbittorrent/.config/qBittorrent
sudo mkdir -p /home/qbittorrent/.cache/qBittorrent
sudo mkdir -p /home/qbittorrent/.local/share/qBittorrent
sudo chown -R qbittorrent:qbittorrent /home/qbittorrent
```

### 3. Create systemd Service

Create `/etc/systemd/system/qbittorrent.service`:

```bash
sudo nano /etc/systemd/system/qbittorrent.service
```

Content:
```ini
[Unit]
Description=qBittorrent Daemon
After=network.target vpn-connect.service
Requires=vpn-connect.service

[Service]
Type=simple
User=qbittorrent
Group=qbittorrent
ExecStart=/usr/bin/qbittorrent-nox --webui-port=8080
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

### 4. Enable and Start qBittorrent

```bash
sudo systemctl daemon-reload
sudo systemctl enable qbittorrent
sudo systemctl start qbittorrent
sudo systemctl status qbittorrent
```

### 5. Access Web UI

Find your VM's IP:
```bash
ip addr show ens18 | grep "inet "
```

Access qBittorrent at `http://VM_IP:8080`
- Default username: `admin`
- Default password: `adminadmin`

### 6. Configure qBittorrent to Use VPN Only

In the Web UI:

1. Go to **Settings → Advanced**
2. **Network Interface:** Select `ppp0` from dropdown
3. **Optional IP address to bind to:** Enter your VPN IP (e.g., `192.168.250.30`)
4. Click **Save**

This ensures qBittorrent ONLY uses the VPN interface.

## VPN Auto-Reconnect Monitor

The VPN connection can occasionally disconnect. This monitor automatically reconnects if the VPN drops.

### 1. Create VPN Monitor Script

Create `/usr/local/bin/vpn-monitor.sh`:

```bash
sudo nano /usr/local/bin/vpn-monitor.sh
```

Content:
```bash
#!/bin/bash
# VPN Monitor - Automatically reconnect if VPN drops

LOG_FILE="/var/log/vpn-monitor.log"
VPN_IP="VPN_SERVER_IP"  # Your MikroTik VPN server IP

log_message() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE"
}

# Check if ppp0 interface exists
if ! ip addr show ppp0 &>/dev/null; then
    log_message "VPN down (ppp0 not found). Reconnecting..."
    
    # Restart VPN services
    systemctl restart strongswan-starter
    sleep 3
    systemctl restart xl2tpd
    sleep 5
    
    # Trigger L2TP connection
    echo "c myvpn" > /var/run/xl2tpd/l2tp-control
    sleep 10
    
    # Verify connection
    if ip addr show ppp0 &>/dev/null; then
        log_message "VPN reconnected successfully"
        
        # Restart qBittorrent to rebind to ppp0
        systemctl restart qbittorrent
        log_message "qBittorrent restarted"
    else
        log_message "VPN reconnection failed"
    fi
else
    # ppp0 exists, verify it's actually working
    if ! ping -c 1 -W 2 -I ppp0 $VPN_IP &>/dev/null; then
        log_message "VPN interface exists but not working. Reconnecting..."
        
        # Disconnect and reconnect
        echo "d myvpn" > /var/run/xl2tpd/l2tp-control
        sleep 2
        
        systemctl restart strongswan-starter
        sleep 3
        systemctl restart xl2tpd
        sleep 5
        
        echo "c myvpn" > /var/run/xl2tpd/l2tp-control
        sleep 10
        
        if ip addr show ppp0 &>/dev/null; then
            log_message "VPN reconnected successfully"
            systemctl restart qbittorrent
        else
            log_message "VPN reconnection failed"
        fi
    fi
fi
```

Replace `VPN_SERVER_IP` with your MikroTik's public IP.

Make executable:
```bash
sudo chmod +x /usr/local/bin/vpn-monitor.sh
```

### 2. Create systemd Service

Create `/etc/systemd/system/vpn-monitor.service`:

```bash
sudo nano /etc/systemd/system/vpn-monitor.service
```

Content:
```ini
[Unit]
Description=VPN Connection Monitor
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/vpn-monitor.sh
```

### 3. Create systemd Timer

Create `/etc/systemd/system/vpn-monitor.timer`:

```bash
sudo nano /etc/systemd/system/vpn-monitor.timer
```

Content:
```ini
[Unit]
Description=VPN Monitor Timer
Requires=vpn-monitor.service

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
Unit=vpn-monitor.service

[Install]
WantedBy=timers.target
```

This runs the monitor every 2 minutes.

### 4. Enable and Start Monitor

```bash
sudo systemctl daemon-reload
sudo systemctl enable vpn-monitor.timer
sudo systemctl start vpn-monitor.timer

# Verify timer is active
sudo systemctl status vpn-monitor.timer
sudo systemctl list-timers vpn-monitor.timer
```

### 5. Test Auto-Reconnect

```bash
# Manually disconnect VPN
sudo bash -c 'echo "d myvpn" > /var/run/xl2tpd/l2tp-control'

# Wait 2 minutes for monitor to detect and reconnect
sleep 120

# Check if reconnected
ip addr show ppp0

# View monitor logs
sudo tail -20 /var/log/vpn-monitor.log
```

## Kill Switch Setup

The kill switch prevents any traffic if VPN disconnects.

### 1. Create Kill Switch Script

Create `/usr/local/bin/vpn-killswitch.sh`:

```bash
sudo nano /usr/local/bin/vpn-killswitch.sh
```

Content:
```bash
#!/bin/bash
# VPN Kill Switch - Only allow traffic through VPN and local network

# Flush existing rules
iptables -F
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT DROP

# Allow loopback
iptables -A INPUT -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT

# Allow VPN tunnel (ppp0)
iptables -A INPUT -i ppp0 -j ACCEPT
iptables -A OUTPUT -o ppp0 -j ACCEPT

# Allow local network access (adjust subnet to match your network)
iptables -A INPUT -i ens18 -s 192.168.0.0/22 -j ACCEPT
iptables -A OUTPUT -o ens18 -d 192.168.0.0/22 -j ACCEPT

# Allow CIFS/SMB to NAS (replace NAS_IP with your actual NAS IP)
iptables -A OUTPUT -o ens18 -d NAS_IP -p tcp --dport 445 -j ACCEPT
iptables -A INPUT -i ens18 -s NAS_IP -p tcp --sport 445 -j ACCEPT
iptables -A OUTPUT -o ens18 -d NAS_IP -p tcp --dport 139 -j ACCEPT
iptables -A INPUT -i ens18 -s NAS_IP -p tcp --sport 139 -j ACCEPT

# Allow VPN connection establishment to MikroTik
# Replace VPN_SERVER_IP with your actual server IP
iptables -A OUTPUT -o ens18 -d VPN_SERVER_IP -p udp --dport 500 -j ACCEPT
iptables -A INPUT -i ens18 -s VPN_SERVER_IP -p udp --sport 500 -j ACCEPT
iptables -A OUTPUT -o ens18 -d VPN_SERVER_IP -p udp --dport 4500 -j ACCEPT
iptables -A INPUT -i ens18 -s VPN_SERVER_IP -p udp --sport 4500 -j ACCEPT
iptables -A OUTPUT -o ens18 -d VPN_SERVER_IP -p udp --dport 1701 -j ACCEPT
iptables -A INPUT -i ens18 -s VPN_SERVER_IP -p udp --sport 1701 -j ACCEPT

# Allow established connections
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
```

**Important:** Replace `NAS_IP` with your NAS's actual IP address (e.g., 192.168.0.192) and `VPN_SERVER_IP` with your MikroTik's public IP.

Make executable:
```bash
sudo chmod +x /usr/local/bin/vpn-killswitch.sh
```

### 2. Create systemd Service for Kill Switch

Create `/etc/systemd/system/vpn-killswitch.service`:

```bash
sudo nano /etc/systemd/system/vpn-killswitch.service
```

Content:
```ini
[Unit]
Description=VPN Kill Switch
After=network.target
Before=qbittorrent.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/vpn-killswitch.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable vpn-killswitch
sudo systemctl start vpn-killswitch
```

## NAS Mount Configuration

### 1. Create Mount Point

```bash
sudo mkdir -p /mnt/downloads
```

### 2. Test Mount Manually

```bash
sudo mount -t cifs //NAS_IP/ShareName /mnt/downloads \
  -o username=NAS_USERNAME,password=NAS_PASSWORD,vers=3.0,uid=qbittorrent,gid=qbittorrent,file_mode=0777,dir_mode=0777
```

**Note:** Use IP address instead of hostname to avoid DNS resolution issues with the kill switch.

Verify:
```bash
ls -la /mnt/downloads
```

### 3. Add to fstab for Persistent Mount

Edit `/etc/fstab`:

```bash
sudo nano /etc/fstab
```

Add line (using IP address):
```
//NAS_IP/ShareName /mnt/downloads cifs username=NAS_USERNAME,password=NAS_PASSWORD,vers=3.0,uid=qbittorrent,gid=qbittorrent,file_mode=0777,dir_mode=0777 0 0
```

Replace:
- `NAS_IP` - Your NAS IP address (e.g., 192.168.0.192)
- `ShareName` - Your share name (e.g., Downloads)
- `NAS_USERNAME` - Your NAS username
- `NAS_PASSWORD` - Your NAS password

**Important:** Always use IP address, not hostname, to ensure mounts work properly with the firewall rules.

Test fstab mount:
```bash
sudo umount /mnt/downloads
sudo mount -a
ls -la /mnt/downloads
```

### 4. Configure qBittorrent Download Path

In qBittorrent Web UI:
- **Settings → Downloads**
- **Default Save Path:** `/mnt/downloads`
- Click **Save**

## Testing & Verification

### 1. Verify VPN Connection

```bash
# Check ppp0 interface exists
ip addr show ppp0

# Check public IP (should be VPN server IP)
curl --interface ppp0 ifconfig.me

# Check IPSec status
sudo ipsec status
```

### 2. Test Kill Switch

```bash
# Disconnect VPN
sudo bash -c 'echo "d myvpn" > /var/run/xl2tpd/l2tp-control'

# This should timeout (good - kill switch working)
curl ifconfig.me

# Reconnect VPN
sudo bash -c 'echo "c myvpn" > /var/run/xl2tpd/l2tp-control'
sleep 10

# This should work again
curl --interface ppp0 ifconfig.me
```

### 3. Verify qBittorrent Binding

In qBittorrent Web UI:
- Settings → Advanced
- Confirm **Network Interface** is set to `ppp0`
- Confirm **Optional IP address to bind to** shows your VPN IP

### 4. Test Download

Add a legal torrent (like Ubuntu ISO) and verify:
- Download starts successfully
- Files save to `/mnt/downloads`
- Download speed is reasonable

## Troubleshooting

### VPN Won't Connect

**Check IPSec status:**
```bash
sudo ipsec status
sudo journalctl -u strongswan-starter -n 50
```

**Check L2TP logs:**
```bash
sudo tail -50 /var/log/syslog | grep xl2tpd
```

**Restart VPN services:**
```bash
sudo systemctl restart strongswan-starter
sudo systemctl restart xl2tpd
sleep 5
sudo bash -c 'echo "c myvpn" > /var/run/xl2tpd/l2tp-control'
```

### qBittorrent Won't Start

**Check status and logs:**
```bash
sudo systemctl status qbittorrent
sudo journalctl -u qbittorrent -n 50
```

**Test manually:**
```bash
sudo -u qbittorrent /usr/bin/qbittorrent-nox --webui-port=8080
```

**Fix permissions if needed:**
```bash
sudo chown -R qbittorrent:qbittorrent /home/qbittorrent
```

### NAS Mount Fails

**Test connection:**
```bash
ping NAS_IP_OR_HOSTNAME
```

**Check CIFS mount manually:**
```bash
sudo mount -t cifs //NAS_IP/ShareName /mnt/downloads \
  -o username=USER,password=PASS,vers=3.0
```

**Check mount is active:**
```bash
mount | grep downloads
```

### VPN Connected but No Internet

**Check routing:**
```bash
ip route show
```

**Test VPN interface specifically:**
```bash
curl --interface ppp0 ifconfig.me
```

**Verify MikroTik NAT is configured:**
- Contact router admin to verify NAT/masquerading is enabled for VPN subnet

### Torrents Stuck at "Downloading metadata"

This usually means VPN disconnected or qBittorrent lost binding to VPN interface.

**Check VPN status:**
```bash
ip addr show ppp0
curl --interface ppp0 ifconfig.me
```

**If VPN is down:**
```bash
# Reconnect manually
sudo bash -c 'echo "c myvpn" > /var/run/xl2tpd/l2tp-control'
sleep 10

# Restart qBittorrent
sudo systemctl restart qbittorrent
```

**Check VPN monitor is running:**
```bash
sudo systemctl status vpn-monitor.timer
sudo tail -20 /var/log/vpn-monitor.log
```

**In qBittorrent Web UI:**
- Settings → Advanced → Network Interface: Verify `ppp0` is selected
- Settings → Advanced → Optional IP address to bind to: Verify VPN IP is set

### VPN Monitor Not Working

**Check timer status:**
```bash
sudo systemctl status vpn-monitor.timer
sudo systemctl list-timers
```

**Check logs:**
```bash
sudo tail -50 /var/log/vpn-monitor.log
```

**Manually trigger monitor:**
```bash
sudo /usr/local/bin/vpn-monitor.sh
```

**Restart timer:**
```bash
sudo systemctl restart vpn-monitor.timer
```

### Kill Switch Blocking Access

**Temporarily disable to test:**
```bash
sudo iptables -F
sudo iptables -P INPUT ACCEPT
sudo iptables -P FORWARD ACCEPT
sudo iptables -P OUTPUT ACCEPT
```

**Re-enable:**
```bash
sudo /usr/local/bin/vpn-killswitch.sh
```

## Service Management Commands

```bash
# VPN Services
sudo systemctl status strongswan-starter
sudo systemctl status xl2tpd
sudo systemctl status vpn-connect

# VPN Monitor
sudo systemctl status vpn-monitor.timer
sudo systemctl list-timers vpn-monitor.timer
sudo tail -f /var/log/vpn-monitor.log  # Watch monitor logs

# qBittorrent
sudo systemctl status qbittorrent
sudo systemctl restart qbittorrent
sudo systemctl stop qbittorrent

# Kill Switch
sudo systemctl status vpn-killswitch

# Connect/Disconnect VPN manually
sudo bash -c 'echo "c myvpn" > /var/run/xl2tpd/l2tp-control'  # Connect
sudo bash -c 'echo "d myvpn" > /var/run/xl2tpd/l2tp-control'  # Disconnect

# Check VPN status
sudo ipsec statusall
ip addr show ppp0
curl --interface ppp0 ifconfig.me  # Should show VPN server IP
```

## Security Notes

1. **Change default qBittorrent password** immediately after first login
2. **Use strong passwords** for NAS and VPN credentials
3. **Keep system updated:** `sudo apt update && sudo apt upgrade`
4. **Monitor logs** periodically for issues
5. **Test kill switch** regularly to ensure it's working
6. **Backup configurations** before making changes

## File Locations Reference

| Description | Path |
|-------------|------|
| IPSec config | `/etc/ipsec.conf` |
| IPSec secrets | `/etc/ipsec.secrets` |
| L2TP config | `/etc/xl2tpd/xl2tpd.conf` |
| PPP options | `/etc/ppp/options.l2tpd.client` |
| qBittorrent service | `/etc/systemd/system/qbittorrent.service` |
| VPN connect service | `/etc/systemd/system/vpn-connect.service` |
| VPN monitor script | `/usr/local/bin/vpn-monitor.sh` |
| VPN monitor service | `/etc/systemd/system/vpn-monitor.service` |
| VPN monitor timer | `/etc/systemd/system/vpn-monitor.timer` |
| VPN monitor log | `/var/log/vpn-monitor.log` |
| Kill switch script | `/usr/local/bin/vpn-killswitch.sh` |
| Kill switch service | `/etc/systemd/system/vpn-killswitch.service` |
| Mounts | `/etc/fstab` |
| qBittorrent config | `/home/qbittorrent/.config/qBittorrent/` |

## Credits

Setup created for secure torrenting with MikroTik L2TP/IPSec VPN on Proxmox VE.

## License

This documentation is provided as-is for personal use.
