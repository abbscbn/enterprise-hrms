import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Provider } from "react-redux";
import { MemoryRouter } from "react-router-dom";
import App, { Workspace } from "../App";
import { makeStore } from "../store";
import { api, csrfToken, type User } from "../api";
const RealRequest = globalThis.Request;
vi.stubGlobal(
  "Request",
  class extends RealRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(
        typeof input === "string" && input.startsWith("/")
          ? "http://localhost" + input
          : input,
        init,
      );
    }
  },
);
const admin: User = {
  id: 1,
  username: "fixture",
  email: "test@example.invalid",
  role: "ADMIN",
  employee_id: 10,
};
function show(path = "/", user?: User) {
  return render(
    <Provider store={makeStore()}>
      <MemoryRouter initialEntries={[path]}>
        {user ? <Workspace user={user} /> : <App />}
      </MemoryRouter>
    </Provider>,
  );
}
function response(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}
afterEach(() => {
  vi.restoreAllMocks();
  document.cookie = "hrms_csrf=;max-age=0";
});
describe("Enterprise authentication and access", () => {
  it("requires login when session and refresh are unauthorized", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      response({}, 401),
    );
    show();
    expect(
      await screen.findByRole("button", { name: "Giriş yap" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Personel rehberi")).not.toBeInTheDocument();
  });
  it("submits credentials without local storage token persistence", async () => {
    const calls: Request[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const r = input as Request;
      calls.push(r);
      return r.url.endsWith("/auth/login")
        ? response({}, 401)
        : response({}, 401);
    });
    show();
    await userEvent.type(
      await screen.findByLabelText("Kullanıcı adı veya e-posta"),
      "fixture",
    );
    await userEvent.type(screen.getByLabelText("Parola"), "Synthetic-pass-42");
    await userEvent.click(screen.getByRole("button", { name: "Giriş yap" }));
    await waitFor(() =>
      expect(calls.some((r) => r.url.endsWith("/auth/login"))).toBe(true),
    );
    expect(localStorage.length).toBe(0);
    expect(screen.getByLabelText("Parola")).toHaveValue("");
  });
  it("shows service error instead of misleading login on server failure", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      response({}, 503),
    );
    show();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Giriş yap" }),
    ).not.toBeInTheDocument();
  });
  it("employee cannot navigate to directory or admin modules", async () => {
    show("/users", { ...admin, role: "EMPLOYEE" });
    expect(screen.getByRole("alert")).toHaveTextContent("Erişim yetkiniz yok");
    expect(
      screen.queryByRole("link", { name: "Kullanıcılar ve roller" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /Personel$/ }),
    ).not.toBeInTheDocument();
  });
  it("HR cannot view audit screen", () => {
    show("/audit", { ...admin, role: "HR_MANAGER" });
    expect(screen.getByRole("alert")).toHaveTextContent("Erişim yetkiniz yok");
  });
  it("admin has users and audit navigation", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => response([]));
    show("/users", admin);
    expect(
      screen.getByRole("link", { name: "Denetim günlüğü" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Yeni kullanıcı" }),
    ).toBeInTheDocument();
  });
  it("employee profile shows contact fields and no HR editor", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      (input as Request).url.includes("/documents")
        ? response({ items: [], page: 1, size: 20 })
        : response({
            id: 10,
            employee_no: "T1",
            first_name: "Test",
            last_name: "Employee",
            email: null,
            hire_date: "2026-01-01",
            department_id: 1,
            is_active: true,
            employment_type: "FULL_TIME",
            employment_status: "ACTIVE",
          }),
    );
    show("/employees/10", { ...admin, role: "EMPLOYEE" });
    expect(await screen.findByLabelText("Kişisel e-posta")).toBeInTheDocument();
    expect(screen.queryByLabelText("Unvan")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Belgeyi yükle" }),
    ).not.toBeInTheDocument();
    expect(
      await screen.findByText("Görüntülenebilir belge bulunmuyor."),
    ).toBeInTheDocument();
  });
  it("document upload uses multipart and csrf with private cookies", async () => {
    document.cookie = "hrms_csrf=synthetic-csrf";
    const calls: Request[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const request = input as Request;
      calls.push(request);
      return response({ id: 1 });
    });
    const store = makeStore();
    const body = new FormData();
    body.set(
      "file",
      new Blob(["%PDF-test"], { type: "application/pdf" }),
      "cv.pdf",
    );
    await store.dispatch(
      api.endpoints.upload.initiate({ id: 10, photo: false, body }),
    );
    expect(calls[0].method).toBe("POST");
    expect(calls[0].headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
    expect(calls[0].credentials).toBe("include");
    expect(calls[0].headers.get("content-type")).toContain(
      "multipart/form-data",
    );
  });
  it("does not retry uncertain writes or server errors", async () => {
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async () => response({}, 500));
    const store = makeStore();
    await store.dispatch(
      api.endpoints.updateUser.initiate({
        id: 1,
        body: { role: "HR_MANAGER" },
      }),
    );
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("refreshes exactly once on 401 before retrying an authorized query", async () => {
    let meCalls = 0;
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input) => {
        const url = (input as Request).url;
        if (url.endsWith("/auth/refresh")) return response(admin);
        meCalls++;
        return meCalls === 1 ? response({}, 401) : response(admin);
      });
    const store = makeStore();
    const result = await store.dispatch(api.endpoints.me.initiate());
    expect(result.data?.role).toBe("ADMIN");
    expect(fetch).toHaveBeenCalledTimes(3);
  });
  it("csrf reader does not use access or refresh values", () => {
    document.cookie = "hrms_csrf=only-csrf";
    expect(csrfToken()).toBe("only-csrf");
  });
  it("sends employee status filter using the backend query contract", async () => {
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async () =>
        response({ items: [], page: 1, size: 20 }),
      );
    const store = makeStore();
    await store.dispatch(
      api.endpoints.employees.initiate({
        page: 1,
        size: 20,
        status: "ON_LEAVE",
        q: "fixture",
      }),
    );
    const url = new URL((fetch.mock.calls[0][0] as Request).url);
    expect(url.searchParams.get("status")).toBe("ON_LEAVE");
    expect(url.searchParams.get("q")).toBe("fixture");
  });
});
