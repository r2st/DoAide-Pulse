import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Templates from "./Templates";
import { api } from "../lib/api";
import { ROUTER_FUTURE } from "../lib/routerFuture";

vi.mock("../lib/api", () => ({
  api: {
    listProjects: vi.fn(),
    templateBuiltins: vi.fn(),
    listTemplates: vi.fn(),
    createTemplate: vi.fn(),
    updateTemplate: vi.fn(),
    deleteTemplate: vi.fn(),
    previewTemplate: vi.fn(),
    useTemplate: vi.fn(),
  },
}));

const toast = { success: vi.fn(), error: vi.fn() };
vi.mock("../components/ui/Toast", () => ({ useToast: () => toast }));

const navigate = vi.fn();
vi.mock("react-router-dom", async () => ({
  ...(await vi.importActual("react-router-dom")),
  useNavigate: () => navigate,
}));

const BUILTINS = [
  { name: "project.name", description: "The project's name." },
  { name: "date.long", description: "Today's date, as 1 August 2026." },
  { name: "signal.headline", description: "What the trigger saw happen." },
];

function template(overrides = {}) {
  return {
    id: 1,
    name: "Weekly changelog",
    description: "Every Friday.",
    mode: "literal",
    mode_label: "Use it as written",
    content_type: "announcement",
    title_template: "{{project.name}} — week of {{date.long}}",
    body_template: "## What shipped\n\n{{summary}}",
    variables: [
      { name: "summary", label: "Summary", description: "", default: "", required: true },
    ],
    default_project_id: 1,
    use_count: 3,
    placeholders_used: ["project.name", "date.long", "summary"],
    created_at: "2026-07-01T10:00:00Z",
    updated_at: "2026-07-30T10:00:00Z",
    ...overrides,
  };
}

function renderPage() {
  return render(
    <MemoryRouter future={ROUTER_FUTURE}>
      <Templates />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.listProjects.mockResolvedValue([{ id: 1, name: "Herald" }]);
  api.templateBuiltins.mockResolvedValue(BUILTINS);
  api.listTemplates.mockResolvedValue([template()]);
  api.previewTemplate.mockResolvedValue({
    title: "Herald — week of 30 July 2026",
    body: "## What shipped\n\nTemplates landed.",
    missing: [],
    filled: ["summary"],
    is_complete: true,
  });
});

describe("the list", () => {
  it("shows a template with its mode, its blanks and how often it has been used", async () => {
    renderPage();

    expect(await screen.findByText("Weekly changelog")).toBeInTheDocument();
    expect(screen.getByText("Use it as written")).toBeInTheDocument();
    expect(screen.getByText(/1 blank to fill/)).toBeInTheDocument();
    expect(screen.getByText(/used 3 times/)).toBeInTheDocument();
    expect(screen.getByText("{{summary}}")).toBeInTheDocument();
  });

  it("says what a template is for when there are none", async () => {
    api.listTemplates.mockResolvedValue([]);

    renderPage();

    expect(await screen.findByText("No templates yet")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /write your first template/i }),
    ).toBeInTheDocument();
  });

  it("surfaces a load failure with a way to retry", async () => {
    api.listTemplates.mockRejectedValueOnce(new Error("nope"));

    renderPage();

    expect(await screen.findByText("nope")).toBeInTheDocument();
  });
});

