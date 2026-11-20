"""Files Library — page rendering on the Fleet design system.

The listing is rendered once at startup from library.json (grouped by
project, with the row markup and search data the template's
client-side filter uses), exactly as the original app.py rendered it;
only the template's styling moved onto the design system.
"""
import html
from pathlib import Path

from .config import BASE_PATH
from .store import ENTRIES, FILES

_HERE = Path(__file__).parent.parent
_TEMPLATE = (_HERE / "index.html").read_text(encoding="utf-8")
FLEET_CSS = (_HERE / "assets" / "fleet.css").read_bytes()


def _human_size(n):
    if not isinstance(n, (int, float)):
        return "—"
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f}".rstrip("0").rstrip(".") + " KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f}".rstrip("0").rstrip(".") + " MB"
    return f"{n / 1024 ** 3:.2f} GB"


def _row(e, held):
    if held:
        search = html.escape(
            " ".join([e["file"], e.get("type") or "", e.get("date") or ""]).lower(),
            quote=True,
        )
        main = f'<span class="file-name">{html.escape(e["file"])}</span>'
        meta = (
            f'<span>{html.escape(e.get("type") or "—")}</span>'
            f'<span>{html.escape(e.get("date") or "—")}</span>'
        )
    else:
        search = html.escape(
            " ".join(
                [
                    e["file"],
                    e.get("desc") or "",
                    e["group"],
                    e.get("type") or "",
                    e.get("date") or "",
                ]
            ).lower(),
            quote=True,
        )
        main = f'<span class="file-name">{html.escape(e["file"])}</span>'
        if e.get("desc"):
            main += f'<span class="file-desc">{html.escape(e["desc"])}</span>'
        meta = (
            f'<span>{html.escape(e.get("type") or "—")}</span>'
            f'<span>{html.escape(_human_size(e.get("size")) if e.get("dl") else "—")}</span>'
            f'<span>{html.escape(e.get("date") or "—")}</span>'
        )
        if e.get("dl"):
            meta += f'<a class="file-dl" href="{BASE_PATH}/dl/{e["dl"]}" download>Download</a>'
        else:
            meta += '<span class="file-tag">On file — available on request</span>'
    return (
        f'<li class="file-row" data-search="{search}">'
        f'<div class="file-main">{main}</div>'
        f'<div class="file-meta">{meta}</div></li>'
    )


def _render_page():
    groups = {}
    held = []
    for e in ENTRIES:
        if e.get("held"):
            held.append(e)
        else:
            groups.setdefault(e["group"], []).append(e)
    parts = []
    for name in sorted(groups, key=lambda s: (s.lower(), s)):
        rows = sorted(groups[name], key=lambda e: (e["file"].lower(), e["file"]))
        label = "file" if len(rows) == 1 else "files"
        parts.append(
            '<div class="files-group">'
            f'<h2 class="files-group-title">{html.escape(name)} '
            f'<span class="files-group-count">{len(rows)} {label}</span></h2>'
            '<ul class="files-list">'
            + "".join(_row(e, False) for e in rows)
            + "</ul></div>"
        )
    if held:
        rows = sorted(held, key=lambda e: (e["file"].lower(), e["file"]))
        parts.append(
            '<div class="files-group files-held">'
            '<h2 class="files-group-title">Held — ask Taylor '
            f'<span class="files-group-count">{len(rows)} files</span></h2>'
            '<ul class="files-list">'
            + "".join(_row(e, True) for e in rows)
            + "</ul></div>"
        )
    counts = (
        f'{len(ENTRIES)} entries · {len(FILES)} downloadable files · '
        f'{sum(1 for e in ENTRIES if not e.get("held") and not e.get("dl"))} on file by request · '
        f"{len(held)} held"
    )
    return _TEMPLATE.replace("__BASE_PATH__", BASE_PATH).replace(
        "__GROUPS__", "\n".join(parts)
    ).replace(
        "__COUNTS__", html.escape(counts)
    ).encode()


PAGE = _render_page()
