import { useState } from "react";
import { Link } from "react-router-dom";
import { Empty, ErrorBanner, SectionHeader, Skeleton, StatusBadge } from "../components/ui/Bits";
import Dialog from "../components/ui/Dialog";
import { useToast } from "../components/ui/Toast";
import { useApi } from "../hooks/useApi";
import { api } from "../lib/api";
import { formatWhen, titleize } from "../lib/format";
import {
  configFromForm,
  describeTrigger,
  fieldsFor,
  formFromConfig,
  isPolled,
  summarizeCheck,
  triggerHealth,
} from "../lib/triggers";

const KIND_LABELS = { github: "GitHub", rss: "RSS", webhook: "Webhook", schedule: "Schedule" };

/**
 * Everything that can make Herald write, and whether it is working.
 *
 * The page is organised by trigger rather than by project because the question
 * it exists to answer is "why hasn't this fired?" — and the answer is nearly
 * always one trigger's last error, last check, or event list, none of which are
 * comparable across projects.
 */
export default function Triggers() {
  const toast = useToast();
  const [projectId, setProjectId] = useState("");
  const { data: projects } = useApi(() => api.listProjects(), []);
  const { data: kinds } = useApi(() => api.triggerKinds(), []);
  const {
    data: triggers,
    error,
    loading,
    reload,
  } = useApi(() => api.listTriggers(projectId ? { project_id: projectId } : {}), [projectId]);

  const [editing, setEditing] = useState(null); // trigger | "new" | null
  // The signing secret, held only for as long as the dialog showing it is open.
  // The API returns it once and never again.
  const [issued, setIssued] = useState(null);

  const hasProjects = (projects?.length ?? 0) > 0;

  return (
    <div className="stagger space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="page-title">Triggers</h1>
          <p className="mt-1 max-w-2xl text-sm text-ink-500">
            What makes the robot write — repos, feeds, webhooks, schedules.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {hasProjects && (
            <select
              className="input w-auto"
              aria-label="Filter by project"
              value={projectId}
              onChange={(event) => setProjectId(event.target.value)}
            >
              <option value="">All projects</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          )}
          <button
            className="btn-primary"
            disabled={!hasProjects}
            title={hasProjects ? undefined : "Add a project first — a trigger writes about one."}
            onClick={() => setEditing("new")}
          >
            Add trigger
          </button>
        </div>
      </div>

      <ErrorBanner message={error} onRetry={reload} />

      {loading && !triggers ? (
        <Skeleton rows={3} />
      ) : !hasProjects ? (
        <Empty
          title="No projects yet"
          hint="Add a project first — triggers write about one."
          action={
            <Link to="/projects" className="btn-primary mt-1">
              Add a project
            </Link>
          }
        />
      ) : triggers?.length === 0 ? (
        <Empty
          title={projectId ? "No triggers on this project" : "Nothing is watching yet"}
          hint="Point the robot at a feed, repo, webhook, or schedule."
          action={
            <button className="btn-primary mt-1" onClick={() => setEditing("new")}>
              Add your first trigger
            </button>
          }
        />
      ) : (
        <div className="space-y-4">
          {triggers?.map((trigger) => (
            <TriggerCard
              key={trigger.id}
              trigger={trigger}
              project={projects?.find((p) => p.id === trigger.project_id)}
              showProject={!projectId}
              onEdit={() => setEditing(trigger)}
              onChanged={reload}
              onSecret={setIssued}
            />
          ))}
        </div>
      )}

      {editing && (
        <TriggerDialog
          trigger={editing === "new" ? null : editing}
          projects={projects ?? []}
          kinds={kinds ?? []}
          defaultProjectId={projectId}
          onClose={() => setEditing(null)}
          onSaved={(created) => {
            setEditing(null);
            reload();
            if (created?.secret) setIssued(created);
            else toast.success("Trigger saved");
          }}
          onError={(message) => toast.error(message)}
        />
      )}

      {issued && <SecretDialog trigger={issued} onClose={() => setIssued(null)} />}
    </div>
  );
}

