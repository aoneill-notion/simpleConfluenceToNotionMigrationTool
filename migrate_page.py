#!/usr/bin/env python3
"""
Single-page Confluence HTML -> Notion block JSON converter (dry run).

Usage:
    python migrate_page.py --html path/to/page.html --assets-root /path/to/export/root --out page.json

Notes:
    - This emits a Notion-style block payload (no API calls). It is meant as a
      dry-run to inspect the structure before wiring to the Notion API.
    - Confluence user mentions (data-account-id) are degraded to plain text.
    - Emoji image tags are converted to their fallback Unicode when present.
    - Column layouts are mapped to Notion column_list/column blocks.
    - Inline tasks are mapped to to_do blocks (unchecked by default).
    - Attachments/images are left as relative file references; upload separately.
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

try:
    from bs4 import BeautifulSoup, NavigableString, Tag
except ImportError as exc:  # pragma: no cover - dependency hint
    sys.stderr.write(
        "Missing dependency: beautifulsoup4\n"
        "Install with: pip install beautifulsoup4\n"
    )
    raise exc


###############################################################################
# Helpers for inline rich text
###############################################################################

def _parse_color_value(raw: str) -> Optional[tuple]:
    raw = raw.strip().lower()
    if raw.startswith("#"):
        raw_hex = raw[1:]
        if len(raw_hex) == 3:
            raw_hex = "".join([c * 2 for c in raw_hex])
        if len(raw_hex) == 6:
            try:
                return tuple(int(raw_hex[i : i + 2], 16) for i in (0, 2, 4))
            except ValueError:
                return None
    if raw.startswith("rgb"):
        try:
            inside = raw[raw.index("(") + 1 : raw.index(")")]
            parts = [p.strip() for p in inside.split(",")]
            if len(parts) >= 3:
                return tuple(int(float(p)) for p in parts[:3])
        except Exception:
            return None
    return None


def _closest_notion_color(rgb: tuple, background: bool) -> str:
    palette = {
        "gray": (150, 150, 150),
        "brown": (143, 86, 58),
        "orange": (255, 153, 31),
        "yellow": (255, 213, 102),
        "green": (54, 179, 126),
        "blue": (38, 132, 255),
        "purple": (101, 84, 192),
        "pink": (255, 120, 203),
        "red": (255, 86, 48),
        "default": (0, 0, 0),
    }
    best = "default"
    best_dist = float("inf")
    for name, val in palette.items():
        dist = sum((a - b) ** 2 for a, b in zip(rgb, val))
        if dist < best_dist:
            best_dist = dist
            best = name
    return f"{best}_background" if background and best != "default" else (best if best != "default" else "default")


def _color_annotation_from_style(tag: Tag) -> Optional[str]:
    style = tag.get("style", "") or ""
    style_lower = style.lower()
    color_val = None
    bg_val = None
    for part in style_lower.split(";"):
        if "background-color" in part:
            _, val = part.split(":", 1)
            bg_val = _parse_color_value(val.strip())
        if part.strip().startswith("color"):
            _, val = part.split(":", 1)
            color_val = _parse_color_value(val.strip())
    if bg_val:
        return _closest_notion_color(bg_val, background=True)
    if color_val:
        return _closest_notion_color(color_val, background=False)
    # Heuristic: if data-colorid present, assume blue text
    if tag.has_attr("data-colorid"):
        return "blue"
    return None


def text_annotations_from_tag(tag: Tag) -> Dict[str, Any]:
    """Map formatting tags to Notion text annotations."""
    ann: Dict[str, Any] = {
        "bold": tag.name == "strong",
        "italic": tag.name == "em",
        "underline": tag.name == "u",
        "strikethrough": tag.name == "del",
        "code": tag.name == "code",
        "color": "default",
    }
    style_color = _color_annotation_from_style(tag)
    if style_color:
        ann["color"] = style_color
    return ann


def make_text_fragment(content: str, link: Optional[str] = None) -> Dict[str, Any]:
    """Create a Notion text fragment with default annotations."""
    return {
        "type": "text",
        "text": {"content": content, "link": {"url": link} if link else None},
        "annotations": {
            "bold": False,
            "italic": False,
            "underline": False,
            "strikethrough": False,
            "code": False,
            "color": "default",
        },
    }


def strip_empty_fragments(fragments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove fragments that are empty/whitespace-only."""
    return [
        frag
        for frag in fragments
        if frag.get("type") == "text"
        and frag.get("text", {}).get("content", "").strip() != ""
    ]


def is_emoji_img(tag: Tag) -> bool:
    return bool(
        tag.name == "img"
        and tag.has_attr("data-emoji-fallback")
        and tag.get("data-emoji-fallback")
    )


