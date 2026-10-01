// The client half of the template contract.
//
// The server refuses a template whose body references a placeholder that is
// neither declared nor built in (see `app/schemas/template.py`), and that is
// the right place for the decision — it is the one that cannot be bypassed.
// But a 422 on save is a poor way to learn about a typo you made four minutes
// ago, so the editor runs the same check on every keystroke and shows the
// answer next to the body. Same rules, twice, on purpose.
//
// Kept out of the page component so the parsing can be tested without mounting
// an editor, and so the two implementations of "what is a placeholder" are one
// regex each rather than one regex and a pile of JSX.

/** Mirrors `PLACEHOLDER` in `app/services/templates.py`. */
const PLACEHOLDER = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\}\}/g;

/** Mirrors `NAME_RE` in `app/schemas/template.py`. */
const VARIABLE_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** The two modes, with the words the picker uses. Mirrors `TemplateMode`. */
export const MODES = [
  {
    value: "literal",
    label: "Use it as written",
    hint: "The filled-in text is the piece. No model runs — it is instant, free, and exactly what you wrote.",
  },
  {
    value: "prompt",
    label: "Brief the model with it",
    hint: "The filled-in text becomes the instructions. The angle stays consistent; the prose is written fresh.",
  },
];

/** Every distinct placeholder across `texts`, in first-seen order. */
export function placeholdersIn(...texts) {
  const seen = [];
  for (const text of texts) {
    for (const match of String(text ?? "").matchAll(PLACEHOLDER)) {
      if (!seen.includes(match[1])) seen.push(match[1]);
    }
  }
  return seen;
}

/**
 * Placeholders the server would reject: neither declared nor built in.
 *
 * `builtins` is the list from `GET /templates/builtins` — passed in rather than
 * hardcoded so the editor cannot offer a placeholder the resolver has dropped.
 */
export function undeclaredIn(declared, builtins, ...texts) {
  const known = new Set([...declared, ...builtins.map((b) => b.name)]);
  return placeholdersIn(...texts).filter((name) => !known.has(name));
}

/**
 * Declared variables the template never uses.
 *
 * Not an error — the server saves it happily — but it is always either a
 * leftover from an edit or a placeholder that was meant to be typed and wasn't,
 * and both are worth a word before the form asks someone to fill it in.
 */
export function unusedVariables(declared, ...texts) {
  const used = new Set(placeholdersIn(...texts));
  return declared.filter((name) => !used.has(name));
}

/** Namespaces a declared variable may not use. Derived from the built-in list. */
export function reservedNamespaces(builtins) {
  return [...new Set(builtins.map((b) => b.name.split(".")[0]))];
}

/** Why this name is unusable, or "" if it is fine. Mirrors the server's rules. */
export function nameProblem(name, builtins, others = []) {
  const trimmed = String(name ?? "").trim();
  if (!trimmed) return "Needs a name.";
  if (!VARIABLE_NAME.test(trimmed)) {
    return "Letters, digits and underscores only, starting with a letter.";
  }
  if (reservedNamespaces(builtins).includes(trimmed)) {
    return `${trimmed} is a built-in the robot fills in for you.`;
  }
  if (others.includes(trimmed)) return "Already used by another variable.";
  return "";
}

/** A readable label for a variable name, matching what the server would pick. */
export function labelFor(name) {
  const words = String(name ?? "").replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** A fresh variable row for the editor. */
export function blankVariable(name = "") {
  return { name, label: "", description: "", default: "", required: false };
}

/** Group built-ins by namespace, for the insert menu. */
export function groupBuiltins(builtins) {
  const groups = new Map();
  for (const builtin of builtins) {
    const [namespace] = builtin.name.split(".");
    if (!groups.has(namespace)) groups.set(namespace, []);
    groups.get(namespace).push(builtin);
  }
  return [...groups].map(([namespace, items]) => ({ namespace, items }));
}

/** The editor's state for an existing template, or a blank one. */
export function formFromTemplate(template) {
  return {
    name: template?.name ?? "",
    description: template?.description ?? "",
    mode: template?.mode ?? "literal",
    content_type: template?.content_type ?? "announcement",
    title_template: template?.title_template ?? "",
    body_template: template?.body_template ?? "",
    variables: (template?.variables ?? []).map((v) => ({ ...v })),
    default_project_id: template?.default_project_id
      ? String(template.default_project_id)
      : "",
  };
}

/**
 * The API payload for an editor's state.
 *
 * The one conversion that matters: `default_project_id` is a select, so it is
 * a string or "", and the API wants a number or null.
 *
 * Every variable in `form.variables` is sent. A row with no name never reaches
 * here: `nameProblem` calls a blank name a problem, and the editor refuses to
 * save while any variable has one. Dropping it here instead would be the worse
 * half of the choice — the save would succeed and the row the user just added
 * would be gone without a word. `templates.test.js` pins that coupling.
 */
export function payloadFromForm(form) {
  return {
    name: form.name.trim(),
    description: form.description.trim(),
    mode: form.mode,
    content_type: form.content_type,
    title_template: form.title_template,
    body_template: form.body_template,
    variables: form.variables.map((v) => ({
      name: v.name.trim(),
      label: v.label.trim(),
      description: v.description.trim(),
      default: v.default,
      required: Boolean(v.required),
    })),
    default_project_id: form.default_project_id
      ? Number(form.default_project_id)
      : null,
  };
}

/**
 * Starting values for the "use this template" form.
 *
 * Seeded with each variable's default so the common case — a template whose
 * defaults are already right — is one click rather than a re-typing exercise.
 */
export function initialValues(variables) {
  return Object.fromEntries(variables.map((v) => [v.name, v.default ?? ""]));
}

/** Required variables with nothing in them yet. What the Use button waits on. */
export function stillMissing(variables, values) {
  return variables
    .filter((v) => v.required && !String(values[v.name] ?? "").trim())
    .map((v) => v.name);
}

/** Insert `text` into `value` at `caret`, returning the new value and caret. */
export function insertAt(value, caret, text) {
  const at = caret ?? value.length;
  return { value: value.slice(0, at) + text + value.slice(at), caret: at + text.length };
}
