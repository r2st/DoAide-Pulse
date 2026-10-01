import { useState } from "react";
import { ErrorBanner, SectionHeader, Skeleton } from "../components/ui/Bits";
import Dialog from "../components/ui/Dialog";
import { useToast } from "../components/ui/Toast";
import { useApi } from "../hooks/useApi";
import { useAuth } from "../hooks/useAuth";
import { api } from "../lib/api";
import { formatWhen } from "../lib/format";

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
  // The detail endpoint, not the public probe: every field this panel renders
  // is one the anonymous /health deliberately withholds.
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
