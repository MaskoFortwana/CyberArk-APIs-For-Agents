# CyberArk API Reference Databases

Build offline-searchable SQLite databases of **567 CyberArk REST API endpoints** across Identity, Privilege Cloud, and PAM Self-Hosted — with FTS5 full-text search built in.

## Built for AI Agents

These databases are designed to be loaded by AI agents and assistants — such as **Claude** (via MCP tools), **LangChain**, or any LLM-based automation — to give them instant, structured knowledge of CyberArk APIs.

Point your agent at these lightweight SQLite files and let it query endpoints, methods, paths, and categories on the fly:

- Look up the correct API endpoint for any CyberArk operation
- Construct valid API calls with the right HTTP method, path, and base URL
- Discover related endpoints by searching across categories
- Work offline without needing access to CyberArk's documentation portal

Each database is self-contained, under 150 KB, and includes a full-text search index.

## Databases

| Database | Endpoints | Categories | Description |
|----------|-----------|------------|-------------|
| `cyberark-identity-api.db` | 210 | 22 | CyberArk Identity Platform API (OAuth2, SCIM, Users, Roles, Policies...) |
| `cyberark-privilege-cloud-api.db` | 113 | 34 | Privilege Cloud Shared Services API (Accounts, Safes, Platforms, Sessions...) |
| `cyberark-pam-selfhosted-api.db` | 244 | 68 | PAM Self-Hosted PVWA API (Accounts, Safes, Platforms, LDAP, PTA, Auth...) |

## Quick Start

```bash
git clone git@github.com:YOUR_USER/cyberark-api-tools.git
cd cyberark-api-tools
pip install requests beautifulsoup4

# Fetch data from live CyberArk docs + build databases (one command)
python fetch_data.py

# Search
python search.py "password change"
python search.py "safe members" --method POST
python search.py --stats
```

## Requirements

- **Python 3.8+**
- `pip install requests beautifulsoup4`

## Usage

### Fetch + Build (one command)

Scrapes live CyberArk documentation sites, saves JSON to `data/`, and builds SQLite databases:

```bash
# All 3 API sources
python fetch_data.py

# Specific source only
python fetch_data.py identity
python fetch_data.py pcloud
python fetch_data.py pam

# Fetch JSON only, skip DB build
python fetch_data.py --fetch-only

# Custom output directory for .db files
python fetch_data.py --output-dir ./databases

# Verbose output
python fetch_data.py -v
```

### Rebuild from Existing Data

If you already fetched the data and just want to rebuild databases:

```bash
python build_databases.py
python build_databases.py identity
python build_databases.py --output-dir ./databases
```

### Search

```bash
# Search across all databases
python search.py "account discovery"

# Search specific database
python search.py "OAuth token" --db identity

# Filter by HTTP method
python search.py "safes" --method GET

# Show documentation URLs
python search.py "platforms" -v

# Database statistics
python search.py --stats

# List all API categories
python search.py --list-categories --db pam
```

### FTS5 Query Syntax

The search uses SQLite FTS5:

- `password change` — match both words (implicit AND)
- `password OR change` — match either word
- `"password change"` — exact phrase
- `password*` — prefix matching
- `NOT deprecated` — exclude term

## How It Works

```
fetch_data.py                          build_databases.py
┌──────────────────────┐               ┌──────────────────┐
│ Scrape CyberArk docs │ ─> data/*.json ─> │ Build SQLite DBs │ ─> *.db
│ (requests + bs4)     │               │ (stdlib only)    │
└──────────────────────┘               └──────────────────┘
```

`fetch_data.py` scrapes three CyberArk documentation sites, extracts API endpoint data (method, path, summary, category, doc URL), and saves structured JSON files. It then calls `build_databases.py` which creates SQLite databases with FTS5 full-text search indexes.

### Safety Features

- **Regression protection** — `save_json()` compares new scrape results against existing JSON files. If the new data has fewer endpoints, it refuses to overwrite and saves the inferior data as `.json.new` for inspection instead.
- **Non-fatal DB verification** — `build_databases.py` no longer crashes on validation failures (e.g., 0 endpoints). It prints warnings and continues building the remaining databases.
- **Per-target error isolation** — If one API source fails during fetch, the others still build normally.

### Data Sources

