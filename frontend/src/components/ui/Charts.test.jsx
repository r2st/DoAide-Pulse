import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AreaChart, BarRow, CompareBars, MultiLineChart } from "./Charts";

/** The width a bar was actually drawn at, as a number of percent. */
function barWidth(element) {
  return parseFloat(element.style.width);
}

describe("AreaChart", () => {
  const points = [
    { label: "2026-07-01", value: 0 },
    { label: "2026-07-02", value: 40 },
    { label: "2026-07-03", value: 20 },
  ];

  it("draws the series and labels both ends of the window", () => {
    const { container } = render(
      <AreaChart points={points} label="Reader-minutes per day" />,
    );
    // Area plus line.
    expect(container.querySelectorAll("path")).toHaveLength(2);
    expect(screen.getByText("2026-07-01")).toBeInTheDocument();
    expect(screen.getByText("2026-07-03")).toBeInTheDocument();
  });

  it("describes itself for anyone who cannot see it", () => {
    render(
      <AreaChart
        points={points}
        label="Reader-minutes per day"
        formatValue={(v) => `${v} min`}
      />,
    );
    const chart = screen.getByRole("img", { name: /Reader-minutes per day/ });
    // Total and peak, not just "a chart".
    expect(chart).toHaveAccessibleName(/60 min over 3 days/);
    expect(chart).toHaveAccessibleName(/peaking at 40 min/);
  });

  it("gives every day its own hit target and value", () => {
    const { container } = render(
      <AreaChart points={points} label="Reader-minutes" formatValue={(v) => `${v}m`} />,
    );
    const titles = [...container.querySelectorAll("rect title")].map((t) => t.textContent);
    expect(titles).toEqual(["2026-07-01: 0m", "2026-07-02: 40m", "2026-07-03: 20m"]);
  });

  it("says so rather than drawing a flat line at zero", () => {
    render(
      <AreaChart
        points={[
          { label: "a", value: 0 },
          { label: "b", value: 0 },
        ]}
        label="Reader-minutes"
        emptyHint="No reads recorded in the last 30 days."
      />,
    );
    expect(screen.getByText("No reads recorded in the last 30 days.")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("does not try to draw a line through a single point", () => {
    render(<AreaChart points={[{ label: "a", value: 5 }]} label="x" emptyHint="Not yet." />);
    expect(screen.getByText("Not yet.")).toBeInTheDocument();
  });

  // The numbers below come from the module's own constants: a 640×180 viewBox
  // with 8px sides, 14 top and 22 bottom, so the plot is 624×144 and its
  // baseline is at y=158. Asserting them directly is the only way to catch a
  // chart that renders and is wrong — a series drawn upside down, or one
  // squeezed into the top half, still produces two <path> elements.
  it("puts the peak at the top of the plot and a zero on the baseline", () => {
    const { container } = render(
      <AreaChart
        points={[
          { label: "a", value: 0 },
          { label: "b", value: 50 },
        ]}
        label="x"
      />,
    );

    const line = container.querySelectorAll("path")[1].getAttribute("d");
    expect(line).toBe("M8,158 L632,14");
  });

  it("spaces the days evenly across the full width", () => {
    const { container } = render(<AreaChart points={points} label="x" />);

    const xs = [...container.querySelectorAll("path")[1].getAttribute("d").matchAll(/[ML](\d+\.?\d*),/g)]
      .map((m) => Number(m[1]));
    expect(xs).toEqual([8, 320, 632]);
  });

  it("closes the fill down to the baseline rather than leaving it open", () => {
    // The area is the line plus two corners and a Z. Without them the gradient
    // fills the region above the line instead of below it.
    const { container } = render(<AreaChart points={points} label="x" />);

    const area = container.querySelectorAll("path")[0].getAttribute("d");
    expect(area).toMatch(/L632,158 L8,158 Z$/);
  });

  it("treats a day the API did not measure as zero rather than as NaN", () => {
    // Analytics sends null for a figure that was never captured. `Number(null)`
    // is 0, but `Number(undefined)` is NaN, and one NaN in a path attribute
    // makes the whole line disappear silently.
    const { container } = render(
      <AreaChart
        points={[
          { label: "a", value: null },
          { label: "b", value: undefined },
          { label: "c", value: 10 },
        ]}
        label="x"
      />,
    );

    const line = container.querySelectorAll("path")[1].getAttribute("d");
    expect(line).not.toMatch(/NaN/);
    expect(line).toBe("M8,158 L320,158 L632,14");
  });

  it("holds the same height when there is nothing to draw", () => {
    // Same footprint as a drawn chart, so a panel does not jump when the data
    // arrives.
    render(<AreaChart points={[]} label="x" emptyHint="Not yet." />);

    expect(screen.getByText("Not yet.").className).toMatch(/h-\[180px\]/);
  });

  it("states the total and the peak for a reader who cannot see the shape", () => {
    render(<AreaChart points={points} label="x" formatValue={(v) => `${v}m`} />);

    expect(screen.getByText(/Peak 40m; 60m in total\./)).toBeInTheDocument();
  });

  it("renders values as plain numbers when given no formatter", () => {
    const { container } = render(<AreaChart points={points} label="Reads" />);

    expect(container.querySelector("rect title").textContent).toBe("2026-07-01: 0");
  });
});

describe("BarRow", () => {
  it("scales against the set's maximum, not its own value", () => {
    const { container } = render(
      <BarRow label="Long · 9+ min" value={25} max={100} display="25 pub" />,
    );
    expect(barWidth(container.querySelector('[style*="width"]'))).toBeCloseTo(25);
    expect(screen.getByText("25 pub")).toBeInTheDocument();
  });

  it("draws nothing when there is no maximum to scale against", () => {
    const { container } = render(<BarRow label="Short" value={0} max={0} display="0 pub" />);
    expect(barWidth(container.querySelector('[style*="width"]'))).toBe(0);
  });

  it("carries the value in the label for screen readers", () => {
    render(<BarRow label="Short · 1–3 min" value={4} max={10} display="4.0%" />);
    expect(screen.getByRole("img", { name: "Short · 1–3 min: 4.0%" })).toBeInTheDocument();
  });

  it("clamps a negative value to nothing rather than drawing backwards", () => {
    const { container } = render(
      <BarRow label="Delta" value={-30} max={100} display="−30" />,
    );

    expect(barWidth(container.querySelector('[style*="width"]'))).toBe(0);
  });

  it("reads a missing value as zero rather than drawing a NaN-wide bar", () => {
    const { container } = render(
      <BarRow label="Unmeasured" value={null} max={100} display="—" />,
    );

    expect(barWidth(container.querySelector('[style*="width"]'))).toBe(0);
  });

  it("mutes every row but the one worth looking at", () => {
    // Analytics gives the accent to the best-performing platform and mutes the
    // rest. One accent per view is the palette's rule; a page of brand-coloured
    // bars means nothing by meaning everything.
    const { container: brand } = render(
      <BarRow label="Dev.to" value={5} max={10} display="5" tone="brand" />,
    );
    const { container: muted } = render(
      <BarRow label="Medium" value={5} max={10} display="5" tone="muted" />,
    );

    expect(brand.querySelector('[style*="width"]').className).toMatch(/bg-brand-500/);
    expect(muted.querySelector('[style*="width"]').className).toMatch(/bg-ink-400/);
    expect(muted.querySelector('[style*="width"]').className).not.toMatch(/brand/);
  });

  it("defaults to the accent when no tone is asked for", () => {
    const { container } = render(<BarRow label="Only" value={5} max={10} display="5" />);

    expect(container.querySelector('[style*="width"]').className).toMatch(/bg-brand-500/);
  });

  it("shows a hint under the bar, and nothing where there is none", () => {
    const { rerender } = render(
      <BarRow label="Dev.to" value={5} max={10} display="5" hint="2.1% of readers" />,
    );
    expect(screen.getByText("2.1% of readers")).toBeInTheDocument();

    rerender(<BarRow label="Dev.to" value={5} max={10} display="5" />);
    expect(screen.queryByText("2.1% of readers")).not.toBeInTheDocument();
  });
});

describe("CompareBars", () => {
  it("draws both shares of each row against the same 100%", () => {
    render(
      <CompareBars
        aLabel="Pieces"
        bLabel="Minutes read"
        rows={[{ label: "Long · 9+ min", a: 0.2, b: 0.8, hint: "20% / 80%" }]}
      />,
    );
    expect(
      screen.getByRole("img", { name: "Long · 9+ min, Pieces: 20%" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: "Long · 9+ min, Minutes read: 80%" }),
    ).toBeInTheDocument();
    expect(screen.getByText("20% / 80%")).toBeInTheDocument();
  });

  it("names both series once, above every row", () => {
    render(
      <CompareBars
        aLabel="Pieces"
        bLabel="Minutes read"
        rows={[
          { label: "Short", a: 0.5, b: 0.1, hint: "50% / 10%" },
          { label: "Long", a: 0.5, b: 0.9, hint: "50% / 90%" },
        ]}
      />,
    );

    expect(screen.getAllByText("Pieces")).toHaveLength(1);
    expect(screen.getAllByRole("img", { name: /Pieces:/ })).toHaveLength(2);
  });

  it("draws each row's two shares against the same 100%, not against each other", () => {
    // The point of the component: 20% of the output earning 80% of the reading
    // is only legible if both strips span the same width.
    const { container } = render(
      <CompareBars
        aLabel="Pieces"
        bLabel="Minutes read"
        rows={[{ label: "Long", a: 0.2, b: 0.8, hint: "" }]}
      />,
    );

    const [a, b] = [...container.querySelectorAll('[style*="width"]')].map(barWidth);
    expect(a).toBeCloseTo(20);
    expect(b).toBeCloseTo(80);
  });

  it("draws a share of nothing as nothing rather than as a full bar", () => {
    const { container } = render(
      <CompareBars
        aLabel="Pieces"
        bLabel="Minutes read"
        rows={[{ label: "Ignored", a: 0.3, b: 0, hint: "30% / 0%" }]}
      />,
    );

    expect(barWidth([...container.querySelectorAll('[style*="width"]')][1])).toBe(0);
    expect(
      screen.getByRole("img", { name: "Ignored, Minutes read: 0%" }),
    ).toBeInTheDocument();
  });

  it("renders the legend and nothing else when there are no rows", () => {
    render(<CompareBars aLabel="Pieces" bLabel="Minutes read" rows={[]} />);

    expect(screen.getByText("Pieces")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});

describe("MultiLineChart", () => {
  const series = [
    {
      key: "views",
      label: "Views",
      points: [
        { label: "2026-07-01", value: 100 },
        { label: "2026-07-02", value: 200 },
        { label: "2026-07-03", value: 50 },
      ],
    },
    {
      key: "reads",
      label: "Reads",
      points: [
        { label: "2026-07-01", value: 10 },
        { label: "2026-07-02", value: 40 },
        { label: "2026-07-03", value: 5 },
      ],
    },
  ];

  it("draws one line per series and names them in a legend", () => {
    const { container } = render(
      <MultiLineChart series={series} label="Views and reads per day" />,
    );
    expect(container.querySelectorAll("path")).toHaveLength(2);
    expect(screen.getByText("Views")).toBeInTheDocument();
    expect(screen.getByText("Reads")).toBeInTheDocument();
  });

  it("puts every series on one scale, so the gap between them is real", () => {
    const { container } = render(
      <MultiLineChart series={series} label="Views and reads per day" />,
    );
    const [viewsPath, readsPath] = [...container.querySelectorAll("path")]
      // Drawn back to front, so the accent series is last in the DOM.
      .reverse()
      .map((path) => path.getAttribute("d"));

    // Both peak on the same day, and the peak of the smaller series must sit
    // well below the peak of the larger one (a smaller y is higher up). Scaled
    // per series they would be identical, which is the lie this chart exists
    // to avoid.
    const peakY = (d) => Math.min(...[...d.matchAll(/,(\d+\.?\d*)/g)].map((m) => +m[1]));
    expect(peakY(viewsPath)).toBeLessThan(peakY(readsPath));
  });

  it("carries every series' value on each day's hit target", () => {
    const { container } = render(
      <MultiLineChart
        series={series}
        label="Views and reads"
        formatValue={(v) => `${v}`}
      />,
    );
    const titles = [...container.querySelectorAll("rect title")].map((t) => t.textContent);
    expect(titles[1]).toBe("2026-07-02\nViews: 200\nReads: 40");
  });

  it("describes each series' peak for anyone who cannot see it", () => {
    render(<MultiLineChart series={series} label="Views and reads per day" />);
    const chart = screen.getByRole("img", { name: /Views and reads per day/ });
    expect(chart).toHaveAccessibleName(/Views peaking at 200/);
    expect(chart).toHaveAccessibleName(/Reads peaking at 40/);
  });

  it("says so rather than drawing flat lines at zero", () => {
    render(
      <MultiLineChart
        series={[
          { key: "a", label: "A", points: [{ label: "x", value: 0 }, { label: "y", value: 0 }] },
        ]}
        label="Nothing"
        emptyHint="Nothing recorded in the last 30 days."
      />,
    );
    expect(screen.getByText("Nothing recorded in the last 30 days.")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("survives being handed no series at all", () => {
    render(<MultiLineChart series={[]} label="Nothing" emptyHint="Not yet." />);
    expect(screen.getByText("Not yet.")).toBeInTheDocument();
  });

  it("draws the accent series last, so it sits on top of its context", () => {
    // Painted back to front. Reversed, the series the panel is about would be
    // hidden under the two it is being compared against wherever they cross.
    const { container } = render(<MultiLineChart series={series} label="x" />);

    const strokes = [...container.querySelectorAll("path")].map(
      (path) => path.getAttribute("class"),
    );
    expect(strokes.at(-1)).toMatch(/stroke-brand-500/);
    expect(strokes.at(0)).toMatch(/stroke-ink-400/);
  });

  it("tells the two ink series apart by dash rather than by another colour", () => {
    // The palette has one accent, spent on the first series. A third hue would
    // be the loudest thing on the page and would mean nothing by being loud.
    const third = {
      key: "clicks",
      label: "Clicks",
      points: [
        { label: "2026-07-01", value: 1 },
        { label: "2026-07-02", value: 2 },
        { label: "2026-07-03", value: 1 },
      ],
    };
    const { container } = render(
      <MultiLineChart series={[...series, third]} label="x" />,
    );

    const paths = [...container.querySelectorAll("path")].reverse();
    expect(paths[1].getAttribute("stroke-dasharray")).toBeNull();
    expect(paths[2].getAttribute("stroke-dasharray")).toBe("3 3");
    expect(paths[1].getAttribute("class")).toBe(paths[2].getAttribute("class"));
  });

  it("cycles the tones rather than dropping a fourth series", () => {
    const extra = (key) => ({
      key,
      label: key,
      points: [
        { label: "a", value: 1 },
        { label: "b", value: 2 },
      ],
    });
    const { container } = render(
      <MultiLineChart
        series={[extra("one"), extra("two"), extra("three"), extra("four")]}
        label="x"
      />,
    );

    expect(container.querySelectorAll("path")).toHaveLength(4);
    const paths = [...container.querySelectorAll("path")].reverse();
    // Index 3 wraps back to the accent.
    expect(paths[3].getAttribute("class")).toBe(paths[0].getAttribute("class"));
  });

  it("reads a shorter series as zero on the days it does not cover", () => {
    // The API returns every day for every series, so this should not happen —
    // and the `?? 0` is there because the alternative is "undefined" printed in
    // a tooltip.
    const { container } = render(
      <MultiLineChart
        series={[
          series[0],
          { key: "short", label: "Short", points: [{ label: "2026-07-01", value: 7 }] },
        ]}
        label="x"
      />,
    );

    const titles = [...container.querySelectorAll("rect title")].map((t) => t.textContent);
    expect(titles[2]).toBe("2026-07-03\nViews: 50\nShort: 0");
  });

  it("treats a day the API did not measure as zero rather than as NaN", () => {
    const { container } = render(
      <MultiLineChart
        series={[
          {
            key: "views",
            label: "Views",
            points: [
              { label: "a", value: null },
              { label: "b", value: 20 },
            ],
          },
        ]}
        label="x"
      />,
    );

    expect(container.querySelector("path").getAttribute("d")).toBe("M8,158 L632,14");
  });

  it("takes its peak from the tallest series, not from the first", () => {
    // The axis comes from series[0]; the scale must not. A context series that
    // happens to be larger would otherwise be drawn off the top of the plot.
    const { container } = render(
      <MultiLineChart
        series={[
          { key: "a", label: "A", points: [{ label: "x", value: 10 }, { label: "y", value: 20 }] },
          { key: "b", label: "B", points: [{ label: "x", value: 40 }, { label: "y", value: 80 }] },
        ]}
        label="x"
      />,
    );

    const ys = [...container.querySelectorAll("path")].flatMap((path) =>
      [...path.getAttribute("d").matchAll(/,(\d+\.?\d*)/g)].map((m) => Number(m[1])),
    );
    // 14 is the top of the plot: nothing may sit above it.
    expect(Math.min(...ys)).toBe(14);
  });

  it("says how many series the caption is summarising", () => {
    render(<MultiLineChart series={series} label="x" formatValue={(v) => `${v}`} />);

    expect(screen.getByText(/Peak 200 across 2 series\./)).toBeInTheDocument();
  });

  it("holds the same height when there is nothing to draw", () => {
    render(<MultiLineChart series={[]} label="x" emptyHint="Not yet." />);

    expect(screen.getByText("Not yet.").className).toMatch(/h-\[180px\]/);
  });
});
