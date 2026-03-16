#!/usr/bin/env python3
"""
Fetch CyberArk API endpoint data from live documentation sites and build SQLite databases.

Scrapes the official CyberArk documentation, saves structured JSON seed files
to data/, and then builds searchable SQLite databases with FTS5 indexes.

Usage:
    python fetch_data.py                     # Fetch all + build databases
    python fetch_data.py identity            # Fetch + build Identity API only
    python fetch_data.py pcloud              # Fetch + build Privilege Cloud only
    python fetch_data.py pam                 # Fetch + build PAM Self-Hosted only
    python fetch_data.py --fetch-only        # Fetch JSON only, skip DB build
    python fetch_data.py --output-dir ./dbs  # Custom output dir for .db files

Requires: requests, beautifulsoup4
    pip install requests beautifulsoup4

Data source strategies:
    Identity API (api-docs.cyberark.com):
        React Router v7 SPA. Fetches each category page and extracts endpoint
        data from embedded <script> tags containing the React Router context
        (window.__reactRouterContext) which includes full OpenAPI specs.

    Privilege Cloud & PAM Self-Hosted (docs.cyberark.com):
        MadCap Flare HTML5 SPA. Fetches the TOC index JS files (Data/Tocs/*.js)
        which use AMD define() format and contain all page paths. Then fetches
        individual .htm content pages and parses METHOD /path patterns.
        NOTE: HEAD requests return 404 on these sites — always use GET.
"""

import json
import re
import sys
import time
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Install them with:")
    print("  pip install requests beautifulsoup4")
    sys.exit(1)


SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"

logging.basicConfig(
    level=logging.INFO,
    format="  %(levelname)-7s %(message)s",
)
log = logging.getLogger(__name__)

# Polite delay between requests (seconds)
REQUEST_DELAY = 0.8

