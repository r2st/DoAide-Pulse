import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Empty, ErrorBanner, Skeleton } from "../components/ui/Bits";
import { useToast } from "../components/ui/Toast";
import { useApi } from "../hooks/useApi";
import { api } from "../lib/api";
import { formatWhen, titleize } from "../lib/format";
import {
  MODES,
  blankVariable,
  formFromTemplate,
  groupBuiltins,
  initialValues,
  insertAt,
  labelFor,
  nameProblem,
  payloadFromForm,
  stillMissing,
  undeclaredIn,
  unusedVariables,
} from "../lib/templates";
import { CONTENT_TYPES } from "../lib/triggers";

/**
 * Templates: the shape of a piece, written once and reused.
 *
 * Organised as a list of templates rather than by project because a template is
 * owned by the user precisely so it can be used on any of them — grouping by
 * project would be grouping by a field that is only a default.
 *
 * The editor validates placeholders as you type rather than waiting for the
 * server's 422. Same rules, run twice: see `lib/templates.js` for why that
 * duplication is deliberate.
 */
export default function Templates() {
  const toast = useToast();
  const { data: projects } = useApi(() => api.listProjects(), []);
  const { data: builtins } = useApi(() => api.templateBuiltins(), []);
  const { data: templates, error, loading, reload } = useApi(() => api.listTemplates(), []);

  const [editing, setEditing] = useState(null); // template | "new" | null
  const [using, setUsing] = useState(null); // template | null

  return (
    <div className="stagger space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="page-title">Templates</h1>
          <p className="mt-1 max-w-2xl text-sm text-ink-500">
            A piece you write the shape of once. Fill in the blanks and Herald
            either publishes it exactly as written or hands it to the model as
            the brief — your choice, per template.
          </p>
        </div>
        <button className="btn-primary" onClick={() => setEditing("new")}>
          New template
        </button>
      </div>

      <ErrorBanner message={error} onRetry={reload} />

      {loading && !templates ? (
        <Skeleton rows={3} />
      ) : templates?.length === 0 ? (
        <Empty
          title="No templates yet"
          hint="A changelog, a release note, a conference post — anything whose shape you already know and would rather not have rewritten every time."
          action={
            <button className="btn-primary mt-1" onClick={() => setEditing("new")}>
              Write your first template
            </button>
          }
        />
      ) : (
        <div className="space-y-4">
          {templates?.map((template) => (
            <TemplateCard
              key={template.id}
              template={template}
              project={projects?.find((p) => p.id === template.default_project_id)}
              onEdit={() => setEditing(template)}
              onUse={() => setUsing(template)}
              onChanged={reload}
            />
          ))}
        </div>
      )}

      {editing && (
        <TemplateDialog
          template={editing === "new" ? null : editing}
          projects={projects ?? []}
          builtins={builtins ?? []}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
            toast.success("Template saved");
          }}
          onError={(message) => toast.error(message)}
        />
      )}

      {using && (
        <UseDialog
          template={using}
          projects={projects ?? []}
          onClose={() => setUsing(null)}
          onDone={reload}
        />
      )}
    </div>
  );
}