def rich_text_from_node(node: Any) -> List[Dict[str, Any]]:
    """Convert a node (text or tag) into Notion rich_text fragments."""
    rich: List[Dict[str, Any]] = []

    if isinstance(node, NavigableString):
        text = str(node)
        if not text:
            return []
        rich.append(make_text_fragment(text))
        return rich

    if isinstance(node, Tag):
        # Emoji images: replace with fallback Unicode
        if is_emoji_img(node):
            emoji = node.get("data-emoji-fallback", "") or ""
            if emoji:
                rich.append(make_text_fragment(emoji))
            return rich

        # Links and mentions
        if node.name == "a":
            # Confluence mentions have data-account-id; degrade to plain text
            content = "".join(t.get_text() if isinstance(t, Tag) else str(t) for t in node.contents)
            href = node.get("href")
            rt = make_text_fragment(content, link=href)
            # If it's a mention, drop the link to avoid noisy profile URLs
            if node.has_attr("data-account-id"):
                rt["text"]["link"] = None
            rich.append(rt)
            return rich

        # Inline formatting
        annotations = text_annotations_from_tag(node)
        for child in node.contents:
            for part in rich_text_from_node(child):
                # Merge annotations (parent annotations should OR with child)
                merged = part.get("annotations", {}).copy()
                for key, val in annotations.items():
                    if key == "color":
                        merged[key] = merged.get(key, "default") if merged.get(key) != "default" else val
                    else:
                        merged[key] = merged.get(key, False) or val
                part["annotations"] = merged
                rich.append(part)
        return rich

    return rich


def collect_inline_and_children(tag: Tag, assets_root: str) -> (List[Dict[str, Any]], List[Dict[str, Any]]):
    """
    For list items: separate inline content (to become rich_text) from nested block children.
    """
    inline_fragments: List[Dict[str, Any]] = []
    child_blocks: List[Dict[str, Any]] = []

    blockish = {"ul", "ol", "table", "blockquote", "pre", "img", "iframe", "hr", "div"}

    for child in tag.contents:
        if isinstance(child, NavigableString):
            inline_fragments.extend(rich_text_from_node(child))
        elif isinstance(child, Tag):
            if child.name in blockish:
                child_blocks.extend(convert_node_to_blocks(child, assets_root))
            elif child.name == "p" and child.find(blockish):
                child_blocks.extend(convert_node_to_blocks(child, assets_root))
            else:
                inline_fragments.extend(rich_text_from_node(child))

    inline_fragments = strip_empty_fragments(inline_fragments)
    return inline_fragments, child_blocks


###############################################################################
# Block converters
###############################################################################

def paragraph_block(tag: Tag) -> Dict[str, Any]:
    return {
        "type": "paragraph",
        "paragraph": {"rich_text": rich_text_from_node(tag), "children": []},
    }


def heading_block(tag: Tag) -> Dict[str, Any]:
    level = tag.name
    notion_type = {
        "h1": "heading_1",
        "h2": "heading_2",
        "h3": "heading_3",
    }.get(level, "heading_3")  # collapse h4-h6 to heading_3
    return {
        "type": notion_type,
        notion_type: {"rich_text": rich_text_from_node(tag), "is_toggleable": False},
    }


def list_item_block(tag: Tag, ordered: bool, assets_root: str) -> Optional[Dict[str, Any]]:
    notion_type = "numbered_list_item" if ordered else "bulleted_list_item"
    inline, children = collect_inline_and_children(tag, assets_root)
    if not inline and not children:
        return None
    return {
        "type": notion_type,
        notion_type: {"rich_text": inline, "children": children},
    }


def todo_block(tag: Tag, assets_root: str) -> Optional[Dict[str, Any]]:
    inline, children = collect_inline_and_children(tag, assets_root)
    if not inline and not children:
        return None
    return {
        "type": "to_do",
        "to_do": {
            "rich_text": inline,
            "checked": False,
            "children": children,
        },
    }


def quote_block(tag: Tag) -> Dict[str, Any]:
    return {"type": "quote", "quote": {"rich_text": rich_text_from_node(tag), "children": []}}


def divider_block() -> Dict[str, Any]:
    return {"type": "divider", "divider": {}}


def code_block(tag: Tag) -> Dict[str, Any]:
    language = None
    params = tag.get("data-syntaxhighlighter-params", "")
    if "brush:" in params:
        # crude parse for language parameter
        try:
            language = params.split("brush:")[1].split(";")[0].strip()
        except Exception:
            language = None
    text_content = tag.get_text()
    return {
        "type": "code",
        "code": {
            "rich_text": [make_text_fragment(text_content)],
            "language": language or "plain text",
        },
    }


