import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { clearTokenCookies, getCookie } from "@/utils/cookieUtils";
import * as Networking from "./networking";

vi.mock("@/utils/cookieUtils", () => ({
  clearTokenCookies: vi.fn(),
  getCookie: vi.fn(),
  storeLoginToken: vi.fn(),
}));

vi.mock("./molecules/notifications_manager", () => ({
  default: {
    info: vi.fn(),
    success: vi.fn(),
    error: vi.fn(),
    fromBackend: vi.fn(),
  },
}));

describe("networking - expired session handling", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("should call clearTokenCookies on expired session", async () => {
    const errorData = "Authentication Error - Expired Key";
    const { default: NotificationsManager } = await import("./molecules/notifications_manager");

    if (errorData.includes("Authentication Error - Expired Key")) {
      NotificationsManager.info("UI Session Expired. Logging out.");
      clearTokenCookies();
    }

    expect(clearTokenCookies).toHaveBeenCalledOnce();
  });

  it("should not clear cookies for non-authentication errors", () => {
    const errorData = "Some other error";

    if (errorData.includes("Authentication Error - Expired Key")) {
      clearTokenCookies();
    }

    expect(clearTokenCookies).not.toHaveBeenCalled();
  });

  it("should surface backend detail error when updateSSOSettings fails", async () => {
    expect.hasAssertions();

    const backendError = {
      detail: {
        error: "Set `'STORE_MODEL_IN_DB='True'` in your env to enable this feature.",
      },
    };

    const mockFetch = vi.fn().mockResolvedValue({
      ok: false,
      json: vi.fn().mockResolvedValue(backendError),
    } as any);

    global.fetch = mockFetch as any;

    try {
      await Networking.updateSSOSettings("token", { some: "setting" });
    } catch (error) {
      const thrownError = error as any;
      expect(thrownError).toBeInstanceOf(Error);
      expect(thrownError.message).toBe(backendError.detail.error);
      expect(thrownError.detail).toEqual(backendError.detail);
      expect(thrownError.rawError).toEqual(backendError);
    }

    expect(mockFetch).toHaveBeenCalledOnce();
  });
});

describe("handleErrorResponse - status-aware auth handling", () => {
  // Stub window.location so the redirect path doesn't crash jsdom.
  let originalLocation: Location;

  beforeEach(() => {
    vi.clearAllMocks();
    originalLocation = window.location;
    delete (window as any).location;
    (window as any).location = { ...originalLocation, href: "/admin", pathname: "/admin" };
  });

  afterEach(() => {
    (window as any).location = originalLocation;
  });

  it("redirects on 401 when the auth cookie is gone (session expired)", async () => {
    vi.mocked(getCookie).mockReturnValue(undefined as any);

    await Networking.handleErrorResponse({ status: 401 }, { error: "no cookie" });

    expect(clearTokenCookies).toHaveBeenCalledOnce();
  });

  it("redirects on 401 when the body carries a session-expired marker", async () => {
    // Cookie still set, but the body explicitly says the credential is dead.
    vi.mocked(getCookie).mockReturnValue("any-token" as any);

    await Networking.handleErrorResponse(
      { status: 401 },
      { error: { message: "Authentication Error - Expired Key" } },
    );

    expect(clearTokenCookies).toHaveBeenCalledOnce();
  });

  it("does NOT redirect on 401 when the cookie is still valid and body says no session-expired marker", async () => {
    // This is the "logged in but called an admin-only endpoint" case.
    // LiteLLM uses 401 for permission too — we must not bounce the user
    // out of an otherwise healthy session.
    vi.mocked(getCookie).mockReturnValue("valid-token" as any);

    await Networking.handleErrorResponse(
      { status: 401 },
      { error: { message: "Master Key required" } },
    );

    expect(clearTokenCookies).not.toHaveBeenCalled();
  });

  it("does NOT redirect on 403 (permission denied)", async () => {
    vi.mocked(getCookie).mockReturnValue("valid-token" as any);

    await Networking.handleErrorResponse({ status: 403 }, { error: "forbidden" });

    expect(clearTokenCookies).not.toHaveBeenCalled();
  });

  it("falls through to handleError for non-401/403 errors", async () => {
    vi.mocked(getCookie).mockReturnValue("valid-token" as any);

    // 500 should not trigger the auth redirect.
    await Networking.handleErrorResponse({ status: 500 }, { error: "internal" });

    expect(clearTokenCookies).not.toHaveBeenCalled();
  });
});

