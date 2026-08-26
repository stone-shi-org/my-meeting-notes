import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { PlugZap, Plus, Trash2, X } from 'lucide-react';
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
import { restrictedLanguagesForModel } from '@/lib/recording';
import type {
  Integration,
  IntegrationTestResult,
  InsightTypeDetail,
  Paginated,
  PromptDetail,
  PromptSummary,
  ProviderSpec,
  SettingEntry,
  TelegramLink,
  User,
} from '@/types/api';

const TABS = [
  { to: '/settings/llm', label: 'LLM' },
  { to: '/settings/diarization', label: 'Diarization' },
  { to: '/settings/live-captions', label: 'Live captions' },
  { to: '/settings/web-search', label: 'Web Search' },
  { to: '/settings/integrations', label: 'Integrations' },
  { to: '/settings/matching', label: 'Matching' },
  { to: '/settings/telegram', label: 'Telegram' },
  { to: '/settings/prompt', label: 'Prompts' },
  { to: '/settings/insight-types', label: 'Meeting types', adminOnly: true },
  { to: '/settings/users', label: 'Users', adminOnly: true },
  // Only on a server with MMN_DEV_PROVIDER_ENABLED set. Detected by whether the
  // provider is offered at all rather than by a capability endpoint of its own:
  // the picker query is already running and already cached.
  { to: '/settings/development', label: 'Development', devOnly: true },
];

