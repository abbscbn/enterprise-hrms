import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { useDispatch } from "react-redux";
import {
  api,
  errorMessage,
  useLoginMutation,
  useLogoutMutation,
  usePasswordMutation,
  useEmployeeQuery,
  useUpdateEmployeeMutation,
  useDocumentsQuery,
  useUploadMutation,
  useArchiveMutation,
  useUsersQuery,
  useCreateUserMutation,
  useUpdateUserMutation,
  useAuditQuery,
  useHierarchyQuery,
  type User,
  type Employee,
} from "./api";

export function Login() {
  const [login, { isLoading, error }] = useLoginMutation();
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  async function submit(e: FormEvent) {
    e.preventDefault();
    try {
      await login({ login: name, password }).unwrap();
      setPassword("");
    } catch {
      setPassword("");
    }
  }
  return (
    <main className="login-page">
      <section className="login-card">
        <span className="eyebrow">ENTERPRISE HRMS · GÜVENLİ ÇALIŞMA ALANI</span>
        <h1>
          İnsan odaklı.
          <br />
          Birlikte güçlü.
        </h1>
        <p>Organizasyonunuza güvenle giriş yapın.</p>
        <form onSubmit={submit}>
          <label>
            Kullanıcı adı veya e-posta
            <input
              autoComplete="username"
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label>
            Parola
            <input
              type="password"
              autoComplete="current-password"
              required
              maxLength={128}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          {error && <p role="alert">{errorMessage(error)}</p>}
          <button disabled={isLoading}>
            {isLoading ? "Doğrulanıyor…" : "Giriş yap"}
          </button>
        </form>
        <small>Oturum ve dosya erişimi sunucu tarafından korunur.</small>
      </section>
    </main>
  );
}
export function Account({ user }: { user: User }) {
  const [change, { error, isLoading }] = usePasswordMutation();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const dispatch = useDispatch();
  return (
    <>
      <span className="eyebrow">HESABIM</span>
      <h1>{user.username}</h1>
      <p>
        {user.email} · {user.role}
      </p>
      {user.employee_id ? (
        <Link to={`/employees/${user.employee_id}`}>Personel profilim →</Link>
      ) : (
        <p>Bu hesap henüz bir personel kaydına bağlı değil.</p>
      )}
      <section className="enterprise-card">
        <h2>Parolayı değiştir</h2>
        <p>İşlem tüm oturumlarınızı kapatır.</p>
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            try {
              await change({
                current_password: current,
                new_password: next,
              }).unwrap();
              setCurrent("");
              setNext("");
              dispatch(api.util.resetApiState());
            } catch {}
          }}
        >
          <label>
            Mevcut parola
            <input
              type="password"
              autoComplete="current-password"
              required
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
            />
          </label>
          <label>
            Yeni parola
            <input
              type="password"
              autoComplete="new-password"
              minLength={12}
              maxLength={128}
              required
              value={next}
              onChange={(e) => setNext(e.target.value)}
            />
          </label>
          {error && <p role="alert">{errorMessage(error)}</p>}
          <button disabled={isLoading}>Değiştir ve oturumları kapat</button>
        </form>
      </section>
    </>
  );
}
export function Logout() {
  const [logout, { isLoading }] = useLogoutMutation();
  const dispatch = useDispatch();
  const [error, setError] = useState("");
  return (
    <>
      <button
        disabled={isLoading}
        onClick={async () => {
          try {
            await logout().unwrap();
            dispatch(api.util.resetApiState());
          } catch (e) {
            setError(errorMessage(e));
          }
        }}
      >
        Çıkış yap
      </button>
      {error && <p role="alert">{error}</p>}
    </>
  );
}
export function Forbidden() {
  return (
    <section role="alert">
      <h1>Erişim yetkiniz yok</h1>
      <Link to="/account">Hesabıma dön</Link>
    </section>
  );
}

