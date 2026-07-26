import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  Confidence,
  Empty,
  ErrorBanner,
  Skeleton,
  StatusBadge,
} from "../components/ui/Bits";
import { useToast } from "../components/ui/Toast";
import { useApi } from "../hooks/useApi";
import { api } from "../lib/api";
import { formatWhen, titleize } from "../lib/format";

const STATUSES = ["draft", "review", "approved", "published", "failed", "archived"];
const TYPES = [
  "tutorial",
  "announcement",
  "feature_spotlight",
  "comparison",
  "how_to",
];

/** Everything written, filterable, plus the button that writes something new. */
export default function ContentList() {
  const [params, setParams] = useSearchParams();
  const toast = useToast();
  const [composing, setComposing] = useState(false);

  const projectId = params.get("project") || "";
  const status = params.get("status") || "";
  const type = params.get("type") || "";

  const { data: projects } = useApi(() => api.listProjects(), []);
  const { data, error, loading, reload } = useApi(
    () =>
      api.listContent({
        project_id: projectId || undefined,
        status: status || undefined,
        content_type: type || undefined,
      }),
    [projectId, status, type],
  );

  const setFilter = (key, value) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next);
  };

  return (
    <div className="stagger space-y-6">
      <div className="flex items-end justify-between gap-4">
        <div>
          <h1 className="page-title">Content</h1>
          <p className="mt-1 text-sm text-ink-500">
            Everything Herald has written, and everything you have.
          </p>
        </div>
        <button
          className="btn-primary"
          onClick={() => setComposing(true)}
          disabled={!projects?.length}
          title={projects?.length ? undefined : "Register a project first"}
        >
          Generate
        </button>
      </div>

      <div className="flex flex-wrap gap-2">
        <select
          className="input w-auto"
          value={projectId}
          onChange={(e) => setFilter("project", e.target.value)}
          aria-label="Filter by project"
        >
          <option value="">All projects</option>
          {projects?.map((project) => (
            <option key={project.id} value={project.id}>
              {project.name}
            </option>
          ))}
        </select>
        <select
          className="input w-auto"
          value={status}
          onChange={(e) => setFilter("status", e.target.value)}
          aria-label="Filter by status"
        >
          <option value="">Any status</option>
          {STATUSES.map((value) => (
            <option key={value} value={value}>
              {titleize(value)}
            </option>
          ))}
        </select>
        <select
          className="input w-auto"
          value={type}
          onChange={(e) => setFilter("type", e.target.value)}
          aria-label="Filter by content type"
        >
          <option value="">Any type</option>
          {TYPES.map((value) => (
            <option key={value} value={value}>
              {titleize(value)}
            </option>
          ))}
        </select>
      </div>

      <ErrorBanner message={error} onRetry={reload} />

      {loading && !data ? (
        <Skeleton rows={5} />
      ) : data?.length === 0 ? (
        <Empty
          title={status || type || projectId ? "Nothing matches" : "Nothing written yet"}
          hint={
            status || type || projectId
              ? "Try clearing a filter."
              : "Generate a draft from one of your registered projects."
          }
          action={
            projects?.length ? (
              <button className="btn-primary mt-1" onClick={() => setComposing(true)}>
                Generate a draft
              </button>
            ) : (
              <Link to="/projects" className="btn-primary mt-1">
                Register a project
              </Link>
            )
          }
        />
      ) : (
        <ul className="panel divide-y divide-line">
          {data?.map((item) => (
            <li key={item.id}>
              <Link
                to={`/content/${item.id}`}
                className="flex items-start justify-between gap-4 px-5 py-4 transition-colors hover:bg-canvas"
              >
                <div className="min-w-0">
                  <p className="truncate font-medium text-ink-900">{item.title}</p>
                  <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-ink-400">
                    <span>{item.project_name}</span>
                    <span>{titleize(item.content_type)}</span>
                    <span>{item.word_count} words</span>
                    <span>{item.read_minutes} min read</span>
                    <span>{formatWhen(item.created_at)}</span>
                    {item.confidence !== null && <Confidence value={item.confidence} />}
                  </p>
                  {item.publications.length > 0 && (
                    <p className="mt-2 flex flex-wrap gap-1.5">
                      {item.publications.map((publication) => (
                        <span key={publication.id} className="chip">
                          {titleize(publication.platform)} · {publication.status}
                        </span>
                      ))}
                    </p>
                  )}
                </div>
                <StatusBadge status={item.status} />
              </Link>
            </li>
          ))}
        </ul>
      )}

      {composing && (
        <GenerateDialog
          projects={projects ?? []}
          defaultProjectId={projectId}
          onClose={() => setComposing(false)}
          onDone={() => {
            setComposing(false);
            reload();
          }}
          onError={(message) => toast.error(message)}
        />
      )}
    </div>
  );
}

