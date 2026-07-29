/**
 * Binds the transport to the store.
 *
 * This is the only place a wire frame becomes application state. Keeping the
 * mapping in one function means the frame vocabulary can change without
 * touching a component, and a component can never subscribe to the socket
 * directly and start a second, divergent copy of the same state.
 */

import type { Envelope } from '@/lib/transport/envelope';
import type { TransportClient } from '@/lib/transport/socket';

import type { AppState, Register, ThinkingPhase } from './store';
import { useStore } from './store';

interface TurnAcceptedData {
  turn_id: string;
  conversation_id: string;
}
interface ThinkingData {
  phase: ThinkingPhase;
}
interface DeltaData {
  text: string;
}
interface MessageData {
  content: string;
  styled: boolean;
  register: Register | null;
  provider?: string | null;
  model_id?: string | null;
}
interface ContextData {
  memory_ids: string[];
}
interface ToolData {
  tool?: string;
  ok?: boolean;
  summary?: string;
  reason?: string;
}
interface ApprovalData {
  request_id: string;
  tool: string;
  params?: Record<string, unknown>;
  prompt: string;
}
interface ErrorData {
  code: string;
  message: string;
}

/**
 * Wire a client to the store. Returns a teardown function.
 *
 * `getState` is injected so this is testable without React and without a
 * module-level singleton.
 */
export function bindTransport(
  client: TransportClient,
  getState: () => AppState = useStore.getState,
): () => void {
  const unbindState = client.onStateChange((state, detail) => {
    getState().setConnection(state, detail);
  });

  const unbindFrames = client.onFrame((frame: Envelope) => {
    const state = getState();
    switch (frame.type) {
      case 'turn.accepted': {
        const data = frame.data as TurnAcceptedData;
        state.beginTurn(data.turn_id, data.conversation_id);
        break;
      }
      case 'turn.thinking': {
        state.setPhase((frame.data as ThinkingData).phase);
        break;
      }
      case 'turn.context': {
        // The working set that left the machine on the user's behalf. Shown in
        // the UI rather than only logged — R5's enforcement clause.
        state.setTurnContext((frame.data as ContextData).memory_ids ?? []);
        break;
      }
      case 'turn.tool_started': {
        state.toolStarted(String((frame.data as ToolData).tool ?? ''));
        break;
      }
      case 'turn.tool_finished': {
        const data = frame.data as ToolData;
        state.toolFinished(String(data.tool ?? ''), data.ok === true, String(data.summary ?? ''));
        break;
      }
      case 'turn.tool_denied': {
        const data = frame.data as ToolData;
        state.toolFinished(String(data.tool ?? ''), false, String(data.reason ?? 'denied'));
        break;
      }
      case 'turn.approval_required': {
        // The turn is now stopped on the server, waiting. Nothing has run.
        const data = frame.data as ApprovalData;
        state.requestApproval({
          requestId: data.request_id,
          tool: data.tool,
          params: data.params ?? {},
          prompt: data.prompt,
        });
        break;
      }
      case 'turn.delta': {
        state.appendDelta((frame.data as DeltaData).text);
        break;
      }
      case 'turn.message': {
        // DEC-027: the settled, post-personality text replaces the streamed
        // buffer in one swap. `styled: false` means the rewrite was a no-op, so
        // there is nothing to swap and the stream is already correct.
        const data = frame.data as MessageData;
        state.setTurnProvenance(data.provider ?? null, data.model_id ?? null);
        if (data.styled) {
          state.settleTurn(data.content, data.register);
        } else {
          state.settleTurn(state.activeTurn?.streamed ?? data.content, data.register);
        }
        break;
      }
      case 'turn.error': {
        state.endTurn('failed', (frame.data as ErrorData).message);
        break;
      }
      case 'turn.done': {
        state.endTurn('completed');
        break;
      }
      default:
        // Unknown frame types are ignored rather than thrown on. A newer
        // sidecar emitting a type this build does not know about must not break
        // the window; the api_version handshake is what catches real skew.
        break;
    }
  });

  return () => {
    unbindState();
    unbindFrames();
  };
}
