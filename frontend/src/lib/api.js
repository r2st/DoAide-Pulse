// API client for the Pulse backend.
// Stores the JWT in localStorage and attaches it as a Bearer token.

const BASE = "/api/v1";
const TOKEN_KEY = "pulse_token";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

async function request(
  path,
  { method = "GET", body, form, auth = true, headers: extra } = {},
) {
  const headers = { ...extra };
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
  // Not everything that answers this fetch is Pulse. A gateway timeout, a
  // proxy's request-too-large page and a stray HTML error page all arrive here
  // as text, and `JSON.parse` on them throws a SyntaxError whose message is
  // "Unexpected token '<'" — which is what the user then sees in the toast
  // instead of the status that would tell them what happened. Parse failure is
  // "no structured body", not an error in its own right.
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!res.ok) {
    // `||` on the status text, not `??`: fetch reports an absent reason phrase
    // as `""`, and HTTP/2 has no reason phrase at all, so an empty string is
    // the common case rather than the odd one — and "Error: " with nothing
    // after it tells the user less than the bare status code does.
    const detail = data?.detail ?? (res.statusText || `HTTP ${res.status}`);
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

  // The public liveness probe: status, database, redis, and nothing else.
  health: () => request("/health", { auth: false }),
  // Everything the Settings page's health panel reads — provider chain, open
  // breakers, GitHub token, credential encryption — lives only on this one.
  // The public probe deliberately omits them, so asking it instead reported a
  // fully configured server as "none configured / anonymous / off".
  healthDetail: () => request("/health/detail"),

  // ---- projects ----
  listProjects: () => request("/projects"),
  createProject: (payload) => request("/projects", { method: "POST", body: payload }),
  updateProject: (id, payload) =>
    request(`/projects/${id}`, { method: "PATCH", body: payload }),
  deleteProject: (id) => request(`/projects/${id}`, { method: "DELETE" }),
  scanProject: (id) => request(`/projects/${id}/scan`, { method: "POST" }),

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
  // Rewrites one selected passage. Returns the replacement and persists
  // nothing — the editor splices it in itself, so the browser's own undo still
  // works and the author is the one who decides it was an improvement. The
  // selection must match the *saved* body, so callers save first.
  editPassage: (id, payload) =>
    request(`/content/${id}/edit`, { method: "POST", body: payload }),
  // `version` is the piece's version as the caller last saw it. Sent as
  // `If-Match`, it makes the save conditional: the API answers 412 rather than
  // writing over an edit somebody else made in the meantime. Optional, because
  // a caller with no version to offer (a script, a bulk tool) should still be
  // able to save — see `Content.version` on the backend.
  updateContent: (id, payload, version) =>
    request(`/content/${id}`, {
      method: "PATCH",
      body: payload,
      headers: version == null ? undefined : { "If-Match": `"${version}"` },
    }),
  deleteContent: (id) => request(`/content/${id}`, { method: "DELETE" }),
  approveContent: (id) => request(`/content/${id}/approve`, { method: "POST" }),
  publishContent: (id, payload) =>
    request(`/content/${id}/publish`, { method: "POST", body: payload }),
  retryPublication: (contentId, publicationId) =>
    request(`/content/${contentId}/retry/${publicationId}`, { method: "POST" }),
  reviewQueue: () => request("/content/queue/review"),
  bulkApprove: (contentIds, dryRun = false) =>
    request("/content/bulk/approve", {
      method: "POST",
      body: { content_ids: contentIds, dry_run: dryRun },
    }),
  bulkReject: (contentIds, dryRun = false) =>
    request("/content/bulk/reject", {
      method: "POST",
      body: { content_ids: contentIds, dry_run: dryRun },
    }),
  bulkPublish: (contentIds, platforms, dryRun = false) =>
    request("/content/bulk/publish", {
      method: "POST",
      body: { content_ids: contentIds, platforms, dry_run: dryRun },
    }),
  bulkRetry: (contentIds) =>
    request("/content/bulk/retry", {
      method: "POST",
      body: { content_ids: contentIds },
    }),
  publicationQueue: () => request("/content/queue/publications"),

  // ---- preview links ----
  // A shareable, unauthenticated, read-only URL for one draft. The URL only
  // ever appears in the response to `create` — a later `list` shows a link
  // exists and lets it be revoked, not what it is.
  listPreviewLinks: (contentId) => request(`/content/${contentId}/preview-links`),
  createPreviewLink: (contentId, ttlHours) =>
    request(`/content/${contentId}/preview-links`, {
      method: "POST",
      body: ttlHours ? { ttl_hours: ttlHours } : {},
    }),
  revokePreviewLink: (contentId, linkId) =>
    request(`/content/${contentId}/preview-links/${linkId}`, { method: "DELETE" }),
  // Unauthenticated on purpose — this is what the link itself resolves.
  publicPreview: (token) => request(`/content/preview/${token}`, { auth: false }),

  // ---- calendar ----
  calendar: (params = {}) => request(`/calendar${qs(params)}`),
  reschedule: (contentId, payload) =>
    request(`/calendar/content/${contentId}`, { method: "PATCH", body: payload }),

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

  // ---- templates ----
  templateBuiltins: () => request("/templates/builtins"),
  // The listing leaves `body_template` out — a body is up to 50,000 characters
  // and an account may keep a hundred templates, and the cards render a name,
  // not a body. `getTemplate` is what the editor opens with.
  listTemplates: (params = {}) => request(`/templates${qs(params)}`),
  getTemplate: (id) => request(`/templates/${id}`),
  createTemplate: (payload) => request("/templates", { method: "POST", body: payload }),
  updateTemplate: (id, payload) =>
    request(`/templates/${id}`, { method: "PATCH", body: payload }),
  deleteTemplate: (id) => request(`/templates/${id}`, { method: "DELETE" }),
  // Renders without saving, and renders a half-filled template rather than
  // refusing — the missing names come back in the response instead.
  previewTemplate: (id, payload) =>
    request(`/templates/${id}/preview`, { method: "POST", body: payload }),
  // Writes a draft. Refuses on the missing values `preview` merely reports.
  useTemplate: (id, payload) =>
    request(`/templates/${id}/use`, { method: "POST", body: payload }),

  // ---- uploads ----
  uploadImage: (file) => {
    const form = new FormData();
    form.append("file", file);
    return request("/uploads", { method: "POST", form });
  },

  // ---- AI generation ----
  generateFields: (payload) =>
    request("/ai/generate-fields", { method: "POST", body: payload }),

  // ---- viral / public ----
  publicArticle: (slug) => request(`/articles/${slug}`, { auth: false }),
  subscribe: (email, source = "embed") =>
    request("/subscribers", { method: "POST", body: { email, source }, auth: false }),
  generateContentIdeas: (niche, count = 5) =>
    request("/tools/content-ideas", { method: "POST", body: { niche, count }, auth: false }),

  // ---- webhooks ----
  webhookEvents: () => request("/webhooks/events"),
  listWebhooks: () => request("/webhooks"),
  createWebhook: (payload) => request("/webhooks", { method: "POST", body: payload }),
  updateWebhook: (id, payload) =>
    request(`/webhooks/${id}`, { method: "PATCH", body: payload }),
  deleteWebhook: (id) => request(`/webhooks/${id}`, { method: "DELETE" }),
  rotateWebhookSecret: (id) =>
    request(`/webhooks/${id}/rotate-secret`, { method: "POST" }),
  pingWebhook: (id) => request(`/webhooks/${id}/ping`, { method: "POST" }),
  webhookDeliveries: (id, params = {}) =>
    request(`/webhooks/${id}/deliveries${qs(params)}`),

  // ---- API keys ----
  apiKeyScopes: () => request("/api-keys/scopes"),
  listApiKeys: (params = {}) => request(`/api-keys${qs(params)}`),
  createApiKey: (payload) => request("/api-keys", { method: "POST", body: payload }),
  rotateApiKey: (id, graceHours = 0) =>
    request(`/api-keys/${id}/rotate`, { method: "POST", body: { grace_hours: graceHours } }),
  revokeApiKey: (id) => request(`/api-keys/${id}`, { method: "DELETE" }),

  // ---- auth (extended) ----
  updateMe: (payload) => request("/auth/me", { method: "PATCH", body: payload }),
  requestPasswordReset: (email) =>
    request("/auth/password-reset", { method: "POST", body: { email }, auth: false }),
  confirmPasswordReset: (token, newPassword) =>
    request("/auth/password-reset/confirm", {
      method: "POST",
      body: { token, new_password: newPassword },
      auth: false,
    }),

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
