# Enterprise HRMS · MVP v2

Yerel geliştirme hedefi **HR_CORE / FREEPDB1**, Oracle 26ai Free. MVP v1
çekirdek tabloları ve kayıtları korunur; v2 profil, kimlik ve dosya tablolarını ekler.

## V2 kullanımı

- Arayüz: `http://127.0.0.1:5173`. Health dışında API kimlik doğrulaması zorunludur.
- İlk ADMIN yalnız ayrıcalıklı yerel operator aracıyla oluşturulur:
  `.venv/bin/python -B -m backend.bootstrap_admin --apply --generate-password`.
  Araç boş kullanıcı tablosunda çalışır; mevcut hesabı/parolayı değiştirmez.
- Bu geliştirme ortamında ilk hesap **admin** oluşturuldu. Başlangıç parolası yalnız
  `.secrets/initial-admin-password` dosyasındadır (0600, dizin 0700); Git/loglara eklenmez.
  İlk girişte **Hesabım** ekranından değiştirin; tüm oturumlar iptal edilir.
  Belirsiz commit sonucunda bootstrap'ı tekrar çalıştırmayın.
- Docker: `docker compose --profile events up -d --build --wait`.
  Mevcut Oracle container/volume korunur. `employee-files` named volume özel dosyaları,
  `notification-data` inbox'ı ve `rabbitmq-data` broker verisini saklar.
  Volume silmek/veri temizlemek bu iş akışının parçası değildir.

## V2 mimarisi ve veri sözleşmesi

`EMPLOYEE_PROFILE` ve `DEPARTMENT_PROFILE` bire bir uzantıları mevcut identity,
PK/FK/UNIQUE ve API alanlarını korur. Profil bulunmayan eski personelde çalışma türü
UNSPECIFIED, durum IS_ACTIVE'a göre ACTIVE/TERMINATED olarak gösterilir. Açık profil
ACTIVE/ON_LEAVE/TERMINATED değerlerini saklar; TERMINATED çekirdek kaydı pasifleştirir.
Pasif kaydı profil üzerinden yeniden aktifleştirmek kabul edilmez.
Audit alanları istemciden alınmaz. Kurumsal e-posta `email`/`corporate_email` alias'ıyla
aynı alanı temsil eder. Özel iletişim alanları, unvan, lokasyon ve yönetici uzantıda kalır.

- `HRMS_RULES`: aktif departmana personel atama ve aktif personeli olan departmanı
  pasifleştirme yasağı; v1 güvenilir kaynak değişmez.
- `HRMS_ENTERPRISE`: aktif yönetici kontrolü, employee manager ve department parent
  döngü/self-reference reddi. Package commit yapmaz. Tüm organizasyon yazıcıları yeni
  guard satırını önce kilitler, sonra çekirdek kilitlerini alır. İlk sürüm global guard
  ile tutarlı sırayı sağlar; yoğun yazma yükünde daraltma için ayrı tasarım gerekir.
  API/package dışından doğrudan SQL yazan oturumlar uygulama kurallarını atlayabilir.
- FastAPI: Pydantic, repository, service transaction, auth dependency, audit ve özel storage.
- React/RTK Query: login, rol menüleri, rehber arama/filtre/sayfalama, personel profili,
  fotoğraf/belgeler, departman hiyerarşisi, kullanıcı/rol ve audit ekranları.
- Mevcut EMPLOYEE_CREATED v1 olayı aynı transaction'da outbox'a yazılır. RabbitMQ
  publisher confirms ve Flask idempotent inbox/DLQ korunur. Belge içeriği, credential
  ve özel iletişim bilgileri broker'a konmaz. Diğer v2 işlemler eşzamanlı audit'tedir;
  gerçek asenkron tüketici ihtiyacı olmadan yeni olay tipleri eklenmedi.

## Authentication ve RBAC

Argon2id parola hash'i; rastgele opaque access/refresh token'larının yalnız SHA-256
hash'leri Oracle'dadır. Access 15 dakika, refresh mutlak 7 gün; rotation eski token'ı
kullanılmış olarak kaydeder. Eski refresh tekrar kullanılırsa ilgili session family
iptal edilir. Logout, parola ve rol/aktiflik değişiklikleri session'ları iptal eder.
Her istekte aktif kullanıcı ve güncel rol DB'den okunur. Login başına istemci bucket'ı
15 dakikada 5 başarısız girişten sonra sınırlar; anahtar hash'lenir.

