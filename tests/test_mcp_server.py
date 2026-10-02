"""MMN-14: the MCP server at /mcp -- transport, auth, isolation and every tool.

Driven over the real wire (JSON-RPC POSTs through the shared TestClient, which
runs the app lifespan and so the session manager), because the interesting
failures live in the wiring: a route the SPA catch-all swallows, a Host check
that 421s the LAN address, a principal that never reaches the tool.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.db import get_conn, utcnow
from app.services import notes as notes_svc
from app.services import search_index
from app.services import threads as threads_svc
from tests.search_support import add_email, add_event, add_summary, add_transcript

LLM_URL = "https://llm.test/v1/chat/completions"
ACCEPT = "application/json, text/event-stream"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_token(as_client, scope="read", name="agent") -> str:
    resp = as_client.post("/api/tokens", json={"name": name, "scope": scope})
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def rpc(client, token, method, params=None, *, path="/mcp", headers=None):
    hdrs = {"Accept": ACCEPT}
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    hdrs.update(headers or {})
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(path, json=body, headers=hdrs)


def call(client, token, name, **arguments) -> dict:
    """A successful tool call's structured result."""
    resp = rpc(client, token, "tools/call", {"name": name, "arguments": arguments})
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert not result.get("isError"), result["content"][0]["text"]
    return result["structuredContent"]


def call_error(client, token, name, **arguments) -> str:
    resp = rpc(client, token, "tools/call", {"name": name, "arguments": arguments})
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result.get("isError"), result
    return result["content"][0]["text"]


def tool_names(client, token) -> set[str]:
    resp = rpc(client, token, "tools/list")
    assert resp.status_code == 200, resp.text
    return {t["name"] for t in resp.json()["result"]["tools"]}


def user_id(db_path, username) -> int:
    with get_conn(db_path) as conn:
        return conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]


def ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


