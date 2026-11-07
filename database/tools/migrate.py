"""Migration dosyalarını yerel incele veya Oracle geçmişini salt okunur göster."""

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIRECTORY = PROJECT_ROOT / "database" / "migrations"
MIGRATION_NAME = re.compile(r"V([0-9]{4})__([a-z][a-z0-9]*(?:_[a-z0-9]+)*)\.sql")


@dataclass(frozen=True)
class Migration:
    version: int
    path: Path
    checksum: str


def collect_migrations(directory: Path) -> list[Migration]:
    """Dosya adlarını ve versiyonları doğrula; SHA-256 özetlerini hesapla."""
    if not directory.is_dir():
        raise ValueError(f"Migration klasörü bulunamadı: {directory}")

    migrations: list[Migration] = []
    versions: dict[int, str] = {}
    errors: list[str] = []

    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue

        match = MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            errors.append(
                f"Geçersiz migration dosya adı: {path.name}. "
                "Beklenen: VNNNN__description.sql; açıklama küçük İngilizce "
                "harfler, rakamlar ve kelimeler arasında alt çizgi içermelidir."
            )
            continue

        version = int(match.group(1))
        if version in versions:
            errors.append(
                f"Tekrarlanan versiyon V{version:04d}: "
                f"{versions[version]} ve {path.name}"
            )
            continue

        versions[version] = path.name
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        migrations.append(Migration(version=version, path=path, checksum=checksum))

    if errors:
        raise ValueError("\n".join(errors))

    return sorted(migrations, key=lambda migration: migration.version)


def read_database_history(command_label: str = "Status") -> list[tuple] | None:
    """Yalnızca SELECT ile hedef oturumu doğrula ve migration geçmişini oku."""
    try:
        import oracledb
        from dotenv import load_dotenv
    except ImportError:
        print(f"Hata: {command_label} için python-oracledb ve python-dotenv gereklidir.", file=sys.stderr)
        return None

    try:
        load_dotenv(PROJECT_ROOT / ".env", override=False)
    except (OSError, UnicodeError):
        print("Hata: Proje kökündeki .env dosyası okunamadı.", file=sys.stderr)
        return None

    names = ("DB_HOST", "DB_PORT", "DB_SERVICE", "DB_USER", "DB_PASSWORD")
    settings = {name: os.environ.get(name, "") for name in names}
    missing = [name for name, value in settings.items() if not value.strip()]
    if missing:
        print("Hata: Eksik veya boş ortam değişkenleri: " + ", ".join(missing), file=sys.stderr)
        return None
    try:
        port = int(settings["DB_PORT"])
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        print("Hata: DB_PORT, 1 ile 65535 arasında bir tam sayı olmalıdır.", file=sys.stderr)
        return None

    try:
        params = oracledb.ConnectParams(
            host=settings["DB_HOST"], port=port, service_name=settings["DB_SERVICE"]
        )
        with oracledb.connect(
            user=settings["DB_USER"], password=settings["DB_PASSWORD"], params=params
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT SYS_CONTEXT('USERENV', 'SESSION_USER'), "
                    "SYS_CONTEXT('USERENV', 'CON_NAME') FROM DUAL"
                )
                if cursor.fetchone() != ("HR_CORE", "FREEPDB1"):
                    print("Hata: Hedef oturum HR_CORE / FREEPDB1 olmalıdır.", file=sys.stderr)
                    return None
                cursor.execute(
                    "SELECT VERSION, SCRIPT_NAME, CHECKSUM, STATUS, STARTED_AT, FINISHED_AT "
                    "FROM HR_CORE.SCHEMA_MIGRATIONS ORDER BY VERSION"
                )
                rows = cursor.fetchall()
        print("Hedef: HR_CORE / FREEPDB1")
        return rows
    except oracledb.Error as exc:
        # Ham hata metni bağlantı bilgileri içerebilir; yalnızca hata kodunu göster.
        detail = exc.args[0] if exc.args else None
        code = getattr(detail, "full_code", "")
        if not re.fullmatch(r"(?:ORA|DPY|DPI)-[0-9]+", code or ""):
            code = "ORACLE_ERROR"
        explanations = {
            "ORA-01017": "Kullanıcı adı veya parola geçersiz.",
            "ORA-28000": "Oracle kullanıcı hesabı kilitli.",
            "ORA-28001": "Oracle kullanıcı parolasının süresi dolmuş.",
            "ORA-12514": "İstenen Oracle servisi bulunamadı; DB_SERVICE ayarını kontrol edin.",
            "ORA-12541": "Oracle listener erişilemiyor; container ve bağlantı ayarlarını kontrol edin.",
            "DPY-6001": "İstenen Oracle servisi bulunamadı.",
            "DPY-6005": "Oracle sunucusuna erişilemiyor; container, ağ, host ve port ayarlarını kontrol edin.",
            "ORA-00942": "SCHEMA_MIGRATIONS tablosu bulunamadı veya tabloya erişim yetkisi yok.",
            "ORA-01031": "Migration geçmişini okumak için gerekli yetki bulunmuyor.",
            "ORA-00904": "SCHEMA_MIGRATIONS kolonları beklenen yapıyla uyuşmuyor.",
        }
        explanation = explanations.get(
            code, "Oracle bağlantısı veya salt okunur durum sorgusu başarısız oldu."
        )
        print(f"Hata ({code}): {explanation}", file=sys.stderr)
        return None