describe("the editor", () => {
  it("refuses to save while a placeholder is undeclared, and offers to declare it", async () => {
    const user = userEvent.setup();
    api.listTemplates.mockResolvedValue([]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: /write your first/i }));
    await user.type(screen.getByLabelText("Name"), "Release note");
    // Pasted, not typed: userEvent reads `{{` as its own escape for a literal
    // brace, so `type` would never put a placeholder in the field at all.
    await user.click(screen.getByLabelText("The piece"));
    await user.paste("Shipped {{version}}");

    const save = screen.getByRole("button", { name: /save template/i });
    expect(save).toBeDisabled();

    // The affordance that makes the live check worth more than a 422.
    await user.click(screen.getByRole("button", { name: /declare \{\{version\}\}/i }));

    expect(save).toBeEnabled();
    expect(screen.getByDisplayValue("version")).toBeInTheDocument();
  });

  it("accepts a built-in without a declaration", async () => {
    const user = userEvent.setup();
    api.listTemplates.mockResolvedValue([]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: /write your first/i }));
    await user.type(screen.getByLabelText("Name"), "Note");
    await user.click(screen.getByLabelText("The piece"));
    await user.paste("About {{project.name}}");

    expect(screen.getByLabelText("The piece")).toHaveValue("About {{project.name}}");
    expect(screen.getByRole("button", { name: /save template/i })).toBeEnabled();
    expect(screen.queryByText(/not\s+declared/i)).not.toBeInTheDocument();
  });

  it("names a variable that the template never uses", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: /add a blank/i }));
    const names = screen.getAllByLabelText("Name");
    await user.type(names[names.length - 1], "spare");

    expect(await screen.findByText(/declared but never used: spare/i)).toBeInTheDocument();
  });

  it("blocks a variable name that shadows a built-in", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: /add a blank/i }));
    const names = screen.getAllByLabelText("Name");
    await user.type(names[names.length - 1], "project");

    expect(await screen.findByText(/built-in Herald fills in/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save template/i })).toBeDisabled();
  });

  it("inserts a built-in at the caret from the menu", async () => {
    const user = userEvent.setup();
    api.listTemplates.mockResolvedValue([]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: /write your first/i }));
    await user.click(screen.getByRole("button", { name: /blanks herald fills in/i }));
    await user.click(screen.getByRole("button", { name: /\{\{date\.long\}\}/ }));

    expect(screen.getByLabelText("The piece")).toHaveValue("{{date.long}}");
  });

  it("saves an edit as a patch of the whole template", async () => {
    const user = userEvent.setup();
    api.updateTemplate.mockResolvedValue(template());
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.clear(screen.getByLabelText("Description"));
    await user.type(screen.getByLabelText("Description"), "Now on Thursdays.");
    await user.click(screen.getByRole("button", { name: /save template/i }));

    await waitFor(() => expect(api.updateTemplate).toHaveBeenCalled());
    const [id, payload] = api.updateTemplate.mock.calls[0];
    expect(id).toBe(1);
    expect(payload.description).toBe("Now on Thursdays.");
    expect(payload.variables).toHaveLength(1);
  });

  it("relabels the body when the template briefs the model instead", async () => {
    const user = userEvent.setup();
    api.listTemplates.mockResolvedValue([]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: /write your first/i }));
    await user.click(screen.getByLabelText(/brief the model with it/i));

    expect(screen.getByLabelText("Instructions for the model")).toBeInTheDocument();
  });

  it("reports a save that the server refuses", async () => {
    const user = userEvent.setup();
    api.updateTemplate.mockRejectedValue(new Error("You already have a template called 'x'."));
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: /save template/i }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("You already have a template called 'x'."),
    );
  });
});

describe("using one", () => {
  it("previews with the server's own renderer as the blanks are filled in", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Use" }));
    await user.type(screen.getByLabelText(/summary/i), "Templates landed.");

    await waitFor(() => expect(api.previewTemplate).toHaveBeenCalled());
    const [id, payload] = api.previewTemplate.mock.calls.at(-1);
    expect(id).toBe(1);
    expect(payload.values.summary).toBe("Templates landed.");
    expect(await screen.findByText("Herald — week of 30 July 2026")).toBeInTheDocument();
  });

  it("waits for a required blank before it will write anything", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Use" }));

    expect(screen.getByRole("button", { name: /create draft/i })).toBeDisabled();
    expect(screen.getByText(/still needs summary/i)).toBeInTheDocument();
  });

  it("creates the draft and opens it", async () => {
    const user = userEvent.setup();
    api.useTemplate.mockResolvedValue({ id: 42 });
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Use" }));
    await user.type(screen.getByLabelText(/summary/i), "Templates landed.");
    await user.click(screen.getByRole("button", { name: /create draft/i }));

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/content/42"));
    expect(api.useTemplate).toHaveBeenCalledWith(1, {
      values: { summary: "Templates landed." },
      project_id: 1,
    });
  });

  it("keeps the dialog open when the write fails", async () => {
    const user = userEvent.setup();
    api.useTemplate.mockRejectedValue(new Error("Still needs a value for summary."));
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Use" }));
    await user.type(screen.getByLabelText(/summary/i), "x");
    await user.click(screen.getByRole("button", { name: /create draft/i }));

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(navigate).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /create draft/i })).toBeEnabled();
  });
});