| İşlem | ADMIN | HR_MANAGER | EMPLOYEE |
|---|---|---|---|
| Department/Employee yönetimi | Evet | Evet | Hayır |
| Personel profili okuma | Tümü | Tümü | Yalnız kendi |
| Profil güncelleme | Tümü | Tümü | Kendi personal_email/phone_number |
| Fotoğraf yükleme | Tümü | Tümü | Kendi |
| Belge yükleme/arşivleme | Tümü | ADMIN_ONLY hariç | Hayır |
| Belge okuma/indirme | Tümü | ADMIN_ONLY hariç | Kendi SELF belgeleri |
| Kullanıcı listeleme | Evet | Sınırlı güvenli projection | Hayır |
| Kullanıcı/rol yönetimi | Evet | Hayır | Hayır |
| Audit okuma | Evet | Hayır | Hayır |

API yetkisi frontend'den bağımsızdır. Son aktif ADMIN pasifleştirilemez/demote edilemez.
EMPLOYEE hesabı employee FK bağlantısı gerektirir. Audit actor/action/entity/outcome,
zaman ve request UUID saklar; parola, token, belge içeriği saklamaz. Normal kullanıcı
API üzerinden audit yazamaz/değiştiremez. Eksik/bozuk session reddedilir; kimlikli rol,
nesne ve CSRF reddi audit edilir. Anonim hatalarda DB'ye sınırsız audit yazımı yapılmaz.

HttpOnly access/refresh, root path, SameSite=Strict; ayrı CSRF cookie hash'i unsafe
isteklerde X-CSRF-Token ile doğrulanır. Origin allowlist ek korumadır. Frontend
localStorage'a token koymaz; 401 pre-authorization sonucunda tek refresh yapabilir;
500/commit belirsizliğinde yazmayı tekrarlamaz. İki sekmede eşzamanlı refresh eski token
replay korumasını tetikleyebilir; güvenli şekilde yeniden giriş gerekir.
**Yerel HTTP** için Secure=0; production için TLS, HRMS_ENV=production,
HRMS_COOKIE_SECURE=1 ve açık HRMS_ALLOWED_ORIGINS gerekir. Insecure production cookie
konfigürasyonu token yazımından önce reddedilir. Bu Compose production dağıtımı değildir.

## Özel dosya depolama

Storage contract: save/read/discard_uncommitted; LocalStorage adapter ileride S3/MinIO
adapter'ıyla değiştirilebilir. Rastgele storage key, 0600 dosya, private directory;
istemci dosya adı hiçbir zaman yol olmaz. Container UID 10001 volume'a yazar.

- Fotoğraf ≤5 MiB: JPEG/PNG/WebP gerçek içerik + piksel sınırı; EXIF temizlenir,
  en fazla 1024 px WebP olarak yeniden kodlanır. Değiştirilen fotoğraf metadata'sı
  arşivlenir, eski fiziksel dosya korunur. Avatar yokluğunda varsayılan görünüm.
- Belgeler ≤10 MiB: PDF/JPEG/PNG/DOCX uzantı/MIME/içerik kontrolü. Aktif PDF eylemleri,
  encrypted PDF, DOCX macro/external relationship/DDE, zip traversal/symlink/bomb reddi.
- Metadata ID, employee, tür, özgün ad, key, MIME, boyut, checksum, actor, zaman,
  açıklama ve görünürlük saklar. API response storage key/parola hash'i döndürmez.
- İndirme nesne yetkisinden sonra boyut/hash kontrolü yapar; attachment ve no-store,
  nosniff, sandbox header'ları kullanır. Public static download yolu yoktur.
- Yazım hatasında yalnız bu isteğin dosyası, rollback kesinleşmişse temizlenir.
  Commit/rollback belirsizliğinde dosya korunur; otomatik retry/silme yapılmaz.
- Arşivleme metadata işlemidir, fiziksel içerik silmez. Retention/backup/manuel
  cleanup ayrı operator politikasıdır. Read-only reconciliation:
  `docker compose exec -T backend python -B -m backend.app.storage.reconcile`.
  Eksik/bozuk metadata ID'leri ve sahipsiz dosya sayısını raporlar; onarım yapmaz.

İçerik doğrulaması tam antivirus garantisi değildir. Production öncesi karantina/AV,
şifreli backup, disk kotası, malware/resource izolasyonu ve saklama politikası gerekir.

