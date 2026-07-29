import { describe, expect, it } from 'vitest';

import { decode, encode, EnvelopeDecodeError, inChannel } from './envelope';

describe('decode', () => {
  it('parses a well-formed frame', () => {
    const frame = decode(
      JSON.stringify({
        id: 'msg_01HQ',
        type: 'turn.delta',
        ts: '2026-07-29T09:41:02.881Z',
        ref: 'trn_01HQ',
        data: { text: 'hello' },
      }),
    );
    expect(frame.id).toBe('msg_01HQ');
    expect(frame.type).toBe('turn.delta');
    expect(frame.ref).toBe('trn_01HQ');
    expect(frame.data).toEqual({ text: 'hello' });
  });

  it('defaults a missing data field to an empty object', () => {
    const frame = decode(JSON.stringify({ id: 'a', type: 'turn.done', ts: 't' }));
    expect(frame.data).toEqual({});
  });

  it('leaves ref undefined when absent', () => {
    const frame = decode(JSON.stringify({ id: 'a', type: 'x', ts: 't' }));
    expect(frame.ref).toBeUndefined();
  });

  it.each([
    ['not json at all', 'not json'],
    ['a bare array', '[]'],
    ['null', 'null'],
    ['a frame missing id', JSON.stringify({ type: 'x', ts: 't' })],
    ['a frame missing type', JSON.stringify({ id: 'a', ts: 't' })],
    ['a frame with a non-string id', JSON.stringify({ id: 1, type: 'x', ts: 't' })],
  ])('rejects %s', (_label, raw) => {
    // Validated rather than trusted: a malformed frame from a version-skewed
    // build should fail here, not three layers away as an undefined.
    expect(() => decode(raw)).toThrow(EnvelopeDecodeError);
  });
});

describe('encode', () => {
  it('omits ref when not supplied', () => {
    expect(JSON.parse(encode('turn.start', { a: 1 }))).toEqual({
      type: 'turn.start',
      data: { a: 1 },
    });
  });

  it('includes ref when supplied', () => {
    expect(JSON.parse(encode('turn.cancel', {}, 'trn_1'))).toMatchObject({ ref: 'trn_1' });
  });
});

describe('inChannel', () => {
  it('matches the exact channel and its dot-namespaced children', () => {
    expect(inChannel('turn', 'turn')).toBe(true);
    expect(inChannel('turn.delta', 'turn')).toBe(true);
    expect(inChannel('turn.tool.result', 'turn')).toBe(true);
  });

  it('does not match a channel that merely shares a prefix', () => {
    // The reason prefix matching is on the dot boundary rather than startsWith.
    expect(inChannel('turnstile.opened', 'turn')).toBe(false);
    expect(inChannel('task.progress', 'turn')).toBe(false);
  });
});
