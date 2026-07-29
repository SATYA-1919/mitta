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
  type RuntimeInfo,
  ShellUnavailableError,
} from '@/lib/ipc/tauri';
import { TransportClient } from '@/lib/transport/socket';
import { useMemoryStore } from '@/state/memory';
import { bindTransport } from '@/state/sync';
import { useStore } from '@/state/store';

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

export async function connect(): Promise<Connection | null> {
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

  // Server-owned state gets its own store fed by the same client, rather than
  // a second client with its own token handling (DEC-018).
  useMemoryStore.getState().attach(api);
  useStore.getState().attachChat(api, transport);

  return {
    api,
    transport,
    dispose: () => {
      unbind();
      unlistenMetrics();
      useMemoryStore.getState().attach(null);
      useStore.getState().attachChat(null, null);
      transport.close();
    },
  };
}
