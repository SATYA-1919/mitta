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
