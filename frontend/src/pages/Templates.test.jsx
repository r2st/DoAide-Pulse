import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Templates from "./Templates";
import { api } from "../lib/api";

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
    <MemoryRouter>
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
});
