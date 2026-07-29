# MITTA — Project Structure (Phase 2)

Canonical repository layout. Where this document and any other disagrees about
*where code lives*, this document wins.

Layout: **monorepo grouped by runtime boundary** (DEC-021).
Last updated: 2026-07-29

---

## 1. The organising principle

The repository is grouped by **process boundary**, not by feature and not by
technical layer. There are exactly three runtimes in the shipped product
(Rust shell, Python sidecar, plugin subprocesses), and each gets one root with
its own dependency manifest, its own build pipeline and its own test suite.

This matters because the process boundary is the only boundary the operating
system actually enforces. Making it also the folder boundary means a developer
can never accidentally import across it — the import simply doesn't resolve.
Every other boundary in this codebase is a convention that tooling has to
police; this one is free.

Within the Python root, code is grouped by **architectural layer** (the ten
layers from `ARCHITECTURE.md` §3), because that is the axis along which
dependency rules are declared and enforced.

---

## 2. Top level

```
mitta/
├── apps/
│   └── desktop/              Tauri shell — Rust core, Swift speech, React UI
├── core/                     Python sidecar — the agent runtime
├── plugins/                  First-party plugins (out-of-process, MCP-compatible)
├── config/                   Default configuration shipped with the app
├── scripts/                  Build, sign, notarise, release, dev orchestration
├── docs/                     Architecture, decisions, requirements, guides
├── tests/                    Cross-runtime integration + end-to-end tests
├── .github/workflows/        CI
├── .gitignore
├── Makefile                  Single entry point for every dev task
└── README.md
```

Note what is **not** here: no top-level `data/`. Runtime data never lives in the
repository — it lives under the configurable storage root
(`~/Library/Application Support/MITTA` by default, resolved by the OS Adapter).
A `data/` directory in the repo is an invitation to commit a user's memory
database, and this product's entire privacy claim depends on that not happening.
The `.gitignore` denies it explicitly.

Also not here: no top-level `assets/`. Assets belong to the surface that renders
them — icons and fonts under `apps/desktop`, prompt templates and style profiles
under `core/`. A shared assets bucket becomes a junk drawer that neither build
pipeline can prune.

---

## 3. `apps/desktop/` — the Tauri shell

```
apps/desktop/
├── package.json
├── tsconfig.json
├── vite.config.ts
├── tailwind.config.ts
├── index.html                Main window entry
├── palette.html              Command palette entry (separate Tauri window)
│
├── src/                      React + TypeScript frontend
│   ├── main.tsx              Main-window bootstrap
│   ├── palette.tsx           Palette bootstrap — MUST stay minimal (§3.2)
│   │
│   ├── app/
│   │   ├── MainWindow.tsx    Shell: sidebar + routed content + status bar
│   │   ├── Palette.tsx       Overlay shell
│   │   ├── routes.tsx
│   │   └── providers.tsx     Composition root for the frontend
│   │
│   ├── features/             One folder per product surface (R2)
│   │   ├── chat/             Message list, composer, streaming, thinking state
│   │   ├── voice/            Waveform, mic state, push-to-talk, hands-free
│   │   ├── memory/           Memory explorer — browse, search, edit, forget
│   │   ├── projects/         Project explorer, per-project context
│   │   ├── tasks/            Active tasks, plan DAG view, automations
│   │   ├── plugins/          Plugin manager — install, permissions, updates
│   │   ├── monitor/          CPU · RAM · GPU · battery
│   │   ├── history/          Conversation history
│   │   ├── notifications/    Notification centre
│   │   ├── models/           Model selector, provider health
│   │   ├── settings/         Settings, API-key entry (DEC-017)
│   │   └── confirm/          Destructive-action confirmation UI (DEC-010)
│   │
│   ├── components/
│   │   ├── ui/               Design-system primitives — Button, Dialog, Input…
│   │   └── layout/           Sidebar, StatusBar, Pane, Split
│   │
│   ├── state/                THE single state layer (DEC-018)
│   │   ├── store.ts          Root store
│   │   ├── slices/           conversation · tasks · memory · metrics · …
│   │   ├── sync.ts           Cross-window state replication
│   │   └── selectors/
│   │
│   ├── lib/
│   │   ├── transport/        WebSocket client, reconnect, envelope codec
│   │   ├── ipc/              Typed wrappers over Tauri `invoke`
│   │   ├── api/              Generated HTTP client (from the OpenAPI schema)
│   │   └── format/
│   │
│   ├── styles/
│   │   ├── globals.css
│   │   └── tokens.css        Design tokens — the only place colour is defined
│   │
│   └── types/
│       └── generated/        Types generated from Python schemas (§6)
│
└── src-tauri/                Rust core
    ├── Cargo.toml
    ├── tauri.conf.json
    ├── build.rs              Also compiles + links the Swift speech library
    ├── capabilities/         Tauri v2 capability manifests (least privilege)
    ├── icons/
    │
    ├── src/
    │   ├── main.rs           Entry, composition root
    │   ├── lib.rs
    │   ├── sidecar/          Spawn · health · restart · reap the Python child
    │   ├── security/
    │   │   ├── keychain.rs   macOS Keychain read/write (DEC-017)
    │   │   ├── session.rs    256-bit session token generation
    │   │   └── approval.rs   Signed single-use approval tokens (DEC-010)
    │   ├── system/
    │   │   ├── metrics.rs    sysinfo sampling at 1 Hz (DEC-003)
    │   │   └── screenshot.rs
    │   ├── shell/
    │   │   ├── tray.rs       Menu bar item
    │   │   ├── hotkey.rs     Global hotkey
    │   │   └── windows.rs    Window lifecycle for the three surfaces
    │   ├── speech/
    │   │   ├── mod.rs        Safe Rust wrapper
    │   │   └── ffi.rs        `extern "C"` bindings to the Swift library
    │   ├── rpc/              Rust↔Python local RPC (see API_DESIGN.md §7)
    │   └── commands/         Tauri `#[command]` handlers exposed to the UI
    │
    └── swift/
        └── MittaSpeech/      Swift static library — Speech + AVFoundation
            ├── Package.swift
            └── Sources/      STT · TTS · VAD · audio session · C ABI shim