function TriggerCard({ trigger, project, showProject, onEdit, onChanged, onSecret }) {
  const toast = useToast();
  const [busy, setBusy] = useState("");
  const [showEvents, setShowEvents] = useState(false);
  const health = triggerHealth(trigger);
  const polled = isPolled(trigger.kind);

  async function checkNow() {
    setBusy("check");
    try {
      const result = await api.checkTrigger(trigger.id);
      const message = summarizeCheck(result);
      if (result?.status === "error") toast.error(message);
      else toast.success(message);
      onChanged();
    } catch (err) {
      toast.error(err.message);
    } finally {
      setBusy("");
    }
  }

  async function toggle() {
    setBusy("toggle");
    try {
      await api.updateTrigger(trigger.id, { is_active: !trigger.is_active });
      onChanged();
    } catch (err) {
      toast.error(err.message);
    } finally {
      setBusy("");
    }
  }

  async function rotate() {
    if (
      !window.confirm(
        "Issue a new URL and signing secret? The current URL stops working " +
          "immediately, so anything already sending to it must be updated.",
      )
    ) {
      return;
    }
    setBusy("rotate");
    try {
      onSecret(await api.rotateTriggerSecret(trigger.id));
      onChanged();
    } catch (err) {
      toast.error(err.message);
    } finally {
      setBusy("");
    }
  }

  async function remove() {
    if (
      !window.confirm(
        `Delete this trigger? The robot stops watching ${describeTrigger(trigger)}. ` +
          "Existing content is kept.",
      )
    ) {
      return;
    }
    try {
      await api.deleteTrigger(trigger.id);
      toast.success("Trigger deleted");
      onChanged();
    } catch (err) {
      toast.error(err.message);
    }
  }

  return (
    <div className="panel p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="chip">{KIND_LABELS[trigger.kind] ?? titleize(trigger.kind)}</span>
            <h2 className="truncate text-base font-semibold text-ink-900">
              {trigger.name || `${KIND_LABELS[trigger.kind] ?? "Trigger"} trigger`}
            </h2>
            <span className={`badge ${health.tone}`} title={health.title}>
              {health.label}
            </span>
          </div>
          <p className="mt-1 truncate font-mono text-[11px] text-ink-400">
            {describeTrigger(trigger)}
          </p>
          {showProject && project && (
            <p className="mt-1 text-xs text-ink-400">
              on{" "}
              <Link className="underline hover:text-ink-700" to={`/content?project=${project.id}`}>
                {project.name}
              </Link>
            </p>
          )}
        </div>
      </div>

      {trigger.last_error && trigger.consecutive_failures > 0 && (
        <p className="mt-3 rounded-lg border border-bad/25 bg-bad-wash px-3 py-2 text-xs text-bad">
          {trigger.last_error}
        </p>
      )}

      {trigger.inbound_url && (
        <InboundUrl url={trigger.inbound_url} signed={Boolean(trigger.config?.require_signature)} />
      )}

      <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1 font-mono text-[11px] text-ink-500">
        <span>
          fired {trigger.fire_count} time{trigger.fire_count === 1 ? "" : "s"}
        </span>
        {trigger.last_fired_at && <span>last {formatWhen(trigger.last_fired_at)}</span>}
        {polled && trigger.last_checked_at && (
          <span title="Last poll">checked {formatWhen(trigger.last_checked_at)}</span>
        )}
        {trigger.config?.every_hours && <span>every {trigger.config.every_hours}h</span>}
        {trigger.config?.content_type && <span>{titleize(trigger.config.content_type)}</span>}
      </dl>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        {polled && (
          <button className="btn-ghost" onClick={checkNow} disabled={busy === "check"}>
            {busy === "check" ? "Checking…" : "Check now"}
          </button>
        )}
        <button className="btn-ghost" onClick={() => setShowEvents((open) => !open)}>
          {showEvents ? "Hide activity" : "Activity"}
        </button>
        <button className="btn-quiet" onClick={onEdit}>
          Edit
        </button>
        <button className="btn-quiet" onClick={toggle} disabled={busy === "toggle"}>
          {trigger.is_active ? "Pause" : "Resume"}
        </button>
        {trigger.kind === "webhook" && (
          <button className="btn-quiet" onClick={rotate} disabled={busy === "rotate"}>
            Rotate secret
          </button>
        )}
        <button className="btn-quiet ml-auto text-bad" onClick={remove}>
          Delete
        </button>
      </div>

      {showEvents && <TriggerEvents triggerId={trigger.id} />}
    </div>
  );
}

/** The URL to paste into whatever is going to call it. */
function InboundUrl({ url, signed }) {
  const toast = useToast();

  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
      toast.success("URL copied");
    } catch {
      // Clipboard access is denied in plenty of ordinary configurations. The
      // URL is on screen and selectable, so this is a convenience, not the
      // only way to get it.
      toast.error("Could not copy — select the URL and copy it manually.");
    }
  }

  return (
    <div className="mt-3 rounded-lg border border-line bg-canvas px-3 py-2">
      <div className="flex items-center gap-2">
        <code className="min-w-0 flex-1 break-all font-mono text-[11px] text-ink-700">{url}</code>
        <button className="btn-quiet shrink-0" onClick={copy}>
          Copy
        </button>
      </div>
      <p className="mt-1 text-xs text-ink-400">
        POST anything here to fire this trigger.{" "}
        {signed
          ? "Requests must carry a valid signature."
          : "Anyone holding this URL can fire it — turn on signatures if the sender can sign."}
      </p>
    </div>
  );
}