# Common headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def fetch_page(url: str, session: requests.Session) -> Optional[str]:
    """Fetch a URL with retry logic. Returns text or None on failure."""
    for attempt in range(3):
        try:
            resp = session.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 404:
                log.debug(f"  404: {url}")
                return None
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            log.warning(f"Attempt {attempt + 1}/3 failed for {url}: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    log.error(f"Failed to fetch: {url}")
    return None


def fetch_json(url: str, session: requests.Session) -> Optional[dict]:
    """Fetch a URL and parse as JSON. Returns dict or None."""
    for attempt in range(3):
        try:
            resp = session.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            log.warning(f"Attempt {attempt + 1}/3 failed for {url}: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    log.error(f"Failed to fetch JSON: {url}")
    return None


def save_json(data: dict, filename: str) -> Path:
    """
    Save data as JSON to the data/ directory.

    Regression protection: if an existing file has MORE endpoints than the
    new data, the existing file is kept and a warning is logged.  The new
    (inferior) data is saved to a .new sibling so it can be inspected.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / filename

    new_ep_count = len(data.get("endpoints", []))

    # Check for regression against existing file
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            old_ep_count = len(existing.get("endpoints", []))

            if old_ep_count > 0 and new_ep_count < old_ep_count:
                pct = (1 - new_ep_count / old_ep_count) * 100
                log.warning(
                    f"Regression detected for {filename}: "
                    f"{old_ep_count} → {new_ep_count} endpoints ({pct:.0f}% loss)"
                )
                log.warning(f"Keeping existing {filename} ({old_ep_count} eps)")
                # Save new data as .new for inspection
                new_path = path.with_suffix(".json.new")
                with open(new_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                log.info(f"New (inferior) data saved to {new_path.name} for inspection")
                return path
        except (json.JSONDecodeError, KeyError):
            pass  # Existing file is corrupt — overwrite

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    size_kb = path.stat().st_size / 1024
    log.info(f"Saved {path.name} ({size_kb:.1f} KB, {new_ep_count} endpoints)")
    return path


def now_iso() -> str:
    """Return current UTC time as ISO string (timezone-aware)."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Identity API Scraper (React Router v7 / api-docs.cyberark.com)
# ---------------------------------------------------------------------------
#
# The Identity docs are a React Router v7 SPA. The page HTML contains
# embedded <script> tags with window.__reactRouterContext which holds
# the full OpenAPI spec for each category page, including all endpoints
# with method, path, summary, and operationId.
#
# The .data URL suffix (e.g., /docs/oauth2-api.data) returns a turbo-stream
# response (status 202) which also contains the data but in a harder-to-parse
# format. We prefer extracting from the HTML script tags.
#
# Fallback: try SwaggerHub public API if direct extraction fails.

IDENTITY_BASE = "https://api-docs.cyberark.com/identity-docs-api"
IDENTITY_DOCS = f"{IDENTITY_BASE}/docs"
IDENTITY_API_BASE_URL = "https://{tenant}/api"

# Known category slugs from the CyberArk Identity API docs sidebar.
IDENTITY_CATEGORY_SLUGS = [
    "oauth2-api",
    "cdirectory-service-api",
    "core-api",
    "auth-profile-api",
    "ext-data-api",
    "user-mgmt-api",
    "roles-api",
    "tenant-cnames-api",
    "policy-api",
    "security-api",
    "tenant-config-api",
    "up-rest-api",
    "saas-manage-api",
    "u2f-api",
    "device-api",
    "mobile-api",
    "org-api",
    "user-api",
    "sysinfo-api",
    "job-flow-api",
    "task-api",
]


def _extract_react_router_data(html: str) -> Optional[dict]:
    """
    Extract the React Router context data from page HTML.

    Looks for window.__reactRouterContext or similar embedded JSON in script tags
    that contains the OpenAPI spec / endpoint definitions.
    """
    # Strategy 1: Look for __reactRouterContext assignment in script tags
    # The data is typically in: window.__reactRouterContext = {...}
    match = re.search(
        r'window\.__reactRouterContext\s*=\s*({.+?})\s*;?\s*(?:</script>|$)',
        html,
        re.DOTALL,
    )
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Strategy 2: Look for large JSON blobs in script tags that contain API paths
    for script_match in re.finditer(
        r'<script[^>]*>\s*((?:window\.|var |let |const ).*?)\s*</script>',
        html,
        re.DOTALL,
    ):
        block = script_match.group(1)
        # Look for JSON objects that contain OpenAPI-like data
        for json_match in re.finditer(r'=\s*({["\{].{500,}})\s*;?\s*$', block, re.DOTALL | re.MULTILINE):
            try:
                data = json.loads(json_match.group(1))
                # Check if this looks like it contains API data
                if _has_api_data(data):
                    return data
            except json.JSONDecodeError:
                continue

    # Strategy 3: Look for embedded JSON in data attributes or type="application/json" scripts
    for match in re.finditer(
        r'<script[^>]*type="application/json"[^>]*>\s*({.+?})\s*</script>',
        html,
        re.DOTALL,
    ):
        try:
            data = json.loads(match.group(1))
            if _has_api_data(data):
                return data
        except json.JSONDecodeError:
            continue

    return None


def _has_api_data(data: dict, depth: int = 0) -> bool:
    """Check if a JSON structure contains OpenAPI / endpoint data."""
    if depth > 5:
        return False
    if isinstance(data, dict):
        # Check for OpenAPI paths object
        if "paths" in data and isinstance(data["paths"], dict):
            return True
        # Check for operations array
        if "operations" in data and isinstance(data["operations"], list):
            return True
        # Check for loaderData (React Router)
        if "loaderData" in data:
            return True
        # Check for content with paths
        if "content" in data and isinstance(data["content"], dict):
            if "paths" in data["content"]:
                return True
        for val in data.values():
            if isinstance(val, dict) and _has_api_data(val, depth + 1):
                return True
    return False


def _extract_openapi_endpoints(data: dict) -> list:
    """
    Extract endpoints from an OpenAPI-like structure.

    Walks the data looking for 'paths' objects and extracts method/path/summary.
    """
    endpoints = []

    def _walk(obj, depth=0):
        if depth > 10 or not isinstance(obj, dict):
            return

        # If this dict has a "paths" key with HTTP methods inside
        paths = obj.get("paths")
        if isinstance(paths, dict):
            for path, path_item in paths.items():
                if not isinstance(path_item, dict) or not path.startswith("/"):
                    continue
                for method in ("get", "post", "put", "delete", "patch"):
                    op = path_item.get(method)
                    if isinstance(op, dict):
                        endpoints.append({
                            "method": method.upper(),
                            "path": path,
                            "summary": op.get("summary") or op.get("description", ""),
                            "operationId": op.get("operationId", ""),
                            "tag": op.get("tags", [""])[0] if op.get("tags") else "",
                            "deprecated": 1 if op.get("deprecated") else 0,
                        })

        # If this dict has "operations" array (React Router TOC format)
        operations = obj.get("operations")
        if isinstance(operations, list):
            for op in operations:
                if isinstance(op, dict) and "method" in op and "path" in op:
                    endpoints.append({
                        "method": op["method"].upper(),
                        "path": op["path"],
                        "summary": op.get("summary", ""),
                        "operationId": op.get("operationId", ""),
                        "tag": op.get("tag", ""),
                        "deprecated": 0,
                    })

        # Recurse into all dict values
        for val in obj.values():
            if isinstance(val, dict):
                _walk(val, depth + 1)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        _walk(item, depth + 1)

    _walk(data)

    # Deduplicate
    seen = set()
    unique = []
    for ep in endpoints:
        key = f"{ep['method']}:{ep['path']}"
        if key not in seen:
            seen.add(key)
            unique.append(ep)

    return unique


def _extract_toc_from_react_context(data: dict) -> Optional[list]:
    """
    Extract the table of contents (categories + operations) from
    the React Router loader data.

    The TOC is typically in:
    loaderData['routes/_docs.$productname.$/_index'].sections[0].items[0].tableOfContents
    """
    def _find_toc(obj, depth=0):
        if depth > 8 or not isinstance(obj, dict):
            return None

        # Direct match
        if "tableOfContents" in obj:
            toc = obj["tableOfContents"]
            if isinstance(toc, list) and len(toc) > 0:
                return toc

        # Look in sections
        sections = obj.get("sections")
        if isinstance(sections, list):
            for section in sections:
                if isinstance(section, dict):
                    items = section.get("items", [])
                    if isinstance(items, list):
                        for item in items:
                            result = _find_toc(item, depth + 1)
                            if result:
                                return result

        # Recurse
        for val in obj.values():
            if isinstance(val, dict):
                result = _find_toc(val, depth + 1)
                if result:
                    return result
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        result = _find_toc(item, depth + 1)
                        if result:
                            return result

        return None

    return _find_toc(data)


def _fetch_identity_data_url(url: str, session: requests.Session) -> list:
    """
    Try React Router v7 .data URL to get loader data.

    React Router v7 serves loader data at {page_url}.data — this returns
    either JSON or a turbo-stream response containing the OpenAPI spec
    for that category page, without needing client-side JS execution.
    """
    data_url = f"{url}.data"
    try:
        resp = session.get(data_url, headers={
            **HEADERS,
            "Accept": "application/json, text/x-turbo, */*",
        }, timeout=30)

        if resp.status_code not in (200, 202):
            return []

        content_type = resp.headers.get("content-type", "")
        body = resp.text

        # Strategy A: Direct JSON response
        if "json" in content_type:
            try:
                data = resp.json()
                endpoints = _extract_openapi_endpoints(data)
                if endpoints:
                    return endpoints
                # Also try walking nested structures
                if _has_api_data(data):
                    return _extract_openapi_endpoints(data)
            except json.JSONDecodeError:
                pass

        # Strategy B: Turbo-stream response — extract JSON from <template> tags
        # Format: <turbo-stream ...><template>JSON_DATA</template></turbo-stream>
        for tmpl_match in re.finditer(
            r'<template[^>]*>(.*?)</template>',
            body,
            re.DOTALL,
        ):
            chunk = tmpl_match.group(1).strip()
            if not chunk or not chunk.startswith(("{", "[")):
                continue
            try:
                data = json.loads(chunk)
                endpoints = _extract_openapi_endpoints(data)
                if endpoints:
                    return endpoints
            except json.JSONDecodeError:
                continue

        # Strategy C: Multipart text/x-turbo — sections separated by boundary
        # or newline-delimited JSON chunks
        if "turbo" in content_type or body.startswith("--"):
            for json_match in re.finditer(r'(\{["\w].{200,}?\})\s*(?:\n|$)', body, re.DOTALL):
                try:
                    data = json.loads(json_match.group(1))
                    endpoints = _extract_openapi_endpoints(data)
                    if endpoints:
                        return endpoints
                except json.JSONDecodeError:
                    continue

        # Strategy D: Look for OpenAPI paths anywhere in the response body
        for json_match in re.finditer(r'("paths"\s*:\s*\{.+?\})\s*\}', body, re.DOTALL):
            try:
                wrapped = "{" + json_match.group(1) + "}"
                data = json.loads(wrapped)
                endpoints = _extract_openapi_endpoints(data)
                if endpoints:
                    return endpoints
            except json.JSONDecodeError:
                continue

    except requests.RequestException:
        pass

    return []


def _parse_identity_html_endpoints(html: str) -> list:
    """Fallback: extract METHOD /path patterns from raw HTML text."""
    endpoints = []
    seen = set()

    for match in re.finditer(
        r"(GET|POST|PUT|DELETE|PATCH)\s+(/[^\s<\"\']+)",
        html,
    ):
        method, path = match.groups()
        path = re.sub(r"[.;,]+$", "", path)
        key = f"{method}:{path}"
        if key not in seen:
            seen.add(key)
            endpoints.append({
                "method": method,
                "path": path.strip(),
                "summary": "",
                "operationId": "",
                "tag": "",
                "deprecated": 0,
            })

    return endpoints


def fetch_identity_api(session: requests.Session) -> dict:
    """
    Fetch all Identity API data from api-docs.cyberark.com.

    The Identity docs are a React Router v7 SPA hosted on SwaggerHub Portal.
    The API data is rendered client-side by JavaScript — the initial HTML returned
    by the server is a shell that does NOT contain endpoint data.

    Strategy (in order):
    1. Check for existing data/identity-api.json (browser-extracted seed data)
    2. Try fetching page HTML and extracting embedded React Router context (SSR)
    3. Fallback: regex METHOD /path from HTML text
    4. If all fail: log instructions for browser-based extraction

    To extract fresh data, open the Identity docs in Chrome and run in console:
        const ld = window.__reactRouterContext.state.loaderData;
        const toc = ld['routes/_docs.$productname/_index'].sections.items[0].tableOfContents;
        // ... (see README for full extraction script)
    """
    log.info("=" * 50)
    log.info("Fetching CyberArk Identity API")
    log.info("=" * 50)

    # Strategy 1: Check for existing seed data (browser-extracted)
    seed_path = DATA_DIR / "identity-api.json"
    if seed_path.exists():
        log.info(f"Found existing seed data: {seed_path}")
        with open(seed_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
        ep_count = len(existing.get("endpoints", []))
        cat_count = len(existing.get("categories", []))
        if ep_count > 0:
            log.info(f"Using existing data: {cat_count} categories, {ep_count} endpoints")
            # Update the fetched_at timestamp
            existing.setdefault("metadata", {})["fetched_at"] = now_iso()
            return existing
        log.warning("Existing seed data has 0 endpoints — trying live fetch")

    # Strategy 2: Try live extraction from page HTML
    log.info("Attempting live extraction from api-docs.cyberark.com...")
    log.info("(Note: This site is a client-side React SPA. Live extraction may")
    log.info(" return 0 endpoints. See README for browser-based extraction.)")

    categories = []
    all_endpoints = []

    for i, slug in enumerate(IDENTITY_CATEGORY_SLUGS, 1):
        log.info(f"[{i}/{len(IDENTITY_CATEGORY_SLUGS)}] Fetching: {slug}")

        url = f"{IDENTITY_DOCS}/{slug}"
        html = fetch_page(url, session)
        if not html:
            continue

        page_endpoints = []

        # Strategy 2a: Try React Router context extraction (works if server does SSR)
        rr_data = _extract_react_router_data(html)
        if rr_data:
            page_endpoints = _extract_openapi_endpoints(rr_data)
            if page_endpoints:
                log.info(f"  React Router SSR: {len(page_endpoints)} endpoints")

        # Strategy 2b: Try .data URL (React Router v7 loader data endpoint)
        if not page_endpoints:
            page_endpoints = _fetch_identity_data_url(url, session)
            if page_endpoints:
                log.info(f"  .data URL: {len(page_endpoints)} endpoints")

        # Strategy 2c: Fallback regex extraction from HTML text
        if not page_endpoints:
            page_endpoints = _parse_identity_html_endpoints(html)
            if page_endpoints:
                log.info(f"  HTML regex: {len(page_endpoints)} endpoints")

        if not page_endpoints:
            log.info(f"  Category '{slug}': 0 endpoints")

        title = slug.replace("-", " ").title().replace("Api", "API")
        cat_id = len(categories) + 1
        categories.append({
            "name": title,
            "slug": slug,
            "docs_url": url,
            "endpoint_count": len(page_endpoints),
        })

        for ep in page_endpoints:
            key = f"{ep['method']}:{ep['path']}"
            if key not in {f"{e['method']}:{e['path']}" for e in all_endpoints}:
                all_endpoints.append({
                    "method": ep["method"],
                    "path": ep["path"],
                    "summary": ep.get("summary", ""),
                    "category_id": cat_id,
                    "tag_id": None,
                    "deprecated": ep.get("deprecated", 0),
                    "base_url": IDENTITY_API_BASE_URL,
                    "doc_reference": url,
                })

        time.sleep(REQUEST_DELAY)

    data = {
        "metadata": {
            "name": "CyberArk Identity API",
            "description": "CyberArk Identity Platform REST API endpoints",
            "default_base_url": IDENTITY_API_BASE_URL,
            "source": IDENTITY_DOCS,
            "fetched_at": now_iso(),
        },
        "categories": categories,
        "tags": [],
        "endpoints": all_endpoints,
    }

    total = len(all_endpoints)
    log.info(f"Identity total: {len(categories)} categories, {total} endpoints")

    if total == 0:
        log.warning("")
        log.warning("=" * 50)
        log.warning("Identity API: 0 endpoints extracted from live site.")
        log.warning("This is expected — the site is a client-side React SPA")
        log.warning("that requires JavaScript execution to render API data.")
        log.warning("")
        log.warning("To populate Identity data, extract it from Chrome:")
        log.warning("  1. Open https://api-docs.cyberark.com/identity-docs-api/docs/oauth2-api")
        log.warning("  2. Open DevTools Console (F12)")
        log.warning("  3. Run the extraction script (see README)")
        log.warning("  4. Save the downloaded identity-api.json to data/")
        log.warning("  5. Re-run this script")
        log.warning("=" * 50)

    return data


# ---------------------------------------------------------------------------
# MadCap Flare TOC Parser (Privilege Cloud & PAM Self-Hosted)
# ---------------------------------------------------------------------------
#
# CyberArk docs.cyberark.com uses MadCap Flare HTML5 TopNav.
# Content pages are .htm files that return 200 for GET but 404 for HEAD.
# The table of contents is stored in static JS files using AMD define() format:
#
#   Main TOC file: Data/Tocs/{prefix}.js
#     Contains: define({"numchunks": N, "prefix": "Prefix_Chunk"})
#
#   Chunk files: Data/Tocs/{prefix}_Chunk{n}.js  (n = 0..N-1)
#     Contains: define({"/content/path.htm": {i:[idx], t:["Title"], b:[""]}, ...})
#
# We fetch the main TOC JS, parse chunk count, then fetch each chunk to get
# all page paths. We filter for API-related paths and fetch those pages.

PCLOUD_BASE = "https://docs.cyberark.com/privilege-cloud-shared-services/latest/en"
PCLOUD_DEFAULT_BASE_URL = "https://{subdomain}.privilegecloud.cyberark.cloud"
PCLOUD_TOC_MAIN = f"{PCLOUD_BASE}/Data/Tocs/PrivCloud__Privilege_Cloud_Online_Help.js"
PCLOUD_TOC_CHUNK_PREFIX = "PrivCloud__Privilege_Cloud_Online_Help_Chunk"

PAM_BASE = "https://docs.cyberark.com/pam-self-hosted/latest/en"
PAM_DEFAULT_BASE_URL = "https://{pvwa_host}"
PAM_TOC_MAIN = f"{PAM_BASE}/Data/Tocs/PAS__OnlineHelp__PAS_OnlineHelp.js"
PAM_TOC_CHUNK_PREFIX = "PAS__OnlineHelp__PAS_OnlineHelp_Chunk"

# Path patterns that indicate API endpoint documentation
API_PATH_PATTERNS = re.compile(
    r"(?i)("
    r"webservices|rest[-_ ]?api|sdk|privilegecloudapis|ispss|"
    r"developer|privilege.cloud|"
    r"rest.api|web.services"
    r")",
)

# Path patterns to exclude (overview/concept pages, not actual endpoint docs)
API_PATH_EXCLUDES = re.compile(
    r"(?i)("
    r"implementing.privileged|overview|getting.started|"
    r"introduction|whats.new|release.notes|"
    r"pam-sdk|concepts?/|tutorials?/"
    r")",
)


def _parse_toc_main_js(js_text: str) -> tuple:
    """
    Parse the main MadCap Flare TOC JS file.

    Returns (num_chunks, chunk_prefix) or (0, "") on failure.

    Handles multiple known formats:
      Old:  define({numchunks:7, prefix:'Prefix_Chunk'})
      New:  define({"numchunks":7, "prefix":"Prefix_Chunk"})
      Alt:  define({"chunks":7, "chunkPrefix":"Prefix_Chunk"})
      Flat: define({'/content/page.htm':{...}, ...})  (no chunks — single file)
    """
    # --- Try chunked formats first ---

    # Pattern 1: numchunks (quoted or unquoted key, any quote style on value)
    nc_match = re.search(r'["\']?numchunks["\']?\s*:\s*(\d+)', js_text, re.I)
    pf_match = re.search(r'["\']?prefix["\']?\s*:\s*["\']([^"\']+)["\']', js_text, re.I)

    # Pattern 2: chunks / chunkPrefix (alternate naming)
    if not nc_match:
        nc_match = re.search(r'["\']?chunks["\']?\s*:\s*(\d+)', js_text, re.I)
    if not pf_match:
        pf_match = re.search(r'["\']?chunkPrefix["\']?\s*:\s*["\']([^"\']+)["\']', js_text, re.I)

    # Pattern 3: numChunks (camelCase)
    if not nc_match:
        nc_match = re.search(r'["\']?numChunks["\']?\s*:\s*(\d+)', js_text, re.I)

    numchunks = int(nc_match.group(1)) if nc_match else 0
    prefix = pf_match.group(1) if pf_match else ""

    if numchunks:
        log.info(f"  Parsed TOC: numchunks={numchunks}, prefix='{prefix}'")
        return numchunks, prefix

    # --- Check if this is a flat TOC (all entries in one file, no chunks) ---
    if _parse_toc_chunk_js(js_text):
        log.info("  Parsed TOC: flat format (no chunks, entries in main file)")
        return -1, ""  # -1 signals "flat format — main file IS the data"

    log.warning(f"  Failed to parse TOC JS (length={len(js_text)})")
    # Log enough to debug but not flood
    log.info(f"  First 500 chars: {js_text[:500]}")

    return 0, ""


def _parse_toc_chunk_js(js_text: str) -> dict:
    """
    Parse a MadCap Flare TOC chunk JS file.

    Returns dict of {"/content/path.htm": {"title": "Page Title"}} entries.

    The file uses JS object literal with single-quoted strings:
        define({'/content/path.htm':{i:[0],t:['Title'],b:['']}, ...})
    """
    pages = {}

    # Extract the define() argument
    match = re.search(r'define\(\s*({.+})\s*\)', js_text, re.DOTALL)
    if not match:
        return pages

    raw = match.group(1)

    # Parse individual entries — keys use single quotes, values use single quotes
    # Pattern: '/content/path.htm':{i:[N],t:['Title'],b:['']}
    for entry_match in re.finditer(
        r"'([^']+\.htm)'\s*:\s*\{([^}]*)\}",
        raw,
    ):
        path = entry_match.group(1)
        props = entry_match.group(2)

        # Extract title from t:['...'] (single-quoted)
        title_match = re.search(r"t\s*:\s*\[\s*'([^']*)'\s*\]", props)
        title = title_match.group(1) if title_match else ""

        pages[path] = {"title": title}

    # Also try double-quoted format as fallback (in case format varies)
    if not pages:
        for entry_match in re.finditer(
            r'"([^"]+\.htm)"\s*:\s*\{([^}]*)\}',
            raw,
        ):
            path = entry_match.group(1)
            props = entry_match.group(2)
            title_match = re.search(r't\s*:\s*\[\s*["\']([^"\']*)["\']', props)
            title = title_match.group(1) if title_match else ""
            pages[path] = {"title": title}

    return pages


def fetch_toc_pages(
    toc_main_url: str,
    toc_chunk_prefix: str,
    base_url: str,
    session: requests.Session,
) -> list:
    """
    Fetch all page paths from MadCap Flare TOC JS files.

    Returns list of (page_path, title) tuples for API-related pages.
    """
    log.info(f"Fetching TOC: {toc_main_url}")
    main_js = fetch_page(toc_main_url, session)
    if not main_js:
        log.error("Failed to fetch TOC main JS file")
        return []

    numchunks, prefix = _parse_toc_main_js(main_js)

    all_pages = {}
    toc_dir = toc_main_url.rsplit("/", 1)[0]

    if numchunks == -1:
        # Flat format: the main JS file itself contains all entries
        log.info("TOC is flat (no chunks) — parsing main file directly")
        all_pages = _parse_toc_chunk_js(main_js)

    elif numchunks > 0:
        # Standard chunked format
        if not prefix:
            prefix = toc_chunk_prefix
        log.info(f"TOC has {numchunks} chunks (prefix: {prefix})")

        for i in range(numchunks):
            chunk_url = f"{toc_dir}/{prefix}{i}.js"
            log.info(f"  Fetching chunk {i}/{numchunks - 1}...")
            chunk_js = fetch_page(chunk_url, session)
            if chunk_js:
                pages = _parse_toc_chunk_js(chunk_js)
                all_pages.update(pages)
                log.info(f"    {len(pages)} pages in chunk {i}")
            else:
                log.warning(f"    Failed to fetch chunk {i}")
            time.sleep(REQUEST_DELAY * 0.5)

    else:
        # numchunks=0: Parsing failed. Try probing chunks directly with the
        # expected prefix — maybe the main JS changed format but chunks still exist.
        log.warning("TOC main JS parse failed — probing chunks directly...")
        prefix = toc_chunk_prefix
        for i in range(20):  # Try up to 20 chunks
            chunk_url = f"{toc_dir}/{prefix}{i}.js"
            chunk_js = fetch_page(chunk_url, session)
            if not chunk_js:
                if i == 0:
                    log.warning(f"  Chunk 0 not found at {chunk_url}")
                break
            pages = _parse_toc_chunk_js(chunk_js)
            all_pages.update(pages)
            log.info(f"  Probed chunk {i}: {len(pages)} pages")
            time.sleep(REQUEST_DELAY * 0.5)

        if not all_pages:
            log.warning("Direct chunk probing found nothing")

    log.info(f"Total pages in TOC: {len(all_pages)}")

    # Filter for API-related pages
    api_pages = []
    for path, info in all_pages.items():
        path_lower = path.lower().replace("%20", " ")
        if API_PATH_PATTERNS.search(path_lower) and not API_PATH_EXCLUDES.search(path_lower):
            api_pages.append((path, info.get("title", "")))

    log.info(f"API-related pages: {len(api_pages)}")
    return api_pages


def parse_doc_endpoint_page(html: str, url: str) -> list:
    """
    Parse a single MadCap Flare API endpoint page.

    Returns list of endpoint dicts found on the page.
    """
    soup = BeautifulSoup(html, "html.parser")
    endpoints = []

    # Check for error page
    title = soup.find("title")
    if title and "404" in title.get_text():
        return []

    text = soup.get_text(" ", strip=True)

    # Strategy 1: Look for explicit method+URL patterns
    # CyberArk docs show: METHOD <URL>  or  METHOD /PasswordVault/...
    method_patterns = re.findall(
        r"(GET|POST|PUT|DELETE|PATCH)\s+(/(?:PasswordVault|api|OAuth2|privilegecloud)[^\s<\"\']+)",
        text,
        re.IGNORECASE,
    )
    for method, path in method_patterns:
        path = re.sub(r"[.;,)+]+$", "", path)
        path = path.split("?")[0]
        if path not in [e["path"] for e in endpoints]:
            summary = _extract_summary(soup, method, path)
            endpoints.append({
                "method": method.upper(),
                "path": path,
                "summary": summary,
            })

    # Strategy 2: Look for URL patterns in code blocks
    if not endpoints:
        for code_block in soup.find_all(["code", "pre"]):
            code_text = code_block.get_text(strip=True)
            url_match = re.search(r"(/(?:PasswordVault|api)/[^\s\"\'<>]+)", code_text)
            method_match = re.search(r"(GET|POST|PUT|DELETE|PATCH)", code_text, re.IGNORECASE)
            if url_match:
                path = url_match.group(1).split("?")[0]
                method = method_match.group(1).upper() if method_match else "POST"
                if path not in [e["path"] for e in endpoints]:
                    summary = _extract_summary(soup, method, path)
                    endpoints.append({
                        "method": method,
                        "path": path,
                        "summary": summary,
                    })

    # Strategy 3: Broader path pattern matching (REST API URLs)
    if not endpoints:
        for match in re.finditer(
            r"(GET|POST|PUT|DELETE|PATCH)\s+(/[A-Za-z0-9/_\{\}\-%.]+)",
            text,
        ):
            method, path = match.groups()
            path = re.sub(r"[.;,)+]+$", "", path)
            if len(path) > 5 and path not in [e["path"] for e in endpoints]:
                endpoints.append({
                    "method": method.upper(),
                    "path": path,
                    "summary": _extract_summary(soup, method, path),
                })

    return endpoints


def _extract_summary(soup, method: str, path: str) -> str:
    """Extract a summary/description from the page for a given endpoint."""
    title = soup.find("title")
    if title:
        text = title.get_text(strip=True)
        text = re.sub(r"\s*\|.*$", "", text)
        text = re.sub(r"\s*-\s*CyberArk.*$", "", text, flags=re.I)
        if text and len(text) > 3:
            return text

    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)[:200]

    return ""


def _normalize_category(page_path: str, link_text: str) -> str:
    """Normalize category names from URL paths and page titles."""
    path_lower = page_path.lower().replace("%20", " ").replace("_", " ")

    category_map = {
        "account": "Accounts",
        "safe": "Safes",
        "safemember": "Safe Members",
        "safe-member": "Safe Members",
        "safe member": "Safe Members",
        "platform": "Platforms",
        "session": "Monitor Sessions",
        "psm": "Session Management",
        "auth": "Authentication",
        "logon": "Authentication",
        "logoff": "Authentication",
        "bulk": "Bulk Upload Accounts",
        "discovery": "Account Discovery",
        "discovered": "Account Discovery",
        "accountgroup": "Account Groups",
        "server": "Server",
        "verify": "Server",
        "health": "System Health",
        "component": "System Health",
        "application": "Applications",
        "request": "Requests",
        "ssh": "Private SSH Authentication",
        "mfacaching": "Private SSH Authentication",
        "allowlist": "IP Allowlist",
        "user": "User Management",
        "license": "User Management",
        "ticketing": "Custom Ticketing Systems",
        "ldap": "LDAP Integration",
        "pta": "PTA Security Events",
        "securityevent": "PTA Security Events",
        "oidc": "OpenID Connect Providers",
        "oauth": "OAuth 2.0 Providers",
        "fido": "FIDO2 Devices",
        "opm": "OPM Rules",
        "branding": "Branding & Themes",
        "theme": "Branding & Themes",
        "group": "Groups",
        "linked": "Linked Accounts",
        "policy": "Policies",
        "report": "Reports & Tasks",
        "task": "Reports & Tasks",
        "security": "Security Settings",
        "jit": "Just In Time Access",
        "ispss": "ISPSS Services",
        "privilegecloud": "Privilege Cloud APIs",
    }

    path_normalized = path_lower.replace(" ", "").replace("-", "")
    for pattern, cat_name in category_map.items():
        if pattern.replace("-", "") in path_normalized:
            return cat_name

    if link_text and len(link_text) > 2:
        return link_text

    return "General"


def fetch_docs_api(
    name: str,
    toc_main_url: str,
    toc_chunk_prefix: str,
    base_url: str,
    default_api_base: str,
    session: requests.Session,
) -> dict:
    """Fetch API data from a docs.cyberark.com documentation set using MadCap Flare TOC."""
    log.info("=" * 50)
    log.info(f"Fetching {name}")
    log.info("=" * 50)

    # Step 1: Get all API page paths from TOC
    api_pages = fetch_toc_pages(toc_main_url, toc_chunk_prefix, base_url, session)

    if not api_pages:
        log.warning("No API pages found in TOC! Trying fallback discovery...")
        api_pages = _fallback_content_discovery_madcap(base_url, session)

    # Step 2: Fetch each page and extract endpoints
    categories = {}
    all_endpoints = []

    for i, (page_path, page_title) in enumerate(api_pages, 1):
        # Build the full URL
        if page_path.startswith("/"):
            page_path = page_path.lstrip("/")
        url = f"{base_url}/{page_path}"

        log.info(f"[{i}/{len(api_pages)}] {page_title or page_path[-50:]}")

        html = fetch_page(url, session)
        if not html:
            continue

        endpoints = parse_doc_endpoint_page(html, url)
        if not endpoints:
            log.debug(f"  No endpoints found on {url}")
            continue

        cat = _normalize_category(page_path, page_title)
        if cat not in categories:
            categories[cat] = {"name": cat, "endpoint_count": 0}

        for ep in endpoints:
            key = f"{ep['method']}:{ep['path']}"
            if key not in {f"{e['method']}:{e['path']}" for e in all_endpoints}:
                cat_id = list(categories.keys()).index(cat) + 1
                all_endpoints.append({
                    "method": ep["method"],
                    "path": ep["path"],
                    "summary": ep.get("summary", ""),
                    "category_id": cat_id,
                    "base_url": default_api_base,
                    "doc_url": url,
                    "deprecated": 0,
                })
                categories[cat]["endpoint_count"] += 1
                log.info(f"  {ep['method']} {ep['path']}")

        time.sleep(REQUEST_DELAY)

    cat_list = [
        {"name": c["name"], "endpoint_count": c["endpoint_count"]}
        for c in categories.values()
    ]

    data = {
        "metadata": {
            "name": name,
            "description": f"{name} REST API endpoints",
            "default_base_url": default_api_base,
            "source": base_url,
            "fetched_at": now_iso(),
        },
        "categories": cat_list,
        "endpoints": all_endpoints,
    }

    total = len(all_endpoints)
    log.info(f"{name} total: {len(cat_list)} categories, {total} endpoints")

    if total == 0:
        log.warning(f"No endpoints found for {name}!")
        log.warning("Check if TOC JS files are still at the expected URLs.")

    return data


def _fallback_content_discovery_madcap(base_url: str, session: requests.Session) -> list:
    """Fallback: try fetching known content paths if TOC parsing fails."""
    pages = []
    seen_paths = set()

    # Comprehensive list of known API doc page paths (works for both pcloud and pam)
    known_paths = [
        # Index / overview pages (these link to many sub-pages)
        "content/webservices/implementing%20privileged%20account%20security%20web%20services%20.htm",
        "content/sdk/rest-api-get-platforms.htm",
        "content/privilegecloudapis/issprequests.htm",
        # Authentication
        "content/webservices/ISP-Auth-APIs.htm",
        "content/webservices/rest%20web%20services%20api%20-%20authentication.htm",
        "content/webservices/cyberark%20identity%20-%20logon.htm",
        "content/webservices/logoff.htm",
        # Accounts
        "content/webservices/add%20account.htm",
        "content/webservices/delete%20account.htm",
        "content/webservices/get%20accounts.htm",
        "content/webservices/get%20account%20details.htm",
        "content/webservices/update%20account.htm",
        "content/webservices/check%20in%20exclusive%20account.htm",
        "content/webservices/change-credentials-immediately.htm",
        "content/webservices/verify-credentials.htm",
        "content/webservices/reconcile-credentials.htm",
        "content/webservices/connect-using-PSM.htm",
        "content/webservices/get-password-value.htm",
        "content/webservices/getaccountactivity.htm",
        "content/webservices/link-accounts.htm",
        "content/webservices/unlink-accounts.htm",
        "content/webservices/getlinkedaccounts.htm",
        # Safes
        "content/webservices/add%20safe.htm",
        "content/webservices/delete%20safe.htm",
        "content/webservices/get%20safes.htm",
        "content/webservices/get%20safe%20details.htm",
        "content/webservices/update%20safe.htm",
        "content/webservices/add%20safe%20member.htm",
        "content/webservices/delete%20safe%20member.htm",
        "content/webservices/get%20safe%20members.htm",
        "content/webservices/update%20safe%20member.htm",
        # Platforms
        "content/webservices/getplatformdetails.htm",
        "content/sdk/rest-api-get-platforms.htm",
        "content/webservices/getplatforms.htm",
        "content/webservices/activate-platform.htm",
        "content/webservices/deactivate-platform.htm",
        "content/webservices/rest-api-duplicate-platforms.htm",
        "content/webservices/rest-api-delete-platforms.htm",
        # Users
        "content/webservices/add-user.htm",
        "content/webservices/delete-user.htm",
        "content/webservices/get-users.htm",
        "content/webservices/get-user-details.htm",
        "content/webservices/update-user.htm",
        "content/webservices/reset-user-password.htm",
        "content/webservices/activate-user.htm",
        # Groups
        "content/webservices/add-group.htm",
        "content/webservices/delete-group.htm",
        "content/webservices/get-groups.htm",
        "content/webservices/add-member-to-group.htm",
        # Sessions / PSM
        "content/webservices/getrecordings.htm",
        "content/webservices/get-all-monitored-sessions.htm",
        "content/webservices/getsessionactivities.htm",
        # Bulk operations
        "content/webservices/bulk-upload-accounts.htm",
        "content/webservices/get-all-bulk-account-actions.htm",
        "content/webservices/get-bulk-account-action.htm",
        # Server / system health
        "content/webservices/verify.htm",
        "content/webservices/get-server.htm",
        "content/webservices/getsystemhealthsummary.htm",
        "content/webservices/getsystemhealthcomponents.htm",
        # Account discovery
        "content/webservices/getdiscoveredrules.htm",
        "content/webservices/adddiscoveredrule.htm",
        # Account groups
        "content/webservices/addaccountgroup.htm",
        "content/webservices/getaccountgroups.htm",
        "content/webservices/addaccounttoaccountgroup.htm",
        # Applications
        "content/webservices/add-application.htm",
        "content/webservices/get-applications.htm",
        "content/webservices/delete-application.htm",
        # Requests
        "content/webservices/myrequests.htm",
        "content/webservices/incomingrequests.htm",
        "content/webservices/confirmingrequest.htm",
        "content/webservices/rejectrequest.htm",
        # OPM / JIT
        "content/webservices/getopmrules.htm",
        "content/webservices/jit-access.htm",
        # LDAP / OIDC
        "content/webservices/ldapintegration.htm",
        "content/webservices/oidcprovider.htm",
    ]

    def _add_page(rel_path: str, title: str):
        if rel_path not in seen_paths:
            seen_paths.add(rel_path)
            pages.append((rel_path, title))

    for path in known_paths:
        url = f"{base_url}/{path}"
        html = fetch_page(url, session)
        if not html:
            continue

        soup = BeautifulSoup(html, "html.parser")

        # Follow links to discover more pages
        for a in soup.find_all("a", href=True):
            href = a["href"]
            full_url = urljoin(url, href).split("#")[0]
            if full_url.endswith(".htm") and base_url in full_url:
                link_text = a.get_text(strip=True)
                rel_path = full_url.replace(f"{base_url}/", "")
                _add_page(rel_path, link_text)

        # Also add the current page itself (it may contain endpoints)
        _add_page(path, "")

        time.sleep(REQUEST_DELAY)

    log.info(f"Fallback discovery found {len(pages)} pages")
    return pages


# ---------------------------------------------------------------------------
# Privilege Cloud API
# ---------------------------------------------------------------------------

def fetch_privilege_cloud_api(session: requests.Session) -> dict:
    """Fetch Privilege Cloud API data."""
    return fetch_docs_api(
        name="CyberArk Privilege Cloud API",
        toc_main_url=PCLOUD_TOC_MAIN,
        toc_chunk_prefix=PCLOUD_TOC_CHUNK_PREFIX,
        base_url=PCLOUD_BASE,
        default_api_base=PCLOUD_DEFAULT_BASE_URL,
        session=session,
    )


# ---------------------------------------------------------------------------
# PAM Self-Hosted API
# ---------------------------------------------------------------------------

def fetch_pam_api(session: requests.Session) -> dict:
    """Fetch PAM Self-Hosted API data."""
    return fetch_docs_api(
        name="CyberArk PAM Self-Hosted API",
        toc_main_url=PAM_TOC_MAIN,
        toc_chunk_prefix=PAM_TOC_CHUNK_PREFIX,
        base_url=PAM_BASE,
        default_api_base=PAM_DEFAULT_BASE_URL,
        session=session,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

FETCHERS = {
    "identity": ("identity-api.json", fetch_identity_api),
    "pcloud": ("privilege-cloud-api.json", fetch_privilege_cloud_api),
    "pam": ("pam-selfhosted-api.json", fetch_pam_api),
}


def main():
    parser = argparse.ArgumentParser(
        description="Fetch CyberArk API data from live docs and build SQLite databases"
    )
    parser.add_argument(
        "targets",
        nargs="*",
        choices=list(FETCHERS.keys()) + [[]],
        default=[],
        help="Which APIs to fetch (default: all)",
    )
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help="Only fetch JSON data, skip database build",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=SCRIPT_DIR,
        help="Output directory for .db files (default: script directory)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()
    targets = args.targets if args.targets else list(FETCHERS.keys())

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    session = requests.Session()

    print("=" * 60)
    print("  CyberArk API Data Fetcher")
    print("=" * 60)

    # Step 1: Fetch JSON data from live sites
    for key in targets:
        json_file, fetcher = FETCHERS[key]
        data = fetcher(session)
        save_json(data, json_file)

    print("\n  JSON seed files saved to data/")

    # Step 2: Build SQLite databases (unless --fetch-only)
    if not args.fetch_only:
        print()
        from build_databases import build_database, verify_database, DB_CONFIGS

        args.output_dir.mkdir(parents=True, exist_ok=True)

        print("=" * 60)
        print("  Building SQLite databases")
        print("=" * 60)

        failures = []
        for key in targets:
            try:
                build_database(key, args.output_dir)
                db_path = args.output_dir / DB_CONFIGS[key]["db_file"]
                if not verify_database(db_path):
                    failures.append(key)
            except Exception as e:
                log.error(f"Failed to build {key} database: {e}")
                failures.append(key)

        if failures:
            log.warning(f"Databases with issues: {', '.join(failures)}")
            log.warning("Re-run with --verbose for details, or see README for manual extraction.")

    print("\n" + "=" * 60)
    print("  Done!")
    print("=" * 60)


if __name__ == "__main__":
    main()