function TemplateCard({ template, project, onEdit, onUse, onChanged }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const mode = MODES.find((m) => m.value === template.mode);

  async function remove() {
    if (
      !window.confirm(
        `Delete "${template.name}"? The pieces it has already produced are not affected.`,
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      await api.deleteTemplate(template.id);
      toast.success("Template deleted");
      onChanged();
    } catch (err) {
      toast.error(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="panel p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="truncate font-medium text-ink-900">{template.name}</h2>
            <span className="badge badge-neutral">{mode?.label ?? template.mode}</span>
            <span className="badge badge-neutral">{titleize(template.content_type)}</span>
          </div>
          {template.description && (
            <p className="mt-1 max-w-2xl text-sm text-ink-500">{template.description}</p>
          )}
          <p className="mt-2 text-xs text-ink-400">
            {template.variables.length === 0
              ? "No blanks to fill"
              : `${template.variables.length} blank${template.variables.length === 1 ? "" : "s"} to fill`}
            {project && ` · usually ${project.name}`}
            {" · "}
            {template.use_count === 0
              ? "never used"
              : `used ${template.use_count} time${template.use_count === 1 ? "" : "s"}`}
            {" · edited "}
            {formatWhen(template.updated_at)}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button className="btn-primary" onClick={onUse}>
            Use
          </button>
          <button className="btn-ghost" onClick={onEdit}>
            Edit
          </button>
          <button className="btn-ghost text-rose-600" disabled={busy} onClick={remove}>
            Delete
          </button>
        </div>
      </div>

      {template.placeholders_used.length > 0 && (
        <p className="mt-3 flex flex-wrap gap-1.5">
          {template.placeholders_used.map((name) => (
            <code key={name} className="rounded bg-canvas px-1.5 py-0.5 text-xs text-ink-600">
              {`{{${name}}}`}
            </code>
          ))}
        </p>
      )}
    </article>
  );
}

// --------------------------------------------------------------------------- //
// The editor                                                                   //
// --------------------------------------------------------------------------- //

function TemplateDialog({ template, projects, builtins, onClose, onSaved, onError }) {
  const [form, setForm] = useState(() => formFromTemplate(template));
  const [busy, setBusy] = useState(false);
  const bodyRef = useRef(null);

  const declared = form.variables.map((v) => v.name.trim()).filter(Boolean);
  const undeclared = undeclaredIn(
    declared,
    builtins,
    form.title_template,
    form.body_template,
  );
  const unused = unusedVariables(declared, form.title_template, form.body_template);
  const nameErrors = form.variables.map((v, i) =>
    nameProblem(
      v.name,
      builtins,
      declared.filter((_, j) => j !== i),
    ),
  );

  const setField = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  const setVariable = (index, patch) =>
    setForm((current) => ({
      ...current,
      variables: current.variables.map((v, i) => (i === index ? { ...v, ...patch } : v)),
    }));

  function addVariable(name = "") {
    setForm((current) => ({
      ...current,
      variables: [...current.variables, blankVariable(name)],
    }));
  }

  function removeVariable(index) {
    setForm((current) => ({
      ...current,
      variables: current.variables.filter((_, i) => i !== index),
    }));
  }

  /** Drop a placeholder in at the caret, so the insert menu is one click. */
  function insertPlaceholder(name) {
    const field = bodyRef.current;
    const { value, caret } = insertAt(
      form.body_template,
      field?.selectionStart,
      `{{${name}}}`,
    );
    setField("body_template", value);
    // Restore focus and put the caret after what we inserted, or the next
    // keystroke lands at the start of the textarea.
    requestAnimationFrame(() => {
      field?.focus();
      field?.setSelectionRange(caret, caret);
    });
  }

  const blocked = undeclared.length > 0 || nameErrors.some(Boolean) || !form.name.trim();

  async function submit(event) {
    event.preventDefault();
    if (blocked) return;
    setBusy(true);
    try {
      const payload = payloadFromForm(form);
      if (template) await api.updateTemplate(template.id, payload);
      else await api.createTemplate(payload);
      onSaved();
    } catch (err) {
      onError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-ink-900/25 p-4 backdrop-blur-sm sm:p-8"
      role="dialog"
      aria-modal="true"
      aria-label={template ? "Edit template" : "New template"}
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <form onSubmit={submit} className="panel my-auto w-full max-w-3xl space-y-5 p-6 shadow-pop">
        <h2 className="font-display text-2xl text-ink-900">
          {template ? "Edit template" : "New template"}
        </h2>

        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label className="label" htmlFor="tpl-name">
              Name
            </label>
            <input
              id="tpl-name"
              className="input"
              required
              value={form.name}
              placeholder="Weekly changelog"
              onChange={(e) => setField("name", e.target.value)}
            />
          </div>
          <div>
            <label className="label" htmlFor="tpl-desc">
              Description
            </label>
            <input
              id="tpl-desc"
              className="input"
              value={form.description}
              placeholder="What this one is for."
              onChange={(e) => setField("description", e.target.value)}
            />
          </div>
        </div>

        <fieldset>
          <legend className="label mb-2">What Herald does with it</legend>
          <div className="grid gap-2 sm:grid-cols-2">
            {MODES.map((option) => (
              <label
                key={option.value}
                className={`flex cursor-pointer gap-2.5 rounded-lg border px-3 py-2.5 text-sm ${
                  form.mode === option.value
                    ? "border-brand-600 bg-brand-50"
                    : "border-line hover:border-ink-300"
                }`}
              >
                <input
                  type="radio"
                  name="tpl-mode"
                  className="mt-0.5"
                  value={option.value}
                  checked={form.mode === option.value}
                  onChange={() => setField("mode", option.value)}
                />
                <span>
                  <span className="font-medium text-ink-900">{option.label}</span>
                  <span className="mt-0.5 block text-xs text-ink-500">{option.hint}</span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>

        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label className="label" htmlFor="tpl-type">
              Kind of piece
            </label>
            <select
              id="tpl-type"
              className="input"
              value={form.content_type}
              onChange={(e) => setField("content_type", e.target.value)}
            >
              {CONTENT_TYPES.map((type) => (
                <option key={type} value={type}>
                  {titleize(type)}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="tpl-project">
              Usually for
            </label>
            <select
              id="tpl-project"
              className="input"
              value={form.default_project_id}
              onChange={(e) => setField("default_project_id", e.target.value)}
            >
              <option value="">No default — ask each time</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div>
          <label className="label" htmlFor="tpl-title">
            Headline
          </label>
          <input
            id="tpl-title"
            className="input font-mono text-sm"
            value={form.title_template}
            placeholder="{{project.name}} — week of {{date.long}}"
            onChange={(e) => setField("title_template", e.target.value)}
          />
        </div>

        <div>
          <label className="label" htmlFor="tpl-body">
            {form.mode === "prompt" ? "Instructions for the model" : "The piece"}
          </label>
          <textarea
            id="tpl-body"
            ref={bodyRef}
            className="input min-h-[12rem] font-mono text-sm"
            value={form.body_template}
            placeholder={
              form.mode === "prompt"
                ? "Write about {{feature}} for {{project.name}}. Mention {{link}}."
                : "## What shipped\n\n{{summary}}\n\nRead more: {{link}}"
            }
            onChange={(e) => setField("body_template", e.target.value)}
          />
          <p className="mt-1 text-xs text-ink-400">
            Anything in {"{{double braces}}"} is a blank. A line whose only
            content is a blank that comes out empty is dropped, so nothing
            publishes “Read more:” with nothing after it.
          </p>
        </div>

        <BuiltinMenu builtins={builtins} onInsert={insertPlaceholder} />

        {undeclared.length > 0 && (
          <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2.5 text-sm">
            <p className="text-amber-900">
              {undeclared.length === 1 ? "This blank is" : "These blanks are"} not
              declared, so {undeclared.length === 1 ? "it" : "they"} would render
              empty forever:
            </p>
            <p className="mt-2 flex flex-wrap gap-2">
              {undeclared.map((name) => (
                <button
                  key={name}
                  type="button"
                  className="btn-ghost border border-amber-300 px-2 py-1 text-xs"
                  onClick={() => addVariable(name)}
                >
                  Declare {`{{${name}}}`}
                </button>
              ))}
            </p>
          </div>
        )}

        {unused.length > 0 && (
          <p className="text-xs text-ink-400">
            Declared but never used: {unused.join(", ")}. The form will still ask
            for {unused.length === 1 ? "it" : "them"}.
          </p>
        )}

        <fieldset className="space-y-3">
          <legend className="label mb-2">Blanks to fill in</legend>
          {form.variables.length === 0 && (
            <p className="text-sm text-ink-400">
              None yet. Type a {"{{blank}}"} above and Herald will offer to
              declare it, or add one here.
            </p>
          )}
          {form.variables.map((variable, index) => (
            <div key={index} className="rounded-lg border border-line p-3">
              <div className="grid gap-3 sm:grid-cols-2">
                <div>
                  <label className="label" htmlFor={`var-name-${index}`}>
                    Name
                  </label>
                  <input
                    id={`var-name-${index}`}
                    className="input font-mono text-sm"
                    value={variable.name}
                    onChange={(e) => setVariable(index, { name: e.target.value })}
                  />
                  {nameErrors[index] && (
                    <p className="mt-1 text-xs text-rose-600">{nameErrors[index]}</p>
                  )}
                </div>
                <div>
                  <label className="label" htmlFor={`var-label-${index}`}>
                    Asked for as
                  </label>
                  <input
                    id={`var-label-${index}`}
                    className="input"
                    value={variable.label}
                    placeholder={labelFor(variable.name)}
                    onChange={(e) => setVariable(index, { label: e.target.value })}
                  />
                </div>
              </div>
              <div className="mt-3">
                <label className="label" htmlFor={`var-default-${index}`}>
                  Default
                </label>
                <input
                  id={`var-default-${index}`}
                  className="input"
                  value={variable.default}
                  placeholder="Used when you leave it blank."
                  onChange={(e) => setVariable(index, { default: e.target.value })}
                />
              </div>
              <div className="mt-3 flex items-center justify-between gap-3">
                <label className="flex items-center gap-2 text-sm text-ink-600">
                  <input
                    type="checkbox"
                    checked={variable.required}
                    onChange={(e) => setVariable(index, { required: e.target.checked })}
                  />
                  Required — refuse to write the piece without it
                </label>
                <button
                  type="button"
                  className="btn-ghost text-xs text-rose-600"
                  onClick={() => removeVariable(index)}
                >
                  Remove
                </button>
              </div>
            </div>
          ))}
          <button type="button" className="btn-ghost text-sm" onClick={() => addVariable()}>
            Add a blank
          </button>
        </fieldset>

        <div className="flex justify-end gap-2 pt-2">
          <button type="button" className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="btn-primary"
            disabled={busy || blocked}
            title={
              undeclared.length > 0
                ? "Declare the blanks above first — otherwise they render empty."
                : undefined
            }
          >
            {busy ? "Saving…" : "Save template"}
          </button>
        </div>
      </form>
    </div>
  );
}

/** The insert menu. Built from the API's list, so it cannot offer a dead name. */
function BuiltinMenu({ builtins, onInsert }) {
  const [open, setOpen] = useState(false);
  const groups = useMemo(() => groupBuiltins(builtins), [builtins]);

  if (groups.length === 0) return null;

  return (
    <div className="rounded-lg border border-line">
      <button
        type="button"
        className="flex w-full items-center justify-between px-3 py-2 text-left text-sm text-ink-600"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span>Blanks Herald fills in for you</span>
        <span className="text-ink-400">{open ? "Hide" : "Show"}</span>
      </button>
      {open && (
        <div className="space-y-3 border-t border-line px-3 py-3">
          {groups.map((group) => (
            <div key={group.namespace}>
              <p className="eyebrow">{group.namespace}</p>
              <div className="mt-1.5 grid gap-1.5 sm:grid-cols-2">
                {group.items.map((builtin) => (
                  <button
                    key={builtin.name}
                    type="button"
                    className="rounded border border-line px-2 py-1.5 text-left text-xs hover:border-ink-300"
                    onClick={() => onInsert(builtin.name)}
                  >
                    <code className="text-ink-900">{`{{${builtin.name}}}`}</code>
                    <span className="mt-0.5 block text-ink-500">{builtin.description}</span>
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Using one                                                                    //
// --------------------------------------------------------------------------- //

/**
 * Fill the blanks in, see exactly what comes out, then write it.
 *
 * The preview is the server's own renderer rather than a second implementation
 * in the browser — for a `literal` template what it shows is byte-for-byte the
 * draft that the Create button produces, and anything less than that would make
 * the preview a guess.
 */
function UseDialog({ template, projects, onClose, onDone }) {
  const toast = useToast();
  const navigate = useNavigate();
  const [values, setValues] = useState(() => initialValues(template.variables));
  const [projectId, setProjectId] = useState(
    String(template.default_project_id ?? projects[0]?.id ?? ""),
  );
  const [preview, setPreview] = useState(null);
  const [previewError, setPreviewError] = useState(null);
  const [busy, setBusy] = useState(false);

  const missing = stillMissing(template.variables, values);

  // Debounced: the preview is a round trip, and one per keystroke would both
  // hammer the API and flicker the pane on every letter of a headline.
  useEffect(() => {
    const timer = setTimeout(() => {
      api
        .previewTemplate(template.id, {
          values,
          project_id: projectId ? Number(projectId) : null,
        })
        .then((result) => {
          setPreview(result);
          setPreviewError(null);
        })
        .catch((err) => setPreviewError(err.message));
    }, 250);
    return () => clearTimeout(timer);
  }, [template.id, values, projectId]);

  async function create() {
    setBusy(true);
    try {
      const content = await api.useTemplate(template.id, {
        values,
        project_id: projectId ? Number(projectId) : null,
      });
      toast.success("Draft created");
      onDone();
      navigate(`/content/${content.id}`);
    } catch (err) {
      toast.error(err.message);
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-ink-900/25 p-4 backdrop-blur-sm sm:p-8"
      role="dialog"
      aria-modal="true"
      aria-label={`Use ${template.name}`}
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="panel my-auto w-full max-w-3xl space-y-4 p-6 shadow-pop">
        <div>
          <h2 className="font-display text-2xl text-ink-900">{template.name}</h2>
          <p className="mt-1 text-sm text-ink-500">
            {template.mode === "prompt"
              ? "Fill these in and the model gets them as its brief."
              : "Fill these in and this is the piece, exactly as shown."}
          </p>
        </div>

        <div>
          <label className="label" htmlFor="use-project">
            For project
          </label>
          <select
            id="use-project"
            className="input"
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
          >
            <option value="">Choose a project</option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        </div>

        {template.variables.map((variable) => (
          <div key={variable.name}>
            <label className="label" htmlFor={`use-${variable.name}`}>
              {variable.label || labelFor(variable.name)}
              {variable.required && <span className="ml-1 text-rose-600">*</span>}
            </label>
            <textarea
              id={`use-${variable.name}`}
              className="input min-h-[3rem]"
              rows={2}
              value={values[variable.name] ?? ""}
              placeholder={variable.description || variable.default}
              onChange={(e) =>
                setValues((current) => ({ ...current, [variable.name]: e.target.value }))
              }
            />
          </div>
        ))}

        <div>
          <p className="eyebrow mb-1.5">
            {template.mode === "prompt" ? "The brief" : "Preview"}
          </p>
          {previewError ? (
            <ErrorBanner message={previewError} />
          ) : (
            <div className="rounded-lg border border-line bg-canvas p-3">
              <p className="text-sm font-medium text-ink-900">
                {preview?.title || <span className="text-ink-400">No headline</span>}
              </p>
              <pre className="mt-2 whitespace-pre-wrap font-mono text-xs text-ink-600">
                {preview?.body || "…"}
              </pre>
            </div>
          )}
        </div>

        {missing.length > 0 && (
          <p className="text-sm text-amber-700">
            Still needs {missing.map((name) => labelFor(name)).join(", ")}.
          </p>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <button type="button" className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={busy || missing.length > 0 || !projectId}
            onClick={create}
          >
            {busy ? "Writing…" : "Create draft"}
          </button>
        </div>
      </div>
    </div>
  );
}