@pytest.fixture
def llm_env(monkeypatch):
    monkeypatch.setenv("MMN_LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("MMN_LLM_MODEL", "test/model")
    from app.config import reset_settings_cache

    reset_settings_cache()


@pytest.fixture
def world(user_client, isolated_settings):
    """Alice's repository: two threads, three meetings, one transcript, one
    summary with action items, notes, emails and an event."""
    db = isolated_settings.db_path
    alice = user_id(db, "alice")
    with get_conn(db) as conn:
        atlas = threads_svc.create_thread(conn, owner_id=alice, title="Atlas Migration", description="Oracle to Postgres")["id"]
        hiring = threads_svc.create_thread(conn, owner_id=alice, title="Hiring", description=None)["id"]
        standup = threads_svc.create_meeting(conn, thread_id=atlas, owner_id=alice, title="Weekly standup", meeting_at=ago(3))["id"]
        kickoff = threads_svc.create_meeting(conn, thread_id=atlas, owner_id=alice, title="Kickoff", meeting_at=ago(40))["id"]
        loop = threads_svc.create_meeting(conn, thread_id=hiring, owner_id=alice, title="Candidate debrief", meeting_at=ago(1))["id"]
        add_transcript(
            conn,
            standup,
            ("SPEAKER_00", "Morning. Cutover rehearsal went fine."),
            ("SPEAKER_01", "The rollback window is two hours."),
            ("SPEAKER_00", "Then we freeze billing on Friday."),
            ("SPEAKER_01", "Agreed, I will tell finance."),
        )
        conn.execute(
            "UPDATE speaker_map SET display_name = 'Priya' WHERE meeting_id = ? AND speaker_id = 'SPEAKER_00'",
            (standup,),
        )
        add_summary(
            conn,
            standup,
            tldr="Rehearsal fine; billing freeze Friday.",
            summary_md="## Notes\nRollback window two hours.",
            decisions=("Freeze billing Friday",),
            topics=("cutover",),
            actions=("Tell finance about the freeze", "Book the war room"),
        )
        note = notes_svc.create_note(
            conn, thread_id=atlas, meeting_id=standup, title="Rollback plan",
            body="Two hours, agreed with Priya.", source="manual", user_id=alice,
        )
        notes_svc.create_note(
            conn, thread_id=hiring, meeting_id=None, title="Panel", body="Ask about Postgres.",
            source="ai_chat", user_id=alice,
        )
        email_a = add_email(conn, atlas, alice, message_id="m1", subject="Freeze dates", snippet="Friday works?")
        email_b = add_email(conn, atlas, alice, message_id="m2", subject="Re: Freeze dates",
                            snippet="Yes", body="Yes, Friday works for finance.")
        conn.execute("UPDATE thread_emails SET direction = 'outbound' WHERE id = ?", (email_b,))
        event = add_event(conn, atlas, alice, uid="ev1", summary="Cutover night", location="War room")
        item_ids = [r[0] for r in conn.execute("SELECT id FROM action_items ORDER BY idx")]
        search_index.index_all_now(conn)
    return {
        "alice": alice,
        "atlas": atlas,
        "hiring": hiring,
        "standup": standup,
        "kickoff": kickoff,
        "loop": loop,
        "note": note["id"],
        "email_a": email_a,
        "email_b": email_b,
        "event": event,
        "items": item_ids,
    }


@pytest.fixture
def alice_token(user_client):
    return make_token(user_client)


# --------------------------------------------------------------------------- #
# Transport and auth
# --------------------------------------------------------------------------- #


def test_initialize_and_list(client, alice_token):
    resp = rpc(
        client,
        alice_token,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    )
    assert resp.status_code == 200, resp.text
    info = resp.json()["result"]
    assert info["serverInfo"]["name"] == "my-meeting-notes"
    assert "search" in info["instructions"]
    assert "get_meeting_transcript" in tool_names(client, alice_token)


def test_a_2026_07_28_header_on_a_handshake_style_request_is_refused_with_a_reason(client, alice_token):
    """2026-07-28 has no `initialize`: a request declaring that version must
    carry the per-request `_meta` envelope. The earlier mcp-1.x workaround
    (appending the string to the SDK's version list) answered this with a
    2025-era handshake that merely echoed "2026-07-28" -- claiming a protocol
    it did not speak. Now it is a clear JSON-RPC refusal naming what is missing;
    a real 2026-07-28 client is covered by the `modern(...)` tests below."""
    resp = rpc(
        client,
        alice_token,
        "initialize",
        {"protocolVersion": "2026-07-28", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
        headers={"mcp-protocol-version": "2026-07-28"},
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == -32602
    assert "io.modelcontextprotocol/protocolVersion" in error["message"]


def test_trailing_slash_and_a_lan_host_header_both_work(client, alice_token):
    assert rpc(client, alice_token, "tools/list", path="/mcp/").status_code == 200
    # FastMCP's default DNS-rebinding guard would 421 this.
    resp = rpc(client, alice_token, "tools/list", headers={"Host": "192.168.1.20:4020"})
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("token", [None, "", "nonsense", "mmn_not-a-real-token"])
def test_unauthenticated_is_401(client, token):
    resp = rpc(client, token, "tools/list")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith("Bearer")
    assert resp.json()["error"]["code"] == "auth_required"


def test_a_session_cookie_alone_is_not_enough(admin_client):
    """The shared client holds the admin's cookie; /mcp must ignore it."""
    assert rpc(admin_client, None, "tools/list").status_code == 401


def test_revoked_expired_inactive_and_forced_password_change_are_401(
    client, user_client, admin_client, isolated_settings
):
    token = make_token(user_client)
    assert rpc(client, token, "tools/list").status_code == 200
    db = isolated_settings.db_path
    with get_conn(db) as conn:
        conn.execute("UPDATE users SET must_change_password = 1 WHERE username = 'alice'")
    assert rpc(client, token, "tools/list").status_code == 401
    with get_conn(db) as conn:
        conn.execute("UPDATE users SET must_change_password = 0, is_active = 0 WHERE username = 'alice'")
    assert rpc(client, token, "tools/list").status_code == 401
    with get_conn(db) as conn:
        conn.execute("UPDATE users SET is_active = 1 WHERE username = 'alice'")
        conn.execute("UPDATE api_tokens SET expires_at = ?", (ago(0.01),))
    assert rpc(client, token, "tools/list").status_code == 401
    with get_conn(db) as conn:
        conn.execute("UPDATE api_tokens SET expires_at = NULL")
    assert rpc(client, token, "tools/list").status_code == 200
    token_id = user_client.get("/api/tokens").json()["tokens"][0]["id"]
    user_client.delete(f"/api/tokens/{token_id}")
    assert rpc(client, token, "tools/list").status_code == 401


def test_a_session_bearer_works_and_counts_as_read_write(client, user_client):
    names = tool_names(client, user_client._auth["Authorization"][7:])
    assert {"create_note", "append_to_note", "set_action_item_status"} <= names


def test_last_used_at_is_recorded(client, alice_token, user_client):
    assert user_client.get("/api/tokens").json()["tokens"][0]["last_used_at"] is None
    rpc(client, alice_token, "tools/list")
    assert user_client.get("/api/tokens").json()["tokens"][0]["last_used_at"] is not None


def test_switching_the_server_off_is_a_404(client, admin_client, alice_token):
    resp = admin_client.put("/api/settings", json={"values": {"mcp_enabled": False}})
    assert resp.status_code == 200, resp.text
    assert rpc(client, alice_token, "tools/list").status_code == 404
    admin_client.put("/api/settings", json={"values": {"mcp_enabled": True}})
    assert rpc(client, alice_token, "tools/list").status_code == 200


def test_the_spa_catch_all_does_not_shadow_mcp(client):
    resp = client.get("/mcp")
    assert resp.status_code == 401  # ours, not index.html / spa_not_built


# --------------------------------------------------------------------------- #
# Protocol versions and negotiation
# --------------------------------------------------------------------------- #

MODERN = "2026-07-28"
HANDSHAKE_VERSIONS = ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"]


def modern(client, token, method, params=None, *, version=MODERN, name=None, headers=None):
    """A 2026-07-28 request: no handshake, the version in both the
    `MCP-Protocol-Version` header and the per-request `_meta` envelope, and the
    method (and tool name) mirrored into `Mcp-Method` / `Mcp-Name`."""
    hdrs = {"Accept": ACCEPT, "MCP-Protocol-Version": version, "Mcp-Method": method}
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    if name is not None:
        hdrs["Mcp-Name"] = name
    hdrs.update(headers or {})
    body_params = dict(params or {})
    body_params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
    }
    return client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 7, "method": method, "params": body_params}, headers=hdrs
    )