function Files({
  id,
  user,
  photoId,
}: {
  id: number;
  user: User;
  photoId?: number | null;
}) {
  const [page, setPage] = useState(1);
  const { data, error, isLoading } = useDocumentsQuery({ id, page });
  const [upload, { isLoading: busy }] = useUploadMutation();
  const [archive] = useArchiveMutation();
  const [message, setMessage] = useState("");
  const [type, setType] = useState("OTHER");
  const [visibility, setVisibility] = useState("SELF");
  async function send(form: HTMLFormElement, photo: boolean) {
    const body = new FormData(form);
    if (!photo) {
      body.set("document_type", type);
      body.set("visibility", visibility);
    }
    try {
      await upload({ id, photo, body }).unwrap();
      setMessage("Dosya güvenle kaydedildi.");
      form.reset();
    } catch (e) {
      setMessage(errorMessage(e));
    }
  }
  async function download(file: number, name: string) {
    try {
      const response = await fetch(
        `/api/employees/${id}/documents/${file}/download`,
        { credentials: "include" },
      );
      if (!response.ok) throw { status: response.status };
      const url = URL.createObjectURL(await response.blob());
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      setMessage(errorMessage(e));
    }
  }
  return (
    <section className="enterprise-card">
      <h2>Özel dosyalar</h2>
      <p>
        Fotoğraf en fazla 5 MB; PDF, DOCX, JPEG ve PNG belgeler en fazla 10 MB.
        Dosyalar herkese açık değildir.
      </p>
      {photoId ? (
        <img
          className="profile-photo"
          alt="Personel profil fotoğrafı"
          src={`/api/employees/${id}/photo?v=${photoId}`}
        />
      ) : (
        <div className="avatar" aria-label="Varsayılan avatar">
          ♧
        </div>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void send(e.currentTarget, true);
        }}
      >
        <label>
          Profil fotoğrafı
          <input
            name="file"
            type="file"
            accept="image/jpeg,image/png,image/webp"
            required
          />
        </label>
        <button disabled={busy}>Fotoğrafı kaydet</button>
      </form>
      {user.role !== "EMPLOYEE" && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void send(e.currentTarget, false);
          }}
        >
          <label>
            Belge
            <input
              name="file"
              type="file"
              accept=".pdf,.docx,.jpg,.jpeg,.png"
              required
            />
          </label>
          <label>
            Belge türü
            <select value={type} onChange={(e) => setType(e.target.value)}>
              {[
                "CV",
                "DIPLOMA",
                "CERTIFICATE",
                "CONTRACT",
                "IDENTITY",
                "OTHER",
              ].map((v) => (
                <option key={v}>{v}</option>
              ))}
            </select>
          </label>
          <label>
            Görünürlük
            <select
              value={visibility}
              onChange={(e) => setVisibility(e.target.value)}
            >
              {[
                "SELF",
                "HR_ONLY",
                ...(user.role === "ADMIN" ? ["ADMIN_ONLY"] : []),
              ].map((v) => (
                <option key={v}>{v}</option>
              ))}
            </select>
          </label>
          <label>
            Açıklama
            <input name="description" maxLength={1000} />
          </label>
          <button disabled={busy}>Belgeyi yükle</button>
        </form>
      )}
      {message && <p role="status">{message}</p>}
      {error && <p role="alert">{errorMessage(error)}</p>}
      {isLoading ? (
        <p>Belgeler yükleniyor…</p>
      ) : (
        <ul className="document-list">
          {data?.items.map((file) => (
            <li key={file.id}>
              <div>
                <strong>{file.original_name}</strong>
                <small>
                  {file.document_type} · {file.visibility} ·{" "}
                  {Math.ceil(file.size_bytes / 1024)} KB
                </small>
              </div>
              <button
                onClick={() => void download(file.id, file.original_name)}
              >
                İndir
              </button>
              {user.role !== "EMPLOYEE" && (
                <button
                  onClick={async () => {
                    if (
                      !window.confirm(
                        "Belge arşivlensin mi? Fiziksel dosya silinmez.",
                      )
                    )
                      return;
                    try {
                      await archive({ id, file: file.id }).unwrap();
                      setMessage("Belge arşivlendi.");
                    } catch (e) {
                      setMessage(errorMessage(e));
                    }
                  }}
                >
                  Arşivle
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {!isLoading && !data?.items.length && (
        <p>Görüntülenebilir belge bulunmuyor.</p>
      )}
      <button disabled={page === 1} onClick={() => setPage(page - 1)}>
        Önceki
      </button>
      <span> Sayfa {page} </span>
      <button
        disabled={(data?.items.length || 0) < 20}
        onClick={() => setPage(page + 1)}
      >
        Sonraki
      </button>
    </section>
  );
}
export function Profile({ user }: { user: User }) {
  const { id = "" } = useParams();
  const { data, error, isLoading } = useEmployeeQuery(id);
  const [update, { isLoading: saving }] = useUpdateEmployeeMutation();
  const [message, setMessage] = useState("");
  if (isLoading) return <p>Profil yükleniyor…</p>;
  if (error) return <p role="alert">{errorMessage(error)}</p>;
  if (!data) return null;
  const fields: [keyof Employee, string][] = [
    ["personal_email", "Kişisel e-posta"],
    ["phone_number", "Telefon"],
    ...(user.role === "EMPLOYEE"
      ? []
      : ([
          ["job_title", "Unvan"],
          ["work_location", "Çalışma lokasyonu"],
          ["manager_id", "Yönetici personel ID"],
        ] as [keyof Employee, string][])),
  ];
  return (
    <>
      <span className="eyebrow">PERSONEL PROFİLİ · {data.employee_no}</span>
      <h1>
        {data.first_name} {data.last_name}
      </h1>
      <p>
        {data.job_title || "Unvan belirtilmedi"} · {data.employment_status} ·{" "}
        {data.employment_type}
      </p>
      <div className="profile-facts">
        <span>Kurumsal e-posta: {data.email || "—"}</span>
        <span>İşe giriş: {data.hire_date}</span>
        <span>Departman: {data.department_id}</span>
        <span>Durum: {data.is_active ? "Aktif" : "Pasif"}</span>
      </div>
      {user.role !== "EMPLOYEE" && (
        <Link to={`/employees/${id}/edit`}>
          Temel personel bilgilerini düzenle →
        </Link>
      )}
      <section className="enterprise-card">
        <h2>İletişim ve çalışma bilgileri</h2>
        <form
          key={JSON.stringify(data)}
          onSubmit={async (e) => {
            e.preventDefault();
            const values = Object.fromEntries(new FormData(e.currentTarget));
            const body: Record<string, unknown> = {};
            fields.forEach(([key]) => {
              body[key] =
                values[key] === ""
                  ? null
                  : key === "manager_id"
                    ? Number(values[key])
                    : values[key];
            });
            if (user.role !== "EMPLOYEE") {
              body.employment_type = values.employment_type;
              body.employment_status = values.employment_status;
            }
            try {
              await update({ id, body }).unwrap();
              setMessage("Profil güncellendi.");
            } catch (err) {
              setMessage(errorMessage(err));
            }
          }}
        >
          {fields.map(([key, label]) => (
            <label key={key}>
              {label}
              <input
                name={key}
                defaultValue={String(data[key] ?? "")}
                type={
                  key === "manager_id"
                    ? "number"
                    : key === "personal_email"
                      ? "email"
                      : "text"
                }
                min={1}
                maxLength={key === "phone_number" ? 30 : 254}
              />
            </label>
          ))}
          {user.role !== "EMPLOYEE" && (
            <>
              <label>
                Çalışma türü
                <select
                  name="employment_type"
                  defaultValue={data.employment_type}
                >
                  {[
                    "FULL_TIME",
                    "PART_TIME",
                    "CONTRACT",
                    "INTERN",
                    "UNSPECIFIED",
                  ].map((v) => (
                    <option key={v}>{v}</option>
                  ))}
                </select>
              </label>
              <label>
                Çalışma durumu
                <select
                  name="employment_status"
                  defaultValue={data.employment_status}
                >
                  {["ACTIVE", "ON_LEAVE", "TERMINATED"].map((v) => (
                    <option key={v}>{v}</option>
                  ))}
                </select>
              </label>
            </>
          )}
          <button disabled={saving}>Profili kaydet</button>
          {message && <p role="status">{message}</p>}
        </form>
      </section>
      <Files id={Number(id)} user={user} photoId={data.photo_id} />
    </>
  );
}
export function Users({ user }: { user: User }) {
  const [page, setPage] = useState(1);
  const { data, error } = useUsersQuery(page);
  const [create, { isLoading }] = useCreateUserMutation();
  const [update] = useUpdateUserMutation();
  const [message, setMessage] = useState("");
  return (
    <>
      <h1>Kullanıcılar ve roller</h1>
      <p>
        ADMIN: tüm yönetim. HR_MANAGER: personel, departman ve HR belgeleri.
        EMPLOYEE: yalnız kendi profili ve SELF belgeleri.
      </p>
      {error && <p role="alert">{errorMessage(error)}</p>}
      {user.role === "ADMIN" && (
        <section className="enterprise-card">
          <h2>Yeni kullanıcı</h2>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              const form = e.currentTarget;
              const body = Object.fromEntries(new FormData(form));
              try {
                await create({
                  ...body,
                  employee_id: body.employee_id
                    ? Number(body.employee_id)
                    : null,
                }).unwrap();
                form.reset();
                setMessage("Kullanıcı oluşturuldu.");
              } catch (err) {
                setMessage(errorMessage(err));
              }
            }}
          >
            {[
              ["username", "Kullanıcı adı"],
              ["email", "E-posta"],
              ["password", "Başlangıç parolası"],
              ["employee_id", "Personel ID"],
            ].map(([key, label]) => (
              <label key={key}>
                {label}
                <input
                  name={key}
                  type={
                    key === "password"
                      ? "password"
                      : key === "employee_id"
                        ? "number"
                        : key === "email"
                          ? "email"
                          : "text"
                  }
                  required={key !== "employee_id"}
                  minLength={key === "password" ? 12 : undefined}
                  autoComplete={key === "password" ? "new-password" : "off"}
                />
              </label>
            ))}
            <label>
              Rol
              <select name="role">
                <option>EMPLOYEE</option>
                <option>HR_MANAGER</option>
                <option>ADMIN</option>
              </select>
            </label>
            <button disabled={isLoading}>Kullanıcı oluştur</button>
          </form>
        </section>
      )}
      {message && <p role="status">{message}</p>}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Kullanıcı</th>
              <th>E-posta</th>
              <th>Personel</th>
              <th>Rol</th>
              <th>İşlem</th>
            </tr>
          </thead>
          <tbody>
            {data?.map((row) => (
              <tr key={row.id}>
                <td>{row.username}</td>
                <td>{row.email}</td>
                <td>{row.employee_id || "—"}</td>
                <td>
                  {user.role === "ADMIN" ? (
                    <select
                      aria-label={`${row.username} rolü`}
                      value={row.role}
                      onChange={async (e) => {
                        if (
                          !window.confirm(
                            "Rol değişikliği kullanıcının oturumlarını kapatır. Devam edilsin mi?",
                          )
                        )
                          return;
                        try {
                          await update({
                            id: row.id,
                            body: { role: e.target.value },
                          }).unwrap();
                          setMessage(
                            "Rol güncellendi; oturumlar iptal edildi.",
                          );
                        } catch (err) {
                          setMessage(errorMessage(err));
                        }
                      }}
                    >
                      {["ADMIN", "HR_MANAGER", "EMPLOYEE"].map((v) => (
                        <option key={v}>{v}</option>
                      ))}
                    </select>
                  ) : (
                    row.role
                  )}
                </td>
                <td>
                  {user.role === "ADMIN" && (
                    <button
                      onClick={async () => {
                        if (
                          !window.confirm(
                            "Kullanıcı pasifleştirilsin ve oturumları kapatılsın mı?",
                          )
                        )
                          return;
                        try {
                          await update({
                            id: row.id,
                            body: { is_active: false },
                          }).unwrap();
                          setMessage("Kullanıcı pasifleştirildi.");
                        } catch (err) {
                          setMessage(errorMessage(err));
                        }
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
      <button disabled={page === 1} onClick={() => setPage(page - 1)}>
        Önceki
      </button>
      <button
        disabled={(data?.length || 0) < 20}
        onClick={() => setPage(page + 1)}
      >
        Sonraki
      </button>
    </>
  );
}
export function Audit() {
  const [page, setPage] = useState(1);
  const { data, error } = useAuditQuery(page);
  return (
    <>
      <h1>Denetim günlüğü</h1>
      <p>Parola, token ve belge içeriği kaydedilmez.</p>
      {error && <p role="alert">{errorMessage(error)}</p>}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {["Zaman", "Aktör", "İşlem", "Nesne", "Sonuç", "İstek"].map(
                (v) => (
                  <th key={v}>{v}</th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {data?.map((row) => (
              <tr key={String(row.id)}>
                {[
                  row.created_at,
                  row.actor_id,
                  row.action,
                  `${row.entity_type} ${row.entity_id ?? ""}`,
                  row.outcome,
                  row.request_id,
                ].map((v, i) => (
                  <td key={i}>{String(v ?? "—")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <button disabled={page === 1} onClick={() => setPage(page - 1)}>
        Önceki
      </button>
      <button
        disabled={(data?.length || 0) < 20}
        onClick={() => setPage(page + 1)}
      >
        Sonraki
      </button>
    </>
  );
}
export function Hierarchy() {
  const { data, error } = useHierarchyQuery();
  return (
    <>
      <h1>Organizasyon yapısı</h1>
      {error && <p role="alert">{errorMessage(error)}</p>}
      {data?.truncated && <p role="alert">İlk 1000 departman gösteriliyor.</p>}
      <div className="enterprise-card">
        {data?.items.map((row) => (
          <div className="hierarchy-row" key={row.id}>
            <strong>{row.department_name}</strong>
            <span>
              {row.department_code} · Üst departman: {row.parent_id ?? "Kök"} ·
              Yönetici: {row.manager_id ?? "—"} ·{" "}
              {row.is_active ? "Aktif" : "Pasif"}
            </span>
          </div>
        ))}
      </div>
    </>
  );
}
