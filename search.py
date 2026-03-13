#!/usr/bin/env python3
"""
Search CyberArk databases using full-text search.

Usage:
    python search.py "password change"               # Search all API databases
    python search.py "SAML logon" --db identity       # Search specific API DB
    python search.py "GET accounts" --method GET      # Filter by HTTP method
    python search.py --ki "CPM password rotation"     # Search Known Issues
    python search.py --ki "EPM agent crash" --product EPM
    python search.py --list-categories                # List all categories
    python search.py --stats                          # Show database statistics

Requires: Python 3.8+ (no external dependencies)
"""

import sqlite3
import argparse
import sys
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).parent

API_DB_FILES = {
    "identity": ("cyberark-identity-api.db", "CyberArk Identity"),
    "pcloud": ("cyberark-privilege-cloud-api.db", "Privilege Cloud"),
    "pam": ("cyberark-pam-selfhosted-api.db", "PAM Self-Hosted"),
}

KI_DB_FILE = ("cyberark-known-issues.db", "Known Issues")


def get_db_path(filename: str) -> Path:
    return SCRIPT_DIR / filename


# ---------------------------------------------------------------------------
# API endpoint search
# ---------------------------------------------------------------------------

def search_endpoints(
    query: str,
    db_key: Optional[str] = None,
    method: Optional[str] = None,
    limit: int = 50,
) -> list:
    """Search endpoints across one or all API databases."""
    results = []
    keys = [db_key] if db_key else list(API_DB_FILES.keys())

    for key in keys:
        filename, label = API_DB_FILES[key]
        db_path = get_db_path(filename)
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
            r["database"] = label
            results.append(r)

        conn.close()

    return results


# ---------------------------------------------------------------------------
# Known Issues search
# ---------------------------------------------------------------------------

def search_known_issues(
    query: str,
    product: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list:
    """Search Known Issues database."""
    db_path = get_db_path(KI_DB_FILE[0])
    if not db_path.exists():
        print(f"  Known Issues database not found at {db_path}")
        return []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    fts_query = query.replace('"', '""')

    sql = """
        SELECT ki.ki_id, ki.title,
               COALESCE(p.name, '') as product,
               COALESCE(c.name, '') as component,
               COALESCE(s.name, '') as status,
               ki.earliest_known_version,
               ki.resolved_in_version,
               ki.description,
               ki.workaround,
               ki.article_url
        FROM known_issues ki
        LEFT JOIN products p ON ki.product_id = p.id
        LEFT JOIN components c ON ki.component_id = c.id
        LEFT JOIN statuses s ON ki.status_id = s.id
        WHERE ki.id IN (
            SELECT rowid FROM known_issues_fts
            WHERE known_issues_fts MATCH ?
        )
    """

    params = [fts_query]

    if product:
        sql += " AND p.name LIKE ?"
        params.append(f"%{product}%")

    if status:
        sql += " AND s.name LIKE ?"
        params.append(f"%{status}%")

    sql += " ORDER BY ki.last_published_date DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    results = [dict(r) for r in rows]
    conn.close()
    return results


# ---------------------------------------------------------------------------
# Stats & categories
# ---------------------------------------------------------------------------

def list_categories(db_key: Optional[str] = None) -> None:
    """List all categories across API databases."""
    keys = [db_key] if db_key else list(API_DB_FILES.keys())

    for key in keys:
        filename, label = API_DB_FILES[key]
        db_path = get_db_path(filename)
        if not db_path.exists():
            continue

        conn = sqlite3.connect(str(db_path))
        print(f"\n{'=' * 50}")
        print(f"  {label}")
        print(f"{'=' * 50}")

        cats = conn.execute(
            "SELECT name, endpoint_count FROM categories ORDER BY name"
        ).fetchall()
        for name, count in cats:
            print(f"  {name:<40} {count:>3} endpoints")
        conn.close()


def show_stats(db_key: Optional[str] = None) -> None:
    """Show database statistics."""
    print(f"\n{'Database':<30} {'Records':>12} {'Categories':>12} {'FTS':>8} {'Size':>10}")
    print("-" * 75)

    # API databases
    keys = [db_key] if db_key else list(API_DB_FILES.keys())
    for key in keys:
        filename, label = API_DB_FILES[key]
        db_path = get_db_path(filename)
        if not db_path.exists():
            print(f"  {label:<28} — not found")
            continue

        conn = sqlite3.connect(str(db_path))
        cats = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        eps = conn.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0]
        fts = conn.execute("SELECT COUNT(*) FROM endpoints_fts").fetchone()[0]
        conn.close()

        size = db_path.stat().st_size / 1024
        print(f"  {label:<28} {eps:>10} ep {cats:>10} cat {fts:>8} {size:>7.0f} KB")

    # Known Issues
    if not db_key:
        ki_path = get_db_path(KI_DB_FILE[0])
        if ki_path.exists():
            conn = sqlite3.connect(str(ki_path))
            total = conn.execute("SELECT COUNT(*) FROM known_issues").fetchone()[0]
            enriched = conn.execute(
                "SELECT COUNT(*) FROM known_issues WHERE description IS NOT NULL AND description != ''"
            ).fetchone()[0]
            fts = conn.execute("SELECT COUNT(*) FROM known_issues_fts").fetchone()[0]
            prods = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            conn.close()

            size = ki_path.stat().st_size / 1024
            print(f"  {'Known Issues':<28} {total:>10} ki {prods:>10} prod {fts:>8} {size:>7.0f} KB")
            print(f"    (enriched: {enriched}/{total})")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_endpoint_result(r: dict, verbose: bool = False) -> str:
    """Format a single API endpoint search result."""
    dep = " [DEPRECATED]" if r["deprecated"] else ""
    line = f"  {r['method']:<7} {r['path']}"
    if r["summary"]:
        line += f"\n          {r['summary']}"
    line += f"\n          [{r['database']}] {r['category']}{dep}"
    if verbose and r.get("doc_link"):
        line += f"\n          Doc: {r['doc_link']}"
    return line


