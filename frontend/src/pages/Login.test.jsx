/**
 * Auth form behaviour on the landing page: sign-in and sign-up share one
 * split-panel page with tab switching. The only branch is which `useAuth`
 * method gets called and which fields are visible.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import LandingPage from "./LandingPage";
import { useAuth } from "../hooks/useAuth";
import { ROUTER_FUTURE } from "../lib/routerFuture";

vi.mock("../hooks/useAuth", () => ({ useAuth: vi.fn() }));

function draw() {
  return render(
    <MemoryRouter future={ROUTER_FUTURE}>
      <LandingPage />
    </MemoryRouter>,
  );
}

let login;
let register;

beforeEach(() => {
  login = vi.fn().mockResolvedValue();
  register = vi.fn().mockResolvedValue();
  useAuth.mockReturnValue({ user: null, login, register });
});

describe("signing in", () => {
  it("submits email and password to login, not register", async () => {
    const user = userEvent.setup();
    draw();

    await user.type(screen.getByLabelText("Email"), "you@example.com");
    await user.type(screen.getByLabelText("Password"), "hunter22222");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(login).toHaveBeenCalledWith("you@example.com", "hunter22222");
    expect(register).not.toHaveBeenCalled();
  });

  it("shows the server's error rather than swallowing it", async () => {
    login.mockRejectedValue(new Error("Incorrect email or password"));
    const user = userEvent.setup();
    draw();

    await user.type(screen.getByLabelText("Email"), "you@example.com");
    await user.type(screen.getByLabelText("Password"), "wrongpassword");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("Incorrect email or password")).toBeInTheDocument();
  });

  it("disables the submit button while the request is in flight", async () => {
    let resolveLogin;
    login.mockReturnValue(
      new Promise((resolve) => {
        resolveLogin = resolve;
      }),
    );
    const user = userEvent.setup();
    draw();

    await user.type(screen.getByLabelText("Email"), "you@example.com");
    await user.type(screen.getByLabelText("Password"), "hunter22222");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(screen.getByRole("button", { name: "One moment…" })).toBeDisabled();
    resolveLogin();
    await screen.findByRole("button", { name: "Sign in" });
  });

  it("does not show a name field", () => {
    draw();
    expect(screen.queryByLabelText(/^Name/)).not.toBeInTheDocument();
  });
});

describe("switching to sign up", () => {
  it("reveals the optional name field and renames the submit button", async () => {
    const user = userEvent.setup();
    draw();

    await user.click(screen.getByRole("tab", { name: "Create account" }));

    expect(screen.getByLabelText(/^Name/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create account" })).toBeInTheDocument();
  });

  it("submits to register with the optional name, not login", async () => {
    const user = userEvent.setup();
    draw();
    await user.click(screen.getByRole("tab", { name: "Create account" }));

    await user.type(screen.getByLabelText("Email"), "new@example.com");
    await user.type(screen.getByLabelText(/^Name/), "Ada Lovelace");
    await user.type(screen.getByLabelText("Password"), "hunter22222");
    await user.click(screen.getByRole("button", { name: "Create account" }));

    expect(register).toHaveBeenCalledWith("new@example.com", "hunter22222", "Ada Lovelace");
    expect(login).not.toHaveBeenCalled();
  });

  it("sends null rather than an empty string for a blank name", async () => {
    const user = userEvent.setup();
    draw();
    await user.click(screen.getByRole("tab", { name: "Create account" }));

    await user.type(screen.getByLabelText("Email"), "new@example.com");
    await user.type(screen.getByLabelText("Password"), "hunter22222");
    await user.click(screen.getByRole("button", { name: "Create account" }));

    expect(register).toHaveBeenCalledWith("new@example.com", "hunter22222", null);
  });

  it("clears a prior error when the mode is toggled", async () => {
    login.mockRejectedValue(new Error("Incorrect email or password"));
    const user = userEvent.setup();
    draw();

    await user.type(screen.getByLabelText("Email"), "you@example.com");
    await user.type(screen.getByLabelText("Password"), "wrongpassword");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByText("Incorrect email or password");

    await user.click(screen.getByRole("tab", { name: "Create account" }));

    expect(screen.queryByText("Incorrect email or password")).not.toBeInTheDocument();
  });

  it("toggles back to signing in, taking the name field with it", async () => {
    const user = userEvent.setup();
    draw();

    await user.click(screen.getByRole("tab", { name: "Create account" }));
    expect(screen.getByLabelText(/Name/)).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Sign in" }));

    expect(screen.queryByLabelText(/Name/)).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Create account" })).toBeInTheDocument();
  });
});

describe("already signed in", () => {
  it("redirects away from the landing page", () => {
    useAuth.mockReturnValue({ user: { id: 1, email: "you@example.com" }, login, register });
    draw();

    expect(screen.queryByRole("button", { name: "Sign in" })).not.toBeInTheDocument();
  });
});
