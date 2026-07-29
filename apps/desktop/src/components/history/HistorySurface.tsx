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
import type { Conversation } from '@/lib/api/client';
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
  if (conversations.length === 0) {
    return <EmptyState title="No conversations yet" hint="Ask MITTA something to start one" />;
  }

  return (
    <div className="scrollable grid-surface h-full">
      <div className="mx-auto max-w-3xl space-y-2 p-6">
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
