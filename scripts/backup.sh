#!/usr/bin/env bash
# Consistent backup of AEON's journal(s). Safe while the agent is running: it uses SQLite's online backup API, so
# the copy is a single point in time even though the journal is being written (a plain `cp` of a WAL database is not).
#
#   ./scripts/backup.sh [dest-dir]        default ./backups ; keeps 14 days (KEEP_DAYS=30 ./scripts/backup.sh)
#
# The destination must be writable by uid 1000 (the container user; also the first user on Debian/Ubuntu).
# If it is not:   sudo chown 1000 backups
# Schedule it (crontab -e):   17 */6 * * *  cd /opt/aeon && ./scripts/backup.sh >> backups/backup.log 2>&1
# ...and copy the folder OFF the server (rclone / rsync / scp): a backup on the same disk dies with the disk.
set -euo pipefail
cd "$(dirname "$0")/.."

dest="${1:-backups}"
keep="${KEEP_DAYS:-14}"
mkdir -p "$dest"
dest="$(cd "$dest" && pwd)"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"

docker compose run --rm --no-deps -T -v "$dest:/out" --entrypoint python aeon - "$stamp" <<'PY'
import glob, os, sqlite3, sys

stamp = sys.argv[1]
journals = sorted(glob.glob("/data/aeon-*.db"))
if not journals:
    sys.exit("no journal found in /data (has AEON run yet?)")
for src in journals:
    name = os.path.basename(src)[:-3]
    dst = f"/out/{name}-{stamp}.db"
    source, target = sqlite3.connect(src), sqlite3.connect(dst)
    with target:
        source.backup(target)
    verdict = target.execute("PRAGMA integrity_check").fetchone()[0]
    source.close()
    target.close()
    if verdict != "ok":
        sys.exit(f"{dst}: integrity check failed ({verdict})")
    print(f"backed up {src} -> {dst}")
PY

for f in "$dest"/aeon-*-"$stamp".db; do
  gzip -f "$f"
done
find "$dest" -name 'aeon-*.db.gz' -mtime "+$keep" -delete
echo "done: $(find "$dest" -name 'aeon-*.db.gz' | wc -l) backup(s) kept in $dest"
