/**
 * The single state layer (DEC-018).
 *
 * The palette and the main window are two front-ends over *one* state model.
 * They are separate webviews and cannot share a JavaScript heap, so "one state
 * layer" is enforced two ways:
 *
 * - **Server-owned state** (conversations, memories, tasks) is never
 *   replicated. Both windows read it from the same sidecar over the same
 *   WebSocket, so there is only ever one copy and it is not in the client.
 * - **Client-owned state** (which pane is open, draft text, connection status)
 *   is replicated through the Rust core, the only component both windows can
 *   see.
 *
 * The load-bearing consequence: a command started in the palette is continuable
 * in the main window, because neither window owns the turn — the sidecar does.
 */

import { create } from 'zustand';

import type { ApiClient, ComponentStatus, Message } from '@/lib/api/client';
import type { SystemMetrics } from '@/lib/ipc/tauri';
import type { ConnectionState, TransportClient } from '@/lib/transport/socket';

export type Surface =
  | 'chat'
  | 'projects'
  | 'memory'
  | 'tasks'
  | 'plugins'
  | 'monitor'
  | 'history'
  | 'settings';

export type ThinkingPhase = 'retrieving' | 'planning' | 'reasoning' | 'executing' | 'styling';

export type Register = 'playful' | 'serious';

/** A tool call waiting on the user. Nothing has run yet. */
export interface PendingApproval {
  requestId: string;
  tool: string;
  /** The exact arguments. The token binds to these, so the user is approving
   *  these values and not a paraphrase of them. */
  params: Record<string, unknown>;
  prompt: string;
}

/** A tool call that has already happened, shown so the user sees what was done. */
export interface ToolActivity {
  tool: string;
  ok: boolean | null;
  summary: string;
}

export interface TurnState {
  approval: PendingApproval | null;
  tools: ToolActivity[];
  /** Memory ids the server reported using. Surfaced so the working set that
   *  left the machine is inspectable (R5). */
  memoryIds: string[];
  provider: string | null;
  modelId: string | null;
  turnId: string;
  conversationId: string;
  /** Streamed, pre-personality text (API_DESIGN.md §4.5). */
  streamed: string;
  /** Post-personality text; replaces `streamed` in one atomic swap. */
  final: string | null;
  register: Register | null;
  phase: ThinkingPhase | null;
  status: 'running' | 'awaiting_approval' | 'completed' | 'failed' | 'cancelled';
  error: string | null;
}

export interface ConnectionSlice {
  connection: ConnectionState;
  connectionDetail: string | null;
  ready: boolean;
  schemaVersion: number;
  components: ComponentStatus[];
  setConnection: (state: ConnectionState, detail?: string) => void;
  setReadiness: (ready: boolean, schemaVersion: number, components: ComponentStatus[]) => void;
}

export interface MetricsSlice {
  metrics: SystemMetrics | null;
  setMetrics: (metrics: SystemMetrics) => void;
}

export interface UiSlice {
  surface: Surface;
  sidebarCollapsed: boolean;
  paletteOpen: boolean;
  setSurface: (surface: Surface) => void;
  toggleSidebar: () => void;
  setPaletteOpen: (open: boolean) => void;
}

export interface TurnSlice {
  activeTurn: TurnState | null;

  // -- chat --------------------------------------------------------------- //
  api: ApiClient | null;
  transport: TransportClient | null;
  conversationId: string | null;
  messages: Message[];
  draft: string;
  /** Held between `send` and `turn.accepted`, when the local echo is rendered.
   *  The draft clears immediately so the input feels responsive. */
  pendingText: string;
  chatError: string | null;

  attachChat: (api: ApiClient | null, transport: TransportClient | null) => void;
  setDraft: (draft: string) => void;
  send: () => boolean;
  newConversation: () => void;
  openConversation: (conversationId: string) => Promise<void>;
  setTurnContext: (memoryIds: string[]) => void;
  requestApproval: (approval: PendingApproval) => void;
  resolveApproval: (approved: boolean) => void;
  toolStarted: (tool: string) => void;
  toolFinished: (tool: string, ok: boolean, summary: string) => void;
  setTurnProvenance: (provider: string | null, modelId: string | null) => void;

  beginTurn: (turnId: string, conversationId: string) => void;
  setPhase: (phase: ThinkingPhase) => void;
  appendDelta: (text: string) => void;
  settleTurn: (final: string, register: Register | null) => void;
  endTurn: (status: TurnState['status'], error?: string) => void;
  clearTurn: () => void;
}

export type AppState = ConnectionSlice & MetricsSlice & UiSlice & TurnSlice;