def table_block(tag: Tag) -> Dict[str, Any]:
    rows = []
    for tr in tag.find_all("tr"):
        cells = []
        for cell in tr.find_all(["th", "td"]):
            cells.append([make_text_fragment(cell.get_text())])
        if cells:
            rows.append({"type": "table_row", "table_row": {"cells": cells}})
    return {
        "type": "table",
        "table": {
            "table_width": max((len(r["table_row"]["cells"]) for r in rows), default=1),
            "has_column_header": bool(tag.find("th")),
            "has_row_header": False,
            "children": rows,
        },
    }


def image_block(tag: Tag, assets_root: str) -> Dict[str, Any]:
    src = tag.get("src", "")
    caption = tag.get("alt") or ""
    src = _clean_url(src or "")
    is_remote = src.startswith("http://") or src.startswith("https://")
    if is_remote:
        resolved = src
        img_payload = {
            "type": "external",
            "external": {"url": resolved},
            "caption": [make_text_fragment(caption)] if caption else [],
        }
    else:
        resolved = os.path.normpath(os.path.join(assets_root, src)) if src else src
        img_payload = {
            "type": "file",
            "file": {"url": resolved, "expiry_time": None},
            "caption": [make_text_fragment(caption)] if caption else [],
        }
    return {"type": "image", "image": img_payload}


def _clean_url(url: str) -> str:
    return url.strip().strip('"').strip("'")


def _looks_like_image_url(url: str) -> bool:
    url_lower = url.lower()
    return url_lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"))


def _should_skip_icon(tag: Tag) -> bool:
    classes = tag.get("class", []) or []
    src = _clean_url(tag.get("src", "") or "")
    # Skip tiny UI chrome icons like expand arrows.
    if "expand-control-image" in classes:
        return True
    if src.endswith("grey_arrow_down.png"):
        return True
    # Skip 1f... emoji images handled elsewhere
    return False


def _is_indented(tag: Tag) -> bool:
    """Detect margin/padding left styles to approximate indentation."""
    style = tag.get("style", "") or ""
    for part in style.split(";"):
        if ":" not in part:
            continue
        key, val = part.split(":", 1)
        key = key.strip().lower()
        if key in {"margin-left", "padding-left", "margin"}:
            try:
                num = float(val.strip().replace("px", "").replace("em", ""))
                if num > 0:
                    return True
            except Exception:
                continue
    return False


def embed_block(tag: Tag, href_override: Optional[str] = None) -> Dict[str, Any]:
    src = href_override if href_override is not None else tag.get("src", "")
    src = _clean_url(src or "")
    return {"type": "embed", "embed": {"url": src}}


def panel_callout_block(tag: Tag, assets_root: str) -> Dict[str, Any]:
    """
    Map Confluence panels to a Notion callout.
    Use a short label and place converted children inside the callout.
    """
    # Color map based on common Confluence panel backgrounds
    panel_color_map = {
        "#eae6ff": "purple_background",
        "#eae6ff;": "purple_background",
        "#dfe1e6": "gray_background",
        "#deebff": "blue_background",
        "#e3fcef": "green_background",
        "#fffae6": "yellow_background",
        "#ffebe6": "red_background",
    }
    style = (tag.get("style") or "").lower()
    color = "default"
    for key, notion_color in panel_color_map.items():
        if key in style:
            color = notion_color
            break

    # Convert children inside the panel
    child_blocks: List[Dict[str, Any]] = []
    for child in tag.contents:
        child_blocks.extend(convert_node_to_blocks(child, assets_root))

    # Keep a concise label to avoid duplicating full content in header
    summary_text = "Note"

    callout = {
        "rich_text": [make_text_fragment(summary_text)],
        "color": color,
        "children": child_blocks,
    }

    return {"type": "callout", "callout": callout}


def toc_macro_block(tag: Tag) -> List[Dict[str, Any]]:
    """
    Convert Confluence toc-macro into a bulleted list of links.
    """
    blocks: List[Dict[str, Any]] = []
    for a in tag.find_all("a"):
        text = a.get_text(strip=True)
        href = a.get("href")
        rich = [make_text_fragment(text, link=href)] if text else []
        if rich:
            blocks.append(
                {
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": rich, "children": []},
                }
            )
    return blocks


def expand_block(tag: Tag, assets_root: str) -> Dict[str, Any]:
    """
    Convert Confluence expand macro into a Notion toggle block.
    """
    title = "Expand"
    ctrl = tag.find(class_="expand-control-text")
    if ctrl:
        title = ctrl.get_text(strip=True) or title
    content_div = tag.find(class_="expand-content")
    child_blocks: List[Dict[str, Any]] = []
    if content_div:
        for child in content_div.contents:
            child_blocks.extend(convert_node_to_blocks(child, assets_root))
    return {
        "type": "toggle",
        "toggle": {
            "rich_text": [make_text_fragment(title)],
            "children": child_blocks,
        },
    }


