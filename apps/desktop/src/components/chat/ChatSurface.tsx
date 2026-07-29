/**
 * Chat — the surface everything else has been building toward.
 *
 * Shows what MITTA is doing while it does it: which phase, how many memories it
 * pulled, who answered. That is not decoration. A memory system that recalls
 * invisibly is a memory system the user cannot correct, and R5 makes
 * inspectability the condition of trust.
 */

import { useEffect, useRef } from 'react';

import { ActivityRing, HudPanel, HudRule } from '@/components/ui/hud';
import { Button, cx, Kbd, StatusDot } from '@/components/ui/primitives';
import type { Message } from '@/lib/api/client';
import { useMemoryStore } from '@/state/memory';
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
      <div className="scrollable grid-surface min-h-0 flex-1">
        {empty ? (
          <StandbyPanel />
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

/**
 * The idle view.
 *
 * An empty chat is the state the user sees most often before typing, so it is
 * the one that decides whether the app reads as an instrument. Two sentences on
 * a grid read as a placeholder; a standby readout reads as something running
 * and waiting.
 *
 * Every value here is real. A fake telemetry panel would look the part and be a
 * lie on the first surface the user meets.
 */
function StandbyPanel() {
  const stats = useMemoryStore((s) => s.stats);
  const connection = useStore((s) => s.connection);
  const detail = useStore((s) => s.connectionDetail);
  const online = connection === 'open';

  return (
    <div className="flex h-full items-center justify-center p-8">
      <HudPanel
        label="standby"
        active={online}
        className="w-full max-w-md"
        right={<ActivityRing active={online} size={14} />}
      >
        <div className="space-y-3">
          <div>
            <p className="text-sm text-fg-primary">Ask MITTA anything</p>
            <p className="mt-0.5 text-2xs text-fg-muted">
              It remembers what you tell it, and shows you what it used
            </p>
          </div>

          <HudRule />

          <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-2xs">
            <Stat label="link" value={connection} tone={online ? 'ok' : 'error'} />
            <Stat
              label="memory"
              value={stats === null ? '—' : `${stats.active} stored`}
              tone={stats === null ? 'idle' : 'ok'}
            />
            <Stat
              label="index"
              value={stats === null ? '—' : `${stats.vectors_indexed} vectors`}
              tone={stats?.index_consistent === false ? 'warn' : 'ok'}
            />
            <Stat
              label="recall"
              value={stats === null ? '—' : stats.embedding_degraded ? 'degraded' : 'semantic'}
              tone={stats?.embedding_degraded === true ? 'warn' : 'ok'}
            />
          </dl>

          {/* The reason, not just the state. "Disconnected" alone sent
              debugging to the wrong place twice. */}
          {!online && detail !== null && (
            <p className="border-l-2 border-danger/60 pl-2 text-2xs text-danger">{detail}</p>
          )}
        </div>
      </HudPanel>
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: 'ok' | 'warn' | 'error' | 'idle';
}) {
  return (
    <div className="flex items-center gap-1.5">
      <StatusDot tone={tone} />
      <dt className="label !text-[0.58rem]">{label}</dt>
      <dd className="readout ml-auto text-fg-secondary">{value}</dd>
    </div>
  );
}

function MessageRow({ message }: { message: Message }) {
  const isUser = message.role === 'user';
  return (
    <div className={cx('flex flex-col gap-1', isUser && 'items-end')}>
      <div
        className={cx(
          'selectable max-w-[85%] whitespace-pre-wrap rounded-xs px-3.5 py-2.5 text-sm leading-relaxed',
          isUser
            ? // The user's own words sit against a left rule rather than in a
              // bubble. Chat bubbles are a messaging idiom; this is a console.
              'border-l-2 border-accent/60 bg-surface-active/40 text-fg-primary'
            : 'border-l-2 border-border-default bg-surface-raised/60 text-fg-primary',
        )}
      >
        {message.content}
      </div>
      {!isUser && message.provider !== null && (
        <div className="flex items-center gap-2 pl-3.5 text-2xs text-fg-faint">
          {/* Who answered. R3 requires the active provider be visible so a reply
              that feels different has a reason rather than seeming arbitrary. */}
          <span className="readout">{message.model_id ?? message.provider}</span>
          {message.latency_ms !== null && (
            <span className="readout">{message.latency_ms}ms</span>
          )}
          {message.register !== null && (
            <span className="readout text-accent-muted">{message.register}</span>
          )}
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
        <div className="flex items-center gap-3 border-l-2 border-accent/30 pl-3.5 text-2xs">
          {turn.phase !== null && (
            <span className="flex items-center gap-1.5 text-accent">
              <ActivityRing active size={12} />
              <span className="label !text-accent">{PHASE_LABEL[turn.phase] ?? turn.phase}</span>
            </span>
          )}
          {turn.memoryIds.length > 0 && (
            // Surfaced, not hidden: this is the working set that left the
            // machine on the user's behalf (R5).
            <span className="readout text-fg-muted">
              MEM {turn.memoryIds.length}
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
              <span className="readout text-accent-muted">{activity.tool}</span>
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
        <div className="selectable max-w-[85%] whitespace-pre-wrap rounded-xs border-l-2 border-border-default bg-surface-raised/60 px-3.5 py-2.5 text-sm leading-relaxed text-fg-primary">
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
    <HudPanel
      label="permission required"
      className="max-w-[85%] !border-warning/60 shadow-[0_0_14px_-3px_var(--color-warning)]"
      right={<StatusDot tone="warn" pulse />}
    >
      <p className="text-sm leading-relaxed text-fg-primary">{approval.prompt}</p>

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
    </HudPanel>
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
            'flex-1 resize-none rounded-xs border border-border-subtle bg-surface-input',
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
