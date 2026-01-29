#!/usr/bin/env python3
"""
Bulk-import Confluence-exported HTML pages into Notion.

This wraps `import_to_notion.py` logic to walk a directory of HTML files and
create a Notion page for each one.

Examples:
    # Dry run (no API calls), write payloads to /tmp/payloads
    python bulk_import.py \
        --html-root /path/to/export \
        --parent-page-id YOUR_PARENT_PAGE_ID \
        --dry-run \
        --out-dir /tmp/payloads

    # Actually create pages (uses NOTION_TOKEN/NOTION_INTEGRATION_KEY env)
    python bulk_import.py \
        --html-root /path/to/export \
        --parent-page-id YOUR_PARENT_PAGE_ID
"""

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

import import_to_notion


def find_html_files(root: str) -> List[str]:
    files: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            if fname.lower().endswith((".html", ".htm")):
                files.append(os.path.join(dirpath, fname))
    files.sort()
    return files


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bulk import Confluence HTML pages into Notion.")
    parser.add_argument(
        "--html-root",
        required=True,
        help="Directory containing Confluence-exported HTML files (searches recursively).",
    )
    parser.add_argument(
        "--assets-root",
        help="Root for resolving images/attachments (defaults to --html-root).",
    )
    parser.add_argument("--parent-page-id", required=True, help="Notion parent page ID.")
    parser.add_argument("--token", help="Notion integration token (fallback to env).")
    parser.add_argument(
        "--confluence-base",
        help="Base URL to prefix relative Confluence links (e.g., https://your-domain.atlassian.net).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only build payloads; do not call Notion API.",
    )
    parser.add_argument(
        "--out-dir",
        help="When --dry-run, write each payload to this directory (one JSON per HTML).",
    )
    parser.add_argument(
        "--links-out",
        help="Optional path to write a JSON mapping of pages to their extracted links (for post-migration redirects).",
    )
    parser.add_argument(
        "--append-report",
        action="store_true",
        help="Append a migration report section to each created page.",
    )
    parser.add_argument(
        "--final-report-out",
        help="Optional path to write a single migration summary report after the run.",
    )
    return parser.parse_args()


def write_payload(out_dir: str, html_path: str, payload: dict) -> None:
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(html_path))[0]
    out_path = os.path.join(out_dir, f"{base}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _resolve_link(href: Optional[str], confluence_base: Optional[str]) -> Optional[str]:
    if not href:
        return None
    allowed_prefixes = ("http://", "https://", "mailto:")
    norm = href.strip()
    if not norm:
        return None
    if norm.lower().startswith(allowed_prefixes):
        return norm
    if confluence_base:
        norm = norm.lstrip("/")
        prefix = confluence_base.rstrip("/")
        return f"{prefix}/{norm}"
    return None


def extract_links(html_path: str, confluence_base: Optional[str]) -> List[Dict[str, Optional[str]]]:
    links: List[Dict[str, Optional[str]]] = []
    with open(html_path, "r", encoding="utf-8", errors="ignore") as fh:
        soup = BeautifulSoup(fh, "html.parser")
    for a in soup.find_all("a"):
        raw = a.get("href")
        resolved = _resolve_link(raw, confluence_base)
        text = a.get_text(strip=True)
        links.append(
            {
                "text": text or None,
                "href_raw": raw,
                "href_resolved": resolved,
            }
        )
    return links


def main() -> None:
    import_to_notion.load_env_file()
    args = parse_args()

    assets_root = args.assets_root or args.html_root
    env_token = os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_INTEGRATION_KEY")
    token = args.token or env_token

    if not args.dry_run and not token:
        sys.stderr.write("Error: Provide NOTION_TOKEN env or --token when not using --dry-run\n")
        sys.exit(1)

    html_files = find_html_files(args.html_root)
    if not html_files:
        sys.stderr.write(f"No HTML files found under {args.html_root}\n")
        sys.exit(1)

    run_start_ts = time.time()
    run_started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(run_start_ts))
    success = 0
    failures = 0
    error_details: List[Dict[str, str]] = []
    link_mappings: List[Dict[str, object]] = []

    for html_path in html_files:
        rel = os.path.relpath(html_path, args.html_root)
        page_start_ts = time.time()
        page_started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(page_start_ts))
        try:
            title, blocks = import_to_notion.load_blocks_from_html(html_path, assets_root)
            sanitized = import_to_notion.sanitize_blocks(blocks, confluence_base=args.confluence_base)
            if args.append_report:
                duration_sec = time.time() - page_start_ts
                report_blocks = import_to_notion.build_report_blocks(
                    source_path=rel,
                    title=title,
                    run_started_at=page_started_at,
                    run_duration_sec=duration_sec,
                    status="pending" if args.dry_run else "success",
                    note=None,
                )
                sanitized.extend(report_blocks)
            payload = import_to_notion.build_page_payload(args.parent_page_id, title, sanitized)
            links = extract_links(html_path, args.confluence_base)

            if args.dry_run:
                if args.out_dir:
                    write_payload(args.out_dir, html_path, payload)
                else:
                    # Print a short summary instead of full payload to keep output manageable.
                    print(f"[dry-run] {rel} -> title='{title}' children={len(payload.get('children', []))}")
                notion_page_id = None
            else:
                result = import_to_notion.create_page(token, payload)
                notion_page_id = result.get("id", None)
                print(f"[created] {rel} -> page_id={notion_page_id or '<unknown>'}")

            if args.links_out:
                link_mappings.append(
                    {
                        "source_html": rel,
                        "title": title,
                        "notion_page_id": notion_page_id,
                        "links": links,
                    }
                )

            success += 1
        except Exception as exc:  # pragma: no cover - CLI diagnostic path
            failures += 1
            sys.stderr.write(f"[error] {rel}: {exc}\n")
            error_details.append({"page": rel, "error": str(exc)})

    if args.links_out and link_mappings:
        out_path = os.path.abspath(args.links_out)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(link_mappings, f, indent=2, ensure_ascii=False)
        print(f"[links] wrote link mapping for {len(link_mappings)} pages to {out_path}")

    if args.final_report_out:
        run_duration = time.time() - run_start_ts
        report = {
            "started_at": run_started_at,
            "duration_sec": run_duration,
            "html_root": args.html_root,
            "parent_page_id": args.parent_page_id,
            "successes": success,
            "failures": failures,
            "errors": error_details,
        }
        out_path = os.path.abspath(args.final_report_out)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"[report] wrote final report to {out_path}")

    if failures:
        sys.stderr.write(f"Completed with {success} successes and {failures} failures.\n")
        sys.exit(1)
    else:
        print(f"Completed successfully: {success} pages processed.")


if __name__ == "__main__":
    main()
