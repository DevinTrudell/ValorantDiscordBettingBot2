# Running the bot on Proxmox

The bot runs 24/7 in its own Proxmox container. The Overwolf app stays on your gaming PC and talks to
the bot over your home network.

```
Gaming PC (Windows)                 Proxmox (<proxmox-ip>)
  Valorant                           └─ LXC container  e.g. <container-ip>
  Overwolf + Valorant Bet Link  ───────→    the bot (service), valbet.db, Overwolf port :8787
```

## 1. Create the container (Proxmox web UI, https://<proxmox-ip>:8006)

1. Download a template: your storage → **CT Templates → Templates** → `debian-12-standard`.
2. **Create CT**:
   - Hostname `valbet`; set a root password.
   - Template: `debian-12-standard`.
   - Disk 4 GB, 1 CPU core, 512 MB RAM.
   - Network: **Static** IPv4, e.g. `<container-ip>/24`, gateway `<your-router-ip>` (pick a free address
     outside your router's DHCP range, or use DHCP and reserve the address in your router).
   - Tick **Start after created**.
3. Open the container's **Console** and check it's online: `ping -c 2 discord.com`.

## 2. Pack the bot on your PC

1. Stop the bot on your PC first, so the database is complete and two copies don't run at once.
2. From the bot folder:
   ```
   python deploy/make_bundle.py
   ```
   This makes `valbet-bundle.tar.gz`, which contains the code, your `.env` (Discord token, tracker.gg key)
   and `valbet.db` (balances, and the private key the Overwolf app uses). Keep it private.

## 3. Copy it to the container and install

From the bot folder on your PC (Windows has `scp` built in), using the container's IP:
```
scp valbet-bundle.tar.gz root@<container-ip>:/root/
```
If SSH login as root is refused, use the container's **Console** instead: run `apt install -y openssh-server`
and set `PermitRootLogin yes` in `/etc/ssh/sshd_config`, or copy the file another way.

Then in the container (Console or `ssh root@<container-ip>`):
```
mkdir -p /root/valbet && tar xzf /root/valbet-bundle.tar.gz -C /root/valbet
bash /root/valbet/deploy/setup.sh
rm /root/valbet-bundle.tar.gz
```
The script installs Python, puts the bot in `/opt/valbet`, and runs it as the `valbet` service: it starts
on boot and restarts by itself if it crashes. Delete `valbet-bundle.tar.gz` from your PC too.

## 4. Point the Overwolf app at the container

The **HomeAssistant Game Events** Overwolf app stays on the gaming PC (it has to see the game) and sends to the
container. The container answers to `valbet.local` (its hostname; `setup.sh` installs avahi for that). On the
gaming PC, from the bot folder:
```
python ha_link.py --host valbet
```
Paste the address it prints into the app's **Webhook URL** and save (same private key as before).

Don't forward port 8787 on your router: it only needs to work inside your home network.

## Day to day (in the container)

| To | Run |
|---|---|
| See the live log | `journalctl -u valbet -f` |
| Restart | `systemctl restart valbet` |
| Stop / start | `systemctl stop valbet` / `systemctl start valbet` |
| Update the code | Copy a new bundle over, then `bash deploy/setup.sh` again (keeps the server's `.env` and database) |
| Back up balances | `cp /opt/valbet/valbet.db /root/valbet-backup.db` |

Only run one copy of the bot at a time (don't also start it on your PC).