## V2 deployment ve testler

V0001–V0004, dar tek CREATE TABLE parser/executor üzerinden korunur.
V0005–V0013 için **ayrı kontrollü** `database/tools/deploy_enterprise.py` aracı sabit
schema generator, artifact byte/hash manifest, gerçek TTY, aynı DBMS_LOCK/history
sözleşmesi ve metadata/source doğrulaması kullanır. Orijinal parser gevşetilmedi.
Bundle yalnız yeni tablolar, yeni guard'da tek satır ve HRMS_ENTERPRISE spec/body'dir.
Uygulandıktan sonra `--apply` olmadan tüm history/metadata/package kaynakları doğrulanır.
Belirsizlikte RUNNING ve kısmi nesneler korunur; otomatik retry/repair yoktur.

```bash
.venv/bin/python -B -m unittest discover -s backend/tests
.venv/bin/python -B -m unittest discover -s database/tools -p 'test_migrate.py'
.venv/bin/python -B -m unittest discover -s notification_service/tests
cd frontend
npm test
npm run build
```

Canlı v2 test opt-in: proje kökünde
`HRMS_RUN_ORACLE_TESTS=1 .venv/bin/python -B -m backend.tests.enterprise_e2e`.
Yalnız özel yerel ilk ADMIN credential dosyasını okur; V2IT etiketli yeni fixture'lar
oluşturur, sonunda test hesap/personel/departmanlarını pasifleştirir ve belgeleri
arşivler. Fiziksel dosya/kayıt silmez. HTTP/commit belirsizliğinde durur; otomatik
tekrar veya cleanup yapmaz. İlk ADMIN parolası değiştirildikten sonra test için HRMS_TEST_USERNAME ve
HRMS_TEST_PASSWORD_FILE (0600 özel dosya) ile ayrı ADMIN test hesabı kullanılabilir;
owner parolasını geri almayın.
SELECT-only olay doğrulaması:
`HRMS_RUN_ORACLE_TESTS=1 .venv/bin/python -B -m backend.tests.verify_enterprise_runtime`.
Eski v1 canlı HTTP test scriptleri auth öncesi sözleşmenin tarihsel testleridir;
v2 canlı uçtan uca kabul testi `enterprise_e2e` girişidir. Mock testler Oracle açmaz.

Doğrulama: 169 backend, 466 migration, 10 bootstrap, 7 Flask ve 22 frontend
testi başarılı (674 yerel test). Canlı v2 kabul akışında 65 HTTP/Oracle kontrolü;
4 test personelinin outbox/Flask receipt eşleşmesi; yeniden oluşturulan container
sonrasında 6 private dosya metadata/checksum eşleşmesi doğrulandı.

Bilinen sınırlar: API istek başına Thin bağlantı; profil listelerinde sayfa başına
bounded enrichment sorguları; global organization lock; hiyerarşi görünümü 1000
kayıtla sınırlı; MFA/SSO/password reset, tam AV ve object storage bu MVP'de yoktur.
Geliştirme ortamı localhost portları, ayrı production secrets/HTTPS ve least-privilege
DB runtime/deployment kullanıcı ayrımı production öncesi gereklidir.

---

## MVP v1 mimari ve kurulum geçmişi

Aşağıdaki bölüm v1 bileşenlerini açıklar; v2 auth ve deployment talimatları önceliklidir.


Yerel geliştirme için Department/Employee yönetimi: Oracle 26ai Free,
FastAPI, React + TypeScript, Redux Toolkit/RTK Query, RabbitMQ ve Flask.

## Mimari

```text
React / RTK Query → nginx /api → FastAPI → HR_CORE (FREEPDB1)
                                    ├─ HRMS_RULES (PL/SQL)
                                    └─ EMPLOYEE + HRMS_OUTBOX (aynı transaction)
                                              ↓
                                      Outbox relay / publisher confirms
                                              ↓
                                  RabbitMQ → Flask consumer → SQLite inbox
```

- **Oracle:** kalıcı kayıtlar, identity, PK/UNIQUE/FK/CHECK ve varsayılanlar.
- **HRMS_RULES:** aktif departmana atama, sıralı departman kilitleri ve aktif
  personeli olan departmanı pasifleştirme yasağı. Package commit/rollback yapmaz.
