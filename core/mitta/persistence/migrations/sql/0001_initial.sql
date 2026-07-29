-- MITTA initial schema. Implements DATABASE_DESIGN.md.
--
-- Forward-only (DEC-031). Applied inside a single transaction by the runner,
-- so this file contains no PRAGMA statements and no explicit BEGIN/COMMIT —
-- connection pragmas are set in persistence/database.py.
--
-- Conventions (DATABASE_DESIGN.md §1):
--   seq  INTEGER PRIMARY KEY AUTOINCREMENT  → never-reused FAISS id (DEC-025)
--   id   TEXT UNIQUE                        → prefixed ULID for external use
--   timestamps                              → INTEGER epoch milliseconds, UTC

-- ===========================================================================
-- Projects
-- ===========================================================================

CREATE TABLE projects (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    description TEXT,
    color       TEXT,
    status      TEXT    NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','archived')),
    settings    TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(settings)),
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE INDEX idx_projects_status ON projects(status, updated_at DESC);

CREATE TABLE project_paths (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path       TEXT    NOT NULL,
    kind       TEXT    NOT NULL DEFAULT 'root'
               CHECK (kind IN ('root','repo','docs','excluded')),
    writable   INTEGER NOT NULL DEFAULT 0 CHECK (writable IN (0,1)),
    created_at INTEGER NOT NULL,
    UNIQUE (project_id, path)
);

-- Resolved on every filesystem tool call by the policy engine. Must not scan.
CREATE INDEX idx_project_paths_lookup ON project_paths(path);

CREATE TABLE project_resources (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT    NOT NULL UNIQUE,
    project_id TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL
               CHECK (kind IN ('repo','url','file','note','task','decision')),
    title      TEXT    NOT NULL,
    uri        TEXT,
    body       TEXT,
    metadata   TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(metadata)),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX idx_project_resources ON project_resources(project_id, kind);

-- ===========================================================================
-- Conversations, turns, messages
-- ===========================================================================

CREATE TABLE conversations (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    id            TEXT    NOT NULL UNIQUE,
    title         TEXT,
    project_id    TEXT    REFERENCES projects(id) ON DELETE SET NULL,
    status        TEXT    NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','archived')),
    pinned        INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0,1)),
    forked_from   TEXT,
    summary       TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);

CREATE INDEX idx_conversations_recent ON conversations(status, updated_at DESC);
CREATE INDEX idx_conversations_project ON conversations(project_id)
    WHERE project_id IS NOT NULL;

CREATE TABLE plans (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    id              TEXT    NOT NULL UNIQUE,
    goal            TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','approved','running','paused',
                                      'completed','failed','cancelled')),
    project_id      TEXT    REFERENCES projects(id) ON DELETE SET NULL,
    conversation_id TEXT    REFERENCES conversations(id) ON DELETE SET NULL,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE INDEX idx_plans_status ON plans(status, updated_at DESC);

CREATE TABLE turns (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    id              TEXT    NOT NULL UNIQUE,
    conversation_id TEXT    NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    project_id      TEXT    REFERENCES projects(id) ON DELETE SET NULL,
    status          TEXT    NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','completed','failed',
                                      'cancelled','awaiting_approval')),
    input_kind      TEXT    NOT NULL
                    CHECK (input_kind IN ('text','voice','palette','scheduled')),
    register        TEXT    CHECK (register IN ('playful','serious')),
    plan_id         TEXT    REFERENCES plans(id) ON DELETE SET NULL,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    tool_call_count INTEGER NOT NULL DEFAULT 0,
    error           TEXT    CHECK (error IS NULL OR json_valid(error)),
    started_at      INTEGER NOT NULL,
    ended_at        INTEGER
);

CREATE INDEX idx_turns_conversation ON turns(conversation_id, started_at DESC);
-- Partial index: how the sidecar finds turns a crash left mid-flight.
CREATE INDEX idx_turns_unfinished ON turns(status)
    WHERE status IN ('running','awaiting_approval');

