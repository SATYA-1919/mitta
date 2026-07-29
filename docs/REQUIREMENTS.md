# MITTA — Product Requirements

Canonical requirements record. Where this document and `ARCHITECTURE.md`
disagree, this document wins and the architecture is wrong.

Owner: Satya (product owner)
Lead engineer: Claude
Last updated: 2026-07-29

---

## R1 — Platform Scope

**macOS-first. Windows is not built at this stage.**

| Item | Requirement |
| --- | --- |
| Minimum OS | macOS Sequoia (15) and newer |
| Primary target | Apple Silicon (M-series) |
| Intel Macs | Not a target; not blocked |
| Windows 10 / 11 | **Deferred.** No development time spent now |

**Modularity requirement.** The OS abstraction must be built now even though
only one implementation exists, so Windows can be added later without a
refactor. Concretely: no `subprocess("osascript")`, no `~/Library/...` path, and
no AppleScript string may appear anywhere above the OS Adapter boundary. A
`WindowsAdapter` stub raising `NotImplementedError` documents the contract and
keeps the abstraction honest.

This is a real cost — a single-implementation abstraction is harder to get right
than one written against two. Accepted deliberately.

---

## R2 — User Interface

The UI is a first-class product requirement, not a wrapper around the backend.
Quality bar: **comparable to commercial desktop applications.**

**Design references.** Raycast, Cursor, Warp, Arc Browser, Linear, Claude
Desktop, VS Code.

**Design language.**

| Property | Requirement |
| --- | --- |
| Theme | Dark |
| Palette | Matte black / graphite |
| Glassmorphism | Only where appropriate — accents, overlays. Not everywhere |
| Animation | Minimal, subtle, purposeful. Nothing decorative |
| Performance | Fast. No jank, no blocking on backend state |
| Tone | Developer-oriented, professional, premium |

**Shell paradigm — three surfaces.**

| Surface | Role |
| --- | --- |
| Menu bar item | Resident presence; status, quick toggles, activation |
| Command palette | Global hotkey overlay, Raycast-shaped. Fast capture and quick commands. Must open in well under 100 ms |
| Main window | Persistent, Cursor/Linear-shaped. Chat, projects, memory, monitor, settings |

The palette and the main window are two front-ends over **one** state layer, not
two applications. A command started in the palette must be continuable in the
window without losing context. This constraint is what keeps the two surfaces
from drifting into inconsistency, and it is a design requirement, not an
implementation detail.

**Amended 2026-07-29.** The original blanket prohibition — "Iron Man / JARVIS
aesthetics, holograms, sci-fi animation, glow effects, neon" — is relaxed at the
product owner's request. He asked for a JARVIS-inspired technical look, with
latitude on the specifics.

**What is now wanted.** Dense and instrumented: a cyan accent, monospace for
every number, corner brackets and rule lines instead of soft cards, uppercase
tracked labels, live readouts, a faint grid.

**What still stands, and why.** No animated rings, no pulsing glow behind text,
no scanlines over content. Not on taste grounds — those specific things fight
legibility, and this is a surface used for hours rather than looked at once. The
accent's chroma is deliberately held below a neon value for the same reason: a
saturated cyan against near-black blooms on an OLED panel and puts a halo on
adjacent text.

The line that survives from the original requirement is the useful half: MITTA
should read as an instrument, not as a title sequence.

**Required surfaces.** Sidebar · chat panel · voice waveform · thinking
indicator · command palette · project explorer · memory explorer · plugin
manager · system monitor (CPU, RAM, GPU, battery) · active tasks · running
automations · recent memories · conversation history · notification centre ·
settings · model selector.

---

## R3 — AI Models

**Confirmed providers: Groq (primary) and OpenRouter (secondary), both BYOK.**

- The gateway must **fail over automatically** between them. If one is
  rate-limited, timing out or erroring, the other serves the request without a
  code change or a restart. Health-based, not round-robin.
- Build the **provider abstraction and configuration system now**, without keys.
- **Stop and ask for the keys** when integration is reached. Do not use
  placeholders, do not stub inference, do not proceed with fake credentials.
- **Never hardcode API keys.** Never commit them. Never write them to the config
  tree or to logs. Keys live only in the macOS Keychain, entered through the
  settings UI so they never transit a file or shell history.
- Multiple providers must be interchangeable through one interface. No component
  above the LLM Gateway may name a vendor.

> **Security note (2026-07-29).** Keys for both providers were shared in chat
> and must be treated as compromised. They were **not** written to disk. Rotate
> both before integration; enter the replacements through the settings UI.

---

## R4 — Memory & Storage

**Everything local. On this Mac.**

Prohibited without exception: Firebase, Supabase, MongoDB Atlas, any hosted
database, any hosted vector service, any hosted memory or sync service.

| Data | Store |
| --- | --- |
| Structured memory | SQLite |
| Semantic memory | FAISS |
| Documents, images, projects, logs | Local filesystem |

Storage location is user-configurable; the default is on-device. There is no
sync, no backup service and no remote replica.

---

## R5 — Privacy

**Local-first is a hard constraint, not a preference.**

- Only the information required to answer the **current request** may be sent to
  the selected LLM.
- The memory database **must never be uploaded**, in whole or in part beyond the
  retrieved working set for the active turn.
