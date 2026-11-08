import { createApi, fetchBaseQuery } from "@reduxjs/toolkit/query/react";
export interface User {
  id: number;
  username: string;
  email: string;
  role: "ADMIN" | "HR_MANAGER" | "EMPLOYEE";
  employee_id: number | null;
  is_active?: boolean;
}
export interface PrivateFile {
  id: number;
  original_name: string;
  document_type: string;
  visibility: string;
  mime_type: string;
  size_bytes: number;
  created_at: string;
}
export interface Department {
  description?: string | null;
  manager_id?: number | null;
  parent_id?: number | null;
  id: number;
  department_code: string;
  department_name: string;
  is_active: boolean;
  created_at: string;
}
export interface Employee {
  personal_email?: string | null;
  phone_number?: string | null;
  job_title?: string | null;
  manager_id?: number | null;
  employment_type?: string;
  employment_status?: string;
  work_location?: string | null;
  photo_id?: number | null;
  id: number;
  employee_no: string;
  first_name: string;
  last_name: string;
  email: string | null;
  hire_date: string;
  department_id: number;
  is_active: boolean;
  created_at: string;
}
export type DepartmentInput = Pick<
  Department,
  "department_code" | "department_name"
>;
export type EmployeeInput = Omit<Employee, "id" | "is_active" | "created_at">;
export interface Page<T> {
  items: T[];
  page: number;
  size: number;
}
export function csrfToken(): string {
  return decodeURIComponent(
    document.cookie
      .split("; ")
      .find((v) => v.startsWith("hrms_csrf="))
      ?.split("=")
      .slice(1)
      .join("=") || "",
  );
}
const rawQuery = fetchBaseQuery({
  baseUrl: "/api",
  credentials: "include",
  prepareHeaders: (headers) => {
    const csrf = csrfToken();
    if (csrf) headers.set("X-CSRF-Token", csrf);
    return headers;
  },
});
let refreshing: ReturnType<typeof rawQuery> | null = null;
const authenticatedQuery: typeof rawQuery = async (args, ctx, options) => {
  let result = await rawQuery(args, ctx, options);
  const url = typeof args === "string" ? args : args.url;
  if (
    result.error?.status === 401 &&
    !["/auth/login", "/auth/refresh", "/auth/logout"].includes(url)
  ) {
    refreshing ||= rawQuery(
      { url: "/auth/refresh", method: "POST" },
      ctx,
      options,
    );
    const refreshed = await refreshing;
    refreshing = null;
    if (!refreshed.error) result = await rawQuery(args, ctx, options);
  }
  return result;
};
export const api = createApi({
  reducerPath: "hrmsApi",
  baseQuery: authenticatedQuery,
  tagTypes: ["Department", "Employee", "Session", "User", "File", "Audit"],
  endpoints: (build) => ({
    me: build.query<User, void>({
      query: () => "/auth/me",
      providesTags: ["Session"],
    }),
    login: build.mutation<User, { login: string; password: string }>({
      query: (body) => ({ url: "/auth/login", method: "POST", body }),
      invalidatesTags: ["Session"],
    }),
    logout: build.mutation<void, void>({
      query: () => ({ url: "/auth/logout", method: "POST" }),
    }),
    password: build.mutation<
      void,
      { current_password: string; new_password: string }
    >({ query: (body) => ({ url: "/auth/password", method: "POST", body }) }),
    users: build.query<User[], number>({
      query: (page) => ({ url: "/users", params: { page, size: 20 } }),
      providesTags: ["User"],
    }),
    createUser: build.mutation<User, Record<string, unknown>>({
      query: (body) => ({ url: "/users", method: "POST", body }),
      invalidatesTags: ["User"],
    }),
    updateUser: build.mutation<
      User,
      { id: number; body: Record<string, unknown> }
    >({
      query: ({ id, body }) => ({ url: `/users/${id}`, method: "PATCH", body }),
      invalidatesTags: ["User", "Session"],
    }),
    audit: build.query<Record<string, unknown>[], number>({
      query: (page) => ({ url: "/audit", params: { page, size: 20 } }),
      providesTags: ["Audit"],
    }),
    hierarchy: build.query<{ items: Department[]; truncated: boolean }, void>({
      query: () => "/department-hierarchy",
      providesTags: ["Department"],
    }),
    documents: build.query<
      { items: PrivateFile[]; page: number; size: number },
      { id: number; page: number }
    >({
      query: ({ id, page }) => ({
        url: `/employees/${id}/documents`,
        params: { page, size: 20 },
      }),
      providesTags: ["File"],
    }),
    upload: build.mutation<
      PrivateFile,
      { id: number; photo: boolean; body: FormData }
    >({
      query: ({ id, photo, body }) => ({
        url: `/employees/${id}/${photo ? "photo" : "documents"}`,
        method: "POST",
        body,
      }),
      invalidatesTags: ["File", "Employee"],
    }),
    archive: build.mutation<void, { id: number; file: number }>({
      query: ({ id, file }) => ({
        url: `/employees/${id}/documents/${file}/archive`,
        method: "POST",
      }),
      invalidatesTags: ["File"],
    }),
    departments: build.query<
      Page<Department>,
      {
        page: number;
        size: number;
        q?: string;
        department_id?: number;
        employment_status?: string;
        is_active?: boolean;
      }
    >({
      query: (params) => ({ url: "/departments", params }),
      providesTags: ["Department"],
    }),
    department: build.query<Department, string>({
      query: (id) => `/departments/${id}`,
      providesTags: ["Department"],
    }),
    createDepartment: build.mutation<Department, DepartmentInput>({
      query: (body) => ({ url: "/departments", method: "POST", body }),
      invalidatesTags: ["Department"],
    }),
    updateDepartment: build.mutation<
      Department,
      { id: string; body: Partial<Department> }
    >({
      query: ({ id, body }) => ({
        url: `/departments/${id}`,
        method: "PATCH",
        body,
      }),
      invalidatesTags: ["Department"],
    }),
    deactivateDepartment: build.mutation<Department, number>({
      query: (id) => ({ url: `/departments/${id}/deactivate`, method: "POST" }),
      invalidatesTags: ["Department"],
    }),
    employees: build.query<
      Page<Employee>,
      {
        page: number;
        size: number;
        q?: string;
        department_id?: number;
        status?: string;
        is_active?: boolean;
      }
    >({
      query: (params) => ({ url: "/employees", params }),
      providesTags: ["Employee"],
    }),
    employee: build.query<Employee, string>({
      query: (id) => `/employees/${id}`,
      providesTags: ["Employee"],
    }),
    createEmployee: build.mutation<Employee, EmployeeInput>({
      query: (body) => ({ url: "/employees", method: "POST", body }),
      invalidatesTags: ["Employee", "Department"],
    }),
    updateEmployee: build.mutation<
      Employee,
      { id: string; body: Partial<Omit<EmployeeInput, "employee_no">> }
    >({
      query: ({ id, body }) => ({
        url: `/employees/${id}`,
        method: "PATCH",
        body,
      }),
      invalidatesTags: ["Employee", "Department"],
    }),
    deactivateEmployee: build.mutation<Employee, number>({
      query: (id) => ({ url: `/employees/${id}/deactivate`, method: "POST" }),
      invalidatesTags: ["Employee", "Department"],
    }),
  }),
});
export const {
  useMeQuery,
  useLoginMutation,
  useLogoutMutation,
  usePasswordMutation,
  useUsersQuery,
  useCreateUserMutation,
  useUpdateUserMutation,
  useAuditQuery,
  useHierarchyQuery,
  useDocumentsQuery,
  useUploadMutation,
  useArchiveMutation,
  useDepartmentsQuery,
  useDepartmentQuery,
  useCreateDepartmentMutation,
  useUpdateDepartmentMutation,
  useDeactivateDepartmentMutation,
  useEmployeesQuery,
  useEmployeeQuery,
  useCreateEmployeeMutation,
  useUpdateEmployeeMutation,
  useDeactivateEmployeeMutation,
} = api;
export function errorMessage(error: unknown): string {
  const value = error as {
    status?: number | string;
    data?: { error?: { message?: string }; detail?: unknown };
  };
  if (value?.data?.error?.message) return value.data.error.message;
  if (value?.status === 422)
    return "Alanları kontrol edin. Gönderilen değerler kabul edilmedi.";
  if (value?.status === 404) return "Kayıt bulunamadı.";
  return "Servise ulaşılamadı. Yazma işlemi yaptıysanız sonucu kontrol etmeden tekrar göndermeyin.";
}
