import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Calendar from "./Calendar";
import { api } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: { calendar: vi.fn(), reschedule: vi.fn() },
}));

const toast = { success: vi.fn(), error: vi.fn() };
vi.mock("../components/ui/Toast", () => ({ useToast: () => toast }));

/** An instant on a given day of August 2026, in the runner's own zone. */
function onAugust(day, hour = 13) {
  return new Date(2026, 7, day, hour, 0, 0).toISOString();
}

let nextId = 1;
function entry(overrides = {}) {
  const id = nextId++;
  return {
    content_id: id,
    publication_id: id,
    title: `Post ${id}`,
    project_id: 1,
    project_name: "Herald",
    content_type: "announcement",
    platform: "devto",
    status: "scheduled",
    when: onAugust(4),
    movable: true,
    ...overrides,
  };
}

function respond(entries) {
  api.calendar.mockResolvedValue({ entries, cadence: [], suggested_slots: [] });
}

function draw() {
  return render(
    <MemoryRouter>
      <Calendar />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  nextId = 1;
  // Only Date is faked — setTimeout has to stay real or the initial fetch
  // never settles and nothing renders.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(2026, 7, 15, 12, 0, 0));
  respond([]);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the month summary", () => {
  it("says what the window holds without making you scan it", async () => {
    respond([
      entry({ status: "scheduled" }),
      entry({ status: "published", movable: false }),
      entry({ status: "published", movable: false }),
      entry({ status: "failed" }),
    ]);
    draw();
    expect(
      await screen.findByText(/1 going out · 2 published · 1 failed/),
    ).toBeInTheDocument();
  });

  it("leaves the failure count out when there are none", async () => {
    respond([entry({ status: "scheduled" })]);
    draw();
    expect(await screen.findByText(/1 going out · 0 published\./)).toBeInTheDocument();
  });

  it("says so plainly when the window is empty", async () => {
    draw();
    expect(
      await screen.findByText(/Nothing on the calendar in this window/),
    ).toBeInTheDocument();
  });
});

describe("filtering", () => {
  beforeEach(() => {
    // One per day: four on the same square would hit the per-day cap and this
    // would be testing the overflow rule instead of the filter.
    respond([
      entry({
        title: "Shipped last week",
        status: "published",
        movable: false,
        when: onAugust(4),
      }),
      entry({ title: "Going out Tuesday", status: "scheduled", when: onAugust(5) }),
      entry({
        title: "Toots and hoots",
        platform: "bluesky",
        status: "scheduled",
        when: onAugust(6),
      }),
      entry({
        title: "Nowhere yet",
        platform: null,
        publication_id: null,
        status: "approved",
        when: onAugust(7),
      }),
    ]);
  });

  it("narrows to one status bucket", async () => {
    draw();
    await screen.findByRole("link", { name: /Shipped last week/ });

    await userEvent.click(screen.getByRole("button", { name: /Published/ }));
    expect(screen.getByRole("link", { name: /Shipped last week/ })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Going out Tuesday/ })).not.toBeInTheDocument();
  });

  it("narrows to one platform", async () => {
    draw();
    await screen.findByRole("link", { name: /Toots and hoots/ });

    await userEvent.click(screen.getByRole("button", { name: "Bluesky" }));
    expect(screen.getByRole("link", { name: /Toots and hoots/ })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Going out Tuesday/ })).not.toBeInTheDocument();
  });

  it("finds the pieces that are not routed anywhere", async () => {
    // These go nowhere until someone picks a destination, which makes them the
    // set most worth being able to isolate.
    draw();
    await screen.findByRole("link", { name: /Nowhere yet/ });

    await userEvent.click(screen.getByRole("button", { name: "Not routed" }));
    expect(screen.getByRole("link", { name: /Nowhere yet/ })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Toots and hoots/ })).not.toBeInTheDocument();
  });

  it("clears a filter by pressing the chip again", async () => {
    draw();
    await screen.findByRole("link", { name: /Shipped last week/ });
    const chip = screen.getByRole("button", { name: /Published/ });

    await userEvent.click(chip);
    expect(chip).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(chip);
    expect(chip).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("link", { name: /Going out Tuesday/ })).toBeInTheDocument();
  });

  it("explains an empty grid that the filter caused", async () => {
    // Otherwise it looks exactly like an empty month.
    draw();
    await screen.findByRole("link", { name: /Toots and hoots/ });

    await userEvent.click(screen.getByRole("button", { name: "Bluesky" }));
    await userEvent.click(screen.getByRole("button", { name: /Published/ }));
    expect(screen.getByText(/Nothing in this month matches the filter/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Clear it" }));
    expect(screen.getByRole("link", { name: /Going out Tuesday/ })).toBeInTheDocument();
  });

  it("offers the platform row only when there is a choice to make", async () => {
    respond([
      entry({ title: "Only Dev.to", when: onAugust(4) }),
      entry({ title: "Also Dev.to", when: onAugust(5) }),
    ]);
    draw();
    await screen.findByRole("link", { name: /Only Dev.to/ });
    expect(screen.queryByRole("group", { name: "Platform" })).not.toBeInTheDocument();
  });
});

describe("a busy day", () => {
  it("caps the cell rather than stretching the whole week", async () => {
    respond(Array.from({ length: 6 }, () => entry({ when: onAugust(4) })));
    draw();
    await screen.findByRole("link", { name: /Post 1/ });

    expect(screen.getByRole("button", { name: "+3 more" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Post 6/ })).not.toBeInTheDocument();
  });

  it("shows the rest when asked, and folds them away again", async () => {
    respond(Array.from({ length: 6 }, () => entry({ when: onAugust(4) })));
    draw();
    await screen.findByRole("link", { name: /Post 1/ });

    await userEvent.click(screen.getByRole("button", { name: "+3 more" }));
    expect(screen.getByRole("link", { name: /Post 6/ })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Show less" }));
    expect(screen.queryByRole("link", { name: /Post 6/ })).not.toBeInTheDocument();
  });

  it("leaves a day that fits alone", async () => {
    respond([entry({ when: onAugust(4) }), entry({ when: onAugust(4) })]);
    draw();
    await screen.findByRole("link", { name: /Post 1/ });
    // "+0 more" would be a lie, and a control that does nothing.
    expect(screen.queryByRole("button", { name: /more$/ })).not.toBeInTheDocument();
  });

  it("counts only what the filter left behind", async () => {
    respond([
      ...Array.from({ length: 4 }, () => entry({ when: onAugust(4) })),
      ...Array.from({ length: 4 }, () =>
        entry({ when: onAugust(4), platform: "bluesky" }),
      ),
    ]);
    draw();
    await screen.findByRole("link", { name: /Post 1/ });
    expect(screen.getByRole("button", { name: "+5 more" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Bluesky" }));
    expect(screen.getByRole("button", { name: "+1 more" })).toBeInTheDocument();
  });
});

describe("what can be moved", () => {
  it("leaves the past where it is", async () => {
    respond([
      entry({ title: "Already out", status: "published", movable: false }),
      entry({ title: "Still to come", status: "scheduled" }),
    ]);
    draw();

    const published = await screen.findByRole("link", { name: /Already out/ });
    const scheduled = screen.getByRole("link", { name: /Still to come/ });
    expect(published).not.toHaveAttribute("draggable", "true");
    expect(scheduled).toHaveAttribute("draggable", "true");
  });
});