- Nothing leaves the machine except requests to the configured LLM APIs.
- No telemetry. No analytics. No crash reporting to a remote endpoint.
- Embeddings are computed locally, so semantic memory never requires a network
  call. This is what makes R5 achievable rather than aspirational.

**Enforcement.** Context assembly runs through a single budgeted chokepoint that
records exactly what was sent. Anything the user can't inspect, they can't trust.

---

## R6 — Working Agreement

The product owner makes product decisions. The lead engineer makes engineering
decisions and surfaces the trade-offs.

**Stop and ask** — do not proceed on assumption — when any of the following is
needed: API keys, PDFs, datasets, configuration, assets, credentials, or a
design decision with multiple valid answers.

**Phase gate.** One phase at a time. After each: explain what was built, why,
assumptions made, limitations accepted. Then wait for explicit approval.

---

## R7 — Voice

**Apple native, on-device.** `Speech` framework for recognition,
`AVSpeechSynthesizer` for synthesis. No model downloads, no per-minute cost, no
audio leaves the machine.

Consequence: the speech layer runs **native-side (Rust/Swift)**, not in Python,
because these are Swift/ObjC-only frameworks. Python consumes transcripts and
emits synthesis requests over RPC. See DEC-019.

**Open — needs a decision at Phase 6.** Apple provides no wake-word API. How
"MITTA" activation works is undecided and must not be assumed.

---

## R8 — Offline Scope

v1 reasoning is **cloud-only** (Groq, OpenRouter). Ollama is deferred entirely.

What still works with no network: the UI, memory storage and retrieval, semantic
search (embeddings are local), project data, system monitoring, and all local
tools. What does not: reasoning, planning and conversation.

This is a deliberate narrowing of the original "work offline with local models"
goal. See DEC-020 for the risk and its mitigation.

---

## Amendment Record

| Date | Change |
| --- | --- |
| 2026-07-29 | Initial requirements from Phase 1 review. Platform narrowed to macOS-first (supersedes cross-platform scope in DEC-001/DEC-003). Model posture changed to two-key BYOK (amends DEC-007). Local-only storage ratified (confirms DEC-005/DEC-006). |
| 2026-07-29 | **Phase 2.** R8 confirmed: "completely cloud" applies to *reasoning models only* — embeddings remain on-device, so R5 stays enforceable (DEC-022). No requirement changed. Design records added: `PROJECT_STRUCTURE.md`, `API_DESIGN.md`, `DATABASE_DESIGN.md`. |
| 2026-07-29 | **Phase 2 amendment.** Personality length is governed by *register*, not a length threshold (DEC-033). Default is playful and short; serious register permits length. Closes the open question in `PERSONALITY_PROFILE.md`. |
| 2026-07-29 | **Phase 3.** Backend foundation implemented: config, telemetry, OS adapter, persistence, migrations, API skeleton. No requirement changed. R3 still pending — **API keys not yet requested**; the LLM gateway lands in Phase 7. |
| 2026-07-29 | **Phase 4a.** Frontend foundation implemented: design tokens, transport, single state layer, main window and palette shells. Phase 4 split — the Tauri Rust shell becomes **4b**, pending the Rust toolchain (DEC-042). R2's sub-100 ms palette budget is now CI-enforced (DEC-045). |
| 2026-07-29 | **Phase 5.** Memory engine implemented: six kinds over one table, local ONNX embeddings, FAISS, hybrid retrieval, retention/decay, HTTP surface. R4 and R5 both hold — the embedding model runs on-device and its download is explicit (DEC-050). No requirement changed. |
| 2026-07-29 | **Phase 4b.** Tauri shell implemented: sidecar supervisor, Keychain, 1 Hz metrics, global hotkey, two windows. R2's three surfaces now exist as real windows. R3's key handling is enforced structurally — no IPC command returns a key (DEC-060). **Wake word confirmed as "MITTA"**; the activation *mechanism* remains open for Phase 6 (R7). |
| 2026-07-29 | **Personal profile supplied.** `Satya_Personal_Profile.pdf` seeded as 31 typed memories (`make seed-profile`). Settles DEC-033's register rule in the user's own words: concise for simple questions, stepped for difficult technical topics, playful for social. R5 unaffected — the profile is stored locally and only the retrieved working set for a turn is ever sent. |
| 2026-07-29 | **R2 amended.** JARVIS-inspired technical aesthetic requested and adopted: cyan accent, monospace readouts, corner brackets, grid texture. The prohibition narrows from all sci-fi styling to the specific effects that harm legibility — animated glow, scanlines over content, neon-chroma accents (DEC-090). |
| 2026-07-30 | **Phase 10.** Projects implemented, scoped to the filesystem boundary (DEC-105). `ARCHITECTURE.md` §9's "outside a configured project root" CONFIRM trigger now has data behind it and a way for the user to state it. No requirement changed; two are newly enforceable rather than aspirational — R5's inspection clause gains `GET /v1/projects/resolve-path`, which reports what the policy engine would decide about a path before any tool asks (DEC-113), and the write boundary is consulted by the engine rather than by each tool (DEC-109). `project_resources` deliberately left unbuilt: `API_DESIGN.md` §3.4 specifies no endpoint for it. |
