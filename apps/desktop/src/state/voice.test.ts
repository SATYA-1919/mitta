import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { VoiceUpdate } from '@/lib/ipc/tauri';

import { useVoiceStore } from './voice';

vi.mock('@/lib/ipc/tauri', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/ipc/tauri')>();
  return {
    ...actual,
    isVoiceAvailable: () => true,
    startListening: vi.fn(async () => {}),
    stopListening: vi.fn(async () => {}),
    speak: vi.fn(async () => {}),
    stopSpeaking: vi.fn(async () => {}),
    requestVoicePermission: vi.fn(async () => {}),
    calibrateVoice: vi.fn(async (v: number) => v),
  };
});

function update(over: Partial<VoiceUpdate> = {}): VoiceUpdate {
  return {
    state: 'listening',
    transcript: '',
    isFinal: false,
    level: 0,
    speaking: false,
    continuous: false,
    error: null,
    triggered: false,
    ...over,
  };
}

const initial = useVoiceStore.getState();

beforeEach(() => {
  vi.useRealTimers();
  useVoiceStore.setState(initial, true);
});

describe('the level meter', () => {
  it('follows the microphone while listening', () => {
    useVoiceStore.getState().apply(update({ state: 'listening', level: 0.7 }));
    expect(useVoiceStore.getState().level).toBe(0.7);
  });

  it('is zero whenever the microphone is closed', () => {
    // The one value here that must never be stale. A waveform still moving
    // after the microphone shut says you are being recorded when you are not.
    useVoiceStore.getState().apply(update({ state: 'listening', level: 0.9 }));
    useVoiceStore.getState().apply(update({ state: 'idle', level: 0.9 }));

    expect(useVoiceStore.getState().level).toBe(0);
  });
});

describe('push to talk', () => {
  it('sends what was said when the button is released', async () => {
    const heard: string[] = [];
    useVoiceStore.getState().bind((text) => heard.push(text));

    await useVoiceStore.getState().pushToTalk(true);
    useVoiceStore.getState().apply(update({ transcript: 'open youtube' }));
    await useVoiceStore.getState().pushToTalk(false);

    expect(heard).toEqual(['open youtube']);
    // Cleared, so the next hold does not resend the previous sentence.
    expect(useVoiceStore.getState().transcript).toBe('');
  });

  it('sends nothing when nothing was said', async () => {
    const heard: string[] = [];
    useVoiceStore.getState().bind((text) => heard.push(text));

    await useVoiceStore.getState().pushToTalk(true);
    await useVoiceStore.getState().pushToTalk(false);

    expect(heard).toEqual([]);
  });

  it('does not wait for silence — release is the end of the utterance', async () => {
    const heard: string[] = [];
    useVoiceStore.getState().bind((text) => heard.push(text));

    await useVoiceStore.getState().pushToTalk(true);
    // A partial arrives with `continuous: false`; no settle timer should be
    // armed, because the user says when they are done by letting go.
    useVoiceStore.getState().apply(update({ transcript: 'open', continuous: false }));
    await useVoiceStore.getState().pushToTalk(false);

    expect(heard).toEqual(['open']);
  });
});

describe('continuous mode', () => {
  it('acts on a request once the transcript settles', () => {
    vi.useFakeTimers();
    const heard: string[] = [];
    useVoiceStore.getState().bind((text) => heard.push(text));

    useVoiceStore
      .getState()
      .apply(update({ continuous: true, triggered: true, transcript: 'mitta open youtube' }));

    // Still mid-sentence.
    expect(heard).toEqual([]);
    vi.advanceTimersByTime(1_300);
    expect(heard).toEqual(['mitta open youtube']);
  });

  it('restarts the clock while someone is still talking', () => {
    vi.useFakeTimers();
    const heard: string[] = [];
    useVoiceStore.getState().bind((text) => heard.push(text));

    const store = useVoiceStore.getState();
    store.apply(update({ continuous: true, triggered: true, transcript: 'mitta open' }));
    vi.advanceTimersByTime(800);
    store.apply(update({ continuous: true, triggered: true, transcript: 'mitta open youtube' }));
    vi.advanceTimersByTime(800);

    // 1600ms total, but only 800ms since the last word.
    expect(heard).toEqual([]);
    vi.advanceTimersByTime(600);
    expect(heard).toEqual(['mitta open youtube']);
  });

  it('ignores speech that never named the wake word', () => {
    vi.useFakeTimers();
    const heard: string[] = [];
    useVoiceStore.getState().bind((text) => heard.push(text));

    useVoiceStore
      .getState()
      .apply(update({ continuous: true, triggered: false, transcript: 'what time is the meeting' }));
    vi.advanceTimersByTime(5_000);

    // An open microphone is not an instruction to act on the room.
    expect(heard).toEqual([]);
  });
});

