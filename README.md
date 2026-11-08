# Enterprise HRMS · MVP v1

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
