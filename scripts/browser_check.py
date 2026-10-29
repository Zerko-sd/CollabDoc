"""Drive the real UI in two browsers and check they stay in step.

Two independent browser contexts -- two users, two sessions, two WebSockets --
open the same document. One types, the other must see it; then the reverse.
Along the way this checks the things only a browser can check: that the page
renders, that presence avatars appear, that the preview tints blocks by author,
and that no JavaScript error is thrown anywhere in the process.

    python -m uvicorn app.main:app --app-dir server --port 8000
    python scripts/browser_check.py --base http://127.0.0.1:8000

Screenshots land in `artifacts/` unless --no-screenshots is given.
"""
from __future__ import annotations

import argparse
import re
import sys
import uuid
from pathlib import Path

try:
    from playwright.sync_api import Page, expect, sync_playwright
except ImportError:  # pragma: no cover
    sys.exit("This script needs Playwright:\n  pip install playwright\n  playwright install chromium")

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "browser-check-1"

# Both authors have to write enough for the model to train on: it needs at
# least three samples each, and a sample is around 120 characters of prose, so
# roughly 400 characters apiece. Anything less only ever exercises the
# keystroke-ledger fallback. The two styles are deliberately far apart.
ALICE_TEXT = (
    "# Quarterly Research Summary\n"
    "It should be noted that the underlying methodology, which has been "
    "extensively documented in the preceding literature, remains fundamentally "
    "sound; furthermore, the observed correlation between these variables "
    "warrants considerable further investigation.\n"
    "Consequently, one observes that such considerations, taken together with "
    "the theoretical framework established earlier, substantiate the original "
    "hypothesis to a satisfactory degree.\n"
    "Moreover, the literature suggests that the theoretical framework, "
    "notwithstanding its acknowledged limitations, accommodates these findings "
    "adequately and without material qualification.\n"
)
BOB_TEXT = (
    "Here's the thing. It's simple. We shipped it. It works!\n"
    "Look, I don't buy the long version. That's it. That's the whole trick!\n"
    "We tried. We failed. We tried again. Nobody's complained yet!\n"
    "So here's my take. Ship it. Watch it. Fix it. Move on!\n"
    "I've seen worse. Much worse. This one's fine. Really!\n"
    "No meetings. No decks. Just code. That's how it went!\n"
)

results: list[tuple[bool, str]] = []
console_errors: list[str] = []