describe("deleting", () => {
  it("says the pieces it produced are safe before deleting", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    api.deleteTemplate.mockResolvedValue(null);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Delete" }));

    expect(confirm.mock.calls[0][0]).toMatch(/already produced are not affected/i);
    await waitFor(() => expect(api.deleteTemplate).toHaveBeenCalledWith(1));
    confirm.mockRestore();
  });

  it("does nothing when the confirm is declined", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Delete" }));

    expect(api.deleteTemplate).not.toHaveBeenCalled();
    confirm.mockRestore();
  });

  it("keeps the card and says why when the delete fails", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    api.deleteTemplate.mockRejectedValue(new Error("Conflict"));
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Delete" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Conflict"));
    expect(screen.getByText("Weekly changelog")).toBeInTheDocument();
    // Once on mount and not again: nothing changed, so nothing to re-read.
    expect(api.listTemplates).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Delete" })).toBeEnabled();
    confirm.mockRestore();
  });
});

/**
 * The card's summary line.
 *
 * It is one sentence assembled from four counts, and every one of them has a
 * zero case and a singular case that read wrong if the pluralisation is done by
 * appending "s": "0 blanks to fill" for a template with nothing to fill in,
 * "used 1 times", "1 blanks". The line is the only thing distinguishing two
 * templates in a list, so it is worth being right.
 */
describe("what a card says about a template", () => {
  it("counts a single blank in the singular", async () => {
    renderPage();

    expect(await screen.findByText(/1 blank to fill/)).toBeInTheDocument();
  });

  it("does not say 'zero blanks' about a template with none", async () => {
    api.listTemplates.mockResolvedValue([
      template({ variables: [], placeholders_used: [] }),
    ]);
    renderPage();

    expect(await screen.findByText(/No blanks to fill/)).toBeInTheDocument();
  });

  it("counts several blanks in the plural", async () => {
    api.listTemplates.mockResolvedValue([
      template({
        variables: [
          { name: "a", label: "", description: "", default: "", required: false },
          { name: "b", label: "", description: "", default: "", required: false },
        ],
      }),
    ]);
    renderPage();

    expect(await screen.findByText(/2 blanks to fill/)).toBeInTheDocument();
  });

  it("says a template has never been used rather than 'used 0 times'", async () => {
    api.listTemplates.mockResolvedValue([template({ use_count: 0 })]);
    renderPage();

    expect(await screen.findByText(/never used/)).toBeInTheDocument();
  });

  it("counts a single use in the singular", async () => {
    api.listTemplates.mockResolvedValue([template({ use_count: 1 })]);
    renderPage();

    expect(await screen.findByText(/used 1 time(?!s)/)).toBeInTheDocument();
  });

  it("names the project a template usually writes for", async () => {
    renderPage();

    expect(await screen.findByText(/usually Herald/)).toBeInTheDocument();
  });

  it("says nothing about a project when the template has no default", async () => {
    api.listTemplates.mockResolvedValue([template({ default_project_id: null })]);
    renderPage();

    await screen.findByText("Weekly changelog");
    expect(screen.queryByText(/usually /)).not.toBeInTheDocument();
  });

  it("omits the description line when there is no description", async () => {
    api.listTemplates.mockResolvedValue([template({ description: "" })]);
    renderPage();

    await screen.findByText("Weekly changelog");
    expect(screen.queryByText("Every Friday.")).not.toBeInTheDocument();
  });

  it("shows every placeholder the template actually references", async () => {
    renderPage();

    await screen.findByText("Weekly changelog");
    expect(screen.getByText("{{project.name}}")).toBeInTheDocument();
    expect(screen.getByText("{{date.long}}")).toBeInTheDocument();
    expect(screen.getByText("{{summary}}")).toBeInTheDocument();
  });

  it("titles the kind of piece rather than printing the enum value", async () => {
    api.listTemplates.mockResolvedValue([template({ content_type: "blog_post" })]);
    renderPage();

    expect(await screen.findByText("Blog Post")).toBeInTheDocument();
  });
});

