import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Empty, ErrorBanner, Skeleton, Tag } from "../components/ui/Bits";
import Dialog from "../components/ui/Dialog";
import { useToast } from "../components/ui/Toast";
import { useApi } from "../hooks/useApi";
import { api } from "../lib/api";
import { formatWhen, titleize } from "../lib/format";

const TONES = ["technical", "casual", "marketing"];
const AUTOPILOT_MODES = [
  { value: "off", label: "Off", hint: "Notice changes, write nothing." },
  { value: "draft", label: "Draft", hint: "Write, then queue for your review." },
  {
    value: "auto",
    label: "Auto",
    hint: "Write and publish when the model is confident.",
  },
];

const EMPTY_FORM = {
  name: "",
  description: "",
  repo_url: "",
  live_url: "",
  tech_stack: "",
  target_audience: "",
  keywords: "",
  tone: "technical",
  autopilot_mode: "draft",
  auto_canonical: true,
  canonical_platform: "",
  utm_enabled: true,
  utm_campaign: "",
};

/** The project registry: what Pulse is allowed to write about. */
export default function Projects() {
  const toast = useToast();
  const { data: projects, error, loading, reload } = useApi(() => api.listProjects(), []);
  const [editing, setEditing] = useState(null); // project | "new" | "quick" | null

  return (
    <div className="stagger space-y-6">
      <div className="flex items-end justify-between gap-4">
        <div>
          <h1 className="page-title">Projects</h1>
          <p className="mt-1 text-sm text-ink-500">
            What the robot writes about.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn-ghost" onClick={() => setEditing("new")}>
            Advanced
          </button>
          <button className="btn-primary" onClick={() => setEditing("quick")}>
            Quick add
          </button>
        </div>
      </div>

      <ErrorBanner message={error} onRetry={reload} />

      {loading && !projects ? (
        <Skeleton rows={3} />
      ) : projects?.length === 0 ? (
        <Empty
          title="No projects yet"
          hint="Register what you ship — the robot watches and writes."
          action={
            <button className="btn-primary mt-1" onClick={() => setEditing("quick")}>
              Add your first project
            </button>
          }
        />
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {projects?.map((project) => (
            <ProjectCard
              key={project.id}
              project={project}
              onEdit={() => setEditing(project)}
              onChanged={reload}
            />
          ))}
        </div>
      )}

      {editing === "quick" && (
        <QuickProjectDialog
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
            toast.success("Project created — autopilot is drafting content");
          }}
          onAdvanced={() => setEditing("new")}
          onError={(message) => toast.error(message)}
        />
      )}

      {editing && editing !== "quick" && (
        <ProjectDialog
          project={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
            toast.success("Project saved");
          }}
          onError={(message) => toast.error(message)}
        />
      )}
    </div>
  );
}

function ProjectCard({ project, onEdit, onChanged }) {
  const toast = useToast();
  const [scanning, setScanning] = useState(false);

  async function scan() {
    setScanning(true);
    try {
      const result = await api.scanProject(project.id);
      const bits = [`${result.new_commit_count} new commit(s)`];
      if (result.new_release_tag) bits.push(`release ${result.new_release_tag}`);
      toast.success(`${result.full_name}: ${bits.join(", ")}`);
      onChanged();
    } catch (err) {
      toast.error(`Repo scan failed: ${err.message}`);
    } finally {
      setScanning(false);
    }
  }

  async function remove() {
    if (
      !window.confirm(
        `Delete "${project.name}" and every piece of content written about it? ` +
          "This cannot be undone.",
      )
    ) {
      return;
    }
    try {
      await api.deleteProject(project.id);
      toast.success("Project deleted");
      onChanged();
    } catch (err) {
      toast.error(`Could not delete project: ${err.message}`);
    }
  }

  return (
    <div className="panel flex flex-col gap-3 p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="truncate text-base font-semibold text-ink-900">{project.name}</h2>
          <p className="mt-1 font-mono text-[11px] text-ink-400">
            {project.repo_full_name || project.live_url || "no links"}
          </p>
        </div>
        <span
          className={`badge ${
            project.autopilot_mode === "off"
              ? "bg-canvas text-ink-500"
              : project.autopilot_mode === "auto"
                ? "bg-good-wash text-good"
                : "bg-brand-50 text-brand-600"
          }`}
          title="Autopilot mode"
        >
          {project.autopilot_mode}
        </span>
      </div>

      {project.description && (
        <p className="line-clamp-2 text-sm text-ink-500">{project.description}</p>
      )}

      {project.tech_stack.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {project.tech_stack.slice(0, 5).map((tech) => (
            <Tag key={tech}>{tech}</Tag>
          ))}
        </div>
      )}

      <dl className="flex flex-wrap gap-x-5 gap-y-1 font-mono text-[11px] text-ink-500">
        <span>
          {project.content_count} piece{project.content_count === 1 ? "" : "s"}
        </span>
        <span>{project.published_count} published</span>
        <span>{titleize(project.tone)}</span>
        {project.last_scanned_at && (
          <span title="Last repo scan">scanned {formatWhen(project.last_scanned_at)}</span>
        )}
      </dl>

      <IdeasPanel projectId={project.id} />

      <div className="mt-auto flex flex-wrap items-center gap-2 pt-1">
        <Link to={`/content?project=${project.id}`} className="btn-ghost">
          Content
        </Link>
        {project.repo_full_name && (
          <button className="btn-ghost" onClick={scan} disabled={scanning}>
            {scanning ? "Scanning…" : "Scan repo"}
          </button>
        )}
        <button className="btn-quiet" onClick={onEdit}>
          Edit
        </button>
        <button className="btn-quiet ml-auto text-bad" onClick={remove}>
          Delete
        </button>
      </div>
    </div>
  );
}