def _tiny_png() -> bytes:
    """A 2x2 PNG, built here so the check needs no image files."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + b"\x23\x83\xe2" * 2 for _ in range(2))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


TINY_PNG = _tiny_png()


def check(condition: bool, label: str) -> bool:
    results.append((bool(condition), label))
    print("  %s %s" % ("PASS" if condition else "FAIL", label))
    return bool(condition)


def watch_console(page: Page, who: str) -> None:
    page.on(
        "console",
        lambda message: (
            console_errors.append("%s: %s" % (who, message.text))
            if message.type == "error"
            else None
        ),
    )
    page.on("pageerror", lambda error: console_errors.append("%s: %s" % (who, error)))


def register(page: Page, base: str, name: str, email: str) -> None:
    # "load" rather than "domcontentloaded": app.js is a module, so its
    # listeners are attached after the DOM is parsed but before load fires.
    # Clicking earlier lands on a button that is not wired up yet.
    page.goto(base, wait_until="load")
    page.click("#auth-toggle")  # switch to the create-account form
    expect(page.locator("#auth-submit")).to_have_text("Create account", timeout=5_000)
    page.fill("#auth-name", name)
    page.fill("#auth-email", email)
    page.fill("#auth-password", PASSWORD)
    page.click("#auth-submit")
    expect(page.locator("#app")).to_be_visible(timeout=10_000)


def type_at_end(page: Page, text: str) -> None:
    """Type character by character into the page editor, the way a person would.

    Newlines arrive as Enter presses, so this also exercises the editor's own
    block splitting rather than a pasted buffer.
    """
    editor = page.locator("#preview")
    editor.click()
    page.keyboard.press("Control+End")
    editor.press_sequentially(text, delay=4)


def new_page(page: Page) -> None:
    """Create a page and wait until it, not the one before it, is open and live.

    The connection chip still reads "live" for the previous page until the new
    one has been created, so waiting on the chip alone starts typing too soon.
    """
    before = page.url
    page.click("#new-doc")
    page.wait_for_url(lambda url: url != before and "?doc=" in url, timeout=10_000)
    expect(page.locator("#connection-text")).to_have_text("live", timeout=10_000)


def main(base: str, screenshots: bool) -> int:
    tag = uuid.uuid4().hex[:6]
    alice_email = "alice-%s@example.com" % tag
    bob_email = "bob-%s@example.com" % tag
    shots = ROOT / "artifacts"
    if screenshots:
        shots.mkdir(exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        # Separate contexts: separate localStorage, so two real sessions.
        alice_context = browser.new_context(viewport={"width": 1440, "height": 900})
        bob_context = browser.new_context(viewport={"width": 1440, "height": 900})
        alice = alice_context.new_page()
        bob = bob_context.new_page()
        watch_console(alice, "alice")
        watch_console(bob, "bob")

        print("\n--- sign-up and the workspace ---")
        register(alice, base, "Alice Chen", alice_email)
        check(alice.locator("#app").is_visible(), "Alice reaches the workspace")
        register(bob, base, "Bob Ortiz", bob_email)
        check(bob.locator("#app").is_visible(), "Bob reaches the workspace")
        if screenshots:
            alice.screenshot(path=str(shots / "01-workspace-empty.png"))

        print("\n--- creating and sharing a document ---")
        alice.click("#new-doc")
        expect(alice.locator("#connection-text")).to_have_text("live", timeout=10_000)
        check(True, "Alice's editor socket reports live")
        alice.fill("#doc-title", "Two Styles, One Document")
        alice.locator("#doc-title").press("Enter")

        alice.click('#tabs button[data-tab="share"]')
        alice.fill("#invite-email", bob_email)
        alice.select_option("#invite-role", "EDITOR")
        alice.click('#invite-form button[type="submit"]')
        expect(alice.locator("#people-list li")).to_have_count(2, timeout=10_000)
        check(True, "Bob appears in the collaborator list as an editor")

        print("\n--- Bob opens the shared document ---")
        bob.reload(wait_until="load")
        expect(bob.locator("#doc-list li")).to_have_count(1, timeout=10_000)
        bob.click("#doc-list li")
        expect(bob.locator("#connection-text")).to_have_text("live", timeout=10_000)
        check(
            bob.locator("#doc-title").input_value() == "Two Styles, One Document",
            "Bob sees the shared document and its title",
        )

        print("\n--- presence ---")
        expect(alice.locator("#presence .avatar")).to_have_count(2, timeout=10_000)
        check(True, "Alice sees two people in the room")
        expect(bob.locator("#presence .avatar")).to_have_count(2, timeout=10_000)
        check(True, "Bob sees two people in the room")

        print("\n--- Alice types, Bob must see it ---")
        type_at_end(alice, ALICE_TEXT)
        expect(bob.locator("#source")).to_have_value(ALICE_TEXT, timeout=15_000)
        check(True, "Alice's keystrokes arrive in Bob's editor")
        check(
            bob.locator("#preview h1").inner_text() == "Quarterly Research Summary",
            "Bob's preview renders the heading Alice typed",
        )

        print("\n--- Bob types, Alice must see it ---")
        type_at_end(bob, BOB_TEXT)
        expected = ALICE_TEXT + BOB_TEXT
        expect(alice.locator("#source")).to_have_value(expected, timeout=15_000)
        check(True, "Bob's keystrokes arrive in Alice's editor")
        check(
            alice.locator("#source").input_value() == bob.locator("#source").input_value(),
            "Both browsers hold identical text",
        )

        print("\n--- AI authorship attribution in the page ---")
        for who, page in (("alice", alice), ("bob", bob)):
            value = page.locator("#source").input_value()
            snapshot = page.evaluate(
                "() => ({conn: document.getElementById('connection-text').textContent,"
                " blocks: document.querySelectorAll('#preview .block').length})"
            )
            print(
                "      %s: %d chars, socket=%s, preview blocks=%d"
                % (who, len(value), snapshot["conn"], snapshot["blocks"])
            )
        server_state = alice.evaluate(
            """async () => {
                const t = localStorage.getItem('collabdoc.token');
                const id = new URLSearchParams(location.search).get('doc');
                const r = await fetch('/api/documents/' + id, {headers: {Authorization: 'Bearer ' + t}});
                const j = await r.json();
                return {chars: j.content.length, version: j.current_version};
            }"""
        )
        print("      server: %d chars at v%d" % (server_state["chars"], server_state["version"]))

        alice.click('#tabs button[data-tab="authors"]')
        # The attribution pass is debounced ~1.5s after typing stops.
        expect(alice.locator("#preview .block[data-author]")).to_have_count(
            10, timeout=30_000
        )
        check(True, "Every block in the preview carries an attributed author")

        authors = alice.eval_on_selector_all(
            "#preview .block[data-author]",
            "nodes => nodes.map(n => ({"
            "  author: n.dataset.author,"
            "  colour: n.style.getPropertyValue('--author-color'),"
            "  who: n.querySelector('.who')?.textContent || ''"
            "}))",
        )
        distinct = {entry["author"] for entry in authors}
        check(len(distinct) == 2, "Two different authors are attributed across the blocks")
        check(
            all(entry["colour"].startswith("#") for entry in authors),
            "Each block is tinted with its author's presence colour",
        )
        names = " | ".join(entry["who"] for entry in authors)
        check("Alice Chen" in names and "Bob Ortiz" in names,
              "Both authors are named on the blocks")
        print("      %s" % names)

        expect(alice.locator("#model-state")).to_contain_text("authors", timeout=20_000)
        model = alice.locator("#model-state").inner_text()
        check("authors" in model, "The stylometric model trained: %r" % model)
        expect(alice.locator("#block-list li")).to_have_count(10, timeout=10_000)
        check(True, "The Authors panel lists every block with its confidence")

        if screenshots:
            alice.screenshot(path=str(shots / "02-editor-alice.png"), full_page=False)
            bob.screenshot(path=str(shots / "03-editor-bob.png"), full_page=False)

        print("\n--- integrity report ---")
        alice.click("#open-integrity")
        expect(alice.locator("#integrity-body .integrity-tile")).to_have_count(4, timeout=20_000)
        rows = alice.locator("#integrity-body .integrity-table tbody tr")
        check(rows.count() == 2 and "Bob Ortiz" in rows.all_inner_texts()[0] + rows.all_inner_texts()[1],
              "The integrity report credits both writers")
        check(alice.locator("#integrity-body .integrity-tile").nth(1).inner_text().startswith("100%"),
              "Text typed live is reported as typed, not pasted")
        with alice.expect_download() as download:
            alice.click("#integrity-download")
        check(download.value.suggested_filename.endswith("integrity report.html"),
              "The report downloads as a printable page")
        if screenshots:
            alice.screenshot(path=str(shots / "12-integrity.png"))
        alice.keyboard.press("Escape")

        print("\n--- undo, formatting, and history ---")
        before = alice.locator("#source").input_value()
        type_at_end(alice, "TYPO")
        expect(alice.locator("#source")).to_have_value(before + "TYPO", timeout=10_000)
        alice.click("#undo")
        expect(alice.locator("#source")).to_have_value(before, timeout=10_000)
        check(True, "Undo removes the local edit")
        expect(bob.locator("#source")).to_have_value(before, timeout=15_000)
        check(True, "The undo propagates to Bob")

        alice.click('#tabs button[data-tab="history"]')
        # History groups each author's editing burst into one entry, so two
        # people typing produce a handful of entries, not one per keystroke.
        alice.wait_for_function(
            "document.querySelectorAll('#version-list li .who').length >= 2", timeout=15_000
        )
        count = alice.locator("#version-list li").count()
        check(count >= 2, "Version history lists the editing bursts (%d entries)" % count)
        alice.locator("#version-list li").last.click()
        expect(alice.locator("#version-modal")).to_be_visible(timeout=10_000)
        check(True, "A version opens for preview")
        if screenshots:
            alice.screenshot(path=str(shots / "04-version-history.png"))
        alice.click("#version-close")

        print("\n--- the block editor ---")
        before = alice.locator("#source").input_value()
        type_at_end(alice, "/h2")
        expect(alice.locator("#slash-menu")).to_be_visible(timeout=5_000)
        check(True, "Typing / opens the block menu")
        alice.keyboard.press("Enter")
        alice.keyboard.type("Next steps")
        alice.keyboard.press("Enter")
        alice.keyboard.type("[] ship it")
        expected = before + "## Next steps\n- [ ] ship it"
        expect(alice.locator("#source")).to_have_value(expected, timeout=10_000)
        check(True, "The slash menu turns a line into a heading; [] starts a to-do")
        expect(bob.locator("#preview h2")).to_have_text("Next steps", timeout=15_000)
        check(True, "Bob sees the new heading rendered")
        alice.locator("#preview .todo-box").last.click()
        expected = expected.replace("- [ ] ship it", "- [x] ship it")
        expect(alice.locator("#source")).to_have_value(expected, timeout=10_000)
        check(True, "Ticking a to-do edits the shared text")
        hidden = alice.eval_on_selector_all(
            "#preview .mk",
            "nodes => nodes.every(n => n.classList.contains('icon') || getComputedStyle(n).display === 'none')",
        )
        check(hidden, "Markup characters are never shown on the page")

        # Select the last word with the keyboard and bold it from the bubble.
        alice.locator("#preview").focus()
        alice.keyboard.press("Control+End")
        alice.keyboard.press("Control+Shift+ArrowLeft")
        expect(alice.locator("#bubble")).to_be_visible(timeout=5_000)
        check(True, "Selecting text shows the formatting bubble")
        alice.locator('#bubble [data-inline="bold"]').click()
        expected = expected.replace("- [x] ship it", "- [x] ship **it**")
        expect(alice.locator("#source")).to_have_value(expected, timeout=10_000)
        check(
            alice.locator("#preview strong").last.inner_text() == "it",
            "Bold from the bubble wraps the selection and renders it bold",
        )

        print("\n--- comments, search and activity ---")
        alice.click('#tabs button[data-tab="comments"]')
        alice.fill("#comment-body", "Can we date the release?")
        alice.click('#comment-form button[type="submit"]')
        expect(alice.locator("#thread-list .thread")).to_have_count(1, timeout=10_000)
        check(True, "Alice's comment appears in the thread list")
        expect(bob.locator("#comment-count")).to_have_text("1", timeout=10_000)
        check(True, "Bob's comment count updates live")

        alice.keyboard.press("Control+k")
        expect(alice.locator("#palette")).to_be_visible(timeout=5_000)
        alice.keyboard.type("Quarterly Research")
        expect(alice.locator("#palette-results .palette-item").first).to_contain_text(
            "Two Styles", timeout=10_000
        )
        check(True, "Search finds the page by its text")
        alice.keyboard.press("Escape")

        alice.click('#tabs button[data-tab="activity"]')
        expect(alice.locator("#activity-chart svg .segment").first).to_be_visible(timeout=10_000)
        rows = alice.locator("#activity-table tbody tr").count()
        check(rows == 2, "The activity panel charts writing for both authors (%d rows)" % rows)
        expect(alice.locator("#activity-feed li").first).to_contain_text(re.compile(r"edit"), timeout=10_000)
        check(True, "Recent activity lists editing sessions, not keystrokes")
        if screenshots:
            alice.screenshot(path=str(shots / "05-comments-activity.png"))

        print("\n--- mentions and the inbox ---")
        shared_url = alice.url
        type_at_end(alice, "\n\nPing @Bo")
        expect(alice.locator("#slash-menu")).to_contain_text("Bob Ortiz", timeout=5_000)
        check(True, "Typing @ offers the people on the page")
        alice.keyboard.press("Enter")
        expect(alice.locator("#source")).to_have_value(
            re.compile(r"Ping @\[Bob Ortiz\]\(user:[0-9a-f-]{36}\) "), timeout=10_000
        )
        check(True, "Choosing a person writes a mention into the text")
        expect(bob.locator("#inbox-count")).to_be_visible(timeout=10_000)
        check(True, "Bob's inbox badge lights up live")
        bob.click("#open-inbox")
        expect(bob.locator("#inbox-list")).to_contain_text("mentioned you in", timeout=5_000)
        check(True, "Bob's inbox says Alice mentioned him")
        expect(bob.locator("#preview .mention")).to_have_count(1, timeout=10_000)
        check(True, "Bob's page shows the mention as a chip")
        bob.keyboard.press("Escape")

        print("\n--- code blocks, tables and images ---")
        type_at_end(alice, "\n\n```py\nx = 1")
        expect(alice.locator("#source")).to_have_value(re.compile(r"```py\nx = 1\n```"), timeout=10_000)
        check(True, "Typing ``` and Enter opens a closed code block")
        expect(bob.locator('#preview .line[data-type="code"] .tok-number')).to_have_count(1, timeout=15_000)
        check(True, "Bob sees the code block, highlighted")

        alice.locator("#preview").click()
        alice.keyboard.press("Control+End")
        alice.keyboard.press("Enter")
        alice.keyboard.press("Enter")
        alice.locator("#preview").press_sequentially("/table", delay=4)
        expect(alice.locator("#slash-menu")).to_contain_text("Table", timeout=5_000)
        alice.keyboard.press("Enter")
        alice.keyboard.type("Owner")
        expect(alice.locator("#source")).to_have_value(re.compile(r"\| Owner \| Column 2 \| Column 3 \|"), timeout=10_000)
        check(True, "/table inserts a table and selects its first header")
        expect(bob.locator('#preview .line.table-head .cell')).to_have_count(3, timeout=15_000)
        check(True, "Bob sees the table with a header row of three cells")

        alice.locator("#preview").click()
        alice.keyboard.press("Control+End")
        alice.set_input_files(
            "#image-input",
            files=[{"name": "dot.png", "mimeType": "image/png", "buffer": TINY_PNG}],
        )
        expect(alice.locator("#source")).to_have_value(
            re.compile(r"!\[dot\]\(/api/files/[0-9a-f-]{36}\)"), timeout=10_000
        )
        check(True, "Uploading an image puts it on its own line")
        bob.wait_for_function(
            "[...document.querySelectorAll('#preview .line[data-type=image] img')].some(i => i.naturalWidth > 0)",
            timeout=15_000,
        )
        check(True, "Bob's browser loads the image with his own session")

        print("\n--- nested pages and the trash ---")
        alice.click("#page-more")
        alice.click('#more-menu [data-action="add-subpage"]')
        expect(alice.locator("#crumb-parents .crumb-step")).to_have_count(1, timeout=10_000)
        check(True, "A sub-page shows its parent in the breadcrumbs")
        alice.fill("#doc-title", "Meeting notes")
        alice.keyboard.press("Enter")
        child_row = alice.locator("#doc-list li", has_text="Meeting notes")
        expect(child_row).to_have_count(1, timeout=10_000)
        check(child_row.evaluate("li => getComputedStyle(li).getPropertyValue('--depth').trim()") == "1",
              "The sidebar nests it one level under its parent")
        expect(bob.locator("#doc-list li", has_text="Meeting notes")).to_have_count(0)
        bob.reload(wait_until="load")
        # Collapsed by default: Bob opens the shared page's branch.
        bob.locator("#doc-list li .tree-caret:not(.leaf)").first.click()
        expect(bob.locator("#doc-list li", has_text="Meeting notes")).to_have_count(1, timeout=10_000)
        check(True, "Bob can see the sub-page through the shared parent")

        alice.click("#page-more")
        alice.click('#more-menu [data-action="delete-page"]')
        alice.click('#more-menu [data-action="delete-page"]')
        expect(alice.locator("#doc-list li", has_text="Meeting notes")).to_have_count(0, timeout=10_000)
        check(True, "Moving a page to the trash removes it from the sidebar")
        alice.click("#open-trash")
        expect(alice.locator("#trash-list")).to_contain_text("Meeting notes", timeout=5_000)
        alice.locator("#trash-list li", has_text="Meeting notes").locator("button", has_text="Restore").click()
        expect(alice.locator("#doc-list li", has_text="Meeting notes")).to_have_count(1, timeout=10_000)
        check(True, "Restoring from the trash brings it back")
        alice.keyboard.press("Escape")

        print("\n--- publish to web ---")
        alice.goto(shared_url, wait_until="load")
        expect(alice.locator("#connection-text")).to_have_text("live", timeout=10_000)
        alice.click('#tabs button[data-tab="share"]')
        alice.check("#publish-toggle")
        expect(alice.locator("#publish-url")).to_have_value(re.compile(r"/p/[0-9a-f-]{36}$"), timeout=10_000)
        public_url = alice.locator("#publish-url").input_value()
        visitor = browser.new_context().new_page()
        visitor.goto(public_url, wait_until="load")
        check(visitor.locator("h1.title").count() == 1 and visitor.locator("table").count() == 1,
              "A visitor with no account reads the published page, table and all")
        # The image is loading="lazy", so it may still be in flight at "load".
        image = visitor.locator("figure img")
        image.scroll_into_view_if_needed()
        check(image.evaluate(
            "i => i.decode().then(() => i.naturalWidth > 0, () => false)"),
              "The published page serves its images without a session")
        visitor.context.close()
        alice.uncheck("#publish-toggle")
        expect(alice.locator("#publish-url")).to_have_value("", timeout=10_000)

        print("\n--- version compare ---")
        alice.click('#tabs button[data-tab="history"]')
        alice.locator("#version-list li").last.click()
        expect(alice.locator("#version-modal")).to_be_visible(timeout=10_000)
        alice.check("#version-compare")
        expect(alice.locator("#version-content .diff-add").first).to_be_visible(timeout=5_000)
        check(True, "Comparing an old version highlights what was added since")
        if screenshots:
            alice.screenshot(path=str(shots / "06-version-compare.png"))
        alice.uncheck("#version-compare")
        alice.click("#version-close")
        if screenshots:
            alice.locator("#preview").click()
            alice.keyboard.press("Control+End")
            alice.screenshot(path=str(shots / "07-new-blocks.png"))

        print("\n--- M8: contents, find and replace, assistant, lock ---")
        new_page(alice)
        type_at_end(alice, "# Alpha\nThe quarterly plan focuses on shipping the new editor features this month.\nfoo bar foo\n## Beta\n")
        alice.keyboard.type("/toc")
        expect(alice.locator("#slash-menu")).to_be_visible(timeout=5_000)
        alice.keyboard.press("Enter")
        expect(alice.locator("#preview .toc .toc-item")).to_have_count(2, timeout=5_000)
        check(True, "/toc renders a live table of contents of the headings")

        alice.keyboard.type("- [ ] Ship the release ")
        alice.keyboard.type("/due")
        alice.keyboard.press("Enter")
        expect(alice.locator("#preview .due").first).to_be_visible(timeout=5_000)
        check(True, "/due puts a date chip on a to-do")

        alice.keyboard.press("Control+h")
        expect(alice.locator("#find-bar")).to_be_visible(timeout=5_000)
        alice.fill("#find-input", "foo")
        expect(alice.locator("#find-count")).to_have_text("1 of 2", timeout=5_000)
        alice.fill("#replace-input", "baz")
        alice.click("#replace-all")
        expect(alice.locator("#preview")).to_contain_text("baz bar baz", timeout=5_000)
        check(True, "Find counts matches and Replace all rewrites them")
        alice.keyboard.press("Escape")

        alice.click("#ai-button")
        expect(alice.locator("#ai-modal")).to_be_visible(timeout=5_000)
        alice.click('#ai-actions [data-ai="summarize"]')
        expect(alice.locator("#ai-source")).to_contain_text("summary", timeout=15_000)
        check(alice.locator("#ai-result .line").count() > 0, "The writing assistant summarises the page")
        if screenshots:
            alice.screenshot(path=str(shots / "08-assistant.png"))
        alice.keyboard.press("Escape")

        alice.click("#page-more")
        alice.click('#more-menu [data-action="toggle-lock"]')
        expect(alice.locator("#lock-chip")).to_be_visible(timeout=5_000)
        check(True, "The owner can lock a page")
        alice.click("#page-more")
        alice.click('#more-menu [data-action="toggle-lock"]')
        expect(alice.locator("#lock-chip")).to_be_hidden(timeout=5_000)

        print("\n--- M8: replay, graph, tasks, home ---")
        alice.click('#tabs button[data-tab="history"]')
        alice.click("#open-replay")
        expect(alice.locator("#replay-sub")).to_contain_text("operations", timeout=10_000)
        alice.click('#replay-speed [data-speed="64"]')
        alice.click("#replay-play")
        expect(alice.locator("#replay-content")).to_contain_text("Alpha", timeout=15_000)
        check(True, "History replay plays the page back keystroke by keystroke")
        if screenshots:
            alice.screenshot(path=str(shots / "09-replay.png"))
        alice.keyboard.press("Escape")

        alice.click("#open-graph")
        expect(alice.locator("svg.graph .node").first).to_be_visible(timeout=10_000)
        check(alice.locator("svg.graph .node").count() >= 2, "The graph view draws every page")
        alice.wait_for_timeout(1500)  # let the layout settle
        first = alice.locator("svg.graph .node").first
        box = first.bounding_box()
        others = alice.locator("svg.graph .node").evaluate_all(
            "nodes => nodes.slice(1).map(n => n.getAttribute('transform'))")
        alice.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        alice.mouse.down()
        alice.mouse.move(box["x"] + 160, box["y"] + 90, steps=12)
        alice.mouse.up()
        alice.wait_for_timeout(600)
        after = alice.locator("svg.graph .node").evaluate_all(
            "nodes => nodes.slice(1).map(n => n.getAttribute('transform'))")
        check(after != others, "Dragging a page moves the pages around it")
        first.click()
        expect(alice.locator("#graph-card")).to_be_visible(timeout=5_000)
        check(alice.locator("#graph-card").inner_text().count("words") == 1,
              "Clicking a page shows its details card")
        before_zoom = alice.locator("svg.graph .viewport").get_attribute("transform")
        alice.click("#graph-zoom-in")
        check(alice.locator("svg.graph .viewport").get_attribute("transform") != before_zoom,
              "The graph zooms")
        alice.fill("#graph-search", "Two Styles")
        check(alice.locator("svg.graph .node.match").count() == 1, "Search highlights the matching page")
        alice.locator("#graph-search").press("Enter")
        alice.click("#graph-fit")
        if screenshots:
            alice.screenshot(path=str(shots / "10-graph.png"))
        alice.keyboard.press("Escape")

        alice.click("#open-tasks")
        expect(alice.locator("#task-groups")).to_contain_text("Ship the release", timeout=10_000)
        check(True, "My tasks lists the dated to-do")
        alice.keyboard.press("Escape")

        alice.click("#open-home")
        expect(alice.locator("#home-greeting")).to_contain_text("Good", timeout=10_000)
        expect(alice.locator("#home-tasks")).to_contain_text("Ship the release", timeout=10_000)
        check(alice.locator("#home-words").inner_text() != "0", "Home shows this week's words and due tasks")
        if screenshots:
            alice.screenshot(path=str(shots / "11-home.png"))

        print("\n--- M8: two-factor sign-in and API tokens ---")
        sys.path.insert(0, str(ROOT / "server"))
        from app.totp import totp  # noqa: E402

        alice.click("#account")
        alice.click("#open-security")
        expect(alice.locator("#security-modal")).to_be_visible(timeout=5_000)
        alice.click("#mfa-submit")
        expect(alice.locator("#mfa-setup")).to_be_visible(timeout=5_000)
        secret = alice.locator("#mfa-secret").inner_text().replace(" ", "")
        alice.fill("#mfa-code", totp(secret))
        alice.click("#mfa-submit")
        expect(alice.locator("#mfa-state")).to_have_text("On", timeout=5_000)
        check(True, "Two-factor sign-in turns on with a code from the secret")
        alice.fill("#token-name", "Browser check")
        alice.locator("#token-form button[type=submit]").click()
        expect(alice.locator("#token-value")).to_contain_text("cd_", timeout=5_000)
        check(alice.locator("#token-list li").count() == 1, "An API token is created and listed")
        alice.keyboard.press("Escape")

        second = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
        second.goto(base, wait_until="load")
        second.fill("#auth-email", alice_email)
        second.fill("#auth-password", PASSWORD)
        second.click("#auth-submit")
        expect(second.locator("#mfa-field")).to_be_visible(timeout=10_000)
        second.fill("#auth-code", totp(secret))
        second.click("#auth-submit")
        expect(second.locator("#app")).to_be_visible(timeout=10_000)
        check(True, "Signing in with 2FA on asks for the code, then lets you in")
        second.context.close()

        alice.click("#account")
        alice.click("#open-security")
        expect(alice.locator("#mfa-state")).to_have_text("On", timeout=5_000)
        alice.fill("#mfa-code", totp(secret))
        alice.click("#mfa-submit")
        expect(alice.locator("#mfa-state")).to_have_text("Off", timeout=5_000)
        alice.keyboard.press("Escape")

        print("\n--- a viewer gets a read-only surface ---")
        alice.goto(shared_url, wait_until="load")
        alice.click('#tabs button[data-tab="share"]')
        alice.select_option("#people-list select", "VIEWER")
        bob.reload(wait_until="load")
        bob.click("#doc-list li")
        expect(bob.locator("#connection-text")).to_have_text("live", timeout=10_000)
        check(bob.locator("#source").get_attribute("readonly") is not None,
              "Bob's editor is read-only after being demoted to viewer")
        check(bob.locator('.toolbar button[data-format="bold"]').is_disabled(),
              "Bob's formatting controls are disabled")
        check(bob.locator("#preview").get_attribute("contenteditable") == "false",
              "Bob's page cannot be edited")

        browser.close()

    print("\n--- console ---")
    check(not console_errors, "No JavaScript errors in either browser")
    for error in console_errors[:10]:
        print("      %s" % error)

    passed = sum(1 for ok, _ in results if ok)
    print("\n%d/%d checks passed" % (passed, len(results)))
    if screenshots:
        print("Screenshots in %s" % (ROOT / "artifacts"))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--no-screenshots", action="store_true")
    arguments = parser.parse_args()
    raise SystemExit(main(arguments.base.rstrip("/"), not arguments.no_screenshots))
