/**
 * HTTP client for the sidecar.
 *
 * Types come from `src/types/generated/api.ts`, which is generated from the
 * Pydantic schemas (DEC-028). Nothing here declares a response shape by hand.
 */

import type { components } from '@/types/generated/api';

export type HealthResponse = components['schemas']['HealthResponse'];
export type StatusResponse = components['schemas']['StatusResponse'];
export type CapabilitiesResponse = components['schemas']['CapabilitiesResponse'];
export type ComponentStatus = components['schemas']['ComponentStatus'];

/** The error envelope every failure uses (API_DESIGN.md §5). */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    retryable: boolean;
    details: Record<string, unknown>;
    request_id: string | null;
  };
}

export class ApiError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly details: Record<string, unknown>;
  readonly requestId: string | null;

  constructor(
    readonly status: number,
    body: ApiErrorBody,
  ) {
    super(body.error.message);
    this.name = 'ApiError';
    this.code = body.error.code;
    this.retryable = body.error.retryable;
    this.details = body.error.details;
    this.requestId = body.error.request_id;
  }

  /** True when the session token was rejected — the shell must re-handshake. */
  get isAuthFailure(): boolean {
    return this.code.startsWith('auth.');
  }
}

export interface ApiClientOptions {
  baseUrl: string;
  token: string;
  fetch?: typeof globalThis.fetch;
  timeoutMs?: number;
}

const DEFAULT_TIMEOUT_MS = 15_000;

export class ApiClient {
  private readonly fetchImpl: typeof globalThis.fetch;
  private readonly timeoutMs: number;

  constructor(private readonly options: ApiClientOptions) {
    this.fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  }

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);

    try {
      const response = await this.fetchImpl(`${this.options.baseUrl}${path}`, {
        ...init,
        signal: controller.signal,
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${this.options.token}`,
          ...init.headers,
        },
      });

      const text = await response.text();
      const body: unknown = text.length > 0 ? JSON.parse(text) : null;

      if (!response.ok) {
        if (isApiErrorBody(body)) throw new ApiError(response.status, body);
        throw new ApiError(response.status, {
          error: {
            code: `http.${response.status}`,
            message: response.statusText || 'Request failed',
            retryable: response.status >= 500,
            details: {},
            request_id: response.headers.get('X-Request-ID'),
          },
        });
      }

      return body as T;
    } finally {
      clearTimeout(timeout);
    }
  }

  get<T>(path: string): Promise<T> {
    return this.request<T>(path, { method: 'GET' });
  }

  post<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>(path, {
      method: 'POST',
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  }

  // -- typed endpoints ------------------------------------------------------

  health(): Promise<HealthResponse> {
    return this.get<HealthResponse>('/health');
  }

  status(): Promise<StatusResponse> {
    return this.get<StatusResponse>('/v1/status');
  }

  capabilities(): Promise<CapabilitiesResponse> {
    return this.get<CapabilitiesResponse>('/v1/capabilities');
  }
}

function isApiErrorBody(value: unknown): value is ApiErrorBody {
  if (typeof value !== 'object' || value === null) return false;
  const error = (value as Record<string, unknown>)['error'];
  if (typeof error !== 'object' || error === null) return false;
  return typeof (error as Record<string, unknown>)['code'] === 'string';
}
