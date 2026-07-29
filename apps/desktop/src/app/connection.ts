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
import { getRuntimeInfo, onMetrics, ShellUnavailableError } from '@/lib/ipc/tauri';
import { TransportClient } from '@/lib/transport/socket';
import { bindTransport } from '@/state/sync';
import { useStore } from '@/state/store';

export interface Connection {
  api: ApiClient;
  transport: TransportClient;
  dispose: () => void;
}

export async function connect(): Promise<Connection | null> {
  const store = useStore.getState();

  let runtime;
  try {
    runtime = await getRuntimeInfo();
  } catch (error) {
    const detail =
      error instanceof ShellUnavailableError
        ? 'Tauri shell not available (Phase 4b)'
        : String(error);
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

  return {
    api,
    transport,
    dispose: () => {
      unbind();
      unlistenMetrics();
      transport.close();
    },
  };
}
