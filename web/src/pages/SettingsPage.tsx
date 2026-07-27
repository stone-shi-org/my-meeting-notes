import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckCircle2, PlugZap, XCircle } from 'lucide-react';
import { useEffect, useState } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import { Button } from '@/components/ui/Button';
import {
  Badge,
  Card,
  Input,
  Label,
  Select,
  Skeleton,
  Textarea,
} from '@/components/ui/primitives';
import { ErrorState } from '@/components/ui/states';
import { useAuth } from '@/hooks/useAuth';
import { api } from '@/lib/api';
import { cn } from '@/lib/cn';
import type {
  McpServer,
  Paginated,
  PromptDetail,
  PromptSummary,
  SettingEntry,
  User,
  UserMcpProfile,
} from '@/types/api';

const TABS = [
  { to: '/settings/llm', label: 'LLM' },
  { to: '/settings/diarization', label: 'Diarization' },
  { to: '/settings/mcp', label: 'Integrations' },
  { to: '/settings/prompt', label: 'Prompts' },
  { to: '/settings/users', label: 'Users', adminOnly: true },
];

export function SettingsPage() {
  const { isAdmin } = useAuth();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="font-display text-2xl font-semibold">Settings</h1>
        <p className="mt-1 text-sm text-fg-subtle">
          Endpoints, prompts and integrations. Changes take effect immediately.
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-[220px_minmax(0,1fr)]">
        <nav aria-label="Settings sections" className="flex gap-1 overflow-x-auto lg:flex-col">
          {TABS.filter((t) => !t.adminOnly || isAdmin).map((tab) => (
            <NavLink
              key={tab.to}
              to={tab.to}
              className={({ isActive }) =>
                cn(
                  'whitespace-nowrap rounded px-3 py-2 text-base font-medium transition-colors duration-fast',
                  isActive
                    ? 'bg-primary-soft text-primary-soft-fg'
                    : 'text-fg-muted hover:bg-surface-2 hover:text-fg',
                )
              }
            >
              {tab.label}
            </NavLink>
          ))}
        </nav>

        <div className="min-w-0">
          <Outlet />
        </div>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Shared settings-form machinery                                             */
/* -------------------------------------------------------------------------- */

function useSettings() {
  return useQuery({
    queryKey: ['settings'],
    queryFn: () => api.get<{ settings: Record<string, SettingEntry> }>('/settings'),
  });
}

interface TestResult {
  ok: boolean;
  error: string | null;
  latency_ms: number;
  response?: string | null;
  models_count?: number;
  /** Set when the connection worked but the result needs a caveat, e.g. a
   * reasoning model that returned no visible text. */
  note?: string | null;
}

function SettingsForm({
  title,
  description,
  keys,
  modelsPath,
  modelKey,
  testPath,
  testKeyMap,
}: {
  title: string;
  description: string;
  keys: { key: string; label: string; hint?: string; type?: string }[];
  modelsPath?: string;
  modelKey?: string;
  /** Endpoint that tests the connection this form configures. */
  testPath?: string;
  /** Maps a settings key (e.g. "llm_base_url") to the short field name the
   * test endpoint expects (e.g. "base_url"), so Test always exercises
   * whatever is currently in the form -- saved or not. */
  testKeyMap?: Record<string, string>;
}) {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const settings = useSettings();
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [testResult, setTestResult] = useState<TestResult | null>(null);

  const models = useQuery({
    queryKey: ['models', modelsPath],
    queryFn: () =>
      api.get<{ models: { id: string }[]; error: string | null }>(modelsPath!),
    enabled: !!modelsPath,
    staleTime: 300_000,
  });

  const entries = settings.data?.settings ?? {};

  const save = useMutation({
    mutationFn: () => api.put('/settings', { values: draft }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['settings'] });
      setDraft({});
    },
  });

  const test = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {};
      for (const [settingKey, shortKey] of Object.entries(testKeyMap ?? {})) {
        const entry = entries[settingKey];
        const value = draft[settingKey] ?? entry?.value;
        // A masked secret that wasn't edited: omit it and let the backend
        // fall back to the stored value, same convention as Save.
        if (typeof value === 'string' && value.startsWith('••••')) continue;
        if (value === undefined || value === '') continue;
        body[shortKey] = value;
      }
      return api.post<TestResult>(testPath!, body);
    },
    onSuccess: setTestResult,
  });

  if (settings.isLoading) return <Skeleton className="h-64 w-full" />;
  if (settings.isError) return <ErrorState error={settings.error} />;

  const dirty = Object.keys(draft).length > 0;

  return (
    <Card className="p-5">
      <h2 className="font-display text-lg font-semibold">{title}</h2>
      <p className="mt-1 text-sm text-fg-subtle">{description}</p>

      <div className="mt-5 space-y-4">
        {keys.map(({ key, label, hint, type }) => {
          const entry = entries[key];
          if (!entry) return null;
          const value = draft[key] ?? (entry.value ?? '');
          const isModelField = key === modelKey;

          return (
            <div key={key}>
              <Label htmlFor={key}>{label}</Label>

              {isModelField && models.data?.models?.length ? (
                <div className="mt-1.5 flex gap-2">
                  <Select
                    id={key}
                    value={String(value)}
                    disabled={!isAdmin}
                    onChange={(e) => {
                      setDraft((d) => ({ ...d, [key]: e.target.value }));
                      setTestResult(null);
                    }}
                  >
                    {!models.data.models.some((m) => m.id === value) && (
                      <option value={String(value)}>{String(value)} (current)</option>
                    )}
                    {models.data.models.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.id}
                      </option>
                    ))}
                  </Select>
                </div>
              ) : (
                <Input
                  id={key}
                  className="mt-1.5"
                  type={entry.is_secret ? 'password' : (type ?? 'text')}
                  value={String(value)}
                  disabled={!isAdmin}
                  placeholder={entry.is_secret ? 'unchanged' : undefined}
                  onChange={(e) => {
                    setDraft((d) => ({ ...d, [key]: e.target.value }));
                    setTestResult(null);
                  }}
                />
              )}

              {hint && <p className="mt-1 text-xs text-fg-subtle">{hint}</p>}
              {isModelField && models.data?.error && (
                <p className="mt-1 text-xs text-warning-ink">
                  Could not list models ({models.data.error}). The field stays editable.
                </p>
              )}
              {entry.overridden && (
                <p className="mt-1 text-xs text-fg-faint">Overridden from the default</p>
              )}
            </div>
          );
        })}
      </div>

      {save.error && (
        <p className="mt-3 text-sm text-danger-ink">{(save.error as Error).message}</p>
      )}

      {testResult && (
        <div
          className={cn(
            'mt-3 rounded border p-3 text-sm',
            testResult.ok
              ? 'border-success/30 bg-success-soft/40 text-success-ink'
              : 'border-danger/30 bg-danger-soft/40 text-danger-ink',
          )}
        >
          {testResult.ok ? (
            <>
              <p>
                Connected in {testResult.latency_ms}ms
                {testResult.response && <> · replied &ldquo;{testResult.response}&rdquo;</>}
                {testResult.models_count !== undefined && (
                  <> · model found among {testResult.models_count} available</>
                )}
              </p>
              {testResult.note && (
                <p className="mt-1 text-xs opacity-80">{testResult.note}</p>
              )}
            </>
          ) : (
            <p>{testResult.error}</p>
          )}
        </div>
      )}

      {isAdmin && (
        <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-border pt-4">
          {testPath && (
            <Button variant="secondary" onClick={() => test.mutate()} loading={test.isPending}>
              <PlugZap />
              Test connection
            </Button>
          )}
          <Button
            variant="primary"
            disabled={!dirty}
            loading={save.isPending}
            onClick={() => save.mutate()}
          >
            Save changes
          </Button>
          {dirty && (
            <Button variant="ghost" onClick={() => setDraft({})}>
              Discard
            </Button>
          )}
          {save.isSuccess && !dirty && (
            <span className="text-sm text-success-ink">Saved</span>
          )}
        </div>
      )}
      {!isAdmin && (
        <p className="mt-4 border-t border-border pt-4 text-sm text-fg-subtle">
          Only administrators can change these.
        </p>
      )}
    </Card>
  );
}