CREATE TABLE messages (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    id              TEXT    NOT NULL UNIQUE,
    conversation_id TEXT    NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    turn_id         TEXT    REFERENCES turns(id) ON DELETE SET NULL,
    role            TEXT    NOT NULL
                    CHECK (role IN ('user','assistant','system','tool')),
    content         TEXT    NOT NULL,
    content_raw     TEXT,
    tool_calls      TEXT    CHECK (tool_calls IS NULL OR json_valid(tool_calls)),
    tool_call_id    TEXT,
    input_kind      TEXT    CHECK (input_kind IS NULL OR
                                   input_kind IN ('text','voice','palette','scheduled')),
    model_id        TEXT,
    provider        TEXT,
    register        TEXT    CHECK (register IS NULL OR register IN ('playful','serious')),
    token_input     INTEGER,
    token_output    INTEGER,
    latency_ms      INTEGER,
    styled          INTEGER NOT NULL DEFAULT 0 CHECK (styled IN (0,1)),
    error           TEXT    CHECK (error IS NULL OR json_valid(error)),
    created_at      INTEGER NOT NULL
);

CREATE INDEX idx_messages_conversation ON messages(conversation_id, seq);
CREATE INDEX idx_messages_turn         ON messages(turn_id);

-- ===========================================================================
-- Memory
-- ===========================================================================

CREATE TABLE memories (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    id                TEXT    NOT NULL UNIQUE,
    kind              TEXT    NOT NULL
                      CHECK (kind IN ('long_term','project','episodic',
                                      'relationship','preference','procedural')),
    project_id        TEXT    REFERENCES projects(id) ON DELETE CASCADE,
    content           TEXT    NOT NULL,
    summary           TEXT,
    attributes        TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(attributes)),
    importance        REAL    NOT NULL DEFAULT 0.5
                      CHECK (importance BETWEEN 0.0 AND 1.0),
    confidence        REAL    NOT NULL DEFAULT 1.0
                      CHECK (confidence BETWEEN 0.0 AND 1.0),
    status            TEXT    NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active','superseded','forgotten')),
    superseded_by     TEXT    REFERENCES memories(id) ON DELETE SET NULL,
    source_kind       TEXT    NOT NULL
                      CHECK (source_kind IN ('conversation','tool','user',
                                             'import','consolidation')),
    source_message_id TEXT    REFERENCES messages(id) ON DELETE SET NULL,
    content_hash      TEXT    NOT NULL,
    pinned            INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0,1)),
    access_count      INTEGER NOT NULL DEFAULT 0,
    last_accessed_at  INTEGER,
    expires_at        INTEGER,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);

CREATE INDEX idx_memories_kind_status ON memories(kind, status);
CREATE INDEX idx_memories_project     ON memories(project_id, status)
    WHERE project_id IS NOT NULL;
CREATE INDEX idx_memories_importance  ON memories(importance DESC, status);
CREATE INDEX idx_memories_created     ON memories(created_at DESC);
CREATE INDEX idx_memories_hash        ON memories(content_hash);
CREATE INDEX idx_memories_expiry      ON memories(expires_at)
    WHERE expires_at IS NOT NULL;

-- Keyword half of hybrid retrieval (DEC-024). External-content table: the
-- content lives in `memories`, FTS5 stores only the index.
CREATE VIRTUAL TABLE memories_fts USING fts5(
    content,
    summary,
    content='memories',
    content_rowid='seq',
    tokenize='porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER memories_fts_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, summary)
    VALUES (new.seq, new.content, new.summary);
END;

CREATE TRIGGER memories_fts_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, summary)
    VALUES ('delete', old.seq, old.content, old.summary);
END;

CREATE TRIGGER memories_fts_au AFTER UPDATE OF content, summary ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, summary)
    VALUES ('delete', old.seq, old.content, old.summary);
    INSERT INTO memories_fts(rowid, content, summary)
    VALUES (new.seq, new.content, new.summary);
END;

-- Vector half. `content_hash` is the hash AT EMBED TIME; comparing it to
-- memories.content_hash is how the background worker discovers stale vectors
-- without an in-memory queue a crash would lose (DEC-025).
CREATE TABLE memory_embeddings (
    memory_seq   INTEGER PRIMARY KEY REFERENCES memories(seq) ON DELETE CASCADE,
    index_name   TEXT    NOT NULL,
    model_id     TEXT    NOT NULL,
    dim          INTEGER NOT NULL,
    content_hash TEXT    NOT NULL,
    indexed_at   INTEGER NOT NULL
);

