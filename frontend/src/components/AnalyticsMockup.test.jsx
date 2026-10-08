import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import AnalyticsMockup from "./AnalyticsMockup";

const wrap = () =>
  render(
    <MemoryRouter>
      <AnalyticsMockup />
    </MemoryRouter>,
  );

describe("AnalyticsMockup", () => {
  it("renders the four stat cards", () => {
    wrap();
    expect(screen.getByText("Open Rate")).toBeInTheDocument();
    expect(screen.getByText("Click Rate")).toBeInTheDocument();
    expect(screen.getByText("Subscribers")).toBeInTheDocument();
    expect(screen.getByText("Bounce Rate")).toBeInTheDocument();
  });

  it("shows mock stat values", () => {
    wrap();
    expect(screen.getByText("42.3%")).toBeInTheDocument();
    expect(screen.getByText("8.7%")).toBeInTheDocument();
    expect(screen.getByText("2,847")).toBeInTheDocument();
  });

  it("shows the preview badge", () => {
    wrap();
    expect(screen.getByText("Preview")).toBeInTheDocument();
  });

  it("renders chart section titles", () => {
    wrap();
    expect(screen.getByText("Opens this week")).toBeInTheDocument();
    expect(screen.getByText("Subscriber growth")).toBeInTheDocument();
  });

  it("shows upgrade CTA", () => {
    wrap();
    expect(screen.getByText(/Start free/)).toBeInTheDocument();
  });

  it("renders day labels for the bar chart", () => {
    wrap();
    expect(screen.getByText("Mon")).toBeInTheDocument();
    expect(screen.getByText("Fri")).toBeInTheDocument();
    expect(screen.getByText("Sun")).toBeInTheDocument();
  });
});
