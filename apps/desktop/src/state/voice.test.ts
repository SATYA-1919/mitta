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
