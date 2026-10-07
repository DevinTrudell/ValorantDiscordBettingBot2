"""Pack the bot into valbet-bundle.tar.gz to copy to the server.

    python deploy/make_bundle.py

Stop the bot first, so the database is complete. The bundle contains .env (your Discord token and
tracker.gg key) and valbet.db (everyone's balances): keep it private and delete it after copying.
"""

import io
import os
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "valbet-bundle.tar.gz")
SKIP_DIRS = {".venv", "__pycache__", ".git"}
SKIP_FILES = {"valbet-bundle.tar.gz", "debug_profile.json", "debug_match.json", "ha-webhook-url.txt"}
UNIX_TEXT = (".sh", ".service")  # must have Linux line endings to run on the server

with tarfile.open(OUT, "w:gz") as tar:
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name in SKIP_FILES:
                continue
            path = os.path.join(folder, name)
            arc = os.path.relpath(path, ROOT).replace(os.sep, "/")
            if name.endswith(UNIX_TEXT):
                data = open(path, "rb").read().replace(b"\r\n", b"\n")
                info = tarfile.TarInfo(arc)
                info.size, info.mode = len(data), 0o755 if name.endswith(".sh") else 0o644
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.add(path, arcname=arc)

print(f"Wrote {os.path.relpath(OUT, os.getcwd())} ({os.path.getsize(OUT) // 1024} KB). "
      "It contains your tokens and balances: keep it private.")
