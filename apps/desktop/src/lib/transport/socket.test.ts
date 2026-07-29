import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  CLOSE_FORBIDDEN_ORIGIN,
  CLOSE_UNAUTHORISED,
  type ConnectionState,
  TransportClient,
} from './socket';

/** Minimal fake standing in for a real WebSocket, driven by the test. */
class FakeSocket {
  static instances: FakeSocket[] = [];

  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(
    readonly url: string,
    readonly protocols: string[],
  ) {
    FakeSocket.instances.push(this);
  }

  send(payload: string): void {
    this.sent.push(payload);
  }

  close(): void {
    this.readyState = 3;
  }

  open(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  receive(frame: object): void {
    this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent);
  }

  serverClose(code = 1006): void {
    this.readyState = 3;
    this.onclose?.({ code } as CloseEvent);
  }

  parsedSent(): { type: string; data: unknown }[] {
    return this.sent.map((raw) => JSON.parse(raw) as { type: string; data: unknown });
  }
}

function build(overrides: Partial<ConstructorParameters<typeof TransportClient>[0]> = {}) {
  return new TransportClient({
    url: 'ws://127.0.0.1:1234/v1/ws',
    token: 'session-token',
    factory: (url, protocols) => new FakeSocket(url, protocols) as unknown as WebSocket,
    baseDelayMs: 100,
    maxDelayMs: 1000,
    random: () => 1, // deterministic: full jitter always picks the ceiling
    ...overrides,
  });
}

beforeEach(() => {
  FakeSocket.instances = [];
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('authentication', () => {
  it('sends the token as a subprotocol, never in the URL', () => {
    // DEC-026: a query parameter would land in every access log.
    const client = build();
    client.connect();

    const socket = FakeSocket.instances[0]!;
    expect(socket.protocols).toEqual(['mitta.v1', 'session-token']);
    expect(socket.url).not.toContain('session-token');
  });

  it.each([
    ['unauthorised', CLOSE_UNAUTHORISED],
    ['forbidden origin', CLOSE_FORBIDDEN_ORIGIN],
  ])('stops retrying after a %s close', (_label, code) => {
    // Retrying a rejected token forever looks exactly like a network fault.
    const client = build();
    const states: ConnectionState[] = [];
    client.onStateChange((state) => states.push(state));

    client.connect();
    FakeSocket.instances[0]!.serverClose(code);
    vi.advanceTimersByTime(10_000);

    expect(FakeSocket.instances).toHaveLength(1);
    expect(states.at(-1)).toBe('closed');
  });
});

describe('subscriptions', () => {
  it('subscribes to the configured channels on open', () => {
    const client = build({ channels: ['turn', 'task'] });
    client.connect();

    const socket = FakeSocket.instances[0]!;
    socket.open();

    expect(socket.parsedSent()[0]).toEqual({
      type: 'subscribe',
      data: { channels: ['turn', 'task'] },
    });
  });

  it('routes frames to channel listeners on the dot boundary', () => {
    const client = build();
    const turnFrames: string[] = [];
    client.on('turn', (frame) => turnFrames.push(frame.type));

    client.connect();
    const socket = FakeSocket.instances[0]!;
    socket.open();
    socket.receive({ id: '1', type: 'turn.delta', ts: 't', data: {} });
    socket.receive({ id: '2', type: 'task.progress', ts: 't', data: {} });
    socket.receive({ id: '3', type: 'turn.done', ts: 't', data: {} });

    expect(turnFrames).toEqual(['turn.delta', 'turn.done']);
  });

  it('stops delivering after unsubscribe', () => {
    const client = build();
    const seen: string[] = [];
    const off = client.onFrame((frame) => seen.push(frame.id));

    client.connect();
    const socket = FakeSocket.instances[0]!;
    socket.open();
    socket.receive({ id: '1', type: 'x', ts: 't', data: {} });
    off();
    socket.receive({ id: '2', type: 'x', ts: 't', data: {} });

    expect(seen).toEqual(['1']);
  });
});

describe('reconnection', () => {
  it('reconnects with exponential backoff', () => {
    const client = build();
    client.connect();

    FakeSocket.instances[0]!.serverClose();
    vi.advanceTimersByTime(100); // base delay
    expect(FakeSocket.instances).toHaveLength(2);

    FakeSocket.instances[1]!.serverClose();
    vi.advanceTimersByTime(199); // not yet — the ceiling has doubled
    expect(FakeSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.instances).toHaveLength(3);
  });

  it('caps the delay at maxDelayMs', () => {
    const client = build({ baseDelayMs: 100, maxDelayMs: 300 });
    client.connect();

    for (let i = 0; i < 6; i += 1) {
      FakeSocket.instances.at(-1)!.serverClose();
      vi.advanceTimersByTime(300);
    }
    expect(FakeSocket.instances).toHaveLength(7);
  });

  it('resets the backoff after a successful open', () => {
    const client = build();
    client.connect();

    FakeSocket.instances[0]!.serverClose();
    vi.advanceTimersByTime(100);
    FakeSocket.instances[1]!.open();
    FakeSocket.instances[1]!.serverClose();

    // Back to the base delay, not the doubled one.
    vi.advanceTimersByTime(100);
    expect(FakeSocket.instances).toHaveLength(3);
  });

  it('gives up after maxAttempts', () => {
    const client = build({ maxAttempts: 2 });
    const states: ConnectionState[] = [];
    client.onStateChange((state) => states.push(state));

    client.connect();
    for (let i = 0; i < 4; i += 1) {
      FakeSocket.instances.at(-1)?.serverClose();
      vi.advanceTimersByTime(5000);
    }

    expect(FakeSocket.instances).toHaveLength(3); // initial + 2 retries
    expect(states.at(-1)).toBe('closed');
  });

  it('does not reconnect after an explicit close', () => {
    const client = build();
    client.connect();
    client.close();
    vi.advanceTimersByTime(10_000);
    expect(FakeSocket.instances).toHaveLength(1);
  });
});

describe('resume', () => {
  it('resumes from the last frame seen', () => {
    // A dropped socket must never abort a running turn — turns are owned by the
    // sidecar, so the client picks up where it left off.
    const client = build();
    client.connect();

    const first = FakeSocket.instances[0]!;
    first.open();
    first.receive({ id: 'msg_7', type: 'turn.delta', ts: 't', data: {} });
    expect(client.resumeToken).toBe('msg_7');

    first.serverClose();
    vi.advanceTimersByTime(100);
    const second = FakeSocket.instances[1]!;
    second.open();

    expect(second.parsedSent()).toContainEqual({ type: 'resume', data: { after: 'msg_7' } });
  });

  it('does not send resume on a first connection', () => {
    const client = build();
    client.connect();
    FakeSocket.instances[0]!.open();

    const types = FakeSocket.instances[0]!.parsedSent().map((frame) => frame.type);
    expect(types).not.toContain('resume');
  });
});

describe('send', () => {
  it('reports failure rather than throwing when the socket is not open', () => {
    const client = build();
    expect(client.send('turn.start', {})).toBe(false);
  });

  it('sends when open', () => {
    const client = build();
    client.connect();
    FakeSocket.instances[0]!.open();
    expect(client.send('turn.start', { text: 'hi' })).toBe(true);
  });
});
