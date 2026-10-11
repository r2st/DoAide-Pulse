import { useState } from "react";
import { ErrorBanner, SectionHeader, Skeleton, Empty } from "../components/ui/Bits";
import Dialog from "../components/ui/Dialog";
import { useToast } from "../components/ui/Toast";
import { useApi } from "../hooks/useApi";
import { useAuth } from "../hooks/useAuth";
import { api } from "../lib/api";
import { formatWhen, titleize } from "../lib/format";

/**
 * Platform connections and service health.
 *
 * Credentials only ever travel inbound — the API never returns them, so a
 * connected platform shows a handle and a status, and re-connecting means
 * re-entering the key. That is deliberate, and the copy says so.
 */
export default function Settings() {
  const { user, refresh } = useAuth();
  const { data, error, loading, reload } = useApi(() => api.platforms(), []);
  const health = useApi(() => api.healthDetail(), []);
  const [connecting, setConnecting] = useState(null);

  return (
    <div className="stagger space-y-8">
      <div>
        <h1 className="page-title">Settings</h1>
        <p className="mt-1 text-sm text-ink-500">
          Signed in as <span className="font-mono text-xs">{user?.email}</span>
        </p>
      </div>

      <ErrorBanner message={error} onRetry={reload} />

      <section>
        <SectionHeader
          title="Publishing platforms"
          subtitle="Credentials are encrypted at rest and never sent back."
        />
        {loading && !data ? (
          <Skeleton rows={4} />
        ) : (
          <div className="space-y-3">
            {(data ?? []).map((platform) => (
              <PlatformRow
                key={platform.platform}
                platform={platform}
                onConnect={() => setConnecting(platform)}
                onChanged={() => {
                  reload();
                  refresh();
                }}
              />
            ))}
          </div>
        )}
      </section>

      <WebhooksSection />
      <ApiKeysSection />

      <section>
        <SectionHeader
          title="Service health"
          subtitle="What is configured on the server."
        />
        {health.loading && !health.data ? (
          <Skeleton rows={1} />
        ) : (
          <dl className="panel divide-y divide-line">
            <HealthRow
              label="AI providers"
              value={
                health.data?.llm_providers?.length
                  ? health.data.llm_providers.join(" → ")
                  : "none configured"
              }
              ok={Boolean(health.data?.llm_providers?.length)}
              hint={
                health.data?.llm_providers?.length
                  ? "Tried in order; failing ones skipped for 5 min."
                  : "Set OPENROUTER_API_KEY for AI generation."
              }
            />
            <HealthRow
              label="GitHub"
              value={health.data?.github_configured ? "token set" : "anonymous"}
              ok={Boolean(health.data?.github_configured)}
              hint={
                health.data?.github_configured
                  ? "5000 req/hr."
                  : "60 req/hr, public only. Set GITHUB_TOKEN."
              }
            />
            <HealthRow
              label="Credential encryption"
              value={health.data?.credential_encryption ? "on" : "off"}
              ok={Boolean(health.data?.credential_encryption)}
              hint={
                health.data?.credential_encryption
                  ? "Tokens encrypted at rest."
                  : "Set TOKEN_ENCRYPTION_KEY for production."
              }
            />
            {Object.keys(health.data?.llm_breakers_open ?? {}).length > 0 && (
              <HealthRow
                label="Tripped providers"
                value={Object.keys(health.data.llm_breakers_open).join(", ")}
                ok={false}
                hint="These are being skipped until their cool-down expires."
              />
            )}
          </dl>
        )}
      </section>

      {connecting && (
        <ConnectDialog
          platform={connecting}
          onClose={() => setConnecting(null)}
          onDone={() => {
            setConnecting(null);
            reload();
            refresh();
          }}
        />
      )}
    </div>
  );
}

function HealthRow({ label, value, ok, hint }) {
  return (
    <div className="flex items-start justify-between gap-4 px-5 py-3">
      <div className="min-w-0">
        <dt className="text-sm text-ink-900">{label}</dt>
        {hint && <dd className="mt-0.5 text-xs text-ink-400">{hint}</dd>}
      </div>
      <dd
        className={`shrink-0 font-mono text-xs ${ok ? "text-good" : "text-warn"}`}
      >
        <span className="sr-only">{ok ? "OK:" : "Warning:"} </span>
        {value}
      </dd>
    </div>
  );
}

