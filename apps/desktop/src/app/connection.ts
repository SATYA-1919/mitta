/**
 * Connection bootstrap for the main window.
 *
 * Deliberately tolerant of the shell being absent: Phase 4b builds the Rust
 * side, and until then the frontend runs in a browser against a manually
 * started sidecar. Failure to connect leaves the UI in a visible "disconnected"
 * state rather than throwing — a shell that renders and reports no backend is
 * far more useful than a blank window with an error in the console.
 */

import { ApiClient } from '@/lib/api/client';
import {
  getRuntimeInfo,
  isTauriAvailable,
  onMetrics,
  onPushToTalk,
  onVoiceUpdate,
  type RuntimeInfo,
  ShellUnavailableError,
} from '@/lib/ipc/tauri';
import { TransportClient } from '@/lib/transport/socket';
import { useMemoryStore } from '@/state/memory';
import { useProjectsStore } from '@/state/projects';
import { bindTransport } from '@/state/sync';
import { useStore } from '@/state/store';
import { useVoiceStore } from '@/state/voice';

export interface Connection {
  api: ApiClient;
  transport: TransportClient;
  dispose: () => void;
}

/**
 * Render whatever `invoke` rejected with.
 *
 * Tauri rejects with the *serialised* error, so `ShellError` arrives as
 * `{code, message, retryable}` — and `String(...)` on that yields
 * `[object Object]`, which is what reached the screen and cost a round-trip to
 * diagnose. The whole point of the detail line is that it says something.
 */
function describe(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === 'string') return error;
  if (typeof error === 'object' && error !== null) {
    const shaped = error as { message?: unknown; code?: unknown };
    if (typeof shaped.message === 'string') {
      return typeof shaped.code === 'string'
        ? `${shaped.message} (${shaped.code})`
        : shaped.message;
    }
    try {
      return JSON.stringify(error);
    } catch {
      return 'unknown error';
    }
  }
  return String(error);
}

/** How long to keep waiting for the sidecar before reporting failure. */
const READY_TIMEOUT_MS = 45_000;
const RETRY_INTERVAL_MS = 400;

/**
 * Wait for the sidecar, rather than asking once and giving up.
 *
 * The shell spawns the sidecar and the webview loads in parallel, and the
 * sidecar takes seconds to be ready — migrations, the FAISS index, the
 * embedding model. Asking once at mount reliably arrives first and gets
 * `sidecar.unavailable`, after which nothing ever tries again. The window then
 * sits on "not connected" beside a backend that came up fine moments later.
 *
 * Polling rather than waiting on the `sidecar:state` event because this must
 * also work when the event was emitted *before* the webview subscribed, which
 * is the common case for a fast start.
 */
async function awaitRuntime(): Promise<RuntimeInfo> {
  const deadline = Date.now() + READY_TIMEOUT_MS;

  for (;;) {
    try {
      return await getRuntimeInfo();
    } catch (error) {
      // A missing shell will never resolve by waiting.
      if (error instanceof ShellUnavailableError) throw error;
      if (Date.now() >= deadline) throw error;
      useStore.getState().setConnection('connecting', 'waiting for the MITTA backend…');
      await new Promise((resolve) => setTimeout(resolve, RETRY_INTERVAL_MS));
    }
  }
}

/**
 * The one connection this window has, memoised.
 *
 * The connection is owned by the *window*, not by a component. It was
 * previously established inside a React effect that disposed on cleanup, and
 * under StrictMode — which `make app` runs, because the shell loads the dev
 * server (DEC-089) — that mounted twice and opened two of everything.
 *
 * The two racing bootstraps then fought over one store slot. Whichever
 * finished second called `attachChat`, and whichever was cancelled called
 * `attachChat(null, null)` on its way out, so the surviving socket's transport
 * could be erased from the store *after* being installed. Ordering was not
 * incidental either: the first bootstrap pays for `import('@tauri-apps/api')`
 * and the second gets it from cache, so the second regularly won.
 *
 * The result was a window that showed `open`, held a live socket, and had a
 * Send button that did nothing at all — `send()` found a null transport and
 * returned false without a word.
 *
 * Memoising makes the second call return the first's promise instead of
 * building a rival connection.
 */
