import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import LandingPage from "./LandingPage";

vi.mock("../hooks/useAuth", () => ({
  useAuth: () => ({
    user: null,
    loading: false,
    login: vi.fn(),
    register: vi.fn(),
  }),
}));

const wrap = () =>
  render(
    <MemoryRouter>
      <LandingPage />
    </MemoryRouter>,
  );

describe("LandingPage", () => {
  describe("comparison table", () => {
    it("renders the comparison section header", () => {
      wrap();
      expect(screen.getByText("Why creators choose Pulse")).toBeInTheDocument();
    });

    it("shows platform column headers", () => {
      wrap();
      const table = screen.getByRole("table");
      expect(table).toBeInTheDocument();
      expect(table.textContent).toContain("Mailchimp");
      expect(table.textContent).toContain("Substack");
    });

    it("shows feature rows in the comparison table", () => {
      wrap();
      const table = screen.getByRole("table");
      expect(table.textContent).toContain("Send time optimization");
      expect(table.textContent).toContain("Built for India");
      expect(table.textContent).toContain("Custom domain");
    });
  });

  describe("pricing section", () => {
    it("renders the pricing section header", () => {
      wrap();
      expect(screen.getByText("Simple, transparent pricing")).toBeInTheDocument();
    });

    it("shows all three tier names", () => {
      wrap();
      expect(screen.getByText("Pro")).toBeInTheDocument();
      expect(screen.getByText("Business")).toBeInTheDocument();
    });

    it("shows tier prices", () => {
      wrap();
      expect(screen.getByText("999")).toBeInTheDocument();
      expect(screen.getByText("2,999")).toBeInTheDocument();
    });

    it("shows the Pro tier with a highlight class", () => {
      wrap();
      const proCard = screen.getByText("Pro").closest(".landing-pricing-card");
      expect(proCard).toHaveClass("landing-pricing-highlight");
    });

    it("lists features for each tier", () => {
      wrap();
      expect(screen.getByText("Unlimited subscribers")).toBeInTheDocument();
      expect(screen.getByText("Advanced analytics dashboard")).toBeInTheDocument();
      expect(screen.getByText("Dedicated IP")).toBeInTheDocument();
    });
  });

  describe("analytics mockup", () => {
    it("renders the analytics preview", () => {
      wrap();
      expect(screen.getByText("See what your audience does")).toBeInTheDocument();
    });
  });
});
