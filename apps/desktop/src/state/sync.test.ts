import { beforeEach, describe, expect, it } from 'vitest';

import type { Envelope } from '@/lib/transport/envelope';
import type { FrameHandler, StateHandler, TransportClient } from '@/lib/transport/socket';

import { displayText, useStore } from './store';
import { bindTransport } from './sync';

/** Stands in for a TransportClient so frames can be injected directly. */
class FakeTransport {
  private frameHandlers: FrameHandler[] = [];
  private stateHandlers: StateHandler[] = [];

  onFrame(handler: FrameHandler): () => void {
    this.frameHandlers.push(handler);
    return () => {
      this.frameHandlers = this.frameHandlers.filter((h) => h !== handler);
    };
  }

  onStateChange(handler: StateHandler): () => void {
    this.stateHandlers.push(handler);
    return () => {
      this.stateHandlers = this.stateHandlers.filter((h) => h !== handler);
    };
  }

  emit(type: string, data: unknown): void {
    const frame: Envelope = { id: `f_${type}`, type, ts: '2026-07-29T00:00:00.000Z', data };
    for (const handler of [...this.frameHandlers]) handler(frame);
  }

  emitState(state: Parameters<StateHandler>[0], detail?: string): void {
    for (const handler of [...this.stateHandlers]) handler(state, detail);
  }

  asClient(): TransportClient {
    return this as unknown as TransportClient;
  }
}

const initial = useStore.getState();

beforeEach(() => {
  useStore.setState(initial, true);
});

describe('connection state', () => {
  it('mirrors transport state into the store', () => {
    const transport = new FakeTransport();
    bindTransport(transport.asClient());

    transport.emitState('reconnecting', 'retrying in 200ms');

    expect(useStore.getState().connection).toBe('reconnecting');
    expect(useStore.getState().connectionDetail).toBe('retrying in 200ms');
  });

  it('clears readiness when the connection drops', () => {
    const transport = new FakeTransport();
    bindTransport(transport.asClient());
    useStore.getState().setReadiness(true, 1, []);

    transport.emitState('closed');

    // `ready` means "can serve a turn". A closed socket cannot.
    expect(useStore.getState().ready).toBe(false);
  });
});

describe('turn lifecycle', () => {
  it('streams deltas into the buffer', () => {
    const transport = new FakeTransport();
    bindTransport(transport.asClient());

    transport.emit('turn.accepted', { turn_id: 'trn_1', conversation_id: 'cnv_1' });
    transport.emit('turn.thinking', { phase: 'reasoning' });
    transport.emit('turn.delta', { text: 'clean' });
    transport.emit('turn.delta', { text: 'ing up' });

    const turn = useStore.getState().activeTurn;
    expect(turn?.turnId).toBe('trn_1');
    expect(turn?.phase).toBe('reasoning');
    expect(displayText(turn)).toBe('cleaning up');
  });

  it('replaces the streamed buffer with the styled text in one swap', () => {
    // DEC-027: stream raw, then settle. The swap is atomic, not token-by-token.
    const transport = new FakeTransport();
    bindTransport(transport.asClient());

    transport.emit('turn.accepted', { turn_id: 'trn_1', conversation_id: 'cnv_1' });
    transport.emit('turn.delta', { text: 'I have cleaned your Downloads folder.' });
    transport.emit('turn.message', { content: 'done ra', styled: true, register: 'playful' });

    const turn = useStore.getState().activeTurn;
    expect(displayText(turn)).toBe('done ra');
    expect(turn?.register).toBe('playful');
    // The stream is retained so the rewrite stays auditable.
    expect(turn?.streamed).toBe('I have cleaned your Downloads folder.');
  });

  it('does not swap when the rewrite was a no-op', () => {
    // styled: false means personality returned its input unchanged, so there is
    // nothing to replace and the user must see no flicker.
    const transport = new FakeTransport();
    bindTransport(transport.asClient());

    transport.emit('turn.accepted', { turn_id: 'trn_1', conversation_id: 'cnv_1' });
    transport.emit('turn.delta', { text: 'Schema version 1.' });
    transport.emit('turn.message', {
      content: 'Schema version 1.',
      styled: false,
      register: 'serious',
    });

    const turn = useStore.getState().activeTurn;
    expect(displayText(turn)).toBe('Schema version 1.');
    expect(turn?.final).toBe(turn?.streamed);
  });

  it('records a terminal error', () => {
    const transport = new FakeTransport();
    bindTransport(transport.asClient());

    transport.emit('turn.accepted', { turn_id: 'trn_1', conversation_id: 'cnv_1' });
    transport.emit('turn.error', { code: 'provider.rate_limited', message: 'Groq rate-limited' });

    expect(useStore.getState().activeTurn?.status).toBe('failed');
    expect(useStore.getState().activeTurn?.error).toBe('Groq rate-limited');
  });

  it('marks the turn complete on turn.done', () => {
    const transport = new FakeTransport();
    bindTransport(transport.asClient());

    transport.emit('turn.accepted', { turn_id: 'trn_1', conversation_id: 'cnv_1' });
    transport.emit('turn.done', {});

    expect(useStore.getState().activeTurn?.status).toBe('completed');
    expect(useStore.getState().activeTurn?.phase).toBeNull();
  });

  it('ignores unknown frame types', () => {
    // A newer sidecar emitting a type this build predates must not break the
    // window; the api_version handshake catches real skew.
    const transport = new FakeTransport();
    bindTransport(transport.asClient());

    expect(() => transport.emit('quantum.entangled', { x: 1 })).not.toThrow();
    expect(useStore.getState().activeTurn).toBeNull();
  });

  it('ignores turn frames that arrive with no active turn', () => {
    const transport = new FakeTransport();
    bindTransport(transport.asClient());

    transport.emit('turn.delta', { text: 'orphan' });

    expect(useStore.getState().activeTurn).toBeNull();
  });
});

describe('teardown', () => {
  it('detaches every handler', () => {
    const transport = new FakeTransport();
    const dispose = bindTransport(transport.asClient());

    dispose();
    transport.emit('turn.accepted', { turn_id: 'trn_1', conversation_id: 'cnv_1' });
    transport.emitState('open');

    expect(useStore.getState().activeTurn).toBeNull();
    expect(useStore.getState().connection).toBe('idle');
  });
});