export function LlmSettingsPage() {
  return (
    <SettingsForm
      title="Language model"
      description="Used to write summaries, detect action items and rank calendar and email matches."
      modelsPath="/llm/models"
      modelKey="llm_model"
      testPath="/llm/test"
      testKeyMap={{
        llm_base_url: 'base_url',
        llm_api_key: 'api_key',
        llm_model: 'model',
      }}
      keys={[
        { key: 'llm_base_url', label: 'Base URL', hint: 'OpenAI-compatible, ending in /v1' },
        { key: 'llm_api_key', label: 'API key' },
        {
          key: 'llm_model',
          label: 'Model',
          hint: 'Use the fully-qualified id from the dropdown (e.g. deepseek/deepseek-v4-flash) -- a bare "deepseek-v4-flash" is listed but not routable on some gateways.',
        },
        { key: 'llm_timeout_sec', label: 'Timeout (seconds)', type: 'number' },
        { key: 'llm_temperature', label: 'Temperature', type: 'number' },
      ]}
    />
  );
}

export function DiarizationSettingsPage() {
  return (
    <SettingsForm
      title="Diarization"
      description="The speech-to-text service that splits a recording into speakers and turns."
      modelsPath="/diarization/models"
      modelKey="diarization_model"
      testPath="/diarization/test"
      testKeyMap={{
        diarization_url: 'url',
        diarization_api_key: 'api_key',
        diarization_model: 'model',
      }}
      keys={[
        {
          key: 'diarization_url',
          label: 'Endpoint URL',
          hint: 'Full path, e.g. http://host:4012/v1/audio/diarization',
        },
        { key: 'diarization_api_key', label: 'API key', hint: 'Leave blank if not required' },
        { key: 'diarization_model', label: 'Model' },
        {
          key: 'diarization_timeout_sec',
          label: 'Timeout (seconds)',
          type: 'number',
          hint: 'A 20-minute recording can take several minutes.',
        },
      ]}
    />
  );
}

