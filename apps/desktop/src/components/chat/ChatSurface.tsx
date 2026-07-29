/**
 * Chat — the surface everything else has been building toward.
 *
 * Shows what MITTA is doing while it does it: which phase, how many memories it
 * pulled, who answered. That is not decoration. A memory system that recalls
 * invisibly is a memory system the user cannot correct, and R5 makes
 * inspectability the condition of trust.
 */

import { useEffect, useRef } from 'react';

import { Button, cx, EmptyState, Kbd, StatusDot } from '@/components/ui/primitives';
import type { Message } from '@/lib/api/client';
import { displayText, type PendingApproval, useStore } from '@/state/store';

export function ChatSurface() {
  const { messages, activeTurn, draft, chatError, connection } = useStore();
  const bottom = useRef<HTMLDivElement>(null);

  const streaming = displayText(activeTurn);

  useEffect(() => {
    // `auto`, not `smooth`: a smooth scroll re-triggered on every token never
    // finishes, so the view lags permanently behind the text.
    bottom.current?.scrollIntoView({ behavior: 'auto' });
  }, [messages.length, streaming]);

  const empty = messages.length === 0 && activeTurn === null;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="scrollable min-h-0 flex-1">
        {empty ? (
          <EmptyState
            title="Ask MITTA anything"
            hint="It remembers what you tell it, and shows you what it used"
          />
        ) : (
          <div className="mx-auto max-w-3xl space-y-5 p-6">
            {messages.map((message) => (
              <MessageRow key={message.id} message={message} />
            ))}
            {activeTurn !== null && <ActiveTurnRow />}
            <div ref={bottom} />
          </div>
        )}
      </div>

      {chatError !== null && (
        <p className="shrink-0 px-6 pb-2 text-xs text-danger">{chatError}</p>
      )}

      <Composer disabled={connection !== 'open'} draftLength={draft.length} />
    </div>
  );
}

function MessageRow({ message }: { message: Message }) {
  const isUser = message.role === 'user';
  return (
    <div className={cx('flex flex-col gap-1', isUser && 'items-end')}>
      <div
        className={cx(
          'selectable max-w-[85%] whitespace-pre-wrap rounded-lg px-3.5 py-2.5 text-sm leading-relaxed',
          isUser
            ? 'bg-surface-active text-fg-primary'
            : 'bg-surface-raised text-fg-primary',
        )}
      >
        {message.content}
      </div>
      {!isUser && message.provider !== null && (
        <div className="flex items-center gap-2 text-2xs text-fg-faint">
          {/* Who answered. R3 requires the active provider be visible so a reply
              that feels different has a reason rather than seeming arbitrary. */}
          <span className="font-mono">{message.model_id ?? message.provider}</span>
          {message.latency_ms !== null && <span>{message.latency_ms} ms</span>}
        </div>
      )}
    </div>
  );
}

const PHASE_LABEL: Record<string, string> = {
  recalling: 'searching memory',
  reasoning: 'thinking',
};

function ActiveTurnRow() {
  const turn = useStore((s) => s.activeTurn);
  if (turn === null) return null;

  const text = displayText(turn);

  return (
    <div className="flex flex-col gap-1.5">
      {(turn.phase !== null || turn.memoryIds.length > 0) && (
        <div className="flex items-center gap-2 text-2xs text-fg-muted">
          {turn.phase !== null && (
            <>
              <StatusDot tone="warn" pulse />
              <span>{PHASE_LABEL[turn.phase] ?? turn.phase}</span>
            </>
          )}
          {turn.memoryIds.length > 0 && (
            // Surfaced, not hidden: this is the working set that left the
            // machine on the user's behalf (R5).
            <span className="text-fg-faint">
              {turn.memoryIds.length}{' '}
              {turn.memoryIds.length === 1 ? 'memory' : 'memories'} used
            </span>
          )}
        </div>
      )}

      {turn.tools.length > 0 && (
        <div className="flex flex-col gap-1">
          {turn.tools.map((activity, index) => (
            <div
              key={`${activity.tool}-${index}`}
              className="flex items-center gap-2 text-2xs text-fg-muted"
            >
              <StatusDot
                tone={activity.ok === null ? 'warn' : activity.ok ? 'ok' : 'error'}
                pulse={activity.ok === null}
              />
              <span className="font-mono">{activity.tool}</span>
              {/* What it actually did. R5 and DEC-081: read-only tools never
                  prompt, so this line is the entire "told you" half. */}
              {activity.summary !== '' && (
                <span className="truncate text-fg-faint">{activity.summary}</span>
              )}
            </div>
          ))}
        </div>
      )}

      {turn.approval !== null && <ApprovalCard approval={turn.approval} />}

      {text.length > 0 && (
        <div className="selectable max-w-[85%] whitespace-pre-wrap rounded-lg bg-surface-raised px-3.5 py-2.5 text-sm leading-relaxed text-fg-primary">
          {text}
          {turn.status === 'running' && <span className="ml-0.5 animate-pulse text-accent">▍</span>}
        </div>
      )}

      {turn.error !== null && (
        <div className="max-w-[85%] rounded-lg bg-danger-surface px-3.5 py-2.5 text-sm text-danger">
          {turn.error}
        </div>
      )}
    </div>
  );
}

