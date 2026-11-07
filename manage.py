"""Admin commands. Run on the server; the dashboard never mints keys or users.

    python manage.py add-hive hive-1 "Backyard hive" temple-hills
    python manage.py add-user client1
    python manage.py list-hives

A device key is printed ONCE. The server stores only its SHA-256 hash, so a
lost key cannot be recovered: delete the hive and add it again.
"""

import getpass
import os
import secrets
import sys

os.environ.setdefault("SESSION_SECRET", "not-used-here")

from main import db, key_hash, pw_hash  # noqa: E402


def add_hive(hive_id: str, name: str, site: str = "temple-hills"):
    key = secrets.token_hex(24)  # 48 hex characters
    with db() as con:
        con.execute(
            "INSERT INTO hives(id, name, site, key_hash) VALUES (?, ?, ?, ?)",
            (hive_id, name, site, key_hash(key)),
        )
    print(f"Hive {hive_id} created at site {site}.")
    print("Give this key to the node. It is not shown again:")
    print(key)


def add_user(username: str, password: str | None = None):
    password = password or getpass.getpass("Password: ")
    if len(password) < 12:
        print("Warning: use at least 12 characters for a client login.")
    salt = secrets.token_bytes(16)
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO users(username, salt, pw_hash) "
            "VALUES (?, ?, ?)",
            (username, salt, pw_hash(password, salt)),
        )
    print(f"User {username} saved.")


def list_hives():
    with db() as con:
        rows = con.execute(
            "SELECT id, name, site, interval_min, reported_interval, last_seen "
            "FROM hives ORDER BY site, id"
        ).fetchall()
    if not rows:
        print("No hives yet.")
        return
    print(f"{'id':<10} {'site':<16} {'asked':>6} {'using':>6}  name")
    for r in rows:
        print(
            f"{r['id']:<10} {r['site']:<16} {r['interval_min']:>6} "
            f"{str(r['reported_interval'] or '-'):>6}  {r['name']}"
        )


if __name__ == "__main__":
    cmd, *args = sys.argv[1:] or ["help"]
    if cmd == "add-hive" and 2 <= len(args) <= 3:
        add_hive(*args)
    elif cmd == "add-user" and len(args) in (1, 2):
        add_user(*args)
    elif cmd == "list-hives" and not args:
        list_hives()
    else:
        print(__doc__)
