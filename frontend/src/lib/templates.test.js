import { describe, expect, it } from "vitest";
import {
  blankVariable,
  formFromTemplate,
  groupBuiltins,
  initialValues,
  insertAt,
  labelFor,
  nameProblem,
  payloadFromForm,
  placeholdersIn,
  reservedNamespaces,
  stillMissing,
  undeclaredIn,
  unusedVariables,
} from "./templates";

const BUILTINS = [
  { name: "project.name", description: "The project's name." },
  { name: "project.url", description: "The project's live URL." },
  { name: "date.today", description: "Today's date." },
  { name: "signal.headline", description: "What the trigger saw." },
];

describe("placeholdersIn", () => {
  it("finds each placeholder once, in first-seen order", () => {
    expect(placeholdersIn("{{b}} {{a}}", "{{a}} {{c}}")).toEqual(["b", "a", "c"]);
  });

  it("allows whitespace inside the braces, like the server", () => {
    expect(placeholdersIn("{{ spaced }}")).toEqual(["spaced"]);
  });

  it("reads dotted built-in names", () => {
    expect(placeholdersIn("{{project.name}}")).toEqual(["project.name"]);
  });

  it("ignores a single brace and an empty pair", () => {
    expect(placeholdersIn("{not} {{}} {{ }}")).toEqual([]);
  });

  it("ignores a name that could not be an identifier", () => {
    expect(placeholdersIn("{{9lives}} {{two words}}")).toEqual([]);
  });

  it("copes with null and undefined text", () => {
    expect(placeholdersIn(null, undefined, "{{a}}")).toEqual(["a"]);
  });
});

describe("undeclaredIn", () => {
  it("flags a placeholder that is neither declared nor built in", () => {
    expect(undeclaredIn(["version"], BUILTINS, "{{version}} {{versoin}}")).toEqual([
      "versoin",
    ]);
  });

  it("accepts built-ins without a declaration", () => {
    expect(undeclaredIn([], BUILTINS, "{{project.name}} {{date.today}}")).toEqual([]);
  });

  it("checks the headline as well as the body", () => {
    expect(undeclaredIn([], BUILTINS, "{{oops}}", "")).toEqual(["oops"]);
  });
});

describe("unusedVariables", () => {
  it("names a declared variable the template never references", () => {
    expect(unusedVariables(["used", "spare"], "{{used}}")).toEqual(["spare"]);
  });

  it("counts a use in the headline", () => {
    expect(unusedVariables(["headline_only"], "{{headline_only}}", "")).toEqual([]);
  });
});

describe("nameProblem", () => {
  it("accepts an identifier", () => {
    expect(nameProblem("release_version", BUILTINS)).toBe("");
  });

  it("refuses a blank name", () => {
    expect(nameProblem("  ", BUILTINS)).toMatch(/name/i);
  });

  it("refuses an absent name the same way as a blank one", () => {
    // A row added by the editor has `name: ""`, but a template imported with a
    // malformed variable has no `name` key at all, and `undefined.trim()` would
    // take the whole editor down rather than flag the row.
    expect(nameProblem(undefined, BUILTINS)).toMatch(/name/i);
    expect(nameProblem(null, BUILTINS)).toMatch(/name/i);
  });

  it("refuses spaces and leading digits", () => {
    expect(nameProblem("release version", BUILTINS)).toMatch(/letters/i);
    expect(nameProblem("9lives", BUILTINS)).toMatch(/letters/i);
  });

  it("refuses a name that shadows a built-in namespace", () => {
    expect(nameProblem("project", BUILTINS)).toMatch(/built-in/i);
    expect(nameProblem("signal", BUILTINS)).toMatch(/built-in/i);
  });

  it("refuses a duplicate", () => {
    expect(nameProblem("version", BUILTINS, ["version"])).toMatch(/already/i);
  });
});

describe("reservedNamespaces", () => {
  it("is derived from the built-in list, not hardcoded", () => {
    expect(reservedNamespaces(BUILTINS)).toEqual(["project", "date", "signal"]);
  });
});

describe("labelFor", () => {
  it("turns a variable name into something a form can ask for", () => {
    expect(labelFor("release_version")).toBe("Release version");
    expect(labelFor("summary")).toBe("Summary");
  });

  it("labels a nameless variable as nothing rather than throwing", () => {
    expect(labelFor(undefined)).toBe("");
    expect(labelFor(null)).toBe("");
  });
});