/**
 * The generate form. Runs the model inline and navigates nowhere until it comes
 * back — a draft that takes 20 seconds is worth waiting for, and a spinner that
 * says what it is doing beats a background job the user cannot see.
 */
function GenerateDialog({ projects, defaultProjectId, onClose, onDone, onError }) {
  const [projectId, setProjectId] = useState(defaultProjectId || projects[0]?.id || "");
  const [contentType, setContentType] = useState("feature_spotlight");
  const [instructions, setInstructions] = useState("");
  const [includeActivity, setIncludeActivity] = useState(false);
  const [busy, setBusy] = useState(false);

  const project = projects.find((p) => String(p.id) === String(projectId));

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    try {
      await api.generateContent({
        project_id: Number(projectId),
        content_type: contentType,
        instructions,
        include_repo_activity: includeActivity,
      });
      onDone();
    } catch (err) {
      onError(err.message);
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-ink-900/25 p-4 backdrop-blur-sm sm:p-8"
      role="dialog"
      aria-modal="true"
      aria-label="Generate content"
      onMouseDown={(e) => e.target === e.currentTarget && !busy && onClose()}
    >
      <form onSubmit={submit} className="panel my-auto w-full max-w-lg space-y-4 p-6 shadow-pop">
        <h2 className="font-display text-2xl text-ink-900">Generate a draft</h2>

        <div>
          <label className="label" htmlFor="g-project">
            Project
          </label>
          <select
            id="g-project"
            className="input"
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
          >
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="label" htmlFor="g-type">
            Type
          </label>
          <select
            id="g-type"
            className="input"
            value={contentType}
            onChange={(e) => setContentType(e.target.value)}
          >
            {TYPES.map((value) => (
              <option key={value} value={value}>
                {titleize(value)}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="label" htmlFor="g-instructions">
            Direction{" "}
            <span className="normal-case tracking-normal">(optional)</span>
          </label>
          <textarea
            id="g-instructions"
            rows={3}
            className="input resize-y"
            placeholder="Focus on the Celery retry logic, and mention the free-tier model chain."
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
          />
        </div>

        {project?.repo_full_name && (
          <label className="flex items-start gap-2.5 text-sm text-ink-700">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={includeActivity}
              onChange={(e) => setIncludeActivity(e.target.checked)}
            />
            <span>
              Pull recent commits and releases from{" "}
              <span className="font-mono text-xs">{project.repo_full_name}</span>
              <span className="mt-0.5 block text-xs text-ink-400">
                Worth it for announcements. Adds a round trip.
              </span>
            </span>
          </label>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <button type="button" className="btn-ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="btn-primary" disabled={busy || !projectId}>
            {busy ? "Writing…" : "Generate"}
          </button>
        </div>
        {busy && (
          <p className="text-xs text-ink-400">
            Asking the model for a full draft. This usually takes 10–30 seconds.
          </p>
        )}
      </form>
    </div>
  );
}