```

### 3.1 Why `features/` and not `components/` alone

Every surface in R2 is a self-contained product area with its own state, its own
API calls and its own components. Grouping by feature means deleting the plugin
manager is deleting one folder. Grouping by technical kind (`components/`,
`hooks/`, `utils/`) spreads every feature across four directories and makes the
blast radius of any change unknowable. `components/ui/` exists only for
genuinely cross-feature primitives.

### 3.2 The palette entry point is load-bearing

R2 requires the command palette to open in **well under 100 ms**. That budget is
a structural constraint, not a performance goal to optimise toward later:
`palette.tsx` gets its own Vite entry and its own bundle, and it may not import
from `features/` other than a narrow palette-specific slice. If the palette
bundle ever pulls in the chat renderer, the memory explorer or the charting used
by the monitor, the budget is gone and no amount of tuning gets it back. This is
enforced by a bundle-size check in CI rather than by reviewer vigilance.

### 3.3 Two windows, one state layer

DEC-018 requires the palette and main window to share state. They are separate
webviews, so they cannot share a JavaScript heap. `state/sync.ts` replicates
mutations between windows through the Rust core, which is the only component
both can see. The store is the source of truth in whichever window is focused;
the other receives a patch stream. Conversation content itself is never
replicated this way — it is served from the sidecar, which both windows read
from over the same WebSocket.

---

## 4. `core/` — the Python sidecar

```
core/
├── pyproject.toml            uv-managed; single installable package
├── mitta.spec                PyInstaller build spec
├── importlinter.ini          Layer dependency contracts (§4.2)
│
├── mitta/
│   ├── __main__.py           Entry point for the bundled binary
│   ├── bootstrap.py          Composition root — the ONLY place wiring happens
│   │
│   ├── api/                  ── Layer: transport ──
│   │   ├── app.py            FastAPI application factory
│   │   ├── auth.py           Session-token verification
│   │   ├── http/             REST routers, one module per resource
│   │   ├── ws/               WebSocket endpoint, channels, envelope codec
│   │   └── schemas/          Pydantic request/response models (source of truth)
│   │
│   ├── input/                ── Layer 1: Input ──
│   ├── speech/               ── Layer 2: Speech (thin — native side owns audio)
│   │   └── bridge.py         Consumes transcripts, emits synthesis requests
│   │
│   ├── context/              ── Layer 3: Context Manager ──
│   │   ├── assembler.py      THE outbound-payload chokepoint (DEC-016)
│   │   ├── budget.py         Token budgeting against the model's window
│   │   └── recorder.py       Records every payload for UI inspection (R5)
│   │
│   ├── agent/                ── Layer 4: Orchestrator ──
│   │   ├── orchestrator.py   Turn state machine
│   │   ├── turn.py           Turn lifecycle + events
│   │   └── events.py
│   │
│   ├── planner/              ── Layer 5a: Planning Engine ──
│   │   ├── decomposer.py     Goal → task DAG
│   │   ├── executor.py       Resumable execution, checkpoints
│   │   └── graph.py          Dependency resolution
│   │
│   ├── reasoning/            ── Layer 5b: Reasoning Engine ──
│   │   ├── engine.py         Single-step inference + tool choice
│   │   └── prompts/          Versioned prompt templates (assets, not code)
│   │
│   ├── tools/                ── Layer 6: Tool Manager ──
│   │   ├── registry.py       Tool discovery + JSON Schema export
│   │   ├── manifest.py       Tool manifest model
│   │   ├── dispatcher.py     Validation + dispatch. Holds NO OS reference (§4.3)
│   │   └── builtin/          Browser · terminal · git · files · clipboard · …
│   │
│   ├── security/             ── Layer 7: Security ──
│   │   ├── policy.py         ALLOW / CONFIRM / DENY evaluation
│   │   ├── permissions.py    Permission model + user rules
│   │   ├── approval.py       Verifies Rust-signed approval tokens
│   │   └── audit.py          Append-only decision log
│   │
│   ├── memory/               ── Layer 8a: Memory Manager ──
│   │   ├── manager.py        Facade over the stores
│   │   ├── stores/           long_term · project · episodic · relationship · …
│   │   ├── working.py        RAM-only session memory
│   │   ├── embedding/        ONNX bge-small runner + FAISS index management
│   │   ├── retrieval.py      Hybrid FAISS + FTS5 retrieval and fusion
│   │   ├── scoring.py        Importance, decay, deduplication
│   │   └── consolidation.py  Idle-time background worker
│   │
│   ├── llm/                  ── Layer 8b: LLM Gateway ──
│   │   ├── gateway.py        The one interface everything above uses
│   │   ├── router.py         Task-class → model routing
│   │   ├── health.py         Provider health tracking + failover
│   │   ├── capabilities.py   Declared capabilities, not assumed (DEC-020)
│   │   └── providers/        groq.py · openrouter.py (+ future adapters)
│   │
│   ├── plugins/              ── Layer 8c: Plugin Manager ──
│   │   ├── manager.py        Install · update · remove · version compatibility
│   │   ├── host.py           Subprocess lifecycle + supervision
│   │   └── protocol.py       MCP-compatible JSON-RPC over stdio (DEC-009)
│   │
│   ├── os_adapter/           ── Layer 9: OS Adapter ──
│   │   ├── base.py           Protocol definition — the contract
│   │   ├── mac.py            The only implementation in v1
│   │   ├── windows.py        Stub raising NotImplementedError (R1)
│   │   └── factory.py        Platform detection
│   │
│   ├── personality/          ── Layer 10: Output ──
│   │   ├── rewriter.py       Terminal, constrained rewrite stage (DEC-008)
│   │   ├── profile.py        Style profile model
│   │   └── guards.py         Pass-through rules for code/paths/numbers
│   │
│   ├── persistence/          ── Cross-cutting: storage ──
│   │   ├── database.py       Connection, WAL, pragmas
│   │   ├── migrations/       Numbered forward-only SQL migrations
│   │   ├── repositories/     One repository per aggregate (Repository Pattern)
│   │   └── unit_of_work.py   Transaction boundary
│   │
│   ├── scheduler/            Task Scheduler — cron + long-running jobs
│   ├── config/               Settings model, JSON loading, env overrides
│   ├── telemetry/            Structured logging + key redaction filter
│   └── errors.py             Exception hierarchy
│
└── tests/
    ├── unit/                 Mirrors the mitta/ tree exactly
    ├── integration/          Real SQLite, real FAISS, faked network
    └── fixtures/