- **FastAPI:** HTTP, validation, transaction sınırları ve güvenli hata yanıtları.
  Repository commit yapmaz. Senkron endpoint ve istek kapsamlı Thin bağlantı kullanılır.
- **React:** dashboard, listeler/sayfalama, oluşturma, düzenleme, personel detayı,
  onaylı pasifleştirme, hata/loading/boş durumlar ve başarı bildirimleri.
- **RabbitMQ:** durable exchange/queue, persistent mesaj ve publisher confirms.
- **Flask:** Oracle CRUD tekrarlamaz; minimum içerikli EMPLOYEE_CREATED olayını
  doğrular ve kendi kalıcı inbox'ına işler. Oracle şifresi bu servise verilmez.

## Ön koşullar ve gizli ayarlar

Python 3.13, Node 24, Docker Compose ve **mevcut** `oracle-free` container gerekir.
Compose Oracle oluşturmaz, mevcut container veya volume'lerini yönetmez/silmez.
macOS container'ları Oracle'a `host.docker.internal:1521/FREEPDB1` ile ulaşır.
Host Python araçları `.env` içindeki `DB_HOST=localhost` kullanır.

Mevcut `.env` dosyasının üzerine yazmayın. Yeni ortamda `.env.example` temel
alınarak DB_HOST, DB_PORT, DB_SERVICE, DB_USER, DB_PASSWORD ve ayrı geliştirme
RABBITMQ_USER/RABBITMQ_PASSWORD değerlerini özel `.env` dosyasında tanımlayın.
Dosyayı Git'e eklemeyin; `chmod 600 .env` kullanın. Ortam değişkenleri `.env`
değerlerine göre önceliklidir. Gerçek ayarları `docker compose config` veya loglarla
paylaşmayın; yapı kontrolü için `docker compose config -q` kullanın.

```bash
.venv/bin/python -m pip install -r backend/requirements.txt
.venv/bin/python -m pip install -r notification_service/requirements.txt
npm --prefix frontend ci
```

## Migration ve PL/SQL deployment

```bash
.venv/bin/python -B database/tools/migrate.py validate
.venv/bin/python -B database/tools/migrate.py plan
.venv/bin/python -B database/tools/migrate.py status
.venv/bin/python -B database/tools/migrate.py db-plan
```

`validate/plan` yereldir; SQL içerik kontrolünü executor ayrıca yapar.
V0001 test tablosu, V0002 DEPARTMENT, V0003 EMPLOYEE, V0004 HRMS_OUTBOX oluşturur.
Yeni boş ortamda bootstrap ayrı gerçek operatör onayıyla yapılır; mevcut ortamda
history tablosunu tekrar oluşturmayın. Bir komut yalnız bir migration uygular:

```bash
.venv/bin/python -B database/tools/migrate.py apply
```

Gerçek Terminal'de hedef, sürüm ve tam SHA-256 onay metni elle girilir. Onayı pipe,
sahte TTY veya otomatik girişle geçmeyin. DDL implicit commit yapar; MISMATCH,
INVALID_METADATA, RUNNING/FAILED veya belirsiz commit durumunda tekrar/repair/DROP
uygulamayın. Ön kontroller isimleri rezerve etmez; DBMS_LOCK dış yazıcıları kilitlemez.

Package DDL, CREATE TABLE parser'ına eklenmemiştir. Sürümlü kaynaklar ve manifest
`database/plsql/` altındadır:

```bash
.venv/bin/python -B database/tools/deploy_rules.py --fingerprint
.venv/bin/python -B database/tools/deploy_rules.py          # SELECT / plan
.venv/bin/python -B database/tools/deploy_rules.py --apply  # gerçek TTY onayı
```

Deployment tek specification/body dosyasını birer DDL olarak gönderir; SQL bölmez.
HR_CORE/FREEPDB1 hedefini, checksum'ı, ortak DBMS_LOCK kilidini, derleme hatalarını,
VALID durumunu ve ALL_SOURCE eşleşmesini kontrol eder. Mevcut farklı veya kısmi
package otomatik replace/repair edilmez. Package sürüm kanıtı VERSION(), kaynak
hash'i, manifest ve Git geçmişidir; SCHEMA_MIGRATIONS içine package için sahte kayıt
atılmaz ve ayrı DB deployment history tablosu yoktur.

