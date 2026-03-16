#!/usr/bin/env python3
"""
Search CyberArk API databases using full-text search.

Usage:
    python search.py "password change"           # Search all databases
    python search.py "SAML logon" --db identity   # Search specific DB
    python search.py "GET accounts" --method GET  # Filter by HTTP method
    python search.py --list-categories            # List all categories
    python search.py --stats                      # Show database statistics

Requires: Python 3.8+ (no external dependencies)
"""

import sqlite3
import argparse
import sys
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).parent

DB_FILES = {
    "identity": ("cyberark-identity-api.db", "CyberArk Identity"),
    "pcloud": ("cyberark-privilege-cloud-api.db", "Privilege Cloud"),
    "pam": ("cyberark-pam-selfhosted-api.db", "PAM Self-Hosted"),
}


def get_db_path(key: str) -> Path:
    return SCRIPT_DIR / DB_FILES[key][0]


def search_endpoints(
    query: str,
    db_key: Optional[str] = None,
    method: Optional[str] = None,
    limit: int = 50,
) -> list:
    """Search endpoints across one or all databases."""
    results = []
    keys = [db_key] if db_key else list(DB_FILES.keys())

    for key in keys:
        db_path = get_db_path(key)
        if not db_path.exists():
            continue

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        fts_query = query.replace('"', '""')

        # Detect schema: Identity has doc_reference, others have doc_url
        columns = [
            r[1]
            for r in conn.execute("PRAGMA table_info(endpoints)").fetchall()
        ]
        doc_col = "doc_reference" if "doc_reference" in columns else "doc_url"

        rows = conn.execute(
            f"""
            SELECT e.method, e.path, e.summary,
                   c.name as category,
                   e.base_url, e.deprecated,
                   COALESCE(e.{doc_col}, '') as doc_link
            FROM endpoints e
            JOIN categories c ON e.category_id = c.id
            WHERE e.id IN (
                SELECT rowid FROM endpoints_fts
                WHERE endpoints_fts MATCH ?
            )
            ORDER BY e.method, e.path
            LIMIT ?
            """,
            (fts_query, limit),
        ).fetchall()

        for row in rows:
            r = dict(row)
            if method and r["method"].upper() != method.upper():
                continue
            r["database"] = DB_FILES[key][1]
            results.append(r)

        conn.close()

    return results


def list_categories(db_key: Optional[str] = None) -> None:
    """List all categories across databases."""
    keys = [db_key] if db_key else list(DB_FILES.keys())

    for key in keys:
        db_path = get_db_path(key)
        if not db_path.exists():
            continue

        conn = sqlite3.connect(str(db_path))
        print(f"\n{'=' * 50}")
        print(f"  {DB_FILES[key][1]}")
        print(f"{'=' * 50}")

        cats = conn.execute(
            "SELECT name, endpoint_count FROM categories ORDER BY name"
        ).fetchall()
        for name, count in cats:
            print(f"  {name:<40} {count:>3} endpoints")
        conn.close()


def show_stats(db_key: Optional[str] = None) -> None:
    """Show database statistics."""
    keys = [db_key] if db_key else list(DB_FILES.keys())

    print(f"\n{'Database':<30} {'Categories':>12} {'Endpoints':>12} {'FTS':>8} {'Size':>10}")
    print("-" * 75)

    for key in keys:
        db_path = get_db_path(key)
        if not db_path.exists():
            print(f"  {DB_FILES[key][1]:<28} — not found (run build_databases.py)")
            continue

        conn = sqlite3.connect(str(db_path))
        cats = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        eps = conn.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0]
        fts = conn.execute("SELECT COUNT(*) FROM endpoints_fts").fetchone()[0]
        conn.close()

        size = db_path.stat().st_size / 1024
        print(f"  {DB_FILES[key][1]:<28} {cats:>10} {eps:>12} {fts:>8} {size:>7.0f} KB")


def format_result(r: dict, verbose: bool = False) -> str:
    """Format a single search result."""
    dep = " [DEPRECATED]" if r["deprecated"] else ""
    line = f"  {r['method']:<7} {r['path']}"
    if r["summary"]:
        line += f"\n          {r['summary']}"
    line += f"\n          [{r['database']}] {r['category']}{dep}"
    if verbose and r.get("doc_link"):
        line += f"\n          Doc: {r['doc_link']}"
    return line


def main():
    parser = argparse.ArgumentParser(description="Search CyberArk API databases")
    parser.add_argument("query", nargs="?", help="Search query (FTS5 syntax)")
    parser.add_argument(
        "--db",
        choices=list(DB_FILES.keys()),
        help="Search specific database only",
    )
    parser.add_argument("--method", help="Filter by HTTP method (GET/POST/PUT/DELETE/PATCH)")
    parser.add_argument("--limit", type=int, default=50, help="Max results (default: 50)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show doc URLs")
    parser.add_argument("--list-categories", action="store_true", help="List all categories")
    parser.add_argument("--stats", action="store_true", help="Show database statistics")

    args = parser.parse_args()

    if args.stats:
        show_stats(args.db)
        return

    if args.list_categories:
        list_categories(args.db)
        return

    if not args.query:
        parser.print_help()
        sys.exit(1)

    results = search_endpoints(args.query, args.db, args.method, args.limit)

    if not results:
        print(f"No results for: {args.query}")
        sys.exit(0)

    print(f"\nFound {len(results)} result(s) for: {args.query}\n")
    for r in results:
        print(format_result(r, args.verbose))
        print()


if __name__ == "__main__":
    main()
