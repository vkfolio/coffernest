"""Public pages the API serves itself: the privacy policy and terms (the App Store links to
them, and /meta points the apps here) and a short note at /.

The text is the markdown in ``coffernest/legal``. It uses only
headings, paragraphs, lists, **bold**, _italics_ and bare links, so a few lines convert it.
"""

from __future__ import annotations

import html
import re
from functools import cache
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

router = APIRouter(include_in_schema=False)

LEGAL = Path(__file__).resolve().parent.parent / "legal"

_PAGE = """<!doctype html><html lang="en-IN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><title>{title}</title><style>
:root{{--ink:#1d2030;--muted:#5d6275;--bg:#f6f6fb;--accent:#4b4fd6}}
@media (prefers-color-scheme:dark){{:root{{--ink:#e9eaf3;--muted:#a3a7ba;--bg:#14151d;
--accent:#9a9dff}}}}
body{{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:680px;margin:0 auto;padding:32px 20px 64px}}
h1{{font-size:1.9rem;line-height:1.2;margin:.2em 0 .4em}}h2{{font-size:1.2rem;margin:1.8em 0 .4em}}
a{{color:var(--accent);overflow-wrap:anywhere}}em{{color:var(--muted)}}li{{margin:.35em 0}}
.home{{font-weight:600;text-decoration:none}}
</style></head><body><main><a class="home" href="/">Coffernest</a>
{body}</main></body></html>"""


def _inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![\w*])_(.+?)_(?!\w)", r"<em>\1</em>", out)
    return re.sub(r"https?://[^\s<]+[^\s<.,;:)]", lambda m: f'<a href="{m[0]}">{m[0]}</a>', out)


def to_html(markdown: str) -> str:
    """Headings, paragraphs, "-" and "1." lists (continuation lines indented)."""
    blocks: list[str] = []
    for chunk in re.split(r"\n\s*\n", markdown.strip()):
        lines = chunk.splitlines()
        first = lines[0]
        if heading := re.match(r"(#{1,3}) (.+)", first):
            level = len(heading[1])
            blocks.append(f"<h{level}>{_inline(heading[2])}</h{level}>")
            continue
        bullet = re.match(r"(-|\d+\.) ", first)
        if bullet is None:
            blocks.append(f"<p>{_inline(' '.join(line.strip() for line in lines))}</p>")
            continue
        tag = "ul" if bullet[1] == "-" else "ol"
        items: list[str] = []
        for line in lines:
            if start := re.match(r"(?:-|\d+\.) (.*)", line):
                items.append(start[1])
            else:
                items[-1] += " " + line.strip()
        blocks.append(f"<{tag}>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + f"</{tag}>")
    return "\n".join(blocks)


@cache
def _legal(name: str) -> str:
    text = (LEGAL / f"{name}.md").read_text(encoding="utf-8")
    title = text.splitlines()[0].lstrip("# ").strip()
    return _PAGE.format(title=f"{html.escape(title)} – Coffernest", body=to_html(text))


@router.get("/privacy", response_class=HTMLResponse)
async def privacy() -> HTMLResponse:
    return HTMLResponse(_legal("privacy"))


@router.get("/terms", response_class=HTMLResponse)
async def terms() -> HTMLResponse:
    return HTMLResponse(_legal("terms"))


@router.get("/", response_class=HTMLResponse)
async def home() -> HTMLResponse:
    body = (
        "<h1>This is a Coffernest server</h1><p>Open the Coffernest app on your iPhone and enter "
        "this page's address to sign in or sync.</p>"
        '<p><a href="/privacy">Privacy policy</a> · <a href="/terms">Terms of use</a></p>'
    )
    return HTMLResponse(_PAGE.format(title="Coffernest server", body=body))


# A browser that once ran an offline web app at this address keeps it as a service worker at
# /sw.js. This replacement worker clears that saved copy and reloads the page.
_RETIRE_SW = """self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) await caches.delete(key);
    await self.registration.unregister();
    const windows = await self.clients.matchAll({ type: "window" });
    for (const client of windows) client.navigate(client.url);
  })());
});
"""


@router.get("/sw.js")
async def retire_service_worker() -> Response:
    return Response(_RETIRE_SW, media_type="text/javascript", headers={"Cache-Control": "no-cache"})