describe("groupBuiltins", () => {
  it("groups by namespace, keeping first-seen order", () => {
    const groups = groupBuiltins(BUILTINS);

    expect(groups.map((g) => g.namespace)).toEqual(["project", "date", "signal"]);
    expect(groups[0].items).toHaveLength(2);
  });
});

describe("form conversions", () => {
  it("round-trips a template through the editor's state", () => {
    const template = {
      name: "Weekly",
      description: "d",
      mode: "prompt",
      content_type: "tutorial",
      title_template: "{{a}}",
      body_template: "{{a}}",
      variables: [{ name: "a", label: "A", description: "", default: "", required: true }],
      default_project_id: 7,
    };

    const payload = payloadFromForm(formFromTemplate(template));

    expect(payload).toMatchObject({
      name: "Weekly",
      mode: "prompt",
      content_type: "tutorial",
      default_project_id: 7,
    });
    expect(payload.variables).toEqual(template.variables);
  });

  it("blanks out to a usable new template", () => {
    const form = formFromTemplate(null);

    expect(form.mode).toBe("literal");
    expect(form.variables).toEqual([]);
    expect(payloadFromForm(form).default_project_id).toBeNull();
  });

  it("sends null rather than a project id of zero", () => {
    const form = { ...formFromTemplate(null), default_project_id: "" };

    expect(payloadFromForm(form).default_project_id).toBeNull();
  });

  // `payloadFromForm` sends every row, which is only safe because the editor
  // will not let a nameless one exist at save time. The two halves are in
  // different files, so assert the half this one leans on.
  it("leaves a half-added variable to the editor, which refuses to save it", () => {
    expect(nameProblem("  ", [])).toBeTruthy();
  });

  it("sends every named variable rather than silently dropping any", () => {
    const form = {
      ...formFromTemplate(null),
      name: "T",
      variables: [blankVariable("first"), blankVariable("second")],
    };

    expect(payloadFromForm(form).variables.map((v) => v.name)).toEqual([
      "first",
      "second",
    ]);
  });

  it("trims a variable's name and label the way it trims the template's", () => {
    const form = {
      ...formFromTemplate(null),
      name: "T",
      variables: [{ ...blankVariable("  spaced  "), label: "  Spaced  " }],
    };

    expect(payloadFromForm(form).variables[0]).toMatchObject({
      name: "spaced",
      label: "Spaced",
    });
  });

  it("trims the name so a stray space is not saved as part of it", () => {
    const form = { ...formFromTemplate(null), name: "  T  ", variables: [] };

    expect(payloadFromForm(form).name).toBe("T");
  });
});

describe("initialValues and stillMissing", () => {
  const variables = [
    { name: "summary", required: true, default: "" },
    { name: "link", required: false, default: "https://example.com" },
  ];

  it("seeds each blank with its default", () => {
    expect(initialValues(variables)).toEqual({
      summary: "",
      link: "https://example.com",
    });
  });

  it("waits only on required blanks that are still empty", () => {
    expect(stillMissing(variables, initialValues(variables))).toEqual(["summary"]);
    expect(stillMissing(variables, { summary: "done" })).toEqual([]);
  });

  it("treats whitespace as empty, like the server does", () => {
    expect(stillMissing(variables, { summary: "   " })).toEqual(["summary"]);
  });

  it("counts a required variable missing from the values entirely", () => {
    // Not the same as an empty string: this is the shape when a template gains
    // a variable while a half-filled use dialog is already open.
    expect(stillMissing(variables, {})).toEqual(["summary"]);
  });

  it("seeds a variable that declares no default with an empty string", () => {
    expect(initialValues([{ name: "summary", required: true }])).toEqual({
      summary: "",
    });
  });
});

describe("insertAt", () => {
  it("inserts at the caret and reports where the caret lands", () => {
    expect(insertAt("ab", 1, "X")).toEqual({ value: "aXb", caret: 2 });
  });

  it("appends when there is no caret", () => {
    expect(insertAt("ab", null, "X")).toEqual({ value: "abX", caret: 3 });
  });
});