/* -------------------------------------------------------------------------- */
/* MCP                                                                         */
/* -------------------------------------------------------------------------- */

function McpServerCard({ server }: { server: McpServer }) {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Partial<McpServer>>({});
  const [testResult, setTestResult] = useState<{
    ok: boolean;
    error: string | null;
    tools: string[];
    latency_ms: number;
  } | null>(null);

  const merged = { ...server, ...draft };
  const dirty = Object.keys(draft).length > 0;

  const save = useMutation({
    mutationFn: () => api.put(`/mcp/servers/${server.name}`, draft),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['mcp-servers'] });
      setDraft({});
    },
  });

  const test = useMutation({
    mutationFn: () => api.post<typeof testResult>(`/mcp/servers/${server.name}/test`, draft),
    onSuccess: (data) => {
      setTestResult(data);
      void queryClient.invalidateQueries({ queryKey: ['mcp-servers'] });
    },
  });

  const last = server.last_test;

  return (
    <Card className="p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-lg font-semibold capitalize">{server.name}</h3>
          <p className="text-sm text-fg-subtle">
            Tool <code className="font-mono text-xs">{server.tool_name}</code>
          </p>
        </div>
        {last.ok === true && (
          <Badge variant="success">
            <CheckCircle2 className="size-3" aria-hidden /> Connected
          </Badge>
        )}
        {last.ok === false && (
          <Badge variant="danger">
            <XCircle className="size-3" aria-hidden /> Failing
          </Badge>
        )}
      </div>

      <div className="mt-4 space-y-3">
        <div>
          <Label htmlFor={`${server.name}-transport`}>Transport</Label>
          <Select
            id={`${server.name}-transport`}
            className="mt-1.5"
            value={merged.transport}
            disabled={!isAdmin}
            onChange={(e) =>
              setDraft((d) => ({ ...d, transport: e.target.value as 'sse' | 'stdio' }))
            }
          >
            <option value="sse">SSE (HTTP)</option>
            <option value="stdio">stdio (local process)</option>
          </Select>
          {merged.transport === 'stdio' && (
            <p className="mt-1 text-xs text-warning-ink">
              A stdio server cannot run inside the container — use SSE when deployed.
            </p>
          )}
        </div>

        {merged.transport === 'sse' ? (
          <>
            <div>
              <Label htmlFor={`${server.name}-url`}>Base URL</Label>
              <Input
                id={`${server.name}-url`}
                className="mt-1.5"
                value={merged.base_url ?? ''}
                disabled={!isAdmin}
                onChange={(e) => setDraft((d) => ({ ...d, base_url: e.target.value }))}
              />
            </div>
            <div>
              <Label htmlFor={`${server.name}-token`}>Token</Label>
              <Input
                id={`${server.name}-token`}
                className="mt-1.5"
                type="password"
                placeholder={server.has_token ? 'unchanged' : 'paste the token'}
                value={draft.auth_token ?? ''}
                disabled={!isAdmin}
                onChange={(e) => setDraft((d) => ({ ...d, auth_token: e.target.value }))}
              />
            </div>
          </>
        ) : (
          <>
            <div>
              <Label htmlFor={`${server.name}-cmd`}>Command</Label>
              <Input
                id={`${server.name}-cmd`}
                className="mt-1.5"
                value={merged.command ?? ''}
                disabled={!isAdmin}
                onChange={(e) => setDraft((d) => ({ ...d, command: e.target.value }))}
              />
            </div>
            <div>
              <Label htmlFor={`${server.name}-cwd`}>Working directory</Label>
              <Input
                id={`${server.name}-cwd`}
                className="mt-1.5"
                value={merged.cwd ?? ''}
                disabled={!isAdmin}
                onChange={(e) => setDraft((d) => ({ ...d, cwd: e.target.value }))}
              />
            </div>
          </>
        )}

        <div className="grid grid-cols-2 gap-3">
          <div>
            <Label htmlFor={`${server.name}-profile`}>Profile</Label>
            <Input
              id={`${server.name}-profile`}
              className="mt-1.5"
              value={merged.default_profile ?? ''}
              disabled={!isAdmin}
              onChange={(e) => setDraft((d) => ({ ...d, default_profile: e.target.value }))}
            />
          </div>
          <div>
            <Label htmlFor={`${server.name}-timeout`}>Timeout (s)</Label>
            <Input
              id={`${server.name}-timeout`}
              className="mt-1.5"
              type="number"
              value={merged.timeout_sec}
              disabled={!isAdmin}
              onChange={(e) => setDraft((d) => ({ ...d, timeout_sec: Number(e.target.value) }))}
            />
          </div>
        </div>
      </div>

      {(testResult || last.error) && (
        <div
          className={cn(
            'mt-4 rounded border p-3 text-sm',
            (testResult?.ok ?? last.ok)
              ? 'border-success/30 bg-success-soft/40 text-success-ink'
              : 'border-danger/30 bg-danger-soft/40 text-danger-ink',
          )}
        >
          {testResult?.ok || (!testResult && last.ok) ? (
            <p>
              Connected in {testResult?.latency_ms ?? '—'}ms ·{' '}
              {(testResult?.tools ?? last.tools).length} tools available
            </p>
          ) : (
            <p>{testResult?.error ?? last.error}</p>
          )}
        </div>
      )}

      {isAdmin && (
        <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-border pt-4">
          <Button variant="secondary" onClick={() => test.mutate()} loading={test.isPending}>
            <PlugZap />
            Test connection
          </Button>
          <Button
            variant="primary"
            disabled={!dirty}
            loading={save.isPending}
            onClick={() => save.mutate()}
          >
            Save
          </Button>
          {dirty && (
            <Button variant="ghost" onClick={() => setDraft({})}>
              Discard
            </Button>
          )}
        </div>
      )}
    </Card>
  );
}