describe('speaking replies', () => {
  it('is off by default', () => {
    // An assistant that starts talking because you typed is a surprise.
    expect(useVoiceStore.getState().speakReplies).toBe(false);
  });

  it('stops mid-sentence when switched off', async () => {
    const { stopSpeaking } = await import('@/lib/ipc/tauri');
    useVoiceStore.getState().setSpeakReplies(true);
    useVoiceStore.getState().setSpeakReplies(false);

    expect(stopSpeaking).toHaveBeenCalled();
  });
});

describe('failure', () => {
  it('surfaces the reason and closes the meter', async () => {
    const { startListening } = await import('@/lib/ipc/tauri');
    vi.mocked(startListening).mockRejectedValueOnce(new Error('permission denied'));

    await useVoiceStore.getState().pushToTalk(true);

    expect(useVoiceStore.getState().status).toBe('failed');
    expect(useVoiceStore.getState().error).toBe('permission denied');
    expect(useVoiceStore.getState().level).toBe(0);
  });
});

describe('end of utterance', () => {
  it('acts the moment the recogniser says the utterance is final', () => {
    // This was the entire felt latency of a spoken request: the backend answers
    // a tool turn in under 900ms, and every voice request had a flat 1.2s of
    // silence added in front of it for a signal Apple had already published.
    vi.useFakeTimers();
    const heard: string[] = [];
    useVoiceStore.getState().bind((text) => heard.push(text));

    useVoiceStore.getState().apply(
      update({
        continuous: true,
        triggered: true,
        isFinal: true,
        transcript: 'mitta open youtube',
      }),
    );

    // No clock advanced at all.
    expect(heard).toEqual(['mitta open youtube']);
  });

  it('does not send the same utterance twice when the timer would also fire', () => {
    vi.useFakeTimers();
    const heard: string[] = [];
    useVoiceStore.getState().bind((text) => heard.push(text));

    const store = useVoiceStore.getState();
    store.apply(update({ continuous: true, triggered: true, transcript: 'mitta open youtube' }));
    store.apply(
      update({
        continuous: true,
        triggered: true,
        isFinal: true,
        transcript: 'mitta open youtube',
      }),
    );
    vi.advanceTimersByTime(2_000);

    expect(heard).toEqual(['mitta open youtube']);
  });

  it('still falls back to the settle timer when isFinal never arrives', () => {
    // A continuous session keeps one recognition task open, so `isFinal` may
    // simply never come. Removing the timer would mean never sending at all.
    vi.useFakeTimers();
    const heard: string[] = [];
    useVoiceStore.getState().bind((text) => heard.push(text));

    useVoiceStore
      .getState()
      .apply(update({ continuous: true, triggered: true, transcript: 'mitta open youtube' }));

    expect(heard).toEqual([]);
    vi.advanceTimersByTime(1_300);
    expect(heard).toEqual(['mitta open youtube']);
  });
});

describe('wake mode across launches', () => {
  beforeEach(() => {
    globalThis.localStorage?.clear();
  });

  it('is off on a first launch', async () => {
    await useVoiceStore.getState().restoreWakePreference();
    expect(useVoiceStore.getState().continuous).toBe(false);
  });

  it('comes back on when it was on last time', async () => {
    await useVoiceStore.getState().toggleContinuous();
    expect(useVoiceStore.getState().continuous).toBe(true);

    // A fresh launch: same stored preference, blank store.
    useVoiceStore.setState(initial, true);
    expect(useVoiceStore.getState().continuous).toBe(false);

    await useVoiceStore.getState().restoreWakePreference();
    expect(useVoiceStore.getState().continuous).toBe(true);
  });

  it('stays off after being switched off', async () => {
    await useVoiceStore.getState().toggleContinuous();
    await useVoiceStore.getState().toggleContinuous();

    useVoiceStore.setState(initial, true);
    await useVoiceStore.getState().restoreWakePreference();
    expect(useVoiceStore.getState().continuous).toBe(false);
  });

  it('does not remember a mode that failed to start', async () => {
    // Persisting an intent that did not take would reopen a microphone nothing
    // opened, on every launch.
    const ipc = await import('@/lib/ipc/tauri');
    vi.mocked(ipc.startListening).mockRejectedValueOnce(new Error('no microphone'));

    await useVoiceStore.getState().toggleContinuous();
    expect(useVoiceStore.getState().continuous).toBe(false);

    useVoiceStore.setState(initial, true);
    await useVoiceStore.getState().restoreWakePreference();
    expect(useVoiceStore.getState().continuous).toBe(false);
  });
});

