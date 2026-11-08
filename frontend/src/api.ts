import { createApi, fetchBaseQuery } from "@reduxjs/toolkit/query/react";
export interface Department {
  id: number;
  department_code: string;
  department_name: string;
  is_active: boolean;
  created_at: string;
}
export interface Employee {
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
export const api = createApi({
  reducerPath: "hrmsApi",
  baseQuery: fetchBaseQuery({ baseUrl: "/api" }),
  tagTypes: ["Department", "Employee"],
  endpoints: (build) => ({
    departments: build.query<Page<Department>, { page: number; size: number }>({
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
      { id: string; body: { department_name: string } }
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
    employees: build.query<Page<Employee>, { page: number; size: number }>({
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
