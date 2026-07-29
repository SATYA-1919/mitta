"""Memory HTTP surface."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from mitta.memory.indexer import Indexer


def create(client: TestClient, headers: dict[str, str], content: str, **kw: object) -> str:
    body: dict[str, object] = {"content": content}
    body.update(kw)
    response = client.post("/v1/memory", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


class TestAuth:
    def test_every_memory_route_requires_a_token(self, client: TestClient) -> None:
        for method, path in (
            ("GET", "/v1/memory"),
            ("POST", "/v1/memory"),
            ("POST", "/v1/memory/search"),
            ("GET", "/v1/memory/stats"),
            ("GET", "/v1/memory/mem_1"),
            ("PATCH", "/v1/memory/mem_1"),
            ("POST", "/v1/memory/mem_1/forget"),
            ("DELETE", "/v1/memory/mem_1"),
            ("POST", "/v1/memory/maintenance/sweep"),
            ("POST", "/v1/memory/maintenance/reindex"),
        ):
            response = client.request(method, path, json={})
            assert response.status_code in (401, 403), f"{method} {path} was reachable"

    def test_routes_are_absent_without_an_engine(
        self, bare_client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        assert bare_client.get("/v1/memory", headers=auth_headers).status_code == 404


class TestCreate:
    def test_stores_and_returns_the_memory(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        response = client.post(
            "/v1/memory",
            json={"content": "Satya prefers dark mode", "importance": 0.8},
            headers=auth_headers,
        )

        assert response.status_code == 201
        body = response.json()
        assert body["id"].startswith("mem_")
        assert body["importance"] == 0.8
        assert body["source_kind"] == "user"
        assert "seq" not in body  # the FAISS key never crosses the boundary

    def test_a_client_cannot_claim_a_source_kind(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        # Otherwise a caller could launder a fabricated memory as machine-derived.
        response = client.post(
            "/v1/memory",
            json={"content": "x", "source_kind": "consolidation"},
            headers=auth_headers,
        )
        assert response.status_code == 422

    def test_duplicate_content_returns_the_original(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        first = create(client, auth_headers, "the same fact")
        second = create(client, auth_headers, "the same fact\n")
        assert first == second

    def test_bad_attributes_are_rejected_with_a_typed_error(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        response = client.post(
            "/v1/memory",
            json={"content": "x", "kind": "long_term", "attributes": {"catgory": "typo"}},
            headers=auth_headers,
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"].startswith("validation.")

    def test_empty_content_is_rejected(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        response = client.post("/v1/memory", json={"content": ""}, headers=auth_headers)
        assert response.status_code == 422


class TestReadAndBrowse:
    def test_get_returns_one_memory(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        memory_id = create(client, auth_headers, "a stored fact")
        response = client.get(f"/v1/memory/{memory_id}", headers=auth_headers)

        assert response.status_code == 200
        assert response.json()["content"] == "a stored fact"

    def test_missing_memory_is_a_typed_404(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        response = client.get("/v1/memory/mem_nope", headers=auth_headers)

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found.memory"

    def test_list_paginates_and_reports_the_total(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        for i in range(5):
            create(client, auth_headers, f"fact {i}")

        body = client.get("/v1/memory?limit=2", headers=auth_headers).json()

        assert len(body["memories"]) == 2
        assert body["total"] == 5
        assert body["limit"] == 2

    def test_list_filters_by_kind(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        create(client, auth_headers, "a fact")
        create(client, auth_headers, "prefers tabs", kind="preference")

        body = client.get("/v1/memory?kind=preference", headers=auth_headers).json()

        assert len(body["memories"]) == 1


class TestSearch:
    def test_finds_a_memory(
        self, client: TestClient, auth_headers: dict[str, str], indexer: Indexer
    ) -> None:
        create(client, auth_headers, "the auth flow uses PKCE")
        indexer.drain()

        body = client.post(
            "/v1/memory/search", json={"query": "auth PKCE"}, headers=auth_headers
        ).json()

        assert len(body["hits"]) == 1
        assert body["semantic_available"] is True

    def test_reports_which_index_matched(
        self, client: TestClient, auth_headers: dict[str, str], indexer: Indexer
    ) -> None:
        # Retrieval stays inspectable rather than being a black box the user has
        # to take on faith.
        create(client, auth_headers, "ticket MITTA-1481 covers auth")
        indexer.drain()

        hit = client.post(
            "/v1/memory/search", json={"query": "MITTA-1481"}, headers=auth_headers
        ).json()["hits"][0]

        assert hit["keyword_rank"] is not None
        assert "matched_both" in hit

    def test_admits_when_semantic_search_is_unavailable(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        # Nothing indexed yet: the UI must not present keyword-only results as
        # though the full search ran.
        create(client, auth_headers, "not yet indexed")

        body = client.post(
            "/v1/memory/search", json={"query": "indexed"}, headers=auth_headers
        ).json()

        assert body["semantic_available"] is False

    def test_preview_search_does_not_record_access(
        self, client: TestClient, auth_headers: dict[str, str], indexer: Indexer
    ) -> None:
        memory_id = create(client, auth_headers, "browsed but not used")
        indexer.drain()

        client.post(
            "/v1/memory/search",
            json={"query": "browsed", "record_access": False},
            headers=auth_headers,
        )

        body = client.get(f"/v1/memory/{memory_id}", headers=auth_headers).json()
        assert body["access_count"] == 0

    def test_an_empty_query_is_rejected(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        response = client.post("/v1/memory/search", json={"query": ""}, headers=auth_headers)
        assert response.status_code == 422


class TestMutation:
    def test_patch_updates_only_what_was_sent(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        memory_id = create(client, auth_headers, "original", summary="keep me")

        body = client.patch(
            f"/v1/memory/{memory_id}", json={"importance": 0.95}, headers=auth_headers
        ).json()

        assert body["importance"] == 0.95
        assert body["summary"] == "keep me"

    def test_correct_supersedes_and_preserves_history(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        original = create(client, auth_headers, "lives in Hyderabad")

        replacement = client.post(
            f"/v1/memory/{original}/correct",
            json={"content": "lives in Bangalore"},
            headers=auth_headers,
        ).json()

        old = client.get(f"/v1/memory/{original}", headers=auth_headers).json()
        assert old["status"] == "superseded"
        assert old["superseded_by"] == replacement["id"]
        assert old["content"] == "lives in Hyderabad"

    def test_forget_is_reversible(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        memory_id = create(client, auth_headers, "temporarily hidden")

        forgotten = client.post(f"/v1/memory/{memory_id}/forget", headers=auth_headers).json()
        assert forgotten["status"] == "forgotten"

        restored = client.post(f"/v1/memory/{memory_id}/restore", headers=auth_headers).json()
        assert restored["status"] == "active"

    def test_delete_is_permanent_and_returns_204(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        memory_id = create(client, auth_headers, "gone for good")

        assert client.delete(f"/v1/memory/{memory_id}", headers=auth_headers).status_code == 204
        assert client.get(f"/v1/memory/{memory_id}", headers=auth_headers).status_code == 404


class TestMaintenance:
    def test_stats_report_the_engine_honestly(
        self, client: TestClient, auth_headers: dict[str, str], indexer: Indexer
    ) -> None:
        create(client, auth_headers, "one")
        create(client, auth_headers, "two")

        before = client.get("/v1/memory/stats", headers=auth_headers).json()
        assert before["pending_embeddings"] == 2
        assert before["vectors_indexed"] == 0

        indexer.drain()

        after = client.get("/v1/memory/stats", headers=auth_headers).json()
        assert after["pending_embeddings"] == 0
        assert after["vectors_indexed"] == 2
        assert after["index_consistent"] is True

    def test_stats_admit_when_the_real_model_is_absent(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        # The suite runs on the stand-in provider. Reporting "model available"
        # here would tell the user semantic search works when it does not.
        body = client.get("/v1/memory/stats", headers=auth_headers).json()

        assert body["embedding_degraded"] is True
        assert body["embedding_model_downloaded"] is False
        assert body["embedding_model_id"].startswith("deterministic-hash")

    def test_sweep_reports_what_it_did(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        response = client.post("/v1/memory/maintenance/sweep", headers=auth_headers)

        assert response.status_code == 200
        assert response.json()["expired"] == 0

    def test_reindex_rebuilds_and_returns_fresh_stats(
        self, client: TestClient, auth_headers: dict[str, str], indexer: Indexer
    ) -> None:
        create(client, auth_headers, "rebuild me")
        indexer.drain()

        body = client.post("/v1/memory/maintenance/reindex", headers=auth_headers).json()

        assert body["vectors_indexed"] == 1
        assert body["index_consistent"] is True


class TestLifespan:
    """The `client` fixture disables the background thread for determinism, so
    the wiring that starts it needs covering on its own."""

    def test_the_app_runs_the_indexer_for_its_lifetime(
        self,
        settings,  # type: ignore[no-untyped-def]
        paths,  # type: ignore[no-untyped-def]
        migrated,  # type: ignore[no-untyped-def]
        memory_service,  # type: ignore[no-untyped-def]
        indexer: Indexer,
        embedder,  # type: ignore[no-untyped-def]
        auth_headers: dict[str, str],
    ) -> None:
        from mitta.api.app import create_app
        from mitta.os_adapter.mac import MacAdapter

        app = create_app(
            settings=settings,
            paths=paths,
            database=migrated,
            os_adapter=MacAdapter(),
            memory=memory_service,
            indexer=indexer,
            embedder=embedder,
        )

        with TestClient(app) as live:
            assert indexer.is_running is True
            create(live, auth_headers, "indexed by the worker")

            # Poll rather than sleep a fixed interval: the worker is a real
            # thread, and a fixed wait is either flaky or needlessly slow.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if live.get("/v1/memory/stats", headers=auth_headers).json()["vectors_indexed"]:
                    break
                time.sleep(0.02)

        assert indexer.is_running is False  # stopped on shutdown

    def test_shutdown_stops_the_worker_even_if_it_never_ran_work(
        self,
        settings,  # type: ignore[no-untyped-def]
        paths,  # type: ignore[no-untyped-def]
        migrated,  # type: ignore[no-untyped-def]
        memory_service,  # type: ignore[no-untyped-def]
        indexer: Indexer,
        embedder,  # type: ignore[no-untyped-def]
    ) -> None:
        # A thread still writing to a database the process is closing produces
        # confusing "not connected" errors on the way down.
        from mitta.api.app import create_app
        from mitta.os_adapter.mac import MacAdapter

        app = create_app(
            settings=settings,
            paths=paths,
            database=migrated,
            os_adapter=MacAdapter(),
            memory=memory_service,
            indexer=indexer,
            embedder=embedder,
        )
        with TestClient(app):
            pass

        assert indexer.is_running is False
