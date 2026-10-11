"""Nightly backup of every server's database (valbet.db, valbet-<server id>.db), safe while the bot runs.

    python deploy/backup.py [data dir] [backup dir]     (defaults: /opt/valbet  /var/backups/valbet)

Uses SQLite's own backup API, so a copy taken mid-write is still consistent (a plain cp of a WAL database can
be broken). Keeps KEEP_DAYS days. Run by valbet-backup.timer; see DEPLOY.md.
"""

import glob
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

KEEP_DAYS = int(os.getenv("BACKUP_KEEP_DAYS", "14"))
SRC = sys.argv[1] if len(sys.argv) > 1 else "/opt/valbet"
DEST = sys.argv[2] if len(sys.argv) > 2 else "/var/backups/valbet"

os.makedirs(DEST, exist_ok=True)
stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
done = 0
for path in sorted(glob.glob(os.path.join(SRC, "valbet*.db"))):
    out = os.path.join(DEST, f"{os.path.basename(path)}.{stamp}")
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    dst = sqlite3.connect(out)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    done += 1

cutoff = time.time() - KEEP_DAYS * 86400
old = [p for p in glob.glob(os.path.join(DEST, "valbet*.db.*")) if os.path.getmtime(p) < cutoff]
for p in old:
    os.remove(p)
print(f"Backed up {done} database(s) to {DEST}; removed {len(old)} older than {KEEP_DAYS} days.")