CREATE INDEX idx_embeddings_model ON memory_embeddings(model_id, index_name);

CREATE TABLE vector_indexes (
    index_name   TEXT    PRIMARY KEY,
    model_id     TEXT    NOT NULL,
    dim          INTEGER NOT NULL,
    metric       TEXT    NOT NULL DEFAULT 'ip',
    factory      TEXT    NOT NULL,
    vector_count INTEGER NOT NULL DEFAULT 0,
    rebuilt_at   INTEGER,
    schema_epoch INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE memory_links (
    from_seq   INTEGER NOT NULL REFERENCES memories(seq) ON DELETE CASCADE,
    to_seq     INTEGER NOT NULL REFERENCES memories(seq) ON DELETE CASCADE,
    relation   TEXT    NOT NULL
               CHECK (relation IN ('supports','contradicts','refines',
                                   'caused_by','part_of','duplicate_of')),
    weight     REAL    NOT NULL DEFAULT 1.0,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (from_seq, to_seq, relation)
) WITHOUT ROWID;

CREATE INDEX idx_links_to ON memory_links(to_seq, relation);

-- A person is an entity with identity, not a fact (DEC-023).
CREATE TABLE people (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT    NOT NULL UNIQUE,
    display_name TEXT    NOT NULL,
    aliases      TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(aliases)),
    relation     TEXT,
    notes        TEXT,
    contact      TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(contact)),
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE TABLE memory_people (
    memory_seq INTEGER NOT NULL REFERENCES memories(seq) ON DELETE CASCADE,
    person_seq INTEGER NOT NULL REFERENCES people(seq)   ON DELETE CASCADE,
    role       TEXT,
    PRIMARY KEY (memory_seq, person_seq)
) WITHOUT ROWID;

CREATE INDEX idx_memory_people_person ON memory_people(person_seq);

-- An episode is an event on a timeline, queried by time range (DEC-023).
CREATE TABLE episodes (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT    NOT NULL UNIQUE,
    occurred_at INTEGER NOT NULL,
    event_type  TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    detail      TEXT,
    project_id  TEXT    REFERENCES projects(id) ON DELETE CASCADE,
    memory_seq  INTEGER REFERENCES memories(seq) ON DELETE SET NULL,
    payload     TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(payload)),
    created_at  INTEGER NOT NULL
);

CREATE INDEX idx_episodes_time    ON episodes(occurred_at DESC);
CREATE INDEX idx_episodes_project ON episodes(project_id, occurred_at DESC);

-- Machine-readable settings, read by key. Distinct from narrative preference
-- memories, which are for semantic recall (DEC-023).
CREATE TABLE preferences (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL CHECK (json_valid(value)),
    scope      TEXT NOT NULL DEFAULT 'global',
    source     TEXT NOT NULL DEFAULT 'explicit'
               CHECK (source IN ('explicit','inferred','default')),
    confidence REAL NOT NULL DEFAULT 1.0,
    updated_at INTEGER NOT NULL
) WITHOUT ROWID;

-- ===========================================================================
-- Planner
-- ===========================================================================

CREATE TABLE tasks (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT    NOT NULL UNIQUE,
    plan_id      TEXT    NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    parent_id    TEXT    REFERENCES tasks(id) ON DELETE CASCADE,
    ordinal      INTEGER NOT NULL,
    title        TEXT    NOT NULL,
    description  TEXT,
    tool_name    TEXT,
    params       TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(params)),
    status       TEXT    NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','ready','running','blocked',
                                   'awaiting_approval','completed','failed','skipped')),
    attempt      INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    result       TEXT    CHECK (result IS NULL OR json_valid(result)),
    error        TEXT    CHECK (error IS NULL OR json_valid(error)),
    started_at   INTEGER,
    ended_at     INTEGER,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE INDEX idx_tasks_plan     ON tasks(plan_id, ordinal);
CREATE INDEX idx_tasks_runnable ON tasks(status) WHERE status IN ('ready','running');

CREATE TABLE task_dependencies (
    task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on),
    CHECK (task_id <> depends_on)
) WITHOUT ROWID;

CREATE INDEX idx_task_deps_reverse ON task_dependencies(depends_on);

