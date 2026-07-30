/**
 * Conversation history.
 *
 * Opening a thread loads its transcript and switches to Chat — the thread is
 * the thing, and a separate reading view would be a second place for the same
 * content to render slightly differently.
 */

import { useEffect, useState } from 'react';

import { HudPanel } from '@/components/ui/hud';
import { Button, EmptyState, StatusDot } from '@/components/ui/primitives';
import type { Conversation, HistoryRange } from '@/lib/api/client';
import { useStore } from '@/state/store';

export function HistorySurface() {
  const api = useStore((s) => s.api);
  const openConversation = useStore((s) => s.openConversation);
  const setSurface = useStore((s) => s.setSurface);
  const currentId = useStore((s) => s.conversationId);

  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(true);

  async function refresh(): Promise<void> {
    if (api === null) return;
    setLoading(true);
    try {
      const body = await api.listConversations(100);
      setConversations(body.conversations);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, [api]);

  if (loading && conversations.length === 0) {
    return <EmptyState title="Loading…" />;
  }

  return (
    <div className="scrollable grid-surface h-full">
      <div className="mx-auto max-w-3xl space-y-2 p-6">
        <ClearHistoryBar onCleared={() => void refresh()} />

        {conversations.length === 0 && (
          <EmptyState title="No conversations yet" hint="Ask MITTA something to start one" />
        )}

        {conversations.map((conversation) => (
          <HudPanel
            key={conversation.id}
            active={conversation.id === currentId}
            className="transition-colors hover:border-accent/50"
          >
            <button
              type="button"
              className="w-full text-left"
              onClick={() => {
                void openConversation(conversation.id);
                setSurface('chat');
              }}
            >
              <div className="flex items-baseline gap-2">
                <span className="truncate text-sm text-fg-primary">
                  {conversation.title ?? 'Untitled'}
                </span>
                {conversation.pinned && <StatusDot tone="ok" />}
                <span className="readout ml-auto shrink-0 text-2xs text-fg-faint">
                  {conversation.message_count} msg
                </span>
              </div>
              <div className="readout mt-1 text-2xs text-fg-faint">
                {new Date(conversation.updated_at * 1000).toLocaleString()}
              </div>
            </button>

            <div className="mt-2">
              <Button
                variant="danger"
                onClick={() => {
                  void api?.deleteConversation(conversation.id).then(refresh);
                }}
              >
                Delete
              </Button>
            </div>
          </HudPanel>
        ))}
      </div>
    </div>
  );
}

const RANGES: { value: HistoryRange; label: string }[] = [
  { value: 'today', label: 'Today' },
  { value: 'week', label: 'This week' },
  { value: 'month', label: 'This month' },
  { value: 'year', label: 'This year' },
  { value: 'all', label: 'Everything' },
];

/**
 * Clear history by period.
 *
 * Two-step, like purging a memory (DEC-053), because this is the most
 * destructive button in the application — "Everything" ends every transcript
 * MITTA has.
 *
 * The confirmation asks with a **count**, not the word that was pressed. "Delete
 * 34 conversations?" is a question a person can answer; "Clear this month?" is
 * one they can only shrug at, because the whole problem is not knowing how much
 * that is. The count comes from the server, computed with the same cutoff the
 * delete will use, so the number cannot disagree with the outcome.
 */
function ClearHistoryBar({ onCleared }: { onCleared: () => void }) {
  const api = useStore((s) => s.api);
  const [pending, setPending] = useState<{ range: HistoryRange; count: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  async function ask(range: HistoryRange): Promise<void> {
    if (api === null) return;
    setNote(null);
    try {
      const { count } = await api.historyCount(range);
      if (count === 0) {
        // Nothing to confirm. A confirmation dialog for a no-op teaches people
        // to click through confirmations.
        setNote('Nothing in that period.');
        return;
      }
      setPending({ range, count });
    } catch {
      setNote('Could not read that period.');
    }
  }

  async function confirm(): Promise<void> {
    if (api === null || pending === null) return;
    setBusy(true);
    try {
      const result = await api.clearHistory(pending.range);
      setNote(`Deleted ${result.deleted} conversation${result.deleted === 1 ? '' : 's'}.`);
      setPending(null);
      onCleared();
    } catch {
      setNote('Could not clear that period.');
    } finally {
      setBusy(false);
    }
  }

  const label = RANGES.find((r) => r.value === pending?.range)?.label.toLowerCase();

  return (
    <HudPanel label="CLEAR HISTORY" className="mb-4">
      {pending === null ? (
        <div className="flex flex-wrap items-center gap-1.5">
          {RANGES.map((range) => (
            <Button
              key={range.value}
              variant={range.value === 'all' ? 'danger' : 'ghost'}
              onClick={() => void ask(range.value)}
            >
              {range.label}
            </Button>
          ))}
          {note !== null && <span className="ml-1 text-2xs text-fg-muted">{note}</span>}
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-fg-primary">
            Delete {pending.count} conversation{pending.count === 1 ? '' : 's'} from {label}?
            <span className="ml-1 text-fg-muted">This cannot be undone.</span>
          </span>
          <span className="flex-1" />
          <Button variant="danger" onClick={() => void confirm()} disabled={busy}>
            {busy ? 'Deleting…' : 'Delete'}
          </Button>
          <Button onClick={() => setPending(null)} disabled={busy}>
            Cancel
          </Button>
        </div>
      )}
    </HudPanel>
  );
}
