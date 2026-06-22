#!/usr/bin/env python3

import zipfile
from pathlib import Path
from datetime import datetime

# --- Configuration ---
BACKUP_ROOT_DIR = Path("/opt/hysbackup")
TIMESTAMP = datetime.now().strftime('%Y%m%d_%H%M%S')
BACKUP_FILENAME = BACKUP_ROOT_DIR / f"hysteria_backup_{TIMESTAMP}.zip"

FILES_TO_BACKUP = [
    Path("/etc/hysteria/ca.key"),
    Path("/etc/hysteria/ca.crt"),
    Path("/etc/hysteria/config.json"),
    Path("/etc/hysteria/.configs.env"),
    Path("/etc/hysteria/users_data.json"),
]

def create_backup():
    """Zips the JSON user store with config files."""
    try:
        BACKUP_ROOT_DIR.mkdir(parents=True, exist_ok=True)

        print(f"Creating backup archive: {BACKUP_FILENAME}")
        with zipfile.ZipFile(BACKUP_FILENAME, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file_path in FILES_TO_BACKUP:
                if file_path.exists() and file_path.is_file():
                    zipf.write(file_path, arcname=file_path.name)
                    print(f"  - Added {file_path.name}")
                else:
                    print(f"  - Warning: Skipping missing file {file_path}")

        print("\nBackup successfully created.")

    except Exception as e:
        print(f"\nBackup failed! An unexpected error occurred: {e}")

if __name__ == "__main__":
    create_backup()
