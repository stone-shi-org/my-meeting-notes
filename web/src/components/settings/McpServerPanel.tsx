/**
 * The MCP server at /mcp, and the personal API tokens that unlock it (MMN-14).
 *
 * An agent -- Claude Code, Claude Desktop, Pocket Agent -- connects with
 * `Authorization: Bearer mmn_...`. Tokens are per user: everyone manages their
 * own here, and nobody (admins included) sees anyone else's. The raw value is
 * shown exactly once, right after it is created; the server only keeps a hash.
 *
 * The on/off switch is a global app setting, so it is the shared admin-gated
 * `SettingsForm`, the same block the other tabs use.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, Check, Copy, KeyRound, Plug, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { Button } from '@/components/ui/Button';
import { ConfirmDialog } from '@/components/ui/ConfirmDialog';
import { Badge, Card, Input, Label, Select, Skeleton } from '@/components/ui/primitives';
import { ErrorState } from '@/components/ui/states';
import { api } from '@/lib/api';
import { copyText } from '@/lib/clipboard';
import { fmtRelative } from '@/lib/time';
import { SettingsForm } from '@/pages/SettingsPage';
import type { ApiToken, ApiTokenList, ApiTokenScope, CreatedApiToken, SettingEntry } from '@/types/api';

export const API_TOKENS_KEY = ['api-tokens'] as const;

const EXPIRY_OPTIONS = [
  { value: '', label: 'Never' },
  { value: '30', label: '30 days' },
  { value: '90', label: '90 days' },
  { value: '365', label: '1 year' },
];

/** Where an MCP client should point: this origin, at the server's path. */
export function endpointUrl(path: string): string {
  const origin = typeof window !== 'undefined' ? window.location.origin : '';
  return `${origin}${path}`;
}

export function clientSnippets(url: string, token: string) {
  return {
    claudeCode: `claude mcp add --transport http my-meeting-notes ${url} --header "Authorization: Bearer ${token}"`,
    json: JSON.stringify(
      {
        mcpServers: {
          'my-meeting-notes': {
            command: 'npx',
            args: ['-y', 'mcp-remote', url, '--header', 'Authorization:${MMN_AUTH}'],
            env: { MMN_AUTH: `Bearer ${token}` },
          },
        },
      },
      null,
      2,
    ),
    http: `URL:    ${url}\nHeader: Authorization: Bearer ${token}`,
  };
}

/** A copy button that says whether the copy actually happened. */
function CopyButton({ text, label }: { text: string; label: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle');
  return (
    <Button
      type="button"
      size="xs"
      variant="secondary"
      onClick={() =>
        void copyText(text).then((ok) => {
          setState(ok ? 'copied' : 'failed');
          window.setTimeout(() => setState('idle'), 2000);
        })
      }
    >
      {state === 'copied' ? <Check aria-hidden /> : <Copy aria-hidden />}
      {state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed — select it' : label}
    </Button>
  );
}

function CodeBlock({ text, label }: { text: string; label: string }) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium text-fg-muted">{label}</span>
        <CopyButton text={text} label="Copy" />
      </div>
      <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded border border-border bg-surface-2 p-2.5 font-mono text-xs text-fg">
        {text}
      </pre>
    </div>
  );
}

function scopeLabel(scope: ApiTokenScope): string {
  return scope === 'read_write' ? 'Read & write' : 'Read only';
}

function StateBadge({ token }: { token: ApiToken }) {
  if (token.state === 'revoked') return <Badge variant="neutral">Revoked</Badge>;
  if (token.state === 'expired') return <Badge variant="warning">Expired</Badge>;
  return <Badge variant="success">Active</Badge>;
}

function NewTokenReveal({
  created,
  path,
  onDone,
}: {
  created: CreatedApiToken;
  path: string;
  onDone: () => void;
}) {
  const url = endpointUrl(path);
  const snippets = clientSnippets(url, created.token);
  return (
    <div
      role="status"
      className="space-y-4 rounded-md border border-success-ink/30 bg-success-soft/40 p-4"
    >
      <div>
        <p className="flex items-center gap-1.5 text-sm font-semibold text-fg">
          <KeyRound className="size-4 text-success-ink" aria-hidden />
          Token “{created.name}” created
        </p>
        <p className="mt-1 text-sm text-fg-muted">
          Copy it now — this is the only time it is shown. If you lose it, revoke it and create
          another.
        </p>
      </div>
      <CodeBlock label="Token" text={created.token} />
      <CodeBlock label="Claude Code" text={snippets.claudeCode} />
      <CodeBlock label="Claude Desktop / JSON config (via mcp-remote)" text={snippets.json} />
      <CodeBlock label="Any streamable-HTTP MCP client" text={snippets.http} />
      <div className="flex justify-end">
        <Button size="sm" variant="secondary" onClick={onDone}>
          Done
        </Button>
      </div>
    </div>
  );
}