describe("the editor, on the parts the live check does not cover", () => {
  /**
   * The template's own Name field.
   *
   * Every declared blank has a field labelled "Name" too, so an unscoped query
   * matches one plus one per variable. First in document order is the
   * template's, which is the one these tests mean.
   */
  function templateName() {
    return screen.getAllByLabelText("Name")[0];
  }

  it("opens blank for a new template and titles itself as one", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: /new template/i }));

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-label", "New template");
    expect(within(dialog).getByLabelText("Name")).toHaveValue("");
    expect(within(dialog).getByLabelText("The piece")).toHaveValue("");
  });

  it("opens an existing template with its own values in the fields", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit" }));

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-label", "Edit template");
    expect(templateName()).toHaveValue("Weekly changelog");
    expect(within(dialog).getByLabelText("Description")).toHaveValue("Every Friday.");
    expect(within(dialog).getByDisplayValue("summary")).toBeInTheDocument();
  });

  it("will not save a template with no name", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.clear(templateName());

    expect(screen.getByRole("button", { name: /save template/i })).toBeDisabled();
  });

  it("treats a name of nothing but spaces as no name", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.clear(templateName());
    await user.type(templateName(), "   ");

    expect(screen.getByRole("button", { name: /save template/i })).toBeDisabled();
  });

  it("creates rather than patches when the template is new", async () => {
    const user = userEvent.setup();
    api.createTemplate.mockResolvedValue(template({ id: 7 }));
    renderPage();

    await user.click(await screen.findByRole("button", { name: /new template/i }));
    await user.type(screen.getByLabelText("Name"), "Release note");
    await user.click(screen.getByRole("button", { name: /save template/i }));

    await waitFor(() => expect(api.createTemplate).toHaveBeenCalled());
    expect(api.updateTemplate).not.toHaveBeenCalled();
    expect(api.createTemplate.mock.calls[0][0].name).toBe("Release note");
  });

  it("closes, re-reads the list and says so on a successful save", async () => {
    const user = userEvent.setup();
    api.updateTemplate.mockResolvedValue(template());
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: /save template/i }));

    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Template saved"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.listTemplates).toHaveBeenCalledTimes(2);
  });

  it("leaves the editor open on a rejected save, with the typing intact", async () => {
    const user = userEvent.setup();
    api.updateTemplate.mockRejectedValue(new Error("Taken"));
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.clear(templateName());
    await user.type(templateName(), "Renamed");
    await user.click(screen.getByRole("button", { name: /save template/i }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Taken"));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(templateName()).toHaveValue("Renamed");
    expect(api.listTemplates).toHaveBeenCalledTimes(1);
  });

  it("throws the edit away on Cancel", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.clear(templateName());
    await user.type(templateName(), "Renamed");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.updateTemplate).not.toHaveBeenCalled();
    expect(screen.getByText("Weekly changelog")).toBeInTheDocument();
  });

  it("explains on the button itself why saving is blocked", async () => {
    const user = userEvent.setup();
    api.listTemplates.mockResolvedValue([]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: /write your first/i }));
    await user.type(screen.getByLabelText("Name"), "Note");
    await user.click(screen.getByLabelText("The piece"));
    await user.paste("Shipped {{version}}");

    expect(screen.getByRole("button", { name: /save template/i })).toHaveAttribute(
      "title",
      "Declare the blanks above first — otherwise they render empty.",
    );
  });

  it("names every undeclared blank, in the plural", async () => {
    const user = userEvent.setup();
    api.listTemplates.mockResolvedValue([]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: /write your first/i }));
    await user.click(screen.getByLabelText("The piece"));
    await user.paste("Shipped {{version}} for {{customer}}");

    expect(screen.getByText(/These blanks are not/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /declare \{\{version\}\}/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /declare \{\{customer\}\}/i }),
    ).toBeInTheDocument();
  });

  it("counts a placeholder in the headline too, not only in the body", async () => {
    const user = userEvent.setup();
    api.listTemplates.mockResolvedValue([]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: /write your first/i }));
    await user.click(screen.getByLabelText("Headline"));
    await user.paste("Shipped {{version}}");

    expect(
      screen.getByRole("button", { name: /declare \{\{version\}\}/i }),
    ).toBeInTheDocument();
  });

  it("rejects two variables sharing one name", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: /add a blank/i }));
    const names = screen.getAllByLabelText("Name");
    await user.type(names[names.length - 1], "summary");

    // Flagged on both rows: neither is more the duplicate than the other, and
    // marking only the second would say the first was fine to keep.
    expect(
      await screen.findAllByText("Already used by another variable."),
    ).toHaveLength(2);
    expect(screen.getByRole("button", { name: /save template/i })).toBeDisabled();
  });

  it("rejects a name that is not a usable identifier", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: /add a blank/i }));
    const names = screen.getAllByLabelText("Name");
    await user.type(names[names.length - 1], "2things");

    expect(
      await screen.findByText(/Letters, digits and underscores only/),
    ).toBeInTheDocument();
  });

  it("removes a blank, and stops complaining about it", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: /add a blank/i }));
    const names = screen.getAllByLabelText("Name");
    await user.type(names[names.length - 1], "spare");
    await screen.findByText(/never used: spare/i);

    await user.click(screen.getAllByRole("button", { name: "Remove" }).at(-1));

    expect(screen.queryByText(/never used: spare/i)).not.toBeInTheDocument();
    expect(screen.getAllByLabelText("Name")).toHaveLength(2); // template name + one variable
  });

  it("will not save while a blank has been added but not named", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: /add a blank/i }));

    // The alternative is dropping the row in `payloadFromForm`, which saves
    // successfully and quietly discards what the user just added. Blocking says
    // so instead, and is why that function sends every row it is given.
    expect(await screen.findByText("Needs a name.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save template/i })).toBeDisabled();

    await user.click(screen.getAllByRole("button", { name: "Remove" }).at(-1));

    expect(screen.getByRole("button", { name: /save template/i })).toBeEnabled();
  });

  it("sends null rather than a string when no default project is chosen", async () => {
    const user = userEvent.setup();
    api.updateTemplate.mockResolvedValue(template());
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.selectOptions(screen.getByLabelText("Usually for"), "");
    await user.click(screen.getByRole("button", { name: /save template/i }));

    await waitFor(() => expect(api.updateTemplate).toHaveBeenCalled());
    expect(api.updateTemplate.mock.calls[0][1].default_project_id).toBeNull();
  });

  it("sends the project as a number when one is chosen", async () => {
    const user = userEvent.setup();
    api.updateTemplate.mockResolvedValue(template());
    api.listProjects.mockResolvedValue([
      { id: 1, name: "Herald" },
      { id: 2, name: "Beacon" },
    ]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.selectOptions(screen.getByLabelText("Usually for"), "2");
    await user.click(screen.getByRole("button", { name: /save template/i }));

    await waitFor(() => expect(api.updateTemplate).toHaveBeenCalled());
    expect(api.updateTemplate.mock.calls[0][1].default_project_id).toBe(2);
  });
});