| Source | URL | Parser |
|--------|-----|--------|
| Identity API | `api-docs.cyberark.com` (React Router v7 SPA) | SSR context extraction → `.data` URL turbo-stream → HTML regex fallback (best-effort; browser extraction recommended) |
| Privilege Cloud | `docs.cyberark.com/privilege-cloud-shared-services` (MadCap Flare) | TOC JS files (`Data/Tocs/*.js`) + chunked/flat TOC detection + ~70 known fallback paths + per-page endpoint parsing |
| PAM Self-Hosted | `docs.cyberark.com/pam-self-hosted` (MadCap Flare) | TOC JS files (`Data/Tocs/*.js`) + chunked/flat TOC detection + ~70 known fallback paths + per-page endpoint parsing |

### SQLite Schema

**Identity API** (includes tags for sub-categorization):

```
categories: id, name, slug, docs_url, endpoint_count
tags:        id, name, category_id
endpoints:   id, method, path, summary, category_id, tag_id, deprecated, base_url, doc_reference
```

**Privilege Cloud & PAM Self-Hosted**:

```
categories: id, name, endpoint_count
endpoints:   id, method, path, summary, category_id, base_url, doc_url, deprecated
```

All databases include an `endpoints_fts` virtual table for full-text search.

## Use with AI/LLM Tools

Example MCP tool integration:

```python
import sqlite3

def search_cyberark_api(query: str, db_path: str) -> list:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    results = conn.execute("""
        SELECT e.method, e.path, e.summary, c.name as category, e.base_url
        FROM endpoints e
        JOIN categories c ON e.category_id = c.id
        WHERE e.id IN (
            SELECT rowid FROM endpoints_fts WHERE endpoints_fts MATCH ?
        )
    """, (query,)).fetchall()
    conn.close()
    return [dict(r) for r in results]
```

## Contributing

To refresh the data after CyberArk updates their docs:

1. `python fetch_data.py` — re-scrape + rebuild
2. `python search.py --stats` — verify counts

### Identity API — Browser Extraction (Recommended)

The Identity API docs (`api-docs.cyberark.com`) are a client-side React SPA hosted on SwaggerHub Portal. The API data is only available after JavaScript execution, so `requests` cannot reliably extract it. The fetcher tries multiple strategies (SSR context, `.data` URL turbo-stream, HTML regex) but the most reliable method is browser extraction. To refresh Identity data:

1. Open `https://api-docs.cyberark.com/identity-docs-api/docs/oauth2-api` in Chrome
2. Open DevTools Console (F12) and paste:

```javascript
const ld = window.__reactRouterContext.state.loaderData;
const toc = ld['routes/_docs.$productname/_index'].sections.items[0].tableOfContents;
const cats = [], eps = [];
toc.filter(c => c.operations?.length).forEach((cat, idx) => {
  const slug = (cat.slug || '').replace('docs/', '');
  cats.push({name: cat.title, slug, docs_url: 'https://api-docs.cyberark.com/identity-docs-api/docs/' + slug, endpoint_count: cat.operations.length});
  cat.operations.forEach(op => eps.push({method: op.method.toUpperCase(), path: op.path, summary: op.summary || '', category_id: idx + 1, tag_id: null, deprecated: 0, base_url: 'https://{tenant}/api', doc_reference: 'https://api-docs.cyberark.com/identity-docs-api/docs/' + slug}));
});
const blob = new Blob([JSON.stringify({metadata: {name: 'CyberArk Identity API', description: 'CyberArk Identity Platform REST API endpoints', default_base_url: 'https://{tenant}/api', source: 'https://api-docs.cyberark.com/identity-docs-api/docs', fetched_at: new Date().toISOString()}, categories: cats, tags: [], endpoints: eps}, null, 2)], {type: 'application/json'});
const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'identity-api.json'; a.click();
```

3. Save the downloaded `identity-api.json` to the `data/` directory
4. Run `python build_databases.py identity` — builds the DB from saved JSON
   (or `python fetch_data.py identity` — it will detect the existing data and skip overwriting thanks to regression protection)

### Privilege Cloud & PAM Self-Hosted

These docs use MadCap Flare with static TOC JS files. The scraper fetches them automatically — no browser needed.

If the doc site structure changes and the scraper breaks, update the parsers in `fetch_data.py`.

## License

MIT
