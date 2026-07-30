import '@testing-library/dom';

/**
 * jsdom has no WebSocket, and the transport tests need the readyState constants
 * to exist as statics on the global. Providing them here rather than in each
 * test keeps the fake socket in `socket.test.ts` focused on behaviour.
 */
class MockWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
}

if (!('WebSocket' in globalThis)) {
  Object.defineProperty(globalThis, 'WebSocket', {
    value: MockWebSocket,
    writable: true,
    configurable: true,
  });
}

/**
 * This Node/jsdom combination ships no `localStorage` — it warns that
 * `--localstorage-file` was not provided and leaves the global undefined.
 *
 * The webview has it in both dev and the shell, so code that persists a UI
 * preference is correct; without a stub here those paths would be silently
 * untestable, which is how a preference that never actually saves gets shipped
 * with a passing suite. In-memory, so it is also per-run isolated.
 */
if (!('localStorage' in globalThis) || globalThis.localStorage == null) {
  const entries = new Map<string, string>();
  Object.defineProperty(globalThis, 'localStorage', {
    value: {
      getItem: (key: string) => entries.get(key) ?? null,
      setItem: (key: string, value: string) => void entries.set(key, String(value)),
      removeItem: (key: string) => void entries.delete(key),
      clear: () => entries.clear(),
      key: (index: number) => [...entries.keys()][index] ?? null,
      get length() {
        return entries.size;
      },
    } satisfies Storage,
    writable: true,
    configurable: true,
  });
}