function MyMcpProfileCard({ profile }: { profile: UserMcpProfile }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [profileDraft, setProfileDraft] = useState(profile.profile ?? '');
  const [tokenDraft, setTokenDraft] = useState('');
  const [testResult, setTestResult] = useState<{
    ok: boolean;
    error: string | null;
    tools: string[];
    latency_ms: number;
  } | null>(null);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['my-mcp-profiles'] });
  };

  const save = useMutation({
    mutationFn: () =>
      api.put<UserMcpProfile>(`/me/mcp-profiles/${profile.server_name}`, {
        profile: profileDraft,
        auth_token: tokenDraft || null,
      }),
    onSuccess: () => {
      invalidate();
      setEditing(false);
      setTokenDraft('');
    },
  });

  const clear = useMutation({
    mutationFn: () => api.del(`/me/mcp-profiles/${profile.server_name}`),
    onSuccess: () => {
      invalidate();
      setEditing(false);
    },
  });

  const test = useMutation({
    mutationFn: () =>
      api.post<typeof testResult>(`/me/mcp-profiles/${profile.server_name}/test`, {
        profile: profileDraft,
        auth_token: tokenDraft || null,
      }),
    onSuccess: setTestResult,
  });

  function startEditing() {
    setProfileDraft(profile.profile ?? '');
    setTokenDraft('');
    setTestResult(null);
    setEditing(true);
  }

  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-medium capitalize">{profile.server_name}</p>
          {profile.has_override ? (
            <p className="text-sm text-fg-subtle">
              You search as <span className="font-medium text-fg">{profile.profile}</span>
              {profile.has_personal_token && ' · using your own token'}
            </p>
          ) : (
            <p className="text-sm text-fg-subtle">
              Using the shared account (<span className="font-medium">{profile.shared_profile}</span>)
            </p>
          )}
        </div>
        {!editing && (
          <Button size="sm" variant="ghost" onClick={startEditing}>
            {profile.has_override ? 'Change' : 'Use my own account'}
          </Button>
        )}
      </div>

      {editing && (
        <div className="mt-3 space-y-3 border-t border-border pt-3">
          <div>
            <Label htmlFor={`my-${profile.server_name}-profile`}>Profile name</Label>
            <Input
              id={`my-${profile.server_name}-profile`}
              className="mt-1.5"
              value={profileDraft}
              onChange={(e) => setProfileDraft(e.target.value)}
              placeholder={profile.shared_profile ?? 'default'}
            />
            <p className="mt-1 text-xs text-fg-subtle">
              The account name your calendar/email administrator gave you (e.g. your username).
            </p>
          </div>
          <div>
            <Label htmlFor={`my-${profile.server_name}-token`}>Personal token (optional)</Label>
            <Input
              id={`my-${profile.server_name}-token`}
              className="mt-1.5"
              type="password"
              value={tokenDraft}
              onChange={(e) => setTokenDraft(e.target.value)}
              placeholder={
                profile.has_personal_token ? 'unchanged' : 'leave blank to use the shared token'
              }
            />
          </div>

          {testResult && (
            <div
              className={cn(
                'rounded border p-2 text-sm',
                testResult.ok
                  ? 'border-success/30 bg-success-soft/40 text-success-ink'
                  : 'border-danger/30 bg-danger-soft/40 text-danger-ink',
              )}
            >
              {testResult.ok
                ? `Connected in ${testResult.latency_ms}ms · ${testResult.tools.length} tools`
                : testResult.error}
            </div>
          )}
          {save.error && (
            <p className="text-sm text-danger-ink">{(save.error as Error).message}</p>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="secondary"
              onClick={() => test.mutate()}
              loading={test.isPending}
              disabled={!profileDraft}
            >
              <PlugZap />
              Test
            </Button>
            <Button
              size="sm"
              variant="primary"
              onClick={() => save.mutate()}
              loading={save.isPending}
              disabled={!profileDraft}
            >
              Save
            </Button>
            {profile.has_override && (
              <Button size="sm" variant="ghost" onClick={() => clear.mutate()} loading={clear.isPending}>
                Use shared account instead
              </Button>
            )}
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </Card>
  );
}

function MyMcpProfilesSection() {
  const profiles = useQuery({
    queryKey: ['my-mcp-profiles'],
    queryFn: () => api.get<UserMcpProfile[]>('/me/mcp-profiles'),
  });

  if (profiles.isLoading) return <Skeleton className="h-32 w-full" />;
  if (profiles.isError) return <ErrorState error={profiles.error} />;

  return (
    <Card className="p-5">
      <h2 className="font-display text-lg font-semibold">Your account</h2>
      <p className="mt-1 text-sm text-fg-subtle">
        By default everyone searches the same shared calendar and inbox below. If you have your
        own account on these servers, point your meetings at it here.
      </p>
      <div className="mt-4 space-y-3">
        {profiles.data!.map((p) => (
          <MyMcpProfileCard key={p.server_name} profile={p} />
        ))}
      </div>
    </Card>
  );
}

export function McpSettingsPage() {
  const { isAdmin } = useAuth();
  const servers = useQuery({
    queryKey: ['mcp-servers'],
    queryFn: () => api.get<McpServer[]>('/mcp/servers'),
  });

  return (
    <div className="space-y-4">
      <MyMcpProfilesSection />

      <div>
        <h2 className="mb-1 font-display text-lg font-semibold">
          {isAdmin ? 'Shared server configuration' : 'Shared servers'}
        </h2>
        <p className="mb-3 text-sm text-fg-subtle">
          {isAdmin
            ? 'How the app reaches each MCP server. This is the account everyone uses unless they set up their own above.'
            : 'How the app reaches each MCP server. Only administrators can change this.'}
        </p>

        {servers.isLoading && <Skeleton className="h-64 w-full" />}
        {servers.isError && <ErrorState error={servers.error} />}
        {servers.data && (
          <div className="space-y-4">
            {servers.data.map((server) => (
              <McpServerCard key={server.name} server={server} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Prompts                                                                     */
/* -------------------------------------------------------------------------- */

export function PromptSettingsPage() {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<string>('summary_prompt');
  const [body, setBody] = useState<string | null>(null);

  const list = useQuery({
    queryKey: ['prompts'],
    queryFn: () => api.get<PromptSummary[]>('/prompts'),
  });

  const detail = useQuery({
    queryKey: ['prompt', selected],
    queryFn: () => api.get<PromptDetail>(`/prompts/${selected}`),
    enabled: !!selected,
  });

  useEffect(() => {
    setBody(null);
  }, [selected]);

  const save = useMutation({
    mutationFn: () => api.put(`/prompts/${selected}`, { body }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['prompt', selected] });
      void queryClient.invalidateQueries({ queryKey: ['prompts'] });
      setBody(null);
    },
  });

  const value = body ?? detail.data?.body ?? '';
  const dirty = body !== null && body !== detail.data?.body;

  return (
    <Card className="p-5">
      <h2 className="font-display text-lg font-semibold">Prompts</h2>
      <p className="mt-1 text-sm text-fg-subtle">
        Edit and regenerate to compare. Every summary records the exact prompt text that
        produced it, so tuning this never rewrites history.
      </p>

      <div className="mt-4">
        <Label htmlFor="prompt-select">Prompt</Label>
        <Select
          id="prompt-select"
          className="mt-1.5"
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
        >
          {list.data?.map((p) => (
            <option key={p.name} value={p.name}>
              {p.name} (v{p.version})
            </option>
          ))}
        </Select>
      </div>

      {detail.data && (
        <>
          <p className="mt-3 text-xs text-fg-subtle">
            Required placeholders:{' '}
            {detail.data.required_placeholders.map((p) => (
              <code key={p} className="mr-1 font-mono">{`{{${p}}}`}</code>
            ))}
          </p>

          <Textarea
            className="mt-2 min-h-[420px] font-mono text-xs"
            spellCheck={false}
            value={value}
            disabled={!isAdmin}
            onChange={(e) => setBody(e.target.value)}
          />

          {save.error && (
            <p className="mt-2 text-sm text-danger-ink">{(save.error as Error).message}</p>
          )}

          {isAdmin && (
            <div className="mt-4 flex items-center gap-2 border-t border-border pt-4">
              <Button
                variant="primary"
                disabled={!dirty}
                loading={save.isPending}
                onClick={() => save.mutate()}
              >
                Save prompt
              </Button>
              {dirty && (
                <Button variant="ghost" onClick={() => setBody(null)}>
                  Discard
                </Button>
              )}
              {save.isSuccess && !dirty && (
                <span className="text-sm text-success-ink">Saved</span>
              )}
            </div>
          )}
        </>
      )}
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Users                                                                       */
/* -------------------------------------------------------------------------- */

export function UsersSettingsPage() {
  const queryClient = useQueryClient();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [isAdminNew, setIsAdminNew] = useState(false);
  const [tempPassword, setTempPassword] = useState<string | null>(null);

  const users = useQuery({
    queryKey: ['users'],
    queryFn: () => api.get<Paginated<User>>('/users', { page_size: 100 }),
  });

  const create = useMutation({
    mutationFn: () =>
      api.post<User>('/users', { username, password, is_admin: isAdminNew }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['users'] });
      setUsername('');
      setPassword('');
      setIsAdminNew(false);
    },
  });

  const reset = useMutation({
    mutationFn: (id: number) =>
      api.post<{ temporary_password: string | null }>(`/users/${id}/reset-password`, {}),
    onSuccess: (data) => setTempPassword(data.temporary_password),
  });

  const deactivate = useMutation({
    mutationFn: (id: number) => api.del(`/users/${id}`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['users'] }),
  });

  return (
    <div className="space-y-4">
      <Card className="p-5">
        <h2 className="font-display text-lg font-semibold">Users</h2>

        {users.isLoading && <Skeleton className="mt-4 h-32 w-full" />}
        {users.isError && <ErrorState error={users.error} className="mt-4" />}

        {users.data && (
          <ul className="mt-4 divide-y divide-border">
            {users.data.items.map((user) => (
              <li key={user.id} className="flex flex-wrap items-center gap-3 py-3">
                <div className="min-w-0 flex-1">
                  <p className="font-medium">
                    {user.display_name || user.username}
                    {user.is_admin && (
                      <Badge variant="primary" size="sm" className="ml-2">
                        admin
                      </Badge>
                    )}
                    {!user.is_active && (
                      <Badge variant="neutral" size="sm" className="ml-2">
                        inactive
                      </Badge>
                    )}
                    {user.must_change_password && (
                      <Badge variant="warning" size="sm" className="ml-2">
                        must change password
                      </Badge>
                    )}
                  </p>
                  <p className="text-xs text-fg-subtle">{user.username}</p>
                </div>

                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => reset.mutate(user.id)}
                  loading={reset.isPending}
                >
                  Reset password
                </Button>
                {user.is_active && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      if (window.confirm(`Deactivate ${user.username}?`)) {
                        deactivate.mutate(user.id);
                      }
                    }}
                  >
                    Deactivate
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}

        {tempPassword && (
          <div className="mt-4 rounded border border-warning/40 bg-warning-soft/40 p-3">
            <p className="text-sm font-medium text-warning-ink">
              Temporary password — shown once
            </p>
            <code className="mt-1 block font-mono text-sm">{tempPassword}</code>
            <Button size="xs" variant="ghost" className="mt-2" onClick={() => setTempPassword(null)}>
              Dismiss
            </Button>
          </div>
        )}
      </Card>

      <Card className="p-5">
        <h3 className="font-display text-base font-semibold">Add a user</h3>
        <form
          className="mt-3 space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <Label htmlFor="new-username">Username</Label>
              <Input
                id="new-username"
                className="mt-1.5"
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="new-password">Initial password</Label>
              <Input
                id="new-password"
                className="mt-1.5"
                type="password"
                required
                minLength={10}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
          </div>

          <label className="flex items-center gap-2 text-sm text-fg-muted">
            <input
              type="checkbox"
              checked={isAdminNew}
              onChange={(e) => setIsAdminNew(e.target.checked)}
              className="size-4 rounded border-border-strong"
            />
            Administrator
          </label>

          {create.error && (
            <p className="text-sm text-danger-ink">{(create.error as Error).message}</p>
          )}

          <Button type="submit" variant="primary" loading={create.isPending}>
            Create user
          </Button>
          <p className="text-xs text-fg-subtle">
            They will be asked to choose their own password at first sign in.
          </p>
        </form>
      </Card>
    </div>
  );
}