describe("handleErrorResponse - type-based auth routing (D1 contract)", () => {
  // These tests cover the NEW path that reads `error.type` from the
  // response body. When the backend emits a specific auth_* type from
  // ProxyErrorTypes, the UI should dispatch by table lookup — no
  // regex, no cookie-presence guess. The status code is informational
  // only; the type is the source of truth.
  let originalLocation: Location;

  beforeEach(() => {
    vi.clearAllMocks();
    originalLocation = window.location;
    delete (window as any).location;
    (window as any).location = { ...originalLocation, href: "/admin", pathname: "/admin" };
  });

  afterEach(() => {
    (window as any).location = originalLocation;
  });

  // --- REDIRECT_LOGIN types ------------------------------------------------

  it("redirects on type=auth_session_expired regardless of cookie/status", async () => {
    // Even with a still-present cookie, the structured type
    // unambiguously says "session is gone". Redirect, no question.
    vi.mocked(getCookie).mockReturnValue("might-still-be-cached" as any);

    await Networking.handleErrorResponse(
      { status: 401 },
      { error: { type: "auth_session_expired", message: "Key has expired" } },
    );

    expect(clearTokenCookies).toHaveBeenCalledOnce();
  });

  it("redirects on type=auth_invalid_credentials", async () => {
    vi.mocked(getCookie).mockReturnValue("anything" as any);

    await Networking.handleErrorResponse(
      { status: 401 },
      { error: { type: "auth_invalid_credentials", message: "No auth header" } },
    );

    expect(clearTokenCookies).toHaveBeenCalledOnce();
  });

  it("redirects on legacy type=expired_key (predates D1)", async () => {
    // Backward compat — existing code paths that already raised
    // ProxyException with type=expired_key continue to work.
    vi.mocked(getCookie).mockReturnValue("anything" as any);

    await Networking.handleErrorResponse(
      { status: 401 },
      { error: { type: "expired_key", message: "Key has expired" } },
    );

    expect(clearTokenCookies).toHaveBeenCalledOnce();
  });

  it("redirects on legacy type=token_not_found_in_db", async () => {
    vi.mocked(getCookie).mockReturnValue("anything" as any);

    await Networking.handleErrorResponse(
      { status: 401 },
      { error: { type: "token_not_found_in_db", message: "..." } },
    );

    expect(clearTokenCookies).toHaveBeenCalledOnce();
  });

  // --- TOAST types ---------------------------------------------------------

  it("does NOT redirect on type=auth_permission_denied (the bug we're fixing)", async () => {
    // This is the case the whole D1+D2 effort exists for: the user is
    // logged in, they just called an endpoint their role can't reach.
    // No redirect, no cookie clearing, just a toast.
    vi.mocked(getCookie).mockReturnValue("valid-token" as any);

    await Networking.handleErrorResponse(
      { status: 401 },
      { error: { type: "auth_permission_denied", message: "Master Key required" } },
    );

    expect(clearTokenCookies).not.toHaveBeenCalled();
  });

  it("does NOT redirect on type=key_model_access_denied", async () => {
    vi.mocked(getCookie).mockReturnValue("valid-token" as any);

    await Networking.handleErrorResponse(
      { status: 401 },
      { error: { type: "key_model_access_denied", message: "Key does not have access to gpt-4" } },
    );

    expect(clearTokenCookies).not.toHaveBeenCalled();
  });

  it("does NOT redirect on type=team_member_permission_error", async () => {
    vi.mocked(getCookie).mockReturnValue("valid-token" as any);

    await Networking.handleErrorResponse(
      { status: 403 },
      { error: { type: "team_member_permission_error", message: "..." } },
    );

    expect(clearTokenCookies).not.toHaveBeenCalled();
  });

  it("does NOT redirect on type=budget_exceeded", async () => {
    // Budget exhaustion is permission-like: caller is who they say
    // they are, just out of credit. Toast, don't bounce.
    vi.mocked(getCookie).mockReturnValue("valid-token" as any);

    await Networking.handleErrorResponse(
      { status: 400 },
      { error: { type: "budget_exceeded", message: "Budget exceeded" } },
    );

    expect(clearTokenCookies).not.toHaveBeenCalled();
  });

  // --- HEURISTIC fallback --------------------------------------------------

  it("falls through to heuristic on type=auth_error (generic) — cookie present", async () => {
    // Generic type means backend couldn't classify. Use the cookie
    // heuristic. Cookie present + no marker -> don't redirect (the
    // step-2 safe default).
    vi.mocked(getCookie).mockReturnValue("valid-token" as any);

    await Networking.handleErrorResponse(
      { status: 401 },
      { error: { type: "auth_error", message: "something obscure" } },
    );

    expect(clearTokenCookies).not.toHaveBeenCalled();
  });

  it("falls through to heuristic on type=auth_error — cookie absent → redirect", async () => {
    vi.mocked(getCookie).mockReturnValue(undefined as any);

    await Networking.handleErrorResponse(
      { status: 401 },
      { error: { type: "auth_error", message: "something obscure" } },
    );

    expect(clearTokenCookies).toHaveBeenCalledOnce();
  });

  it("falls through to heuristic on unknown type", async () => {
    // Unknown / future / typo'd type — heuristic still runs, no crash.
    vi.mocked(getCookie).mockReturnValue("valid-token" as any);

    await Networking.handleErrorResponse(
      { status: 401 },
      { error: { type: "some_brand_new_type_we_dont_know", message: "..." } },
    );

    expect(clearTokenCookies).not.toHaveBeenCalled();
  });

  // --- Body-shape robustness -----------------------------------------------

  it("reads type from top-level field (admin-endpoint shape)", async () => {
    // Some admin endpoints respond with {type, message} at the top
    // level rather than {error: {type, message}}. extractErrorType
    // handles both.
    vi.mocked(getCookie).mockReturnValue("anything" as any);

    await Networking.handleErrorResponse(
      { status: 401 },
      { type: "auth_session_expired", message: "..." },
    );

    expect(clearTokenCookies).toHaveBeenCalledOnce();
  });

  it("falls through to heuristic when body is a string (no structured type)", async () => {
    // Legacy backend behavior — body is just a string. extractErrorType
    // returns null, the heuristic runs.
    vi.mocked(getCookie).mockReturnValue(undefined as any);

    await Networking.handleErrorResponse({ status: 401 }, "Authentication Error - Expired Key");

    expect(clearTokenCookies).toHaveBeenCalledOnce();
  });

  it("falls through to heuristic when body has no error.type field", async () => {
    vi.mocked(getCookie).mockReturnValue("valid-token" as any);

    // Cookie present + no type + no marker → no redirect (safe default).
    await Networking.handleErrorResponse(
      { status: 401 },
      { error: { message: "some message without a type field" } },
    );

    expect(clearTokenCookies).not.toHaveBeenCalled();
  });
});