def show_status() -> int:
    print("Status komutu henüz yerel migration dosyalarıyla checksum karşılaştırması yapmaz.")
    rows = read_database_history()
    if rows is None:
        return 1
    if not rows:
        print("Migration geçmişi boş; bu normal bir durumdur.")
        return 0
    print("VERSION | SCRIPT_NAME | CHECKSUM | STATUS | STARTED_AT | FINISHED_AT")
    for version, script_name, checksum, status, started_at, finished_at in rows:
        started = started_at.isoformat(sep=" ") if started_at is not None else "-"
        finished = finished_at.isoformat(sep=" ") if finished_at is not None else "-"
        print(f"V{version:04d} | {script_name} | {checksum} | {status} | {started} | {finished}")
    return 0


@dataclass(frozen=True)
class DatabasePlanEntry:
    version: int
    script_name: str
    status: str
    database_status: str | None = None
    script_name_mismatch: bool = False
    database_script_name: str | None = None


def compare_migrations(
    migrations: list[Migration], history: list[tuple]
) -> list[DatabasePlanEntry]:
    """Yerel dosya ve geçmiş verilerinden plan üret; bağlantı veya dosya I/O yapma.

    history satırları read_database_history tarafından döndürülen altı alanı taşır.
    SUCCESS için dosya adı uyuşmazlığı, checksum durumundan bağımsız tutulur.
    """
    local: dict[int, Migration] = {}
    for migration in migrations:
        if migration.version in local:
            raise ValueError(f"Yerel veride tekrarlanan versiyon: V{migration.version:04d}")
        local[migration.version] = migration
    recorded: dict[int, tuple] = {}
    for row in history:
        version, script_name, checksum, status, started_at, finished_at = row
        if version in recorded:
            raise ValueError(f"Oracle geçmişinde tekrarlanan versiyon: V{version:04d}")
        if status not in {"SUCCESS", "FAILED", "RUNNING"}:
            raise ValueError(f"V{version:04d}: Oracle geçmişinde beklenmeyen durum değeri.")
        recorded[version] = row

    entries: list[DatabasePlanEntry] = []
    for version in sorted(local.keys() | recorded.keys()):
        migration = local.get(version)
        record = recorded.get(version)
        if migration is None:
            entries.append(DatabasePlanEntry(
                version, record[1], "ORPHANED", database_status=record[3]
            ))
        elif record is None:
            entries.append(DatabasePlanEntry(version, migration.path.name, "PENDING"))
        else:
            _, script_name, checksum, database_status, _, _ = record
            status = database_status
            name_mismatch = False
            if database_status == "SUCCESS":
                status = "APPLIED" if migration.checksum == checksum else "CHECKSUM MISMATCH"
                name_mismatch = migration.path.name != script_name
            entries.append(DatabasePlanEntry(
                version, migration.path.name, status,
                database_status=database_status,
                script_name_mismatch=name_mismatch,
                database_script_name=script_name,
            ))
    return entries


def show_database_plan(migrations: list[Migration]) -> int:
    history = read_database_history("Db-plan")
    if history is None:
        return 1
    try:
        entries = compare_migrations(migrations, history)
    except ValueError as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        return 1

    if not migrations:
        print("Henüz yerel migration dosyası bulunmuyor; bu normal bir durumdur.")
    if not entries:
        print("Oracle migration geçmişi de boş; planlanacak migration bulunmuyor.")
    print("Salt okunur karşılaştırma planı; hiçbir migration uygulanmaz.")
    totals = dict.fromkeys(
        ("PENDING", "APPLIED", "FAILED", "RUNNING", "CHECKSUM MISMATCH", "ORPHANED"), 0
    )
    name_mismatches = 0
    for entry in entries:
        totals[entry.status] += 1
        details = ""
        if entry.status == "ORPHANED":
            details = f" | Yerel dosya yok; Oracle durumu: {entry.database_status}"
        if entry.script_name_mismatch:
            name_mismatches += 1
            details += f" | SCRIPT_NAME MISMATCH: Oracle dosya adı={entry.database_script_name}"
        print(f"V{entry.version:04d} | {entry.script_name} | {entry.status}{details}")
    print("Durum toplamları: " + " | ".join(f"{name}: {count}" for name, count in totals.items()))
    print(f"SUCCESS kayıtlarında dosya adı tutarsızlığı: {name_mismatches}")
    # PENDING normaldir; tutarsızlıklar ve tamamlanmamış işlemler hata çıkışı üretir.
    return int(bool(name_mismatches or any(
        totals[name] for name in ("FAILED", "RUNNING", "CHECKSUM MISMATCH", "ORPHANED")
    )))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migration dosyalarını yerel incele veya Oracle geçmişini salt okunur göster."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="Dosya adlarını, versiyonları ve checksum'ları doğrula.")
    commands.add_parser("plan", help="Dosyaları sayısal versiyon sırasıyla ve checksum'larıyla göster.")
    commands.add_parser("status", help="Oracle migration geçmişini yalnızca SELECT ile göster.")
    commands.add_parser("db-plan", help="Yerel dosyaları Oracle geçmişiyle salt okunur karşılaştır.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "status":
        return show_status()

    try:
        migrations = collect_migrations(MIGRATIONS_DIRECTORY)
    except (OSError, ValueError) as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        return 1

    if args.command == "db-plan":
        return show_database_plan(migrations)

    if not migrations:
        print("Henüz migration dosyası bulunmuyor; bu normal bir durumdur.")
        return 0

    if args.command == "validate":
        print(f"Yerel doğrulama başarılı: {len(migrations)} migration dosyası.")
        print("SHA-256 checksum değerleri hesaplandı; kayıtlı geçmişle karşılaştırılmadı.")
    else:
        print("Yerel migration sırası (veritabanındaki uygulanma durumu bilinmiyor):")
        for migration in migrations:
            print(
                f"V{migration.version:04d} | {migration.path.name} | "
                f"SHA-256: {migration.checksum}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