describe("the built-in insert menu", () => {
  it("stays shut until asked, and says which way it will go", async () => {
    const user = userEvent.setup();
    api.listTemplates.mockResolvedValue([]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: /write your first/i }));
    const toggle = screen.getByRole("button", { name: /blanks herald fills in/i });

    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(within(toggle).getByText("Show")).toBeInTheDocument();

    await user.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(within(toggle).getByText("Hide")).toBeInTheDocument();
  });

  it("groups the built-ins by namespace", async () => {
    const user = userEvent.setup();
    api.listTemplates.mockResolvedValue([]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: /write your first/i }));
    await user.click(screen.getByRole("button", { name: /blanks herald fills in/i }));

    expect(screen.getByText("project")).toBeInTheDocument();
    expect(screen.getByText("date")).toBeInTheDocument();
    expect(screen.getByText("signal")).toBeInTheDocument();
    expect(screen.getByText("The project's name.")).toBeInTheDocument();
  });

  it("is absent entirely when the server offers no built-ins", async () => {
    const user = userEvent.setup();
    api.templateBuiltins.mockResolvedValue([]);
    api.listTemplates.mockResolvedValue([]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: /write your first/i }));

    expect(
      screen.queryByRole("button", { name: /blanks herald fills in/i }),
    ).not.toBeInTheDocument();
  });

  it("inserts at the caret rather than appending to the end", async () => {
    const user = userEvent.setup();
    api.listTemplates.mockResolvedValue([]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: /write your first/i }));
    const body = screen.getByLabelText("The piece");
    await user.click(body);
    await user.paste("Shipped  today");
    // Between the two spaces, which is where a writer stops to reach for the menu.
    body.setSelectionRange(8, 8);

    await user.click(screen.getByRole("button", { name: /blanks herald fills in/i }));
    await user.click(screen.getByRole("button", { name: /\{\{project\.name\}\}/ }));

    expect(body).toHaveValue("Shipped {{project.name}} today");
  });
});