export const useStore = create<AppState>((set, get) => ({
  // -- connection ----------------------------------------------------------
  connection: 'idle',
  connectionDetail: null,
  ready: false,
  schemaVersion: 0,
  components: [],
  setConnection: (connection, detail) =>
    set({ connection, connectionDetail: detail ?? null, ...(connection !== 'open' && { ready: false }) }),
  setReadiness: (ready, schemaVersion, components) => set({ ready, schemaVersion, components }),

  // -- metrics -------------------------------------------------------------
  metrics: null,
  setMetrics: (metrics) => set({ metrics }),

  // -- ui ------------------------------------------------------------------
  surface: 'chat',
  sidebarCollapsed: false,
  paletteOpen: false,
  setSurface: (surface) => set({ surface }),
  toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
  setPaletteOpen: (paletteOpen) => set({ paletteOpen }),

  // -- turn ----------------------------------------------------------------
  activeTurn: null,

  api: null,
  transport: null,
  conversationId: null,
  messages: [],
  draft: '',
  pendingText: '',
  chatError: null,

  attachChat: (api, transport) => set({ api, transport }),
  setDraft: (draft) => set({ draft }),

  send: () => {
    const state = get();
    const text = state.draft.trim();
    if (text.length === 0 || state.transport === null) return false;
    // One turn at a time. The sidecar would accept a second, but two streams
    // into one buffer would interleave into nonsense.
    if (state.activeTurn !== null && state.activeTurn.status === 'running') return false;

    const sent = state.transport.send('turn.start', {
      text,
      ...(state.conversationId === null ? {} : { conversation_id: state.conversationId }),
    });
    if (!sent) {
      set({ chatError: 'Not connected to MITTA.' });
      return false;
    }
    set({ draft: '', pendingText: text, chatError: null });
    return true;
  },

  newConversation: () =>
    // No server round-trip: the orchestrator creates a conversation when a turn
    // arrives without one. Creating it here would leave an empty thread behind
    // every time someone clicked "new" and changed their mind.
    set({ conversationId: null, messages: [], activeTurn: null, chatError: null }),

  openConversation: async (conversationId) => {
    const { api } = get();
    if (api === null) return;
    set({ conversationId, activeTurn: null, chatError: null });
    try {
      const body = await api.conversationMessages(conversationId);
      set({ messages: body.messages });
    } catch (error) {
      set({ chatError: String(error) });
    }
  },

  setTurnContext: (memoryIds) =>
    set((s) => (s.activeTurn === null ? s : { activeTurn: { ...s.activeTurn, memoryIds } })),

  requestApproval: (approval) =>
    set((s) => (s.activeTurn === null ? s : { activeTurn: { ...s.activeTurn, approval } })),

  resolveApproval: (approved) => {
    const state = get();
    const approval = state.activeTurn?.approval;
    if (approval === undefined || approval === null || state.transport === null) return;

    // The request id is all that goes back. The parameters stay on the server,
    // which mints the token from what it recorded when it raised the prompt —
    // a client returning altered arguments would get a token that fails
    // verification against the ones actually used.
    state.transport.send(approved ? 'turn.approve' : 'turn.deny', {
      request_id: approval.requestId,
    });
    set((s) => (s.activeTurn === null ? s : { activeTurn: { ...s.activeTurn, approval: null } }));
  },

  toolStarted: (tool) =>
    set((s) =>
      s.activeTurn === null
        ? s
        : {
            activeTurn: {
              ...s.activeTurn,
              tools: [...s.activeTurn.tools, { tool, ok: null, summary: '' }],
            },
          },
    ),

  toolFinished: (tool, ok, summary) =>
    set((s) => {
      if (s.activeTurn === null) return s;
      const tools = [...s.activeTurn.tools];
      // Update the last matching entry rather than appending: a tool can be
      // called twice in a turn, and two rows for one call would read as two
      // actions having been taken.
      for (let i = tools.length - 1; i >= 0; i -= 1) {
        if (tools[i]?.tool === tool && tools[i]?.ok === null) {
          tools[i] = { tool, ok, summary };
          break;
        }
      }
      return { activeTurn: { ...s.activeTurn, tools } };
    }),

  setTurnProvenance: (provider, modelId) =>
    set((s) =>
      s.activeTurn === null ? s : { activeTurn: { ...s.activeTurn, provider, modelId } },
    ),

  beginTurn: (turnId, conversationId) =>
    set((s) => ({
      conversationId,
      pendingText: '',
      // The user's message is echoed locally rather than waiting for a reload.
      // Seeing your own sentence appear instantly is most of what makes a chat
      // feel responsive; the persisted row replaces it on the next load.
      messages:
        s.pendingText.length > 0
          ? [...s.messages, localUserMessage(s.pendingText, turnId, conversationId)]
          : s.messages,
      activeTurn: {
        turnId,
        conversationId,
        streamed: '',
        final: null,
        register: null,
        phase: null,
        status: 'running',
        error: null,
        memoryIds: [],
        provider: null,
        modelId: null,
        approval: null,
        tools: [],
      },
    })),
  setPhase: (phase) =>
    set((s) => (s.activeTurn === null ? s : { activeTurn: { ...s.activeTurn, phase } })),
  appendDelta: (text) =>
    set((s) =>
      s.activeTurn === null
        ? s
        : { activeTurn: { ...s.activeTurn, streamed: s.activeTurn.streamed + text } },
    ),
  /**
   * The atomic swap from DEC-027. `final` replaces the streamed buffer in one
   * step rather than re-rendering token by token; the UI crossfades between
   * them. When the rewrite was a no-op the server sends `styled: false` and the
   * caller passes the streamed text back unchanged, so no visible swap occurs.
   */
  settleTurn: (final, register) =>
    set((s) => (s.activeTurn === null ? s : { activeTurn: { ...s.activeTurn, final, register } })),
  endTurn: (status, error) =>
    set((s) =>
      s.activeTurn === null
        ? s
        : { activeTurn: { ...s.activeTurn, status, phase: null, error: error ?? null } },
    ),
  clearTurn: () => set({ activeTurn: null }),
}));

/** The text to render: settled output if it has arrived, otherwise the stream. */
export function displayText(turn: TurnState | null): string {
  if (turn === null) return '';
  return turn.final ?? turn.streamed;
}

/**
 * The user's message, rendered before the server has confirmed it.
 *
 * Given a `local_` id so it is distinguishable from a persisted row — on reload
 * the real one arrives with a `msg_` id and full provenance.
 */
function localUserMessage(content: string, turnId: string, conversationId: string): Message {
  return {
    id: `local_${turnId}`,
    conversation_id: conversationId,
    turn_id: turnId,
    role: 'user',
    content,
    content_raw: null,
    model_id: null,
    provider: null,
    register: null,
    styled: false,
    latency_ms: null,
    created_at: Math.floor(Date.now() / 1000),
  };
}
