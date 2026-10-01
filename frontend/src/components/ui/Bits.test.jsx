/**
 * The shared primitives every page is assembled from.
 *
 * They are exercised indirectly by a dozen page tests, which is not the same
 * as being tested: a page test asserts on the sentence a panel renders, and
 * would go on passing if `Empty` dropped its call to action or `Confidence`
 * stopped colouring a number that has fallen under the auto-publish bar. The
 * decisions each of these makes about *what to show* are made once, here, for
 * the whole app — so they are asserted once, here, too.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  Confidence,
  Empty,
  ErrorBanner,
  SectionHeader,
  Skeleton,
  Stat,
  StatusBadge,
  Tag,
} from "./Bits";

describe("StatusBadge", () => {
  it("shows the status as its own label by default", () => {
    render(<StatusBadge status="published" />);
    expect(screen.getByText("published")).toBeInTheDocument();
  });

  it("lets a caller override the text without losing the tone", () => {
    // The tone is keyed off `status`, so a badge reading "3 failed" still has
    // to be the red one.
    const { container } = render(
      <StatusBadge status="failed">3 failed</StatusBadge>,
    );
    expect(screen.getByText("3 failed")).toBeInTheDocument();
    expect(container.firstChild.className).toContain("text-bad");
  });

  it("falls back to a neutral tone for a status it has never seen", () => {
    const { container } = render(<StatusBadge status="quantum" />);
    expect(container.firstChild.className).toContain("text-ink-500");
  });
});

describe("SectionHeader", () => {
  it("renders the title as a heading", () => {
    render(<SectionHeader title="Where it lands" />);
    expect(
      screen.getByRole("heading", { name: "Where it lands" }),
    ).toBeInTheDocument();
  });

  it("omits the subtitle line entirely when there is none", () => {
    const { container } = render(<SectionHeader title="Plain" />);
    expect(container.querySelectorAll("p")).toHaveLength(0);
  });

  it("places an action alongside the title", () => {
    render(
      <SectionHeader title="Best performing" action={<button>All content</button>} />,
    );
    expect(
      screen.getByRole("button", { name: "All content" }),
    ).toBeInTheDocument();
  });
});

describe("Empty", () => {
  it("says what to do next, not only that there is nothing", () => {
    // The whole point of this component: most of Pulse's lists start empty,
    // and an empty list with no next action is a dead end.
    render(
      <Empty
        title="No projects yet"
        hint="Add one to start drafting."
        action={<button>New project</button>}
      />,
    );

    expect(screen.getByText("No projects yet")).toBeInTheDocument();
    expect(screen.getByText("Add one to start drafting.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New project" })).toBeInTheDocument();
  });

  it("renders with a title alone", () => {
    render(<Empty title="Nothing here" />);
    expect(screen.getByText("Nothing here")).toBeInTheDocument();
  });
});

describe("Stat", () => {
  it("shows label, figure and hint", () => {
    render(<Stat label="Views" value="4,200" hint="across 14 publications" />);

    expect(screen.getByText("Views")).toBeInTheDocument();
    expect(screen.getByText("4,200")).toBeInTheDocument();
    expect(screen.getByText("across 14 publications")).toBeInTheDocument();
  });

  it("drops the hint line when there is no hint", () => {
    const { container } = render(<Stat label="Views" value="0" />);
    expect(container.querySelectorAll("p")).toHaveLength(2);
  });
});

describe("Skeleton", () => {
  it("draws the number of rows it was asked for", () => {
    const { container } = render(<Skeleton rows={5} />);
    expect(container.querySelectorAll(".animate-shimmer")).toHaveLength(5);
  });

  it("is hidden from assistive technology", () => {
    // It stands in for content that is not there yet. Announcing five empty
    // boxes is worse than announcing nothing; a caller that needs the wait
    // announced says so itself, as PreviewPage does.
    const { container } = render(<Skeleton rows={2} />);
    expect(container.firstChild).toHaveAttribute("aria-hidden", "true");
  });
});

describe("ErrorBanner", () => {
  it("renders nothing at all without a message", () => {
    // Callers pass `error` straight through, and `error` is null on the happy
    // path — so the null render is the common case, not an edge one.
    const { container } = render(<ErrorBanner message={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("announces itself as an alert", () => {
    render(<ErrorBanner message="Service unavailable" />);
    expect(screen.getByRole("alert")).toHaveTextContent("Service unavailable");
  });

  it("offers a retry only when there is something to retry with", () => {
    const { rerender } = render(<ErrorBanner message="Nope" />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();

    const onRetry = vi.fn();
    rerender(<ErrorBanner message="Nope" onRetry={onRetry} />);
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("calls back when the retry is pressed", async () => {
    const onRetry = vi.fn();
    render(<ErrorBanner message="Nope" onRetry={onRetry} />);

    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

describe("Tag", () => {
  it("title-cases whatever the API called it", () => {
    render(<Tag>case_study</Tag>);
    expect(screen.getByText("Case Study")).toBeInTheDocument();
  });
});

describe("Confidence", () => {
  it("renders nothing when the model reported no confidence", () => {
    // Null and zero are different answers: one is "it did not say", the other
    // is "it said none". Only the first disappears.
    expect(render(<Confidence value={null} />).container).toBeEmptyDOMElement();
    expect(render(<Confidence value={undefined} />).container).toBeEmptyDOMElement();
  });

  it("still renders a confidence of zero", () => {
    render(<Confidence value={0} />);
    expect(screen.getByText("0% confident")).toBeInTheDocument();
  });

  it("shows a whole percentage", () => {
    render(<Confidence value={0.837} />);
    expect(screen.getByText("84% confident")).toBeInTheDocument();
  });

  it("colours by band, so a low score reads as one", () => {
    // The bands exist because a number alone does not say whether it is good.
    expect(render(<Confidence value={0.9} />).container.firstChild.className).toContain(
      "text-good",
    );
    expect(render(<Confidence value={0.6} />).container.firstChild.className).toContain(
      "text-warn",
    );
    expect(render(<Confidence value={0.2} />).container.firstChild.className).toContain(
      "text-ink-400",
    );
  });

  it("puts the boundary values in the better band", () => {
    expect(render(<Confidence value={0.8} />).container.firstChild.className).toContain(
      "text-good",
    );
    expect(render(<Confidence value={0.5} />).container.firstChild.className).toContain(
      "text-warn",
    );
  });
});
