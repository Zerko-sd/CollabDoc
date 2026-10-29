"""Read-only HTML for a page published to the web.

The buffer's markup is rendered the way the editor shows it -- headings,
lists, to-dos, quotes, callouts, dividers, code blocks, tables, images and
inline marks -- but on the server, for visitors without an account. Every
piece of text is escaped before it is wrapped in a tag, so nothing a writer
types can become markup on the published page.
"""
from __future__ import annotations

import re
from html import escape

from .notifications import MENTION

_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_INLINE = [
    (re.compile(r"`([^`]+)`"), "code"),
    (re.compile(r"\*\*(.+?)\*\*"), "strong"),
    (re.compile(r"~~(.+?)~~"), "s"),
    (re.compile(r"__(.+?)__"), "u"),
    (re.compile(r"(?<![*\w])\*([^*\s](?:[^*]*[^*\s])?)\*(?![*\w])"), "em"),
]
_IMAGE = re.compile(r"^!\[([^\]\n]*)\]\(/api/files/([0-9a-f-]{36})\)\s*$")
_TABLE_RULE = re.compile(r"^\|(\s*:?-{3,}:?\s*\|)+\s*$")


def _safe_href(url: str) -> str | None:
    if re.match(r"^(https?:|mailto:)", url, re.I):
        return url
    if re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", url, re.I):
        return "https://" + url
    return None


def inline(text: str) -> str:
    """Inline marks as HTML. Code spans are cut out first so their contents
    stay literal, then links and mentions, then the emphasis marks."""
    tokens: list[str] = []

    def stash(html: str) -> str:
        tokens.append(html)
        return "\x00%d\x00" % (len(tokens) - 1)

    text = re.sub(r"`([^`]+)`", lambda m: stash("<code>%s</code>" % escape(m.group(1))), text)
    text = MENTION.sub(lambda m: stash('<span class="mention">@%s</span>' % escape(m.group(1))), text)

    def link(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        if url.startswith("page:"):
            return stash('<span class="page-link">%s</span>' % escape(label))
        href = _safe_href(url)
        if href is None:
            return stash(escape(label))
        return stash(
            '<a href="%s" rel="noopener nofollow" target="_blank">%s</a>'
            % (escape(href, quote=True), escape(label))
        )

    text = _LINK.sub(link, text)
    html = escape(text)
    for pattern, tag in _INLINE[1:]:
        html = pattern.sub(lambda m, tag=tag: "<%s>%s</%s>" % (tag, m.group(1), tag), html)
    return re.sub(r"\x00(\d+)\x00", lambda m: tokens[int(m.group(1))], html)


def _cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in body.split("|")]