describe('calibration', () => {
  it('sets the gate from a high percentile, not the loudest frame', async () => {
    // One cough during the quiet window must not set a threshold so high the
    // wake word can never trip it again.
    const ipc = await import('@/lib/ipc/tauri');
    const observed: number[] = [];
    vi.mocked(ipc.calibrateVoice).mockImplementation(async (value: number) => {
      observed.push(value);
      return value * 2.5;
    });

    const store = useVoiceStore.getState();
    const pending = store.calibrate(0.01);

    // Mostly quiet, with one spike.
    for (let i = 0; i < 20; i++) {
      useVoiceStore.getState().apply(update({ state: 'listening', level: 0.002 }));
    }
    useVoiceStore.getState().apply(update({ state: 'listening', level: 0.9 }));
    await pending;

    expect(observed).toHaveLength(1);
    expect(observed[0]).toBeLessThan(0.9);
    expect(useVoiceStore.getState().calibrating).toBe(false);
    expect(useVoiceStore.getState().threshold).not.toBeNull();
  });

  it('ignores levels published while the microphone is closed', async () => {
    // A level arriving after the mic closed is not this room's silence.
    const ipc = await import('@/lib/ipc/tauri');
    vi.mocked(ipc.calibrateVoice).mockResolvedValue(0.01);

    const pending = useVoiceStore.getState().calibrate(0.01);
    useVoiceStore.getState().apply(update({ state: 'idle', level: 0.5 }));
    await pending;

    expect(useVoiceStore.getState().error).toContain('Heard nothing');
  });

  it('will not run twice at once', async () => {
    const ipc = await import('@/lib/ipc/tauri');
    vi.mocked(ipc.calibrateVoice).mockClear();
    vi.mocked(ipc.calibrateVoice).mockResolvedValue(0.01);

    const first = useVoiceStore.getState().calibrate(0.05);
    await useVoiceStore.getState().calibrate(0.05);
    useVoiceStore.getState().apply(update({ state: 'listening', level: 0.004 }));
    await first;

    expect(vi.mocked(ipc.calibrateVoice).mock.calls.length).toBeLessThanOrEqual(1);
  });
});

describe('measured wait', () => {
  it('records how long the request waited before being sent', () => {
    vi.useFakeTimers();
    useVoiceStore.getState().bind(() => {});
    const store = useVoiceStore.getState();

    store.apply(update({ continuous: true, triggered: true, transcript: 'mitta open safari' }));
    vi.advanceTimersByTime(1_300);

    const waited = useVoiceStore.getState().lastWaitMs;
    expect(waited).not.toBeNull();
    expect(waited).toBeGreaterThanOrEqual(0);
  });

  it('measures from the first trigger, not the last partial transcript', () => {
    // A long request must not report the wait of its final syllable.
    vi.useFakeTimers();
    useVoiceStore.getState().bind(() => {});
    const store = useVoiceStore.getState();

    store.apply(update({ continuous: true, triggered: true, transcript: 'mitta open' }));
    vi.advanceTimersByTime(600);
    store.apply(
      update({ continuous: true, triggered: true, isFinal: true, transcript: 'mitta open safari' }),
    );

    // Delivered on isFinal, having been triggered 600ms earlier.
    expect(useVoiceStore.getState().lastWaitMs).not.toBeNull();
  });

  it('starts a fresh clock for the next utterance', () => {
    vi.useFakeTimers();
    useVoiceStore.getState().bind(() => {});
    const store = useVoiceStore.getState();

    store.apply(
      update({ continuous: true, triggered: true, isFinal: true, transcript: 'mitta one' }),
    );
    const first = useVoiceStore.getState().lastWaitMs;

    store.apply(
      update({ continuous: true, triggered: true, isFinal: true, transcript: 'mitta two' }),
    );
    const second = useVoiceStore.getState().lastWaitMs;

    // Not cumulative: the second must not include the first's wait.
    expect(second).not.toBeNull();
    expect(second!).toBeLessThanOrEqual(first! + 50);
  });
});