/** Recent firings, and what became of each. */
function TriggerEvents({ triggerId }) {
  const { data: events, error, loading, reload } = useApi(
    () => api.triggerEvents(triggerId, { limit: 20 }),
    [triggerId],
  );

  return (
    <div className="mt-4 border-t border-line pt-4">
      <SectionHeader
        title="Recent activity"
        subtitle="Every firing and its result."
      />
      <ErrorBanner message={error} onRetry={reload} />
      {loading && !events ? (
        <Skeleton rows={2} />
      ) : events?.length === 0 ? (
        <p className="py-4 text-center text-sm text-ink-400">
          Nothing yet. Firings show up here with what they produced.
        </p>
      ) : (
        <ul className="divide-y divide-line">
          {events?.map((event) => (
            <li key={event.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2.5">
              <StatusBadge status={event.status} />
              <span className="min-w-0 flex-1 truncate text-sm text-ink-700">
                {event.headline || "(no headline)"}
              </span>
              {event.content_id && (
                <Link className="btn-quiet" to={`/content/${event.content_id}`}>
                  Open draft
                </Link>
              )}
              <span className="font-mono text-[11px] text-ink-400">
                {formatWhen(event.created_at)}
              </span>
              {event.detail && (
                <p className="w-full text-xs text-ink-400">{event.detail}</p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Create/edit. The field list comes from the kind — see `lib/triggers`. */
function TriggerDialog({ trigger, projects, kinds, defaultProjectId, onClose, onSaved, onError }) {
  const [kind, setKind] = useState(trigger?.kind ?? "rss");
  const [projectId, setProjectId] = useState(
    String(trigger?.project_id ?? defaultProjectId ?? projects[0]?.id ?? ""),
  );
  const [name, setName] = useState(trigger?.name ?? "");
  // Keyed by kind so switching kinds and switching back does not lose what was
  // typed, while `configFromForm` still drops anything the chosen kind rejects.
  const [forms, setForms] = useState(() => ({
    [trigger?.kind ?? "rss"]: formFromConfig(trigger?.kind ?? "rss", trigger?.config),
  }));
  const [busy, setBusy] = useState(false);

  const form = forms[kind] ?? formFromConfig(kind, {});
  const setField = (key, value) =>
    setForms((current) => ({ ...current, [kind]: { ...form, [key]: value } }));

  const kindMeta = kinds.find((k) => k.kind === kind);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    const config = configFromForm(kind, form);
    try {
      if (trigger) {
        await api.updateTrigger(trigger.id, { name, config });
        onSaved(null);
      } else {
        onSaved(
          await api.createTrigger({
            project_id: Number(projectId),
            kind,
            name,
            config,
          }),
        );
      }
    } catch (err) {
      onError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      label={trigger ? "Edit trigger" : "Add trigger"}
      onClose={onClose}
      width="xl"
      onSubmit={submit}
    >
      <h2 className="font-display text-2xl text-ink-900">
        {trigger ? "Edit trigger" : "Add a trigger"}
      </h2>

      {!trigger && (
        <>
          <div>
            <label className="label" htmlFor="t-project">
              Project
            </label>
            <select
              id="t-project"
              className="input"
              required
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
            >
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </div>

          <fieldset>
            <legend className="label mb-2">What starts it</legend>
            <div className="grid gap-2 sm:grid-cols-2">
              {kinds.map((option) => (
                <label
                  key={option.kind}
                  className={`flex cursor-pointer gap-2.5 rounded-lg border px-3 py-2.5 text-sm ${
                    kind === option.kind
                      ? "border-brand-600 bg-brand-50"
                      : "border-line hover:border-ink-300"
                  }`}
                >
                  <input
                    type="radio"
                    name="kind"
                    className="mt-0.5"
                    value={option.kind}
                    checked={kind === option.kind}
                    onChange={() => setKind(option.kind)}
                  />
                  <span className="min-w-0">
                    <span className="font-medium text-ink-900">{option.label}</span>
                    <span className="mt-0.5 block text-xs text-ink-500">
                      {option.description}
                    </span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
        </>
      )}

      {trigger && kindMeta && (
        <p className="text-sm text-ink-500">
          <span className="chip mr-2">{kindMeta.label}</span>
          {kindMeta.description}
        </p>
      )}

      <div>
        <label className="label" htmlFor="t-name">
          Name
        </label>
        <input
          id="t-name"
          className="input"
          maxLength={120}
          placeholder="Changelog feed"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <p className="mt-1.5 text-xs text-ink-400">
          Distinguishes multiple triggers on the same project.
        </p>
      </div>

      {fieldsFor(kind).map((field) => (
        <ConfigField
          key={field.key}
          field={field}
          value={form[field.key]}
          onChange={(value) => setField(field.key, value)}
        />
      ))}

      <div className="flex justify-end gap-2 pt-2">
        <button type="button" className="btn-ghost" onClick={onClose}>
          Cancel
        </button>
        <button type="submit" className="btn-primary" disabled={busy}>
          {busy ? "Saving…" : trigger ? "Save changes" : "Create trigger"}
        </button>
      </div>
    </Dialog>
  );
}

function ConfigField({ field, value, onChange }) {
  const id = `t-cfg-${field.key}`;

  if (field.type === "checkbox") {
    return (
      <label className="flex items-start gap-2.5 text-sm text-ink-700">
        <input
          type="checkbox"
          className="mt-0.5"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span>
          {field.label}
          {field.hint && <span className="mt-0.5 block text-xs text-ink-400">{field.hint}</span>}
        </span>
      </label>
    );
  }

  return (
    <div>
      <label className="label" htmlFor={id}>
        {field.label}
        {field.unit && (
          <span className="normal-case tracking-normal text-ink-400"> ({field.unit})</span>
        )}
      </label>

      {field.type === "select" ? (
        <select id={id} className="input" value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">{field.blank}</option>
          {field.options.map((option) => (
            <option key={option} value={option}>
              {titleize(option)}
            </option>
          ))}
        </select>
      ) : field.type === "textarea" ? (
        <textarea
          id={id}
          rows={3}
          className="input resize-y"
          placeholder={field.placeholder}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <input
          id={id}
          type={field.type === "number" ? "number" : "text"}
          className={`input ${field.mono ? "font-mono text-xs" : ""}`}
          required={field.required}
          step={field.step}
          min={field.min}
          max={field.max}
          placeholder={field.placeholder}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      )}

      {field.hint && <p className="mt-1.5 text-xs text-ink-400">{field.hint}</p>}
    </div>
  );
}

/**
 * The signing secret, shown exactly once.
 *
 * Herald stores it encrypted and has no endpoint that returns it again, so this
 * dialog is the only chance to copy it. Saying so plainly is the difference
 * between a rotation and a support question.
 */
function SecretDialog({ trigger, onClose }) {
  const toast = useToast();

  async function copy(text, what) {
    try {
      await navigator.clipboard.writeText(text);
      toast.success(`${what} copied`);
    } catch {
      toast.error("Could not copy — select it and copy manually.");
    }
  }

  return (
    <Dialog
      label="Webhook trigger credentials"
      onClose={onClose}
      width="xl"
    >
      <h2 className="font-display text-2xl text-ink-900">Your trigger is ready</h2>
      <p className="text-sm text-ink-500">
        POST to this URL to fire the trigger. The secret is shown once — copy it now.
      </p>

      <div>
        <p className="label">URL</p>
        <div className="flex items-center gap-2 rounded-lg border border-line bg-canvas px-3 py-2">
          <code className="min-w-0 flex-1 break-all font-mono text-[11px] text-ink-700">
            {trigger.inbound_url}
          </code>
          <button className="btn-quiet shrink-0" onClick={() => copy(trigger.inbound_url, "URL")}>
            Copy
          </button>
        </div>
      </div>

      <div>
        <p className="label">Signing secret</p>
        <div className="flex items-center gap-2 rounded-lg border border-line bg-canvas px-3 py-2">
          <code className="min-w-0 flex-1 break-all font-mono text-[11px] text-ink-700">
            {trigger.secret}
          </code>
          <button className="btn-quiet shrink-0" onClick={() => copy(trigger.secret, "Secret")}>
            Copy
          </button>
        </div>
        <div className="mt-1.5 space-y-1.5 text-xs text-ink-400">
          <p>
            Send as <code>X-Herald-Signature</code>:
          </p>
          <pre className="overflow-x-auto rounded border border-line bg-canvas px-2.5 py-1.5 font-mono text-[11px] text-ink-700">
            t=&lt;unix seconds&gt;,v1=HMAC_SHA256(secret, &quot;&lt;t&gt;.&lt;raw body&gt;&quot;)
          </pre>
          <p>
            Signatures expire after five minutes.
          </p>
        </div>
      </div>

      <div className="flex justify-end pt-2">
        <button className="btn-primary" onClick={onClose}>
          I&rsquo;ve saved it
        </button>
      </div>
    </Dialog>
  );
}
