"""Audit log, rate limiting, security headers, OAuth sign-in, the writing
assistant, and Kafka fan-out."""
from __future__ import annotations

import asyncio
import urllib.parse
import uuid
from types import SimpleNamespace

import pytest

from app import assistant
from app.config import get_settings
from app.realtime.broadcaster import KafkaBroadcaster
from app.routers import oauth


def share(owner, document_id, guest, role="EDITOR"):
    response = owner.post(
        "/api/documents/%s/collaborators" % document_id, json={"email": guest.email, "role": role}
    )
    assert response.status_code == 201, response.text


# -- audit log ------------------------------------------------------------------


def test_the_audit_log_records_sharing_and_lifecycle(alice, bob):
    document_id = alice.new_document("Plans")
    share(alice, document_id, bob, "VIEWER")
    alice.patch("/api/documents/%s/collaborators/%s" % (document_id, bob.id), json={"role": "EDITOR"})
    alice.patch("/api/documents/%s" % document_id, json={"title": "Q3 plans"})
    alice.put("/api/documents/%s/share" % document_id, json={"role": "VIEWER"})
    alice.put("/api/documents/%s/publish" % document_id, json={"published": True})

    log = alice.get("/api/documents/%s/audit" % document_id).json()
    actions = [entry["action"] for entry in log]
    assert actions == ["published", "link_sharing", "renamed", "role_changed", "invited", "created"]
    assert log[2]["detail"] == "Plans → Q3 plans"
    assert log[4]["detail"] == "%s as viewer" % bob.email
    assert all(entry["actor"]["id"] == alice.id for entry in log)


def test_the_audit_log_follows_the_trash(alice):
    document_id = alice.new_document("Temporary")
    alice.delete("/api/documents/%s" % document_id)
    alice.post("/api/documents/%s/restore" % document_id)
    actions = [entry["action"] for entry in alice.get("/api/documents/%s/audit" % document_id).json()]
    assert actions[:2] == ["restored", "trashed"]


def test_viewers_cannot_read_the_audit_log(alice, bob):
    document_id = alice.new_document("Plans")
    share(alice, document_id, bob, "VIEWER")
    assert bob.get("/api/documents/%s/audit" % document_id).status_code == 403


# -- rate limiting and headers ---------------------------------------------------