let established: Promise<Connection | null> | null = null;

export function connect(): Promise<Connection | null> {
  established ??= establish();
  return established;
}

async function establish(): Promise<Connection | null> {
  const store = useStore.getState();

  let runtime: RuntimeInfo;
  try {
    runtime = await awaitRuntime();
  } catch (error) {
    // Distinguish the reasons, because they need different fixes and
    // "Disconnected" sent debugging to the wrong place three times.
    const detail = isTauriAvailable()
      ? `Shell present, IPC failed: ${describe(error)}`
      : error instanceof ShellUnavailableError
        ? 'No shell and no VITE_MITTA_* — use `make app` for the window, or `make dev` for the browser'
        : describe(error);
    store.setConnection('closed', detail);
    // Cleared so a later caller can try again. A cached rejection would make
    // the failure permanent for the lifetime of the window.
    established = null;
    return null;
  }

  const api = new ApiClient({ baseUrl: runtime.baseUrl, token: runtime.token });
  const transport = new TransportClient({
    url: runtime.wsUrl,
    token: runtime.token,
    channels: ['turn', 'task', 'notification', 'provider'],
  });

  const unbind = bindTransport(transport);
  transport.connect();

  // Readiness comes over HTTP, not the socket: `ready` reflects whether the
  // sidecar can serve a turn, which is a different question from whether the
  // socket is open.
  void api
    .status()
    .then((status) => {
      store.setReadiness(status.ready, status.schema_version, status.components);
    })
    .catch(() => {
      store.setReadiness(false, 0, []);
    });

  const unlistenMetrics = await onMetrics((metrics) => {
    useStore.getState().setMetrics(metrics);
  });

  // Voice (R7). A finished utterance becomes a turn through exactly the same
  // path as typing — `setDraft` then `send` — so a spoken request and a typed
  // one cannot diverge in behaviour.
  const unlistenVoice = await onVoiceUpdate((update) => {
    useVoiceStore.getState().apply(update);
  });
  useVoiceStore.getState().bind((text) => {
    const store = useStore.getState();
    store.setDraft(text);
    store.send();
  });
  // Wake mode is a standing choice, not a per-session one. Restored after the
  // handler is bound, so a wake word heard immediately has somewhere to land.
  void useVoiceStore.getState().restoreWakePreference();

  // ⌘⇧V from anywhere, not just while the window has focus. Bound here rather
  // than in the voice bar because the shortcut is global and the voice bar
  // unmounts with its surface — a hold-to-talk key that stops working when you
  // switch to Memory is worse than one that never existed.
  const unlistenPushToTalk = await onPushToTalk((down) => {
    void useVoiceStore.getState().pushToTalk(down);
  });

  // Server-owned state gets its own store fed by the same client, rather than
  // a second client with its own token handling (DEC-018).
  useMemoryStore.getState().attach(api);
  useProjectsStore.getState().attach(api);
  useStore.getState().attachChat(api, transport);

  const connection: Connection = {
    api,
    transport,
    dispose: () => {
      unbind();
      unlistenMetrics();
      unlistenVoice();
      unlistenPushToTalk();
      useVoiceStore.getState().bind(null);
      transport.close();
      // Only detach what is still attached. Clearing unconditionally is how a
      // disposed connection used to erase a live one's transport.
      if (useStore.getState().transport === transport) {
        useMemoryStore.getState().attach(null);
        useProjectsStore.getState().attach(null);
        useStore.getState().attachChat(null, null);
      }
      if (established !== null) {
        void established.then((held) => {
          if (held === connection) established = null;
        });
      }
    },
  };
  return connection;
}
