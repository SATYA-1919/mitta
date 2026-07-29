/**
 * Settings — where several "trust me" claims become things you can see.
 *
 * Provider health, key status, embedding model, storage, and the audit log.
 * R5's enforcement clause is that anything the user cannot inspect they cannot
 * trust; this is where the inspection happens.
 *
 * Nothing here reads a key back. `has_api_key` returns a boolean and there is
 * no command that returns a value (DEC-060), so the pane can say *configured*
 * and never *what*.
 */

import { useEffect, useState } from 'react';

import { HudPanel, HudRule } from '@/components/ui/hud';
import { Button, cx, StatusDot } from '@/components/ui/primitives';
import type { MemoryStats } from '@/lib/api/client';
import { useMemoryStore } from '@/state/memory';
import { useStore } from '@/state/store';

interface ProviderRow {
  name: string;
  configured: boolean;
  state: string;
  last_error: string | null;
  model_count: number;
}

interface ProvidersBody {
  providers: ProviderRow[];
  reasoning_available: boolean;
  key_source: string;
}

export function SettingsSurface() {
  const api = useStore((s) => s.api);
  const stats = useMemoryStore((s) => s.stats);
  const [providers, setProviders] = useState<ProvidersBody | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (api === null) return;
    void api
      .get<ProvidersBody>('/v1/providers')
      .then(setProviders)
      .catch((e: unknown) => setError(String(e)));
  }, [api]);

  return (
    <div className="scrollable grid-surface h-full">
      <div className="mx-auto max-w-3xl space-y-4 p-6">
        <HudPanel label="reasoning" right={<KeySourceBadge source={providers?.key_source} />}>
          {providers === null ? (
            <p className="text-xs text-fg-muted">{error ?? 'Loading…'}</p>
          ) : (
            <div className="space-y-2">
              {providers.providers.map((provider) => (
                <ProviderLine key={provider.name} provider={provider} />
              ))}
              {!providers.reasoning_available && (
                // Said plainly rather than left for the user to infer from a
                // failing message (R8).
                <p className="pt-1 text-xs text-warning">
                  No API key configured — MITTA can remember and search, but cannot
                  answer. Add one with <code className="readout">make set-key-groq</code>.
                </p>
              )}
            </div>
          )}
        </HudPanel>

        <HudPanel label="memory">
          {stats === null ? (
            <p className="text-xs text-fg-muted">Loading…</p>
          ) : (
            <MemoryPanel stats={stats} />
          )}
        </HudPanel>

        <HudPanel label="permissions">
          <div className="space-y-2 text-xs text-fg-secondary">
            <PermissionLine tier="read" behaviour="Runs, then tells you" examples="web search, open app" />
            <PermissionLine tier="write" behaviour="Asks first" examples="save a note" />
            <PermissionLine
              tier="destructive"
              behaviour="Asks first, shows the full list"
              examples="none built yet"
            />
            <HudRule className="!my-3" />
            <p className="text-2xs text-fg-faint">
              Approvals are single-use, expire in two minutes, and are bound to a hash of
              the exact arguments — approving three files cannot be replayed against three
              hundred. There is deliberately no “always allow”.
            </p>
          </div>
        </HudPanel>

        <AuditPanel />
      </div>
    </div>
  );
}

function ProviderLine({ provider }: { provider: ProviderRow }) {
  const tone =
    !provider.configured ? 'idle' : provider.state === 'healthy' ? 'ok' : provider.state === 'degraded' ? 'warn' : 'error';

  return (
    <div className="flex items-center gap-2 text-xs">
      <StatusDot tone={tone} />
      <span className="readout w-24 text-fg-primary">{provider.name}</span>
      <span className={cx('readout', provider.configured ? 'text-fg-muted' : 'text-fg-faint')}>
        {provider.configured ? provider.state : 'no key'}
      </span>
      <span className="readout text-fg-faint">{provider.model_count} models</span>
      {provider.last_error !== null && (
        <span className="truncate text-2xs text-danger">{provider.last_error}</span>
      )}
    </div>
  );
}