V0004 history SUCCESS ve metadata MATCH; HRMS_RULES v1 VALID/kaynak MATCH sonrası
özel `.env` içinde şu **gizli olmayan** feature ayarlarını etkinleştirin:

```dotenv
HRMS_OUTBOX_ENABLED=1
HRMS_PLSQL_ENABLED=1
```

`0` yalnız deployment öncesi uyum modudur; tam MVP olay garantisi sağlamaz.
PL/SQL modu açıkken aktiflik iş kurallarında package güvenilir kaynaktır; Python
aynı kuralı tekrar sorgulamaz. Kapalı mod eski service kurallarını kullanır.
Bu package'ı atlayan doğrudan owner DML'si çapraz tablo kurallarını aşabilir.
Yeni bir Oracle yazıcı servis package sözleşmesine uymalıdır; production için ayrı
kısıtlı runtime kullanıcısı/EXECUTE yetkileri tasarlanmalıdır. Flask Oracle yazıcısı değildir.

## Servisleri çalıştırma

Deployment doğrulandıktan sonra:

```bash
docker compose --profile events up -d --build --wait
docker compose --profile events ps
```

- Arayüz: http://localhost:5173
- FastAPI/OpenAPI: http://localhost:8000/docs
- API sağlık: http://localhost:8000/health (Oracle'a bağlanmaz)
- Flask sağlık: http://localhost:5001/health (consumer bağlıysa 200; aksi 503)
- RabbitMQ yönetimi: http://localhost:15672 (özel `.env` kimlik bilgileri)

Portlar yalnız `127.0.0.1` üzerinde yayınlanır. `outbox` servisi `events` profili
ile başlar; Oracle commit belirsizliğinde otomatik restart/reconnect yapmaz.
Geliştirme servislerini durdurmak Oracle'ı etkilemez:

```bash
docker compose --profile events stop
```

Veri volume'lerini silen komutlar kullanmayın.

