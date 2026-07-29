#!/usr/bin/env python3
"""Seed MITTA's memory from Satya's personal context profile.

Each line becomes a **typed** memory rather than one long blob. That matters:
`kind` is what makes a preference retrievable as a preference and a workflow
retrievable as something to execute (DEC-023, DEC-041). A profile stored as a
single `long_term` paragraph would come back whole or not at all, and every
recall would spend context on football when the question was about Docker.

Importance is assigned by how stable the fact is, not by how interesting it
sounds. "Supports FC Barcelona" will still be true in five years; "currently
building MITTA" will not, and the retention curve should reflect that.

Idempotent: `remember` deduplicates on content hash, so re-running updates
nothing and creates nothing.

    make seed-profile                       # your real storage root
    python scripts/seed-profile.py --storage-root .dev/storage
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from mitta.config.paths import Paths, resolve_paths  # noqa: E402
from mitta.config.settings import DatabaseSettings, load_settings  # noqa: E402
from mitta.memory.embedding.deterministic import DeterministicEmbedder  # noqa: E402
from mitta.memory.embedding.local import LocalEmbedder  # noqa: E402
from mitta.memory.indexer import Indexer  # noqa: E402
from mitta.memory.models import MemoryDraft, MemoryKind, SourceKind  # noqa: E402
from mitta.memory.repository import MemoryRepository  # noqa: E402
from mitta.memory.service import MemoryService  # noqa: E402
from mitta.memory.vectors.store import VectorStore, build_index  # noqa: E402
from mitta.os_adapter.factory import create_os_adapter  # noqa: E402
from mitta.persistence.database import Database  # noqa: E402
from mitta.persistence.migrations import migrate  # noqa: E402

# (kind, content, importance, attributes)
Entry = tuple[MemoryKind, str, float, dict[str, object]]

PROFILE: list[Entry] = [
    # ── Identity ────────────────────────────────────────────────────────────
    # Pinned. These are the facts that must never decay: an assistant that
    # forgets who it is talking to has failed at the one thing it exists for.
    (MemoryKind.LONG_TERM, "Satya is a fourth-year BTech student in India.", 1.0,
     {"category": "identity", "entities": ["Satya", "India", "BTech"]}),
    (MemoryKind.LONG_TERM,
     "Satya is curious, practical and technology-focused, and likes trying new ideas.", 0.8,
     {"category": "identity", "entities": ["Satya"]}),

    # ── Interests ───────────────────────────────────────────────────────────
    (MemoryKind.LONG_TERM,
     "Satya is interested in AI agents, machine learning, practical AI applications, "
     "automation and intelligent software.", 0.9,
     {"category": "interest", "entities": ["AI", "machine learning", "AI agents"]}),
    (MemoryKind.LONG_TERM,
     "Satya enjoys building applications and understanding how frontend, backend, APIs, "
     "deployment, hosting and infrastructure connect together.", 0.9,
     {"category": "interest", "entities": ["software development", "APIs", "deployment"]}),
    (MemoryKind.LONG_TERM,
     "Satya enjoys hackathons and building under constraints with unfamiliar technology.", 0.7,
     {"category": "interest", "entities": ["hackathons"]}),
    (MemoryKind.LONG_TERM,
     "Satya is interested in screenwriting, filmmaking and storytelling.", 0.7,
     {"category": "interest", "entities": ["screenwriting", "filmmaking", "storytelling"]}),

    # ── Football ────────────────────────────────────────────────────────────
    (MemoryKind.LONG_TERM, "Satya plays football and follows the sport closely.", 0.9,
     {"category": "interest", "entities": ["football"]}),
    (MemoryKind.LONG_TERM,
     "Satya supports FC Barcelona at club level and Argentina internationally. "
     "Neymar is a player he follows with particular interest.", 0.9,
     {"category": "interest", "entities": ["FC Barcelona", "Argentina", "Neymar", "football"]}),
    (MemoryKind.LONG_TERM,
     "Satya enjoys talking about matches, players, transfers, new signings and football news.", 0.7,
     {"category": "interest", "entities": ["football"]}),

    # ── Communication and learning preferences ──────────────────────────────
    # `preference` rather than `long_term`: these should surface when deciding
    # *how* to answer, which is a different retrieval than *what* to answer.
    (MemoryKind.PREFERENCE,
     "Satya prefers to learn by understanding how things work rather than receiving "
     "finished answers.", 1.0,
     {"domain": "learning", "polarity": "likes", "derived_from": "profile"}),
    (MemoryKind.PREFERENCE,
     "For a new technical topic, start from fundamentals and do not assume prior knowledge.", 0.9,
     {"domain": "learning", "polarity": "likes", "derived_from": "profile"}),
    (MemoryKind.PREFERENCE,
     "Explain the problem before presenting the solution, and explain why an approach is "
     "used rather than only how to use it.", 0.9,
     {"domain": "learning", "polarity": "likes", "derived_from": "profile"}),
    (MemoryKind.PREFERENCE,
     "Use concrete examples instead of abstract definitions.", 0.8,
     {"domain": "learning", "polarity": "likes", "derived_from": "profile"}),
    (MemoryKind.PREFERENCE,
     "Keep simple questions short. Break genuinely difficult technical topics into clear steps.", 0.9,
     {"domain": "learning", "polarity": "likes", "derived_from": "profile"}),
    (MemoryKind.PREFERENCE,
     "Satya prefers natural, casual, direct communication over polished corporate wording.", 1.0,
     {"domain": "communication", "polarity": "likes", "derived_from": "profile"}),
    (MemoryKind.PREFERENCE,
     "For social messages Satya prefers short, confident, playful wording rather than long "
     "or dramatic ones. For professional messages, clean and professional but still natural.", 0.9,
     {"domain": "communication", "polarity": "likes", "derived_from": "profile"}),
    (MemoryKind.PREFERENCE,
     "Satya dislikes copy-paste solutions for coding problems; he wants the reasoning.", 0.9,
     {"domain": "learning", "polarity": "dislikes", "derived_from": "profile"}),
    (MemoryKind.PREFERENCE,
     "When rewriting Satya's messages, keep his own voice rather than making them formal.", 0.9,
     {"domain": "communication", "polarity": "likes", "derived_from": "profile"}),

    # ── How to work with him ────────────────────────────────────────────────
    # `procedural`: a trigger plus steps, which is exactly why this kind was
    # kept (DEC-041). Folded into `long_term` it would stop being retrievable
    # as something to *do*.
    (MemoryKind.PROCEDURAL,
     "When Satya asks about code: explain the logic first, then walk through the important "
     "lines and variables, then show the complete solution.", 1.0,
     {"trigger": "Satya asks a coding question",
      "steps": ["explain the logic", "walk through key lines and variables",
                "show the complete solution"]}),
    (MemoryKind.PROCEDURAL,
     "When Satya asks about a project: think end-to-end — architecture, implementation, "
     "testing, deployment, usability and realistic constraints.", 0.9,
     {"trigger": "Satya asks about a project",
      "steps": ["architecture", "implementation", "testing", "deployment", "usability",
                "constraints"]}),
    (MemoryKind.PROCEDURAL,
     "When recommending a tool or technology to Satya, explain the trade-offs and why it "
     "fits his specific project rather than just naming it.", 0.9,
     {"trigger": "recommending a tool or technology",
      "steps": ["state the trade-offs", "explain the fit for his project"]}),

    # ── Technical background ────────────────────────────────────────────────
    (MemoryKind.LONG_TERM,
     "Satya has worked with Python, FastAPI, frontend development, APIs, Docker, Tailscale, "
     "Judge0, AI models, agentic AI, cloud and deployment concepts, and VS Code.", 0.9,
     {"category": "skills",
      "entities": ["Python", "FastAPI", "Docker", "Tailscale", "Judge0", "VS Code"]}),
    (MemoryKind.LONG_TERM,
     "Satya's building style is practical: build it, run it locally, understand each "
     "component, test it, then work out how it becomes a real deployed product.", 1.0,
     {"category": "working_style", "entities": ["Satya"]}),
    (MemoryKind.LONG_TERM,
     "Satya wants to understand the complete path from code running in VS Code to an "
     "application actually usable on his Mac, the web, or another device.", 0.9,
     {"category": "working_style", "entities": ["VS Code", "Mac", "deployment"]}),

    # ── Projects ────────────────────────────────────────────────────────────
    # Lower importance than identity: projects finish, and the decay curve
    # should let them fade rather than crowd out stable facts forever.
    (MemoryKind.LONG_TERM,
     "Satya is building MITTA, a personal AI agent that understands its user rather than "
     "behaving like a generic chatbot.", 1.0,
     {"category": "project", "entities": ["MITTA", "AI agent"]}),
    (MemoryKind.LONG_TERM,
     "Satya reached the semi-finals of an all-India LAM Research Lab hackathon, and took "
     "part in a Silicon Labs ideathon.", 0.7,
     {"category": "achievement", "entities": ["LAM Research", "Silicon Labs", "hackathon"]}),
    (MemoryKind.LONG_TERM,
     "Satya worked on Travel Disruption Concierge, an AI travel assistant for cancelled "
     "flights, missed connections and rebooking.", 0.6,
     {"category": "project", "entities": ["Travel Disruption Concierge"]}),
    (MemoryKind.LONG_TERM,
     "Satya explored an online coding and execution setup using Docker, Judge0 and Tailscale.", 0.6,
     {"category": "project", "entities": ["Docker", "Judge0", "Tailscale"]}),
    (MemoryKind.LONG_TERM,
     "Satya completed an IoT and Robotics training programme through Technook with "
     "Cognizance '24 at IIT Roorkee.", 0.6,
     {"category": "education", "entities": ["Technook", "IIT Roorkee", "IoT", "Robotics"]}),

    # ── Career ──────────────────────────────────────────────────────────────
    (MemoryKind.LONG_TERM,
     "Satya is looking for internships and early-career roles in AI, machine learning, "
     "software engineering, AI engineering, data analysis and data research.", 0.9,
     {"category": "career", "entities": ["internship", "AI", "machine learning"]}),
    (MemoryKind.LONG_TERM,
     "Satya is interested in the intersection of technology and football, including "
     "technical or data roles at football clubs and sports organisations.", 0.8,
     {"category": "career", "entities": ["football", "sports analytics"]}),
]

# Facts that must never decay. Everything else is subject to the retention
# curve, which is the point of having one.
PINNED_CATEGORIES = {"identity", "working_style"}


def build_service(storage_root: Path | None) -> tuple[MemoryService, Indexer, Database]:
    if storage_root is not None:
        paths = Paths(
            storage_root=storage_root,
            runtime_dir=storage_root / "runtime",
            log_dir=storage_root / "logs",
        )
    else:
        paths = resolve_paths(load_settings(), create_os_adapter())
    paths.ensure()

    database = Database(paths.database, DatabaseSettings())
    database.connect()
    migrate(database)

    local = LocalEmbedder(paths.models)
    embedder = local if local.is_available() else DeterministicEmbedder()
    if not local.is_available():
        print("  note: embedding model not downloaded — indexing with the fallback provider.")
        print("        run 'make download-model', then 'make dev' will re-embed automatically.")

    repository = MemoryRepository(database)
    store = VectorStore(database, build_index(paths.vectors / "memories.faiss", embedder), embedder)
    store.open()
    indexer = Indexer(repository, store)
    return MemoryService(repository, store, indexer), indexer, database


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-root", type=Path, default=None)
    args = parser.parse_args()

    service, indexer, database = build_service(args.storage_root)
    try:
        created = 0
        for kind, content, importance, attributes in PROFILE:
            before = service.count(status=None)
            service.remember(
                MemoryDraft(
                    kind=kind,
                    content=content,
                    attributes=attributes,
                    importance=importance,
                    # `import`, not `user`: these came from a document, not from
                    # something Satya said in conversation. The distinction
                    # matters when he later asks where a memory came from.
                    source_kind=SourceKind.IMPORT,
                    pinned=attributes.get("category") in PINNED_CATEGORIES,
                )
            )
            if service.count(status=None) > before:
                created += 1

        indexed = indexer.drain()
        status = service.index_status()

        print(f"\n  {created} new memories ({len(PROFILE) - created} already present)")
        print(f"  {indexed} embedded · {status.vector_count} vectors · model {status.model_id}")

        by_kind: dict[str, int] = {}
        for kind, *_ in PROFILE:
            by_kind[kind.value] = by_kind.get(kind.value, 0) + 1
        for kind_name, count in sorted(by_kind.items()):
            print(f"    {kind_name:14} {count}")
        print("\n  Satya can correct or delete any of these from the Memory surface.")
        return 0
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
