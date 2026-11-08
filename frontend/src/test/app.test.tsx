import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Provider } from "react-redux";
import { MemoryRouter } from "react-router-dom";
import { Workspace, employeePayload } from "../App";
import { makeStore, notify, dismiss } from "../store";
import { api, errorMessage } from "../api";
const department = {
  id: 1,
  department_code: "ENG",
  department_name: "Mühendislik",
  is_active: true,
  created_at: "2026-01-01T00:00:00Z",
};
function show(path: string) {
  return render(
    <Provider store={makeStore()}>
      <MemoryRouter initialEntries={[path]}>
        <Workspace
          user={{
            id: 1,
            username: "testadmin",
            email: "test@example.invalid",
            role: "ADMIN",
            employee_id: null,
          }}
        />
      </MemoryRouter>
    </Provider>,
  );
}
function mockFetch(items: unknown[] = []) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(
    async () =>
      new Response(JSON.stringify({ items, page: 1, size: 10 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
  );
}
// fetchBaseQuery builds Request from relative browser URL; Node needs an absolute URL.
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
afterEach(() => vi.restoreAllMocks());
describe("MVP UI", () => {
  it("renders empty state and navigation", async () => {
    mockFetch();
    show("/departments");
    expect(await screen.findByText("Henüz kayıt yok")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Departman ekle/ }),
    ).toHaveAttribute("href", "/departments/new");
  });
  it("renders department and active badge", async () => {
    mockFetch([department]);
    show("/departments");
    expect(await screen.findByText("Mühendislik")).toBeInTheDocument();
    expect(
      screen.getByText("Aktif", { selector: "span.badge" }),
    ).toBeInTheDocument();
  });
  it("shows safe service errors", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error: { message: "İşlem çakışıyor." } }), {
        status: 409,
        headers: { "content-type": "application/json" },
      }),
    );
    show("/employees");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "İşlem çakışıyor.",
    );
  });
  it("does not submit a whitespace-only department code", async () => {
    const fetch = mockFetch();
    const user = userEvent.setup();
    show("/departments/new");
    await user.type(screen.getByLabelText("Departman kodu"), "   ");
    await user.type(screen.getByLabelText("Departman adı"), "Test");
    await user.click(screen.getByRole("button", { name: "Kaydet" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });
  it("posts department and returns to list with notification", async () => {
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input) => {
        const request = input as Request;
        return new Response(
          JSON.stringify(
            request.method === "POST"
              ? department
              : { items: [department], page: 1, size: 10 },
          ),
          {
            status: request.method === "POST" ? 201 : 200,
            headers: { "content-type": "application/json" },
          },
        );
      });
    const user = userEvent.setup();
    show("/departments/new");
    await user.type(screen.getByLabelText("Departman kodu"), "ENG");
    await user.type(screen.getByLabelText("Departman adı"), "Mühendislik");
    await user.click(screen.getByRole("button", { name: "Kaydet" }));
    expect(
      await screen.findByText("Departman kaydedildi.", { exact: false }),
    ).toBeInTheDocument();
    expect(
      fetch.mock.calls.some(
        ([request]) => (request as Request).method === "POST",
      ),
    ).toBe(true);
  });
  it("requires explicit deactivation confirmation", async () => {
    const fetch = mockFetch([department]);
    const user = userEvent.setup();
    show("/departments");
    await user.click(
      await screen.findByRole("button", { name: "Pasifleştir" }),
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(
      fetch.mock.calls.every(
        ([request]) => (request as Request).method === "GET",
      ),
    ).toBe(true);
    await user.click(screen.getByRole("button", { name: "Vazgeç" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it("pages with bounded size and page query", async () => {
    const fetch = mockFetch(
      Array.from({ length: 10 }, (_, i) => ({
        ...department,
        id: i + 1,
        department_name: "Dept " + i,
      })),
    );
    const user = userEvent.setup();
    show("/departments");
    await screen.findByText("Dept 0");
    await user.click(screen.getByRole("button", { name: /Sonraki/ }));
    await waitFor(() =>
      expect(
        fetch.mock.calls.some(([request]) =>
          (request as Request).url.includes("page=2"),
        ),
      ).toBe(true),
    );
  });
  it("keeps date, boolean and nullable contracts", () => {
    const body = employeePayload({
      employee_no: " E1 ",
      first_name: " Ada ",
      last_name: " L ",
      email: " ",
      hire_date: "2026-01-01",
      department_id: "1",
    });
    expect(body).toEqual({
      employee_no: "E1",
      first_name: "Ada",
      last_name: "L",
      email: null,
      hire_date: "2026-01-01",
      department_id: 1,
    });
    expect("is_active" in body).toBe(false);
  });
  it("invalidates list caches after employee writes", async () => {
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(
        async (input) =>
          new Response(
            JSON.stringify(
              (input as Request).method === "GET"
                ? { items: [], page: 1, size: 10 }
                : { id: 1 },
            ),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
      );
    const store = makeStore();
    const subscription = store.dispatch(
      api.endpoints.employees.initiate({ page: 1, size: 10 }),
    );
    await subscription;
    await store
      .dispatch(
        api.endpoints.createEmployee.initiate({
          employee_no: "E",
          first_name: "A",
          last_name: "B",
          email: null,
          hire_date: "2026-01-01",
          department_id: 1,
        }),
      )
      .unwrap();
    await waitFor(() =>
      expect(
        fetch.mock.calls.filter(
          ([request]) => (request as Request).method === "GET",
        ).length,
      ).toBe(2),
    );
    subscription.unsubscribe();
    store.dispatch(api.util.resetApiState());
  });
  it("uses stable safe error and notice contracts", () => {
    expect(errorMessage({ status: 422 })).toContain("Alanları");
    const store = makeStore();
    store.dispatch(notify("Kaydedildi"));
    expect(store.getState().ui.message).toBe("Kaydedildi");
    store.dispatch(dismiss());
    expect(store.getState().ui.message).toBe("");
  });
});