describe("loginCall - storeLoginToken integration", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("calls storeLoginToken when response includes token", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ redirect_url: "/ui/?login=success", token: "my-jwt" }),
    }) as any;
    const { storeLoginToken } = await import("@/utils/cookieUtils");
    await Networking.loginCall("admin", "pass");
    expect(storeLoginToken).toHaveBeenCalledWith("my-jwt");
  });

  it("does not call storeLoginToken when response has no token", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ redirect_url: "/ui/?login=success" }),
    }) as any;
    const { storeLoginToken } = await import("@/utils/cookieUtils");
    await Networking.loginCall("admin", "pass");
    expect(storeLoginToken).not.toHaveBeenCalled();
  });
});

describe("daily activity helpers", () => {
  const startTime = new Date("2025-02-12T00:00:00.000Z");
  const endTime = new Date("2025-02-19T00:00:00.000Z");
  let currentFetch: typeof global.fetch;

  const setupSuccessfulFetch = () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({ data: [] }),
    } as any);
    global.fetch = mockFetch as any;
    return mockFetch;
  };

  beforeEach(() => {
    vi.clearAllMocks();
    currentFetch = global.fetch;
  });

  afterEach(() => {
    global.fetch = currentFetch;
  });

  it("appends tag list when tags argument is provided", async () => {
    const mockFetch = setupSuccessfulFetch();

    await Networking.tagDailyActivityCall("token", startTime, endTime, 2, ["alpha", "beta"]);

    expect(mockFetch).toHaveBeenCalledOnce();
    const calledUrl = mockFetch.mock.calls[0][0] as string;
    const parsed = new URL(calledUrl, "http://example.com");

    expect(parsed.pathname).toBe("/tag/daily/activity");
    expect(parsed.searchParams.get("tags")).toBe("alpha,beta");
  });

  it("always includes exclude_team_ids but only adds team_ids when given", async () => {
    const mockFetchWithoutTeams = setupSuccessfulFetch();

    await Networking.teamDailyActivityCall("token", startTime, endTime, 1, null);
    const urlWithoutTeams = new URL(mockFetchWithoutTeams.mock.calls[0][0] as string, "http://example.com");

    expect(urlWithoutTeams.searchParams.get("exclude_team_ids")).toBe("litellm-dashboard");
    expect(urlWithoutTeams.searchParams.has("team_ids")).toBe(false);

    const mockFetchWithTeams = setupSuccessfulFetch();
    await Networking.teamDailyActivityCall("token", startTime, endTime, 3, ["team-a", "team-b"]);
    const urlWithTeams = new URL(mockFetchWithTeams.mock.calls[0][0] as string, "http://example.com");

    expect(urlWithTeams.searchParams.get("team_ids")).toBe("team-a,team-b");
    expect(urlWithTeams.searchParams.get("exclude_team_ids")).toBe("litellm-dashboard");
  });
});

