import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { PlugZap, Plus, Trash2 } from 'lucide-react';
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
  Integration,
  IntegrationTestResult,
  Paginated,
  PromptDetail,
  PromptSummary,
  ProviderSpec,
  SettingEntry,
  User,
} from '@/types/api';

const TABS = [
  { to: '/settings/llm', label: 'LLM' },
  { to: '/settings/diarization', label: 'Diarization' },
  { to: '/settings/integrations', label: 'Integrations' },
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
/* Integrations                                                                */
/* -------------------------------------------------------------------------- */

const STATUS_BADGE: Record<
  Integration['status'],
  { label: string; variant: 'success' | 'warning' | 'danger' | 'neutral' }
> = {
  ok: { label: 'Connected', variant: 'success' },
  unverified: { label: 'Not tested', variant: 'neutral' },
  error: { label: 'Error', variant: 'danger' },
  reauth_required: { label: 'Reconnect needed', variant: 'warning' },
};

function CheckList({ result }: { result: IntegrationTestResult }) {
  return (
    <div
      className={cn(
        'mt-3 rounded border p-2 text-sm',
        result.ok
          ? 'border-success/30 bg-success-soft/40 text-success-ink'
          : 'border-danger/30 bg-danger-soft/40 text-danger-ink',
      )}
    >
      <p>{result.ok ? `Connected in ${result.latency_ms}ms` : result.error}</p>
      {/* Per-check detail, because a provider can be half-working: reaching a
          calendar while the mailbox login is rejected is a normal outcome. */}
      {result.checks.length > 1 && (
        <ul className="mt-1 space-y-0.5 text-xs">
          {result.checks.map((check) => (
            <li key={check.name}>
              {check.ok ? '✓' : '✕'} {check.name}
              {check.error ? ` — ${check.error}` : ''}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function IntegrationCard({ integration }: { integration: Integration }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [label, setLabel] = useState(integration.account_label ?? '');
  const [secret, setSecret] = useState('');
  const [result, setResult] = useState<IntegrationTestResult | null>(null);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['integrations'] });
    void queryClient.invalidateQueries({ queryKey: ['integrations', 'summary'] });
  };

  const save = useMutation({
    mutationFn: () =>
      api.patch<Integration>(`/integrations/${integration.id}`, {
        account_label: label,
        ...(secret ? { secret: { auth_token: secret } } : {}),
      }),
    onSuccess: () => {
      invalidate();
      setEditing(false);
      setSecret('');
    },
  });

  const toggle = useMutation({
    mutationFn: (patch: Partial<Integration>) =>
      api.patch<Integration>(`/integrations/${integration.id}`, patch),
    onSuccess: invalidate,
  });

  const disconnect = useMutation({
    mutationFn: () => api.del(`/integrations/${integration.id}`),
    onSuccess: invalidate,
  });

  const test = useMutation({
    mutationFn: () => api.post<IntegrationTestResult>(`/integrations/${integration.id}/test`, {}),
    onSuccess: (data) => {
      setResult(data);
      invalidate();
    },
    onError: (error) =>
      setResult({ ok: false, latency_ms: 0, checks: [], error: (error as Error).message }),
  });

  const badge = STATUS_BADGE[integration.status] ?? STATUS_BADGE.unverified;
  const canCalendar = integration.supported_kinds.includes('calendar');
  const canEmail = integration.supported_kinds.includes('email');

  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <p className="truncate font-medium">
              {integration.account_label || integration.account_key}
            </p>
            <Badge variant={badge.variant} size="sm">
              {badge.label}
            </Badge>
          </div>
          <p className="mt-0.5 truncate text-sm text-fg-subtle">
            {integration.provider_label}
            {integration.config.profile ? ` · ${String(integration.config.profile)}` : ''}
            {integration.config.base_url ? ` · ${String(integration.config.base_url)}` : ''}
          </p>
        </div>
        {!editing && (
          <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>
            Edit
          </Button>
        )}
      </div>

      {integration.status === 'reauth_required' && (
        <p className="mt-2 rounded bg-warning-soft/40 p-2 text-xs text-warning-ink">
          {integration.last_test.error ||
            'This account needs reconnecting before it can be searched again.'}
        </p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-4 text-sm">
        {canCalendar && (
          <label className="inline-flex cursor-pointer items-center gap-2">
            <input
              type="checkbox"
              className="size-4 rounded border-border-strong"
              checked={integration.calendar_enabled}
              onChange={(e) => toggle.mutate({ calendar_enabled: e.target.checked })}
            />
            Search calendar
          </label>
        )}
        {canEmail && (
          <label className="inline-flex cursor-pointer items-center gap-2">
            <input
              type="checkbox"
              className="size-4 rounded border-border-strong"
              checked={integration.email_enabled}
              onChange={(e) => toggle.mutate({ email_enabled: e.target.checked })}
            />
            Search email
          </label>
        )}
        <Button
          size="sm"
          variant="secondary"
          className="ml-auto"
          loading={test.isPending}
          onClick={() => test.mutate()}
        >
          <PlugZap />
          Test
        </Button>
      </div>

      {editing && (
        <div className="mt-3 space-y-3 border-t border-border pt-3">
          <div>
            <Label htmlFor={`int-${integration.id}-label`}>Name</Label>
            <Input
              id={`int-${integration.id}-label`}
              className="mt-1.5"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
            />
          </div>
          {integration.auth_type === 'token' && (
            <div>
              <Label htmlFor={`int-${integration.id}-secret`}>Token</Label>
              <Input
                id={`int-${integration.id}-secret`}
                className="mt-1.5"
                type="password"
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                placeholder={integration.secret_preview ?? 'not set'}
              />
              <p className="mt-1 text-xs text-fg-subtle">Leave blank to keep the current token.</p>
            </div>
          )}
          {save.error && <p className="text-sm text-danger-ink">{(save.error as Error).message}</p>}
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="primary" loading={save.isPending} onClick={() => save.mutate()}>
              Save
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="ml-auto text-danger-ink"
              loading={disconnect.isPending}
              onClick={() => disconnect.mutate()}
            >
              <Trash2 />
              Disconnect
            </Button>
          </div>
        </div>
      )}

      {result && <CheckList result={result} />}
    </Card>
  );
}

/** Connect an MCP-backed calendar or inbox. */
function AddMcpForm({ spec, onDone }: { spec: ProviderSpec; onDone: () => void }) {
  const queryClient = useQueryClient();
  const [baseUrl, setBaseUrl] = useState('');
  const [profile, setProfile] = useState('');
  const [token, setToken] = useState('');

  const create = useMutation({
    mutationFn: () =>
      api.post<Integration>('/integrations', {
        provider: spec.id,
        account_label: `${spec.label} (${profile || 'default'})`,
        config: {
          transport: 'sse',
          base_url: baseUrl.trim(),
          profile: profile.trim() || null,
          tool_name: spec.id === 'mcp_calendar' ? 'search_events' : 'search_emails',
        },
        ...(token ? { secret: { auth_token: token } } : {}),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['integrations'] });
      void queryClient.invalidateQueries({ queryKey: ['integrations', 'summary'] });
      onDone();
    },
  });

  return (
    <div className="space-y-3">
      <div>
        <Label htmlFor="add-base-url">Server URL</Label>
        <Input
          id="add-base-url"
          className="mt-1.5"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder="http://calendar-mcp.internal:4006"
        />
      </div>
      <div>
        <Label htmlFor="add-profile">Profile</Label>
        <Input
          id="add-profile"
          className="mt-1.5"
          value={profile}
          onChange={(e) => setProfile(e.target.value)}
          placeholder="your account name on that server"
        />
        <p className="mt-1 text-xs text-fg-subtle">
          Which calendar or inbox on that server belongs to you.
        </p>
      </div>
      <div>
        <Label htmlFor="add-token">Token</Label>
        <Input
          id="add-token"
          className="mt-1.5"
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
        />
      </div>
      {create.error && <p className="text-sm text-danger-ink">{(create.error as Error).message}</p>}
      <div className="flex items-center gap-2">
        <Button
          variant="primary"
          size="sm"
          loading={create.isPending}
          disabled={!baseUrl.trim()}
          onClick={() => create.mutate()}
        >
          Connect
        </Button>
        <Button variant="ghost" size="sm" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

/** Hand off to the provider's consent screen. */
function ConnectOAuth({ spec, onDone }: { spec: ProviderSpec; onDone: () => void }) {
  const start = useMutation({
    mutationFn: () => api.get<{ authorize_url: string }>(`/integrations/oauth/${spec.id}/start`),
    onSuccess: (data) => {
      // Full navigation, not a popup: the callback redirects back into the SPA,
      // and a popup would be blocked as often as not.
      window.location.href = data.authorize_url;
    },
  });

  return (
    <div className="space-y-3">
      <p className="text-sm text-fg-subtle">
        You will be sent to {spec.label} to approve read-only access to your calendar and mail,
        then returned here.
      </p>
      {start.error && (
        <p className="text-sm text-danger-ink">{(start.error as Error).message}</p>
      )}
      <div className="flex items-center gap-2">
        <Button variant="primary" size="sm" loading={start.isPending} onClick={() => start.mutate()}>
          Continue to {spec.label}
        </Button>
        <Button variant="ghost" size="sm" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

function AddIntegration({ providers }: { providers: ProviderSpec[] }) {
  const [picked, setPicked] = useState<ProviderSpec | null>(null);
  const [open, setOpen] = useState(false);

  if (!open) {
    return (
      <Button variant="secondary" onClick={() => setOpen(true)}>
        <Plus />
        Add integration
      </Button>
    );
  }

  const close = () => {
    setOpen(false);
    setPicked(null);
  };

  return (
    <Card className="p-4">
      <h3 className="font-display text-base font-semibold">
        {picked ? `Connect ${picked.label}` : 'What would you like to connect?'}
      </h3>

      {!picked ? (
        <div className="mt-3 space-y-2">
          {providers.map((spec) => (
            <button
              key={spec.id}
              type="button"
              onClick={() => setPicked(spec)}
              className="flex w-full items-center justify-between rounded border border-border p-3 text-left text-sm hover:bg-surface-2"
            >
              <span>
                <span className="font-medium">{spec.label}</span>
                <span className="ml-2 text-xs text-fg-subtle">{spec.kinds.join(' · ')}</span>
              </span>
              <span className="text-primary">Connect →</span>
            </button>
          ))}
          <Button variant="ghost" size="sm" onClick={close}>
            Cancel
          </Button>
        </div>
      ) : (
        <div className="mt-3">
          {picked.auth_type === 'oauth2' ? (
            <ConnectOAuth spec={picked} onDone={close} />
          ) : (
            <AddMcpForm spec={picked} onDone={close} />
          )}
        </div>
      )}
    </Card>
  );
}

/** App-level OAuth registration. Unavoidably shared: a provider will only
 *  redirect back to a URI registered against one client. Each user still
 *  authorises their own account against it. */
function OAuthClientSettings() {
  return (
    <SettingsForm
      title="Google sign-in (admin)"
      description={
        'Register one OAuth client at console.cloud.google.com, add ' +
        'PUBLIC_BASE_URL/api/integrations/oauth/google/callback as an authorised redirect ' +
        'URI, and set the consent screen to "In production" — while it is in Testing, ' +
        'Google expires refresh tokens after 7 days and everyone has to reconnect weekly.'
      }
      keys={[
        {
          key: 'public_base_url',
          label: 'Public base URL',
          hint: 'Where this app is reachable. Google only accepts https:// or http://localhost — a LAN IP will be rejected.',
        },
        { key: 'google_client_id', label: 'Google client ID' },
        { key: 'google_client_secret', label: 'Google client secret' },
      ]}
    />
  );
}

export function IntegrationsSettingsPage() {
  const { isAdmin } = useAuth();
  const integrations = useQuery({
    queryKey: ['integrations'],
    queryFn: () => api.get<Integration[]>('/integrations'),
  });
  const providers = useQuery({
    queryKey: ['integration-providers'],
    queryFn: () => api.get<ProviderSpec[]>('/integrations/providers'),
  });

  if (integrations.isLoading) return <Skeleton className="h-48 w-full" />;
  if (integrations.isError) return <ErrorState error={integrations.error} />;

  const mine = integrations.data ?? [];

  return (
    <div className="space-y-4">
      <Card className="p-5">
        <h2 className="font-display text-lg font-semibold">Your calendars and inboxes</h2>
        <p className="mt-1 text-sm text-fg-subtle">
          Meetings are matched against the accounts you connect here. They are yours alone —
          nobody else can see or search them.
        </p>

        {mine.length === 0 ? (
          <p className="mt-4 rounded border border-dashed border-border p-4 text-sm text-fg-subtle">
            Nothing connected yet. Until you add a calendar or an inbox, meetings cannot be
            matched to events or email.
          </p>
        ) : (
          <div className="mt-4 space-y-3">
            {mine.map((integration) => (
              <IntegrationCard key={integration.id} integration={integration} />
            ))}
          </div>
        )}
      </Card>

      <AddIntegration providers={providers.data ?? []} />

      {isAdmin && <OAuthClientSettings />}
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