export function SettingsPage() {
  const { isAdmin } = useAuth();
  const providers = useQuery({
    queryKey: ['integration-providers'],
    queryFn: () => api.get<ProviderSpec[]>('/integrations/providers'),
  });
  const devEnabled = (providers.data ?? []).some((p) => p.id === 'dev');

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
          {TABS.filter(
            (t) => (!t.adminOnly || isAdmin) && (!t.devOnly || devEnabled),
          ).map((tab) => (
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
  testExtra,
}: {
  title: string;
  description?: string;
  keys: {
    key: string;
    label: string;
    /** Static, or computed from the current draft/saved settings -- e.g.
     * live_caption_language's own hint depends on whichever backend's model
     * is currently configured, not just this field's own value. */
    hint?: string | ((draft: Record<string, string>, entries: Record<string, { value?: unknown }>) => string | undefined);
    type?: string;
    step?: string;
    /** Static, or computed the same way `hint` above is -- a dropdown whose
     * choices depend on another field's current value (see
     * live_caption_language, driven by whichever backend's model is set).
     * A computed `undefined` means "no restriction known, fall back to free
     * text" -- distinct from an empty array, which would render a picklist
     * with nothing pickable. */
    options?:
      | { value: string; label: string }[]
      | ((
          draft: Record<string, string>,
          entries: Record<string, { value?: unknown }>,
        ) => { value: string; label: string }[] | undefined);
    badge?: (value: string) => { label: string; variant: 'info' | 'success' | 'warning' | 'neutral' | 'primary' | 'danger' } | null;
    visible?: (draft: Record<string, string>, entries: Record<string, { value?: unknown }>) => boolean;
    /** Greys the field out without hiding it -- for a setting that's saved
     * but unused given another field's current value (e.g. the chunk
     * settings while "Diarization only" is on), where hiding it would lose
     * the fact that a value is still sitting there, just not read right now. */
    disabled?: boolean;
    /** Replaces `hint` while `disabled` is true, explaining why. */
    disabledHint?: string;
  }[];
  modelsPath?: string;
  modelKey?: string;
  /** Endpoint that tests the connection this form configures. */
  testPath?: string;
  /** Maps a settings key (e.g. "llm_base_url") to the short field name the
   * test endpoint expects (e.g. "base_url"), so Test always exercises
   * whatever is currently in the form -- saved or not. */
  testKeyMap?: Record<string, string>;
  /** Extra fixed fields merged into the Test request body alongside
   * testKeyMap's, for an endpoint that needs to know which of several
   * things it's testing (e.g. /live-caption/test's `backend`) -- not itself
   * a settings key, so there's nothing to look up in `entries`/`draft`. */
  testExtra?: Record<string, unknown>;
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
  const modelOptions = (models.data?.models ?? []).map((m) => m.id);

  const save = useMutation({
    mutationFn: () => api.put('/settings', { values: draft }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['settings'] });
      setDraft({});
    },
  });

  const test = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = { ...testExtra };
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
      {description && <p className="mt-1 text-sm text-fg-subtle">{description}</p>}

      <div className="mt-5 space-y-4">
        {keys.map(({ key, label, hint, type, step, options, badge, visible, disabled: keyDisabled, disabledHint }) => {
          const entry = entries[key];
          if (!entry) return null;
          if (visible && !visible(draft, entries)) return null;

          const value = draft[key] ?? (entry.value ?? '');
          const isModelField = key === modelKey;
          const badgeObj = badge ? badge(String(value)) : null;
          const fieldDisabled = !isAdmin || !!keyDisabled;
          const resolvedHint = typeof hint === 'function' ? hint(draft, entries) : hint;
          const resolvedOptions = typeof options === 'function' ? options(draft, entries) : options;

          return (
            <div key={key}>
              <div className="flex items-center gap-2">
                <Label htmlFor={key}>{label}</Label>
                {badgeObj && (
                  <Badge variant={badgeObj.variant} size="sm">
                    {badgeObj.label}
                  </Badge>
                )}
              </div>

              {resolvedOptions ? (
                <Select
                  id={key}
                  className="mt-1.5"
                  value={String(value)}
                  disabled={fieldDisabled}
                  onChange={(e) => {
                    setDraft((d) => ({ ...d, [key]: e.target.value }));
                    setTestResult(null);
                  }}
                >
                  {resolvedOptions.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </Select>
              ) : entry.type === 'bool' ? (
                // A select rather than a checkbox: the rest of this form is a
                // draft you save, and a checkbox reads as taking effect on click.
                <Select
                  id={key}
                  className="mt-1.5"
                  value={String(value) === 'true' ? 'true' : 'false'}
                  disabled={fieldDisabled}
                  onChange={(e) => setDraft((d) => ({ ...d, [key]: e.target.value }))}
                >
                  <option value="true">On</option>
                  <option value="false">Off</option>
                </Select>
              ) : (
                <>
                  {/* A model field is a plain text box with the catalog behind a
                      datalist, not a select: the list runs to hundreds of ids on
                      a gateway, which is unpickable by scrolling. Typing filters
                      it, and an id the catalog doesn't have is still typeable. */}
                  <Input
                    id={key}
                    className="mt-1.5"
                    type={entry.is_secret ? 'password' : (type ?? 'text')}
                    step={step}
                    value={String(value)}
                    disabled={fieldDisabled}
                    placeholder={entry.is_secret ? 'unchanged' : undefined}
                    list={isModelField && modelOptions.length ? `${key}-options` : undefined}
                    autoComplete={isModelField ? 'off' : undefined}
                    onChange={(e) => {
                      setDraft((d) => ({ ...d, [key]: e.target.value }));
                      setTestResult(null);
                    }}
                  />
                  {isModelField && modelOptions.length > 0 && (
                    <datalist id={`${key}-options`}>
                      {modelOptions.map((id) => (
                        <option key={id} value={id} />
                      ))}
                    </datalist>
                  )}
                </>
              )}

              {keyDisabled && disabledHint ? (
                <p className="mt-1 text-xs text-fg-faint">{disabledHint}</p>
              ) : (
                resolvedHint && <p className="mt-1 text-xs text-fg-subtle">{resolvedHint}</p>
              )}

              {isModelField && modelOptions.length > 0 && (
                <p className="mt-1 text-xs text-fg-subtle">
                  {modelOptions.length} models available — start typing to filter, or clear
                  the box to see them all.
                </p>
              )}
              {isModelField && models.data?.error && (
                <p className="mt-1 text-xs text-warning-ink">
                  Could not list models ({models.data.error}). Type the id in full.
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

/**
 * Which models the AI chat panels (thread and transcript) let people pick
 * from, in addition to `llm_model` above (always implicitly allowed -- see
 * `llm_svc.enabled_chat_models`). A separate small form rather than a
 * `SettingsForm` field: it's list-valued, everything else here is scalar.
 */
function ChatModelsField() {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const settings = useSettings();
  const [draft, setDraft] = useState<string[] | null>(null);
  const [customModel, setCustomModel] = useState('');

  const models = useQuery({
    queryKey: ['models', '/llm/models'],
    queryFn: () => api.get<{ models: { id: string }[]; error: string | null }>('/llm/models'),
    staleTime: 300_000,
  });

  const entry = settings.data?.settings.llm_chat_models;
  const saved = Array.isArray(entry?.value) ? entry.value : [];
  const enabled = draft ?? saved;

  const save = useMutation({
    mutationFn: (values: string[]) =>
      api.put('/settings', { values: { llm_chat_models: values } }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['settings'] });
      setDraft(null);
    },
  });

  if (settings.isLoading) return <Skeleton className="h-32 w-full" />;

  function remove(id: string) {
    setDraft(enabled.filter((m) => m !== id));
  }

  function addCustom() {
    const id = customModel.trim();
    if (!id || enabled.includes(id)) return;
    setDraft([...enabled, id]);
    setCustomModel('');
  }

  // The suggestions only offer what isn't picked yet -- an id already in the
  // list below would be a no-op choice. Enabled ids the catalog no longer
  // lists still keep their chip, so a model disabled server-side doesn't
  // silently vanish from the saved value.
  const available = (models.data?.models ?? [])
    .map((m) => m.id)
    .filter((id) => !enabled.includes(id));

  return (
    <Card className="p-5">
      <h2 className="font-display text-lg font-semibold">Chat models</h2>
      <p className="mt-1 text-sm text-fg-subtle">
        Models people can choose between in the AI chat panels (thread and transcript chat).
        The language model above is always available too.
      </p>

      <div className="mt-4 flex min-h-9 flex-wrap items-center gap-1.5 rounded border border-border bg-surface-2 p-2">
        {enabled.length === 0 && (
          <span className="px-1 text-sm text-fg-subtle">No models chosen yet.</span>
        )}
        {enabled.map((id) => (
          <Badge key={id} variant="outline" className="bg-surface py-1 pl-2 pr-1 text-sm">
            {id}
            {isAdmin && (
              <button
                type="button"
                aria-label={`Remove ${id}`}
                onClick={() => remove(id)}
                className="rounded-sm p-0.5 text-fg-subtle hover:bg-surface-3 hover:text-fg focus-visible:outline-none focus-visible:ring-2"
              >
                <X className="size-3.5" />
              </button>
            )}
          </Badge>
        ))}
      </div>

      {isAdmin && (
        <div className="mt-3">
          <Label htmlFor="chat-model-add">Add a model</Label>
          <div className="mt-1 flex gap-2">
            <Input
              id="chat-model-add"
              value={customModel}
              onChange={(e) => setCustomModel(e.target.value)}
              placeholder="Type a model id, e.g. deepseek/deepseek-v4-flash"
              list={available.length ? 'chat-model-options' : undefined}
              autoComplete="off"
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  addCustom();
                }
              }}
            />
            {available.length > 0 && (
              <datalist id="chat-model-options">
                {available.map((id) => (
                  <option key={id} value={id} />
                ))}
              </datalist>
            )}
            <Button variant="secondary" onClick={addCustom}>
              <Plus />
              Add
            </Button>
          </div>
          {available.length > 0 && (
            <p className="mt-1 text-xs text-fg-subtle">
              {available.length} models available — start typing to filter. An id the catalog
              doesn't list works too.
            </p>
          )}
        </div>
      )}

      {models.data?.error && (
        <p className="mt-2 text-xs text-warning-ink">
          Could not list models ({models.data.error}). Add ids manually.
        </p>
      )}

      {isAdmin && (
        <div className="mt-4 flex items-center gap-3 border-t border-border pt-4">
          <Button
            variant="primary"
            disabled={draft === null}
            loading={save.isPending}
            onClick={() => save.mutate(enabled)}
          >
            Save changes
          </Button>
          {draft !== null && (
            <Button variant="ghost" onClick={() => setDraft(null)}>
              Discard
            </Button>
          )}
          {save.isSuccess && draft === null && (
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
    <div className="space-y-4">
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
            hint: 'Use the fully-qualified id from the suggestions (e.g. deepseek/deepseek-v4-flash) -- a bare "deepseek-v4-flash" is listed but not routable on some gateways.',
          },
          { key: 'llm_timeout_sec', label: 'Timeout (seconds)', type: 'number' },
          { key: 'llm_temperature', label: 'Temperature', type: 'number' },
        ]}
      />
      <SettingsForm
        title="Insights model"
        description="Live meeting insights during a recording (see the Insights panel while recording) -- a separate, optionally cheaper/faster model, called every few seconds while that panel is open. Reuses the base URL and API key above."
        modelsPath="/llm/models"
        modelKey="insights_model"
        testPath="/llm/test"
        testKeyMap={{
          llm_base_url: 'base_url',
          llm_api_key: 'api_key',
          insights_model: 'model',
        }}
        keys={[
          {
            key: 'insights_model',
            label: 'Model',
            hint: 'Leave blank to turn the Insights panel off.',
          },
          { key: 'insights_interval_sec', label: 'Analysis interval (seconds)', type: 'number' },
        ]}
      />
      <ChatModelsField />
    </div>
  );
}

/**
 * "Diarization only" gates two other sections on this page (the Transcription
 * form below, and whether the chunk settings do anything), so unlike every
 * other field here it saves immediately on click instead of sitting in a
 * draft -- half-toggling it without saving would leave the visible fields
 * not matching what's actually configured. A real checkbox, not the
 * Select-as-bool convention the rest of the page uses, for the same reason:
 * it needs to read as taking effect immediately, because it does.
 */
function DiarizeOnlyToggle() {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const settings = useSettings();
  const entry = settings.data?.settings.diarize_only;
  const checked = String(entry?.value) === 'true';

  const toggle = useMutation({
    mutationFn: (next: boolean) => api.put('/settings', { values: { diarize_only: next } }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['settings'] }),
  });

  if (settings.isLoading) return <Skeleton className="h-20 w-full" />;

  return (
    <Card className="p-5">
      <label className="flex items-start gap-3">
        <input
          type="checkbox"
          className="mt-1 size-4 shrink-0 accent-primary"
          checked={checked}
          disabled={!isAdmin || toggle.isPending}
          onChange={(e) => toggle.mutate(e.target.checked)}
        />
        <span>
          <span className="font-medium text-fg">Diarization only</span>
          <p className="mt-0.5 text-sm text-fg-subtle">
            Check this if the diarization service below only splits speaker turns and never
            fills in the words -- confirmed on pyannote/speaker-diarization-community-1, which
            returns an empty transcript on every turn by design. A separate transcription
            service then supplies the text, matched to each turn by timestamp overlap. This
            path handles a recording of any length in one request, so the chunk settings
            below go unused while it's on.
          </p>
        </span>
      </label>
    </Card>
  );
}

export function DiarizationSettingsPage() {
  const settings = useSettings();
  const diarizeOnly = String(settings.data?.settings.diarize_only?.value) === 'true';

  return (
    <div className="space-y-4">
      <DiarizeOnlyToggle />

      <SettingsForm
        title="Diarization"
        description={
          diarizeOnly
            ? 'With "Diarization only" on above, this service only needs to produce speaker turns -- the words come from Transcription below.'
            : 'The speech-to-text service that splits a recording into speakers and turns.'
        }
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
          {
            key: 'diarize_chunk_threshold_sec',
            label: 'Chunk if longer than (seconds)',
            type: 'number',
            hint:
              'A recording past this length is split and diarized in pieces so one request ' +
              'never risks the model\'s own output budget (see meeting 24: a real ~59 minute ' +
              'recording that overran it). 3000 = 50 minutes.',
            disabled: diarizeOnly,
            disabledHint:
              'Unused while "Diarization only" is on -- that path handles any length in one request.',
          },
          {
            key: 'diarize_chunk_size_sec',
            label: 'Chunk size (seconds)',
            type: 'number',
            hint: 'How long each piece is once chunking kicks in. 1500 = 25 minutes.',
            disabled: diarizeOnly,
            disabledHint: 'Unused while "Diarization only" is on.',
          },
        ]}
      />

      {diarizeOnly && (
        <SettingsForm
          title="Transcription"
          description={
            'Supplies the words the diarization service above doesn\'t. Combined with its ' +
            'speaker turns by timestamp overlap; a stretch with no matching turn at all -- the ' +
            'signature of a model hallucinating during silence, confirmed on whisper-large-' +
            'turbo-q8_0 -- is dropped rather than shown unattributed.'
          }
          modelsPath="/transcribe/models"
          modelKey="transcribe_model"
          testPath="/transcribe/test"
          testKeyMap={{
            transcribe_url: 'url',
            transcribe_api_key: 'api_key',
            transcribe_model: 'model',
          }}
          keys={[
            {
              key: 'transcribe_url',
              label: 'Endpoint URL',
              hint: 'Full path, e.g. http://host:4012/v1/audio/transcriptions',
            },
            { key: 'transcribe_api_key', label: 'API key', hint: 'Leave blank if not required' },
            { key: 'transcribe_model', label: 'Model' },
            {
              key: 'transcribe_timeout_sec',
              label: 'Timeout (seconds)',
              type: 'number',
              hint: 'A 20-minute recording can take several minutes.',
            },
          ]}
        />
      )}
    </div>
  );
}

/**
 * Endpoint type is its own immediate-save control, same pattern as
 * DiarizeOnlyToggle above -- it gates which of the three per-backend forms
 * below is shown, and a half-saved draft value would leave the visible form
 * not matching what's actually configured.
 */
function LiveCaptionBackendSelect() {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const settings = useSettings();
  const value = String(settings.data?.settings.live_caption_backend?.value ?? 'live_stt');

  const save = useMutation({
    mutationFn: (next: string) => api.put('/settings', { values: { live_caption_backend: next } }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['settings'] }),
  });

  if (settings.isLoading) return <Skeleton className="h-24 w-full" />;

  return (
    <Card className="p-5">
      <Label htmlFor="live_caption_backend">Endpoint type</Label>
      <Select
        id="live_caption_backend"
        className="mt-2"
        value={value}
        disabled={!isAdmin || save.isPending}
        onChange={(e) => save.mutate(e.target.value)}
      >
        <option value="live_stt">Live STT (gRPC)</option>
        <option value="realtime">OpenAI /v1/realtime (WebSocket)</option>
        <option value="transcriptions">/v1/audio/transcriptions (periodic POST)</option>
      </Select>
    </Card>
  );
}

export function LiveCaptionsSettingsPage() {
  const settings = useSettings();
  const backend = String(settings.data?.settings.live_caption_backend?.value ?? 'live_stt');

  return (
    <div className="space-y-4">
      <SettingsForm
        title="Live captions"
        keys={[
          {
            key: 'live_caption_enabled',
            label: 'Show live captions while recording',
          },
          {
            key: 'live_caption_language',
            label: 'Language',
            // See restrictedLanguagesForModel's doc comment: undefined for
            // a model with no known restriction falls back to free text.
            options: (_draft, entries) => {
              const model = String(entries[`live_caption_${backend}_model`]?.value ?? '');
              return restrictedLanguagesForModel(model)?.map(({ code, label }) => ({ value: code, label }));
            },
          },
          {
            key: 'live_caption_commit_interval_sec',
            label: 'Commit interval (seconds)',
            type: 'number',
            visible: () => backend === 'realtime' || backend === 'transcriptions',
          },
          {
            key: 'live_caption_timeout_sec',
            label: 'Connect timeout (seconds)',
            type: 'number',
          },
        ]}
      />

      <LiveCaptionBackendSelect />

      {backend === 'live_stt' && (
        <SettingsForm
          title="Live STT (gRPC)"
          modelsPath="/live-caption/models/live_stt"
          modelKey="live_caption_live_stt_model"
          testPath="/live-caption/test"
          testExtra={{ backend: 'live_stt' }}
          testKeyMap={{
            live_caption_live_stt_url: 'url',
            live_caption_live_stt_api_key: 'api_key',
            live_caption_live_stt_model: 'model',
          }}
          keys={[
            { key: 'live_caption_live_stt_url', label: 'Endpoint URL' },
            { key: 'live_caption_live_stt_api_key', label: 'API key' },
            { key: 'live_caption_live_stt_model', label: 'Model' },
          ]}
        />
      )}

      {backend === 'realtime' && (
        <SettingsForm
          title="OpenAI /v1/realtime (WebSocket)"
          modelsPath="/live-caption/models/realtime"
          modelKey="live_caption_realtime_model"
          testPath="/live-caption/test"
          testExtra={{ backend: 'realtime' }}
          testKeyMap={{
            live_caption_realtime_url: 'url',
            live_caption_realtime_api_key: 'api_key',
            live_caption_realtime_model: 'model',
          }}
          keys={[
            { key: 'live_caption_realtime_url', label: 'Endpoint URL' },
            { key: 'live_caption_realtime_api_key', label: 'API key' },
            { key: 'live_caption_realtime_model', label: 'Model' },
          ]}
        />
      )}

      {backend === 'transcriptions' && (
        <SettingsForm
          title="/v1/audio/transcriptions (periodic POST)"
          modelsPath="/live-caption/models/transcriptions"
          modelKey="live_caption_transcriptions_model"
          testPath="/live-caption/test"
          testExtra={{ backend: 'transcriptions' }}
          testKeyMap={{
            live_caption_transcriptions_url: 'url',
            live_caption_transcriptions_api_key: 'api_key',
            live_caption_transcriptions_model: 'model',
          }}
          keys={[
            { key: 'live_caption_transcriptions_url', label: 'Endpoint URL' },
            { key: 'live_caption_transcriptions_api_key', label: 'API key' },
            { key: 'live_caption_transcriptions_model', label: 'Model' },
          ]}
        />
      )}
    </div>
  );
}



export function WebSearchSettingsPage() {
  return (
    <SettingsForm
      title="Web search"
      description="Lets the AI chat (home screen and thread) search the public web for things not in your threads or calendar."
      testPath="/web-search/test"
      testKeyMap={{ web_search_base_url: 'base_url', web_search_api_key: 'api_key' }}
      keys={[
        {
          key: 'web_search_base_url',
          label: 'Base URL',
          hint: 'Searches {base URL}/v1/search',
        },
        { key: 'web_search_api_key', label: 'API key' },
        { key: 'web_search_timeout_sec', label: 'Timeout (seconds)', type: 'number' },
      ]}
    />
  );
}

export function MatchingSettingsPage() {
  return (
    <div className="space-y-4">
      <SettingsForm
        title="Matching"
        description="How far around a meeting the search for related email reaches."
        keys={[
          {
            key: 'match_window_days_before',
            label: 'Days before',
            type: 'number',
            hint: 'How far back from the meeting to search.',
          },
          { key: 'match_window_days_after', label: 'Days after', type: 'number' },
          {
            key: 'match_max_candidates',
            label: 'Maximum candidates',
            type: 'number',
            hint: 'Per kind, nearest in time first. Everything found is ranked, so a big number costs LLM tokens on every match.',
          },
          { key: 'match_max_keywords', label: 'Maximum keywords', type: 'number' },
        ]}
      />

      <SettingsForm
        title="Calendar matching window"
        description="How far around a meeting the calendar search reaches. Kept separate from email: a wider date range costs a calendar provider nothing extra, and interviews and appointments get booked much further out than an email ever goes unanswered."
        keys={[
          {
            key: 'match_window_calendar_days_before',
            label: 'Days before',
            type: 'number',
            hint: 'How far back from the meeting to search calendars.',
          },
          {
            key: 'match_window_calendar_days_after',
            label: 'Days after',
            type: 'number',
            hint: 'How far ahead of the meeting to search calendars.',
          },
        ]}
      />

      <SettingsForm
        title="Automatic follow-ups"
        description={
          'On a timer, every thread is re-searched and anything the ranker is confident ' +
          'about is attached on its own, marked unread until someone opens it. It never ' +
          'attaches to a meeting, so a summary is never rewritten by something nobody ' +
          'confirmed — and when the language model is unavailable it attaches nothing at all.'
        }
        keys={[
          {
            key: 'auto_match_enabled',
            label: 'Watch threads for follow-ups',
            hint: 'Off by default. It spends language-model and provider quota on its own schedule.',
          },
          {
            key: 'auto_match_interval_minutes',
            label: 'Check each thread every (minutes)',
            type: 'number',
            hint: 'Per thread, not globally. 30 means each thread is looked at twice an hour.',
          },
          {
            key: 'auto_match_threshold',
            label: 'Confidence to attach (0–1)',
            type: 'number',
            step: '0.05',
            hint: 'Deliberately above the 0.6 used to *suggest* a match. Attaching while nobody is watching deserves a higher bar. Below about 0.7 expect noise.',
          },
          {
            key: 'auto_match_max_threads_per_cycle',
            label: 'Threads per cycle',
            type: 'number',
            hint: 'Bounds one sweep. Whatever is skipped is first in line next time, so nothing is starved.',
          },
          {
            key: 'auto_match_idle_days',
            label: 'Stop watching after (days idle)',
            type: 'number',
            hint: 'A thread nobody has touched in this long has no follow-ups coming.',
          },
        ]}
      />
    </div>
  );
}

/**
 * One user's own Telegram link: generate a one-time code, send it to the bot
 * from Telegram, done. This is the only way a bot can learn someone's chat id
 * -- there's nowhere to send a code *to* until they've made contact -- so it
 * doubles as the verification step, not just onboarding polish: whoever sends
 * a given code is who that code's chat id gets linked to, and Telegram itself
 * sets the sender on every message, which can't be spoofed.
 */
function MyTelegramCard() {
  const queryClient = useQueryClient();
  const link = useQuery({
    queryKey: ['my-telegram'],
    queryFn: () => api.get<TelegramLink>('/auth/me/telegram'),
    // While a code is pending, poll so linking completes live the moment the
    // user sends /start <code> from their phone -- no manual refresh needed.
    refetchInterval: (query) => (query.state.data?.pending_code ? 3000 : false),
  });

  const generateCode = useMutation({
    mutationFn: () => api.post('/auth/me/telegram/link-code'),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['my-telegram'] }),
  });

  const unlink = useMutation({
    mutationFn: () => api.del('/auth/me/telegram'),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['my-telegram'] }),
  });

  const savePrefs = useMutation({
    mutationFn: (values: Omit<TelegramLink, 'linked' | 'linked_at' | 'pending_code' | 'pending_code_expires_at'>) =>
      api.put('/auth/me/telegram/preferences', values),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['my-telegram'] }),
  });

  if (link.isLoading) return <Skeleton className="h-48 w-full" />;
  if (link.isError) return <ErrorState error={link.error} />;
  const data = link.data!;

  const prefKeys: { key: keyof TelegramLink; label: string }[] = [
    { key: 'notify_new_attachments', label: 'Notify on new attachments' },
    { key: 'notify_next_steps', label: 'Notify on new next steps' },
    { key: 'notify_transcript_ready', label: 'Notify when a transcript is ready' },
    { key: 'notify_transcript_failed', label: 'Notify when a transcript fails' },
  ];

  function togglePref(key: keyof TelegramLink, checked: boolean) {
    savePrefs.mutate({
      notify_new_attachments: data.notify_new_attachments,
      notify_next_steps: data.notify_next_steps,
      notify_transcript_ready: data.notify_transcript_ready,
      notify_transcript_failed: data.notify_transcript_failed,
      [key]: checked,
    });
  }

  return (
    <Card className="p-5">
      <h2 className="font-display text-lg font-semibold">Your Telegram</h2>
      <p className="mt-1 text-sm text-fg-subtle">
        Link your own Telegram account to chat with the AI about your threads and get
        notifications for your own work.
      </p>

      {data.linked ? (
        <div className="mt-4 space-y-4">
          <p className="text-sm text-success-ink">
            Connected
            {data.linked_at && ` since ${new Date(data.linked_at).toLocaleString()}`}.
          </p>
          <div className="space-y-2">
            {prefKeys.map(({ key, label }) => (
              <label key={key} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={Boolean(data[key])}
                  onChange={(e) => togglePref(key, e.target.checked)}
                  className="size-4 rounded border-border-strong"
                />
                {label}
              </label>
            ))}
          </div>
          <Button variant="secondary" onClick={() => unlink.mutate()} loading={unlink.isPending}>
            Disconnect
          </Button>
        </div>
      ) : data.pending_code ? (
        <div className="mt-4 space-y-2">
          <p className="text-sm">
            Send this to the bot:{' '}
            <code className="rounded bg-surface-2 px-1.5 py-0.5 font-mono">
              /start {data.pending_code}
            </code>
          </p>
          <p className="text-xs text-fg-subtle">
            Expires{' '}
            {data.pending_code_expires_at
              ? new Date(data.pending_code_expires_at).toLocaleTimeString()
              : 'soon'}
            . Don&rsquo;t share this code — anyone who sends it links their own Telegram to your
            account.
          </p>
          <Button
            variant="secondary"
            onClick={() => generateCode.mutate()}
            loading={generateCode.isPending}
          >
            Generate a new code
          </Button>
        </div>
      ) : (
        <Button
          className="mt-4"
          variant="primary"
          onClick={() => generateCode.mutate()}
          loading={generateCode.isPending}
        >
          Generate a linking code
        </Button>
      )}
    </Card>
  );
}

