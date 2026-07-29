# MITTA

A personal AI desktop companion for macOS. It remembers what you tell it,
learns from conversation, searches the web, and asks before it touches anything.

Built for one person, on one machine. Memory lives in SQLite and FAISS on your
disk; embeddings run locally; nothing leaves the machine except the requests
that answer what you just asked.

---

## Run it

```bash
make install && make install-ui     # once
make set-key-groq                   # paste your key — hidden input, no shell history
make download-model                 # 67 MB, on-device embeddings
make seed-profile                   # optional: load a personal context profile

make app                            # the desktop app
make dev                            # or in a browser at http://127.0.0.1:1420
```

`make help` lists everything.

## What works

| | |
| --- | --- |
| **Memory** | Six kinds over one table, hybrid vector + keyword recall, decay, deduplication |
| **Learning** | Durable facts extracted from conversation, credentials refused |
| **Reasoning** | Groq and OpenRouter with health-based failover |
| **Personality** | Register-based rewrite, verified so it cannot change meaning |
| **Tools** | Web search, open an application, write a note |
| **Permissions** | Parameter-bound single-use approvals, hash-chained audit log |
| **Surfaces** | Chat, Memory, History, Monitor, Settings |
| **Shell** | Tauri window, sidecar supervisor, Keychain, ⌘⇧Space palette |

## What does not

Projects, Tasks and Plugins are placeholders and say so. There is no voice
input — the wake word is decided ("MITTA") but Apple ships no wake-word API and
the activation mechanism is still open (R7). No planner, so tool use is one
round rather than a chain. Tool *selection* is unreliable: naming the tool
works, natural phrasing is hit-or-miss.

## Checking it rather than trusting it

```bash
make check-all      # 448 Python · 55 TypeScript · 37 Rust, plus three import contracts
make secrets        # fails if a credential is in any committed file
make check-models   # verifies the hardcoded model ids still exist upstream
```

**Settings** shows provider health, which key source is active (never a key
value), the embedding model and whether it is the fallback, the permission
tiers, and every action MITTA has taken with its hash chain verified on read.

## Layout

```
core/           Python sidecar — memory, agent, LLM gateway, tools, policy
apps/desktop/   React frontend
  src-tauri/    Rust shell — supervises the sidecar, owns the Keychain
docs/           Requirements, architecture, and 93 recorded decisions
scripts/        Development entry points
```

`docs/DECISIONS.md` is the honest record: what was chosen, why, what was tried
first and failed, and which bugs only appeared when the thing was actually run.

## Keys

Two paths, and neither puts a key in a file you can commit:

- `make set-key-groq` writes to `.env`, which is gitignored and verified as
  uncommittable.
- In the shipped app, Settings writes to the macOS Keychain. No IPC command
  returns a key value, so it cannot be read back into the webview.

A pre-commit hook (`make install-hooks`) fails any commit containing something
shaped like a credential.
