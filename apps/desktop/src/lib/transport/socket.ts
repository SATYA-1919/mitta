/**
 * Authenticated WebSocket client (API_DESIGN.md §4).
 *
 * Two properties drive the design:
 *
 * 1. **A dropped socket must never abort a running turn.** Turns are owned by
 *    the sidecar, not by the connection. The client reconnects and resumes from
 *    the last frame it saw; if the server's buffer has rolled past that point it
 *    replies `resume.gap` and the client refetches over HTTP.
 * 2. **The token rides the subprotocol header** (DEC-026), never a query
 *    parameter, which would write the credential into every access log.
 */

import { type Channel, decode, encode, type Envelope, inChannel } from './envelope';

export type ConnectionState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed';

export type FrameHandler = (frame: Envelope) => void;
export type StateHandler = (state: ConnectionState, detail?: string) => void;

/** Injected so tests can drive a fake socket without a server. */
export type SocketFactory = (url: string, protocols: string[]) => WebSocket;

export interface SocketOptions {
  url: string;
  token: string;
  channels?: Channel[];
  factory?: SocketFactory;
  /** Overridable for tests; production values are the defaults. */
  baseDelayMs?: number;
  maxDelayMs?: number;
  maxAttempts?: number;
  random?: () => number;
}

const SUBPROTOCOL = 'mitta.v1';
const DEFAULT_BASE_DELAY_MS = 250;
const DEFAULT_MAX_DELAY_MS = 15_000;

/** Matches the server's close codes for auth failure. */
export const CLOSE_UNAUTHORISED = 4401;
export const CLOSE_FORBIDDEN_ORIGIN = 4403;

export class TransportClient {
  private socket: WebSocket | null = null;
  private state: ConnectionState = 'idle';
  private attempt = 0;
  private lastFrameId: string | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;

  private readonly frameHandlers = new Set<FrameHandler>();
  private readonly stateHandlers = new Set<StateHandler>();
  private readonly channels: Set<Channel>;

  private readonly factory: SocketFactory;
  private readonly baseDelayMs: number;
  private readonly maxDelayMs: number;
  private readonly maxAttempts: number;
  private readonly random: () => number;

  constructor(private readonly options: SocketOptions) {
    this.channels = new Set(options.channels ?? []);
    this.factory = options.factory ?? ((url, protocols) => new WebSocket(url, protocols));
    this.baseDelayMs = options.baseDelayMs ?? DEFAULT_BASE_DELAY_MS;
    this.maxDelayMs = options.maxDelayMs ?? DEFAULT_MAX_DELAY_MS;
    this.maxAttempts = options.maxAttempts ?? Number.POSITIVE_INFINITY;
    this.random = options.random ?? Math.random;
  }

  // -- lifecycle ------------------------------------------------------------

  connect(): void {
    if (this.socket !== null || this.stopped) return;
    this.setState(this.attempt === 0 ? 'connecting' : 'reconnecting');

    const socket = this.factory(this.options.url, [SUBPROTOCOL, this.options.token]);
    this.socket = socket;

    socket.onopen = () => {
      this.attempt = 0;
      this.setState('open');
      if (this.channels.size > 0) {
        this.send('subscribe', { channels: [...this.channels] });
      }
      if (this.lastFrameId !== null) {
        this.send('resume', { after: this.lastFrameId });
      }
    };

    socket.onmessage = (event: MessageEvent) => {
      if (typeof event.data !== 'string') return;
      const frame = decode(event.data);
      this.lastFrameId = frame.id;
      for (const handler of this.frameHandlers) handler(frame);
    };

    socket.onclose = (event: CloseEvent) => {
      this.socket = null;
      if (this.stopped) {
        this.setState('closed');
        return;
      }
      // Authentication failures are terminal. Retrying with the same rejected
      // token would produce an infinite reconnect loop that looks, from the
      // outside, exactly like a network problem.
      if (event.code === CLOSE_UNAUTHORISED || event.code === CLOSE_FORBIDDEN_ORIGIN) {
        this.stopped = true;
        this.setState('closed', `authentication rejected (${event.code})`);
        return;
      }
      this.scheduleReconnect();
    };

    socket.onerror = () => {
      // `close` always follows `error`; reconnection is handled there so the
      // two paths cannot both schedule a retry.
    };
  }

  close(): void {
    this.stopped = true;
    this.clearTimer();
    this.socket?.close(1000, 'client shutdown');
    this.socket = null;
    this.setState('closed');
  }

  // -- messaging ------------------------------------------------------------

  send(type: string, data: unknown, ref?: string): boolean {
    if (this.socket === null || this.socket.readyState !== WebSocket.OPEN) return false;
    this.socket.send(encode(type, data, ref));
    return true;
  }

  subscribe(...channels: Channel[]): void {
    for (const channel of channels) this.channels.add(channel);
    this.send('subscribe', { channels });
  }

  unsubscribe(...channels: Channel[]): void {
    for (const channel of channels) this.channels.delete(channel);
    this.send('unsubscribe', { channels });
  }

  /** Listen to every frame. Returns an unsubscribe function. */
  onFrame(handler: FrameHandler): () => void {
    this.frameHandlers.add(handler);
    return () => this.frameHandlers.delete(handler);
  }

  /** Listen to one channel's frames, e.g. `on('turn', …)` for `turn.*`. */
  on(channel: string, handler: FrameHandler): () => void {
    return this.onFrame((frame) => {
      if (inChannel(frame.type, channel)) handler(frame);
    });
  }

  onStateChange(handler: StateHandler): () => void {
    this.stateHandlers.add(handler);
    return () => this.stateHandlers.delete(handler);
  }

  get connectionState(): ConnectionState {
    return this.state;
  }

  get resumeToken(): string | null {
    return this.lastFrameId;
  }

  // -- reconnection ---------------------------------------------------------

  /**
   * Exponential backoff with full jitter.
   *
   * Jitter matters even with a single client: without it, the palette and main
   * windows both retry on the same schedule and hammer the sidecar in lockstep
   * every time it restarts.
   */
  private nextDelay(): number {
    const ceiling = Math.min(this.maxDelayMs, this.baseDelayMs * 2 ** this.attempt);
    return Math.round(this.random() * ceiling);
  }

  private scheduleReconnect(): void {
    if (this.attempt >= this.maxAttempts) {
      this.setState('closed', 'retry limit reached');
      return;
    }
    const delay = this.nextDelay();
    this.attempt += 1;
    this.setState('reconnecting', `retrying in ${delay}ms`);
    this.clearTimer();
    this.timer = setTimeout(() => {
      this.timer = null;
      this.connect();
    }, delay);
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  private setState(state: ConnectionState, detail?: string): void {
    this.state = state;
    for (const handler of this.stateHandlers) handler(state, detail);
  }
}
