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

  it("stringifies a detail that is neither a string nor a validation list", async () => {
    vi.stubGlobal(
      "fetch",
      respondWith({ status: 400, body: JSON.stringify({ detail: { code: 7 } }) }),
    );

    await expect(api.listProjects()).rejects.toThrow('{"code":7}');
  });
});

/**
 * What `request` puts on the wire.
 *
 * Everything above is about reading a response; none of it looks at the request
 * that produced one. But the three decisions this function makes before the
 * fetch — whether to attach the bearer token, whether the body is JSON or a
 * form, and which query parameters survive — are each load-bearing and each
 * silent when wrong. A `Content-Type: application/json` on the login form is a
 * 422 from FastAPI's OAuth2 form parser; an `Authorization` header on the
 * public preview endpoint turns a link anyone can open into one only its author
 * can; and `?status=` reaches the API as an empty string rather than as the
 * absence of a filter.
 */
describe("the outgoing request", () => {
  /** The single fetch call `fn` made, as `[url, init]`. */
  function sentBy(fetchMock) {
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    return { url, init };
  }

  function stubOk(body = "{}") {
    const fetchMock = respondWith({ status: 200, body });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  beforeEach(() => {
    setToken(null);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("attaches the stored token as a bearer credential", async () => {
    setToken("abc123");
    const fetchMock = stubOk();

    await api.me();

    const { url, init } = sentBy(fetchMock);
    expect(url).toBe("/api/v1/auth/me");
    expect(init.headers.Authorization).toBe("Bearer abc123");
  });

  it("sends no credential at all when there is no token", async () => {
    const fetchMock = stubOk();

    await api.me();

    expect(sentBy(fetchMock).init.headers).not.toHaveProperty("Authorization");
  });

  it("keeps the token off the endpoints that are public by design", async () => {
    setToken("abc123");
    const fetchMock = stubOk();

    await api.publicPreview("tok");

    const { url, init } = sentBy(fetchMock);
    expect(url).toBe("/api/v1/content/preview/tok");
    expect(init.headers).not.toHaveProperty("Authorization");
  });

  it("authenticates the detail health check even though the probe is public", async () => {
    setToken("abc123");
    const publicProbe = stubOk();
    await api.health();
    expect(sentBy(publicProbe).init.headers).not.toHaveProperty("Authorization");

    vi.unstubAllGlobals();
    const detail = stubOk();
    await api.healthDetail();
    expect(sentBy(detail).init.headers.Authorization).toBe("Bearer abc123");
  });

  it("encodes a JSON body and declares it", async () => {
    const fetchMock = stubOk();

    await api.createProject({ name: "Herald" });

    const { url, init } = sentBy(fetchMock);
    expect(url).toBe("/api/v1/projects");
    expect(init.method).toBe("POST");
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(init.body).toBe('{"name":"Herald"}');
  });

  it("hands a form through untouched, so the browser sets its own content type", async () => {
    const fetchMock = stubOk(JSON.stringify({ access_token: "t" }));

    await api.login("you@example.com", "pw");

    const { url, init } = sentBy(fetchMock);
    expect(url).toBe("/api/v1/auth/login");
    expect(init.headers).not.toHaveProperty("Content-Type");
    expect(init.body).toBeInstanceOf(URLSearchParams);
    expect(init.body.get("username")).toBe("you@example.com");
    expect(init.body.get("password")).toBe("pw");
  });

  it("sends no body and no content type on a plain read", async () => {
    const fetchMock = stubOk("[]");

    await api.listProjects();

    const { init } = sentBy(fetchMock);
    expect(init.method).toBe("GET");
    expect(init.body).toBeUndefined();
    expect(init.headers).not.toHaveProperty("Content-Type");
  });

  it("stores the token a successful login returns", async () => {
    stubOk(JSON.stringify({ access_token: "fresh", token_type: "bearer" }));

    await api.login("you@example.com", "pw");

    expect(getToken()).toBe("fresh");
  });

  it("registers without a token, so a signed-out browser can", async () => {
    setToken("someone-elses");
    const fetchMock = stubOk();

    await api.register("you@example.com", "pw", "You");

    const { init } = sentBy(fetchMock);
    expect(init.headers).not.toHaveProperty("Authorization");
    expect(JSON.parse(init.body)).toEqual({
      email: "you@example.com",
      password: "pw",
      full_name: "You",
    });
  });

  it("forgets the token on logout", () => {
    setToken("abc123");

    api.logout();

    expect(getToken()).toBeNull();
  });
});

describe("query strings", () => {
  function stubOk() {
    const fetchMock = respondWith({ status: 200, body: "[]" });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("omits the question mark entirely when nothing is being filtered", async () => {
    const fetchMock = stubOk();

    await api.listContent();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/content");
  });

  it("drops a filter the user cleared rather than sending it empty", async () => {
    const fetchMock = stubOk();

    await api.listContent({ status: "", project_id: 4, q: null });

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/content?project_id=4");
  });

  it("keeps a zero, which is a value and not an absence", async () => {
    const fetchMock = stubOk();

    await api.listContent({ offset: 0, limit: 25 });

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/content?offset=0&limit=25");
  });

  it("escapes a value that would otherwise break the query", async () => {
    const fetchMock = stubOk();

    await api.listContent({ q: "a&b=c d" });

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/content?q=a%26b%3Dc+d");
  });

  it("sends no window when the caller did not choose one", async () => {
    const fetchMock = stubOk();

    await api.engagementTrend(undefined);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/analytics/engagement-trend");
  });
});

/**
 * Endpoints whose method or body is easy to get wrong by one character, and
 * where getting it wrong is a 404 or a 405 rather than anything the UI can
 * explain. The preview-link pair is the sharp one: `create` is the only
 * response that will ever contain the URL, and its body differs by whether the
 * caller chose a lifetime.
 */
describe("endpoint shapes", () => {
  function stubOk() {
    const fetchMock = respondWith({ status: 200, body: "{}" });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts an empty body for a preview link with no chosen lifetime", async () => {
    const fetchMock = stubOk();

    await api.createPreviewLink(9, undefined);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/content/9/preview-links");
    expect(init.method).toBe("POST");
    expect(init.body).toBe("{}");
  });

  it("carries the lifetime when one was chosen", async () => {
    const fetchMock = stubOk();

    await api.createPreviewLink(9, 48);

    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ ttl_hours: 48 });
  });

  it("revokes a link by id under its own content", async () => {
    const fetchMock = stubOk();

    await api.revokePreviewLink(9, 3);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/content/9/preview-links/3");
    expect(init.method).toBe("DELETE");
  });

  it("patches rather than replaces on every update", async () => {
    for (const [call, url] of [
      [() => api.updateProject(1, { name: "x" }), "/api/v1/projects/1"],
      [() => api.updateContent(2, { title: "x" }), "/api/v1/content/2"],
      [() => api.updateTrigger(3, { active: false }), "/api/v1/triggers/3"],
      [() => api.updateTemplate(4, { name: "x" }), "/api/v1/templates/4"],
      [() => api.reschedule(5, { scheduled_for: null }), "/api/v1/calendar/content/5"],
    ]) {
      const fetchMock = stubOk();
      await call();
      expect(fetchMock.mock.calls[0][0]).toBe(url);
      expect(fetchMock.mock.calls[0][1].method).toBe("PATCH");
      vi.unstubAllGlobals();
    }
  });

  it("puts a connection, because saving one twice must not make two", async () => {
    const fetchMock = stubOk();

    await api.saveConnection("devto", { api_key: "k" });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/settings/connections");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body)).toEqual({
      platform: "devto",
      credentials: { api_key: "k" },
    });
  });

  it("retries one publication under its own content, not the whole piece", async () => {
    const fetchMock = stubOk();

    await api.retryPublication(7, 12);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/content/7/retry/12");
    expect(init.method).toBe("POST");
  });
});