/** Create/edit form. Lists are entered as comma-separated text — the fastest
 *  input for a handful of tags, and it round-trips cleanly. */
function ProjectDialog({ project, onClose, onSaved, onError }) {
  const [form, setForm] = useState(() =>
    project
      ? {
          name: project.name,
          description: project.description,
          // `?? ""` on exactly the three fields `ProjectOut` declares nullable
          // (see backend/app/schemas/project.py). The rest carry non-null
          // defaults in the schema, and coalescing them here only hid the
          // question of which ones a form actually has to defend against.
          // An input handed `undefined` goes uncontrolled, which the console
          // guard in the test setup turns into a failure rather than a shrug.
          repo_url: project.repo_url ?? "",
          live_url: project.live_url ?? "",
          tech_stack: project.tech_stack.join(", "),
          target_audience: project.target_audience,
          keywords: project.keywords.join(", "),
          tone: project.tone,
          autopilot_mode: project.autopilot_mode,
          auto_canonical: project.auto_canonical,
          canonical_platform: project.canonical_platform ?? "",
          utm_enabled: project.utm_enabled,
          utm_campaign: project.utm_campaign,
        }
      : EMPTY_FORM,
  );
  const [busy, setBusy] = useState(false);
  // Only the platforms that can actually publish are worth offering as the
  // canonical home — naming an unfinished adapter would designate an original
  // that never appears, and every copy would wait behind it for nothing.
  const { data: platforms } = useApi(() => api.platforms(), []);

  const set = (key) => (event) =>
    setForm((current) => ({ ...current, [key]: event.target.value }));

  const splitList = (value) =>
    value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    const payload = {
      ...form,
      tech_stack: splitList(form.tech_stack),
      keywords: splitList(form.keywords),
      repo_url: form.repo_url || null,
      live_url: form.live_url || null,
      // "" is the select's way of saying "no platform is privileged", which the
      // API spells as null.
      canonical_platform: form.canonical_platform || null,
    };
    try {
      if (project) await api.updateProject(project.id, payload);
      else await api.createProject(payload);
      onSaved();
    } catch (err) {
      onError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      label={project ? "Edit project" : "Add project"}
      onClose={onClose}
      width="xl"
      onSubmit={submit}
    >
      <h2 className="font-display text-2xl text-ink-900">
        {project ? `Edit ${project.name}` : "Add a project"}
      </h2>

      <div>
        <label className="label" htmlFor="p-name">
          Name
        </label>
        <input
          id="p-name"
          required
          className="input"
          value={form.name}
          onChange={set("name")}
        />
      </div>

      <div>
        <label className="label" htmlFor="p-desc">
          What it is
        </label>
        <textarea
          id="p-desc"
          rows={3}
          className="input resize-y"
          placeholder="One paragraph — the robot builds every post from this."
          value={form.description}
          onChange={set("description")}
        />
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label className="label" htmlFor="p-repo">
            Repo URL
          </label>
          <input
            id="p-repo"
            className="input font-mono text-xs"
            placeholder="https://github.com/you/project"
            value={form.repo_url}
            onChange={set("repo_url")}
          />
        </div>
        <div>
          <label className="label" htmlFor="p-live">
            Live URL
          </label>
          <input
            id="p-live"
            className="input font-mono text-xs"
            placeholder="https://project.example.com"
            value={form.live_url}
            onChange={set("live_url")}
          />
        </div>
      </div>

      <div>
        <label className="label" htmlFor="p-stack">
          Tech stack <span className="normal-case tracking-normal">(comma separated)</span>
        </label>
        <input
          id="p-stack"
          className="input"
          placeholder="FastAPI, React, PostgreSQL"
          value={form.tech_stack}
          onChange={set("tech_stack")}
        />
      </div>

      <div>
        <label className="label" htmlFor="p-audience">
          Target audience
        </label>
        <input
          id="p-audience"
          className="input"
          placeholder="Indie developers shipping side projects"
          value={form.target_audience}
          onChange={set("target_audience")}
        />
      </div>

      <div>
        <label className="label" htmlFor="p-keywords">
          SEO keywords{" "}
          <span className="normal-case tracking-normal">(comma separated)</span>
        </label>
        <input
          id="p-keywords"
          className="input"
          placeholder="marketing automation, developer marketing"
          value={form.keywords}
          onChange={set("keywords")}
        />
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label className="label" htmlFor="p-tone">
            Tone
          </label>
          <select id="p-tone" className="input" value={form.tone} onChange={set("tone")}>
            {TONES.map((tone) => (
              <option key={tone} value={tone}>
                {titleize(tone)}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="p-autopilot">
            Autopilot
          </label>
          <select
            id="p-autopilot"
            className="input"
            value={form.autopilot_mode}
            onChange={set("autopilot_mode")}
          >
            {AUTOPILOT_MODES.map((mode) => (
              <option key={mode.value} value={mode.value}>
                {mode.label}
              </option>
            ))}
          </select>
          <p className="mt-1.5 text-xs text-ink-400">
            {AUTOPILOT_MODES.find((m) => m.value === form.autopilot_mode)?.hint}
          </p>
        </div>
      </div>

      <fieldset className="space-y-3 rounded-lg border border-line px-4 py-3">
        <legend className="label px-1">Syndication</legend>

        <label className="flex items-start gap-2.5 text-sm text-ink-700">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={form.auto_canonical}
            onChange={(e) =>
              setForm((current) => ({ ...current, auto_canonical: e.target.checked }))
            }
          />
          <span>
            Auto-set canonical URL
            <span className="mt-0.5 block text-xs text-ink-400">
              First published URL becomes the original — copies won&rsquo;t
              compete in search.
            </span>
          </span>
        </label>

        <div>
          <label className="label" htmlFor="p-canonical">
            Primary destination
          </label>
          <select
            id="p-canonical"
            className="input"
            value={form.canonical_platform}
            disabled={!form.auto_canonical}
            onChange={set("canonical_platform")}
          >
            <option value="">Whichever publishes first</option>
            {(platforms ?? [])
              .filter((p) => p.implemented)
              .map((p) => (
                <option key={p.platform} value={p.platform}>
                  {p.display_name}
                </option>
              ))}
          </select>
          <p className="mt-1.5 text-xs text-ink-400">
            {form.canonical_platform
              ? "This destination owns the canonical URL."
              : "First to publish owns the canonical URL."}
          </p>
        </div>
      </fieldset>

      <fieldset className="space-y-3 rounded-lg border border-line px-4 py-3">
        <legend className="label px-1">Attribution</legend>

        <label className="flex items-start gap-2.5 text-sm text-ink-700">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={form.utm_enabled}
            onChange={(e) =>
              setForm((current) => ({ ...current, utm_enabled: e.target.checked }))
            }
          />
          <span>
            Tag links with UTM parameters
            <span className="mt-0.5 block text-xs text-ink-400">
              Links carry the platform name so analytics can attribute visits.
            </span>
          </span>
        </label>

        <div>
          <label className="label" htmlFor="p-utm-campaign">
            Campaign name
          </label>
          <input
            id="p-utm-campaign"
            className="input"
            value={form.utm_campaign}
            disabled={!form.utm_enabled}
            placeholder={project?.slug || "the project slug"}
            onChange={set("utm_campaign")}
            maxLength={120}
          />
          <p className="mt-1.5 text-xs text-ink-400">
            The <code>utm_campaign</code> value. Leave blank to use the
            project slug.
          </p>
        </div>
      </fieldset>

      <div className="flex justify-end gap-2 pt-2">
        <button type="button" className="btn-ghost" onClick={onClose}>
          Cancel
        </button>
        <button type="submit" className="btn-primary" disabled={busy}>
          {busy ? "Saving…" : "Save project"}
        </button>
      </div>
    </Dialog>
  );
}

function QuickProjectDialog({ onClose, onSaved, onAdvanced, onError }) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    const isRepo = url.includes("github.com") || url.includes("gitlab.com") || url.includes("bitbucket.org");
    const payload = {
      name,
      description: "",
      repo_url: isRepo ? url : null,
      live_url: isRepo ? null : url || null,
      tech_stack: [],
      keywords: [],
      tone: "technical",
      autopilot_mode: "draft",
      auto_canonical: true,
      canonical_platform: null,
      utm_enabled: true,
      utm_campaign: "",
      target_audience: "",
    };
    try {
      await api.createProject(payload);
      onSaved();
    } catch (err) {
      onError(err.message);
      setBusy(false);
    }
  }

  return (
    <Dialog label="Quick add project" onClose={onClose} closable={!busy} onSubmit={submit}>
      <h2 className="font-display text-2xl text-ink-900">Quick add</h2>
      <p className="text-sm text-ink-500">
        Just a name and URL — autopilot starts drafting right away.
      </p>

      <div>
        <label className="label" htmlFor="q-name">Name</label>
        <input
          id="q-name"
          required
          className="input"
          placeholder="My Project"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </div>

      <div>
        <label className="label" htmlFor="q-url">URL (repo or live site)</label>
        <input
          id="q-url"
          className="input font-mono text-xs"
          placeholder="https://github.com/you/project or https://myapp.com"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
        />
      </div>

      <div className="rounded-lg bg-good-wash/50 px-4 py-3 text-sm text-good">
        Autopilot is ON — Pulse will start drafting content for review.
      </div>

      <div className="flex items-center justify-between gap-2 pt-2">
        <button type="button" className="btn-quiet text-brand-500" onClick={onAdvanced}>
          More options
        </button>
        <div className="flex gap-2">
          <button type="button" className="btn-ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="btn-primary" disabled={busy || !name.trim()}>
            {busy ? "Creating…" : "Create project"}
          </button>
        </div>
      </div>
    </Dialog>
  );
}

function IdeasPanel({ projectId }) {
  const toast = useToast();
  const navigate = useNavigate();
  const { data: ideas, reload } = useApi(() => api.listIdeas(projectId), [projectId]);
  const [refreshing, setRefreshing] = useState(false);
  const [writing, setWriting] = useState(null);

  async function refresh() {
    setRefreshing(true);
    try {
      await api.listIdeas(projectId, true);
      reload();
    } catch (err) {
      toast.error(`Could not generate ideas: ${err.message}`);
    } finally {
      setRefreshing(false);
    }
  }

  async function write(idea) {
    setWriting(idea.id);
    try {
      const content = await api.writeFromIdea(idea.id);
      toast.success("Draft created");
      navigate(`/content/${content.id}`);
    } catch (err) {
      toast.error(`Could not write draft from idea: ${err.message}`);
    } finally {
      setWriting(null);
    }
  }

  if (!ideas || ideas.length === 0) {
    return (
      <div className="flex items-center gap-2 text-xs text-ink-400">
        <span>No ideas yet</span>
        <button className="btn-quiet text-xs" onClick={refresh} disabled={refreshing}>
          {refreshing ? "Generating…" : "Generate ideas"}
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-ink-500">Ideas</span>
        <button className="btn-quiet text-xs" onClick={refresh} disabled={refreshing}>
          {refreshing ? "Refreshing…" : "Refresh"}
        </button>
      </div>
      {ideas.slice(0, 3).map((idea) => (
        <div key={idea.id} className="flex items-center justify-between gap-2 rounded-lg bg-canvas px-3 py-2 text-sm">
          <div className="min-w-0">
            <span className="truncate text-ink-800">{idea.headline}</span>
            <span className="ml-2 text-xs text-ink-400">{idea.content_type}</span>
          </div>
          <button
            className="btn-quiet shrink-0 text-xs"
            onClick={() => write(idea)}
            disabled={writing === idea.id}
            title="Create a draft from this idea"
          >
            {writing === idea.id ? "Writing…" : "Write"}
          </button>
        </div>
      ))}
    </div>
  );
}