def format_ki_result(r: dict, verbose: bool = False) -> str:
    """Format a single Known Issues search result."""
    status_str = f" [{r['status']}]" if r["status"] else ""
    resolved = f" -> Fixed in {r['resolved_in_version']}" if r["resolved_in_version"] else ""

    line = f"  {r['ki_id']}{status_str}{resolved}"
    line += f"\n    {r['title']}"
    line += f"\n    Product: {r['product']}  |  Component: {r['component']}"

    if verbose:
        if r.get("description") and r["description"].strip():
            desc = r["description"][:200].replace("\n", " ")
            if len(r["description"]) > 200:
                desc += "..."
            line += f"\n    Desc: {desc}"
        if r.get("workaround") and r["workaround"].strip():
            wa = r["workaround"][:200].replace("\n", " ")
            if len(r["workaround"]) > 200:
                wa += "..."
            line += f"\n    Workaround: {wa}"
        if r.get("article_url"):
            line += f"\n    URL: {r['article_url']}"

    return line


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Search CyberArk databases")
    parser.add_argument("query", nargs="?", help="Search query (FTS5 syntax) for API endpoints")
    parser.add_argument(
        "--db", choices=list(API_DB_FILES.keys()),
        help="Search specific API database only",
    )
    parser.add_argument("--method", help="Filter by HTTP method (GET/POST/PUT/DELETE/PATCH)")
    parser.add_argument("--ki", metavar="QUERY", help="Search Known Issues instead of APIs")
    parser.add_argument("--product", help="Filter KI by product name (e.g., EPM, 'Core PAS')")
    parser.add_argument("--status", help="Filter KI by status (e.g., Open, Fixed)")
    parser.add_argument("--limit", type=int, default=50, help="Max results (default: 50)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show details/URLs")
    parser.add_argument("--list-categories", action="store_true", help="List all API categories")
    parser.add_argument("--stats", action="store_true", help="Show database statistics")

    args = parser.parse_args()

    if args.stats:
        show_stats(args.db)
        return

    if args.list_categories:
        list_categories(args.db)
        return

    # Known Issues search
    if args.ki:
        results = search_known_issues(args.ki, args.product, args.status, args.limit)
        if not results:
            print(f"No Known Issues found for: {args.ki}")
            sys.exit(0)

        print(f"\nFound {len(results)} Known Issue(s) for: {args.ki}\n")
        for r in results:
            print(format_ki_result(r, args.verbose))
            print()
        return

    # API endpoint search
    if not args.query:
        parser.print_help()
        sys.exit(1)

    results = search_endpoints(args.query, args.db, args.method, args.limit)

    if not results:
        print(f"No results for: {args.query}")
        sys.exit(0)

    print(f"\nFound {len(results)} result(s) for: {args.query}\n")
    for r in results:
        print(format_endpoint_result(r, args.verbose))
        print()


if __name__ == "__main__":
    main()
