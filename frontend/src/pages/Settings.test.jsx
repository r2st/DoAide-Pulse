/**
 * The Settings page's service-health panel.
 *
 * Every row in it — the provider chain, the open breakers, the GitHub token,
 * credential encryption — is a field only `GET /health/detail` returns. The
 * public `/health` is a liveness probe and deliberately withholds all of them,
 * so a page that asked it instead rendered a fully configured server as "none
 * configured", "anonymous" and "off": three rows telling the operator to go and
 * set environment variables that were already set.
 */
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Settings from "./Settings";
import { api } from "../lib/api";
import { ROUTER_FUTURE } from "../lib/routerFuture";

vi.mock("../lib/api", () => ({
  api: { platforms: vi.fn(), healthDetail: vi.fn(), health: vi.fn() },
}));

const toast = { success: vi.fn(), error: vi.fn() };
vi.mock("../components/ui/Toast", () => ({ useToast: () => toast }));

vi.mock("../hooks/useAuth", () => ({
  useAuth: () => ({ user: { email: "you@example.com" }, refresh: vi.fn() }),
}));

function draw() {
  return render(
    <MemoryRouter future={ROUTER_FUTURE}>
      <Settings />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.platforms.mockResolvedValue([]);
  api.healthDetail.mockResolvedValue({
    status: "ok",
    database: { status: "ok", required: true, detail: "" },
    redis: { status: "ok", required: false, detail: "" },
    llm_providers: ["openrouter", "groq"],
    llm_breakers_open: {},
    github_configured: true,
    credential_encryption: true,
    implemented_platforms: ["devto"],
  });
});

describe("service health", () => {
  it("reads the authenticated detail endpoint, not the public probe", async () => {
    draw();
    await screen.findByText("openrouter → groq");

    expect(api.healthDetail).toHaveBeenCalled();
    expect(api.health).not.toHaveBeenCalled();
  });

  it("reports a configured server as configured", async () => {
    draw();

    expect(await screen.findByText("openrouter → groq")).toBeInTheDocument();
    expect(screen.getByText("token set")).toBeInTheDocument();
    expect(screen.getByText("on")).toBeInTheDocument();
  });

  it("still reports an unconfigured one honestly", async () => {
    api.healthDetail.mockResolvedValue({
      status: "ok",
      llm_providers: [],
      llm_breakers_open: {},
      github_configured: false,
      credential_encryption: false,
    });
    draw();

    expect(await screen.findByText("none configured")).toBeInTheDocument();
    expect(screen.getByText("anonymous")).toBeInTheDocument();
    expect(screen.getByText("off")).toBeInTheDocument();
  });

  it("names the providers being skipped while their cool-down runs", async () => {
    api.healthDetail.mockResolvedValue({
      status: "ok",
      llm_providers: ["openrouter", "groq"],
      llm_breakers_open: { groq: { failures: 0, seconds_until_retry: 240 } },
      github_configured: true,
      credential_encryption: true,
    });
    draw();

    expect(await screen.findByText("groq")).toBeInTheDocument();
  });
});
