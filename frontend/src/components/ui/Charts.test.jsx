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
});