describe("the use dialog, beyond the happy path", () => {
  it("starts on the project the template usually writes for", async () => {
    const user = userEvent.setup();
    api.listProjects.mockResolvedValue([
      { id: 2, name: "Beacon" },
      { id: 1, name: "Herald" },
    ]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Use" }));

    expect(screen.getByLabelText("For project")).toHaveValue("1");
  });

  it("falls back to the first project when the template has no usual one", async () => {
    const user = userEvent.setup();
    api.listTemplates.mockResolvedValue([template({ default_project_id: null })]);
    api.listProjects.mockResolvedValue([
      { id: 2, name: "Beacon" },
      { id: 1, name: "Herald" },
    ]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Use" }));

    expect(screen.getByLabelText("For project")).toHaveValue("2");
  });

  it("will not write a draft with no project at all", async () => {
    const user = userEvent.setup();
    api.listTemplates.mockResolvedValue([
      template({ default_project_id: null, variables: [] }),
    ]);
    api.listProjects.mockResolvedValue([]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Use" }));

    expect(screen.getByLabelText("For project")).toHaveValue("");
    expect(screen.getByRole("button", { name: /create draft/i })).toBeDisabled();
  });

  it("calls the filled-in text a brief when the template briefs the model", async () => {
    const user = userEvent.setup();
    api.listTemplates.mockResolvedValue([template({ mode: "prompt" })]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Use" }));

    expect(screen.getByText(/the model gets them as its brief/i)).toBeInTheDocument();
    expect(screen.getByText("The brief")).toBeInTheDocument();
    expect(screen.queryByText("Preview")).not.toBeInTheDocument();
  });

  it("promises the preview is the piece when it is used as written", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Use" }));

    expect(screen.getByText(/exactly as shown/i)).toBeInTheDocument();
    expect(screen.getByText("Preview")).toBeInTheDocument();
  });

  it("marks a required blank as required", async () => {
    const user = userEvent.setup();
    api.listTemplates.mockResolvedValue([
      template({
        variables: [
          { name: "summary", label: "Summary", description: "", default: "", required: true },
          { name: "extra", label: "Extra", description: "", default: "", required: false },
        ],
      }),
    ]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Use" }));

    expect(screen.getByLabelText("Summary*")).toBeInTheDocument();
    expect(screen.getByLabelText("Extra")).toBeInTheDocument();
  });

  it("seeds a blank with its default, so a template of defaults is one click", async () => {
    const user = userEvent.setup();
    api.useTemplate.mockResolvedValue({ id: 5 });
    api.listTemplates.mockResolvedValue([
      template({
        variables: [
          {
            name: "summary",
            label: "Summary",
            description: "",
            default: "Nothing much.",
            required: true,
          },
        ],
      }),
    ]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Use" }));

    expect(screen.getByLabelText(/Summary/)).toHaveValue("Nothing much.");
    expect(screen.getByRole("button", { name: /create draft/i })).toBeEnabled();
  });

  it("labels a blank from its name when the template gave it no label", async () => {
    const user = userEvent.setup();
    api.listTemplates.mockResolvedValue([
      template({
        variables: [
          { name: "release_notes", label: "", description: "", default: "", required: false },
        ],
      }),
    ]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Use" }));

    expect(screen.getByLabelText("Release notes")).toBeInTheDocument();
  });

  it("names every blank it is still waiting on", async () => {
    const user = userEvent.setup();
    api.listTemplates.mockResolvedValue([
      template({
        variables: [
          { name: "summary", label: "", description: "", default: "", required: true },
          { name: "release_notes", label: "", description: "", default: "", required: true },
        ],
      }),
    ]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Use" }));

    expect(
      screen.getByText(/Still needs Summary, Release notes/),
    ).toBeInTheDocument();
  });

  it("shows the server's complaint instead of a stale preview", async () => {
    const user = userEvent.setup();
    api.previewTemplate.mockRejectedValue(new Error("Template body is malformed."));
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Use" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Template body is malformed.",
    );
  });

  it("does not preview once per keystroke", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Use" }));
    api.previewTemplate.mockClear();

    const typed = "Templates landed.";
    await user.type(screen.getByLabelText(/summary/i), typed);
    await waitFor(() =>
      expect(api.previewTemplate.mock.calls.at(-1)?.[1].values.summary).toBe(typed),
    );

    // Seventeen keystrokes, each restarting the 250ms timer. Without the
    // debounce this is seventeen round trips and seventeen repaints of the
    // preview pane; with it, the render that matters is the last one.
    expect(api.previewTemplate.mock.calls.length).toBeLessThan(typed.length);
  });

  it("closes without writing anything on Cancel", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Use" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.useTemplate).not.toHaveBeenCalled();
  });
});
