## Confluence to Notion Migration Tools

Scripts to convert Confluence HTML exports to Notion pages and bulk-import them via the Notion API.

### Key scripts
- `migrate_page.py`: Converts a single Confluence-exported HTML file into Notion block JSON (dry-run only).
- `import_to_notion.py`: Converts and creates a Notion page. Handles panels → callouts, expand → toggle, TOC, embeds, inline colors/highlights, indentation as quotes, external images, and sanitization.
- `bulk_import.py`: Recursively imports many HTML files, with optional link mapping and final JSON report.

### Prerequisites
- Python 3.9+ and `pip install requests beautifulsoup4`
- Notion integration token in `NOTION_TOKEN` (or `NOTION_INTEGRATION_KEY`)
- Target Notion parent page ID

### Common usage
Single page:
```bash
python import_to_notion.py \
  --html /path/to/page.html \
  --assets-root /path/to/export-root \
  --parent-page-id YOUR_PARENT_PAGE_ID \
  --confluence-base https://your-domain.atlassian.net \
  --append-report       # optional: add per-page migration report
  # --dry-run           # optional: inspect payload without creating
```

Bulk import (with link map and final run report):
```bash
ROOT="/path/to/export-root"
python bulk_import.py \
  --html-root "$ROOT" \
  --parent-page-id YOUR_PARENT_PAGE_ID \
  --confluence-base https://your-domain.atlassian.net \
  --links-out "$ROOT/tmp/notion-link-map.json" \
  --final-report-out "$ROOT/tmp/migration-report.json"
  # --append-report    # optional: per-page report appended in Notion
  # --dry-run          # optional: build payloads only
```

### Notes
- Images with http/https sources stay external; local paths remain file references. Host attachments or swap in reachable URLs for full rendering.
- Embeds: YouTube and `data-card-appearance="embed"` links become Notion embeds.
- Expand blocks map to toggles; Confluence panels map to callouts; TOC macros map to bullet links.
- Inline colors/highlights are mapped to the nearest Notion text/background colors.
- A final report JSON can be written for the whole run via `--final-report-out`.