function ApprovalCard({ approval }: { approval: PendingApproval }) {
  const resolve = useStore((s) => s.resolveApproval);
  const deny = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    // Focus lands on Deny, not Approve. A stray Enter or Space while a prompt
    // appears should not authorise something — the safe action is the one that
    // costs a click to undo.
    deny.current?.focus();
  }, []);

  return (
    <div
      role="alertdialog"
      aria-label="MITTA needs permission"
      className="max-w-[85%] rounded-lg border border-warning/40 bg-surface-raised p-3.5"
    >
      <div className="flex items-center gap-2 text-2xs text-warning">
        <StatusDot tone="warn" pulse />
        <span>MITTA needs your permission</span>
      </div>

      <p className="mt-2 text-sm leading-relaxed text-fg-primary">{approval.prompt}</p>

      {/* The exact arguments, not a summary. The approval token binds to a hash
          of these values, so what is shown is what can run — approving "3 files"
          cannot be replayed against 300 (DEC-080). */}
      <pre className="scrollable selectable mt-2 max-h-40 rounded-md bg-surface-input p-2 font-mono text-2xs text-fg-secondary">
        {approval.tool}({JSON.stringify(approval.params, null, 2)})
      </pre>

      <div className="mt-3 flex items-center gap-2">
        <Button ref={deny} onClick={() => resolve(false)}>
          Deny
        </Button>
        <Button variant="primary" onClick={() => resolve(true)}>
          Approve once
        </Button>
        {/* Deliberately no "always allow". A remembered blanket approval is an
            approval that is not bound to parameters, which is the property the
            whole token design exists to keep. */}
      </div>
    </div>
  );
}

function Composer({ disabled, draftLength }: { disabled: boolean; draftLength: number }) {
  const setDraft = useStore((s) => s.setDraft);
  const send = useStore((s) => s.send);
  const draft = useStore((s) => s.draft);
  const activeTurn = useStore((s) => s.activeTurn);
  const input = useRef<HTMLTextAreaElement>(null);

  const running = activeTurn !== null && activeTurn.status === 'running';
  const blocked = disabled || running;

  useEffect(() => {
    // Grow with the content rather than scrolling inside a fixed box — a
    // three-line question should be readable while it is being written.
    const el = input.current;
    if (el === null) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [draft]);

  return (
    <div className="shrink-0 border-t border-border-subtle p-4">
      <div className="mx-auto flex max-w-3xl items-end gap-2">
        <textarea
          ref={input}
          rows={1}
          value={draft}
          disabled={disabled}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            // Enter sends; Shift+Enter is a newline. The opposite would make
            // every multi-line question an accidental send.
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault();
              send();
            }
          }}
          placeholder={disabled ? 'Not connected to MITTA' : 'Ask MITTA…'}
          aria-label="Message"
          className={cx(
            'flex-1 resize-none rounded-lg border border-border-subtle bg-surface-input',
            'px-3.5 py-2.5 text-sm leading-relaxed text-fg-primary',
            'placeholder:text-fg-faint focus-visible:border-accent focus-visible:outline-none',
            'disabled:opacity-50',
          )}
        />
        <Button
          variant="primary"
          disabled={blocked || draftLength === 0}
          onClick={() => send()}
          className="h-[42px]"
        >
          {running ? 'Working…' : 'Send'}
        </Button>
      </div>
      <p className="mx-auto mt-1.5 max-w-3xl text-2xs text-fg-faint">
        <Kbd>↵</Kbd> send · <Kbd>⇧↵</Kbd> newline
      </p>
    </div>
  );
}
