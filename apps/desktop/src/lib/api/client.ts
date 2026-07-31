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

export type Memory = components['schemas']['MemoryResource'];
export type MemoryKind = components['schemas']['MemoryKind'];
export type MemoryStatus = components['schemas']['MemoryStatus'];
export type MemoryList = components['schemas']['MemoryListResponse'];
export type MemoryStats = components['schemas']['MemoryStatsResponse'];
export type SearchResponse = components['schemas']['SearchResponse'];
export type SearchHit = components['schemas']['SearchHitResource'];
export type CreateMemoryRequest = components['schemas']['CreateMemoryRequest'];
export type UpdateMemoryRequest = components['schemas']['UpdateMemoryRequest'];
export type SearchRequest = components['schemas']['SearchRequest'];
export type SweepResponse = components['schemas']['SweepResponse'];

export type Conversation = components['schemas']['ConversationResource'];
export type Message = components['schemas']['MessageResource'];
export type ConversationList = components['schemas']['ConversationListResponse'];
export type MessageList = components['schemas']['MessageListResponse'];
export type HistoryRange = components['schemas']['HistoryRange'];
export type HistoryCount = components['schemas']['HistoryCountResponse'];
export type ClearHistoryResult = components['schemas']['ClearHistoryResponse'];

export type Project = components['schemas']['ProjectSummary'];
export type ProjectList = components['schemas']['ProjectListResponse'];
export type ProjectPath = components['schemas']['PathResource'];
export type PathList = components['schemas']['PathListResponse'];
export type PathKind = components['schemas']['PathKind'];
export type PathResolution = components['schemas']['ResolutionResource'];
export type Containment = components['schemas']['Containment'];
export type CreateProjectRequest = components['schemas']['CreateProjectRequest'];
export type UpdateProjectRequest = components['schemas']['UpdateProjectRequest'];
export type AddPathRequest = components['schemas']['AddPathRequest'];
export type Timeline = components['schemas']['TimelineResponse'];

export type Task = components['schemas']['TaskResource'];
export type TaskStatus = components['schemas']['TaskStatus'];
export type TaskList = components['schemas']['TaskListResponse'];
export type TaskDetail = components['schemas']['TaskDetailResponse'];
export type Plan = components['schemas']['PlanResource'];
export type PlanStatus = components['schemas']['PlanStatus'];
export type PlanDetail = components['schemas']['PlanDetailResponse'];
export type Schedule = components['schemas']['ScheduleResource'];
export type ScheduleList = components['schemas']['ScheduleListResponse'];
export type CreateScheduleRequest = components['schemas']['CreateScheduleRequest'];
export type UpdateScheduleRequest = components['schemas']['UpdateScheduleRequest'];
export type RunResult = components['schemas']['RunResponse'];

