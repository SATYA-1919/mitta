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

import type { ComponentStatus } from '@/lib/api/client';
import type { SystemMetrics } from '@/lib/ipc/tauri';
import type { ConnectionState } from '@/lib/transport/socket';

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

export interface TurnState {
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
  beginTurn: (turnId: string, conversationId: string) => void;
  setPhase: (phase: ThinkingPhase) => void;
  appendDelta: (text: string) => void;
  settleTurn: (final: string, register: Register | null) => void;
  endTurn: (status: TurnState['status'], error?: string) => void;
  clearTurn: () => void;
}

export type AppState = ConnectionSlice & MetricsSlice & UiSlice & TurnSlice;

export const useStore = create<AppState>((set) => ({
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
  beginTurn: (turnId, conversationId) =>
    set({
      activeTurn: {
        turnId,
        conversationId,
        streamed: '',
        final: null,
        register: null,
        phase: null,
        status: 'running',
        error: null,
      },
    }),
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