function KeySourceBadge({ source }: { source: string | undefined }) {
  if (source === undefined) return null;
  const label = source === 'env_file' ? '.env' : source === 'keychain' ? 'keychain' : 'none';
  return <span className="readout text-2xs text-fg-faint">key: {label}</span>;
}

function MemoryPanel({ stats }: { stats: MemoryStats }) {
  const reindex = useMemoryStore((s) => s.reindex);

  return (
    <div className="space-y-2 text-xs">
      <Row label="memories" value={`${stats.active} active · ${stats.total} total`} />
      <Row
        label="vectors"
        value={`${stats.vectors_indexed} indexed${
          stats.pending_embeddings > 0 ? ` · ${stats.pending_embeddings} pending` : ''
        }`}
      />
      <Row label="model" value={stats.embedding_model_id} />
      <Row label="index" value={stats.index_consistent ? 'consistent' : 'out of sync'} />

      {stats.embedding_degraded && (
        <p className="pt-1 text-2xs text-warning">
          Running on the fallback embedder — recall is by word overlap, not meaning.
          Run <code className="readout">make download-model</code>; every vector
          re-embeds automatically on the next start.
        </p>
      )}

      <HudRule className="!my-3" />
      <Button onClick={() => void reindex()}>Rebuild index</Button>
    </div>
  );
}

function PermissionLine({
  tier,
  behaviour,
  examples,
}: {
  tier: string;
  behaviour: string;
  examples: string;
}) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="label w-20 shrink-0">{tier}</span>
      <span className="text-fg-primary">{behaviour}</span>
      <span className="readout text-2xs text-fg-faint">{examples}</span>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="label w-20 shrink-0">{label}</span>
      <span className="readout text-fg-primary">{value}</span>
    </div>
  );
}

interface AuditEntry {
  id: string;
  at: number;
  actor: string;
  action: string;
  subject: string | null;
  verdict: string | null;
}

interface AuditBody {
  entries: AuditEntry[];
  chain_intact: boolean;
}

function AuditPanel() {
  const api = useStore((s) => s.api);
  const [body, setBody] = useState<AuditBody | null>(null);

  useEffect(() => {
    if (api === null) return;
    void api.get<AuditBody>('/v1/audit?limit=40').then(setBody).catch(() => setBody(null));
  }, [api]);

  return (
    <HudPanel
      label="activity"
      right={
        body !== null && (
          <span className="flex items-center gap-1.5">
            {/* The chain is verified on read, not asserted. A broken chain is
                shown as broken (DEC-082). */}
            <StatusDot tone={body.chain_intact ? 'ok' : 'error'} />
            <span className="readout text-2xs text-fg-faint">
              {body.chain_intact ? 'chain intact' : 'chain broken'}
            </span>
          </span>
        )
      }
    >
      {body === null || body.entries.length === 0 ? (
        <p className="text-xs text-fg-muted">Nothing yet.</p>
      ) : (
        <ul className="space-y-1">
          {body.entries.map((entry) => (
            <li key={entry.id} className="flex items-baseline gap-2 text-2xs">
              <span className="readout w-16 shrink-0 text-fg-faint">
                {new Date(entry.at * 1000).toLocaleTimeString()}
              </span>
              <span className="readout w-14 shrink-0 text-fg-muted">{entry.actor}</span>
              <span className="readout truncate text-fg-secondary">{entry.action}</span>
              {entry.subject !== null && (
                <span className="truncate text-fg-faint">{entry.subject}</span>
              )}
              {entry.verdict !== null && (
                <span
                  className={cx(
                    'readout ml-auto shrink-0',
                    entry.verdict === 'deny' ? 'text-danger' : 'text-fg-faint',
                  )}
                >
                  {entry.verdict}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </HudPanel>
  );
}
