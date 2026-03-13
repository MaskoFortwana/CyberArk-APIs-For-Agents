# CyberArk API Reference Databases

Offline-searchable SQLite databases covering **534 CyberArk REST API endpoints** and **4,170 Known Issues** — with FTS5 full-text search built in.

## Built for AI Agents

These databases are specifically designed to be loaded by AI agents and assistants — such as **Claude** (via MCP tools or file context), **OpenClaw**, **LangChain**, or any LLM-based automation — to give them instant, structured knowledge of CyberArk APIs.

Instead of pasting API docs into prompts or relying on the model's training data, you can point your agent at these lightweight SQLite files and let it query endpoints, methods, paths, and categories on the fly. This makes it easy to build agents that can:

- Look up the correct API endpoint for any CyberArk operation
- Construct valid API calls with the right HTTP method, path, and base URL
- Discover related endpoints by searching across categories
- Search Known Issues by product, component, or keyword to troubleshoot problems
- Work offline without needing access to CyberArk's documentation portal

Each database is self-contained (no external dependencies), typically under 3 MB, and includes a full-text search index for fast semantic lookups.

## Databases

### API Reference

| Database | Endpoints | Categories | Description |
|----------|-----------|------------|-------------|
| `cyberark-identity-api.db` | 174 | 21 | CyberArk Identity Platform API (OAuth2, SCIM, Users, Roles, Policies...) |
| `cyberark-privilege-cloud-api.db` | 117 | 18 | Privilege Cloud Shared Services API (Accounts, Safes, Platforms, Sessions...) |
| `cyberark-pam-selfhosted-api.db` | 243 | 29 | PAM Self-Hosted PVWA API (Accounts, Safes, Platforms, LDAP, PTA, Auth...) |

### Known Issues

| Database | Articles | Products | Components | Description |
|----------|----------|----------|------------|-------------|
| `cyberark-known-issues.db` | 4,170 | 17 | 93 | Known Issues from CyberArk community portal (832 with full descriptions) |

## Quick Start

```bash
# Clone and build
git clone git@github.com:MaskoFortwana/CyberArk-APIs-For-Agents.git
cd CyberArk-APIs-For-Agents
python build_databases.py

# Search API endpoints
python search.py "password change"
python search.py "SAML logon" --db identity
python search.py "safe members" --method POST

# Search Known Issues
python search.py --ki "CPM password rotation"
python search.py --ki "EPM agent crash" --product EPM -v
python search.py --ki "SmartCard" --status Open

# Stats
python search.py --stats
```

## Requirements

- Python 3.8+
- No external dependencies — uses only stdlib (`sqlite3`, `json`, `argparse`)

## Build

```bash
# Build all 4 databases
python build_databases.py

# Build specific database
python build_databases.py identity
python build_databases.py pcloud
python build_databases.py pam
python build_databases.py ki

# Custom output directory
python build_databases.py --output-dir ./databases
```

## Search

### API Endpoints

```bash
# Basic search (queries all 3 API databases)
python search.py "account discovery"

# Search specific database
python search.py "OAuth token" --db identity

# Filter by HTTP method
python search.py "safes" --method GET

# Show documentation URLs
python search.py "platforms" -v

# List all API categories
python search.py --list-categories --db pam
```

### Known Issues

```bash
# Search by keyword
python search.py --ki "vault replication"

# Filter by product
python search.py --ki "agent" --product EPM

# Filter by status
python search.py --ki "password change" --status Open

# Verbose output (shows description, workaround, URL)
python search.py --ki "PVWA" -v
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

The raw data lives in `data/*.json`:

- `identity-api.json` — 174 endpoints, 21 categories, 51 tags
- `privilege-cloud-api.json` — 117 endpoints, 18 categories
- `pam-selfhosted-api.json` — 243 endpoints, 29 categories
- `known-issues.json` — 4,170 articles, 17 products, 93 components, 7 statuses

### SQLite Schemas

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

**Known Issues**:

```
products:      id, name
components:    id, name
statuses:      id, name
known_issues:  id, ki_id, title, product_id, component_id, status_id,
               earliest_known_version, resolved_in_version, description,
               workaround, article_url
```

All databases include FTS5 virtual tables for full-text search.

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

def search_known_issues(query: str, db_path: str) -> list:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    results = conn.execute("""
        SELECT ki.ki_id, ki.title, ki.description, ki.workaround,
               p.name as product, c.name as component, s.name as status
        FROM known_issues ki
        LEFT JOIN products p ON ki.product_id = p.id
        LEFT JOIN components c ON ki.component_id = c.id
        LEFT JOIN statuses s ON ki.status_id = s.id
        WHERE ki.id IN (
            SELECT rowid FROM known_issues_fts WHERE known_issues_fts MATCH ?
        )
    """, (query,)).fetchall()
    conn.close()
    return [dict(r) for r in results]
```

## Contributing

To update the data:

1. Edit the JSON files in `data/`
2. Run `python build_databases.py` to rebuild
3. Verify with `python search.py --stats`

## Data Sources

- **Identity API**: [CyberArk Identity API Docs](https://api-docs.cyberark.com/identity-docs-api/)
- **Privilege Cloud**: [CyberArk Privilege Cloud API Docs](https://docs.cyberark.com/privilege-cloud-shared-services/latest/en/)
- **PAM Self-Hosted**: [CyberArk PAM Self-Hosted API Docs](https://docs.cyberark.com/pam-self-hosted/latest/en/)
- **Known Issues**: [CyberArk Community Known Issues](https://community.cyberark.com/s/known-issues)

## License

MIT