export function TelegramSettingsPage() {
  return (
    <div className="space-y-4">
      <SettingsForm
        title="Telegram bot"
        description="The bot every account links their own Telegram to. One bot for everyone; who it talks to is set below, not here."
        testPath="/telegram/test"
        testKeyMap={{ telegram_bot_token: 'bot_token' }}
        keys={[
          { key: 'telegram_enabled', label: 'Enabled' },
          { key: 'telegram_bot_token', label: 'Bot token', hint: 'From @BotFather' },
        ]}
      />
      <MyTelegramCard />
    </div>
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

/** Apple ID + app-specific password. iCloud offers no OAuth, so this is the
 *  only route available, not a shortcut. */
function AddAppleForm({ spec, onDone }: { spec: ProviderSpec; onDone: () => void }) {
  const queryClient = useQueryClient();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [imapUsername, setImapUsername] = useState('');

  const create = useMutation({
    mutationFn: () =>
      api.post<Integration>('/integrations', {
        provider: spec.id,
        account_label: username.trim(),
        config: {
          username: username.trim(),
          ...(imapUsername.trim() ? { imap_username: imapUsername.trim() } : {}),
        },
        secret: { username: username.trim(), password },
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
        <Label htmlFor="apple-user">Apple ID</Label>
        <Input
          id="apple-user"
          className="mt-1.5"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="you@icloud.com"
        />
      </div>
      <div>
        <Label htmlFor="apple-pw">App-specific password</Label>
        <Input
          id="apple-pw"
          className="mt-1.5"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="xxxx-xxxx-xxxx-xxxx"
        />
        <p className="mt-1 text-xs text-fg-subtle">
          Not your Apple ID password — iCloud rejects that. Generate one at{' '}
          <a
            href={spec.docs_url || 'https://appleid.apple.com/account/manage'}
            target="_blank"
            rel="noopener noreferrer"
            className="text-primary hover:underline"
          >
            appleid.apple.com
          </a>{' '}
          under Sign-In and Security.
        </p>
      </div>
      <div>
        <Label htmlFor="apple-imap">iCloud email address (optional)</Label>
        <Input
          id="apple-imap"
          className="mt-1.5"
          value={imapUsername}
          onChange={(e) => setImapUsername(e.target.value)}
          placeholder="you@icloud.com"
        />
        <p className="mt-1 text-xs text-fg-subtle">
          Only needed if your Apple ID is not itself an @icloud.com address. Calendar works with
          the Apple ID, but iCloud Mail will only accept the account&rsquo;s own iCloud address.
        </p>
      </div>
      {create.error && <p className="text-sm text-danger-ink">{(create.error as Error).message}</p>}
      <div className="flex items-center gap-2">
        <Button
          variant="primary"
          size="sm"
          loading={create.isPending}
          disabled={!username.trim() || !password}
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

/** Connecting the Development provider, which has nothing to authenticate.
 *
 * A name is the whole form: the "account" is just a label for a set of rows in
 * this app's own database. The label is also what its account_key is slugged
 * from, snapshotted at create time -- so two fixture sets need two names, and
 * connecting the same name twice is a conflict rather than a duplicate row.
 */
function AddDevForm({ spec, onDone }: { spec: ProviderSpec; onDone: () => void }) {
  const queryClient = useQueryClient();
  const [label, setLabel] = useState('Fixtures');

  const create = useMutation({
    mutationFn: () =>
      api.post<Integration>('/integrations', {
        provider: spec.id,
        account_label: label.trim(),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['integrations'] });
      void queryClient.invalidateQueries({ queryKey: ['integrations', 'summary'] });
      onDone();
    },
  });

  return (
    <div className="space-y-3">
      <p className="text-sm text-fg-subtle">
        A calendar and inbox you write yourself. No credentials — the data lives in this app, and
        you fill it in under <strong>Settings → Development</strong> once this is connected.
      </p>

      <div>
        <Label htmlFor="dev-label">Name</Label>
        <Input
          id="dev-label"
          className="mt-1.5"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="Fixtures"
        />
        <p className="mt-1 text-xs text-fg-subtle">
          Only to tell one set of fake data from another. Add a second account under a different
          name if you want two independent inboxes.
        </p>
      </div>

      {create.error && (
        <p role="alert" className="text-sm text-danger-ink">
          {(create.error as Error).message}
        </p>
      )}

      <div className="flex gap-2">
        <Button
          variant="primary"
          loading={create.isPending}
          disabled={!label.trim()}
          onClick={() => create.mutate()}
        >
          Connect
        </Button>
        <Button variant="ghost" onClick={onDone}>
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
          ) : picked.auth_type === 'password' ? (
            <AddAppleForm spec={picked} onDone={close} />
          ) : picked.auth_type === 'none' ? (
            <AddDevForm spec={picked} onDone={close} />
          ) : (
            // 'token' -- the MCP servers, the only thing left that wants a URL.
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

function ZohoClientSettings() {
  return (
    <SettingsForm
      title="Zoho sign-in (admin)"
      description={
        'Register a client at api-console.zoho.com with the same callback path. ' +
        'Zoho is regional — the data centre must match where the accounts live, or ' +
        'requests authenticate fine and return nothing.'
      }
      keys={[
        { key: 'zoho_client_id', label: 'Zoho client ID' },
        { key: 'zoho_client_secret', label: 'Zoho client secret' },
        {
          key: 'zoho_dc',
          label: 'Data centre',
          hint: 'The suffix of your Zoho domain: com, eu, in, com.au or jp.',
        },
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
      {isAdmin && <ZohoClientSettings />}
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
/* Meeting types (insight_types)                                              */
/* -------------------------------------------------------------------------- */

// Mirrors app/services/insight_types.py's REQUIRED_PLACEHOLDERS -- shown as
// a hint rather than fetched, since it's a fixed fact about every type now,
// not something the server needs a round trip to say.
const REQUIRED_PLACEHOLDERS = ['previous_topics', 'previous_questions', 'previous_action_items'];

function insightTypesQueryKey() {
  return ['insight-types', 'admin'];
}

function InsightTypeCard({ type }: { type: InsightTypeDetail }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(type.name);
  const [prompt, setPrompt] = useState(type.prompt);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: insightTypesQueryKey() });
    // The recorder's own picker (see InsightsPanel.tsx) reads the public
    // list under a different key -- a rename should show up there without
    // waiting for that panel's own staleTime to lapse.
    void queryClient.invalidateQueries({ queryKey: ['insight-types'] });
  };

  const save = useMutation({
    mutationFn: () =>
      api.put<InsightTypeDetail>(`/settings/insight-types/${type.slug}`, { name, prompt }),
    onSuccess: () => {
      invalidate();
      setEditing(false);
    },
  });

  const remove = useMutation({
    mutationFn: () => api.del(`/settings/insight-types/${type.slug}`),
    onSuccess: invalidate,
  });

  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <p className="truncate font-medium">{type.name}</p>
          </div>
          <p className="mt-0.5 truncate text-xs text-fg-subtle">slug: {type.slug}</p>
        </div>
        {!editing && (
          <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>
            Edit
          </Button>
        )}
      </div>

      {editing && (
        <div className="mt-3 space-y-3 border-t border-border pt-3">
          <div>
            <Label htmlFor={`it-${type.slug}-name`}>Name</Label>
            <Input
              id={`it-${type.slug}-name`}
              className="mt-1.5"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div>
            <Label htmlFor={`it-${type.slug}-prompt`}>Prompt</Label>
            <p className="mt-1 text-xs text-fg-subtle">
              Required placeholders:{' '}
              <code className="font-mono">{'{{transcript}}'}</code>{' '}
              {REQUIRED_PLACEHOLDERS.map((p) => (
                <code key={p} className="ml-1 font-mono">{`{{${p}}}`}</code>
              ))}
            </p>
            <Textarea
              id={`it-${type.slug}-prompt`}
              className="mt-1.5 min-h-[320px] font-mono text-xs"
              spellCheck={false}
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
            />
          </div>
          {save.error && (
            <p className="text-sm text-danger-ink">{(save.error as Error).message}</p>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="primary" loading={save.isPending} onClick={() => save.mutate()}>
              Save
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setEditing(false);
                setName(type.name);
                setPrompt(type.prompt);
              }}
            >
              Cancel
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="ml-auto text-danger-ink"
              loading={remove.isPending}
              onClick={() => {
                if (window.confirm(`Delete "${type.name}"? Recordings already using it keep it.`)) {
                  remove.mutate();
                }
              }}
            >
              <Trash2 />
              Delete
            </Button>
          </div>
        </div>
      )}
    </Card>
  );
}

const NEW_TYPE_PROMPT_STUB = [
  '---',
  'name: custom_insight_type',
  'temperature: 0.2',
  '---',
  '',
  '## SYSTEM',
  '',
  'You are watching a live, rough transcript. Return ONLY this JSON, nothing else:',
  '',
  '  {',
  '    "topics": [{"title": string, "summary": string, "current": boolean}],',
  '    "questions": [{"question": string, "ai_answer_points": [string, ...], "discussion": string}],',
  '    "action_items": [{"text": string, "owner": string|null}]',
  '  }',
  '',
  '## USER',
  '',
  'Topics so far:',
  '{{previous_topics}}',
  '',
  'Questions so far:',
  '{{previous_questions}}',
  '',
  'Action items so far:',
  '{{previous_action_items}}',
  '',
  'Live transcript so far:',
  '{{transcript}}',
  '',
].join('\n');

function AddInsightType() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [prompt, setPrompt] = useState(NEW_TYPE_PROMPT_STUB);

  const close = () => {
    setOpen(false);
    setName('');
    setPrompt(NEW_TYPE_PROMPT_STUB);
  };

  const create = useMutation({
    mutationFn: () => api.post<InsightTypeDetail>('/settings/insight-types', { name, prompt }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: insightTypesQueryKey() });
      void queryClient.invalidateQueries({ queryKey: ['insight-types'] });
      close();
    },
  });

  if (!open) {
    return (
      <Button variant="secondary" onClick={() => setOpen(true)}>
        <Plus />
        Add meeting type
      </Button>
    );
  }

  return (
    <Card className="space-y-3 p-4">
      <h3 className="font-display text-base font-semibold">New meeting type</h3>
      <div>
        <Label htmlFor="new-it-name">Name</Label>
        <Input
          id="new-it-name"
          className="mt-1.5"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Sales Call"
        />
      </div>
      <div>
        <Label htmlFor="new-it-prompt">Prompt</Label>
        <p className="mt-1 text-xs text-fg-subtle">
          Required placeholders: <code className="font-mono">{'{{transcript}}'}</code>{' '}
          {REQUIRED_PLACEHOLDERS.map((p) => (
            <code key={p} className="ml-1 font-mono">{`{{${p}}}`}</code>
          ))}
        </p>
        <Textarea
          id="new-it-prompt"
          className="mt-1.5 min-h-[280px] font-mono text-xs"
          spellCheck={false}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
        />
      </div>
      {create.error && <p className="text-sm text-danger-ink">{(create.error as Error).message}</p>}
      <div className="flex items-center gap-2">
        <Button
          variant="primary"
          disabled={!name.trim()}
          loading={create.isPending}
          onClick={() => create.mutate()}
        >
          Create
        </Button>
        <Button variant="ghost" onClick={close}>
          Cancel
        </Button>
      </div>
    </Card>
  );
}

export function InsightTypesSettingsPage() {
  const types = useQuery({
    queryKey: insightTypesQueryKey(),
    queryFn: () => api.get<InsightTypeDetail[]>('/settings/insight-types'),
  });

  if (types.isLoading) return <Skeleton className="h-48 w-full" />;
  if (types.isError) return <ErrorState error={types.error} />;

  const rows = types.data ?? [];

  return (
    <div className="space-y-4">
      <Card className="p-5">
        <h2 className="font-display text-lg font-semibold">Meeting types</h2>
        <p className="mt-1 text-sm text-fg-subtle">
          What the recorder's "Meeting type" picker offers, and the prompt behind each one -- see
          app/services/insight_types.py. General Meeting and Interview are the built-ins; add more
          for whatever else you record regularly.
        </p>

        {rows.length === 0 ? (
          <p className="mt-4 rounded border border-dashed border-border p-4 text-sm text-fg-subtle">
            No meeting types configured -- the recorder's picker will be empty until you add one.
          </p>
        ) : (
          <div className="mt-4 space-y-3">
            {rows.map((type) => (
              <InsightTypeCard key={type.slug} type={type} />
            ))}
          </div>
        )}
      </Card>

      <AddInsightType />
    </div>
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
