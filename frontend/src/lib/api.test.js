// What the API client does with a response that is not Herald's.
//
// Every error the UI shows comes out of `request`, and the shape it reads —
// `{detail: "..."}` — is FastAPI's. But not everything that answers a fetch is
// FastAPI: a gateway timeout, a proxy's request-too-large page and a plain HTML
// error page all arrive here as text. Parsing those as JSON threw a SyntaxError
// whose message is "Unexpected token '<'", and *that* is what landed in the
// toast — a browser diagnostic in place of the status that would have told the
// user what happened.

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";

import { api, getToken, setToken } from "./api";

/** Stand in for one fetch round trip. */
function respondWith({ status = 200, body = "", statusText = "" } = {}) {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    statusText,
    text: async () => body,
  });
}

describe("request", () => {
  beforeEach(() => {
    setToken(null);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("surfaces the status when an error body is not JSON at all", async () => {
    vi.stubGlobal(
      "fetch",
      respondWith({
        status: 502,
        statusText: "Bad Gateway",
        body: "<html><body>502 Bad Gateway</body></html>",
      }),
    );

    await expect(api.listProjects()).rejects.toThrow("Bad Gateway");
  });

  it("never leaks a JSON parse error into the message", async () => {
    vi.stubGlobal(
      "fetch",
      respondWith({ status: 504, statusText: "Gateway Timeout", body: "timeout" }),
    );

    await expect(api.listProjects()).rejects.toThrow(/^Gateway Timeout$/);
  });

  it("falls back to the status code when there is no status text either", async () => {
    vi.stubGlobal("fetch", respondWith({ status: 413, statusText: "", body: "too big" }));

    await expect(api.listProjects()).rejects.toThrow("HTTP 413");
  });

  it("still prefers FastAPI's own detail when there is one", async () => {
    vi.stubGlobal(
      "fetch",
      respondWith({
        status: 409,
        statusText: "Conflict",
        body: JSON.stringify({ detail: "Already published" }),
      }),
    );

    await expect(api.listProjects()).rejects.toThrow("Already published");
  });

  it("joins a validation error's messages", async () => {
    vi.stubGlobal(
      "fetch",
      respondWith({
        status: 422,
        body: JSON.stringify({
          detail: [{ msg: "field required" }, { msg: "too long" }],
        }),
      }),
    );

    await expect(api.listProjects()).rejects.toThrow("field required, too long");
  });

  it("returns null rather than throwing for an empty 204 body", async () => {
    vi.stubGlobal("fetch", respondWith({ status: 204, body: "" }));

    await expect(api.deleteProject(1)).resolves.toBeNull();
  });

  it("clears a token the server has stopped accepting", async () => {
    setToken("stale");
    vi.stubGlobal("fetch", respondWith({ status: 401, statusText: "Unauthorized" }));

    await expect(api.me()).rejects.toThrow();
    expect(getToken()).toBeNull();
  });
});