```

### 4.1 One package, not eight

`core/` contains exactly one installable Python package. Splitting `memory`,
`planner`, `llm` and friends into sibling top-level roots (the shape in the
original brief) looks tidier at the file-manager level but produces real,
recurring costs: eight `pyproject.toml` files or a namespace-package
arrangement, an ambiguous import graph, eight paths to add to the PyInstaller
spec, and `pytest` collection that needs explicit configuration to find
anything. One package with layer subpackages gives the same conceptual
separation with none of that.

### 4.2 Layer dependencies are enforced, not documented

`ARCHITECTURE.md` §3 says each layer depends only on the interfaces below it.
A diagram cannot enforce that. `importlinter.ini` declares the layer order as a
machine-checked contract and CI fails the build on violation. Two contracts
matter most:

- **Layered contract** — the ten layers in order. `memory` may not import
  `agent`; `llm` may not import `tools`.
- **Forbidden contract** — `tools` may not import `os_adapter` (see §4.3), and
  nothing above `os_adapter` may import `subprocess`, reference `osascript`, or
  contain a `~/Library` path (R1's enforcement clause).

This is the difference between an architecture and a description of one.

### 4.3 The Tool Manager has no reference to the OS Adapter

`ARCHITECTURE.md` §3 states the security layer cannot be bypassed, and says this
is enforced structurally. Concretely: `bootstrap.py` constructs the dispatcher
with a reference to the **policy engine**, and the policy engine holds the OS
adapter. The dispatcher never receives one. A developer who wants to skip the
policy check has to change the composition root and the import contract, both of
which are reviewed and both of which fail CI. Convention would have made this a
comment; construction makes it a compile-time-ish property.

### 4.4 Prompts are assets, not code

`reasoning/prompts/` holds versioned template files, not Python string
constants. Prompts change far more often than the code that uses them, they need
to be diffable in review, and eventually they need A/B comparison across model
providers. Embedding them in source makes all three awkward.

---

## 5. `plugins/`, `config/`, `scripts/`, `tests/`

```
plugins/
├── README.md                 Plugin authoring guide
├── _template/                Scaffold for new plugins
└── <plugin-name>/
    ├── manifest.json         Name, version, permissions, tool schemas
    └── src/