def column_list_from_layout(layout: Tag, assets_root: str) -> Dict[str, Any]:
    columns: List[Dict[str, Any]] = []
    for cell in layout.find_all("div", class_="cell", recursive=False):
        inner = cell.find("div", class_="innerCell")
        child_blocks = []
        if inner:
            for child in inner.contents:
                blocks = convert_node_to_blocks(child, assets_root)
                if blocks:
                    child_blocks.extend(blocks)
        columns.append({"type": "column", "column": {"children": child_blocks}})
    return {"type": "column_list", "column_list": {"children": columns}}


###############################################################################
# Main conversion dispatcher
###############################################################################

def convert_node_to_blocks(node: Any, assets_root: str) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []

    if isinstance(node, NavigableString):
        text = str(node).strip()
        if text:
            blocks.append({
                "type": "paragraph",
                "paragraph": {"rich_text": [make_text_fragment(text)], "children": []},
            })
        return blocks

    if not isinstance(node, Tag):
        return blocks

    # Skip style tags entirely
    if node.name == "style":
        return blocks

    name = node.name

    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        blocks.append(heading_block(node))
    elif name == "p":
        rich = strip_empty_fragments(rich_text_from_node(node))
        if rich:
            if _is_indented(node):
                blocks.append({"type": "quote", "quote": {"rich_text": rich, "children": []}})
            else:
                blocks.append({
                    "type": "paragraph",
                    "paragraph": {"rich_text": rich, "children": []},
                })
    elif name == "blockquote":
        blocks.append(quote_block(node))
    elif name == "hr":
        blocks.append(divider_block())
    elif name == "pre":
        blocks.append(code_block(node))
    elif name == "table":
        blocks.append(table_block(node))
    elif name == "img":
        if not is_emoji_img(node) and not _should_skip_icon(node):
            blocks.append(image_block(node, assets_root))
    elif name == "iframe":
        blocks.append(embed_block(node))
    elif name == "a":
        href = node.get("href", "")
        href_clean = _clean_url(href)
        is_embed_link = node.get("data-card-appearance") == "embed"
        is_youtube = "youtube.com" in href_clean or "youtu.be" in href_clean
        if is_embed_link or is_youtube:
            blocks.append(embed_block(node, href_override=href_clean))
        elif _looks_like_image_url(href_clean):
            # Some attachments are linked, not img tags; render as external image.
            blocks.append(
                {
                    "type": "image",
                    "image": {
                        "type": "external",
                        "external": {"url": href_clean},
                        "caption": [],
                    },
                }
            )
        else:
            rich = rich_text_from_node(node)
            if rich:
                blocks.append(
                    {
                        "type": "paragraph",
                        "paragraph": {"rich_text": rich, "children": []},
                    }
                )
    elif name in {"ul", "ol"}:
        ordered = name == "ol"
        for li in node.find_all("li", recursive=False):
            if li.has_attr("data-inline-task-id"):
                block = todo_block(li, assets_root)
            else:
                block = list_item_block(li, ordered, assets_root)
            if block:
                blocks.append(block)
    elif name == "div" and "columnLayout" in node.get("class", []):
        blocks.append(column_list_from_layout(node, assets_root))
    elif name == "div" and "panel" in node.get("class", []):
        blocks.append(panel_callout_block(node, assets_root))
    elif name == "div" and "toc-macro" in node.get("class", []):
        blocks.extend(toc_macro_block(node))
    elif name == "div" and "expand-container" in node.get("class", []):
        blocks.append(expand_block(node, assets_root))
    else:
        # Fallback: process children and bubble up paragraphs
        for child in node.contents:
            blocks.extend(convert_node_to_blocks(child, assets_root))

    return blocks


###############################################################################
# Entry point
###############################################################################

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert a Confluence HTML page to Notion block JSON (dry run).")
    parser.add_argument("--html", required=True, help="Path to the Confluence-exported HTML file.")
    parser.add_argument("--assets-root", required=True, help="Root directory of the export (for resolving images/attachments).")
    parser.add_argument("--out", help="Output JSON file. Defaults to stdout.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    with open(args.html, "r", encoding="utf-8", errors="ignore") as f:
        soup = BeautifulSoup(f, "html.parser")

    title_tag = soup.find("title")
    page_title = title_tag.get_text() if title_tag else os.path.basename(args.html)

    content_root = soup.find(id="main-content") or soup.body
    blocks: List[Dict[str, Any]] = []
    if content_root:
        for child in content_root.contents:
            blocks.extend(convert_node_to_blocks(child, args.assets_root))

    result = {
        "page_title": page_title,
        "blocks": blocks,
    }

    output = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as outf:
            outf.write(output)
    else:
        print(output)


if __name__ == "__main__":
    main()
