"""Automated project backup utility.

Usage:
  python backup.py "Optional description"
  python backup.py --list
  python backup.py --restore backups/backup_YYYYMMDD_HHMMSS.zip
"""
import os
import sys
import json
import zipfile
import datetime
import shutil

ROOT = os.path.abspath(os.path.dirname(__file__))
BACKUP_DIR = os.path.join(ROOT, "backups")

EXCLUDE_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", "backups"}
EXCLUDE_EXTS = {".pyc", ".log", ".tmp"}


def create_backup(description: str = "Auto backup before major change") -> str:
    """Create a full zip snapshot of source code, configs, and database."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"backup_{ts}.zip"
    zip_path = os.path.join(BACKUP_DIR, zip_name)

    files_backed_up = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Walk and zip project files
        for dirpath, dirnames, filenames in os.walk(ROOT):
            # Prune excluded directories in-place
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            
            rel_dir = os.path.relpath(dirpath, ROOT)
            if rel_dir == ".":
                rel_dir = ""

            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext in EXCLUDE_EXTS:
                    continue
                full_path = os.path.join(dirpath, fn)
                arc_name = os.path.join(rel_dir, fn) if rel_dir else fn
                zf.write(full_path, arc_name)
                files_backed_up += 1

        # Add manifest
        manifest = {
            "timestamp": ts,
            "created_at": datetime.datetime.now().isoformat(),
            "description": description,
            "files_count": files_backed_up
        }
        zf.writestr("_backup_manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))

    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"[BACKUP SUCCESS] Created: backups/{zip_name} ({size_mb:.2f} MB, {files_backed_up} files)")
    print(f"Description: {description}")
    return zip_path


def list_backups():
    if not os.path.exists(BACKUP_DIR):
        print("No backups directory found.")
        return []
    files = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith("backup_") and f.endswith(".zip")], reverse=True)
    if not files:
        print("No backups found.")
        return []
    print(f"Found {len(files)} backup(s):")
    for f in files:
        p = os.path.join(BACKUP_DIR, f)
        size_mb = os.path.getsize(p) / (1024 * 1024)
        desc = "No description"
        try:
            with zipfile.ZipFile(p, "r") as zf:
                if "_backup_manifest.json" in zf.namelist():
                    data = json.loads(zf.read("_backup_manifest.json"))
                    desc = data.get("description", desc)
        except Exception:
            pass
        print(f" - {f} ({size_mb:.2f} MB) -> {desc}")
    return files


def restore_backup(zip_path: str):
    if not os.path.isabs(zip_path):
        zip_path = os.path.join(ROOT, zip_path)
    if not os.path.exists(zip_path):
        print(f"Error: backup file not found: {zip_path}")
        return False
    print(f"Restoring project from {os.path.basename(zip_path)}...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            if member.filename == "_backup_manifest.json":
                continue
            zf.extract(member, ROOT)
    print("[RESTORE SUCCESS] Project restored to state from backup.")
    return True


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--list":
        list_backups()
    elif len(sys.argv) > 2 and sys.argv[1] == "--restore":
        restore_backup(sys.argv[2])
    else:
        desc = sys.argv[1] if len(sys.argv) > 1 else "Checkpoint before changes"
        create_backup(desc)