CREATE TABLE task_checkpoints (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT    NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    label      TEXT    NOT NULL,
    state      TEXT    NOT NULL CHECK (json_valid(state)),
    created_at INTEGER NOT NULL
);

CREATE INDEX idx_checkpoints_task ON task_checkpoints(task_id, seq DESC);

CREATE TABLE schedules (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    cron        TEXT    NOT NULL,
    timezone    TEXT    NOT NULL DEFAULT 'UTC',
    action      TEXT    NOT NULL CHECK (json_valid(action)),
    enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    last_run_at INTEGER,
    next_run_at INTEGER,
    created_at  INTEGER NOT NULL
);

CREATE INDEX idx_schedules_due ON schedules(next_run_at) WHERE enabled = 1;

-- ===========================================================================
-- Plugins
-- ===========================================================================

CREATE TABLE plugins (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT    NOT NULL UNIQUE,
    name         TEXT    NOT NULL UNIQUE,
    version      TEXT    NOT NULL,
    manifest     TEXT    NOT NULL CHECK (json_valid(manifest)),
    install_path TEXT    NOT NULL,
    source       TEXT    NOT NULL CHECK (source IN ('builtin','local','registry')),
    core_range   TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'enabled'
                 CHECK (status IN ('enabled','disabled','error','incompatible')),
    checksum     TEXT    NOT NULL,
    last_error   TEXT,
    installed_at INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE TABLE plugin_permissions (
    plugin_id  TEXT    NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
    permission TEXT    NOT NULL,
    granted    INTEGER NOT NULL DEFAULT 0 CHECK (granted IN (0,1)),
    granted_at INTEGER,
    PRIMARY KEY (plugin_id, permission)
) WITHOUT ROWID;

-- ===========================================================================
-- Security, audit, tool execution
-- ===========================================================================

CREATE TABLE approval_tokens (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT    NOT NULL UNIQUE,
    turn_id     TEXT    REFERENCES turns(id) ON DELETE CASCADE,
    tool_name   TEXT    NOT NULL,
    params_hash TEXT    NOT NULL,
    nonce       TEXT    NOT NULL UNIQUE,
    issued_at   INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    consumed_at INTEGER,
    decision    TEXT    NOT NULL CHECK (decision IN ('approved','denied'))
);

-- Single-use enforced in the database, not in process memory, so a crash
-- cannot reopen a replay window (DEC-030).
CREATE INDEX idx_approval_open ON approval_tokens(expires_at)
    WHERE consumed_at IS NULL;

CREATE TABLE tool_invocations (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT    NOT NULL UNIQUE,
    turn_id     TEXT    REFERENCES turns(id) ON DELETE CASCADE,
    task_id     TEXT    REFERENCES tasks(id) ON DELETE SET NULL,
    tool_name   TEXT    NOT NULL,
    plugin_id   TEXT    REFERENCES plugins(id) ON DELETE SET NULL,
    params      TEXT    NOT NULL CHECK (json_valid(params)),
    params_hash TEXT    NOT NULL,
    verdict     TEXT    NOT NULL CHECK (verdict IN ('allow','confirm','deny')),
    approval_id TEXT    REFERENCES approval_tokens(id) ON DELETE SET NULL,
    status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','running','succeeded','failed',
                                  'denied','timeout')),
    result      TEXT    CHECK (result IS NULL OR json_valid(result)),
    error       TEXT    CHECK (error IS NULL OR json_valid(error)),
    duration_ms INTEGER,
    created_at  INTEGER NOT NULL
);

CREATE INDEX idx_invocations_turn ON tool_invocations(turn_id, seq);
CREATE INDEX idx_invocations_tool ON tool_invocations(tool_name, created_at DESC);

CREATE TABLE permission_rules (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT    NOT NULL UNIQUE,
    subject      TEXT    NOT NULL,
    subject_kind TEXT    NOT NULL
                 CHECK (subject_kind IN ('tool','plugin','capability')),
    condition    TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(condition)),
    effect       TEXT    NOT NULL CHECK (effect IN ('allow','confirm','deny')),
    priority     INTEGER NOT NULL DEFAULT 100,
    source       TEXT    NOT NULL DEFAULT 'user'
                 CHECK (source IN ('default','user','plugin')),
    enabled      INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    created_at   INTEGER NOT NULL
);

