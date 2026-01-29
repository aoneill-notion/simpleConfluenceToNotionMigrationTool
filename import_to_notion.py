#!/usr/bin/env python3
"""
Create a Notion page from a single Confluence-exported HTML file.

Dependencies:
    - requests (pip install requests)
    - beautifulsoup4 (already needed by migrate_page.py)

Usage (dry run by default):
    python import_to_notion.py --html path/to/page.html --assets-root /path/to/export --parent-page-id YOUR_PARENT_PAGE_ID --dry-run

To actually create the page, drop --dry-run and provide NOTION_TOKEN in env or --token:
    NOTION_TOKEN=secret_xxx python import_to_notion.py --html ... --assets-root ... --parent-page-id ...

Notes:
    - Local image/file paths are not directly uploadable via the Notion API.
      For now, local files are rendered as a fallback paragraph: "Attachment: <path>".
      If you have externally reachable URLs, pass them through in the HTML so the
      generated image blocks use external URLs instead.
"""

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

import migrate_page
from migrate_page import make_text_fragment


NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def load_env_file(path: str = ".env") -> None:
    """Lightweight .env loader (avoids extra dependency)."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip("'").strip('"')
            os.environ.setdefault(key, val)


def load_blocks_from_html(html_path: str, assets_root: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Reuse migrate_page conversion to produce title and blocks."""
    with open(html_path, "r", encoding="utf-8", errors="ignore") as f:
        soup = BeautifulSoup(f, "html.parser")

    title_tag = soup.find("title")
    page_title = title_tag.get_text() if title_tag else os.path.basename(html_path)
    content_root = soup.find(id="main-content") or soup.body

    blocks: List[Dict[str, Any]] = []
    if content_root:
        for child in content_root.contents:
            blocks.extend(migrate_page.convert_node_to_blocks(child, assets_root))
    return page_title, blocks


def _fallback_paragraph(text: str) -> Dict[str, Any]:
    return {
        "type": "paragraph",
        "paragraph": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {"content": text},
                    "annotations": {
                        "bold": False,
                        "italic": False,
                        "underline": False,
                        "strikethrough": False,
                        "code": False,
                        "color": "default",
                    },
                }
            ],
            "children": [],
        },
    }


def _sanitize_rich_text_links(
    rich_text: List[Dict[str, Any]], confluence_base: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Normalize or drop links:
    - Allow only http/https/mailto
    - If a base is provided and link is relative or missing scheme, prefix with base
    - Otherwise strip the link to avoid Notion validation errors
    """
    allowed_prefixes = ("http://", "https://", "mailto:")
    cleaned: List[Dict[str, Any]] = []
    for frag in rich_text:
        if frag.get("type") == "text":
            link = frag.get("text", {}).get("link")
            if link and isinstance(link, dict):
                url = link.get("url")
                norm = url.strip().strip('"').strip("'") if isinstance(url, str) else ""
                if norm and not norm.lower().startswith(allowed_prefixes) and confluence_base:
                    # Prefix relative or schemeless URLs with Confluence base
                    norm = norm.lstrip("/")
                    prefix = confluence_base.rstrip("/")
                    norm = f"{prefix}/{norm}"
                if not norm or not norm.lower().startswith(allowed_prefixes):
                    frag = json.loads(json.dumps(frag))
                    frag["text"]["link"] = None
                else:
                    # write back normalized URL
                    frag = json.loads(json.dumps(frag))
                    frag["text"]["link"]["url"] = norm
        cleaned.append(frag)
    return cleaned


def sanitize_blocks(blocks: List[Dict[str, Any]], confluence_base: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Ensure blocks are acceptable for Notion API.
    - Convert local file image paths to a textual placeholder (Attachment: path).
    - Recursively sanitize children.
    """
    sanitized: List[Dict[str, Any]] = []

    for block in blocks:
        btype = block.get("type")
        if not btype or btype not in block:
            continue

        # Copy shallow to avoid mutating original
        new_block = {"type": btype, btype: json.loads(json.dumps(block[btype]))}

        # Handle children for various block types
        def _sanitize_children(child_key: str):
            if child_key in new_block[btype]:
                new_block[btype][child_key] = sanitize_blocks(new_block[btype][child_key], confluence_base)

        if btype == "table":
            # table children are in table.children
            if "children" in new_block[btype]:
                new_block[btype]["children"] = sanitize_blocks(new_block[btype]["children"], confluence_base)
        if btype == "column_list":
            # Must have at least two columns; if not, flatten its children
            cols = new_block[btype].get("children", [])
            if len(cols) < 2:
                # flatten columns' children into top-level
                flattened: List[Dict[str, Any]] = []
                for col in cols:
                    if col.get("type") == "column" and "column" in col:
                        flattened.extend(sanitize_blocks(col["column"].get("children", []), confluence_base))
                sanitized.extend(flattened)
                continue
            else:
                new_block[btype]["children"] = sanitize_blocks(cols, confluence_base)
        if btype == "column":
            # Columns must always have a children array; ensure it exists even if empty.
            if "children" in new_block[btype]:
                new_block[btype]["children"] = sanitize_blocks(new_block[btype]["children"], confluence_base)
            else:
                new_block[btype]["children"] = []
        if btype in {"paragraph", "quote", "to_do", "bulleted_list_item", "numbered_list_item", "callout", "toggle"}:
            _sanitize_children("children")

        # Clean invalid links in any rich_text field BEFORE structural tweaks
        if "rich_text" in new_block[btype]:
            rt = new_block[btype]["rich_text"]
            if isinstance(rt, list):
                new_block[btype]["rich_text"] = _sanitize_rich_text_links(rt, confluence_base)

        # Remove empty children arrays to satisfy Notion API validation.
        # Skip removal for columns (they require an explicit children array).
        # For callouts, keep children key only when non-empty.
        if "children" in new_block[btype] and btype not in {"column", "callout"}:
            children_val = new_block[btype]["children"]
            if isinstance(children_val, list) and len(children_val) == 0:
                new_block[btype].pop("children")
        if btype == "callout":
            children_val = new_block[btype].get("children", [])
            if isinstance(children_val, list):
                if len(children_val) == 0:
                    new_block[btype].pop("children", None)
            # Notion requires icon to be absent or an object; drop nulls.
            if new_block[btype].get("icon") is None:
                new_block[btype].pop("icon", None)

        # For list items, Notion may reject nested children in page create calls.
        # Flatten any list-item children into siblings to avoid validation errors.
        if btype in {"bulleted_list_item", "numbered_list_item"} and "children" in new_block[btype]:
            child_blocks = new_block[btype].pop("children")
            sanitized.append(new_block)
            sanitized.extend(child_blocks)
            continue
        # For toggles, keep children if present; drop empty.
        if btype == "toggle":
            children_val = new_block[btype].get("children", [])
            if isinstance(children_val, list) and len(children_val) == 0:
                new_block[btype].pop("children", None)

        # Images: convert local file paths to placeholder
        if btype == "image":
            img = new_block[btype]
            if img.get("type") == "file":
                url = img.get("file", {}).get("url", "")
                if url and (url.startswith("http://") or url.startswith("https://")):
                    img["type"] = "external"
                    img["external"] = {"url": url}
                    img.pop("file", None)
                else:
                    sanitized.append(_fallback_paragraph(f"Attachment: {url or '[missing path]'}"))
                    continue

        # If block has empty rich_text (e.g., after sanitize), drop it
        if "rich_text" in new_block[btype]:
            rt = new_block[btype]["rich_text"]
            if isinstance(rt, list) and len(rt) == 0:
                # allow blocks with children only; otherwise drop
                if not new_block[btype].get("children"):
                    continue

        sanitized.append(new_block)

    return sanitized


def build_report_blocks(
    *,
    source_path: str,
    title: str,
    run_started_at: str,
    run_duration_sec: float,
    status: str,
    note: Optional[str] = None,
) -> List[Dict[str, Any]]:
    summary = f"Migration report — {status}"
    detail_lines = [
        f"Source: {source_path}",
        f"Title: {title}",
        f"Started: {run_started_at}",
        f"Duration: {run_duration_sec:.2f}s",
    ]
    if note:
        detail_lines.append(f"Note: {note}")

    children: List[Dict[str, Any]] = []
    for line in detail_lines:
        children.append(
            {
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [make_text_fragment(line)],
                    "children": [],
                },
            }
        )

    return [
        {"type": "divider", "divider": {}},
        {
            "type": "callout",
            "callout": {
                "rich_text": [make_text_fragment(summary)],
                "children": children,
                "color": "gray_background",
            },
        },
    ]


