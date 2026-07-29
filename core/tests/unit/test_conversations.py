"""Conversation, turn and message persistence."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mitta.conversations.models import (
    ConversationDraft,
    ConversationStatus,
    InputKind,
    MessageDraft,
    MessageRole,
    Register,
    TurnStatus,
)
from mitta.conversations.repository import ConversationRepository
from mitta.errors import NotFoundError

NOW = 1_800_000_000


def user(content: str, **kw: object) -> MessageDraft:
    return MessageDraft(role=MessageRole.USER, content=content, **kw)  # type: ignore[arg-type]


class TestConversations:
    def test_create_and_read_back(self, conversations: ConversationRepository) -> None:
        conversation = conversations.create(ConversationDraft(title="Deploy debugging"))

        assert conversation.id.startswith("cnv_")
        assert conversation.status is ConversationStatus.ACTIVE
        assert conversation.message_count == 0

    def test_untitled_conversations_are_allowed(
        self, conversations: ConversationRepository
    ) -> None:
        # A title is assigned after the first exchange, so it cannot be required
        # at creation without inventing a placeholder the user then has to fix.
        assert conversations.create(ConversationDraft()).title is None

    def test_listing_puts_pinned_first_then_most_recently_touched(
        self, conversations: ConversationRepository
    ) -> None:
        conversations.create(ConversationDraft(title="old"), now=NOW - 1000)
        conversations.create(ConversationDraft(title="recent"), now=NOW)
        pinned = conversations.create(ConversationDraft(title="pinned"), now=NOW - 5000)
        conversations.set_pinned(pinned.id, True, now=NOW - 5000)

        titles = [c.title for c in conversations.list_conversations()]

        assert titles[0] == "pinned"
        assert titles.index("recent") < titles.index("old")

    def test_archiving_hides_without_destroying(
        self, conversations: ConversationRepository
    ) -> None:
        conversation = conversations.create(ConversationDraft(title="done with this"))

        conversations.archive(conversation.id)

        assert conversations.list_conversations() == []
        archived = conversations.list_conversations(status=ConversationStatus.ARCHIVED)
        assert [c.title for c in archived] == ["done with this"]

    def test_unarchive_restores(self, conversations: ConversationRepository) -> None:
        conversation = conversations.create(ConversationDraft())
        conversations.archive(conversation.id)

        assert conversations.unarchive(conversation.id).status is ConversationStatus.ACTIVE

    def test_delete_cascades_to_messages(self, conversations: ConversationRepository) -> None:
        conversation = conversations.create(ConversationDraft())
        message = conversations.add_message(conversation.id, user("hello"))

        conversations.delete(conversation.id)

        with pytest.raises(NotFoundError):
            conversations.get_message(message.id)

    def test_deleting_a_missing_conversation_raises(
        self, conversations: ConversationRepository
    ) -> None:
        with pytest.raises(NotFoundError):
            conversations.delete("cnv_nope")


class TestMessages:
    def test_adding_a_message_bumps_the_counter_atomically(
        self, conversations: ConversationRepository
    ) -> None:
        # The sidebar reads message_count. If it could drift from the transcript
        # nothing would ever recompute it, so the disagreement would be permanent.
        conversation = conversations.create(ConversationDraft(), now=NOW)

        conversations.add_message(conversation.id, user("first"), now=NOW + 1)
        conversations.add_message(conversation.id, user("second"), now=NOW + 2)

        reloaded = conversations.get(conversation.id)
        assert reloaded.message_count == 2
        assert reloaded.updated_at == NOW + 2

    def test_transcript_is_oldest_first(self, conversations: ConversationRepository) -> None:
        conversation = conversations.create(ConversationDraft())
        for text in ("one", "two", "three"):
            conversations.add_message(conversation.id, user(text))

        assert [m.content for m in conversations.messages(conversation.id)] == [
            "one",
            "two",
            "three",
        ]

    def test_pagination_returns_the_tail_first(
        self, conversations: ConversationRepository
    ) -> None:
        # A long thread should load what the user is looking at, then earlier
        # pages as they scroll up.
        conversation = conversations.create(ConversationDraft())
        for index in range(10):
            conversations.add_message(conversation.id, user(f"message {index}"))

        tail = conversations.messages(conversation.id, limit=3)
        assert [m.content for m in tail] == ["message 7", "message 8", "message 9"]

        earlier = conversations.messages(conversation.id, limit=3, before_seq=tail[0].seq)
        assert [m.content for m in earlier] == ["message 4", "message 5", "message 6"]

    def test_the_pre_personality_text_is_preserved(
        self, conversations: ConversationRepository
    ) -> None:
        # DEC-008 claims the style pass changes expression and never meaning.
        # Keeping both makes that auditable rather than merely asserted.
        conversation = conversations.create(ConversationDraft())

        message = conversations.add_message(
            conversation.id,
            MessageDraft(
                role=MessageRole.ASSISTANT,
                content="done ra",
                content_raw="I have cleaned your Downloads folder.",
                styled=True,
                register=Register.PLAYFUL,
            ),
        )

        assert message.content_raw == "I have cleaned your Downloads folder."
        assert message.was_rewritten is True

    def test_a_no_op_rewrite_is_not_reported_as_a_rewrite(
        self, conversations: ConversationRepository
    ) -> None:
        # `styled` records the pass ran; `was_rewritten` records it did
        # something. Without the distinction the UI swaps text it already shows.
        conversation = conversations.create(ConversationDraft())

        message = conversations.add_message(
            conversation.id,
            MessageDraft(
                role=MessageRole.ASSISTANT,
                content="Schema version 1.",
                content_raw="Schema version 1.",
                styled=True,
            ),
        )

        assert message.styled is True
        assert message.was_rewritten is False

    def test_tool_calls_round_trip(self, conversations: ConversationRepository) -> None:
        conversation = conversations.create(ConversationDraft())
        calls = [{"id": "call_1", "function": {"name": "read_file", "arguments": "{}"}}]

        message = conversations.add_message(
            conversation.id,
            MessageDraft(role=MessageRole.ASSISTANT, content="", tool_calls=calls),
        )

        assert message.tool_calls == calls

    def test_recent_context_is_smaller_than_a_ui_page(
        self, conversations: ConversationRepository
    ) -> None:
        # A UI page size doubling as a prompt size is how a context window
        # silently overflows.
        conversation = conversations.create(ConversationDraft())
        for index in range(50):
            conversations.add_message(conversation.id, user(f"m{index}"))

        assert len(conversations.recent_context(conversation.id)) == 20


class TestTurns:
    def test_a_turn_records_its_accounting(
        self, conversations: ConversationRepository
    ) -> None:
        conversation = conversations.create(ConversationDraft())
        turn = conversations.begin_turn(conversation.id, input_kind=InputKind.PALETTE, now=NOW)

        assert turn.status is TurnStatus.RUNNING
        assert turn.input_kind is InputKind.PALETTE

        finished = conversations.end_turn(
            turn.id,
            status=TurnStatus.COMPLETED,
            register=Register.SERIOUS,
            tokens_in=120,
            tokens_out=340,
            tool_call_count=2,
            now=NOW + 5,
        )

        assert finished.status is TurnStatus.COMPLETED
        assert (finished.tokens_in, finished.tokens_out) == (120, 340)
        assert finished.register is Register.SERIOUS
        assert finished.duration_ms == 5000

    def test_a_failed_turn_keeps_its_error(
        self, conversations: ConversationRepository
    ) -> None:
        conversation = conversations.create(ConversationDraft())
        turn = conversations.begin_turn(conversation.id)

        failed = conversations.end_turn(
            turn.id,
            status=TurnStatus.FAILED,
            error={"code": "provider.rate_limited", "message": "Groq is rate-limiting"},
        )

        assert failed.error is not None
        assert failed.error["code"] == "provider.rate_limited"

    def test_turns_orphaned_by_a_crash_are_reconciled(
        self, conversations: ConversationRepository
    ) -> None:
        # Without this a crash mid-turn leaves a row saying `running` forever,
        # and the UI shows a thinking indicator for work nothing is doing.
        conversation = conversations.create(ConversationDraft())
        turn = conversations.begin_turn(conversation.id)

        assert conversations.reconcile_orphaned_turns(now=NOW) == 1

        reconciled = conversations.get_turn(turn.id)
        assert reconciled.status is TurnStatus.FAILED
        assert reconciled.error is not None
        assert reconciled.error["code"] == "turn.interrupted"

    def test_reconciliation_leaves_finished_turns_alone(
        self, conversations: ConversationRepository
    ) -> None:
        conversation = conversations.create(ConversationDraft())
        turn = conversations.begin_turn(conversation.id)
        conversations.end_turn(turn.id, status=TurnStatus.COMPLETED)

        assert conversations.reconcile_orphaned_turns() == 0
        assert conversations.get_turn(turn.id).status is TurnStatus.COMPLETED

    def test_messages_can_be_grouped_by_turn(
        self, conversations: ConversationRepository
    ) -> None:
        conversation = conversations.create(ConversationDraft())
        turn = conversations.begin_turn(conversation.id)
        conversations.add_message(conversation.id, user("in the turn", turn_id=turn.id))
        conversations.add_message(conversation.id, user("outside it"))

        assert [m.content for m in conversations.turn_messages(turn.id)] == ["in the turn"]


class TestApi:
    def test_crud_over_http(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        created = client.post(
            "/v1/conversations", json={"title": "First thread"}, headers=auth_headers
        )
        assert created.status_code == 201
        conversation_id = created.json()["id"]

        listed = client.get("/v1/conversations", headers=auth_headers).json()
        assert listed["total"] == 1

        renamed = client.patch(
            f"/v1/conversations/{conversation_id}",
            json={"title": "Renamed", "pinned": True},
            headers=auth_headers,
        ).json()
        assert renamed["title"] == "Renamed"
        assert renamed["pinned"] is True

    def test_transcript_endpoint(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
        conversations: ConversationRepository,
    ) -> None:
        conversation = conversations.create(ConversationDraft())
        conversations.add_message(conversation.id, user("hello"))

        body = client.get(
            f"/v1/conversations/{conversation.id}/messages", headers=auth_headers
        ).json()

        assert [m["content"] for m in body["messages"]] == ["hello"]

    def test_a_bad_id_is_a_404_not_an_empty_transcript(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        response = client.get("/v1/conversations/cnv_nope/messages", headers=auth_headers)

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found.conversation"

    def test_archive_is_reversible_and_delete_is_not(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        conversation_id = client.post(
            "/v1/conversations", json={}, headers=auth_headers
        ).json()["id"]

        archived = client.post(
            f"/v1/conversations/{conversation_id}/archive", headers=auth_headers
        ).json()
        assert archived["status"] == "archived"

        restored = client.post(
            f"/v1/conversations/{conversation_id}/unarchive", headers=auth_headers
        ).json()
        assert restored["status"] == "active"

        assert (
            client.delete(f"/v1/conversations/{conversation_id}", headers=auth_headers).status_code
            == 204
        )
        assert (
            client.get(f"/v1/conversations/{conversation_id}", headers=auth_headers).status_code
            == 404
        )

    def test_every_route_requires_a_token(self, client: TestClient) -> None:
        for method, path in (
            ("GET", "/v1/conversations"),
            ("POST", "/v1/conversations"),
            ("GET", "/v1/conversations/cnv_1"),
            ("PATCH", "/v1/conversations/cnv_1"),
            ("DELETE", "/v1/conversations/cnv_1"),
            ("GET", "/v1/conversations/cnv_1/messages"),
        ):
            response = client.request(method, path, json={})
            assert response.status_code in (401, 403), f"{method} {path} was reachable"

    def test_there_is_no_send_message_endpoint(self, client: TestClient) -> None:
        """Sending is a turn, and a turn needs the agent.

        An endpoint that persists a user message with nothing to answer it would
        be worse than none — it would look like chat and never reply.
        """
        paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
        assert "/v1/conversations/{conversation_id}/messages" in paths
        assert "post" not in paths["/v1/conversations/{conversation_id}/messages"]
