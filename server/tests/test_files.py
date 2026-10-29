"""Image uploads, and how code, tables and images are parsed into blocks."""
from __future__ import annotations

import pytest

from app.attribution.blocks import parse
from app.config import get_settings
from app.models import BlockType
from app.publish import render_body

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def upload(account, document_id, data=PNG, mime="image/png", filename="shot.png"):
    return account.client.post(
        "/api/documents/%s/files" % document_id,
        params={"filename": filename},
        content=data,
        headers={**account.headers, "Content-Type": mime},
    )


def test_upload_and_fetch_an_image(alice):
    document_id = alice.new_document("Gallery")
    response = upload(alice, document_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["url"] == "/api/files/%s" % body["id"]
    assert body["size"] == len(PNG)

    fetched = alice.get(body["url"])
    assert fetched.status_code == 200
    assert fetched.content == PNG
    assert fetched.headers["content-type"] == "image/png"
    assert fetched.headers["x-content-type-options"] == "nosniff"


def test_files_are_private_to_people_who_can_open_the_page(alice, bob):
    document_id = alice.new_document("Private")
    url = upload(alice, document_id).json()["url"]
    assert bob.get(url).status_code == 404
    alice.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": bob.email, "role": "VIEWER"},
    )
    assert bob.get(url).status_code == 200
    # Viewers may look but not upload.
    assert upload(bob, document_id).status_code == 403


@pytest.mark.parametrize(
    "data, mime",
    [
        (b"<svg onload=alert(1)>", "image/svg+xml"),
        (b"<html>not a png</html>", "image/png"),
        (PNG, "text/html"),
        (b"", "image/png"),
    ],
)
def test_unsafe_or_mislabelled_uploads_are_refused(alice, data, mime):
    document_id = alice.new_document("Gallery")
    assert upload(alice, document_id, data=data, mime=mime).status_code in (415, 422)


def test_oversized_uploads_are_refused(alice, monkeypatch):
    monkeypatch.setattr(get_settings(), "upload_max_bytes", 32)
    document_id = alice.new_document("Gallery")
    assert upload(alice, document_id).status_code == 413


def test_published_pages_serve_their_own_images_only(alice, client):
    document_id = alice.new_document("Public")
    other = alice.new_document("Other")
    image = upload(alice, document_id).json()["id"]
    foreign = upload(alice, other).json()["id"]
    alice.put("/api/documents/%s/publish" % document_id, json={"published": True})
    assert client.get("/p/%s/files/%s" % (document_id, image)).status_code == 200
    assert client.get("/p/%s/files/%s" % (document_id, foreign)).status_code == 404


# -- parsing ----------------------------------------------------------------


def test_code_tables_and_images_are_not_prose():
    text = "\n".join(
        [
            "Intro paragraph",
            "```python",
            "# not a heading",
            "x = 1",
            "```",
            "| a | b |",
            "|---|---|",
            "![chart](/api/files/00000000-0000-0000-0000-000000000000)",
            "# Real heading",
        ]
    )
    blocks = parse(text)
    prose = {block.content: block.prose for block in blocks}
    assert prose["Intro paragraph"] and prose["# Real heading"]
    assert not any(prose[line] for line in ("```python", "# not a heading", "x = 1", "```", "| a | b |"))
    comment = next(block for block in blocks if block.content == "# not a heading")
    assert comment.block_type is BlockType.PARAGRAPH and comment.text == ""


def test_published_rendering_of_the_new_blocks():
    image_id = "00000000-0000-0000-0000-000000000000"
    html = render_body(
        "```js\nif (a < b) {}\n```\n| Name | Role |\n|---|---|\n| **Ada** | Eng |\n"
        "![a \"chart\"](/api/files/%s)\n- [x] done\n> 💡 Tip" % image_id,
        "doc-id",
    )
    assert '<pre data-lang="js"><code>if (a &lt; b) {}</code></pre>' in html
    assert "<th>Name</th>" in html and "<td><strong>Ada</strong></td>" in html
    assert '<img src="/p/doc-id/files/%s" alt="a &quot;chart&quot;"' % image_id in html
    assert "checked" in html
    assert 'class="callout"' in html


def test_published_links_are_sanitised():
    html = render_body("[click](javascript:alert(1)) [ok](https://example.com) [p](page:abc)", "d")
    assert "javascript:" not in html
    assert 'href="https://example.com"' in html
    assert '<span class="page-link">p</span>' in html


class _FakeS3:
    """Just enough of the boto3 S3 client for S3Storage."""

    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[(Bucket, Key)] = (Body, ContentType)

    def get_object(self, Bucket, Key):
        import io

        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey()
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)][0])}


def test_s3_storage_round_trips_under_its_prefix():
    from app.storage import S3Storage

    client = _FakeS3()
    storage = S3Storage("bucket", client=client)
    storage.put("a.png", PNG, "image/png")
    assert client.objects[("bucket", "uploads/a.png")] == (PNG, "image/png")
    assert storage.get("a.png") == PNG
    assert storage.get("missing.png") is None