export interface ListMemoriesParams {
  kind?: MemoryKind;
  projectId?: string;
  status?: MemoryStatus;
  pinnedOnly?: boolean;
  limit?: number;
  offset?: number;
}

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

  // -- memory ---------------------------------------------------------------

  listMemories(params: ListMemoriesParams = {}): Promise<MemoryList> {
    const query = new URLSearchParams();
    if (params.kind !== undefined) query.set('kind', params.kind);
    if (params.projectId !== undefined) query.set('project_id', params.projectId);
    if (params.status !== undefined) query.set('status', params.status);
    if (params.pinnedOnly === true) query.set('pinned_only', 'true');
    if (params.limit !== undefined) query.set('limit', String(params.limit));
    if (params.offset !== undefined) query.set('offset', String(params.offset));
    const suffix = query.size > 0 ? `?${query.toString()}` : '';
    return this.get<MemoryList>(`/v1/memory${suffix}`);
  }

  getMemory(id: string): Promise<Memory> {
    return this.get<Memory>(`/v1/memory/${encodeURIComponent(id)}`);
  }

  createMemory(body: CreateMemoryRequest): Promise<Memory> {
    return this.post<Memory>('/v1/memory', body);
  }

  /**
   * Search is a POST because the query is conversational text. A GET would put
   * it in a URL, and therefore in history and access logs — the same instinct
   * behind R5, applied to what gets written down locally.
   */
  searchMemories(body: SearchRequest): Promise<SearchResponse> {
    return this.post<SearchResponse>('/v1/memory/search', body);
  }

  updateMemory(id: string, body: UpdateMemoryRequest): Promise<Memory> {
    return this.request<Memory>(`/v1/memory/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    });
  }

  forgetMemory(id: string): Promise<Memory> {
    return this.post<Memory>(`/v1/memory/${encodeURIComponent(id)}/forget`);
  }

  restoreMemory(id: string): Promise<Memory> {
    return this.post<Memory>(`/v1/memory/${encodeURIComponent(id)}/restore`);
  }

  /** Irreversible. `forgetMemory` is the reversible one (DEC-053). */
  async purgeMemory(id: string): Promise<void> {
    await this.request<null>(`/v1/memory/${encodeURIComponent(id)}`, { method: 'DELETE' });
  }

  memoryStats(): Promise<MemoryStats> {
    return this.get<MemoryStats>('/v1/memory/stats');
  }

  sweepMemories(): Promise<SweepResponse> {
    return this.post<SweepResponse>('/v1/memory/maintenance/sweep');
  }

  reindexMemories(): Promise<MemoryStats> {
    return this.post<MemoryStats>('/v1/memory/maintenance/reindex');
  }

  // -- conversations --------------------------------------------------------

  listConversations(limit = 50): Promise<ConversationList> {
    return this.get<ConversationList>(`/v1/conversations?limit=${limit}`);
  }

  conversationMessages(id: string, limit = 200): Promise<MessageList> {
    return this.get<MessageList>(
      `/v1/conversations/${encodeURIComponent(id)}/messages?limit=${limit}`,
    );
  }

  createConversation(title?: string): Promise<Conversation> {
    return this.post<Conversation>('/v1/conversations', title === undefined ? {} : { title });
  }

  deleteConversation(id: string): Promise<void> {
    return this.request<void>(`/v1/conversations/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    });
  }

  /** How many conversations clearing `range` would delete — for the confirmation. */
  historyCount(range: HistoryRange): Promise<HistoryCount> {
    return this.get<HistoryCount>(`/v1/conversations/history/count?range=${range}`);
  }

  /**
   * Irreversible. Deletes whole conversations and their transcripts.
   *
   * `confirm` is sent explicitly and the server rejects the request without it.
   * The cutoff is never sent — the server computes it from the named range, so
   * the set deleted cannot differ from the set the button promised.
   */
  clearHistory(range: HistoryRange): Promise<ClearHistoryResult> {
    return this.post<ClearHistoryResult>('/v1/conversations/history/clear', {
      range,
      confirm: true,
    });
  }

  // -- projects -------------------------------------------------------------

  listProjects(includeArchived = false): Promise<ProjectList> {
    // `all`, not an omitted parameter: `status=archived` would return *only*
    // archived, which is not the toggle the UI wants.
    return this.get<ProjectList>(`/v1/projects?status=${includeArchived ? 'all' : 'active'}`);
  }

  createProject(body: CreateProjectRequest): Promise<Project> {
    return this.post<Project>('/v1/projects', body);
  }

  updateProject(id: string, body: UpdateProjectRequest): Promise<Project> {
    return this.request<Project>(`/v1/projects/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    });
  }

  async deleteProject(id: string): Promise<void> {
    await this.request<null>(`/v1/projects/${encodeURIComponent(id)}`, { method: 'DELETE' });
  }

  projectPaths(id: string): Promise<PathList> {
    return this.get<PathList>(`/v1/projects/${encodeURIComponent(id)}/paths`);
  }

  addProjectPath(id: string, body: AddPathRequest): Promise<ProjectPath> {
    return this.post<ProjectPath>(`/v1/projects/${encodeURIComponent(id)}/paths`, body);
  }

  async removeProjectPath(id: string, path: string): Promise<void> {
    // The path travels as a query parameter, not a URL segment: an absolute
    // filesystem path in a segment has to be encoded, and a double-encoded
    // slash is how the wrong permission gets revoked.
    await this.request<null>(
      `/v1/projects/${encodeURIComponent(id)}/paths?path=${encodeURIComponent(path)}`,
      { method: 'DELETE' },
    );
  }

  /**
   * What the policy engine would conclude about a path, before any tool asks.
   *
   * R5: a boundary the user can only observe through a confirmation card at the
   * moment of action is not one they can audit.
   */
  resolvePath(path: string): Promise<PathResolution> {
    return this.get<PathResolution>(`/v1/projects/resolve-path?path=${encodeURIComponent(path)}`);
  }

  projectMemories(id: string, limit = 50): Promise<MemoryList> {
    return this.get<MemoryList>(`/v1/projects/${encodeURIComponent(id)}/memory?limit=${limit}`);
  }

  // -- tasks and schedules --------------------------------------------------

  listTasks(activeOnly = false, limit = 50): Promise<TaskList> {
    return this.get<TaskList>(`/v1/tasks?active=${activeOnly}&limit=${limit}`);
  }

  task(id: string): Promise<TaskDetail> {
    return this.get<TaskDetail>(`/v1/tasks/${encodeURIComponent(id)}`);
  }

  /** Stops the whole plan, not just this step (API_DESIGN.md §3.5). */
  cancelTask(id: string): Promise<RunResult> {
    return this.post<RunResult>(`/v1/tasks/${encodeURIComponent(id)}/cancel`);
  }

  /**
   * Re-runs a failed step. Completed steps are not run again — for a task that
   * already wrote a file, that would be a second write rather than a resume.
   */
  resumeTask(id: string): Promise<RunResult> {
    return this.post<RunResult>(`/v1/tasks/${encodeURIComponent(id)}/resume`);
  }

  plan(id: string): Promise<PlanDetail> {
    return this.get<PlanDetail>(`/v1/plans/${encodeURIComponent(id)}`);
  }

  listSchedules(): Promise<ScheduleList> {
    return this.get<ScheduleList>('/v1/schedules');
  }

  /**
   * Creating a `tool` schedule authorises that exact call at every later fire
   * (DEC-122), so this request is a permission grant and the server audits it
   * as one. Nothing MITTA generates can reach this method.
   */
  createSchedule(body: CreateScheduleRequest): Promise<Schedule> {
    return this.post<Schedule>('/v1/schedules', body);
  }

  updateSchedule(id: string, body: UpdateScheduleRequest): Promise<Schedule> {
    return this.request<Schedule>(`/v1/schedules/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    });
  }

  /** The only way to withdraw the grant. Past runs survive it. */
  async deleteSchedule(id: string): Promise<void> {
    await this.request<null>(`/v1/schedules/${encodeURIComponent(id)}`, { method: 'DELETE' });
  }

  /** Fires now, through the path a real fire takes. Leaves the timetable alone. */
  runSchedule(id: string): Promise<RunResult> {
    return this.post<RunResult>(`/v1/schedules/${encodeURIComponent(id)}/run`);
  }
}

function isApiErrorBody(value: unknown): value is ApiErrorBody {
  if (typeof value !== 'object' || value === null) return false;
  const error = (value as Record<string, unknown>)['error'];
  if (typeof error !== 'object' || error === null) return false;
  return typeof (error as Record<string, unknown>)['code'] === 'string';
}