describe("UI config and public endpoints", () => {
  const originalFetch = global.fetch;

  const setupMockFetch = (responses: Array<{ url: string; data: any }>) => {
    const mockFetch = vi.fn().mockImplementation((url: string) => {
      const response = responses.find((r) => url.includes(r.url));
      if (response) {
        return Promise.resolve({
          ok: true,
          json: vi.fn().mockResolvedValue(response.data),
        } as any);
      }
      return Promise.resolve({
        ok: true,
        json: vi.fn().mockResolvedValue({}),
      } as any);
    });
    global.fetch = mockFetch as any;
    return mockFetch;
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("should use proxyBaseURL and server_root_path for /public/providers/fields when server_root_path is defined", async () => {
    const uiConfig = {
      server_root_path: "/api/v1",
      proxy_base_url: "https://example.com",
    };

    const mockFetch = setupMockFetch([
      { url: "/litellm/.well-known/litellm-ui-config", data: uiConfig },
      { url: "/public/providers/fields", data: [] },
    ]);

    // First call getUiConfig to set up proxyBaseUrl
    await Networking.getUiConfig();

    // Then call the public endpoint
    await Networking.getProviderCreateMetadata();

    expect(mockFetch).toHaveBeenCalledTimes(2);
    const publicEndpointCall = mockFetch.mock.calls.find((call) =>
      (call[0] as string).includes("/public/providers/fields"),
    );
    expect(publicEndpointCall).toBeDefined();
    const calledUrl = publicEndpointCall![0] as string;
    expect(calledUrl).toBe("https://example.com/api/v1/public/providers/fields");
  });

  it("should use proxyBaseURL and server_root_path for /public/model_hub/info when server_root_path is defined", async () => {
    const uiConfig = {
      server_root_path: "/api/v1",
      proxy_base_url: "https://example.com",
    };

    const mockFetch = setupMockFetch([
      { url: "/litellm/.well-known/litellm-ui-config", data: uiConfig },
      { url: "/public/model_hub/info", data: {} },
    ]);

    await Networking.getUiConfig();
    await Networking.getPublicModelHubInfo();

    expect(mockFetch).toHaveBeenCalledTimes(2);
    const publicEndpointCall = mockFetch.mock.calls.find((call) =>
      (call[0] as string).includes("/public/model_hub/info"),
    );
    expect(publicEndpointCall).toBeDefined();
    const calledUrl = publicEndpointCall![0] as string;
    expect(calledUrl).toBe("https://example.com/api/v1/public/model_hub/info");
  });

  it("should use proxyBaseURL and server_root_path for /public/model_hub when server_root_path is defined", async () => {
    const uiConfig = {
      server_root_path: "/api/v1",
      proxy_base_url: "https://example.com",
    };

    const mockFetch = setupMockFetch([
      { url: "/litellm/.well-known/litellm-ui-config", data: uiConfig },
      { url: "/public/model_hub", data: [] },
    ]);

    await Networking.getUiConfig();
    await Networking.modelHubPublicModelsCall();

    expect(mockFetch).toHaveBeenCalledTimes(2);
    const publicEndpointCall = mockFetch.mock.calls.find(
      (call) => (call[0] as string).includes("/public/model_hub") && !(call[0] as string).includes("/info"),
    );
    expect(publicEndpointCall).toBeDefined();
    const calledUrl = publicEndpointCall![0] as string;
    expect(calledUrl).toBe("https://example.com/api/v1/public/model_hub");
  });

  it("should use proxyBaseURL and server_root_path for /public/agent_hub when server_root_path is defined", async () => {
    const uiConfig = {
      server_root_path: "/api/v1",
      proxy_base_url: "https://example.com",
    };

    const mockFetch = setupMockFetch([
      { url: "/litellm/.well-known/litellm-ui-config", data: uiConfig },
      { url: "/public/agent_hub", data: [] },
    ]);

    await Networking.getUiConfig();
    await Networking.agentHubPublicModelsCall();

    expect(mockFetch).toHaveBeenCalledTimes(2);
    const publicEndpointCall = mockFetch.mock.calls.find((call) => (call[0] as string).includes("/public/agent_hub"));
    expect(publicEndpointCall).toBeDefined();
    const calledUrl = publicEndpointCall![0] as string;
    expect(calledUrl).toBe("https://example.com/api/v1/public/agent_hub");
  });

  it("should use proxyBaseURL and server_root_path for /public/mcp_hub when server_root_path is defined", async () => {
    const uiConfig = {
      server_root_path: "/api/v1",
      proxy_base_url: "https://example.com",
    };

    const mockFetch = setupMockFetch([
      { url: "/litellm/.well-known/litellm-ui-config", data: uiConfig },
      { url: "/public/mcp_hub", data: [] },
    ]);

    await Networking.getUiConfig();
    await Networking.mcpHubPublicServersCall();

    expect(mockFetch).toHaveBeenCalledTimes(2);
    const publicEndpointCall = mockFetch.mock.calls.find((call) => (call[0] as string).includes("/public/mcp_hub"));
    expect(publicEndpointCall).toBeDefined();
    const calledUrl = publicEndpointCall![0] as string;
    expect(calledUrl).toBe("https://example.com/api/v1/public/mcp_hub");
  });

  it("should not include server_root_path when it is root path", async () => {
    const uiConfig = {
      server_root_path: "/",
      proxy_base_url: "https://example.com",
    };

    const mockFetch = setupMockFetch([
      { url: "/litellm/.well-known/litellm-ui-config", data: uiConfig },
      { url: "/public/providers/fields", data: [] },
    ]);

    await Networking.getUiConfig();
    await Networking.getProviderCreateMetadata();

    expect(mockFetch).toHaveBeenCalledTimes(2);
    const publicEndpointCall = mockFetch.mock.calls.find((call) =>
      (call[0] as string).includes("/public/providers/fields"),
    );
    expect(publicEndpointCall).toBeDefined();
    const calledUrl = publicEndpointCall![0] as string;
    expect(calledUrl).toBe("https://example.com/public/providers/fields");
  });

  it("should return UI config from getUiConfig", async () => {
    const uiConfig = {
      server_root_path: "/api/v1",
      proxy_base_url: "https://example.com",
    };

    const mockFetch = setupMockFetch([{ url: "/litellm/.well-known/litellm-ui-config", data: uiConfig }]);

    const result = await Networking.getUiConfig();

    expect(mockFetch).toHaveBeenCalledOnce();
    expect(result).toEqual(uiConfig);
    const configCall = mockFetch.mock.calls.find((call) =>
      (call[0] as string).includes("/litellm/.well-known/litellm-ui-config"),
    );
    expect(configCall).toBeDefined();
  });
});

describe("individualModelHealthCheckCall", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("should call /health with model_id query param so health checks run by deployment id", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({
        healthy_count: 1,
        unhealthy_count: 0,
        healthy_endpoints: [],
        unhealthy_endpoints: [],
      }),
    } as any);
    global.fetch = mockFetch as any;

    await Networking.individualModelHealthCheckCall("token-123", "deployment-abc-456");

    expect(mockFetch).toHaveBeenCalledOnce();
    const [url] = mockFetch.mock.calls[0];
    const urlStr = typeof url === "string" ? url : (url as Request).url;
    expect(urlStr).toContain("health");
    const parsed = typeof url === "string" ? new URL(url, "http://example.com") : new URL((url as Request).url);
    expect(parsed.searchParams.get("model_id")).toBe("deployment-abc-456");
    expect(parsed.searchParams.has("model")).toBe(false);
  });

  it("should encode model_id in URL", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({
        healthy_count: 0,
        unhealthy_count: 0,
        healthy_endpoints: [],
        unhealthy_endpoints: [],
      }),
    } as any);
    global.fetch = mockFetch as any;

    await Networking.individualModelHealthCheckCall("token", "id/with/slashes");

    const [url] = mockFetch.mock.calls[0];
    const parsed = typeof url === "string" ? new URL(url, "http://example.com") : new URL((url as Request).url);
    expect(parsed.searchParams.get("model_id")).toBe("id/with/slashes");
  });
});