CREATE INDEX idx_rules_eval ON permission_rules(subject_kind, enabled, priority);

-- Hash-chained: tamper-evident, not tamper-proof (DEC-030).
CREATE TABLE audit_log (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    id            TEXT    NOT NULL UNIQUE,
    at            INTEGER NOT NULL,
    actor         TEXT    NOT NULL
                  CHECK (actor IN ('user','agent','plugin','scheduler','system')),
    action        TEXT    NOT NULL,
    subject       TEXT,
    verdict       TEXT    CHECK (verdict IS NULL OR verdict IN ('allow','confirm','deny')),
    turn_id       TEXT,
    invocation_id TEXT,
    detail        TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(detail)),
    prev_hash     TEXT,
    entry_hash    TEXT    NOT NULL
);

CREATE INDEX idx_audit_time   ON audit_log(at DESC);
CREATE INDEX idx_audit_action ON audit_log(action, at DESC);

-- ===========================================================================
-- LLM request record — this table IS the R5 privacy guarantee (DEC-016).
-- Holds a verbatim copy of every payload sent upstream; shortest retention.
-- ===========================================================================

CREATE TABLE llm_requests (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    id            TEXT    NOT NULL UNIQUE,
    turn_id       TEXT    REFERENCES turns(id) ON DELETE CASCADE,
    purpose       TEXT    NOT NULL
                  CHECK (purpose IN ('reasoning','planning','personality',
                                     'summarisation','titling','extraction')),
    provider      TEXT    NOT NULL,
    model_id      TEXT    NOT NULL,
    payload       TEXT    NOT NULL CHECK (json_valid(payload)),
    payload_bytes INTEGER NOT NULL,
    memory_ids    TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(memory_ids)),
    tokens_in     INTEGER,
    tokens_out    INTEGER,
    latency_ms    INTEGER,
    status        TEXT    NOT NULL
                  CHECK (status IN ('succeeded','failed','timeout','rate_limited')),
    failover_from TEXT,
    created_at    INTEGER NOT NULL
);

CREATE INDEX idx_llm_requests_turn ON llm_requests(turn_id, seq);
CREATE INDEX idx_llm_requests_time ON llm_requests(created_at DESC);

-- ===========================================================================
-- Notifications and settings
-- ===========================================================================

CREATE TABLE notifications (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT    NOT NULL UNIQUE,
    level      TEXT    NOT NULL CHECK (level IN ('info','success','warning','error')),
    title      TEXT    NOT NULL,
    body       TEXT,
    source     TEXT,
    action     TEXT    CHECK (action IS NULL OR json_valid(action)),
    read_at    INTEGER,
    created_at INTEGER NOT NULL
);

CREATE INDEX idx_notifications_unread ON notifications(created_at DESC)
    WHERE read_at IS NULL;

CREATE TABLE app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL CHECK (json_valid(value)),
    updated_at INTEGER NOT NULL
) WITHOUT ROWID;

-- ===========================================================================
-- Views — for the human who opens this file in a SQLite browser (DEC-005).
-- ===========================================================================

CREATE VIEW v_memories AS
SELECT m.id,
       m.kind,
       m.content,
       m.importance,
       m.status,
       datetime(m.created_at / 1000, 'unixepoch', 'localtime')       AS created,
       datetime(m.last_accessed_at / 1000, 'unixepoch', 'localtime') AS last_accessed,
       p.name                                                        AS project,
       (e.memory_seq IS NOT NULL AND e.content_hash = m.content_hash) AS indexed
FROM   memories m
LEFT   JOIN projects p          ON p.id = m.project_id
LEFT   JOIN memory_embeddings e ON e.memory_seq = m.seq;

CREATE VIEW v_index_health AS
SELECT (SELECT COUNT(*) FROM memories WHERE status = 'active') AS active_memories,
       (SELECT COUNT(*) FROM memory_embeddings)                AS embedded,
       (SELECT COUNT(*)
          FROM memories m
          LEFT JOIN memory_embeddings e ON e.memory_seq = m.seq
         WHERE m.status = 'active'
           AND (e.memory_seq IS NULL OR e.content_hash <> m.content_hash)) AS pending;
