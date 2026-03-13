# CyberArk API Reference Databases

Offline-searchable SQLite databases covering **534 CyberArk REST API endpoints** across Identity, Privilege Cloud, and PAM Self-Hosted — with FTS5 full-text search built in.

## Built for AI Agents

These databases are specifically designed to be loaded by AI agents and assistants — such as **Claude** (via MCP tools or file context), **OpenClaw**, **LangChain**, or any LLM-based automation — to give them instant, structured knowledge of CyberArk APIs.

Instead of pasting API docs into prompts or relying on the model's training data, you can point your agent at these lightweight SQLite files and let it query endpoints, methods, paths, and categories on the fly. This makes it easy to build agents that can:

- Look up the correct API endpoint for any CyberArk operation
- Construct valid API calls with the right HTTP method, path, and base URL
- Discover related endpoints by searching across categories
- Work offline without needing access to CyberArk's documentation portal

Each database is self-contained (no external dependencies), typically under 150 KB, and includes a full-text search index for fast semantic lookups.

## Databases

| Database | Endpoints | Categories | Description |
|----------|-----------|------------|-------------|
| `cyberark-identity-api.db` | 174 | 21 | CyberArk Identity Platform API (OAuth2, SCIM, Users, Roles, Policies...) |
| `cyberark-privilege-cloud-api.db` | 117 | 18 | Privilege Cloud Shared Services API (Accounts, Safes, Platforms, Sessions...) |
| `cyberark-pam-selfhosted-api.db` | 243 | 29 | PAM Self-Hosted PVWA API (Accounts, Safes, Platforms, LDAP, PTA, Auth...) |

## Quick Start

```bash
# Clone and build
git clone git@github.com:MaskoFortwana/CyberArk-APIs-For-Agents.git
cd cyberark-api-tools
python build_databases.py

# Search across all databases
python search.py "password change"
python search.py "SAML logon" --db identity
python search.py "safe members" --method POST

# List categories or stats
python search.py --stats
python search.py --list-categories
```

## Requirements

- Python 3.8+
- No external dependencies — uses only stdlib (`sqlite3`, `json`, `argparse`)

## Build

```bash
# Build all 3 databases
python build_databases.py

# Build specific database
python build_databases.py identity
python build_databases.py pcloud
python build_databases.py pam

# Custom output directory
python build_databases.py --output-dir ./databases
```

## Search

```bash
# Basic search (queries all 3 databases)
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

The search uses SQLite FTS5. Supported syntax:

- `password change` — match both words (implicit AND)
- `password OR change` — match either word
- `"password change"` — exact phrase
- `password*` — prefix matching
- `NOT deprecated` — exclude term

## Data Structure

### JSON Seed Files (`data/`)

The raw API data lives in `data/*.json`. Each file contains:

```json
{
  "metadata": {
    "name": "CyberArk Identity API",
    "default_base_url": "https://{tenant}/api",
    "source": "https://api-docs.cyberark.com/..."
  },
  "categories": [...],
  "endpoints": [...]
}
```

### SQLite Schema

**Identity API** (has tags for sub-categorization):

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

All databases include an `endpoints_fts` virtual table for full-text search across method, path, summary, and category name.

## Use with AI/LLM Tools

These databases are designed to be used as context sources for AI assistants working with CyberArk environments. Example MCP tool integration:

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

To update the endpoint data:

1. Edit the JSON files in `data/`
2. Run `python build_databases.py` to rebuild
3. Verify with `python search.py --stats`

## Data Sources

- **Identity API**: [CyberArk Identity API Docs](https://api-docs.cyberark.com/identity-docs-api/)
- **Privilege Cloud**: [CyberArk Privilege Cloud API Docs](https://docs.cyberark.com/privilege-cloud-shared-services/latest/en/)
- **PAM Self-Hosted**: [CyberArk PAM Self-Hosted API Docs](https://docs.cyberark.com/pam-self-hosted/latest/en/)

## License

MIT