Docker olmadan FastAPI: `.venv/bin/python -m uvicorn backend.app.main:app --port 8000`.
Frontend: `npm --prefix frontend run dev` (`/api` localhost:8000'e proxy edilir).
Flask: `.venv/bin/python -m notification_service.app`; yerelde INBOX_PATH için
Git dışı yazılabilir bir dizin ve RABBITMQ_HOST=localhost ayarlayın.
Relay: `.venv/bin/python -m backend.app.events.relay` (DB ve broker env değerleri
süreç ortamında tanımlı olmalıdır; CLI relay kendiliğinden `.env` yüklemez).

## API sözleşmesi

Her iki kaynak için POST/GET `/departments` ve `/employees`; GET/PATCH `/{id}`;
POST `/{id}/deactivate`. Fiziksel DELETE veya reactivation yoktur.

- ID/CREATED_AT/IS_ACTIVE istemciden alınmaz; yeni kayıt aktiftir.
- Kod ve personel no oluşturulduktan sonra değişmez.
- PATCH'te eksik alan korunur; açık EMAIL null değeri temizler. Boş PATCH 422'dir.
- EMAIL benzersiz değildir. Tarih `YYYY-MM-DD`; timestamp timezone offset içerir.
- Sayfalama `page>=1`, `1<=size<=100`; yanıt `{items,page,size}`. Toplam sayım yoktur;
  tam dolu son sayfanın ardından boş sayfa görülebilir.
- Dashboard ve departman seçim listesi ilk 100 kaydı kapsar; UI bunu belirtir.
- Sayısal ID sözleşmesi mevcut API'de korunmuştur; JavaScript safe-integer sınırını
  aşan gerçek ID'ler için ileride string-ID sözleşmesi gerekir.
- Hatalar: 404 bulunamadı, 409 UNIQUE/FK/iş kuralı/kilit, 422 validation, güvenli 500.
- `commit_outcome_unknown`, `connection_cleanup_failed` veya
  `transaction_cleanup_failed` durumunda yazma isteğini otomatik tekrarlamayın.

## Transaction, kilit ve olay güvenilirliği

Personel değişikliğinde önce personel satırı; sonra eski/yeni departmanlar artan
ID sırasıyla kilitlenir. Create yalnız hedef departmanı kilitler. Departman
pasifleştirmesi aynı parent kilidini alır, personel satırlarını kilitlemeden COUNT
ile kontrol eder. WAIT 5; timeout/deadlock otomatik retry edilmez. Transaction
başarıda tek commit yapar; commit öncesi hata rollback olur. Commit istisnasında
sonuç belirsizdir, rollback veya tekrar gönderim başarı kanıtı sayılmaz.

Employee ve outbox INSERT'i aynı transaction'dadır. Broker'la dağıtık transaction
veya exactly-once iddiası yoktur. Relay `FOR UPDATE SKIP LOCKED` ile bir kayıt alır,
persistent/mandatory mesajı confirm ile yayınlar, ardından PUBLISHED_AT işaretini
commit eder. Confirm sonrası DB işareti kaybolursa mesaj tekrar teslim edilebilir.
Flask SQLite inbox, event ID + fingerprint ile aynı işleme etkisini tekilleştirir.
Inbox commit'ten sonra ack edilir; geçersiz/çelişkili event DLQ'ya gider. Geçici
consumer hatası bir kez requeue edilir, tekrarında DLQ'ya gider. Consumer broker'a
yeniden bağlanabilir; Oracle commit hatası relay'yi durdurur ve manuel inceleme ister.
Broker teslim hatası outbox'ı korur; ATTEMPTS<10 sınırı vardır. İncelemeden poison
olayı silmeyin veya history düzeltmeyin. Relay idle döngüsünde heartbeat işler.

Olay yalnız event_id, event_type, version, employee_id, occurred_at taşır;
isim, e-posta veya bağlantı sırrı içermez. SQLite receipt ve kalıcı broker volume'leri
korunmalıdır. Yapılandırılmış loglar gözlemlenebilirliktir; logun tam bir kez
basılacağı garanti edilmez. MVP consumer iş etkisi kalıcı receipt kaydıdır; gerçek
SMTP/e-posta gönderimi yoktur. `/receipts/{event_id}` yalnız varlık boolean'ı sunar.

## Testler

Bağlantısız:

```bash
.venv/bin/python -B -m unittest discover -s backend/tests
.venv/bin/python -B -m unittest discover -s notification_service/tests
npm --prefix frontend test
npm --prefix frontend run build
.venv/bin/python -B -m unittest discover -s database/tools -p 'test_migrate.py'
```

Canlı testler yalnız doğrulanmış yerel HR_CORE ve açık opt-in içindir:

```bash
HRMS_RUN_ORACLE_TESTS=1 .venv/bin/python -B -m backend.tests.oracle_integration
HRMS_RUN_ORACLE_TESTS=1 .venv/bin/python -B -m backend.tests.oracle_concurrency
HRMS_RUN_ORACLE_TESTS=1 .venv/bin/python -B -m backend.tests.http_e2e
```

Her çalışma yeni MVPIT_/MVPLOCK_/MVPE2E_ etiketi üretir. Mevcut gerçek kayıtlar
korunur; başarılı fixture'lar pasif bırakılır, silinmez. Başarısız çalışmada tag'ı
inceleyin; belirsiz yazmayı otomatik tekrarlamayın. Testler identity boşlukları
oluşturabilir. Gerçek ağ kopması/commit belirsizliği kasıtlı tetiklenmez; bu yollar
mock testlerle kapsanır. UI tests fetch mock kullanır; E2E gerçek nginx ingress,
Oracle outbox ve Flask receipt korelasyonunu ayrıca doğrular.

## Doğrulama ve MVP sınırları

Bu geliştirme ortamında V0001–V0004 APPLIED; HRMS_OUTBOX MATCH; HRMS_RULES v1
VALID/kaynak MATCH doğrulandı. Gerçek CRUD, RETURNING, UTC `Z` dahil timezone,
UNIQUE/FK, rollback, iki oturumlu kilit yarışları, durable event/duplicate/DLQ ve
HTTP → outbox → RabbitMQ → Flask receipt E2E kontrolü geçti. Başlangıçta UTC test
beklentisi ve idle relay heartbeat hatası bulundu, düzeltildi ve yeniden doğrulandı.

Bu **yerel geliştirme MVP'sidir**: authentication/RBAC/TLS, production deployment,
çoklu instance Flask/SQLite ölçekleme, SMTP, büyük veri raporlaması ve kalıcı
izleme/alerting kapsam dışıdır. Flask development server yalnız dahili geliştirme
servisidir; production WSGI kurulumu gerekir. Mevcut TestClient/httpx kombinasyonu
deprecation uyarısı verir; geçiş ayrı bağımlılık güncellemesinde ele alınmalıdır.