def render_body(content: str, document_id: str) -> str:
    out: list[str] = []
    lists: list[str] = []  # open <ul>/<ol> tags
    lines = content.split("\n")
    i = 0

    def close_lists() -> None:
        while lists:
            out.append("</%s>" % lists.pop())

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        fence = re.match(r"^```\s*([\w+#-]*)\s*$", stripped)
        if fence:
            close_lists()
            body: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != "```":
                body.append(lines[i])
                i += 1
            language = fence.group(1)
            out.append(
                '<pre%s><code>%s</code></pre>'
                % (' data-lang="%s"' % escape(language, quote=True) if language else "", escape("\n".join(body)))
            )
            i += 1
            continue

        if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1:
            close_lists()
            rows: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
                rows.append(lines[i])
                i += 1
            out.append("<table>")
            for index, row in enumerate(rows):
                if _TABLE_RULE.match(row.strip()):
                    continue
                tag = "th" if index == 0 and len(rows) > 1 and _TABLE_RULE.match(rows[1].strip()) else "td"
                out.append(
                    "<tr>%s</tr>"
                    % "".join("<%s>%s</%s>" % (tag, inline(cell), tag) for cell in _cells(row))
                )
            out.append("</table>")
            continue

        i += 1
        if not stripped:
            close_lists()
            continue

        image = _IMAGE.match(stripped)
        if image:
            close_lists()
            out.append(
                '<figure><img src="/p/%s/files/%s" alt="%s" loading="lazy"></figure>'
                % (document_id, image.group(2), escape(image.group(1), quote=True))
            )
            continue
        if re.match(r"^(-{3,}|\*{3,})$", stripped):
            close_lists()
            out.append("<hr>")
            continue
        heading = re.match(r"^(#{1,3}) (.*)$", stripped)
        if heading:
            close_lists()
            level = len(heading.group(1))
            out.append("<h%d>%s</h%d>" % (level, inline(heading.group(2)), level))
            continue
        todo = re.match(r"^[-*+] \[([ xX])\] (.*)$", stripped)
        bullet = re.match(r"^[-*+] (.*)$", stripped)
        number = re.match(r"^\d{1,9}[.)] (.*)$", stripped)
        if todo or bullet or number:
            kind = "ol" if number else "ul"
            if not lists or lists[-1] != kind:
                close_lists()
                out.append('<%s%s>' % (kind, ' class="todos"' if todo else ""))
                lists.append(kind)
            if todo:
                checked = todo.group(1) != " "
                out.append(
                    '<li class="todo%s"><input type="checkbox" disabled%s> %s</li>'
                    % (" done" if checked else "", " checked" if checked else "", inline(todo.group(2)))
                )
            else:
                out.append("<li>%s</li>" % inline((bullet or number).group(1)))
            continue
        close_lists()
        if stripped.startswith("> "):
            body = stripped[2:]
            callout = re.match(r"^(\S+?) (.*)$", body)
            if callout and not callout.group(1).isascii():
                out.append(
                    '<aside class="callout"><span>%s</span><div>%s</div></aside>'
                    % (escape(callout.group(1)), inline(callout.group(2)))
                )
            else:
                out.append("<blockquote>%s</blockquote>" % inline(body))
            continue
        out.append("<p>%s</p>" % inline(stripped))
    close_lists()
    return "\n".join(out)


_STYLE = """
:root{--bg:#fff;--fg:#37352f;--muted:#787774;--line:#e9e9e7;--soft:#f7f6f3;--accent:#2383e2}
@media (prefers-color-scheme:dark){:root{--bg:#191919;--fg:#e6e6e4;--muted:#9b9a97;--line:#2f2f2f;--soft:#252525;--accent:#529cca}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.65 ui-sans-serif,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
main{max-width:720px;margin:0 auto;padding:64px 16px 96px}
.icon{font-size:56px;line-height:1}h1.title{font-size:40px;margin:8px 0 24px}
h1,h2,h3{line-height:1.3;margin:1.4em 0 .3em}a{color:var(--accent)}
code{background:var(--soft);border-radius:4px;padding:.1em .35em;font-size:.88em;
font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
pre{background:var(--soft);border-radius:6px;padding:14px 16px;overflow-x:auto}pre code{background:none;padding:0}
blockquote{border-left:3px solid var(--fg);margin:.5em 0;padding-left:14px}
.callout{display:flex;gap:10px;background:var(--soft);border-radius:6px;padding:14px 16px;margin:.5em 0}
hr{border:0;border-top:1px solid var(--line);margin:1.5em 0}
table{border-collapse:collapse;width:100%;margin:.75em 0;display:block;overflow-x:auto}
th,td{border:1px solid var(--line);padding:6px 10px;text-align:left}th{background:var(--soft)}
figure{margin:1em 0}img{max-width:100%;border-radius:4px}
.todos{list-style:none;padding-left:4px}.todo.done{color:var(--muted);text-decoration:line-through}
.mention,.page-link{color:var(--accent);font-weight:500}
footer{margin-top:64px;color:var(--muted);font-size:13px}
"""


def render_page(title: str, icon: str | None, content: str, document_id: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex">'
        "<title>%s</title><style>%s</style></head><body><main>"
        "%s<h1 class=\"title\">%s</h1>%s"
        "<footer>Published with CollabDoc · read-only</footer></main></body></html>"
    ) % (
        escape(title),
        _STYLE,
        '<div class="icon">%s</div>' % escape(icon) if icon else "",
        escape(title),
        render_body(content, document_id),
    )
