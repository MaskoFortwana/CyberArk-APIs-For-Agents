#!/usr/bin/env python3
"""
Build CyberArk API SQLite databases from JSON seed data.

Creates three searchable SQLite databases with FTS5 full-text search:
  - cyberark-identity-api.db       (174 endpoints, 21 categories, 51 tags)
  - cyberark-privilege-cloud-api.db (117 endpoints, 18 categories)
  - cyberark-pam-selfhosted-api.db  (243 endpoints, 29 categories)

Usage:
    python build_databases.py              # Build all 3 databases
    python build_databases.py identity     # Build only Identity API
    python build_databases.py pcloud       # Build only Privilege Cloud API
    python build_databases.py pam          # Build only PAM Self-Hosted API
    python build_databases.py --output-dir ./my-dbs  # Custom output directory

Requires: Python 3.8+ (no external dependencies)
"""

import sqlite3
import json
import sys
import os
import argparse
from pathlib import Path
from datetime import datetime


SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"

DB_CONFIGS = {
    "identity": {
        "json_file": "identity-api.json",
        "db_file": "cyberark-identity-api.db",
        "has_tags": True,
    },
    "pcloud": {
        "json_file": "privilege-cloud-api.json",
        "db_file": "cyberark-privilege-cloud-api.db",
        "has_tags": False,
    },
    "pam": {
        "json_file": "pam-selfhosted-api.json",
        "db_file": "cyberark-pam-selfhosted-api.db",
        "has_tags": False,
    },
}


def create_identity_db(data: dict, db_path: Path) -> dict:
    """Build the CyberArk Identity API database."""
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    # Schema
    cur.executescript("""
        CREATE TABLE categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            slug TEXT NOT NULL,
            docs_url TEXT NOT NULL,
            endpoint_count INTEGER DEFAULT 0
        );

        CREATE TABLE tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category_id INTEGER NOT NULL,
            FOREIGN KEY (category_id) REFERENCES categories(id),
            UNIQUE(name, category_id)
        );

        CREATE TABLE endpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            method TEXT NOT NULL,
            path TEXT NOT NULL,
            summary TEXT,
            category_id INTEGER NOT NULL,
            tag_id INTEGER,
            deprecated INTEGER DEFAULT 0,
            base_url TEXT DEFAULT 'https://{tenant}/api',
            doc_reference TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (category_id) REFERENCES categories(id),
            FOREIGN KEY (tag_id) REFERENCES tags(id)
        );

        CREATE VIRTUAL TABLE endpoints_fts USING fts5(
            method,
            path,
            summary,
            category_name,
            tag_name
        );
    """)

    # Insert categories
    cat_id_map = {}
    for cat in data["categories"]:
        cur.execute(
            "INSERT INTO categories (name, slug, docs_url, endpoint_count) VALUES (?, ?, ?, ?)",
            (cat["name"], cat["slug"], cat["docs_url"], cat["endpoint_count"]),
        )
        cat_id_map[cat["name"]] = cur.lastrowid

    # Build old_id -> new_id mapping for categories
    old_cat_map = {}
    for i, cat in enumerate(data["categories"], 1):
        old_cat_map[i] = cat_id_map[cat["name"]]

    # Insert tags
    tag_id_map = {}
    for tag in data["tags"]:
        new_cat_id = old_cat_map.get(tag["category_id"], tag["category_id"])
        cur.execute(
            "INSERT INTO tags (name, category_id) VALUES (?, ?)",
            (tag["name"], new_cat_id),
        )
        tag_id_map[tag["name"]] = cur.lastrowid

    # Build old_tag_id -> new_tag_id mapping
    old_tag_map = {}
    for i, tag in enumerate(data["tags"], 1):
        old_tag_map[i] = tag_id_map[tag["name"]]

    # Insert endpoints
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for ep in data["endpoints"]:
        new_cat_id = old_cat_map.get(ep["category_id"], ep["category_id"])
        new_tag_id = old_tag_map.get(ep["tag_id"]) if ep.get("tag_id") else None
        cur.execute(
            """INSERT INTO endpoints (method, path, summary, category_id, tag_id,
               deprecated, base_url, doc_reference, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ep["method"],
                ep["path"],
                ep.get("summary"),
                new_cat_id,
                new_tag_id,
                ep.get("deprecated", 0),
                ep.get("base_url", "https://{tenant}/api"),
                ep.get("doc_reference"),
                now,
            ),
        )

    # Build FTS index
    cur.execute("""
        INSERT INTO endpoints_fts (method, path, summary, category_name, tag_name)
        SELECT e.method, e.path, e.summary, c.name,
               COALESCE(t.name, '')
        FROM endpoints e
        JOIN categories c ON e.category_id = c.id
        LEFT JOIN tags t ON e.tag_id = t.id
    """)

    conn.commit()

    # Stats
    stats = {
        "categories": cur.execute("SELECT COUNT(*) FROM categories").fetchone()[0],
        "tags": cur.execute("SELECT COUNT(*) FROM tags").fetchone()[0],
        "endpoints": cur.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0],
        "fts_entries": cur.execute("SELECT COUNT(*) FROM endpoints_fts").fetchone()[0],
    }
    conn.close()
    return stats


def create_standard_db(data: dict, db_path: Path, default_base_url: str) -> dict:
    """Build a Privilege Cloud or PAM Self-Hosted API database."""
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    # Schema
    cur.executescript(f"""
        CREATE TABLE categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            endpoint_count INTEGER DEFAULT 0
        );

        CREATE TABLE endpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            method TEXT NOT NULL,
            path TEXT NOT NULL,
            summary TEXT,
            category_id INTEGER NOT NULL,
            base_url TEXT DEFAULT '{default_base_url}',
            doc_url TEXT,
            deprecated INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (category_id) REFERENCES categories(id)
        );

        CREATE VIRTUAL TABLE endpoints_fts USING fts5(
            method,
            path,
            summary,
            category_name
        );
    """)

    # Insert categories
    cat_id_map = {}
    for cat in data["categories"]:
        cur.execute(
            "INSERT INTO categories (name, endpoint_count) VALUES (?, ?)",
            (cat["name"], cat["endpoint_count"]),
        )
        cat_id_map[cat["name"]] = cur.lastrowid

    # Build old_id -> new_id mapping
    old_cat_map = {}
    for i, cat in enumerate(data["categories"], 1):
        old_cat_map[i] = cat_id_map[cat["name"]]

    # Insert endpoints
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for ep in data["endpoints"]:
        new_cat_id = old_cat_map.get(ep["category_id"], ep["category_id"])
        cur.execute(
            """INSERT INTO endpoints (method, path, summary, category_id,
               base_url, doc_url, deprecated, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ep["method"],
                ep["path"],
                ep.get("summary"),
                new_cat_id,
                ep.get("base_url", default_base_url),
                ep.get("doc_url"),
                ep.get("deprecated", 0),
                now,
            ),
        )

    # Build FTS index
    cur.execute("""
        INSERT INTO endpoints_fts (method, path, summary, category_name)
        SELECT e.method, e.path, e.summary, c.name
        FROM endpoints e
        JOIN categories c ON e.category_id = c.id
    """)

    conn.commit()

    stats = {
        "categories": cur.execute("SELECT COUNT(*) FROM categories").fetchone()[0],
        "endpoints": cur.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0],
        "fts_entries": cur.execute("SELECT COUNT(*) FROM endpoints_fts").fetchone()[0],
    }
    conn.close()
    return stats


