// API client for the Herald backend.
// Stores the JWT in localStorage and attaches it as a Bearer token.

const BASE = "/api/v1";
const TOKEN_KEY = "herald_token";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

async function request(path, { method = "GET", body, form, auth = true } = {}) {
  const headers = {};
  const token = getToken();
  if (auth && token) headers["Authorization"] = `Bearer ${token}`;

  let payload;
  if (form) {
    payload = form; // URLSearchParams or FormData
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }

  const res = await fetch(`${BASE}${path}`, { method, headers, body: payload });

  if (res.status === 401) setToken(null);

  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail = data?.detail ?? res.statusText;
    // FastAPI validation errors arrive as a list of {loc, msg} objects.
    const message = Array.isArray(detail)
      ? detail.map((d) => d.msg).join(", ")
      : typeof detail === "string"
        ? detail
        : JSON.stringify(detail);
    throw new Error(message);
  }
  return data;
}

/** Build a query string, dropping empty values so `?status=` never appears. */
function qs(params) {
  const entries = Object.entries(params).filter(
    ([, v]) => v !== null && v !== undefined && v !== "",
  );
  return entries.length ? `?${new URLSearchParams(entries)}` : "";
}

export const api = {
  // ---- auth ----
  register: (email, password, full_name) =>
    request("/auth/register", {
      method: "POST",
      auth: false,
      body: { email, password, full_name },
    }),
  login: async (email, password) => {
    const form = new URLSearchParams({ username: email, password });
    const data = await request("/auth/login", { method: "POST", auth: false, form });
    setToken(data.access_token);
    return data;
  },
  me: () => request("/auth/me"),
  logout: () => setToken(null),

  health: () => request("/health", { auth: false }),

  // ---- projects ----
  listProjects: () => request("/projects"),
  createProject: (payload) => request("/projects", { method: "POST", body: payload }),
  getProject: (id) => request(`/projects/${id}`),
  updateProject: (id, payload) =>
    request(`/projects/${id}`, { method: "PATCH", body: payload }),
  deleteProject: (id) => request(`/projects/${id}`, { method: "DELETE" }),
  scanProject: (id) => request(`/projects/${id}/scan`, { method: "POST" }),
  projectIdeas: (id, refresh = false) =>
    request(`/projects/${id}/ideas${refresh ? "?refresh=true" : ""}`),

  // ---- content ----
  listContent: (params = {}) => request(`/content${qs(params)}`),
  getContent: (id) => request(`/content/${id}`),
  // Makes outbound HTTP requests, so it is called on demand rather than with
  // every editor load.
  checkLinks: (id) => request(`/content/${id}/links`),
  // The Open Graph / Twitter tags for a piece, as saved. The editor predicts
  // the *card* locally while you type (lib/socialCards); this is the authority
  // for the tags themselves, which you paste once into your own template.
  socialCards: (id) => request(`/content/${id}/social`),
  generateContent: (payload) =>
    request("/content/generate", { method: "POST", body: payload }),
  createContent: (payload) => request("/content", { method: "POST", body: payload }),
  updateContent: (id, payload) =>
    request(`/content/${id}`, { method: "PATCH", body: payload }),
  deleteContent: (id) => request(`/content/${id}`, { method: "DELETE" }),
  approveContent: (id) => request(`/content/${id}/approve`, { method: "POST" }),
  publishContent: (id, payload) =>
    request(`/content/${id}/publish`, { method: "POST", body: payload }),
  retryPublication: (contentId, publicationId) =>
    request(`/content/${contentId}/retry/${publicationId}`, { method: "POST" }),
  writeFromIdea: (ideaId) =>
    request(`/content/ideas/${ideaId}/write`, { method: "POST" }),
  reviewQueue: () => request("/content/queue/review"),
  publicationQueue: () => request("/content/queue/publications"),

  // ---- calendar ----
  calendar: (params = {}) => request(`/calendar${qs(params)}`),
  reschedule: (contentId, payload) =>
    request(`/calendar/content/${contentId}`, { method: "PATCH", body: payload }),
  cadence: (platform) => request(`/calendar/cadence${qs({ platform })}`),

  // ---- analytics ----
  dashboard: () => request("/analytics/dashboard"),
  analytics: () => request("/analytics/overview"),
  // Kept out of /analytics/dashboard on purpose: the read-time panel is one
  // section far down the home page, and the dashboard payload is fetched on
  // every navigation back to it.
  readTime: () => request("/analytics/read-time"),
  engagementTrend: (days) => request(`/analytics/engagement-trend${qs({ days })}`),
  // How fast pieces found an audience, read from the stored snapshot series
  // rather than the latest number per publication.
  velocity: () => request("/analytics/velocity"),
  velocityCurve: (publicationId) =>
    request(`/analytics/velocity/${publicationId}`),
  // The dashboard payload already carries a capped list of these; this is the
  // full one, for the analytics page.
  alerts: (limit) => request(`/analytics/alerts${qs({ limit })}`),

  // ---- triggers ----
  triggerKinds: () => request("/triggers/kinds"),
  listTriggers: (params = {}) => request(`/triggers${qs(params)}`),
  // The response carries the signing secret, and no later read of the trigger
  // ever will — same contract as an outbound webhook's.
  createTrigger: (payload) => request("/triggers", { method: "POST", body: payload }),
  updateTrigger: (id, payload) =>
    request(`/triggers/${id}`, { method: "PATCH", body: payload }),
  deleteTrigger: (id) => request(`/triggers/${id}`, { method: "DELETE" }),
  rotateTriggerSecret: (id) =>
    request(`/triggers/${id}/rotate-secret`, { method: "POST" }),
  // Polls now, ignoring the schedule. Synchronous: the question the button asks
  // is "does my feed URL work?", and an answer that turns up in a list a minute
  // later does not answer it.
  checkTrigger: (id) => request(`/triggers/${id}/check`, { method: "POST" }),
  triggerEvents: (id, params = {}) => request(`/triggers/${id}/events${qs(params)}`),

  // ---- settings ----
  platforms: () => request("/settings/platforms"),
  saveConnection: (platform, credentials) =>
    request("/settings/connections", {
      method: "PUT",
      body: { platform, credentials },
    }),
  verifyConnection: (platform) =>
    request(`/settings/connections/${platform}/verify`, { method: "POST" }),
  deleteConnection: (platform) =>
    request(`/settings/connections/${platform}`, { method: "DELETE" }),
};