def build_page_payload(parent_page_id: str, title: str, children: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "parent": {"page_id": parent_page_id},
        "properties": {
            "title": {
                "title": [
                    {
                        "type": "text",
                        "text": {"content": title},
                    }
                ]
            }
        },
        "children": children,
    }


def create_page(token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    resp = requests.post(f"{NOTION_API_BASE}/pages", headers=headers, json=payload, timeout=30)
    if resp.status_code >= 300:
        raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text}")
    return resp.json()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a Notion page from a Confluence HTML export.")
    parser.add_argument("--html", required=True, help="Path to the Confluence-exported HTML file.")
    parser.add_argument("--assets-root", required=True, help="Root directory of the export (for resolving images/attachments).")
    parser.add_argument("--parent-page-id", required=True, help="Notion parent page ID.")
    parser.add_argument("--token", help="Notion integration token (fallback to NOTION_TOKEN env).")
    parser.add_argument(
        "--confluence-base",
        help="Base URL to prefix relative Confluence links (e.g., https://your-domain.atlassian.net).",
    )
    parser.add_argument(
        "--append-report",
        action="store_true",
        help="Append a migration report section at the end of the page.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only print the payload; do not call Notion API.")
    parser.add_argument("--out", help="When --dry-run, write payload JSON to this path instead of stdout.")
    return parser.parse_args()


def main() -> None:
    # Load local .env so PARENT_PAGE_ID / NOTION_TOKEN / NOTION_INTEGRATION_KEY can be picked up.
    load_env_file()
    args = parse_args()
    # Allow either NOTION_TOKEN or NOTION_INTEGRATION_KEY
    env_token = os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_INTEGRATION_KEY")
    token = args.token or env_token
    if not args.dry_run and not token:
        sys.stderr.write("Error: Provide NOTION_TOKEN env or --token when not using --dry-run\n")
        sys.exit(1)

    run_start_ts = time.time()
    run_started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(run_start_ts))

    title, blocks = load_blocks_from_html(args.html, args.assets_root)
    sanitized_blocks = sanitize_blocks(blocks, confluence_base=args.confluence_base)

    if args.append_report:
        duration_sec = time.time() - run_start_ts
        report_blocks = build_report_blocks(
            source_path=args.html,
            title=title,
            run_started_at=run_started_at,
            run_duration_sec=duration_sec,
            status="success" if not args.dry_run else "dry-run",
            note=None,
        )
        sanitized_blocks.extend(report_blocks)

    payload = build_page_payload(args.parent_page_id, title, sanitized_blocks)

    if args.dry_run:
        output = json.dumps(payload, indent=2, ensure_ascii=False)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(output)
        else:
            print(output)
        return

    result = create_page(token, payload)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