def build_database(key: str, output_dir: Path) -> None:
    """Build a single database by key."""
    config = DB_CONFIGS[key]
    json_path = DATA_DIR / config["json_file"]
    db_path = output_dir / config["db_file"]

    if not json_path.exists():
        print(f"  ERROR: {json_path} not found — skipping")
        return

    with open(json_path, "r") as f:
        data = json.load(f)

    meta = data.get("metadata", {})
    print(f"\n  Building: {meta.get('name', key)}")
    print(f"  Source:   {config['json_file']}")
    print(f"  Output:   {db_path}")

    if config["has_tags"]:
        stats = create_identity_db(data, db_path)
    else:
        default_url = meta.get("default_base_url", "")
        stats = create_standard_db(data, db_path, default_url)

    print(f"  Result:   {stats}")
    size_kb = db_path.stat().st_size / 1024
    print(f"  Size:     {size_kb:.1f} KB")


def verify_database(db_path: Path) -> None:
    """Run basic verification queries on a built database."""
    if not db_path.exists():
        return

    conn = sqlite3.connect(str(db_path))

    # Check tables exist
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    assert "categories" in tables, "Missing categories table"
    assert "endpoints" in tables, "Missing endpoints table"
    assert "endpoints_fts" in tables, "Missing FTS table"

    # Check data
    cat_count = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    ep_count = conn.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0]
    fts_count = conn.execute("SELECT COUNT(*) FROM endpoints_fts").fetchone()[0]

    assert cat_count > 0, "No categories found"
    assert ep_count > 0, "No endpoints found"
    assert fts_count == ep_count, f"FTS mismatch: {fts_count} != {ep_count}"

    # Test FTS search
    result = conn.execute(
        "SELECT COUNT(*) FROM endpoints_fts WHERE endpoints_fts MATCH 'password OR account'"
    ).fetchone()[0]

    conn.close()
    print(f"  Verify:   OK ({cat_count} cats, {ep_count} eps, FTS search={result} hits)")


def main():
    parser = argparse.ArgumentParser(
        description="Build CyberArk API SQLite databases from JSON seed data"
    )
    parser.add_argument(
        "targets",
        nargs="*",
        choices=list(DB_CONFIGS.keys()) + [[]],
        default=[],
        help="Which databases to build (default: all)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=SCRIPT_DIR,
        help="Output directory for .db files (default: script directory)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        default=True,
        help="Run verification after building (default: True)",
    )

    args = parser.parse_args()
    targets = args.targets if args.targets else list(DB_CONFIGS.keys())

    args.output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  CyberArk API Database Builder")
    print("=" * 60)

    for key in targets:
        build_database(key, args.output_dir)
        if args.verify:
            db_path = args.output_dir / DB_CONFIGS[key]["db_file"]
            verify_database(db_path)

    print("\n" + "=" * 60)
    print("  Done! Databases built successfully.")
    print("=" * 60)


if __name__ == "__main__":
    main()