function CreateTokenForm({ onCreated }: { onCreated: (t: CreatedApiToken) => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [scope, setScope] = useState<ApiTokenScope>('read');
  const [expiry, setExpiry] = useState('');
  const create = useMutation({
    mutationFn: () =>
      api.post<CreatedApiToken>('/tokens', {
        name: name.trim(),
        scope,
        expires_in_days: expiry ? Number(expiry) : null,
      }),
    onSuccess: (token) => {
      setName('');
      void queryClient.invalidateQueries({ queryKey: API_TOKENS_KEY });
      onCreated(token);
    },
  });

  return (
    <form
      className="grid gap-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-end"
      onSubmit={(e) => {
        e.preventDefault();
        if (name.trim()) create.mutate();
      }}
    >
      <div>
        <Label htmlFor="token-name">Name</Label>
        <Input
          id="token-name"
          className="mt-1.5"
          placeholder="e.g. Claude Code on my laptop"
          maxLength={100}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </div>
      <div>
        <Label htmlFor="token-scope">Access</Label>
        <Select
          id="token-scope"
          className="mt-1.5"
          value={scope}
          onChange={(e) => setScope(e.target.value as ApiTokenScope)}
        >
          <option value="read">Read only</option>
          <option value="read_write">Read &amp; write</option>
        </Select>
      </div>
      <div>
        <Label htmlFor="token-expiry">Expires</Label>
        <Select
          id="token-expiry"
          className="mt-1.5"
          value={expiry}
          onChange={(e) => setExpiry(e.target.value)}
        >
          {EXPIRY_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </Select>
      </div>
      <Button type="submit" loading={create.isPending} disabled={!name.trim() || create.isPending}>
        Create token
      </Button>
      {create.isError ? (
        <p role="alert" className="flex items-start gap-1 text-xs text-danger-ink sm:col-span-4">
          <AlertCircle className="mt-0.5 size-3 shrink-0" aria-hidden />
          {create.error instanceof Error ? create.error.message : 'Could not create the token.'}
        </p>
      ) : null}
    </form>
  );
}

function TokenTable({ tokens }: { tokens: ApiToken[] }) {
  const queryClient = useQueryClient();
  const [confirm, setConfirm] = useState<ApiToken | null>(null);
  const revoke = useMutation({
    mutationFn: (id: number) => api.del<ApiToken>(`/tokens/${id}`),
    onSuccess: () => {
      setConfirm(null);
      void queryClient.invalidateQueries({ queryKey: API_TOKENS_KEY });
    },
  });

  if (tokens.length === 0) {
    return <p className="text-sm text-fg-subtle">No tokens yet.</p>;
  }

  return (
    <>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <caption className="sr-only">Your API tokens</caption>
          <thead>
            <tr className="text-left text-xs text-fg-subtle">
              <th scope="col" className="pb-2 font-medium">Name</th>
              <th scope="col" className="pb-2 font-medium">Access</th>
              <th scope="col" className="pb-2 font-medium">Last used</th>
              <th scope="col" className="pb-2 font-medium">Expires</th>
              <th scope="col" className="pb-2 font-medium">State</th>
              <th scope="col" className="pb-2">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {tokens.map((t) => (
              <tr key={t.id}>
                <th scope="row" className="py-2 pr-3 text-left font-normal">
                  <span className="text-fg">{t.name}</span>
                  <span className="ml-2 font-mono text-xs text-fg-subtle">{t.prefix}…</span>
                </th>
                <td className="py-2 pr-3 text-fg-muted">{scopeLabel(t.scope)}</td>
                <td className="py-2 pr-3 text-fg-muted">
                  {t.last_used_at ? (
                    <time dateTime={t.last_used_at} title={t.last_used_at}>
                      {fmtRelative(t.last_used_at)}
                    </time>
                  ) : (
                    'Never'
                  )}
                </td>
                <td className="py-2 pr-3 text-fg-muted">
                  {t.expires_at ? (
                    <time dateTime={t.expires_at} title={t.expires_at}>
                      {new Date(t.expires_at).toLocaleDateString()}
                    </time>
                  ) : (
                    'Never'
                  )}
                </td>
                <td className="py-2 pr-3">
                  <StateBadge token={t} />
                </td>
                <td className="py-2 text-right">
                  {t.state !== 'revoked' ? (
                    <Button
                      size="xs"
                      variant="ghost"
                      aria-label={`Revoke ${t.name}`}
                      onClick={() => setConfirm(t)}
                    >
                      <Trash2 aria-hidden />
                      Revoke
                    </Button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ConfirmDialog
        open={confirm !== null}
        onOpenChange={(open) => {
          if (!open) setConfirm(null);
        }}
        title="Revoke this token?"
        description={
          confirm
            ? `Anything using “${confirm.name}” will stop working immediately. This cannot be undone.`
            : undefined
        }
        confirmLabel="Revoke"
        loading={revoke.isPending}
        onConfirm={() => {
          if (confirm) revoke.mutate(confirm.id);
        }}
      />
    </>
  );
}

export function McpServerPanel() {
  const tokens = useQuery({
    queryKey: API_TOKENS_KEY,
    queryFn: () => api.get<ApiTokenList>('/tokens'),
  });
  // The switch lives in app settings; reading it from the same query the
  // form below saves through keeps the badge in step after a save.
  const settings = useQuery({
    queryKey: ['settings'],
    queryFn: () => api.get<{ settings: Record<string, SettingEntry> }>('/settings'),
  });
  const [created, setCreated] = useState<CreatedApiToken | null>(null);

  if (tokens.isLoading) return <Skeleton className="h-96 w-full" />;
  if (tokens.isError) {
    return <ErrorState error={tokens.error} onRetry={() => void tokens.refetch()} />;
  }

  const data = tokens.data!;
  const setting = settings.data?.settings?.mcp_enabled?.value;
  const enabled = setting === undefined ? data.mcp_enabled : String(setting) === 'true';
  const url = endpointUrl(data.endpoint_path);
  const readTools = data.tools.filter((t) => !t.write);
  const writeTools = data.tools.filter((t) => t.write);

  return (
    <div className="space-y-4">
      <Card className="p-5">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 font-display text-lg font-semibold">
              <Plug className="size-5 text-fg-subtle" aria-hidden />
              MCP server
            </h2>
            <p className="mt-1 text-sm text-fg-subtle">
              Lets AI assistants such as Claude Code, Claude Desktop or Pocket Agent read your
              meetings, transcripts, summaries, notes, emails and calendar — for example “get the
              transcript of last week’s Atlas standup”. Every client needs a personal token from
              below and only ever sees your own data.
            </p>
          </div>
          <Badge
            variant={enabled ? 'success' : 'neutral'}
            className="shrink-0"
            aria-label={`MCP server is ${enabled ? 'on' : 'off'}`}
          >
            {enabled ? 'On' : 'Off'}
          </Badge>
        </div>

        <div className="mt-4">
          <CodeBlock label="Endpoint (streamable HTTP)" text={url} />
        </div>

        {data.protocol_versions.length > 0 ? (
          <div className="mt-3">
            <p className="text-xs font-medium text-fg-muted">MCP protocol versions</p>
            <ul aria-label="Supported MCP protocol versions" className="mt-1.5 flex flex-wrap gap-1.5">
              {data.protocol_versions.map((v, i) => (
                <li key={v}>
                  <Badge variant={i === 0 ? 'primary' : 'neutral'} className="font-mono">
                    {v}
                    {i === 0 ? ' (latest)' : ''}
                  </Badge>
                </li>
              ))}
            </ul>
            <p className="mt-1.5 text-xs text-fg-subtle">
              Negotiated automatically: newer clients use the latest revision, older ones fall back to
              the version they ask for.
            </p>
          </div>
        ) : null}

        <details className="mt-4 text-sm">
          <summary className="cursor-pointer text-fg-muted hover:text-fg">
            {data.tools.length} tools available
          </summary>
          <div className="mt-2 space-y-2">
            <p className="text-xs text-fg-subtle">Every token:</p>
            <ul className="flex flex-wrap gap-1.5">
              {readTools.map((t) => (
                <li key={t.name}>
                  <Badge variant="neutral" title={t.title} className="font-mono">
                    {t.name}
                  </Badge>
                </li>
              ))}
            </ul>
            <p className="text-xs text-fg-subtle">Read &amp; write tokens only:</p>
            <ul className="flex flex-wrap gap-1.5">
              {writeTools.map((t) => (
                <li key={t.name}>
                  <Badge variant="primary" title={t.title} className="font-mono">
                    {t.name}
                  </Badge>
                </li>
              ))}
            </ul>
          </div>
        </details>
      </Card>

      <SettingsForm
        title="Server switch"
        description="Off makes /mcp answer 404 for everyone, without revoking anyone's tokens."
        keys={[
          {
            key: 'mcp_enabled',
            label: 'MCP server',
            hint: 'Applies to every user. Takes effect on the next request.',
          },
        ]}
      />

      <Card className="p-5">
        <h2 className="flex items-center gap-2 font-display text-lg font-semibold">
          <KeyRound className="size-5 text-fg-subtle" aria-hidden />
          Your API tokens
        </h2>
        <p className="mt-1 text-sm text-fg-subtle">
          <strong className="font-medium text-fg-muted">Read only</strong> tokens can look things
          up. <strong className="font-medium text-fg-muted">Read &amp; write</strong> tokens can
          also add notes and tick off action items. Tokens work only with the MCP server, not the
          rest of the app.
        </p>

        <div className="mt-4 space-y-4">
          {created ? (
            <NewTokenReveal
              created={created}
              path={data.endpoint_path}
              onDone={() => setCreated(null)}
            />
          ) : null}
          <CreateTokenForm onCreated={setCreated} />
          <div className="border-t border-border pt-4">
            <TokenTable tokens={data.tokens} />
          </div>
        </div>
      </Card>
    </div>
  );
}
