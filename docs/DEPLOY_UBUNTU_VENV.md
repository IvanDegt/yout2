# Deploy on Ubuntu 22.04 (venv + systemd + Gunicorn)

Target: one server, access by IP, no Docker. The app listens on **port 5002** (same as local `app.py`).

## Prerequisites

- Ubuntu 22.04 LTS with `sudo`
- Outbound HTTPS (OpenAI API)
- For YouTube transcription: `ffmpeg` on the server; optional `node` if `yt-dlp` needs it for some videos

## 1) System packages

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip ffmpeg ufw
```

Optional (helps some `yt-dlp` flows):

```bash
sudo apt install -y nodejs
```

## 2) Dedicated user (recommended)

```bash
sudo adduser --disabled-password --gecos "" rewrite
sudo mkdir -p /opt/rewrite-app
sudo chown rewrite:rewrite /opt/rewrite-app
```

## 3) Clone the repo

As `rewrite`:

```bash
sudo -u rewrite -i
cd /opt
git clone https://github.com/IvanDegt/yout2.git rewrite-app
cd rewrite-app
```

If the repo is private, use SSH deploy key or HTTPS with a fine-scoped PAT (never commit secrets).

## 4) Python venv and dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate
```

## 5) Environment file

```bash
cp .env.example .env
chmod 600 .env
nano .env   # or vim
```

Required:

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4.1
```

`projects/` is created at runtime; ensure the service user can write the app directory (already `rewrite` owns `/opt/rewrite-app`).

## 6) Firewall (UFW)

Allow SSH first, then the app port:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 5002/tcp
sudo ufw enable
sudo ufw status
```

Open in browser: `http://YOUR_SERVER_IP:5002/`

## 7) systemd service (production)

From the repo (as root or with sudo):

```bash
sudo cp /opt/rewrite-app/deploy/rewrite-master.service.example /etc/systemd/system/rewrite-master.service
# Edit paths/User if you did not use /opt/rewrite-app and user rewrite
sudo nano /etc/systemd/system/rewrite-master.service

sudo systemctl daemon-reload
sudo systemctl enable --now rewrite-master
sudo systemctl status rewrite-master
```

Logs:

```bash
journalctl -u rewrite-master -f
```

### Why `workers 1`

Streaming NDJSON (`/run`, transcribe) is safer with **one worker** so in-memory state and long responses are not split across processes. Threads still help concurrent light requests.

## 8) Updates after `git pull`

```bash
sudo -u rewrite -i
cd /opt/rewrite-app
git pull
source venv/bin/activate
pip install -r requirements.txt
deactivate
sudo systemctl restart rewrite-master
```

## 9) Security checklist (IP-only public deploy)

- Prefer **SSH keys**, disable password SSH if possible (`/etc/ssh/sshd_config`).
- Restrict **port 5002** to your office IP in UFW if you can (`ufw allow from x.x.x.x to any port 5002`).
- Never commit `.env`; keep `chmod 600 .env`.
- If the box is on the public internet, consider putting **Caddy/nginx** in front with basic auth or VPN-only access.

## 10) Troubleshooting

| Symptom | Check |
|--------|--------|
| 502 / connection refused | `systemctl status rewrite-master`, firewall, bind `0.0.0.0:5002` |
| OpenAI errors | `.env` key, outbound DNS/HTTPS |
| Transcribe fails | `ffmpeg`, `yt-dlp`, cookies/browser note in app docs |
| Permission errors on projects | owner of `/opt/rewrite-app` = service `User` |

## Related files

- `deploy/rewrite-master.service.example` — systemd unit
- `README.md` — local dev and doc map
