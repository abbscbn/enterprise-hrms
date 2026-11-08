import {
  Account,
  Audit,
  Forbidden,
  Hierarchy,
  Login,
  Logout,
  Profile,
  Users,
} from "./Enterprise";
import { useMeQuery, type User } from "./api";
import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import {
  Link,
  NavLink,
  Route,
  Routes,
  useNavigate,
  useParams,
} from "react-router-dom";
import { useDispatch, useSelector } from "react-redux";
import { dismiss, notify, type RootState } from "./store";
import {
  type EmployeeInput,
  type Employee,
  type Department,
  errorMessage,
  useDepartmentsQuery,
  useEmployeesQuery,
  useEmployeeQuery,
  useDepartmentQuery,
  useCreateDepartmentMutation,
  useUpdateDepartmentMutation,
  useCreateEmployeeMutation,
  useUpdateEmployeeMutation,
  useDeactivateDepartmentMutation,
  useDeactivateEmployeeMutation,
} from "./api";

const date = (value: string) =>
  new Intl.DateTimeFormat("tr-TR", { dateStyle: "medium" }).format(
    new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? value + "T12:00:00" : value),
  );
function Badge({ active }: { active: boolean }) {
  return (
    <span className={`badge ${active ? "active" : "inactive"}`}>
      <i />
      {active ? "Aktif" : "Pasif"}
    </span>
  );
}
function Failure({ error }: { error: unknown }) {
  return (
    <div className="error" role="alert">
      {errorMessage(error)}
    </div>
  );
}
function Heading({
  title,
  subtitle,
  action,
}: {
  title: string;
  subtitle: string;
  action?: ReactNode;
}) {
  return (
    <header className="page-heading">
      <div>
        <p className="eyebrow">İNSAN KAYNAKLARI</p>
        <h1>{title}</h1>
        <p className="muted">{subtitle}</p>
      </div>
      {action}
    </header>
  );
}
function Dashboard() {
  const departments = useDepartmentsQuery({ page: 1, size: 100 });
  const employees = useEmployeesQuery({ page: 1, size: 100 });
  const active =
    employees.data?.items.filter((item) => item.is_active).length ?? 0;
  return (
    <>
      <Heading
        title="İnsan ve organizasyon"
        subtitle="Ekibinizin bugününe net bir bakış."
        action={
          <Link className="button" to="/employees/new">
            + Personel ekle
          </Link>
        }
      />
      <section className="hero">
        <div>
          <span className="hero-tag">ENTERPRISE HRMS · MVP v1</span>
          <h2>
            Güçlü ekipler,
            <br />
            düzenli başlangıçlar.
          </h2>
          <p>
            Departmanları ve çalışanları tek bir yerden yönetin.
            <br />
            İnsan odaklı, sade ve birbiriyle bağlantılı.
          </p>
          <Link to="/departments" className="light-button">
            Organizasyonu keşfet ↗
          </Link>
        </div>
        <div className="hero-art" aria-hidden="true">
          <span>HR</span>
          <i />
          <i />
          <i />
        </div>
      </section>
      {(departments.error || employees.error) && (
        <Failure error={departments.error || employees.error} />
      )}
      <div className="metrics">
        <article>
          <span>Departman</span>
          <strong>
            {departments.isLoading
              ? "—"
              : (departments.data?.items.length ?? "—")}
          </strong>
          <small>Organizasyon birimleri</small>
        </article>
        <article>
          <span>Personel</span>
          <strong>
            {employees.isLoading ? "—" : (employees.data?.items.length ?? "—")}
          </strong>
          <small>Personel kayıtları</small>
        </article>
        <article>
          <span>Aktif personel</span>
          <strong>{employees.isLoading ? "—" : active}</strong>
          <small>Görüntülenen kayıtlarda</small>
        </article>
      </div>
      <p className="muted small">
        Özet ilk 100 kaydı kapsar. Tüm kayıtlar için listeleri kullanın.
      </p>
      <div className="section-title">
        <h2>Ekibe son katılanlar</h2>
        <Link to="/employees">Tüm personel →</Link>
      </div>
      <div className="panel">
        {employees.isLoading ? (
          <p role="status">Kayıtlar yükleniyor…</p>
        ) : employees.data?.items.length ? (
          <div className="people">
            {[...employees.data.items]
              .sort((a, b) => b.id - a.id)
              .slice(0, 5)
              .map((e) => (
                <Link key={e.id} to={`/employees/${e.id}`} className="person">
                  <span className="avatar">
                    {e.first_name[0]}
                    {e.last_name[0]}
                  </span>
                  <span>
                    <b>
                      {e.first_name} {e.last_name}
                    </b>
                    <small>
                      {e.employee_no} · {date(e.hire_date)}
                    </small>
                  </span>
                  <Badge active={e.is_active} />
                </Link>
              ))}
          </div>
        ) : (
          <p>Henüz personel kaydı yok. İlk çalışanınızı ekleyerek başlayın.</p>
        )}
      </div>
    </>
  );
}
function Pagination({
  page,
  setPage,
  count,
  busy,
}: {
  page: number;
  setPage: (value: number) => void;
  count: number;
  busy: boolean;
}) {
  return (
    <div className="pagination">
      <span>
        Sayfa {page} · {count} kayıt
      </span>
      <div>
        <button
          className="secondary"
          disabled={page === 1 || busy}
          onClick={() => setPage(page - 1)}
        >
          ← Önceki
        </button>
        <button
          className="secondary"
          disabled={count < 10 || busy}
          onClick={() => setPage(page + 1)}
        >
          Sonraki →
        </button>
      </div>
    </div>
  );
}
function ResourceList({ kind }: { kind: "departments" | "employees" }) {
  const employee = kind === "employees";
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("");
  const [pending, setPending] = useState<Department | Employee | null>(null);
  const [operationError, setOperationError] = useState<unknown>(null);
  const departments = useDepartmentsQuery(
    {
      page,
      size: 10,
      q: search || undefined,
      is_active: filter ? filter === "ACTIVE" : undefined,
    },
    { skip: employee },
  );
  const employees = useEmployeesQuery(
    {
      page,
      size: 10,
      q: search || undefined,
      status: filter || undefined,
    },
    { skip: !employee },
  );
  const query = employee ? employees : departments;
  const [deactivateDepartment, ds] = useDeactivateDepartmentMutation();
  const [deactivateEmployee, es] = useDeactivateEmployeeMutation();
  const dispatch = useDispatch();
  const rows = query.currentData?.items ?? [];
  const busy = ds.isLoading || es.isLoading;
  async function deactivate() {
    if (!pending) return;
    setOperationError(null);
    try {
      if (employee) await deactivateEmployee(pending.id).unwrap();
      else await deactivateDepartment(pending.id).unwrap();
      setPending(null);
      dispatch(notify("Kayıt pasifleştirildi."));
    } catch (error) {
      setOperationError(error);
    }
  }
  return (
    <>
      <Heading
        title={employee ? "Personel" : "Departmanlar"}
        subtitle={
          employee
            ? "Çalışan bilgilerini güncel tutun, ekibinizi birlikte yönetin."
            : "Organizasyonunuzun yapı taşlarını yönetin."
        }
        action={
          <Link className="button" to={`/${kind}/new`}>
            + {employee ? "Personel" : "Departman"} ekle
          </Link>
        }
      />
      <div className="enterprise-card">
        <label>
          Ara
          <input
            aria-label="Rehberde ara"
            value={search}
            maxLength={100}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
          />
        </label>
        <label>
          Durum filtresi
          <select
            value={filter}
            onChange={(e) => {
              setFilter(e.target.value);
              setPage(1);
            }}
          >
            <option value="">Tümü</option>
            <option value="ACTIVE">Aktif</option>
            {employee ? (
              <>
                <option value="ON_LEAVE">İzinli</option>
                <option value="TERMINATED">Ayrılmış</option>
              </>
            ) : (
              <option value="INACTIVE">Pasif</option>
            )}
          </select>
        </label>
      </div>
      <div className="panel table-panel">
        <div className="table-top">
          <h2>{employee ? "Personel rehberi" : "Departman rehberi"}</h2>
          <span className="muted">10 kayıt / sayfa</span>
        </div>
        {query.error ? (
          <Failure error={query.error} />
        ) : query.isFetching ? (
          <p role="status" className="empty">
            Kayıtlar yükleniyor…
          </p>
        ) : !rows.length ? (
          <div className="empty">
            <span className="empty-icon">◇</span>
            <h3>Henüz kayıt yok</h3>
            <p>
              Yeni bir {employee ? "personel" : "departman"} ekleyebilir veya
              önceki sayfaya dönebilirsiniz.
            </p>
          </div>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>{employee ? "Çalışan" : "Departman"}</th>
                  <th>{employee ? "Personel no" : "Kod"}</th>
                  <th>{employee ? "İşe giriş" : "Oluşturulma"}</th>
                  <th>Durum</th>
                  <th>
                    <span className="sr-only">İşlemler</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id}>
                    <td>
                      {employee ? (
                        <Link className="name" to={`/employees/${row.id}`}>
                          {(row as Employee).first_name}{" "}
                          {(row as Employee).last_name}
                        </Link>
                      ) : (
                        <b>{(row as Department).department_name}</b>
                      )}
                    </td>
                    <td>
                      <code>
                        {employee
                          ? (row as Employee).employee_no
                          : (row as Department).department_code}
                      </code>
                    </td>
                    <td>
                      {date(
                        employee ? (row as Employee).hire_date : row.created_at,
                      )}
                    </td>
                    <td>
                      <Badge active={row.is_active} />
                    </td>
                    <td className="actions">
                      <Link to={`/${kind}/${row.id}/edit`}>Düzenle</Link>
                      {row.is_active && (
                        <button
                          className="text-button danger"
                          onClick={() => {
                            setPending(row);
                            setOperationError(null);
                          }}
                        >
                          Pasifleştir
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <Pagination
          page={page}
          setPage={setPage}
          count={rows.length}
          busy={query.isFetching}
        />
      </div>
      {pending && (
        <div className="modal-backdrop">
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="confirm-title"
          >
            <h2 id="confirm-title">Kaydı pasifleştir?</h2>
            <p>
              Kayıt silinmez. Aktif personeli olan departman pasifleştirilemez.
            </p>
            {operationError !== null && <Failure error={operationError} />}
            <div className="form-actions">
              <button
                className="secondary"
                disabled={busy}
                onClick={() => setPending(null)}
              >
                Vazgeç
              </button>
              <button className="button" disabled={busy} onClick={deactivate}>
                {busy ? "Kaydediliyor…" : "Pasifleştir"}
              </button>
            </div>
          </section>
        </div>
      )}
    </>
  );
}
function DepartmentForm() {
  const { id } = useParams();
  const editing = !!id;
  const query = useDepartmentQuery(id ?? "", { skip: !editing });
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [parent, setParent] = useState("");
  const [manager, setManager] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [create, cs] = useCreateDepartmentMutation();
  const [update, us] = useUpdateDepartmentMutation();
  const navigate = useNavigate();
  const dispatch = useDispatch();
  useEffect(() => {
    if (query.data) {
      setCode(query.data.department_code);
      setName(query.data.department_name);
      setDescription(query.data.description ?? "");
      setParent(String(query.data.parent_id ?? ""));
      setManager(String(query.data.manager_id ?? ""));
    }
  }, [query.data]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (!name.trim() || (!editing && !code.trim())) {
      setError({ status: 422 });
      return;
    }
    try {
      if (id)
        await update({
          id,
          body: {
            department_name: name.trim(),
            description: description || null,
            parent_id: parent ? Number(parent) : null,
            manager_id: manager ? Number(manager) : null,
          },
        }).unwrap();
      else
        await create({
          department_code: code.trim(),
          department_name: name.trim(),
          ...{
            description: description || null,
            parent_id: parent ? Number(parent) : null,
            manager_id: manager ? Number(manager) : null,
          },
        }).unwrap();
      dispatch(notify("Departman kaydedildi."));
      navigate("/departments");
    } catch (e) {
      setError(e);
    }
  }
  return (
    <>
      <Heading
        title={editing ? "Departmanı düzenle" : "Yeni departman"}
        subtitle="Organizasyonunuz için bir çalışma alanı tanımlayın."
      />
      {query.error ? (
        <Failure error={query.error} />
      ) : query.isLoading ? (
        <p role="status">Yükleniyor…</p>
      ) : (
        <form className="panel form-panel" onSubmit={submit}>
          <div className="form-section">
            <h2>Departman bilgileri</h2>
            <p className="muted">Kod oluşturulduktan sonra değiştirilemez.</p>
          </div>
          <label>
            Departman kodu
            <input
              required
              maxLength={30}
              value={code}
              disabled={editing}
              onChange={(e) => setCode(e.target.value)}
              placeholder="Örn. ENGINEERING"
            />
          </label>
          <label>
            Departman adı
            <input
              required
              maxLength={150}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Örn. Mühendislik"
            />
          </label>
          <label>
            Açıklama
            <textarea
              maxLength={1000}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          <label>
            Üst departman ID
            <input
              type="number"
              min={1}
              value={parent}
              onChange={(e) => setParent(e.target.value)}
            />
          </label>
          <label>
            Yönetici personel ID
            <input
              type="number"
              min={1}
              value={manager}
              onChange={(e) => setManager(e.target.value)}
            />
          </label>
          {error !== null && <Failure error={error} />}
          <div className="form-actions">
            <Link className="secondary" to="/departments">
              Vazgeç
            </Link>
            <button className="button" disabled={cs.isLoading || us.isLoading}>
              Kaydet
            </button>
          </div>
        </form>
      )}
    </>
  );
}
export function employeePayload(form: Record<string, string>): EmployeeInput {
  return {
    employee_no: form.employee_no.trim(),
    first_name: form.first_name.trim(),
    last_name: form.last_name.trim(),
    email: form.email.trim() || null,
    hire_date: form.hire_date,
    department_id: Number(form.department_id),
  };
}
function EmployeeForm() {
  const { id } = useParams();
  const editing = !!id;
  const employee = useEmployeeQuery(id ?? "", { skip: !editing });
  const departments = useDepartmentsQuery({ page: 1, size: 100 });
  const [form, setForm] = useState<Record<string, string>>({
    employee_no: "",
    first_name: "",
    last_name: "",
    email: "",
    hire_date: "",
    department_id: "",
  });
  const [error, setError] = useState<unknown>(null);
  const [create, cs] = useCreateEmployeeMutation();
  const [update, us] = useUpdateEmployeeMutation();
  const navigate = useNavigate();
  const dispatch = useDispatch();
  useEffect(() => {
    if (employee.data) {
      const e = employee.data;
      setForm({
        employee_no: e.employee_no,
        first_name: e.first_name,
        last_name: e.last_name,
        email: e.email ?? "",
        hire_date: e.hire_date,
        department_id: String(e.department_id),
      });
    }
  }, [employee.data]);
  const set = (key: string, value: string) =>
    setForm((previous) => ({ ...previous, [key]: value }));
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    const body = employeePayload(form);
    if (
      !body.first_name ||
      !body.last_name ||
      !body.employee_no ||
      !body.hire_date ||
      !body.department_id
    ) {
      setError({ status: 422 });
      return;
    }
    try {
      if (id) {
        const { employee_no, ...changes } = body;
        void employee_no;
        if (body.department_id === employee.data?.department_id)
          delete (changes as Partial<EmployeeInput>).department_id;
        await update({ id, body: changes }).unwrap();
      } else await create(body).unwrap();
      dispatch(notify("Personel kaydedildi."));
      navigate("/employees");
    } catch (e) {
      setError(e);
    }
  }
  const options = departments.data?.items.filter((d) => d.is_active) ?? [];
  return (
    <>
      <Heading
        title={editing ? "Personeli düzenle" : "Yeni personel"}
        subtitle="Çalışanınızın kurumdaki yolculuğunu başlatın."
      />
      {employee.error || departments.error ? (
        <Failure error={employee.error || departments.error} />
      ) : employee.isLoading || departments.isLoading ? (
        <p role="status">Yükleniyor…</p>
      ) : (
        <form className="panel form-panel" onSubmit={submit}>
          <div className="form-section">
            <h2>Kişisel ve kurumsal bilgiler</h2>
            <p className="muted">
              * İşaretli alanlar zorunludur. Personel numarası kalıcıdır.
            </p>
          </div>
          <div className="form-grid">
            <label>
              Personel no *
              <input
                required
                disabled={editing}
                maxLength={30}
                value={form.employee_no}
                onChange={(e) => set("employee_no", e.target.value)}
              />
            </label>
            <label>
              Departman *
              <select
                required
                value={form.department_id}
                onChange={(e) => set("department_id", e.target.value)}
              >
                <option value="">Departman seçin</option>
                {editing &&
                  !options.some((d) => String(d.id) === form.department_id) && (
                    <option value={form.department_id}>
                      Mevcut departman #{form.department_id}
                    </option>
                  )}
                {options.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.department_name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Ad *
              <input
                required
                maxLength={100}
                value={form.first_name}
                onChange={(e) => set("first_name", e.target.value)}
              />
            </label>
            <label>
              Soyad *
              <input
                required
                maxLength={100}
                value={form.last_name}
                onChange={(e) => set("last_name", e.target.value)}
              />
            </label>
            <label>
              E-posta
              <input
                type="email"
                maxLength={254}
                value={form.email}
                onChange={(e) => set("email", e.target.value)}
                placeholder="isteğe bağlı"
              />
            </label>
            <label>
              İşe giriş tarihi *
              <input
                required
                type="date"
                value={form.hire_date}
                onChange={(e) => set("hire_date", e.target.value)}
              />
            </label>
          </div>
          <p className="muted small">
            Seçim listesi ilk 100 departmandaki aktif kayıtları gösterir.
          </p>
          {error !== null && <Failure error={error} />}
          <div className="form-actions">
            <Link className="secondary" to="/employees">
              Vazgeç
            </Link>
            <button className="button" disabled={cs.isLoading || us.isLoading}>
              Kaydet
            </button>
          </div>
        </form>
      )}
    </>
  );
}
function EmployeeDetail() {
  const { id = "" } = useParams();
  const query = useEmployeeQuery(id);
  const e = query.data;
  return (
    <>
      <Heading
        title={e ? `${e.first_name} ${e.last_name}` : "Personel detayı"}
        subtitle="Personel kartı ve kurumsal bilgiler."
        action={
          e && (
            <Link className="button" to={`/employees/${id}/edit`}>
              Düzenle
            </Link>
          )
        }
      />
      {query.error ? (
        <Failure error={query.error} />
      ) : !e ? (
        <p role="status">Yükleniyor…</p>
      ) : (
        <div className="panel detail">
          <span className="avatar large">
            {e.first_name[0]}
            {e.last_name[0]}
          </span>
          <Badge active={e.is_active} />
          <dl>
            <dt>Personel no</dt>
            <dd>{e.employee_no}</dd>
            <dt>E-posta</dt>
            <dd>{e.email ?? "Belirtilmedi"}</dd>
            <dt>Departman</dt>
            <dd>#{e.department_id}</dd>
            <dt>İşe giriş</dt>
            <dd>{date(e.hire_date)}</dd>
            <dt>Kayıt zamanı</dt>
            <dd>{date(e.created_at)}</dd>
          </dl>
          <Link to="/employees">← Personel listesi</Link>
        </div>
      )}
    </>
  );
}
export function Workspace({ user }: { user: User }) {
  const message = useSelector((state: RootState) => state.ui.message);
  const dispatch = useDispatch();
  useEffect(() => {
    if (message) {
      const timer = setTimeout(() => dispatch(dismiss()), 5000);
      return () => clearTimeout(timer);
    }
  }, [message, dispatch]);
  return (
    <div className="app-shell">
      <aside>
        <Link className="brand" to="/">
          <span>H</span>
          <div>
            enterprise<b>HRMS</b>
          </div>
        </Link>
        <p className="nav-label">ÇALIŞMA ALANI</p>
        <nav>
          {user.role !== "EMPLOYEE" && (
            <>
              <NavLink to="/" end>
                <span>◈</span> Genel bakış
              </NavLink>
              <NavLink to="/departments">
                <span>▦</span> Departmanlar
              </NavLink>
              <NavLink to="/employees">
                <span>♧</span> Personel
              </NavLink>
            </>
          )}
          <NavLink to="/account">Hesabım</NavLink>
          <NavLink to="/organization">Organizasyon</NavLink>
          {user.role !== "EMPLOYEE" && (
            <NavLink to="/users">Kullanıcılar ve roller</NavLink>
          )}
          {user.role === "ADMIN" && (
            <NavLink to="/audit">Denetim günlüğü</NavLink>
          )}
        </nav>
        <div className="sidebar-note">
          <i /> Geliştirme ortamı<small>Enterprise HRMS · MVP v2</small>
        </div>
      </aside>
      <div className="workspace">
        <div className="topbar">
          <span>Organizasyon yönetimi</span>
          <div>
            <span className="workspace-badge">{user.role}</span> {user.username}{" "}
            <Logout />
          </div>
        </div>
        <main>
          <Routes>
            <Route path="/account" element={<Account user={user} />} />
            <Route path="/organization" element={<Hierarchy />} />
            <Route
              path="/users"
              element={
                user.role !== "EMPLOYEE" ? <Users user={user} /> : <Forbidden />
              }
            />
            <Route
              path="/audit"
              element={user.role === "ADMIN" ? <Audit /> : <Forbidden />}
            />
            <Route
              path="/"
              element={
                user.role !== "EMPLOYEE" ? (
                  <Dashboard />
                ) : (
                  <Account user={user} />
                )
              }
            />
            <Route
              path="/departments"
              element={
                user.role !== "EMPLOYEE" ? (
                  <ResourceList key="departments" kind="departments" />
                ) : (
                  <Forbidden />
                )
              }
            />
            <Route
              path="/departments/new"
              element={
                user.role !== "EMPLOYEE" ? <DepartmentForm /> : <Forbidden />
              }
            />
            <Route
              path="/departments/:id/edit"
              element={
                user.role !== "EMPLOYEE" ? <DepartmentForm /> : <Forbidden />
              }
            />
            <Route
              path="/employees"
              element={
                user.role !== "EMPLOYEE" ? (
                  <ResourceList key="employees" kind="employees" />
                ) : (
                  <Forbidden />
                )
              }
            />
            <Route
              path="/employees/new"
              element={
                user.role !== "EMPLOYEE" ? <EmployeeForm /> : <Forbidden />
              }
            />
            <Route
              path="/employees/:id/edit"
              element={
                user.role !== "EMPLOYEE" ? <EmployeeForm /> : <Forbidden />
              }
            />
            <Route path="/employees/:id" element={<Profile user={user} />} />
            <Route
              path="*"
              element={
                <>
                  <h1>Sayfa bulunamadı</h1>
                  <Link to="/">Genel bakışa dön</Link>
                </>
              }
            />
          </Routes>
        </main>
        <footer>
          Enterprise HRMS <span>İnsan odaklı organizasyon yönetimi.</span>
        </footer>
      </div>
      {message && (
        <div role="status" className="toast">
          ✓ {message}
          <button
            onClick={() => dispatch(dismiss())}
            aria-label="Bildirimi kapat"
          >
            ×
          </button>
        </div>
      )}
    </div>
  );
}

export default function App() {
  const { data, error, isLoading } = useMeQuery();
  if (isLoading)
    return (
      <main className="login-page" role="status">
        Oturum doğrulanıyor…
      </main>
    );
  if (data) return <Workspace user={data} />;
  if (error && "status" in error && error.status !== 401)
    return (
      <main className="login-page">
        <p role="alert">{errorMessage(error)}</p>
      </main>
    );
  return <Login />;
}
