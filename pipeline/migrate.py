"""One-time migration: move single-user data into user_kerbel/ directory."""
from __future__ import annotations

import shutil
from pathlib import Path

from pipeline.users import BASE_DIR, create_user, get_user, update_status

LEGACY_PROFILE = BASE_DIR / "profile"
LEGACY_PIPELINE = BASE_DIR / "pipeline"


def migrate():
    if get_user("kerbel"):
        print("User 'kerbel' already exists — skipping migration")
        return

    print("Migrating existing data to user_kerbel/...")
    create_user("kerbel", "Maksim", "180163996")
    ud = BASE_DIR / "user_kerbel"

    if LEGACY_PROFILE.exists():
        target = ud / "profile"
        if not target.exists():
            shutil.move(str(LEGACY_PROFILE), str(target))
            print(f"  Moved {LEGACY_PROFILE} -> {target}")

    for fname in ["token.json", "processed_posts.json", "pending_events.json"]:
        src = LEGACY_PIPELINE / fname
        dst = ud / fname
        if src.exists() and not dst.exists():
            shutil.move(str(src), str(dst))
            print(f"  Moved {src} -> {dst}")

    update_status("kerbel", "active")
    print("Migration complete. User 'kerbel' is active.")


if __name__ == "__main__":
    migrate()