def test_sign_in_is_rate_limited(client, alice, monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_rate_limit", 3)
    attempts = [
        client.post("/api/auth/login", json={"email": alice.email, "password": "wrong-password"})
        for _ in range(4)
    ]
    assert [r.status_code for r in attempts] == [401, 401, 401, 429]
    assert int(attempts[-1].headers["retry-after"]) >= 1


def test_responses_carry_security_headers(client):
    response = client.get("/api/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert "strict-origin" in response.headers["referrer-policy"]


# -- OAuth ---------------------------------------------------------------------


@pytest.fixture()
def github(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "github_client_id", "client-123")
    monkeypatch.setattr(settings, "github_client_secret", "secret-456")
    calls = []
    # A fresh identity per test: the test database is shared across tests.
    uid = uuid.uuid4().int % 10**9
    profile = {"id": uid, "name": "Grace Hopper", "login": "grace", "avatar_url": None}
    emails = [{"email": "grace-%d@example.com" % uid, "primary": True, "verified": True}]

    async def fake_http(method, url, data=None, headers=None):
        calls.append((method, url, data))
        if url == oauth.PROVIDERS["github"].token_url:
            return {"access_token": "gho_token"} if data["code"] == "good-code" else {"error": "bad_verification_code"}
        if url.endswith("/user"):
            return profile
        if url.endswith("/user/emails"):
            return emails
        raise AssertionError(url)

    monkeypatch.setattr(oauth, "http_json", fake_http)
    return SimpleNamespace(calls=calls, profile=profile, emails=emails)


def begin(client):
    response = client.get("/api/auth/oauth/github/start", follow_redirects=False)
    assert response.status_code == 302
    query = urllib.parse.parse_qs(urllib.parse.urlparse(response.headers["location"]).query)
    return query


def finish(client, state, code="good-code"):
    response = client.get(
        "/api/auth/oauth/github/callback",
        params={"code": code, "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 302
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(response.headers["location"]).fragment))


def test_only_configured_providers_are_offered(client, github):
    assert client.get("/api/auth/providers").json() == [{"id": "github", "label": "GitHub"}]
    assert client.get("/api/auth/oauth/google/start", follow_redirects=False).status_code == 404


def test_github_sign_in_creates_an_account_and_issues_a_token(client, github):
    query = begin(client)
    assert query["client_id"] == ["client-123"]
    assert query["code_challenge_method"] == ["S256"]
    fragment = finish(client, query["state"][0])
    token = fragment["oauth_token"]
    me = client.get("/api/auth/me", headers={"Authorization": "Bearer %s" % token}).json()
    assert me["email"] == github.emails[0]["email"] and me["name"] == "Grace Hopper"
    # The PKCE verifier went to the token endpoint and matches the challenge.
    import base64, hashlib

    verifier = github.calls[0][2]["code_verifier"]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert query["code_challenge"] == [challenge]

    # Signing in again finds the same account.
    again = finish(client, begin(client)["state"][0])
    me_again = client.get("/api/auth/me", headers={"Authorization": "Bearer %s" % again["oauth_token"]}).json()
    assert me_again["id"] == me["id"]


def test_a_verified_email_links_to_the_existing_account(client, github, register):
    existing = register("Grace")
    github.emails[0]["email"] = existing.email
    fragment = finish(client, begin(client)["state"][0])
    me = client.get("/api/auth/me", headers={"Authorization": "Bearer %s" % fragment["oauth_token"]}).json()
    assert me["id"] == existing.id


def test_an_unverified_email_never_links_to_an_account(client, github, register):
    existing = register("Victim")
    github.emails[0].update(email=existing.email, verified=False)
    fragment = finish(client, begin(client)["state"][0])
    me = client.get("/api/auth/me", headers={"Authorization": "Bearer %s" % fragment["oauth_token"]}).json()
    assert me["id"] != existing.id


def test_a_callback_from_another_browser_is_refused(client, github):
    state = begin(client)["state"][0]
    client.cookies.clear()
    fragment = finish(client, state)
    assert "oauth_token" not in fragment and "browser" in fragment["oauth_error"]


def test_a_forged_state_or_bad_code_is_refused(client, github):
    begin(client)
    assert "oauth_error" in finish(client, "not-a-real-state")
    assert "oauth_error" in finish(client, begin(client)["state"][0], code="stolen-code")


def test_oauth_accounts_cannot_sign_in_with_a_password(client, github):
    finish(client, begin(client)["state"][0])
    response = client.post("/api/auth/login", json={"email": github.emails[0]["email"], "password": "oauth$github"})
    assert response.status_code == 401


# -- writing assistant ---------------------------------------------------------

PAGE = (
    "# Launch review\n"
    "The launch slipped by two weeks because the payment provider changed its API.\n"
    "Payment retries now run in a background queue, which fixed the timeouts.\n"
    "Marketing wants the launch date confirmed before the press briefing.\n"
    "```\nprint('not prose')\n```\n"
    "We agreed the payment queue needs monitoring before the launch goes ahead.\n"
)


def test_summaries_work_without_an_api_key(alice):
    document_id = alice.new_document("Review", content=PAGE)
    response = alice.post("/api/documents/%s/assist" % document_id, json={"action": "summarize"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "local"
    assert body["text"].startswith("- ")
    assert "print(" not in body["text"]


def test_writing_actions_say_they_need_the_ai_service(alice):
    document_id = alice.new_document("Review", content=PAGE)
    response = alice.post(
        "/api/documents/%s/assist" % document_id, json={"action": "improve", "text": "teh plan"}
    )
    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


def test_viewers_may_summarise_but_not_generate(alice, bob):
    document_id = alice.new_document("Review", content=PAGE)
    share(alice, document_id, bob, "VIEWER")
    assert bob.post("/api/documents/%s/assist" % document_id, json={"action": "summarize"}).status_code == 200
    assert bob.post("/api/documents/%s/assist" % document_id, json={"action": "continue"}).status_code == 403


def test_claude_is_used_when_configured(alice, monkeypatch):
    sent = {}

    class Messages:
        async def create(self, **kwargs):
            sent.update(kwargs)
            return SimpleNamespace(
                stop_reason="end_turn",
                model=kwargs["model"],
                content=[SimpleNamespace(type="text", text="The plan is clear and on schedule.")],
            )

    fake = SimpleNamespace(beta=SimpleNamespace(messages=Messages()))
    monkeypatch.setattr(get_settings(), "ai_provider", "claude")
    monkeypatch.setattr(assistant, "_client", lambda: fake)
    document_id = alice.new_document("Review", content=PAGE)
    body = alice.post(
        "/api/documents/%s/assist" % document_id, json={"action": "improve", "text": "teh plan is clear"}
    ).json()
    assert body == {"text": "The plan is clear and on schedule.", "source": "claude", "model": "claude-opus-5-5"}
    assert sent["model"] == "claude-opus-5-5"
    assert sent["fallbacks"] == "default"
    assert "teh plan is clear" in sent["messages"][0]["content"]


def test_a_refusal_is_reported_not_inserted(alice, monkeypatch):
    class Messages:
        async def create(self, **kwargs):
            return SimpleNamespace(stop_reason="refusal", model=kwargs["model"], content=[])

    monkeypatch.setattr(get_settings(), "ai_provider", "claude")
    monkeypatch.setattr(assistant, "_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=Messages())))
    document_id = alice.new_document("Review", content=PAGE)
    response = alice.post("/api/documents/%s/assist" % document_id, json={"action": "continue"})
    assert response.status_code == 503 and "declined" in response.json()["detail"]


def test_the_assistant_is_rate_limited_per_user(alice, monkeypatch):
    monkeypatch.setattr(get_settings(), "assist_rate_limit", 2)
    document_id = alice.new_document("Review", content=PAGE)
    codes = [
        alice.post("/api/documents/%s/assist" % document_id, json={"action": "summarize"}).status_code
        for _ in range(3)
    ]
    assert codes == [200, 200, 429]


# -- Kafka fan-out ---------------------------------------------------------------


class FakeBroker:
    """One topic, many consumers: every consumer sees every record, in order."""

    def __init__(self):
        self.queues: list[asyncio.Queue] = []

    def producer(self):
        broker = self

        class Producer:
            async def start(self):
                pass

            async def stop(self):
                pass

            async def send_and_wait(self, topic, value, key):
                for queue in broker.queues:
                    await queue.put(SimpleNamespace(key=key, value=value))

        return Producer()

    def consumer(self):
        queue: asyncio.Queue = asyncio.Queue()
        self.queues.append(queue)

        class Consumer:
            async def start(self):
                pass

            async def stop(self):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                return await queue.get()

        return Consumer()


def test_kafka_fan_out_reaches_every_node_in_order():
    async def scenario():
        broker = FakeBroker()
        nodes = [
            KafkaBroadcaster("broker:9092", producer_factory=broker.producer, consumer_factory=broker.consumer)
            for _ in range(2)
        ]
        for node in nodes:
            await node.start()
        received = {0: [], 1: []}

        for index, node in enumerate(nodes):
            async def handler(message, index=index):
                received[index].append(message["n"])

            await node.subscribe("document:a", handler)

        # Node 0 publishes; both nodes deliver to their own sockets, in order.
        for n in range(20):
            await nodes[0].publish("document:a", {"n": n})
        await nodes[1].publish("document:b", {"n": -1})  # nobody listening: dropped
        for _ in range(50):
            if len(received[0]) == len(received[1]) == 20:
                break
            await asyncio.sleep(0.01)
        for node in nodes:
            await node.stop()
        return received, nodes[0].group_id != nodes[1].group_id

    received, distinct_groups = asyncio.run(scenario())
    assert received[0] == list(range(20)) and received[1] == list(range(20))
    assert distinct_groups, "each node needs its own consumer group to see every event"
