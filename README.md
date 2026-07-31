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
| **Tools** | Web search, open an application, close an application, open a website, write a note |
| **Planner** | Bounded tool chains — four rounds, six calls, repeats served from cache |
| **Permissions** | Parameter-bound single-use approvals, hash-chained audit log |
| **Projects** | Scope for memory and conversations, and the filesystem boundary — registered roots, exclusions that beat them, write granted per path |
| **Schedules** | Cron in your own timezone, DST included. A scheduled question can search and read; a scheduled tool call runs the exact arguments you wrote, and nothing else |
| **Tasks** | Every step of every run, with what it did, why it stopped, and a retry that does not repeat what already succeeded |
| **Voice** | On-device speech in and out — push-to-talk, or wake on "MITTA" with an energy-gated microphone |
| **Surfaces** | Chat, Memory, Projects, Tasks, History (clearable by period), Monitor, Settings |
| **Shell** | Tauri window, sidecar supervisor, Keychain, ⌘⇧Space palette |

## What does not

Plugins is a placeholder and says so.

The project write boundary is enforced by the policy engine, but no tool
currently declares a filesystem path for it to check: `write_note` has its own
narrower sandbox. The boundary was built before the tool that needs it on
purpose, and it is exercised only by tests until that tool lands (DEC-112).
A project's timeline endpoint returns nothing, because nothing writes an episode
yet.

Nothing writes a plan except a schedule coming due, so task dependencies are
always a straight line and the cycle check has only tests to catch (DEC-121).
Resuming a scheduled *question* re-asks it rather than continuing it — its steps
were chosen by a model one at a time, so there is no recorded sequence to
continue from (DEC-126). The Tasks surface polls rather than being pushed to: a
scheduled run starts with no socket frame behind it, and one surface did not
justify a second push channel.

Tool selection depends on a provider that intermittently rejects its own
model's tool calls. MITTA recovers the call from Groq's error body when it can
(DEC-102), but a chain is still only as reliable as the model driving it.

## Checking it rather than trusting it

```bash
make check-all      # 691 Python · 103 TypeScript · 46 Rust, plus three import contracts
make secrets        # fails if a credential is in any committed file
make check-models   # verifies the hardcoded model ids still exist upstream
```

**Settings** shows provider health, which key source is active (never a key
value), the embedding model and whether it is the fallback, the permission
tiers, and every action MITTA has taken with its hash chain verified on read.

## Layout

```
core/           Python sidecar — memory, agent, LLM gateway, tools, policy, tasks
apps/desktop/   React frontend
  src-tauri/    Rust shell — supervises the sidecar, owns the Keychain
docs/           Requirements, architecture, and 127 recorded decisions
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