config/
├── default.json              Shipped defaults. NEVER contains secrets (DEC-017)
├── schema.json               JSON Schema for validation
├── policy.default.json       Default ALLOW/CONFIRM/DENY rules
└── personality/
    └── mitta.profile.json    Style profile (structure only — no conversations)

scripts/
├── dev.sh                    Run frontend + sidecar with hot reload
├── build.sh                  Full release build
├── package.sh                PyInstaller → .app bundle → .dmg
├── sign.sh                   Codesign + notarise
├── gen-types.sh              Pydantic → TypeScript (§6)
└── db/                       Migration helpers, index rebuild

tests/
├── integration/              Rust ↔ Python across the real process boundary
├── e2e/                      Driven through the built application
└── contract/                 API schema conformance
```

Unit tests live beside their runtime (`core/tests/`, `src-tauri/src/**/tests`)
because they need that runtime's test harness. Only genuinely cross-runtime
tests live at the top level.

---

## 6. The type boundary between Python and TypeScript

Two languages describe the same API. Hand-maintaining both sides guarantees
drift, and the failure mode is a runtime error in production rather than a
build error in CI.

**Pydantic schemas in `core/mitta/api/schemas/` are the single source of
truth.** `scripts/gen-types.sh` exports the OpenAPI document and generates
`apps/desktop/src/types/generated/`. Generated files are committed (so the
frontend builds without a Python environment) and CI fails if regenerating them
produces a diff. Nobody edits generated files by hand.

---

## 7. What each `.gitignore` rule protects

Beyond the usual build artefacts, three exclusions are security-relevant and
exist for stated reasons:

| Pattern | Reason |
| --- | --- |
| `data/`, `*.db`, `*.db-wal`, `vectors/` | A committed memory database would leak everything R5 promises to protect |
| `.env*`, `*.key`, `secrets*` | Defence in depth. Keys belong in the Keychain and should never reach a file, but the repo should refuse them anyway (DEC-017) |
| `models/` | Multi-hundred-MB ONNX weights are build inputs, fetched by script and checksummed, never committed |

---

## 8. Naming and conventions

| Item | Convention |
| --- | --- |
| Python modules, packages | `snake_case` |
| Python classes | `PascalCase`; protocols suffixed `Protocol` |
| React components, files | `PascalCase.tsx` |
| Hooks, utilities | `camelCase.ts` |
| Rust modules | `snake_case`; one concern per module |
| Migrations | `NNNN_short_description.sql`, forward-only |
| Tests | `test_<module>.py` mirroring the source path exactly |

Every Python module is fully type-hinted and checked with `mypy --strict`.
Every TypeScript file compiles under `strict: true`. Neither is negotiable per
the code-quality requirement, and both are cheapest to adopt now — retrofitting
strict typing onto a grown codebase is a project of its own.
