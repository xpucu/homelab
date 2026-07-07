# ✅ How to Mount a Synology NFS Share in Docker on Windows (WSL2)

This guide explains how to mount a Synology NAS folder via NFS inside a Docker container running on Windows (with Docker Desktop using WSL2).

---

## 📦 Prerequisites

- Synology NAS with NFS support
- Docker Desktop for Windows (uses WSL2 backend)
- A full WSL2 distro installed (e.g., Ubuntu)
- Local network access to the NAS

---

## 1. 🔧 Enable NFS on Synology NAS

1. Login to DSM
2. Go to **Control Panel → File Services → NFS**
3. ✅ Check **Enable NFS**
4. Click **Apply**

---

## 2. 🔐 Set NFS Permissions for the Shared Folder

1. Go to **Control Panel → Shared Folder**
2. Select the folder you want to mount (e.g., `calibre`)
3. Click **Edit → NFS Permissions**
4. Click **Create** and add:

| Field               | Value                      |
|---------------------|----------------------------|
| Hostname / IP       | `192.168.1.0/24` *(or your PC IP)* |
| Privilege           | `Read/Write`               |
| Squash              | `No mapping`               |
| Security            | `sys`                      |

5. Click **Apply** to save

---

## 3. 🛠 Install Ubuntu in WSL2 (if not already)

Open PowerShell or Command Prompt:

```powershell
wsl --install -d Ubuntu
```

Launch it:

```powershell
wsl -d Ubuntu
```

---

## 4. 🧰 Install NFS Client in WSL

Inside Ubuntu:

```bash
sudo apt update
sudo apt install nfs-common -y
```

---

## 5. ✅ Test Mount the NFS Share in WSL

```bash
sudo mkdir -p /mnt/calibre-test
sudo mount -t nfs 192.168.1.216:/volume1/calibre /mnt/calibre-test
ls /mnt/calibre-test
```

If you can list files, the NAS is configured correctly.

---

## 6. 🐳 Use NFS in Docker Compose

Update your `docker-compose.yaml`:

```yaml
version: "3.8"

volumes:
  calibre-nfs:
    driver: local
    driver_opts:
      type: "nfs"
      o: "addr=192.168.1.216,nolock,hard,rw"
      device: ":/volume1/calibre"

services:
  calibre-web-automated:
    image: crocodilestick/calibre-web-automated:latest
    container_name: calibre-web-automated
    ports:
      - "8083:8083"
    volumes:
      - calibre-nfs:/calibre-library
      - ./config:/config
      - ./ingest:/cwa-book-ingest
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=MST
    restart: unless-stopped
```

> Replace `192.168.1.216` and `/volume1/calibre` with your actual NAS IP and share path.

---

## 7. 🚀 Start Docker

```bash
docker compose up -d
```

Your container will now use the live-mounted NFS share.

---

## ✅ Quick Reference

| Task                    | Command / Path                                |
|-------------------------|-----------------------------------------------|
| Install WSL Ubuntu      | `wsl --install -d Ubuntu`                     |
| Install NFS client      | `sudo apt install nfs-common`                |
| Mount test (in WSL)     | `sudo mount -t nfs <NAS>:/share /mnt/test`   |
| Docker volume (compose) | `type: nfs`, `device: :/volume1/share`       |
| Synology permissions    | IP: `192.168.1.0/24`, Privilege: `Read/Write`|
