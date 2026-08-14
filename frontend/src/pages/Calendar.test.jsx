import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Calendar from "./Calendar";
import { api } from "../lib/api";
import { formatDateTime } from "../lib/format";
import { ROUTER_FUTURE } from "../lib/routerFuture";

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

function respond(entries, aside = {}) {
  api.calendar.mockResolvedValue({
    entries,
    cadence: [],
    suggested_slots: [],
    ...aside,
  });
}

/**
 * The grid square holding a given date.
 *
 * The squares are layout — no role, no label — so the only thing unique to one
 * is the date printed in its corner. Sound for the 7th to the 26th and no
 * further: a 42-square grid pads with the tail of the previous month and the
 * head of the next, so in August 2026 the numbers 1-6 and 27-31 each appear
 * twice.
 */
function cell(dayNumber) {
  const printed = screen.getByText(String(dayNumber), { selector: "span" });
  return printed.parentElement.parentElement;
}

function draw() {
  return render(
    <MemoryRouter future={ROUTER_FUTURE}>
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

/**
 * The keyboard's route to a reschedule.
 *
 * These were written first, on the understanding that jsdom has no HTML5 drag
 * and so this was the only testable route to `move` at all. Half right: there
 * is no drag *gesture* to simulate, but the handlers are plain DOM events and
 * `fireEvent` dispatches them — see "moving by drag" below, which covers the
 * other route and pins the two to the same request.
 */
describe("moving with the keyboard", () => {
  /** A chip on the 20th: after the faked "now" of the 15th, so it can move. */
  function upcoming(overrides = {}) {
    return entry({ title: "Release notes", when: onAugust(20, 13), ...overrides });
  }

  async function focusChip(name = /Release notes/) {
    const chip = await screen.findByRole("link", { name });
    chip.focus();
    return chip;
  }

  /** What `reschedule` was told to set, as a local Date. */
  function rescheduledTo() {
    const [, body] = api.reschedule.mock.calls[0];
    return new Date(body.scheduled_for);
  }

  it("moves a day later on ArrowRight, keeping the time of day", async () => {
    respond([upcoming()]);
    api.reschedule.mockResolvedValue({});
    draw();
    await focusChip();

    await userEvent.keyboard("{ArrowRight}");

    const target = rescheduledTo();
    expect(target.getDate()).toBe(21);
    expect(target.getHours()).toBe(13);
  });

  it("moves a day earlier on ArrowLeft", async () => {
    respond([upcoming()]);
    api.reschedule.mockResolvedValue({});
    draw();
    await focusChip();

    await userEvent.keyboard("{ArrowLeft}");

    expect(rescheduledTo().getDate()).toBe(19);
  });

  it("moves a week — one grid row — on ArrowDown", async () => {
    respond([upcoming()]);
    api.reschedule.mockResolvedValue({});
    draw();
    await focusChip();

    await userEvent.keyboard("{ArrowDown}");

    expect(rescheduledTo().getDate()).toBe(27);
  });

  it("moves a week back on ArrowUp", async () => {
    // From the 25th, not the default 20th: a week back from the 20th is the
    // 13th, which is behind the faked "now" and correctly refused.
    respond([upcoming({ when: onAugust(25, 13) })]);
    api.reschedule.mockResolvedValue({});
    draw();
    await focusChip();

    await userEvent.keyboard("{ArrowUp}");

    expect(rescheduledTo().getDate()).toBe(18);
  });

  it("carries the publication id, so one platform moves and not the piece", async () => {
    const item = upcoming({ publication_id: 44 });
    respond([item]);
    api.reschedule.mockResolvedValue({});
    draw();
    await focusChip();

    await userEvent.keyboard("{ArrowRight}");

    const [contentId, body] = api.reschedule.mock.calls[0];
    expect(contentId).toBe(item.content_id);
    expect(body.publication_id).toBe(44);
  });

  it("refuses a move into the past and says why", async () => {
    // The 16th at 1pm is a day past "now"; ArrowUp lands it on the 9th.
    respond([upcoming({ when: onAugust(16, 13) })]);
    draw();
    await focusChip();

    await userEvent.keyboard("{ArrowUp}");

    expect(api.reschedule).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledWith(expect.stringMatching(/in the past/));
  });

  it("does not wire the arrow keys to a published post at all", async () => {
    // Load-bearing for `move`, which explains a refusal in one way only —
    // "that day is in the past" — on the grounds that an explained refusal can
    // only ever be a movable entry. That holds precisely because the keyboard
    // never reaches an immovable one, which is what this asserts.
    respond([upcoming({ status: "published", movable: false })]);
    draw();
    const chip = await focusChip();

    await userEvent.keyboard("{ArrowRight}");

    // No shortcut announced either: announcing one that does nothing is worse
    // than announcing none.
    expect(chip).not.toHaveAttribute("aria-keyshortcuts");
    expect(api.reschedule).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("announces the shortcut on a chip that has one", async () => {
    respond([upcoming()]);
    draw();

    expect(await focusChip()).toHaveAttribute(
      "aria-keyshortcuts",
      "ArrowLeft ArrowRight ArrowUp ArrowDown",
    );
  });

  it("hands focus back to the chip once the month has redrawn", async () => {
    // The same entry both times, moved — `upcoming()` twice would mint two
    // different content ids, and the chip focus is handed back to is identified
    // by that id.
    const before = upcoming();
    const after = { ...before, when: onAugust(21, 13) };
    api.calendar
      .mockResolvedValueOnce({ entries: [before], cadence: [], suggested_slots: [] })
      .mockResolvedValue({ entries: [after], cadence: [], suggested_slots: [] });
    api.reschedule.mockResolvedValue({});
    draw();
    await focusChip();

    await userEvent.keyboard("{ArrowRight}");

    // The same chip, re-rendered into a different cell of the grid — a different
    // parent, so React unmounts the node and focus is genuinely lost and put
    // back rather than never leaving. `waitFor` because the reload it waits on
    // is a second round trip.
    await vi.waitFor(() =>
      expect(screen.getByRole("link", { name: /Release notes/ })).toHaveFocus(),
    );
  });

  it("leaves focus alone when the move was refused", async () => {
    respond([upcoming({ when: onAugust(16, 13) })]);
    draw();
    const chip = await focusChip();

    await userEvent.keyboard("{ArrowUp}");

    expect(chip).toHaveFocus();
  });

  it("reports what the API said when the move fails", async () => {
    respond([upcoming()]);
    api.reschedule.mockRejectedValue(new Error("Slot already taken"));
    draw();
    await focusChip();

    await userEvent.keyboard("{ArrowRight}");

    expect(toast.error).toHaveBeenCalledWith("Slot already taken");
  });

  it("ignores a key that is not an arrow", async () => {
    respond([upcoming()]);
    draw();
    await focusChip();

    await userEvent.keyboard("{End}");

    expect(api.reschedule).not.toHaveBeenCalled();
  });
});

/**
 * The drag, which is the route the page was built around.
 *
 * `userEvent` has no drag — the HTML5 gesture needs a real drag data store —
 * but the handlers are ordinary DOM events, and `fireEvent` dispatches them.
 * That matters for one assertion in particular: `fireEvent` returns `false`
 * when a handler called `preventDefault`, which is the *only* observable
 * difference between a square that will take the drop and one that will not.
 * The refusal is the browser's own "no drop" cursor, drawn because Herald
 * declined to accept the dragover — there is no state, no request and no toast
 * to assert on instead.
 */
describe("moving by drag", () => {
  function upcoming(overrides = {}) {
    return entry({ title: "Release notes", when: onAugust(20, 13), ...overrides });
  }

  /** Whether the square would take the drop, per the browser's own rule. */
  function wouldAccept(dayNumber) {
    return fireEvent.dragOver(cell(dayNumber)) === false;
  }

  async function pickUp(name = /Release notes/) {
    const chip = await screen.findByRole("link", { name });
    fireEvent.dragStart(chip);
    return chip;
  }

  it("reschedules to the square it was dropped on, keeping the time of day", async () => {
    respond([upcoming()]);
    api.reschedule.mockResolvedValue({});
    draw();
    await pickUp();

    wouldAccept(24);
    fireEvent.drop(cell(24));

    await waitFor(() => expect(api.reschedule).toHaveBeenCalled());
    const [, body] = api.reschedule.mock.calls[0];
    const target = new Date(body.scheduled_for);
    expect(target.getDate()).toBe(24);
    expect(target.getHours()).toBe(13);
  });

  it("asks for exactly what the keyboard asks for", async () => {
    // The claim in this page's docstring — one `move`, two ways in, no second
    // implementation to drift. Two routes to the same day, byte for byte.
    respond([upcoming()]);
    api.reschedule.mockResolvedValue({});
    draw();
    await pickUp();
    fireEvent.drop(cell(21));
    await waitFor(() => expect(api.reschedule).toHaveBeenCalled());
    const dragged = api.reschedule.mock.calls[0];

    vi.clearAllMocks();
    respond([upcoming()]);
    api.reschedule.mockResolvedValue({});
    draw();
    (await screen.findAllByRole("link", { name: /Release notes/ }))[0].focus();
    await userEvent.keyboard("{ArrowRight}");
    await waitFor(() => expect(api.reschedule).toHaveBeenCalled());

    expect(api.reschedule.mock.calls[0]).toEqual(dragged);
  });

  it("refuses the drop on a day already past, before any request", async () => {
    // "Now" is the 15th. The refusal is the browser's, and it costs nothing:
    // no round trip that comes back as a red toast saying so.
    respond([upcoming()]);
    draw();
    await pickUp();

    expect(wouldAccept(10)).toBe(false);
    expect(wouldAccept(24)).toBe(true);
    expect(api.reschedule).not.toHaveBeenCalled();
  });

  it("still checks on drop, because a long drag outlives what made it legal", async () => {
    // The belt-and-braces guard inside `move`. Reached here by dropping on a
    // square that never accepted the dragover — which a browser would not do,
    // and which is exactly the state a drag held across midnight arrives in.
    respond([upcoming()]);
    draw();
    await pickUp();

    fireEvent.drop(cell(10));

    await waitFor(() => expect(screen.getByText(/Release notes/)).toBeInTheDocument());
    expect(api.reschedule).not.toHaveBeenCalled();
    // Silent, unlike the keyboard's refusal: the dimmed square and the no-drop
    // cursor already said it, and a toast on top would be the third telling.
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("dims the squares that will not take the entry, only while it is held", async () => {
    // Unprompted, this would dim the whole left-hand side of every month.
    respond([upcoming()]);
    draw();
    await screen.findByText(/Release notes/);
    expect(cell(10)).not.toHaveAttribute("aria-disabled");

    const chip = await pickUp();
    expect(cell(10)).toHaveAttribute("aria-disabled", "true");
    expect(cell(24)).not.toHaveAttribute("aria-disabled");

    fireEvent.dragEnd(chip);
    expect(cell(10)).not.toHaveAttribute("aria-disabled");
  });

  it("drops nothing when nothing was picked up", async () => {
    // A drop can arrive from outside the page — a file, a selection, a chip
    // from another window — and `dragging` is null for all of them.
    respond([upcoming()]);
    draw();
    await screen.findByText(/Release notes/);

    fireEvent.drop(cell(24));

    await waitFor(() => expect(screen.getByText(/Release notes/)).toBeInTheDocument());
    expect(api.reschedule).not.toHaveBeenCalled();
  });

  it("lets a square go once the pointer leaves it", async () => {
    // The highlight follows the pointer. One square lit at a time, or a slow
    // drag across a week leaves a trail of them.
    respond([upcoming()]);
    draw();
    await pickUp();

    wouldAccept(24);
    const lit = cell(24).className;
    fireEvent.dragLeave(cell(24));

    expect(cell(24).className).not.toEqual(lit);
  });
});

describe("changing month", () => {
  it("asks the API for the window it moved to", async () => {
    respond([]);
    draw();
    await screen.findByText("August 2026");

    await userEvent.click(screen.getByLabelText("Next month"));

    expect(await screen.findByText("September 2026")).toBeInTheDocument();
    // Not filtered in the browser: the month is the query.
    const { start } = api.calendar.mock.calls.at(-1)[0];
    expect(new Date(start).getMonth()).toBe(7); // the padding days of August
  });

  it("goes back the way it came", async () => {
    respond([]);
    draw();
    await screen.findByText("August 2026");

    await userEvent.click(screen.getByLabelText("Previous month"));

    expect(await screen.findByText("July 2026")).toBeInTheDocument();
  });

  it("returns to this month from wherever you wandered to", async () => {
    // Three clicks out is far enough that clicking back is the wrong way home,
    // and the month heading is the only thing saying where you are.
    respond([]);
    draw();
    await screen.findByText("August 2026");
    await userEvent.click(screen.getByLabelText("Next month"));
    await userEvent.click(screen.getByLabelText("Next month"));
    await userEvent.click(screen.getByLabelText("Next month"));
    await screen.findByText("November 2026");

    await userEvent.click(screen.getByRole("button", { name: "Today" }));

    expect(await screen.findByText("August 2026")).toBeInTheDocument();
  });
});

/**
 * The aside: what Herald suggests, and on whose authority.
 *
 * Both panels were rendered by no test at all — the harness sent an empty
 * `cadence` and `suggested_slots` on every response, so the whole right-hand
 * column of the page was the empty state, permanently.
 *
 * The provenance chip is the part that earns the tests. Herald has two sources
 * for "post at 09:00 on Tuesdays": the account's own first-day view counts, and
 * a generic published table used until there are enough posts to say anything.
 * They look identical on screen. A suggestion the reader cannot interrogate is
 * one they are right to ignore, and one presented as theirs when it is not is
 * worse than none.
 */
describe("the suggestions aside", () => {
  const cadence = (overrides = {}) => ({
    platform: "devto",
    max_per_week: 3,
    best_weekdays: ["Tue", "Thu"],
    best_time_utc: "09:00",
    rationale: "Your posts land best mid-morning.",
    source: "learned",
    weekdays_source: "learned",
    sample: 12,
    ...overrides,
  });

  it("points at Settings when there is nothing connected to suggest for", async () => {
    respond([]);
    draw();
    expect(
      await screen.findByText(/Connect a platform in Settings/),
    ).toBeInTheDocument();
    expect(screen.getByText(/No platforms connected yet/)).toBeInTheDocument();
  });

  it("lists every suggested slot as a time, not as the ISO string it arrived as", async () => {
    const slots = [onAugust(18, 9), onAugust(20, 9)];
    respond([], { suggested_slots: slots });
    draw();
    await screen.findByText("Suggested slots");

    const panel = within(screen.getByText("Suggested slots").parentElement);
    const rows = panel.getAllByRole("listitem");

    expect(rows).toHaveLength(2);
    expect(rows.map((row) => row.textContent)).toEqual(slots.map(formatDateTime));
    // The ISO string is what the API sends and what a missing formatter would
    // leave on screen — "2026-08-18T13:00:00.000Z" in the middle of a sidebar.
    expect(rows[0]).not.toHaveTextContent(slots[0]);
  });

  it("says a cadence is the account's own, and how much is behind it", async () => {
    respond([], { cadence: [cadence()] });
    draw();

    expect(await screen.findByText("Learned from 12 posts")).toBeInTheDocument();
    expect(screen.getByText(/≤3\/week · Tue, Thu · 09:00 UTC/)).toBeInTheDocument();
    expect(screen.getByText("Your posts land best mid-morning.")).toBeInTheDocument();
  });

  it("does not dress the published table up as the account's own numbers", async () => {
    respond([], {
      cadence: [cadence({ source: "table", weekdays_source: "table", sample: 0 })],
    });
    draw();

    const chip = await screen.findByText("Generic guidance");
    expect(chip).toHaveAttribute("title", expect.stringMatching(/not enough of your own/));
  });

  it("splits the claim when the hour is learned but the weekdays are not", async () => {
    // The two clear the bar separately, and the days shown alongside are the
    // table's here. "Learned from 6 posts" next to them would read as a claim
    // about both.
    respond([], {
      cadence: [cadence({ weekdays_source: "table", sample: 6 })],
    });
    draw();

    const chip = await screen.findByText("Hour learned from 6 posts");
    expect(chip).toHaveAttribute("title", expect.stringMatching(/days are still published guidance/));
  });

  it("names each platform it has a cadence for", async () => {
    respond([], {
      cadence: [cadence(), cadence({ platform: "hashnode", sample: 1 })],
    });
    draw();

    expect(await screen.findByText("Devto")).toBeInTheDocument();
    expect(screen.getByText("Hashnode")).toBeInTheDocument();
    // One post, not "1 posts".
    expect(screen.getByText("Learned from 1 post")).toBeInTheDocument();
  });
});
