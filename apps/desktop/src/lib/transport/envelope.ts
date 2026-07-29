/**
 * WebSocket envelope codec (API_DESIGN.md §4.1).
 *
 * Every frame in both directions shares one shape, which is what allows one
 * codec, one logger and one replay tool rather than a parser per message type.
 */

export interface Envelope<T = unknown> {
  /** ULID, unique per frame. */
  id: string;
  /** Dot-namespaced; the namespace is the subsystem. */
  type: string;
  /** ISO-8601 with milliseconds. */
  ts: string;
  /** Correlates to the originating request, when there is one. */
  ref?: string | undefined;
  data: T;
}

export type Channel = 'turn' | 'task' | 'memory' | 'notification' | 'provider' | 'voice' | 'plugin';

export class EnvelopeDecodeError extends Error {
  constructor(
    message: string,
    readonly raw: string,
  ) {
    super(message);
    this.name = 'EnvelopeDecodeError';
  }
}

/**
 * Parse an inbound frame.
 *
 * Validates structurally rather than trusting the server. The sidecar is
 * trusted, but a malformed frame from a version-skewed build should surface as
 * one clear error rather than as `undefined` propagating into the store and
 * failing three layers away.
 */
export function decode(raw: string): Envelope {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (cause) {
    throw new EnvelopeDecodeError(`Frame is not valid JSON: ${String(cause)}`, raw);
  }

  if (typeof parsed !== 'object' || parsed === null) {
    throw new EnvelopeDecodeError('Frame is not an object', raw);
  }

  const candidate = parsed as Record<string, unknown>;
  for (const field of ['id', 'type', 'ts'] as const) {
    if (typeof candidate[field] !== 'string') {
      throw new EnvelopeDecodeError(`Frame is missing a string "${field}"`, raw);
    }
  }

  return {
    id: candidate['id'] as string,
    type: candidate['type'] as string,
    ts: candidate['ts'] as string,
    ref: typeof candidate['ref'] === 'string' ? candidate['ref'] : undefined,
    data: candidate['data'] ?? {},
  };
}

export function encode(type: string, data: unknown, ref?: string): string {
  const frame: Record<string, unknown> = { type, data };
  if (ref !== undefined) frame['ref'] = ref;
  return JSON.stringify(frame);
}

/**
 * Match a frame type against a channel prefix.
 *
 * Prefix matching on the dot boundary, not `startsWith`: `"turn"` must match
 * `"turn.delta"` but never `"turnstile.x"`.
 */
export function inChannel(type: string, channel: string): boolean {
  return type === channel || type.startsWith(`${channel}.`);
}