def modern_call(client, token, tool, **arguments):
    resp = modern(client, token, "tools/call", {"name": tool, "arguments": arguments}, name=tool)
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def test_the_server_reports_every_revision_it_speaks():
    from app.mcp_server.server import SUPPORTED_PROTOCOL_VERSIONS

    assert SUPPORTED_PROTOCOL_VERSIONS == (*HANDSHAKE_VERSIONS, MODERN)


@pytest.mark.parametrize("version", HANDSHAKE_VERSIONS)
def test_initialize_agrees_to_every_handshake_revision(client, alice_token, version):
    resp = rpc(
        client,
        alice_token,
        "initialize",
        {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["protocolVersion"] == version


def test_initialize_counter_offers_the_newest_handshake_revision_for_an_unknown_one(client, alice_token):
    """Per the handshake rules: a version the server does not know is answered
    with one it does, and the client decides whether it can live with that."""
    resp = rpc(
        client,
        alice_token,
        "initialize",
        {"protocolVersion": "2099-01-01", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    )
    assert resp.json()["result"]["protocolVersion"] == "2025-11-25"


@pytest.mark.parametrize("version", HANDSHAKE_VERSIONS)
def test_a_negotiated_older_revision_keeps_working_after_the_handshake(client, alice_token, version):
    resp = rpc(client, alice_token, "tools/list", headers={"MCP-Protocol-Version": version})
    assert resp.status_code == 200, resp.text
    assert "search" in {t["name"] for t in resp.json()["result"]["tools"]}


def test_modern_discover_advertises_the_per_request_revision(client, alice_token):
    resp = modern(client, alice_token, "server/discover")
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["supportedVersions"] == [MODERN]
    # Exactly what this server does: tools, and no change notifications.
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["instructions"].startswith("My Meeting Notes")
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "my-meeting-notes"


def test_initialize_advertises_tools_only(client, alice_token):
    resp = rpc(
        client,
        alice_token,
        "initialize",
        {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    )
    caps = resp.json()["result"]["capabilities"]
    assert caps["tools"] == {"listChanged": False}
    assert "prompts" not in caps and "resources" not in caps


def test_no_listen_stream_is_offered_or_held_open(client, alice_token):
    """The MCP Inspector failure (MMN-14): the SDK's default handlers made
    discover promise change notifications, so a client opened a
    `subscriptions/listen` stream that never completes. From a browser that
    pins one of its six per-origin connections for good and starves every
    later request. Now listen is not served: an immediate METHOD_NOT_FOUND
    instead of a held-open stream, and nothing advertises it."""
    resp = modern(
        client, alice_token, "subscriptions/listen", {"notifications": {"toolsListChanged": True}}
    )
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["error"]["code"] == -32601


@pytest.mark.parametrize(
    "method", ["prompts/list", "resources/list", "resources/templates/list", "prompts/get", "resources/read"]
)
def test_prompts_and_resources_are_not_served_on_either_path(client, alice_token, method):
    params = {"name": "x"} if method == "prompts/get" else {"uri": "mmn://x"} if method == "resources/read" else {}
    modern_resp = modern(client, alice_token, method, params, name=params.get("name") or params.get("uri"))
    assert modern_resp.json()["error"]["code"] == -32601
    legacy_resp = rpc(client, alice_token, method, params)
    assert legacy_resp.json()["error"]["code"] == -32601


def test_modern_tools_list_and_call_without_a_handshake(client, alice_token, world):
    resp = modern(client, alice_token, "tools/list")
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["resultType"] == "complete"
    assert "get_meeting_transcript" in {t["name"] for t in result["tools"]}

    out = modern_call(client, alice_token, "list_meetings", since=ago(7)[:10])
    assert not out["isError"]
    assert [m["title"] for m in out["structuredContent"]["meetings"]] == ["Candidate debrief", "Weekly standup"]

    transcript = modern_call(client, alice_token, "get_meeting_transcript", meeting_id=world["standup"], format="text")
    assert "Priya: Morning." in transcript["structuredContent"]["text"]


def test_modern_tool_errors_and_isolation(client, other_user_client, world):
    bob = make_token(other_user_client)
    out = modern_call(client, bob, "get_meeting", meeting_id=world["standup"])
    assert out["isError"] is True
    assert "[not_found]" in out["content"][0]["text"]


def test_modern_scope_hides_and_refuses_write_tools(client, user_client, alice_token, world):
    names = {t["name"] for t in modern(client, alice_token, "tools/list").json()["result"]["tools"]}
    assert not (names & WRITE_TOOLS)
    refused = modern_call(client, alice_token, "create_note", thread_id=world["atlas"], body="x")
    assert refused["isError"] is True and "Unknown tool" in refused["content"][0]["text"]

    rw = make_token(user_client, scope="read_write")
    names = {t["name"] for t in modern(client, rw, "tools/list").json()["result"]["tools"]}
    assert WRITE_TOOLS <= names
    note = modern_call(client, rw, "create_note", thread_id=world["atlas"], body="Via 2026-07-28.", title="Modern")
    assert note["structuredContent"]["source"] == "mcp"


def test_an_unsupported_modern_revision_is_refused_with_the_supported_list(client, alice_token):
    resp = modern(client, alice_token, "tools/list", version="2027-01-01")
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == -32022
    assert error["data"] == {"supported": [MODERN], "requested": "2027-01-01"}


def test_modern_header_and_envelope_must_agree(client, alice_token):
    mismatch = modern(client, alice_token, "tools/list", headers={"Mcp-Method": "tools/call"})
    assert mismatch.status_code == 400 and mismatch.json()["error"]["code"] == -32020

    no_envelope = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Authorization": f"Bearer {alice_token}",
            "Accept": ACCEPT,
            "MCP-Protocol-Version": MODERN,
            "Mcp-Method": "tools/list",
        },
    )
    assert no_envelope.status_code == 400 and no_envelope.json()["error"]["code"] == -32602


def test_modern_requests_still_need_a_token_and_a_lan_host_is_fine(client, alice_token):
    assert modern(client, None, "server/discover").status_code == 401
    assert modern(client, "mmn_bogus", "tools/list").status_code == 401
    lan = modern(client, alice_token, "tools/list", headers={"Host": "192.168.1.20:4020"})
    assert lan.status_code == 200, lan.text


def test_the_settings_listing_reports_the_versions_newest_first(user_client):
    listed = user_client.get("/api/tokens").json()
    assert listed["protocol_versions"] == [MODERN, *reversed(HANDSHAKE_VERSIONS)]


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #

WRITE_TOOLS = {"create_note", "append_to_note", "set_action_item_status"}


def test_read_tokens_never_see_write_tools(client, user_client, alice_token):
    assert not (tool_names(client, alice_token) & WRITE_TOOLS)
    rw = make_token(user_client, scope="read_write")
    assert WRITE_TOOLS <= tool_names(client, rw)


def test_read_tokens_cannot_call_write_tools(client, alice_token, world):
    text = call_error(client, alice_token, "create_note", thread_id=world["atlas"], body="x")
    assert "Unknown tool" in text
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM thread_notes WHERE source = 'mcp'").fetchone()[0] == 0


def test_tools_are_annotated_read_only_or_not(client, user_client):
    rw = make_token(user_client, scope="read_write")
    tools = {t["name"]: t for t in rpc(client, rw, "tools/list").json()["result"]["tools"]}
    assert tools["get_meeting_transcript"]["annotations"]["readOnlyHint"] is True
    assert tools["create_note"]["annotations"]["readOnlyHint"] is False


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #

ID_TOOLS = [
    ("get_thread", lambda w: {"thread_id": w["atlas"]}),
    ("get_thread_timeline", lambda w: {"thread_id": w["atlas"]}),
    ("get_meeting", lambda w: {"meeting_id": w["standup"]}),
    ("get_meeting_transcript", lambda w: {"meeting_id": w["standup"]}),
    ("get_meeting_summary", lambda w: {"meeting_id": w["standup"]}),
    ("list_meetings", lambda w: {"thread_id": w["atlas"]}),
    ("list_action_items", lambda w: {"meeting_id": w["standup"]}),
    ("list_notes", lambda w: {"thread_id": w["atlas"]}),
    ("get_note", lambda w: {"note_id": w["note"]}),
    ("list_emails", lambda w: {"thread_id": w["atlas"]}),
    ("get_email", lambda w: {"email_id": w["email_a"]}),
    ("list_calendar_events", lambda w: {"thread_id": w["atlas"]}),
    ("search", lambda w: {"query": "rollback", "thread_id": w["atlas"]}),
]


@pytest.mark.parametrize("name,args", ID_TOOLS, ids=[n for n, _ in ID_TOOLS])
def test_someone_elses_ids_are_not_found(client, other_user_client, world, name, args):
    bob = make_token(other_user_client)
    text = call_error(client, bob, name, **args(world))
    assert "[not_found]" in text


@pytest.mark.parametrize(
    "name,args",
    [
        ("create_note", lambda w: {"thread_id": w["atlas"], "body": "hi"}),
        ("append_to_note", lambda w: {"note_id": w["note"], "body": "hi"}),
        ("set_action_item_status", lambda w: {"item_id": w["items"][0], "status": "done"}),
    ],
)
def test_write_tools_respect_ownership(client, other_user_client, world, name, args):
    bob = make_token(other_user_client, scope="read_write")
    assert "[not_found]" in call_error(client, bob, name, **args(world))


def test_lists_and_search_only_show_your_own(client, other_user_client, admin_client, world):
    for as_client in (other_user_client, admin_client):
        token = make_token(as_client)
        assert call(client, token, "list_threads")["threads"] == []
        assert call(client, token, "list_meetings")["meetings"] == []
        assert call(client, token, "list_notes")["notes"] == []
        assert call(client, token, "list_action_items", status="all")["action_items"] == []
        assert call(client, token, "search", query="rollback")["hits"] == []


# --------------------------------------------------------------------------- #
# Threads and meetings
# --------------------------------------------------------------------------- #


def test_list_threads_and_groups(client, user_client, alice_token, world):
    out = call(client, alice_token, "list_threads")
    assert out["total"] == 2 and "server_time" in out
    assert {t["title"] for t in out["threads"]} == {"Atlas Migration", "Hiring"}
    assert call(client, alice_token, "list_threads", query="oracle")["threads"][0]["title"] == "Atlas Migration"

    resp = user_client.post("/api/thread-groups", json={"name": "Infra"})
    assert resp.status_code == 201, resp.text
    group = resp.json()
    resp = user_client.put(f"/api/threads/{world['atlas']}/group", json={"group_id": group["id"]})
    assert resp.status_code == 200, resp.text
    by_name = call(client, alice_token, "list_threads", group="infra")["threads"]
    assert [t["title"] for t in by_name] == ["Atlas Migration"]
    assert by_name[0]["group_name"] == "Infra"
    assert [t["title"] for t in call(client, alice_token, "list_threads", group="none")["threads"]] == ["Hiring"]
    groups = call(client, alice_token, "list_groups")
    assert groups["groups"] == [{"id": group["id"], "name": "Infra", "thread_count": 1}]
    assert groups["ungrouped_thread_count"] == 1
    assert "[not_found]" in call_error(client, alice_token, "list_threads", group="Nope")


def test_get_thread_and_timeline(client, alice_token, world):
    thread = call(client, alice_token, "get_thread", thread_id=world["atlas"])
    assert [m["title"] for m in thread["meetings"]] == ["Weekly standup", "Kickoff"]
    assert thread["meetings"][0]["summary_tldr"].startswith("Rehearsal")
    assert thread["note_count"] == 1 and thread["email_count"] == 2

    timeline = call(client, alice_token, "get_thread_timeline", thread_id=world["atlas"])
    kinds = {i["kind"] for i in timeline["items"]}
    assert kinds == {"meeting", "event", "email_chain", "note"}
    chain = next(i for i in timeline["items"] if i["kind"] == "email_chain")
    assert chain["message_count"] == 2
    assert set(chain["email_ids"]) == {world["email_a"], world["email_b"]}


def test_list_meetings_by_date_and_query(client, alice_token, world):
    everything = call(client, alice_token, "list_meetings")
    assert [m["title"] for m in everything["meetings"]] == ["Candidate debrief", "Weekly standup", "Kickoff"]
    last_week = call(client, alice_token, "list_meetings", since=ago(7)[:10])
    assert [m["title"] for m in last_week["meetings"]] == ["Candidate debrief", "Weekly standup"]
    older = call(client, alice_token, "list_meetings", until=ago(30)[:10])
    assert [m["title"] for m in older["meetings"]] == ["Kickoff"]
    # The thread's title counts: "the Atlas meeting" finds meetings filed under Atlas.
    atlas = call(client, alice_token, "list_meetings", query="atlas", since=ago(7)[:10])
    assert [m["title"] for m in atlas["meetings"]] == ["Weekly standup"]
    assert atlas["meetings"][0]["thread_title"] == "Atlas Migration"
    assert atlas["meetings"][0]["has_transcript"] is True
    assert "[validation_error]" in call_error(client, alice_token, "list_meetings", since="last week")


def test_get_meeting(client, alice_token, world):
    meeting = call(client, alice_token, "get_meeting", meeting_id=world["standup"])
    assert meeting["thread_title"] == "Atlas Migration"
    assert {s["name"] for s in meeting["speakers"]} == {"Priya", "SPEAKER_01"}
    assert meeting["summary"]["version"] == 1
    assert meeting["attached"]["notes"] == 1


# --------------------------------------------------------------------------- #
# Transcripts and summaries
# --------------------------------------------------------------------------- #


def test_transcript_applies_names_without_touching_raw_json(client, alice_token, world):
    with get_conn() as conn:
        before = conn.execute(
            "SELECT raw_json FROM diarizations WHERE meeting_id = ?", (world["standup"],)
        ).fetchone()[0]
    out = call(client, alice_token, "get_meeting_transcript", meeting_id=world["standup"], format="text")
    assert out["available"] is True
    assert "Priya: Morning. Cutover rehearsal went fine." in out["text"]
    assert "SPEAKER_00" not in out["text"]
    assert out["next_offset"] is None
    with get_conn() as conn:
        after = conn.execute(
            "SELECT raw_json FROM diarizations WHERE meeting_id = ?", (world["standup"],)
        ).fetchone()[0]
    assert after == before


def test_transcript_paging_window_and_speaker(client, alice_token, world):
    full = call(client, alice_token, "get_meeting_transcript", meeting_id=world["standup"], format="text")
    total = full["total_chars"]
    first = call(client, alice_token, "get_meeting_transcript", meeting_id=world["standup"],
                 format="text", max_chars=1000)
    assert first["text"] == full["text"][:1000]
    # Short fixture: one page holds it all. Page by offset explicitly instead.
    tail = call(client, alice_token, "get_meeting_transcript", meeting_id=world["standup"],
                format="text", offset=total - 10)
    assert tail["text"] == full["text"][-10:] and tail["next_offset"] is None

    window = call(client, alice_token, "get_meeting_transcript", meeting_id=world["standup"],
                  format="text", start_sec=10, end_sec=25)
    # Overlap, strictly: the segment ending at 10s and the one starting at 30s
    # only touch the window, so they are out.
    assert window["segment_count"] == 2
    assert "rollback window" in window["text"] and "freeze billing" in window["text"]
    assert "Morning" not in window["text"] and "finance" not in window["text"]
    jump = call(client, alice_token, "get_meeting_transcript", meeting_id=world["standup"],
                format="text", start_sec=10)
    assert jump["text"].splitlines()[0].endswith("The rollback window is two hours.")

    priya = call(client, alice_token, "get_meeting_transcript", meeting_id=world["standup"],
                 format="text", speaker="priya")
    assert priya["segment_count"] == 2 and priya["speakers"] == ["Priya"]

    md = call(client, alice_token, "get_meeting_transcript", meeting_id=world["standup"])
    assert md["format"] == "markdown" and "**Priya**" in md["text"]
    vtt = call(client, alice_token, "get_meeting_transcript", meeting_id=world["standup"], format="vtt")
    assert vtt["text"].startswith("WEBVTT")


def test_transcript_paging_with_next_offset(client, alice_token, world, isolated_settings):
    with get_conn() as conn:
        long_meeting = threads_svc.create_meeting(
            conn, thread_id=world["atlas"], owner_id=world["alice"], title="Long one"
        )["id"]
        add_transcript(conn, long_meeting, *[("SPEAKER_00", f"Line {i} " + "x" * 80) for i in range(40)])
    pages, offset = [], 0
    while offset is not None:
        page = call(client, alice_token, "get_meeting_transcript", meeting_id=long_meeting,
                    format="text", offset=offset, max_chars=1000)
        pages.append(page["text"])
        offset = page["next_offset"]
    full = call(client, alice_token, "get_meeting_transcript", meeting_id=long_meeting, format="text")
    assert len(pages) > 1 and "".join(pages) == full["text"]


def test_transcript_not_ready_yet(client, alice_token, world):
    out = call(client, alice_token, "get_meeting_transcript", meeting_id=world["kickoff"])
    assert out["available"] is False and "No transcript yet" in out["message"]


def test_summary_current_missing_versioned_and_failed(client, alice_token, world):
    current = call(client, alice_token, "get_meeting_summary", meeting_id=world["standup"])
    assert current["available"] is True
    assert current["tldr"].startswith("Rehearsal")
    assert current["key_decisions"] == [{"text": "Freeze billing Friday"}]
    assert [a["text"] for a in current["action_items"]] == [
        "Tell finance about the freeze", "Book the war room",
    ]
    assert "prompt_text" not in current

    missing = call(client, alice_token, "get_meeting_summary", meeting_id=world["kickoff"])
    assert missing["available"] is False and "no summary" in missing["message"]

    with get_conn() as conn:
        add_summary(conn, world["standup"], tldr="v2")
        conn.execute("UPDATE summaries SET status = 'error', error = 'LLM down' WHERE version = 2")
    v1 = call(client, alice_token, "get_meeting_summary", meeting_id=world["standup"], version=1)
    assert v1["tldr"].startswith("Rehearsal") and v1["is_current"] is False
    failed = call(client, alice_token, "get_meeting_summary", meeting_id=world["standup"])
    assert failed["available"] is False and "LLM down" in failed["message"]
    assert "[not_found]" in call_error(client, alice_token, "get_meeting_summary",
                                       meeting_id=world["standup"], version=9)


def test_action_items(client, alice_token, world):
    out = call(client, alice_token, "list_action_items")
    assert [a["text"] for a in out["action_items"]] == ["Tell finance about the freeze", "Book the war room"]
    assert out["action_items"][0]["thread_title"] == "Atlas Migration"
    assert call(client, alice_token, "list_action_items", status="done")["action_items"] == []
    assert call(client, alice_token, "list_action_items", until=ago(30)[:10])["action_items"] == []


# --------------------------------------------------------------------------- #
# Notes, emails, events
# --------------------------------------------------------------------------- #


def test_notes(client, alice_token, world):
    everywhere = call(client, alice_token, "list_notes")
    assert {n["title"] for n in everywhere["notes"]} == {"Rollback plan", "Panel"}
    on_meeting = call(client, alice_token, "list_notes", meeting_id=world["standup"])
    assert [n["title"] for n in on_meeting["notes"]] == ["Rollback plan"]
    assert call(client, alice_token, "list_notes", thread_id=world["hiring"])["notes"][0]["source"] == "ai_chat"
    note = call(client, alice_token, "get_note", note_id=world["note"])
    assert note["body"] == "Two hours, agreed with Priya." and note["truncated"] is False


def test_emails_grouped_and_flat(client, alice_token, world):
    grouped = call(client, alice_token, "list_emails", thread_id=world["atlas"])
    assert grouped["grouped"] is True and grouped["total"] == 2
    [conversation] = grouped["conversations"]
    assert conversation["message_count"] == 2
    directions = {m["subject"]: m["direction"] for m in conversation["messages"]}
    # NULL is unknown, never guessed as inbound.
    assert directions == {"Freeze dates": "unknown", "Re: Freeze dates": "outbound"}
    assert "body" not in conversation["messages"][0]

    flat = call(client, alice_token, "list_emails", thread_id=world["atlas"], group_by_conversation=False)
    assert flat["grouped"] is False and len(flat["emails"]) == 2


def test_get_email_body_states(client, alice_token, world):
    stored = call(client, alice_token, "get_email", email_id=world["email_b"])
    assert stored["body_status"] == "stored" and stored["body"].startswith("Yes, Friday")
    pending = call(client, alice_token, "get_email", email_id=world["email_a"])
    assert pending["body_status"] == "not_fetched" and pending["body"] is None
    with get_conn() as conn:
        conn.execute("UPDATE thread_emails SET body_fetched_at = ? WHERE id = ?", (utcnow(), world["email_a"]))
    assert call(client, alice_token, "get_email", email_id=world["email_a"])["body_status"] == "unavailable"


def test_calendar_events(client, alice_token, world):
    out = call(client, alice_token, "list_calendar_events", thread_id=world["atlas"])
    assert [e["summary"] for e in out["events"]] == ["Cutover night"]
    assert "[validation_error]" in call_error(client, alice_token, "list_calendar_events")


def test_upcoming_events_reads_connected_calendars(client, alice_token, monkeypatch):
    from app.services.providers.base import EventCandidate, IntegrationRef

    start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()

    class FakeCalendar:
        ref = IntegrationRef(id=1, provider="fake", account_label="work@x", calendar_enabled=True)

        async def search_events(self, **kwargs):
            return [EventCandidate(uid="fake:1:a", summary="Go/no-go", start=start,
                                   attendees=("Priya Raman",), calendar_name="Work")]

    monkeypatch.setattr(
        "app.services.upcoming.providers_svc.load_for_user",
        lambda conn, user_id, *, kind=None: [] if kind == "email" else [FakeCalendar()],
    )
    out = call(client, alice_token, "get_upcoming_events", days=3)
    assert out["connected_calendars"] == 1
    assert out["events"][0]["summary"] == "Go/no-go"
    assert out["events"][0]["attendees"] == ["Priya Raman"]


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


def test_search_finds_a_transcript_line_with_its_timestamp(client, alice_token, world):
    out = call(client, alice_token, "search", query="rollback window", kinds=["segment"])
    [hit] = out["hits"]
    assert hit["kind"] == "segment"
    assert hit["meeting_id"] == world["standup"] and hit["start_sec"] == 10.0
    assert "**rollback**" in hit["snippet"].lower()
    assert "\x02" not in hit["snippet"]
    assert out["mode_used"] == "keyword"  # embeddings are off in the suite

    everything = call(client, alice_token, "search", query="freeze")
    assert {"summary", "action_item", "email"} <= {h["kind"] for h in everything["hits"]}
    assert "[validation_error]" in call_error(client, alice_token, "search", query="x", kinds=["bogus"])


# --------------------------------------------------------------------------- #
# No side effects from reading
# --------------------------------------------------------------------------- #


def _snapshot():
    with get_conn() as conn:
        return (
            [tuple(r) for r in conn.execute("SELECT id, updated_at FROM threads ORDER BY id")],
            [tuple(r) for r in conn.execute("SELECT id, seen_at, body_fetched_at, body FROM thread_emails ORDER BY id")],
            [tuple(r) for r in conn.execute("SELECT id, seen_at FROM thread_calendar_events ORDER BY id")],
            conn.execute("SELECT COUNT(*) FROM thread_notes").fetchone()[0],
            [tuple(r) for r in conn.execute("SELECT id, status FROM action_items ORDER BY id")],
        )


def test_reading_everything_changes_nothing_and_calls_no_llm(client, alice_token, world, llm_env):
    before = _snapshot()
    with respx.mock(assert_all_called=False) as router:
        llm = router.post(LLM_URL).mock(return_value=httpx.Response(500))
        for name, args in ID_TOOLS:
            call(client, alice_token, name, **args(world))
        call(client, alice_token, "list_threads")
        call(client, alice_token, "list_meetings")
        call(client, alice_token, "list_action_items", status="all")
        call(client, alice_token, "list_notes")
        call(client, alice_token, "get_email", email_id=world["email_b"])
    assert llm.call_count == 0
    assert _snapshot() == before


# --------------------------------------------------------------------------- #
# Write tools
# --------------------------------------------------------------------------- #


@pytest.fixture
def rw_token(user_client):
    return make_token(user_client, scope="read_write")


def test_create_note_on_a_meeting(client, rw_token, world):
    note = call(client, rw_token, "create_note", meeting_id=world["standup"],
                body="Follow up with finance.", title="Finance follow-up")
    assert note["source"] == "mcp"
    assert note["thread_id"] == world["atlas"] and note["meeting_id"] == world["standup"]
    assert note["title"] == "Finance follow-up"
    listed = call(client, rw_token, "list_notes", meeting_id=world["standup"])
    assert "Finance follow-up" in {n["title"] for n in listed["notes"]}


def test_create_note_title_falls_back_when_the_llm_fails(client, rw_token, world, llm_env):
    with respx.mock(assert_all_called=False) as router:
        router.post(LLM_URL).mock(return_value=httpx.Response(500))
        note = call(client, rw_token, "create_note", thread_id=world["hiring"],
                    body="# Ask about replication\nAnd failover.")
    assert note["title"] == "Ask about replication"


def test_create_note_generates_a_title(client, rw_token, world, llm_env):
    reply = {"choices": [{"message": {"content": json.dumps({"title": "Replication questions"})}}]}
    with respx.mock(assert_all_called=False) as router:
        router.post(LLM_URL).mock(return_value=httpx.Response(200, json=reply))
        note = call(client, rw_token, "create_note", thread_id=world["hiring"], body="Ask about it.")
    assert note["title"] == "Replication questions"


def test_create_note_validation(client, rw_token, world):
    assert "[validation_error]" in call_error(client, rw_token, "create_note", body="x")
    assert "[validation_error]" in call_error(client, rw_token, "create_note", thread_id=world["atlas"], body="  ")
    text = call_error(client, rw_token, "create_note", thread_id=world["hiring"],
                      meeting_id=world["standup"], body="x")
    assert "[not_found]" in text


def test_append_to_note(client, rw_token, world):
    out = call(client, rw_token, "append_to_note", note_id=world["note"], body="Finance confirmed.")
    assert out["body"] == "Two hours, agreed with Priya." + notes_svc.APPEND_SEPARATOR + "Finance confirmed."
    assert out["title"] == "Rollback plan"


def test_set_action_item_status(client, rw_token, world):
    done = call(client, rw_token, "set_action_item_status", item_id=world["items"][0], status="done")
    assert done["status"] == "done" and done["done_at"]
    assert [a["text"] for a in call(client, rw_token, "list_action_items")["action_items"]] == ["Book the war room"]
    reopened = call(client, rw_token, "set_action_item_status", item_id=world["items"][0], status="open")
    assert reopened["done_at"] is None


def test_mcp_notes_are_labelled_as_ai_written_for_the_model():
    assert notes_svc.source_origin("mcp") == "added by an AI assistant via MCP"
    assert notes_svc.source_origin("ai_chat") == "saved from an AI answer"
    assert notes_svc.source_origin("manual") == "written by the user"