function PlatformRow({ platform, onConnect, onChanged }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const connection = platform.connection;

  async function verify() {
    setBusy(true);
    try {
      const result = await api.verifyConnection(platform.platform);
      if (result.status === "connected") toast.success(`${platform.display_name} is fine`);
      else toast.error(result.last_error || "Credentials were rejected");
      onChanged();
    } catch (err) {
      toast.error(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function disconnect() {
    if (!window.confirm(`Disconnect ${platform.display_name}?`)) return;
    setBusy(true);
    try {
      await api.deleteConnection(platform.platform);
      toast.success("Disconnected");
      onChanged();
    } catch (err) {
      toast.error(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel flex flex-wrap items-start justify-between gap-4 p-5">
      <div className="min-w-0">
        <div className="flex items-center gap-2.5">
          <h3 className="text-sm font-semibold text-ink-900">{platform.display_name}</h3>
          {!platform.implemented && (
            <span className="badge bg-canvas text-ink-400">not built</span>
          )}
          {connection?.status === "connected" && (
            <span className="badge bg-good-wash text-good">connected</span>
          )}
          {connection?.status === "invalid" && (
            <span className="badge bg-bad-wash text-bad">reconnect</span>
          )}
        </div>

        {connection?.display_name && (
          <p className="mt-1 font-mono text-[11px] text-ink-500">
            {connection.display_name}
            {connection.last_verified_at &&
              ` · verified ${formatWhen(connection.last_verified_at)}`}
          </p>
        )}
        {connection?.last_error && (
          <p className="mt-2 break-words rounded bg-bad-wash px-2 py-1 font-mono text-[11px] text-bad">
            {connection.last_error}
          </p>
        )}
        {platform.caveat && (
          <p className="mt-2 max-w-lg text-xs leading-relaxed text-ink-400">
            {platform.caveat}
          </p>
        )}
        {!platform.supports_metrics && platform.implemented && (
          <p className="mt-1 text-xs text-ink-400">
            No stats API — views for this platform will stay empty.
          </p>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-2">
        {connection && (
          <>
            <button className="btn-quiet" onClick={verify} disabled={busy}>
              Verify
            </button>
            <button className="btn-quiet text-bad" onClick={disconnect} disabled={busy}>
              Disconnect
            </button>
          </>
        )}
        <button
          className="btn-ghost"
          onClick={onConnect}
          disabled={!platform.implemented}
          title={platform.implemented ? undefined : "This adapter is not finished yet"}
        >
          {connection ? "Replace key" : "Connect"}
        </button>
      </div>
    </div>
  );
}

/* ---- Webhooks ---- */

function WebhooksSection() {
  const toast = useToast();
  const { data, loading, reload } = useApi(() => api.listWebhooks(), []);
  const [creating, setCreating] = useState(false);
  const [secret, setSecret] = useState(null);

  async function remove(webhook) {
    if (!window.confirm(`Delete webhook to ${webhook.url}?`)) return;
    try {
      await api.deleteWebhook(webhook.id);
      toast.success("Webhook deleted");
      reload();
    } catch (err) {
      toast.error(err.message);
    }
  }

  async function toggle(webhook) {
    try {
      await api.updateWebhook(webhook.id, { is_active: !webhook.is_active });
      toast.success(webhook.is_active ? "Paused" : "Resumed");
      reload();
    } catch (err) {
      toast.error(err.message);
    }
  }

  async function ping(webhook) {
    try {
      const delivery = await api.pingWebhook(webhook.id);
      if (delivery.status === "delivered") toast.success("Ping delivered");
      else toast.error(delivery.error || "Ping failed");
    } catch (err) {
      toast.error(err.message);
    }
  }

  return (
    <section>
      <SectionHeader
        title="Outbound webhooks"
        subtitle="HTTP callbacks fired when something happens."
        action={
          <button className="btn-ghost text-sm" onClick={() => setCreating(true)}>
            Add webhook
          </button>
        }
      />
      {loading && !data ? (
        <Skeleton rows={2} />
      ) : (data ?? []).length === 0 ? (
        <Empty
          title="No webhooks"
          hint="Get notified when content is published or fails."
          action={
            <button className="btn-primary mt-1" onClick={() => setCreating(true)}>
              Add webhook
            </button>
          }
        />
      ) : (
        <div className="space-y-3">
          {data.map((webhook) => (
            <div key={webhook.id} className="panel p-5">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <p className="truncate font-mono text-xs text-ink-900">
                      {webhook.url}
                    </p>
                    {!webhook.is_active && (
                      <span className="badge bg-canvas text-ink-400">paused</span>
                    )}
                    {webhook.consecutive_failures > 0 && (
                      <span className="badge bg-bad-wash text-bad">
                        {webhook.consecutive_failures} failures
                      </span>
                    )}
                  </div>
                  {webhook.description && (
                    <p className="mt-1 text-xs text-ink-500">{webhook.description}</p>
                  )}
                  <p className="mt-1 flex flex-wrap gap-1.5">
                    {webhook.events.map((event) => (
                      <span key={event} className="chip">{titleize(event)}</span>
                    ))}
                  </p>
                  {webhook.last_error && (
                    <p className="mt-2 break-words rounded bg-bad-wash px-2 py-1 font-mono text-[11px] text-bad">
                      {webhook.last_error}
                    </p>
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <button className="btn-quiet" onClick={() => ping(webhook)}>
                    Ping
                  </button>
                  <button className="btn-quiet" onClick={() => toggle(webhook)}>
                    {webhook.is_active ? "Pause" : "Resume"}
                  </button>
                  <button className="btn-quiet text-bad" onClick={() => remove(webhook)}>
                    Delete
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {creating && (
        <CreateWebhookDialog
          onClose={() => setCreating(false)}
          onDone={(created) => {
            setCreating(false);
            setSecret(created.secret);
            reload();
          }}
        />
      )}

      {secret && (
        <Dialog label="Signing secret" onClose={() => setSecret(null)}>
          <h2 className="font-display text-2xl text-ink-900">Signing secret</h2>
          <p className="text-sm text-ink-700">
            Copy this now — it will not be shown again.
          </p>
          <input
            readOnly
            className="input font-mono text-xs"
            value={secret}
            onFocus={(e) => e.target.select()}
          />
          <div className="flex justify-end pt-1">
            <button className="btn-primary" onClick={() => setSecret(null)}>
              Done
            </button>
          </div>
        </Dialog>
      )}
    </section>
  );
}

function CreateWebhookDialog({ onClose, onDone }) {
  const toast = useToast();
  const { data: events } = useApi(() => api.webhookEvents(), []);
  const [url, setUrl] = useState("");
  const [description, setDescription] = useState("");
  const [selected, setSelected] = useState([]);
  const [busy, setBusy] = useState(false);

  function toggleEvent(event) {
    setSelected((prev) =>
      prev.includes(event) ? prev.filter((e) => e !== event) : [...prev, event],
    );
  }

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    try {
      const result = await api.createWebhook({
        url,
        events: selected,
        description,
      });
      onDone(result);
    } catch (err) {
      toast.error(err.message);
      setBusy(false);
    }
  }

  return (
    <Dialog
      label="Add webhook"
      onClose={onClose}
      closable={!busy}
      onSubmit={submit}
    >
      <h2 className="font-display text-2xl text-ink-900">Add webhook</h2>

      <div>
        <label className="label" htmlFor="wh-url">Endpoint URL</label>
        <input
          id="wh-url"
          className="input font-mono text-xs"
          type="url"
          required
          placeholder="https://example.com/webhook"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
        />
      </div>

      <div>
        <label className="label" htmlFor="wh-desc">
          Description <span className="normal-case tracking-normal">(optional)</span>
        </label>
        <input
          id="wh-desc"
          className="input"
          maxLength={200}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </div>

      <fieldset>
        <legend className="label mb-2">Events</legend>
        <div className="space-y-2">
          {(events ?? []).map((ev) => (
            <label key={ev.event} className="flex items-start gap-2.5 text-sm text-ink-700">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={selected.includes(ev.event)}
                onChange={() => toggleEvent(ev.event)}
              />
              <span>
                <span className="font-medium">{titleize(ev.event)}</span>
                <span className="mt-0.5 block text-xs text-ink-400">{ev.description}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      <div className="flex justify-end gap-2 pt-2">
        <button type="button" className="btn-ghost" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button
          type="submit"
          className="btn-primary"
          disabled={busy || !url || selected.length === 0}
        >
          {busy ? "Creating…" : "Create"}
        </button>
      </div>
    </Dialog>
  );
}

/* ---- API Keys ---- */

function ApiKeysSection() {
  const toast = useToast();
  const { data, loading, reload } = useApi(() => api.listApiKeys(), []);
  const [creating, setCreating] = useState(false);
  const [token, setToken] = useState(null);

  async function revoke(key) {
    if (!window.confirm(`Revoke API key "${key.name}"?`)) return;
    try {
      await api.revokeApiKey(key.id);
      toast.success("Key revoked");
      reload();
    } catch (err) {
      toast.error(err.message);
    }
  }

  return (
    <section>
      <SectionHeader
        title="API keys"
        subtitle="Machine credentials scoped to a project."
        action={
          <button className="btn-ghost text-sm" onClick={() => setCreating(true)}>
            Create key
          </button>
        }
      />
      {loading && !data ? (
        <Skeleton rows={2} />
      ) : (data ?? []).length === 0 ? (
        <Empty
          title="No API keys"
          hint="Create a key to let scripts file ideas or read analytics."
          action={
            <button className="btn-primary mt-1" onClick={() => setCreating(true)}>
              Create key
            </button>
          }
        />
      ) : (
        <div className="space-y-3">
          {data.map((key) => (
            <div key={key.id} className="panel flex items-start justify-between gap-4 p-5">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <p className="text-sm font-semibold text-ink-900">{key.name}</p>
                  <span className="font-mono text-[11px] text-ink-400">{key.prefix}…</span>
                  {key.revoked_at && (
                    <span className="badge bg-canvas text-ink-400">revoked</span>
                  )}
                </div>
                <p className="mt-1 flex flex-wrap gap-1.5">
                  {key.scopes.map((scope) => (
                    <span key={scope} className="chip">{titleize(scope)}</span>
                  ))}
                </p>
                <p className="mt-1 text-[11px] text-ink-400">
                  Created {formatWhen(key.created_at)}
                  {key.last_used_at && <> · last used {formatWhen(key.last_used_at)}</>}
                  {key.expires_at && <> · expires {formatWhen(key.expires_at)}</>}
                </p>
              </div>
              {!key.revoked_at && (
                <button className="btn-quiet shrink-0 text-bad" onClick={() => revoke(key)}>
                  Revoke
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {creating && (
        <CreateApiKeyDialog
          onClose={() => setCreating(false)}
          onDone={(created) => {
            setCreating(false);
            setToken(created.token);
            reload();
          }}
        />
      )}

      {token && (
        <Dialog label="API token" onClose={() => setToken(null)}>
          <h2 className="font-display text-2xl text-ink-900">API token</h2>
          <p className="text-sm text-ink-700">
            Copy this now — it will not be shown again.
          </p>
          <input
            readOnly
            className="input font-mono text-xs"
            value={token}
            onFocus={(e) => e.target.select()}
          />
          <div className="flex justify-end pt-1">
            <button className="btn-primary" onClick={() => setToken(null)}>
              Done
            </button>
          </div>
        </Dialog>
      )}
    </section>
  );
}

function CreateApiKeyDialog({ onClose, onDone }) {
  const toast = useToast();
  const { data: projects } = useApi(() => api.listProjects(), []);
  const { data: scopes } = useApi(() => api.apiKeyScopes(), []);
  const [projectId, setProjectId] = useState("");
  const [name, setName] = useState("");
  const [selected, setSelected] = useState([]);
  const [expiryDays, setExpiryDays] = useState("");
  const [busy, setBusy] = useState(false);

  function toggleScope(scope) {
    setSelected((prev) =>
      prev.includes(scope) ? prev.filter((s) => s !== scope) : [...prev, scope],
    );
  }

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    try {
      const result = await api.createApiKey({
        project_id: Number(projectId),
        name,
        scopes: selected,
        expires_in_days: expiryDays ? Number(expiryDays) : null,
      });
      onDone(result);
    } catch (err) {
      toast.error(err.message);
      setBusy(false);
    }
  }

  return (
    <Dialog
      label="Create API key"
      onClose={onClose}
      closable={!busy}
      onSubmit={submit}
    >
      <h2 className="font-display text-2xl text-ink-900">Create API key</h2>

      <div>
        <label className="label" htmlFor="ak-project">Project</label>
        <select
          id="ak-project"
          className="input"
          value={projectId}
          onChange={(e) => setProjectId(e.target.value)}
          required
        >
          <option value="">Select a project</option>
          {(projects ?? []).map((p) => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </select>
      </div>

      <div>
        <label className="label" htmlFor="ak-name">Key name</label>
        <input
          id="ak-name"
          className="input"
          required
          maxLength={120}
          placeholder="CI deploy key"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </div>

      <fieldset>
        <legend className="label mb-2">Scopes</legend>
        <div className="space-y-2">
          {(scopes ?? []).map((s) => (
            <label key={s.scope} className="flex items-start gap-2.5 text-sm text-ink-700">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={selected.includes(s.scope)}
                onChange={() => toggleScope(s.scope)}
              />
              <span>
                <span className="font-medium">{titleize(s.scope)}</span>
                <span className="mt-0.5 block text-xs text-ink-400">{s.description}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      <div>
        <label className="label" htmlFor="ak-expiry">
          Expires in days <span className="normal-case tracking-normal">(blank = never)</span>
        </label>
        <input
          id="ak-expiry"
          className="input"
          type="number"
          min={1}
          max={365}
          value={expiryDays}
          onChange={(e) => setExpiryDays(e.target.value)}
        />
      </div>

      <div className="flex justify-end gap-2 pt-2">
        <button type="button" className="btn-ghost" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button
          type="submit"
          className="btn-primary"
          disabled={busy || !projectId || !name || selected.length === 0}
        >
          {busy ? "Creating…" : "Create"}
        </button>
      </div>
    </Dialog>
  );
}

function ConnectDialog({ platform, onClose, onDone }) {
  const toast = useToast();
  const [values, setValues] = useState(() =>
    Object.fromEntries(platform.credential_fields.map((field) => [field.key, ""])),
  );
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    // Optional blanks are omitted rather than sent as "" — the API validates
    // supplied keys against the adapter's field list.
    const payload = Object.fromEntries(
      Object.entries(values).filter(([, value]) => value.trim()),
    );
    try {
      await api.saveConnection(platform.platform, payload);
      toast.success(`${platform.display_name} connected`);
      onDone();
    } catch (err) {
      toast.error(err.message);
      setBusy(false);
    }
  }

  return (
    <Dialog
      label={`Connect ${platform.display_name}`}
      onClose={onClose}
      closable={!busy}
      width="md"
      onSubmit={submit}
    >
      <h2 className="font-display text-2xl text-ink-900">
        Connect {platform.display_name}
      </h2>
      {platform.caveat && (
        <p className="rounded-lg bg-warn-wash px-3 py-2 text-xs leading-relaxed text-warn">
          {platform.caveat}
        </p>
      )}

      {platform.credential_fields.map((field) => (
        <div key={field.key}>
          <label className="label" htmlFor={`cred-${field.key}`}>
            {field.label}
            {!field.required && (
              <span className="normal-case tracking-normal"> (optional)</span>
            )}
          </label>
          <input
            id={`cred-${field.key}`}
            className="input font-mono text-xs"
            type={field.secret ? "password" : "text"}
            required={field.required}
            autoComplete="off"
            value={values[field.key]}
            onChange={(e) =>
              setValues((current) => ({ ...current, [field.key]: e.target.value }))
            }
          />
          {field.help_text && (
            <p className="mt-1 text-xs text-ink-400">{field.help_text}</p>
          )}
        </div>
      ))}

      <p className="text-xs text-ink-400">
        Verified against {platform.display_name} before saving.
      </p>

      <div className="flex justify-end gap-2 pt-1">
        <button type="button" className="btn-ghost" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button type="submit" className="btn-primary" disabled={busy}>
          {busy ? "Verifying…" : "Connect"}
        </button>
      </div>
    </Dialog>
  );
}
