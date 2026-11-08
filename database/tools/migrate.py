"""Yerel/salt okunur komutlar ve onayla tek migration uygulayan CLI."""

import argparse
import hashlib
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIRECTORY = PROJECT_ROOT / "database" / "migrations"
MIGRATION_NAME = re.compile(r"V([0-9]{4})__([a-z][a-z0-9]*(?:_[a-z0-9]+)*)\.sql")
MIGRATION_LOCK_NAME = "enterprise-hrms:FREEPDB1:HR_CORE:migrations"


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


def load_connection_settings(load_dotenv) -> tuple[dict[str, str], int]:
    """Ayarları yükle; ortam değişkenlerini koru ve hassas değerleri yazdırma."""
    try:
        load_dotenv(PROJECT_ROOT / ".env", override=False)
    except (OSError, UnicodeError):
        raise ValueError("Proje kökündeki .env dosyası okunamadı.") from None

    names = ("DB_HOST", "DB_PORT", "DB_SERVICE", "DB_USER", "DB_PASSWORD")
    settings = {name: os.environ.get(name, "") for name in names}
    missing = [name for name, value in settings.items() if not value.strip()]
    if missing:
        raise ValueError("Eksik veya boş ortam değişkenleri: " + ", ".join(missing))
    try:
        port = int(settings["DB_PORT"])
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError("DB_PORT, 1 ile 65535 arasında bir tam sayı olmalıdır.") from None
    return settings, port


class ConnectionSetupError(ValueError):
    """Kurulum asıl hatadır; yalnızca güvenli kesinti/temizlik bilgisi taşınır."""

    def __init__(self, *, setup_interrupted: bool, cleanup_failed: bool, cleanup_interrupted: bool):
        super().__init__("Oracle bağlantısında autocommit kurulumu başarısız; işlem durduruldu.")
        self.setup_interrupted = setup_interrupted
        self.cleanup_failed = cleanup_failed
        self.cleanup_interrupted = cleanup_interrupted


def _is_interruption(error: BaseException) -> bool:
    # Exception dışındaki SystemExit, KeyboardInterrupt ve özel BaseException'lar.
    return not isinstance(error, Exception)


def open_oracle_connection(settings: dict[str, str], port: int, oracledb):
    """Thin bağlantı aç; kapatma ve transaction yaşam döngüsü çağırana aittir.

    SQL çalıştırmaz ve hedefi doğrulamaz. Kullanımdan önce
    validate_target_session() çağrılmalıdır.
    """
    if not oracledb.is_thin_mode():
        raise ValueError("Bu araç yalnızca python-oracledb Thin modunda çalışır.")
    params = oracledb.ConnectParams(
        host=settings["DB_HOST"], port=port, service_name=settings["DB_SERVICE"]
    )
    connection = oracledb.connect(
        user=settings["DB_USER"], password=settings["DB_PASSWORD"], params=params
    )
    try:
        connection.autocommit = False
    except BaseException as setup_error:
        cleanup_failed = False
        cleanup_interrupted = False
        try:
            connection.close()
        except BaseException as cleanup_error:
            cleanup_failed = True
            cleanup_interrupted = _is_interruption(cleanup_error)
        raise ConnectionSetupError(
            setup_interrupted=_is_interruption(setup_error), cleanup_failed=cleanup_failed,
            cleanup_interrupted=cleanup_interrupted) from None
    return connection


def validate_target_session(connection) -> None:
    """Verilen bağlantıyı yalnızca SELECT ile doğrula; bağlantıyı kapatma."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT SYS_CONTEXT('USERENV', 'SESSION_USER'), "
            "SYS_CONTEXT('USERENV', 'CON_NAME'), "
            "SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM DUAL"
        )
        if cursor.fetchone() != ("HR_CORE", "FREEPDB1", "HR_CORE"):
            raise ValueError("Hedef oturum HR_CORE / FREEPDB1 ve CURRENT_SCHEMA=HR_CORE olmalıdır.")


def read_history_from_connection(connection) -> list[tuple]:
    """Doğrulanmış bağlantıdan geçmişi oku; commit/rollback/kapatma yapma."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT VERSION, SCRIPT_NAME, CHECKSUM, STATUS, STARTED_AT, FINISHED_AT "
            "FROM HR_CORE.SCHEMA_MIGRATIONS ORDER BY VERSION"
        )
        return cursor.fetchall()


def read_database_history(command_label: str = "Status") -> list[tuple] | None:
    """Mevcut CLI için bağlantıyı yönet; Türkçe çıktı ve hata sözleşmesini koru."""
    try:
        import oracledb
        from dotenv import load_dotenv
    except ImportError:
        print(f"Hata: {command_label} için python-oracledb ve python-dotenv gereklidir.", file=sys.stderr)
        return None

    try:
        settings, port = load_connection_settings(load_dotenv)
        with open_oracle_connection(settings, port, oracledb) as connection:
            validate_target_session(connection)
            rows = read_history_from_connection(connection)
        print("Hedef: HR_CORE / FREEPDB1")
        return rows
    except ValueError as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        return None
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


@dataclass(frozen=True)
class ApplyEligibility:
    """Yalnızca verilen karşılaştırma anlık görüntüsüne ilişkin uygunluk kararı."""

    eligible: bool
    next_migration: DatabasePlanEntry | None
    blockers: tuple[str, ...]


def assess_apply_eligibility(entries: list[DatabasePlanEntry]) -> ApplyEligibility:
    """I/O yapmadan engelleri bul ve yalnızca en küçük uygun PENDING'i seç.

    eligible, çalıştırma izni değildir: SQL doğrulaması, güncel geçmiş, kullanıcı
    onayı ve eşzamanlılık kilidi ileride ayrıca gereklidir. Boş planda False döner.
    """
    blockers: list[str] = []
    seen: set[int] = set()
    allowed = {"PENDING", "APPLIED", "FAILED", "RUNNING", "ORPHANED", "CHECKSUM MISMATCH"}
    highest_applied = max(
        (entry.version for entry in entries
         if entry.status == "APPLIED" or entry.database_status == "SUCCESS"),
        default=None,
    )
    pending: list[DatabasePlanEntry] = []
    for entry in sorted(entries, key=lambda item: item.version):
        label = f"V{entry.version:04d}"
        if entry.version in seen:
            blockers.append(f"{label}: karşılaştırma sonucunda tekrarlanan versiyon.")
        seen.add(entry.version)
        if entry.status not in allowed:
            blockers.append(f"{label}: desteklenmeyen karşılaştırma durumu.")
        elif entry.status not in {"PENDING", "APPLIED"}:
            blockers.append(f"{label}: engelleyici durum {entry.status}.")
        if entry.script_name_mismatch:
            blockers.append(f"{label}: SCRIPT_NAME tutarsızlığı.")
        if entry.database_status in {"FAILED", "RUNNING"} and entry.status in {"PENDING", "APPLIED"}:
            blockers.append(f"{label}: Oracle durumu karşılaştırma sonucuyla tutarsız.")
        if entry.status == "PENDING":
            if entry.database_status is not None:
                blockers.append(f"{label}: PENDING için mevcut Oracle kaydı bulunmamalıdır.")
            if highest_applied is not None and entry.version < highest_applied:
                blockers.append(
                    f"{label}: uygulanmış en yüksek V{highest_applied:04d} versiyonunun gerisinde."
                )
            pending.append(entry)
    candidate = pending[0] if pending and not blockers else None
    return ApplyEligibility(candidate is not None, candidate, tuple(blockers))


@dataclass(frozen=True)
class ColumnDefinition:
    """SQL'deki kolon tanımı; PK kaynaklı nullability burada açık NOT NULL sayılmaz."""

    name: str
    data_type: str
    char_length: int | None = None
    char_semantics: str | None = None
    number_precision: int | None = None
    number_scale: int | None = None
    timestamp_precision: int | None = None
    with_time_zone: bool = False
    explicit_not_null: bool = False
    default: str | None = None
    identity: bool = False


@dataclass(frozen=True)
class PrimaryKeyDefinition:
    columns: tuple[str, ...]
    constraint_name: str | None = None


@dataclass(frozen=True)
class UniqueConstraintDefinition:
    """Dar gramerde isimli, tablo düzeyinde ve tek kolonlu UNIQUE."""

    constraint_name: str
    column_name: str


@dataclass(frozen=True)
class ForeignKeyDefinition:
    constraint_name: str
    column_name: str
    referenced_table: str
    referenced_column: str


@dataclass(frozen=True)
class ActiveCheckDefinition:
    constraint_name: str
    column_name: str = "IS_ACTIVE"


@dataclass(frozen=True)
class CreateTableDefinition:
    table_name: str
    column_definitions: tuple[ColumnDefinition, ...]
    statement: str
    primary_key: PrimaryKeyDefinition | None = None
    unique_constraints: tuple[UniqueConstraintDefinition, ...] = ()
    foreign_keys: tuple[ForeignKeyDefinition, ...] = ()
    active_checks: tuple[ActiveCheckDefinition, ...] = ()

    @property
    def columns(self) -> tuple[str, ...]:
        """Eski sıralı kolon adı erişimini tek doğruluk kaynağından üret."""
        return tuple(column.name for column in self.column_definitions)

    @property
    def primary_key_constraint_name(self) -> str | None:
        """Eski PK adı erişimini koru; isimsiz PK ile PK yokluğu modelde ayrıdır."""
        return self.primary_key.constraint_name if self.primary_key is not None else None


@dataclass(frozen=True)
class PreparedMigration:
    """Checksum ve SQL aynı değişmez dosya içeriğinden üretilir."""

    migration: Migration
    content: bytes
    text: str
    definition: CreateTableDefinition


def tokenize_create_table(text: str) -> tuple[str, ...]:
    """Statement bölmeden, dar gramerin token'larını üret; literal kabul etme."""
    tokens: list[str] = []
    position = 0
    token_pattern = re.compile(r"[A-Za-z][A-Za-z0-9_]*|[0-9]+|[(),.;]")
    while position < len(text):
        if text[position].isspace():
            position += 1
            continue
        if text.startswith("--", position):
            end = re.search(r"[\r\n]", text[position + 2:])
            position = position + 2 + end.start() if end else len(text)
            continue
        if text.startswith("/*", position):
            end = text.find("*/", position + 2)
            if end == -1 or text.startswith("/*+", position):
                raise ValueError("Kapanmamış blok yorumu veya desteklenmeyen SQL hint'i.")
            if "/*" in text[position + 2:end]:
                raise ValueError("İç içe blok yorumları desteklenmiyor.")
            position = end + 2
            continue
        match = token_pattern.match(text, position)
        if match is None:
            raise ValueError(
                "Desteklenmeyen SQL içeriği: literal, tırnaklı tanımlayıcı, "
                "SQL*Plus veya özel işaretler bu MVP'de kabul edilmez."
            )
        tokens.append(match.group().upper())
        position = match.end()
    if tokens and tokens[-1] == ";":
        tokens.pop()
    if not tokens:
        raise ValueError("Migration SQL içeriği boş veya yalnızca yorum içeriyor.")
    if ";" in tokens:
        raise ValueError("Yalnızca tek CREATE TABLE ve isteğe bağlı tek son ';' desteklenir.")
    return tuple(tokens)


def validate_create_table(text: str) -> CreateTableDefinition:
    """Saf ve tam tüketimli dar gramer; Oracle parser'ı veya yetki kontrolü değildir.

    CREATE TABLE [HR_CORE.]name (column type [NOT NULL], ...,
        [CONSTRAINT name] PRIMARY KEY (column, ...),
        CONSTRAINT name UNIQUE (column), ...)
    Constraint'ler kolonlardan sonra gelir; UNIQUE yalnız isimli ve tek kolonludur.
    FK yalnız isimli tek kolon ve HR_CORE; CHECK yalnız isimli IS_ACTIVE IN (0,1).
    DEFAULT yalnız IS_ACTIVE NUMBER(1,0) için 1 veya saat dilimli CREATED_AT için
    SYSTIMESTAMP; IDENTITY yalnız ID NUMBER(19,0) GENERATED ALWAYS AS IDENTITY.
    DEFAULT/IDENTITY tipten sonra, NOT NULL'dan önce gelir.
    Tipler: NUMBER[(p[,s])], VARCHAR2(n CHAR), DATE, CLOB,
    TIMESTAMP[(6)] [WITH TIME ZONE]. Başka clause kabul edilmez.
    """
    tokens = tokenize_create_table(text)
    position = 0

    def peek() -> str | None:
        return tokens[position] if position < len(tokens) else None

    def take(expected: str) -> None:
        nonlocal position
        if peek() != expected:
            raise ValueError(f"CREATE TABLE formatı desteklenmiyor; beklenen token: {expected}.")
        position += 1

    reserved = {
        "CREATE", "TABLE", "CONSTRAINT", "PRIMARY", "KEY", "NOT", "NULL",
        "NUMBER", "VARCHAR2", "DATE", "CLOB", "TIMESTAMP", "WITH", "TIME",
        "ZONE", "CHAR", "DEFAULT", "CHECK", "REFERENCES", "UNIQUE", "FOREIGN",
        "AS", "SELECT", "FROM", "WHERE", "ALTER", "DROP", "TRUNCATE",
        "GENERATED", "ALWAYS", "IDENTITY", "IN", "SYSTIMESTAMP",
    }

    def identifier() -> str:
        nonlocal position
        token = peek()
        if (
            token is None or re.fullmatch(r"[A-Z][A-Z0-9_]{0,29}", token) is None
            or token in reserved or token.startswith(("SYS_", "ORA_"))
        ):
            raise ValueError("Tanımlayıcı 1–30 ASCII karakterlik, tırnaksız İngilizce ad olmalıdır.")
        position += 1
        return token

    def integer(minimum: int, maximum: int) -> int:
        nonlocal position
        token = peek()
        if token is None or re.fullmatch(r"[0-9]{1,4}", token) is None:
            raise ValueError("Veri tipi boyutu desteklenen bir tam sayı olmalıdır.")
        position += 1
        value = int(token)
        if not minimum <= value <= maximum:
            raise ValueError(f"Veri tipi boyutu {minimum}–{maximum} aralığında olmalıdır.")
        return value

    take("CREATE")
    take("TABLE")
    table_name = identifier()
    if peek() == ".":
        if table_name != "HR_CORE":
            raise ValueError("Yalnızca HR_CORE şeması desteklenir.")
        take(".")
        table_name = identifier()
    if table_name == "SCHEMA_MIGRATIONS":
        raise ValueError("Migration geçmişi tablosu bu katmandan oluşturulamaz.")
    take("(")
    columns: list[str] = []
    column_definitions: list[ColumnDefinition] = []
    primary_key: list[str] = []
    primary_key_constraint_name: str | None = None
    unique_constraints: list[UniqueConstraintDefinition] = []
    constraint_names: set[str] = set()
    unique_columns: set[str] = set()
    foreign_keys: list[ForeignKeyDefinition] = []
    active_checks: list[ActiveCheckDefinition] = []
    foreign_columns: set[str] = set()
    while True:
        column = identifier()
        if column in columns:
            raise ValueError("Tekrarlanan kolon adı.")
        columns.append(column)
        kind = peek()
        char_length = None
        char_semantics = None
        number_precision = None
        number_scale = None
        timestamp_precision = None
        with_time_zone = False
        explicit_not_null = False
        if kind == "NUMBER":
            take("NUMBER")
            if peek() == "(":
                take("(")
                number_precision = integer(1, 38)
                # NUMBER(p), Oracle'da NUMBER(p,0) ile aynı mantıksal tanımdır.
                # Parametresiz NUMBER ise precision/scale=None olarak ayrı kalır.
                number_scale = 0
                if peek() == ",":
                    take(",")
                    number_scale = integer(0, number_precision)
                take(")")
        elif kind == "VARCHAR2":
            take("VARCHAR2")
            take("(")
            char_length = integer(1, 1000)
            take("CHAR")
            char_semantics = "CHAR"
            take(")")
        elif kind in {"DATE", "CLOB"}:
            take(kind)
        elif kind == "TIMESTAMP":
            take("TIMESTAMP")
            timestamp_precision = 6
            if peek() == "(":
                take("(")
                timestamp_precision = integer(6, 6)
                take(")")
            if peek() == "WITH":
                take("WITH")
                take("TIME")
                take("ZONE")
                with_time_zone = True
        else:
            raise ValueError("Desteklenmeyen kolon veri tipi.")
        default = None
        identity = False
        if peek() == "GENERATED":
            if column != "ID" or kind != "NUMBER" or number_precision != 19 or number_scale != 0:
                raise ValueError("IDENTITY yalnız ID NUMBER(19,0) için desteklenir.")
            for token in ("GENERATED", "ALWAYS", "AS", "IDENTITY"):
                take(token)
            identity = True
        elif peek() == "DEFAULT":
            take("DEFAULT")
            if column == "IS_ACTIVE" and kind == "NUMBER" and number_precision == 1 and number_scale == 0:
                take("1")
                default = "1"
            elif column == "CREATED_AT" and kind == "TIMESTAMP" and with_time_zone:
                take("SYSTIMESTAMP")
                default = "SYSTIMESTAMP"
            else:
                raise ValueError("DEFAULT yalnız IS_ACTIVE NUMBER(1,0)=1 veya CREATED_AT saat dilimli TIMESTAMP=SYSTIMESTAMP olabilir.")
        if peek() == "NOT":
            take("NOT")
            take("NULL")
            explicit_not_null = True
        column_definitions.append(ColumnDefinition(
            name=column,
            data_type=kind,
            char_length=char_length,
            char_semantics=char_semantics,
            number_precision=number_precision,
            number_scale=number_scale,
            timestamp_precision=timestamp_precision,
            with_time_zone=with_time_zone,
            explicit_not_null=explicit_not_null,
            default=default, identity=identity,
        ))
        if peek() != ",":
            break
        take(",")
        if peek() in {"CONSTRAINT", "PRIMARY"}:
            while True:
                constraint_name = None
                if peek() == "CONSTRAINT":
                    take("CONSTRAINT")
                    constraint_name = identifier()
                    if constraint_name in constraint_names:
                        raise ValueError("Tablo içinde constraint adı tekrarlanamaz.")
                    constraint_names.add(constraint_name)
                if peek() == "PRIMARY":
                    if primary_key:
                        raise ValueError("Bir tabloda yalnızca tek primary key desteklenir.")
                    primary_key_constraint_name = constraint_name
                    take("PRIMARY")
                    take("KEY")
                    take("(")
                    primary_key.append(identifier())
                    while peek() == ",":
                        take(",")
                        primary_key.append(identifier())
                    take(")")
                elif peek() == "UNIQUE" and constraint_name is not None:
                    take("UNIQUE")
                    take("(")
                    unique_column = identifier()
                    take(")")  # Virgül/composite, ifade ve seçenek kabul edilmez.
                    if unique_column not in columns or unique_column in unique_columns:
                        raise ValueError("UNIQUE kolonu tabloda bulunmalı ve UNIQUE tanımı tekrarlanmamalıdır.")
                    unique_columns.add(unique_column)
                    unique_constraints.append(UniqueConstraintDefinition(constraint_name, unique_column))
                elif peek() == "FOREIGN" and constraint_name is not None:
                    take("FOREIGN")
                    take("KEY")
                    take("(")
                    local_column = identifier()
                    take(")")
                    take("REFERENCES")
                    ref_table = identifier()
                    if peek() == ".":
                        if ref_table != "HR_CORE":
                            raise ValueError("FK yalnız HR_CORE içindeki tabloları hedefleyebilir.")
                        take(".")
                        ref_table = identifier()
                    if ref_table == "SCHEMA_MIGRATIONS":
                        raise ValueError("Migration geçmişine FK tanımlanamaz.")
                    take("(")
                    ref_column = identifier()
                    take(")")
                    if local_column not in columns or local_column in foreign_columns:
                        raise ValueError("FK kolonu tabloda bulunmalı ve yinelenmemelidir.")
                    foreign_columns.add(local_column)
                    foreign_keys.append(ForeignKeyDefinition(constraint_name, local_column, ref_table, ref_column))
                elif peek() == "CHECK" and constraint_name is not None:
                    for token in ("CHECK", "(", "IS_ACTIVE", "IN", "(", "0", ",", "1", ")", ")"):
                        take(token)
                    active_column = next((c for c in column_definitions if c.name == "IS_ACTIVE"), None)
                    if (active_checks or active_column is None or active_column.data_type != "NUMBER"
                        or active_column.number_precision != 1 or active_column.number_scale != 0):
                        raise ValueError("CHECK yalnız IS_ACTIVE NUMBER(1,0) IN (0,1) için desteklenir.")
                    active_checks.append(ActiveCheckDefinition(constraint_name))
                else:
                    raise ValueError("Desteklenmeyen tablo constraint biçimi.")
                if peek() != ",":
                    break
                take(",")
            break
    take(")")
    if position != len(tokens):
        raise ValueError("CREATE TABLE sonrasında ek statement veya desteklenmeyen clause var.")
    if len(primary_key) != len(set(primary_key)) or not set(primary_key).issubset(columns):
        raise ValueError("Primary key kolonları benzersiz olmalı ve tabloda bulunmalıdır.")
    return CreateTableDefinition(
        table_name=table_name,
        column_definitions=tuple(column_definitions),
        statement=" ".join(tokens),
        primary_key=(
            PrimaryKeyDefinition(tuple(primary_key), primary_key_constraint_name)
            if primary_key else None
        ),
        unique_constraints=tuple(unique_constraints),
        foreign_keys=tuple(foreign_keys), active_checks=tuple(active_checks),
    )


class MetadataPrecheckError(RuntimeError):
    """Salt okunur ön kontrol başarısız; belirsiz metadata yokluk sayılmaz."""


def _validate_precheck_definition(definition: CreateTableDefinition) -> None:
    """Elle oluşturulmuş veya SQL'iyle tutarsız modelden isim kabul etme."""
    try:
        validated = validate_create_table(definition.statement)
        matches = (
            definition.table_name == validated.table_name
            and definition.column_definitions == validated.column_definitions
            and definition.primary_key == validated.primary_key
            and type(definition.unique_constraints) is tuple
            and all(type(unique) is UniqueConstraintDefinition for unique in definition.unique_constraints)
            and definition.unique_constraints == validated.unique_constraints
            and type(definition.foreign_keys) is tuple and definition.foreign_keys == validated.foreign_keys
            and all(type(fk) is ForeignKeyDefinition for fk in definition.foreign_keys)
            and type(definition.active_checks) is tuple and definition.active_checks == validated.active_checks
            and all(type(check) is ActiveCheckDefinition for check in definition.active_checks)
        )
    except Exception:
        raise MetadataPrecheckError("Metadata ön kontrolü için doğrulanmış CREATE TABLE modeli gerekir.") from None
    if not matches:
        raise MetadataPrecheckError("CREATE TABLE modeli doğrulanan SQL ile tutarsız.")


def _read_precheck_count(connection, statement: str, params: dict[str, str]) -> int:
    """Yalnızca sabit dahili SELECT'ler için; bağlantı/transaction yönetimi yapma."""
    try:
        validate_target_session(connection)
        with connection.cursor() as cursor:
            cursor.execute(statement, params)
            row = cursor.fetchone()
    except Exception:
        # Ham sürücü metni bağlantı bilgileri veya SQL içerebilir.
        raise MetadataPrecheckError(
            "Hedef doğrulaması veya metadata sorgusu başarısız; nesne yok kabul edilemez."
        ) from None
    if not isinstance(row, (tuple, list)) or len(row) != 1 or type(row[0]) is not int or row[0] < 0:
        raise MetadataPrecheckError("Metadata sorgusu kesin bir sayım sonucu döndürmedi; işlem durdurulmalıdır.")
    return row[0]


def check_target_object_available(connection, definition: CreateTableDefinition) -> None:
    """HR_CORE içinde tabloyla aynı isim alanındaki nesneleri kontrol et.

    Bu yalnızca ön koşul doğrulamasıdır. DBMS_LOCK kullanmayan diğer Oracle
    oturumlarının CREATE işlemlerini engellemez; Oracle'ın CREATE sırasında
    yaptığı kontroller son otoritedir. Ayrı isim alanındaki index/trigger aynı
    adla bulunabilir. Materialized view aynı adlı iç tablo oluşturabildiği için
    ayrıca kapsanır; yalnızca USER_TABLES kontrolü yeterli değildir.
    """
    _validate_precheck_definition(definition)
    count = _read_precheck_count(
        connection,
        "SELECT COUNT(*) FROM ALL_OBJECTS "
        "WHERE OWNER = :owner AND OBJECT_NAME = :object_name "
        "AND OBJECT_TYPE IN ('TABLE', 'VIEW', 'SEQUENCE', 'SYNONYM', 'PACKAGE', "
        "'PROCEDURE', 'FUNCTION', 'OPERATOR', 'TYPE', 'MATERIALIZED VIEW')",
        {"owner": "HR_CORE", "object_name": definition.table_name},
    )
    if count:
        raise MetadataPrecheckError("HR_CORE içinde hedef tablo adıyla çakışan bir nesne mevcut.")


def check_primary_key_name_available(connection, definition: CreateTableDefinition) -> None:
    """Açık PK constraint adı varsa HR_CORE constraint isim alanını kontrol et.

    Bu sorgu ismi rezerve etmez; diğer oturumlarla yarış CREATE anına kadar sürer.
    Ad belirtilmemişse Oracle'ın üreteceği isim tahmin edilmez ve sorgulanmaz.
    """
    _validate_precheck_definition(definition)
    name = definition.primary_key_constraint_name
    if name is None:
        return
    count = _read_precheck_count(
        connection,
        "SELECT COUNT(*) FROM ALL_CONSTRAINTS "
        "WHERE OWNER = :owner AND CONSTRAINT_NAME = :constraint_name",
        {"owner": "HR_CORE", "constraint_name": name},
    )
    if count:
        raise MetadataPrecheckError("HR_CORE içinde belirtilen primary key constraint adı zaten kullanılıyor.")


def check_unique_constraint_names_available(connection, definition: CreateTableDefinition) -> None:
    """Uyumlu API: UNIQUE/FK/CHECK açık adlarını şema çapında kontrol et; rezerve etmez."""
    _validate_precheck_definition(definition)
    for unique in (*definition.unique_constraints, *definition.foreign_keys, *definition.active_checks):
        count = _read_precheck_count(
            connection,
            "SELECT COUNT(*) FROM ALL_CONSTRAINTS "
            "WHERE OWNER = :owner AND CONSTRAINT_NAME = :constraint_name",
            {"owner": "HR_CORE", "constraint_name": unique.constraint_name},
        )
        if count:
            raise MetadataPrecheckError("HR_CORE içinde belirtilen constraint adı zaten kullanılıyor.")


def validate_create_table_preconditions(connection, definition: CreateTableDefinition) -> None:
    """Mevcut bağlantıyla salt okunur ön kontroller; transaction yönetmez."""
    check_target_object_available(connection, definition)
    check_primary_key_name_available(connection, definition)
    check_unique_constraint_names_available(connection, definition)


@dataclass(frozen=True)
class OracleColumnMetadata:
    """Oracle kolon alanları; beklenen SQL tanımına dönüştürülmeden korunur."""

    column_name: str
    column_id: int
    data_type: str
    data_precision: int | None
    data_scale: int | None
    char_length: int | None
    char_used: str | None
    nullable: str


@dataclass(frozen=True)
class TableColumnMetadata:
    """Mantıksal kolon görüntüsü; nesnenin TABLE olduğunu tek başına kanıtlamaz."""

    owner: str
    table_name: str
    columns: tuple[OracleColumnMetadata, ...]


class ColumnMetadataReadError(RuntimeError):
    """Okuma veya metadata biçimi hatası; yapı uyuşmazlığı kararı değildir."""


def _parse_column_metadata_rows(rows) -> tuple[OracleColumnMetadata, ...]:
    """Saf biçim doğrulaması; beklenen kolon/tip/nullability karşılaştırması yapma."""
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ColumnMetadataReadError("Kolon metadata sonucu boş veya geçersiz; tablo yapısı doğrulanamaz.")
    columns: list[OracleColumnMetadata] = []
    names: set[str] = set()
    ids: set[int] = set()
    previous_id = 0
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 8:
            raise ColumnMetadataReadError("Kolon metadata satırı beklenen sekiz alanı içermiyor.")
        name, column_id, data_type, precision, scale, char_length, char_used, nullable = row
        if (
            not isinstance(name, str) or not name or len(name) > 128
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
        ):
            raise ColumnMetadataReadError("COLUMN_NAME alanı geçersiz.")
        if type(column_id) is not int or column_id < 1:
            raise ColumnMetadataReadError("COLUMN_ID pozitif bir tam sayı olmalıdır.")
        if name in names or column_id in ids:
            raise ColumnMetadataReadError("Metadata sonucunda yinelenen kolon adı veya COLUMN_ID var.")
        if column_id <= previous_id:
            raise ColumnMetadataReadError("Kolon metadata sonucu COLUMN_ID sırasını korumuyor.")
        # Yalnızca metin biçimi kontrol edilir. Örneğin BLOB okunabilir, fakat
        # ilerideki karşılaştırıcı onu beklenen CLOB ile eşdeğer kabul etmemelidir.
        # Timestamp hassasiyetini DATA_TYPE'tan burada türetmeyiz; DATA_SCALE dahil
        # Oracle'ın döndürdüğü değerler aynen saklanır. NULL alanlar sıfırlanmaz.
        if (
            not isinstance(data_type, str) or len(data_type) > 128
            or re.fullmatch(
                r"[A-Z][A-Z0-9_]*(?:\([0-9]+\))?(?: [A-Z][A-Z0-9_]*(?:\([0-9]+\))?)*",
                data_type,
            ) is None
        ):
            raise ColumnMetadataReadError("DATA_TYPE alanı geçersiz veya desteklenmeyen metadata biçiminde.")
        if precision is not None and (type(precision) is not int or precision < 1):
            raise ColumnMetadataReadError("DATA_PRECISION alanı geçersiz.")
        if scale is not None and type(scale) is not int:
            raise ColumnMetadataReadError("DATA_SCALE alanı geçersiz.")
        if char_length is not None and (type(char_length) is not int or char_length < 0):
            raise ColumnMetadataReadError("CHAR_LENGTH alanı geçersiz.")
        if char_used is not None and char_used not in ("B", "C"):
            raise ColumnMetadataReadError("CHAR_USED alanı geçersiz.")
        if nullable not in ("Y", "N"):
            raise ColumnMetadataReadError("NULLABLE alanı geçersiz.")
        columns.append(OracleColumnMetadata(
            name, column_id, data_type, precision, scale, char_length, char_used, nullable
        ))
        names.add(name)
        ids.add(column_id)
        previous_id = column_id
    return tuple(columns)


def read_table_column_metadata(connection, table_name: str) -> TableColumnMetadata:
    """Mevcut oturumdan HR_CORE kolonlarını oku; transaction/bağlantı yönetme.

    Beklenen SQL modeliyle karşılaştırmaz; PK/constraint veya nesne türü sorgusu
    yapmaz. ALL_TAB_COLUMNS sistem üretimi gizli kolonları filtreler; bu görüntü
    fiziksel kolon envanteri veya eşzamanlı DDL'ye karşı atomik şema görüntüsü
    değildir. COLUMN_ID boşlukları kabul edilir, sıra/benzersizlik zorunludur.
    """
    if not isinstance(table_name, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,29}", table_name) is None:
        raise ColumnMetadataReadError("Metadata için doğrulanmış, tırnaksız bir tablo adı gerekir.")
    try:
        validate_target_session(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COLUMN_NAME, COLUMN_ID, DATA_TYPE, DATA_PRECISION, DATA_SCALE, "
                "CHAR_LENGTH, CHAR_USED, NULLABLE FROM ALL_TAB_COLUMNS "
                "WHERE OWNER = :owner AND TABLE_NAME = :table_name ORDER BY COLUMN_ID",
                {"owner": "HR_CORE", "table_name": table_name},
            )
            rows = cursor.fetchall()
    except Exception:
        # Ham Oracle/driver mesajı, SQL veya bind değerleri kullanıcıya taşınmaz.
        raise ColumnMetadataReadError(
            "Hedef doğrulaması veya kolon metadata okuması başarısız; sonuç belirsiz."
        ) from None
    return TableColumnMetadata("HR_CORE", table_name, _parse_column_metadata_rows(rows))


@dataclass(frozen=True)
class OraclePrimaryKeyMetadata:
    """Tek PK görüntüsü; kolon tuple'ının sırası Oracle POSITION sırasıdır."""

    owner: str
    table_name: str
    constraint_name: str
    columns: tuple[str, ...]


class PrimaryKeyMetadataReadError(RuntimeError):
    """PK yokluğundan farklı olarak okuma/biçim hatasını temsil eder."""


def _valid_pk_metadata_name(value) -> bool:
    """Oracle üretimi SYS_ adlarını ve harf duyarlılığını değiştirmeden kabul et."""
    return (
        isinstance(value, str) and 0 < len(value) <= 128
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _parse_primary_key_constraint(rows, table_name: str) -> str | None:
    """Sorgu sonucunda hiç PK olmaması ile bozuk/çoklu PK sonucunu ayır."""
    if not isinstance(rows, (tuple, list)):
        raise PrimaryKeyMetadataReadError("PK constraint metadata sonucu geçersiz.")
    if not rows:
        return None
    if len(rows) != 1:
        raise PrimaryKeyMetadataReadError("Bir tablo için birden fazla PK constraint kaydı döndü.")
    row = rows[0]
    if not isinstance(row, (tuple, list)) or len(row) != 3:
        raise PrimaryKeyMetadataReadError("PK constraint metadata satırı bozuk veya eksik.")
    owner, actual_table, constraint_name = row
    if owner != "HR_CORE" or actual_table != table_name or not _valid_pk_metadata_name(constraint_name):
        raise PrimaryKeyMetadataReadError("PK constraint kimliği veya hedef kapsamı geçersiz.")
    return constraint_name


def _parse_primary_key_columns(
    rows, table_name: str, constraint_name: str
) -> tuple[str, ...]:
    """PK varsa kolonları zorunludur; sonuç sessizce sıralanmaz/düzeltilmez."""
    if not isinstance(rows, (tuple, list)) or not rows:
        raise PrimaryKeyMetadataReadError("PK constraint mevcut ancak kolon metadata sonucu boş veya geçersiz.")
    columns: list[str] = []
    positions: set[int] = set()
    names: set[str] = set()
    for expected_position, row in enumerate(rows, start=1):
        if not isinstance(row, (tuple, list)) or len(row) != 5:
            raise PrimaryKeyMetadataReadError("PK kolon metadata satırı bozuk veya eksik.")
        owner, actual_table, actual_constraint, name, position = row
        if owner != "HR_CORE" or actual_table != table_name or actual_constraint != constraint_name:
            raise PrimaryKeyMetadataReadError("PK kolon kaydı beklenen constraint veya hedef kapsamıyla uyuşmuyor.")
        if not _valid_pk_metadata_name(name):
            raise PrimaryKeyMetadataReadError("PK kolon adı geçersiz.")
        if type(position) is not int or position < 1:
            raise PrimaryKeyMetadataReadError("PK POSITION pozitif bir tam sayı olmalıdır.")
        if position in positions or name in names:
            raise PrimaryKeyMetadataReadError("PK kolon adı veya POSITION tekrarlanıyor.")
        if position != expected_position:
            raise PrimaryKeyMetadataReadError("PK POSITION 1'den başlayıp sıralı ve kesintisiz ilerlemelidir.")
        columns.append(name)
        positions.add(position)
        names.add(name)
    return tuple(columns)


def read_table_primary_key_metadata(connection, table_name: str) -> OraclePrimaryKeyMetadata | None:
    """Mevcut HR_CORE oturumunda PK oku; beklenen SQL modeliyle karşılaştırma.

    None yalnızca başarılı constraint sorgusunda sıfır PK satırı varsa döner.
    Bu, tablonun mevcut olduğunu kanıtlamaz; tablo/kolon görüntüsüyle birlikte
    kullanılmalıdır. İki sorgu atomik şema görüntüsü sağlamaz. İkinci sorguda
    constraint yeniden P olarak eşleştirilir; kaybolmuş/değişmiş PK nedeniyle
    eksik kolon sonucu hata sayılır. Diğer oturumların DDL'si engellenmez.
    Commit, rollback, bağlantı kapatma veya yeni bağlantı açma yapılmaz.
    """
    if not isinstance(table_name, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,29}", table_name) is None:
        raise PrimaryKeyMetadataReadError("PK metadata için doğrulanmış, tırnaksız tablo adı gerekir.")
    try:
        validate_target_session(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT OWNER, TABLE_NAME, CONSTRAINT_NAME FROM ALL_CONSTRAINTS "
                "WHERE OWNER = :owner AND TABLE_NAME = :table_name AND CONSTRAINT_TYPE = 'P'",
                {"owner": "HR_CORE", "table_name": table_name},
            )
            constraint_name = _parse_primary_key_constraint(cursor.fetchall(), table_name)
            if constraint_name is None:
                return None
            cursor.execute(
                "SELECT c.OWNER, c.TABLE_NAME, c.CONSTRAINT_NAME, c.COLUMN_NAME, c.POSITION "
                "FROM ALL_CONS_COLUMNS c JOIN ALL_CONSTRAINTS p "
                "ON p.OWNER = c.OWNER AND p.CONSTRAINT_NAME = c.CONSTRAINT_NAME "
                "AND p.TABLE_NAME = c.TABLE_NAME "
                "WHERE p.OWNER = :owner AND p.TABLE_NAME = :table_name "
                "AND p.CONSTRAINT_NAME = :constraint_name AND p.CONSTRAINT_TYPE = 'P' "
                "ORDER BY c.POSITION",
                {"owner": "HR_CORE", "table_name": table_name, "constraint_name": constraint_name},
            )
            columns = _parse_primary_key_columns(cursor.fetchall(), table_name, constraint_name)
    except PrimaryKeyMetadataReadError:
        raise
    except Exception:
        raise PrimaryKeyMetadataReadError(
            "Hedef doğrulaması veya PK metadata okuması başarısız; PK yok kabul edilemez."
        ) from None
    return OraclePrimaryKeyMetadata("HR_CORE", table_name, constraint_name, columns)


@dataclass(frozen=True)
class OracleColumnProperties:
    column_name: str
    column_id: int | None
    default_length: int | None
    hidden: str
    virtual: str
    identity: str
    user_generated: str
    default_expression: str | None = field(default=None, repr=False)
    default_on_null: str | None = None
    identity_generation: str | None = None
    identity_sequence: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class OracleForeignKeyReference:
    owner: str
    table_name: str
    constraint_name: str
    constraint_type: str
    columns: tuple[str, ...]
    delete_rule: str
    status: str
    validated: str


@dataclass(frozen=True)
class OracleConstraintMetadata:
    constraint_name: str
    constraint_type: str
    status: str
    validated: str
    deferrable: str
    deferred: str
    # CHECK metni repr/log çıktısına taşınmaz.
    condition: str | None = field(repr=False)
    # None: bu türe ait kolon görüntüsü yok; U için eksik bilgi MATCH olamaz.
    columns: tuple[str, ...] | None = None
    reference: OracleForeignKeyReference | None = None


@dataclass(frozen=True)
class TableSafetyMetadata:
    owner: str
    table_name: str
    object_types: tuple[tuple[str, str], ...]
    temporary: str
    iot_type: str | None
    nested: str
    secondary: str
    column_properties: tuple[OracleColumnProperties, ...]
    constraints: tuple[OracleConstraintMetadata, ...]


class TableSafetyMetadataReadError(RuntimeError):
    """Eksik/bozuk güvenlik görüntüsü; nesne yokluğu veya MATCH değildir."""


def _validate_table_safety_metadata(metadata: TableSafetyMetadata) -> None:
    if (type(metadata) is not TableSafetyMetadata
        or not _valid_pk_metadata_name(metadata.owner)
        or not _valid_pk_metadata_name(metadata.table_name)
        or type(metadata.object_types) is not tuple or not metadata.object_types
        or metadata.temporary not in ("Y", "N")
        or metadata.iot_type not in (None, "IOT", "IOT_OVERFLOW", "IOT_MAPPING")
        or metadata.nested not in ("YES", "NO") or metadata.secondary not in ("Y", "N")
        or type(metadata.column_properties) is not tuple or not metadata.column_properties
        or type(metadata.constraints) is not tuple):
        raise TableSafetyMetadataReadError("Tablo güvenlik metadata biçimi geçersiz.")
    object_types = set()
    for row in metadata.object_types:
        if (type(row) is not tuple or len(row) != 2
            or not _valid_pk_metadata_name(row[0]) or row[1] not in ("VALID", "INVALID", "N/A")
            or row[0] in object_types):
            raise TableSafetyMetadataReadError("Nesne metadata sonucu geçersiz.")
        object_types.add(row[0])
    names, ids = set(), set()
    previous_id = 0
    for column in metadata.column_properties:
        if (type(column) is not OracleColumnProperties
            or not _valid_pk_metadata_name(column.column_name) or column.column_name in names
            or (column.column_id is not None and
                (type(column.column_id) is not int or column.column_id < 1 or column.column_id in ids))
            or (column.default_length is not None and
                (type(column.default_length) is not int or column.default_length < 0))
            or any(value not in ("YES", "NO") for value in
                   (column.hidden, column.virtual, column.identity, column.user_generated))):
            raise TableSafetyMetadataReadError("Kolon özellik metadata sonucu geçersiz.")
        if column.default_length is not None:
            if (type(column.default_expression) is not str or not column.default_expression.strip()
                or len(column.default_expression) > 4000 or column.default_on_null not in ("YES", "NO")):
                raise TableSafetyMetadataReadError("DEFAULT metadata görüntüsü eksik veya bozuk.")
        elif column.default_expression is not None or column.default_on_null is not None:
            raise TableSafetyMetadataReadError("DEFAULT metadata görüntüleri çelişiyor.")
        if column.identity == "YES":
            if (column.identity_generation not in ("ALWAYS", "BY DEFAULT")
                or type(column.identity_sequence) is not str
                or re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", column.identity_sequence) is None
                or column.default_length is None):
                raise TableSafetyMetadataReadError("IDENTITY metadata görüntüsü eksik veya bozuk.")
            # Ayrı dictionary görüntüleri aynı sequence'i göstermelidir. Bu
            # çelişki beklenen SQL'e karşı kesin fark veya FAILED kanıtı değildir.
            if not _identity_default_matches(column):
                raise TableSafetyMetadataReadError("IDENTITY sequence ve DEFAULT görüntüleri çelişiyor.")
        elif column.identity_generation is not None or column.identity_sequence is not None:
            raise TableSafetyMetadataReadError("IDENTITY metadata görüntüleri çelişiyor.")
        if column.column_id is not None:
            if column.column_id <= previous_id:
                raise TableSafetyMetadataReadError("Kolon özellik sırası geçersiz.")
            ids.add(column.column_id)
            previous_id = column.column_id
        names.add(column.column_name)
    names = set()
    for constraint in metadata.constraints:
        if (type(constraint) is not OracleConstraintMetadata
            or not _valid_pk_metadata_name(constraint.constraint_name)
            or constraint.constraint_name in names
            or constraint.constraint_type not in ("P", "C", "U", "R", "V", "O", "H", "F", "S")
            or constraint.status not in ("ENABLED", "DISABLED")
            or constraint.validated not in ("VALIDATED", "NOT VALIDATED")
            or constraint.deferrable not in ("DEFERRABLE", "NOT DEFERRABLE")
            or constraint.deferred not in ("DEFERRED", "IMMEDIATE")
            or (constraint.condition is not None and type(constraint.condition) is not str)
            or (constraint.constraint_type == "C" and not constraint.condition)
            or (constraint.constraint_type != "C" and constraint.condition is not None)):
            raise TableSafetyMetadataReadError("Constraint metadata sonucu geçersiz.")
        if constraint.constraint_type in ("U", "R"):
            if (type(constraint.columns) is not tuple or not constraint.columns
                or any(not _valid_pk_metadata_name(name) for name in constraint.columns)
                or len(set(constraint.columns)) != len(constraint.columns)):
                raise TableSafetyMetadataReadError("Constraint kolon metadata bilgisi eksik veya bozuk.")
        elif constraint.columns is not None:
            raise TableSafetyMetadataReadError("Constraint türü ile kolon metadata bilgisi çelişiyor.")
        if constraint.constraint_type == "R":
            ref = constraint.reference
            if (type(ref) is not OracleForeignKeyReference or ref.owner != "HR_CORE"
                or not _valid_pk_metadata_name(ref.table_name) or not _valid_pk_metadata_name(ref.constraint_name)
                or ref.constraint_type not in ("P", "U") or type(ref.columns) is not tuple
                or len(ref.columns) != len(constraint.columns) or not ref.columns
                or any(not _valid_pk_metadata_name(c) for c in ref.columns)
                or len(set(ref.columns)) != len(ref.columns)
                or ref.delete_rule not in ("NO ACTION", "CASCADE", "SET NULL")
                or ref.status not in ("ENABLED", "DISABLED") or ref.validated not in ("VALIDATED", "NOT VALIDATED")):
                raise TableSafetyMetadataReadError("FK referans metadata bilgisi eksik veya bozuk.")
        elif constraint.reference is not None:
            raise TableSafetyMetadataReadError("Constraint türü ile referans metadata bilgisi çelişiyor.")
        names.add(constraint.constraint_name)


def _parse_unique_constraint_columns(rows, table_name: str, names: set[str]) -> dict[str, tuple[str, ...]]:
    """U kayıtlarının tam kolon eşlemesi; eksik/ek kapsam ve POSITION hataları reddedilir."""
    if not isinstance(rows, (tuple, list)):
        raise TableSafetyMetadataReadError("UNIQUE kolon metadata sonucu geçersiz.")
    grouped: dict[str, list[str]] = {name: [] for name in names}
    previous_key = None
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 5:
            raise TableSafetyMetadataReadError("UNIQUE kolon metadata satırı bozuk.")
        owner, actual_table, constraint, column, position = row
        if (owner != "HR_CORE" or actual_table != table_name or not _valid_pk_metadata_name(constraint)
            or constraint not in grouped or not _valid_pk_metadata_name(column)
            or type(position) is not int or position < 1):
            raise TableSafetyMetadataReadError("UNIQUE kolon kapsamı veya POSITION geçersiz.")
        key = (constraint, position)
        if previous_key is not None and key <= previous_key:
            raise TableSafetyMetadataReadError("UNIQUE kolon metadata sırası veya benzersizliği geçersiz.")
        columns = grouped[constraint]
        if position != len(columns) + 1 or column in columns:
            raise TableSafetyMetadataReadError("UNIQUE kolon POSITION sırası veya kolonları tutarsız.")
        columns.append(column)
        previous_key = key
    if any(not columns for columns in grouped.values()):
        raise TableSafetyMetadataReadError("UNIQUE mevcut ancak kolon metadata bilgisi eksik.")
    return {name: tuple(columns) for name, columns in grouped.items()}


def _read_foreign_key_metadata(connection, table_name: str, constraints: tuple) -> tuple:
    """Yalnız HR_CORE kaynak/hedef; eksik join satırı FK yokluğu sayılmaz."""
    names = {c.constraint_name for c in constraints if c.constraint_type == "R"}
    if not names:
        return constraints
    if any(not _valid_pk_metadata_name(name) for name in names):
        raise TableSafetyMetadataReadError("FK constraint kimliği geçersiz.")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.OWNER, c.TABLE_NAME, c.CONSTRAINT_NAME, c.COLUMN_NAME, c.POSITION, "
            "r.OWNER, r.TABLE_NAME, r.CONSTRAINT_NAME, r.CONSTRAINT_TYPE, rc.COLUMN_NAME, "
            "rc.POSITION, f.DELETE_RULE, r.STATUS, r.VALIDATED, "
            "(SELECT COUNT(*) FROM ALL_CONS_COLUMNS k WHERE k.OWNER=r.OWNER "
            "AND k.TABLE_NAME=r.TABLE_NAME AND k.CONSTRAINT_NAME=r.CONSTRAINT_NAME) "
            "FROM ALL_CONSTRAINTS f JOIN ALL_CONS_COLUMNS c "
            "ON c.OWNER=f.OWNER AND c.TABLE_NAME=f.TABLE_NAME AND c.CONSTRAINT_NAME=f.CONSTRAINT_NAME "
            "JOIN ALL_CONSTRAINTS r ON r.OWNER=f.R_OWNER AND r.CONSTRAINT_NAME=f.R_CONSTRAINT_NAME "
            "JOIN ALL_CONS_COLUMNS rc ON rc.OWNER=r.OWNER AND rc.TABLE_NAME=r.TABLE_NAME "
            "AND rc.CONSTRAINT_NAME=r.CONSTRAINT_NAME AND rc.POSITION=c.POSITION "
            "WHERE f.OWNER=:owner AND f.TABLE_NAME=:table_name AND f.CONSTRAINT_TYPE='R' "
            "AND r.OWNER=:owner ORDER BY c.CONSTRAINT_NAME, c.POSITION",
            {"owner": "HR_CORE", "table_name": table_name})
        rows = cursor.fetchall()
    if not isinstance(rows, (list, tuple)):
        raise TableSafetyMetadataReadError("FK metadata sonucu bozuk.")
    groups = {name: [] for name in names}
    references = {}
    previous = None
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 15:
            raise TableSafetyMetadataReadError("FK metadata satırı bozuk.")
        owner, table, name, column, pos, ro, rt, rn, kind, rcol, rpos, rule, status, validated, ref_count = row
        if (owner != "HR_CORE" or table != table_name or name not in groups
            or ro != "HR_CORE" or kind not in ("P", "U")
            or any(not _valid_pk_metadata_name(v) for v in (column, rt, rn, rcol))
            or type(pos) is not int or type(rpos) is not int or pos != rpos or pos < 1
            or type(ref_count) is not int or ref_count < 1
            or pos != len(groups[name]) + 1 or column in groups[name]
            or rule not in ("NO ACTION", "CASCADE", "SET NULL")
            or status not in ("ENABLED", "DISABLED") or validated not in ("VALIDATED", "NOT VALIDATED")):
            raise TableSafetyMetadataReadError("FK metadata kapsamı veya sırası tutarsız.")
        key = (name, pos)
        if previous is not None and key <= previous:
            raise TableSafetyMetadataReadError("FK metadata sırası tutarsız.")
        previous = key
        ref = (ro, rt, rn, kind, rule, status, validated, ref_count)
        if name in references and references[name][0] != ref:
            raise TableSafetyMetadataReadError("FK referans görüntüsü çelişkili.")
        if name not in references:
            references[name] = (ref, [])
        if rcol in references[name][1]:
            raise TableSafetyMetadataReadError("FK referans kolonu yineleniyor.")
        groups[name].append(column)
        references[name][1].append(rcol)
    if any(not group for group in groups.values()):
        raise TableSafetyMetadataReadError("FK mevcut ancak referans kolon metadata bilgisi eksik.")
    result = []
    for constraint in constraints:
        if constraint.constraint_type == "R":
            ref, columns = references[constraint.constraint_name]
            ro, rt, rn, kind, rule, status, validated, ref_count = ref
            if ref_count != len(columns):
                raise TableSafetyMetadataReadError("FK referans anahtarının kolon görüntüsü eksik.")
            constraint = replace(constraint, columns=tuple(groups[constraint.constraint_name]),
                reference=OracleForeignKeyReference(ro, rt, rn, kind, tuple(columns), rule, status, validated))
        result.append(constraint)
    return tuple(result)


def _read_column_value_metadata(connection, table_name: str, properties: tuple) -> tuple:
    """DEFAULT LONG yalnız str olarak alınır; LOB.read veya str(driver_value) yok.

    İç sequence adı ALL_TAB_IDENTITY_COLS ile eşleştirilir; salt SYS_ adı
    IDENTITY kanıtı değildir. Bu yeni dictionary biçimleri canlı doğrulanmalıdır.
    """
    defaults = {p.column_name for p in properties if p.default_length is not None}
    identities = {p.column_name for p in properties if p.identity == "YES"}
    if not defaults and not identities:
        return properties
    params = {"owner": "HR_CORE", "table_name": table_name}
    values = {}
    identity_values = {}
    with connection.cursor() as cursor:
        if defaults:
            cursor.execute(
                "SELECT COLUMN_NAME, DATA_DEFAULT, DEFAULT_ON_NULL FROM ALL_TAB_COLUMNS "
                "WHERE OWNER=:owner AND TABLE_NAME=:table_name AND DEFAULT_LENGTH IS NOT NULL "
                "ORDER BY COLUMN_ID", params)
            rows = cursor.fetchall()
            if not isinstance(rows, (tuple, list)):
                raise TableSafetyMetadataReadError("DEFAULT metadata sonucu bozuk.")
            for row in rows:
                if not isinstance(row, (tuple, list)) or len(row) != 3:
                    raise TableSafetyMetadataReadError("DEFAULT metadata satırı bozuk.")
                name, expression, on_null = row
                if (name not in defaults or name in values or type(expression) is not str
                    or not expression.strip() or len(expression) > 4000 or on_null not in ("YES", "NO")):
                    raise TableSafetyMetadataReadError("DEFAULT metadata eksik veya çelişkili.")
                values[name] = (expression, on_null)
            if set(values) != defaults:
                raise TableSafetyMetadataReadError("DEFAULT kolon görüntüleri çelişiyor.")
        if identities:
            cursor.execute(
                "SELECT COLUMN_NAME, GENERATION_TYPE, SEQUENCE_NAME FROM ALL_TAB_IDENTITY_COLS "
                "WHERE OWNER=:owner AND TABLE_NAME=:table_name ORDER BY COLUMN_NAME", params)
            rows = cursor.fetchall()
            if not isinstance(rows, (tuple, list)):
                raise TableSafetyMetadataReadError("IDENTITY metadata sonucu bozuk.")
            for row in rows:
                if not isinstance(row, (tuple, list)) or len(row) != 3:
                    raise TableSafetyMetadataReadError("IDENTITY metadata satırı bozuk.")
                name, generation, sequence = row
                if (name not in identities or name in identity_values
                    or generation not in ("ALWAYS", "BY DEFAULT") or type(sequence) is not str
                    or re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", sequence) is None):
                    raise TableSafetyMetadataReadError("IDENTITY metadata eksik veya çelişkili.")
                identity_values[name] = (generation, sequence)
            if set(identity_values) != identities:
                raise TableSafetyMetadataReadError("IDENTITY kolon görüntüleri çelişiyor.")
    return tuple(replace(p,
        default_expression=values.get(p.column_name, (None, None))[0],
        default_on_null=values.get(p.column_name, (None, None))[1],
        identity_generation=identity_values.get(p.column_name, (None, None))[0],
        identity_sequence=identity_values.get(p.column_name, (None, None))[1]) for p in properties)


def _normalized_default(expression: str | None) -> str | None:
    if type(expression) is not str:
        return None
    value = expression.strip()
    if value == "1":
        return "1"
    if value.upper() == "SYSTIMESTAMP":
        return "SYSTIMESTAMP"
    return None


def _identity_default_matches(column: OracleColumnProperties) -> bool:
    if type(column.default_expression) is not str or type(column.identity_sequence) is not str:
        return False
    # Quoted sequence ve owner büyük/küçük harfi korunur; yalnız NEXTVAL keyword'ü case-insensitive.
    text = column.default_expression.strip()
    sequence = re.escape(column.identity_sequence)
    return re.fullmatch(r'(?:"HR_CORE"\."' + sequence + r'"|HR_CORE\.' + sequence + r')\.(?i:NEXTVAL)', text) is not None


def _is_active_check(condition: str | None) -> bool:
    """Sadece sabit IS_ACTIVE IN (0,1); literal, OR, ek ifade ve tırnak case-fold yok."""
    if type(condition) is not str or len(condition) > 1000:
        return False
    # Mevcut NOT NULL okuyucusu gibi dengeli güvenli dış parantezler dışında normalizasyon yok.
    text = condition.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        closes_at_end = True
        for index, ch in enumerate(text):
            depth += (ch == "(") - (ch == ")")
            if depth == 0 and index != len(text) - 1:
                closes_at_end = False
                break
            if depth < 0:
                return False
        if not closes_at_end or depth != 0:
            break
        text = text[1:-1].strip()
    return re.fullmatch(r'(?:IS_ACTIVE|"IS_ACTIVE")\s+(?i:IN)\s*\(\s*0\s*,\s*1\s*\)', text) is not None


def read_table_safety_metadata(connection, table_name: str) -> TableSafetyMetadata:
    """Aynı bağlantıda SELECT; transaction sahibi değildir, atomik görüntü sağlamaz.

    DEFAULT varsa DATA_DEFAULT (LONG) kontrollü ek SELECT ile yalnız str olarak
    okunur; ifade çalıştırılmaz, LOB nesnesi okunmaz veya metne çevrilmez. Bilinmeyen türler fail-closed.
    İç kolon ve dictionary bayrakları gerçek 26ai üzerinde ayrıca doğrulanmalıdır.
    """
    if not isinstance(table_name, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,29}", table_name) is None:
        raise TableSafetyMetadataReadError("Doğrulanmış tablo adı gerekir.")
    params = {"owner": "HR_CORE", "table_name": table_name}
    try:
        validate_target_session(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT OBJECT_TYPE, STATUS FROM ALL_OBJECTS "
                "WHERE OWNER = :owner AND OBJECT_NAME = :table_name "
                "AND SUBOBJECT_NAME IS NULL AND OBJECT_TYPE IN "
                "('TABLE', 'VIEW', 'MATERIALIZED VIEW', 'SYNONYM') ORDER BY OBJECT_TYPE", params)
            objects = cursor.fetchall()
            cursor.execute(
                "SELECT TEMPORARY, IOT_TYPE, NESTED, SECONDARY FROM ALL_TABLES "
                "WHERE OWNER = :owner AND TABLE_NAME = :table_name", params)
            tables = cursor.fetchall()
            cursor.execute(
                "SELECT COLUMN_NAME, COLUMN_ID, DEFAULT_LENGTH, HIDDEN_COLUMN, "
                "VIRTUAL_COLUMN, IDENTITY_COLUMN, USER_GENERATED FROM ALL_TAB_COLS "
                "WHERE OWNER = :owner AND TABLE_NAME = :table_name "
                "ORDER BY COLUMN_ID NULLS LAST, INTERNAL_COLUMN_ID", params)
            properties = cursor.fetchall()
            cursor.execute(
                "SELECT CONSTRAINT_NAME, CONSTRAINT_TYPE, STATUS, VALIDATED, "
                "DEFERRABLE, DEFERRED, SEARCH_CONDITION_VC FROM ALL_CONSTRAINTS "
                "WHERE OWNER = :owner AND TABLE_NAME = :table_name ORDER BY CONSTRAINT_NAME", params)
            constraints = cursor.fetchall()
            if (not isinstance(constraints, (tuple, list))
                or any(not isinstance(row, (tuple, list)) or len(row) != 7 for row in constraints)):
                raise TableSafetyMetadataReadError("Constraint metadata satırları bozuk.")
            models = tuple(OracleConstraintMetadata(*row) for row in constraints)
            unique_names = {c.constraint_name for c in models if c.constraint_type == "U"}
            if unique_names:
                if (any(not _valid_pk_metadata_name(name) for name in unique_names)
                    or len(unique_names) != sum(c.constraint_type == "U" for c in models)):
                    raise TableSafetyMetadataReadError("UNIQUE constraint kimlikleri geçersiz.")
                cursor.execute(
                    "SELECT c.OWNER, c.TABLE_NAME, c.CONSTRAINT_NAME, c.COLUMN_NAME, c.POSITION "
                    "FROM ALL_CONS_COLUMNS c JOIN ALL_CONSTRAINTS u "
                    "ON u.OWNER = c.OWNER AND u.CONSTRAINT_NAME = c.CONSTRAINT_NAME "
                    "AND u.TABLE_NAME = c.TABLE_NAME "
                    "WHERE u.OWNER = :owner AND u.TABLE_NAME = :table_name "
                    "AND u.CONSTRAINT_TYPE = 'U' ORDER BY c.CONSTRAINT_NAME, c.POSITION", params)
                unique_columns = _parse_unique_constraint_columns(cursor.fetchall(), table_name, unique_names)
                models = tuple(replace(c, columns=unique_columns[c.constraint_name])
                               if c.constraint_type == "U" else c for c in models)
        if (not isinstance(objects, (tuple, list)) or not isinstance(tables, (tuple, list))
            or len(tables) != 1 or not isinstance(tables[0], (tuple, list)) or len(tables[0]) != 4
            or not isinstance(properties, (tuple, list)) or not isinstance(constraints, (tuple, list))
            or any(not isinstance(row, (tuple, list)) or len(row) != 7 for row in properties)
            or any(not isinstance(row, (tuple, list)) or len(row) != 7 for row in constraints)):
            raise TableSafetyMetadataReadError("Tablo güvenlik metadata satırları eksik veya bozuk.")
        models = _read_foreign_key_metadata(connection, table_name, models)
        column_properties = _read_column_value_metadata(
            connection, table_name, tuple(OracleColumnProperties(*row) for row in properties))
        metadata = TableSafetyMetadata(
            "HR_CORE", table_name, tuple(tuple(row) for row in objects), *tables[0],
            column_properties, models)
        _validate_table_safety_metadata(metadata)
        return metadata
    except TableSafetyMetadataReadError:
        raise
    except Exception:
        raise TableSafetyMetadataReadError("Tablo güvenlik metadata okuması doğrulanamadı.") from None


def _not_null_column(condition: str | None) -> str | None:
    """Yalnızca dengeli dış parantezler ve tek IS NOT NULL; literal/yorum yok."""
    if type(condition) is not str or len(condition) > 1000:
        return None
    tokens = re.findall(r'"[A-Z][A-Z0-9_]{0,29}"|[A-Za-z][A-Za-z0-9_]*|[()]|\S', condition)
    depth = 0
    for token in tokens:
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        if depth < 0:
            return None
    if depth:
        return None
    while tokens and tokens[0] == "(" and tokens[-1] == ")":
        depth = 0
        for index, token in enumerate(tokens):
            depth += (token == "(") - (token == ")")
            if depth == 0:
                break
        if index != len(tokens) - 1:
            break
        tokens = tokens[1:-1]
    if len(tokens) != 4 or [token.upper() for token in tokens[1:]] != ["IS", "NOT", "NULL"]:
        return None
    name = tokens[0]
    if re.fullmatch(r'"[A-Z][A-Z0-9_]{0,29}"', name):
        return name[1:-1]
    return name.upper() if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,29}", name) else None


class StructureComparisonStatus(Enum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    INVALID_METADATA = "INVALID_METADATA"
    INVALID_EXPECTATION = "INVALID_EXPECTATION"


@dataclass(frozen=True)
class StructureDifference:
    # Yalnızca dahili alan yolları/kodlar; ham isim, SQL veya değer gösterilmez.
    field: str
    code: str


@dataclass(frozen=True)
class TableStructureComparison:
    status: StructureComparisonStatus
    differences: tuple[StructureDifference, ...] = ()

    @property
    def matched(self) -> bool:
        return self.status is StructureComparisonStatus.MATCH


def compare_table_structure(
    expected: CreateTableDefinition,
    actual: TableColumnMetadata,
    primary_key: OraclePrimaryKeyMetadata | None,
    *, safety: TableSafetyMetadata | None = None,
) -> TableStructureComparison:
    """Saf tam MVP karşılaştırması; eksik güvenlik görüntüsü MATCH üretemez.

    NUMBER ve TIMESTAMP dictionary gösterimleri canlı 26ai üzerinde ayrıca
    doğrulanmalıdır. Çoklu sorgular atomik değildir; dış DDL'yi engellemez.
    """
    def invalid(status: StructureComparisonStatus, code: str) -> TableStructureComparison:
        return TableStructureComparison(status, (StructureDifference("input", code),))

    # Frozen dataclass elle hatalı kurulabilir; parser çıktısıyla tutarlılığı ve
    # bool/int gibi Python eşitliği tuzaklarını ayrıca denetle.
    try:
        if type(expected) is not CreateTableDefinition or type(expected.column_definitions) is not tuple:
            raise ValueError
        canonical = validate_create_table(expected.statement)
        if (expected.table_name != canonical.table_name or expected.primary_key != canonical.primary_key
            or type(expected.unique_constraints) is not tuple
            or expected.unique_constraints != canonical.unique_constraints
            or any(type(unique) is not UniqueConstraintDefinition for unique in expected.unique_constraints)
            or type(expected.foreign_keys) is not tuple or expected.foreign_keys != canonical.foreign_keys
            or any(type(fk) is not ForeignKeyDefinition for fk in expected.foreign_keys)
            or type(expected.active_checks) is not tuple or expected.active_checks != canonical.active_checks
            or any(type(c) is not ActiveCheckDefinition for c in expected.active_checks)):
            raise ValueError
        if len(expected.column_definitions) != len(canonical.column_definitions):
            raise ValueError
        for column, validated in zip(expected.column_definitions, canonical.column_definitions):
            if type(column) is not ColumnDefinition:
                raise ValueError
            for name in ColumnDefinition.__dataclass_fields__:
                value, reference = getattr(column, name), getattr(validated, name)
                if type(value) is not type(reference) or value != reference:
                    raise ValueError
        if expected.primary_key is not None:
            if type(expected.primary_key) is not PrimaryKeyDefinition or type(expected.primary_key.columns) is not tuple:
                raise ValueError
    except Exception:
        return invalid(StructureComparisonStatus.INVALID_EXPECTATION, "INVALID_EXPECTATION")

    try:
        if (
            type(actual) is not TableColumnMetadata or type(actual.columns) is not tuple
            or not _valid_pk_metadata_name(actual.owner) or not _valid_pk_metadata_name(actual.table_name)
        ):
            raise ValueError
        _validate_table_safety_metadata(safety)
        # Okuyucu iç kolonları silmez. Kaynak kolon/ifade eşlemesi henüz
        # kanıtlanmadığından bunlar ne normal kolon sayılır ne kesin yapı farkı.
        # SYS_ adı veya bayraklardan kaynak tahmini yapılmaz; MATCH/FAILED yok.
        if any(c.user_generated == "NO" for c in safety.column_properties):
            return invalid(StructureComparisonStatus.INVALID_METADATA, "UNMAPPED_INTERNAL_COLUMN")
        # Ayrı SELECT görüntüleri birbirini yalanlıyorsa kesin yapı farkı
        # üretemeyiz. Bu durum eşzamanlı DDL veya bozuk metadata olabilir.
        observed_pks = tuple(c for c in safety.constraints if c.constraint_type == "P")
        if (len(observed_pks) != int(primary_key is not None)
            or (primary_key is not None and
                (type(primary_key) is not OraclePrimaryKeyMetadata
                 or observed_pks[0].constraint_name != primary_key.constraint_name))):
            raise ValueError
        rows = []
        for column in actual.columns:
            if type(column) is not OracleColumnMetadata:
                raise ValueError
            rows.append((
                column.column_name, column.column_id, column.data_type,
                column.data_precision, column.data_scale, column.char_length,
                column.char_used, column.nullable,
            ))
        _parse_column_metadata_rows(rows)
        column_names = {column.column_name for column in actual.columns}
        if any(not set(c.columns).issubset(column_names) for c in safety.constraints if c.constraint_type in ("U", "R")):
            raise ValueError
        # İki dictionary kaynağı aynı kolon kimlik/sıra görüntüsünü vermeli.
        # Çelişki beklenen SQL'e karşı kesin fark değildir; FAILED yazdırmaz.
        if tuple((c.column_name, c.column_id) for c in actual.columns) != tuple(
            (c.column_name, c.column_id) for c in safety.column_properties
        ):
            return invalid(StructureComparisonStatus.INVALID_METADATA, "CONFLICTING_COLUMN_SNAPSHOTS")
        if primary_key is not None:
            if type(primary_key) is not OraclePrimaryKeyMetadata or type(primary_key.columns) is not tuple:
                raise ValueError
            if (
                not _valid_pk_metadata_name(primary_key.owner)
                or not _valid_pk_metadata_name(primary_key.table_name)
                or not _valid_pk_metadata_name(primary_key.constraint_name)
            ):
                raise ValueError
            # Mevcut saf PK doğrulayıcısını kullan; I/O yoktur.
            _parse_primary_key_columns([
                ("HR_CORE", actual.table_name, primary_key.constraint_name, name, position)
                for position, name in enumerate(primary_key.columns, start=1)
            ], actual.table_name, primary_key.constraint_name)
            if not set(primary_key.columns).issubset(column.column_name for column in actual.columns):
                raise ValueError
    except Exception:
        return invalid(StructureComparisonStatus.INVALID_METADATA, "INVALID_METADATA")

    differences: list[StructureDifference] = []

    def check(path: str, wanted, found) -> None:
        if type(wanted) is not type(found) or wanted != found:
            differences.append(StructureDifference(path, "VALUE_MISMATCH"))

    check("safety.owner", "HR_CORE", safety.owner)
    check("safety.table_name", expected.table_name, safety.table_name)
    check("table.object_types", (("TABLE", "VALID"),), safety.object_types)
    check("table.temporary", "N", safety.temporary)
    check("table.iot_type", None, safety.iot_type)
    check("table.nested", "NO", safety.nested)
    check("table.secondary", "N", safety.secondary)
    for index, column in enumerate(safety.column_properties, 1):
        wanted = expected.column_definitions[index - 1] if index <= len(expected.column_definitions) else None
        if wanted is not None and wanted.identity:
            check(f"properties[{index}].identity_generation", "ALWAYS", column.identity_generation)
            check(f"properties[{index}].identity_default", True, _identity_default_matches(column))
        else:
            check(f"properties[{index}].default_present", wanted is not None and wanted.default is not None,
                  column.default_length is not None)
            if wanted is not None and wanted.default is not None:
                check(f"properties[{index}].default", wanted.default, _normalized_default(column.default_expression))
        if column.default_length is not None:
            check(f"properties[{index}].default_on_null", "NO", column.default_on_null)
        check(f"properties[{index}].hidden", "NO", column.hidden)
        check(f"properties[{index}].virtual", "NO", column.virtual)
        check(f"properties[{index}].identity", "YES" if wanted is not None and wanted.identity else "NO", column.identity)
    constraint_pks = tuple(c for c in safety.constraints if c.constraint_type == "P")
    check("constraints.primary_key.count", int(primary_key is not None), len(constraint_pks))
    if primary_key is not None and len(constraint_pks) == 1:
        check("constraints.primary_key.name", primary_key.constraint_name, constraint_pks[0].constraint_name)
    mandatory = {c.name for c in expected.column_definitions if c.explicit_not_null or c.identity}
    if expected.primary_key is not None:
        mandatory.update(expected.primary_key.columns)
    seen_not_null = set()
    seen_active_checks = set()
    expected_checks = {c.constraint_name: c for c in expected.active_checks}
    for index, constraint in enumerate(safety.constraints, 1):
        path = f"constraints[{index}]"
        check(path + ".status", "ENABLED", constraint.status)
        check(path + ".validated", "VALIDATED", constraint.validated)
        check(path + ".deferrable", "NOT DEFERRABLE", constraint.deferrable)
        check(path + ".deferred", "IMMEDIATE", constraint.deferred)
        if constraint.constraint_type == "C":
            name = _not_null_column(constraint.condition)
            if constraint.constraint_name in expected_checks:
                check(path + ".active_check", True, _is_active_check(constraint.condition))
                seen_active_checks.add(constraint.constraint_name)
            else:
                check(path + ".not_null", True, name in mandatory and name not in seen_not_null)
                seen_not_null.add(name)
        elif constraint.constraint_type not in ("P", "U", "R"):
            differences.append(StructureDifference(path + ".type", "UNSUPPORTED_CONSTRAINT"))
    actual_uniques = {c.constraint_name: c for c in safety.constraints if c.constraint_type == "U"}
    check("unique_constraints.count", len(expected.unique_constraints), len(actual_uniques))
    for index, unique in enumerate(expected.unique_constraints, 1):
        path = f"unique_constraints[{index}]"
        metadata = actual_uniques.get(unique.constraint_name)
        check(path + ".presence", True, metadata is not None)
        if metadata is not None:
            check(path + ".columns", (unique.column_name,), metadata.columns)
    check("active_checks.names", set(expected_checks), seen_active_checks)
    actual_fks = {c.constraint_name: c for c in safety.constraints if c.constraint_type == "R"}
    check("foreign_keys.count", len(expected.foreign_keys), len(actual_fks))
    for index, fk in enumerate(expected.foreign_keys, 1):
        path = f"foreign_keys[{index}]"
        metadata = actual_fks.get(fk.constraint_name)
        check(path + ".presence", True, metadata is not None)
        if metadata is not None:
            check(path + ".columns", (fk.column_name,), metadata.columns)
            ref = metadata.reference
            check(path + ".owner", "HR_CORE", ref.owner)
            check(path + ".table", fk.referenced_table, ref.table_name)
            check(path + ".ref_columns", (fk.referenced_column,), ref.columns)
            check(path + ".delete_rule", "NO ACTION", ref.delete_rule)
            check(path + ".status", "ENABLED", ref.status)
            check(path + ".validated", "VALIDATED", ref.validated)
    # Açık NOT NULL için bir CHECK kaydı zorunlu; yalnızca PK'den türeyen NOT
    # NULL için Oracle'ın ayrıca C kaydı üretmesi şart koşulmaz.
    check("constraints.explicit_not_null", True,
          {c.name for c in expected.column_definitions if c.explicit_not_null}.issubset(seen_not_null))
    check("table.owner", "HR_CORE", actual.owner)
    check("table.name", expected.table_name, actual.table_name)
    check("columns.count", len(expected.column_definitions), len(actual.columns))
    pk_columns = expected.primary_key.columns if expected.primary_key is not None else ()
    for index, (column, metadata) in enumerate(zip(expected.column_definitions, actual.columns), start=1):
        path = f"columns[{index}]"
        check(path + ".name", column.name, metadata.column_name)
        wanted_type = column.data_type
        if column.data_type == "TIMESTAMP":
            wanted_type = f"TIMESTAMP({column.timestamp_precision})"
            if column.with_time_zone:
                wanted_type += " WITH TIME ZONE"
        check(path + ".data_type", wanted_type, metadata.data_type)
        check(path + ".data_precision", column.number_precision, metadata.data_precision)
        wanted_scale = column.timestamp_precision if column.data_type == "TIMESTAMP" else column.number_scale
        check(path + ".data_scale", wanted_scale, metadata.data_scale)
        if column.data_type == "VARCHAR2":
            check(path + ".char_length", column.char_length, metadata.char_length)
            check(path + ".char_used", "C", metadata.char_used)
        else:
            check(path + ".char_used", None, metadata.char_used)
        effective_not_null = column.explicit_not_null or column.identity or column.name in pk_columns
        check(path + ".nullable", "N" if effective_not_null else "Y", metadata.nullable)

    check("primary_key.presence", expected.primary_key is not None, primary_key is not None)
    if primary_key is not None:
        check("primary_key.owner", "HR_CORE", primary_key.owner)
        check("primary_key.table_name", expected.table_name, primary_key.table_name)
        if expected.primary_key is not None:
            check("primary_key.columns", expected.primary_key.columns, primary_key.columns)
            # İsim SQL'de verilmemişse SYS_ öneki şartı konmaz; sadece isim beklentisi yoktur.
            if expected.primary_key.constraint_name is not None:
                check("primary_key.constraint_name", expected.primary_key.constraint_name, primary_key.constraint_name)
    return TableStructureComparison(
        StructureComparisonStatus.MISMATCH if differences else StructureComparisonStatus.MATCH,
        tuple(differences),
    )


class DDLOutcome(Enum):
    NOT_STARTED = "NOT_STARTED"
    CONFIRMED_REJECTED = "CONFIRMED_REJECTED"
    SUCCEEDED = "SUCCEEDED"
    UNKNOWN = "UNKNOWN"


class HistoryState(Enum):
    NOT_RECORDED = "NOT_RECORDED"
    RUNNING_POSSIBLE = "RUNNING_POSSIBLE"
    RUNNING_CONFIRMED = "RUNNING_CONFIRMED"
    SUCCESS_CONFIRMED = "SUCCESS_CONFIRMED"
    FAILED_CONFIRMED = "FAILED_CONFIRMED"
    COMMIT_UNKNOWN = "COMMIT_UNKNOWN"


class ExecutionOutcome(Enum):
    NOT_EXECUTED = "NOT_EXECUTED"
    DDL_REJECTED = "DDL_REJECTED"
    DDL_VERIFIED = "DDL_VERIFIED"
    DDL_METADATA_MISMATCH = "DDL_METADATA_MISMATCH"
    DDL_UNKNOWN = "DDL_UNKNOWN"
    METADATA_UNVERIFIED = "METADATA_UNVERIFIED"
    HISTORY_COMMIT_UNKNOWN = "HISTORY_COMMIT_UNKNOWN"
    COMPLETE = "COMPLETE"
    INVALID_EVIDENCE = "INVALID_EVIDENCE"


@dataclass(frozen=True)
class ExecutionClassification:
    """Durum kararıdır; history yazımı veya transaction yönetimi yapmaz."""

    outcome: ExecutionOutcome
    manual_review_required: bool
    failed_reason: str | None = None

    @property
    def can_mark_failed(self) -> bool:
        return self.failed_reason is not None

    @property
    def can_mark_success(self) -> bool:
        return self.outcome is ExecutionOutcome.DDL_VERIFIED

    @property
    def retry_allowed(self) -> bool:
        return False

    @property
    def automatic_cleanup_allowed(self) -> bool:
        return False

    @property
    def ddl_rollback_allowed(self) -> bool:
        return False


def classify_execution_outcome(
    ddl_outcome: DDLOutcome,
    *,
    metadata: TableStructureComparison | None = None,
    history_state: HistoryState = HistoryState.NOT_RECORDED,
) -> ExecutionClassification:
    """Saf ve fail-closed sonuç sınıflandırması; canlı Oracle kanıtı üretmez.

    CONFIRMED_REJECTED yalnızca çağıranın kesin sunucu reddi kanıtıdır. Generic
    driver exception, timeout veya bağlantı kaybı bu kanıt sayılmaz. SQL gönderimi
    sonrası istisna için UNKNOWN kullanılmalıdır. Metadata MATCH atomik şema
    görüntüsü veya kilit sahipliği kanıtı değildir.
    can_mark_* sadece sonraki karara girdi sağlar; onay, canlı kilit ve bağlantı
    uygunluğu yerine geçmez. Hiçbir sonuç retry, DROP veya DDL rollback önermez.
    """
    invalid = ExecutionClassification(ExecutionOutcome.INVALID_EVIDENCE, True)
    if type(ddl_outcome) is not DDLOutcome or type(history_state) is not HistoryState:
        return invalid
    # Commit belirsizliği, DDL veya metadata başarısıyla ortadan kalkmaz.
    if history_state is HistoryState.COMMIT_UNKNOWN:
        return ExecutionClassification(ExecutionOutcome.HISTORY_COMMIT_UNKNOWN, True)
    if ddl_outcome is DDLOutcome.UNKNOWN:
        return ExecutionClassification(ExecutionOutcome.DDL_UNKNOWN, True)
    if history_state is HistoryState.FAILED_CONFIRMED:
        # Sonraki history yazımını önermeden kalıcı FAILED bilgisini koru.
        if (ddl_outcome is DDLOutcome.SUCCEEDED
            and type(metadata) is TableStructureComparison
            and metadata.status is StructureComparisonStatus.MISMATCH
            and type(metadata.differences) is tuple and metadata.differences
            and all(type(item) is StructureDifference for item in metadata.differences)):
            return ExecutionClassification(ExecutionOutcome.DDL_METADATA_MISMATCH, True)
        return invalid
    if ddl_outcome is DDLOutcome.NOT_STARTED:
        if metadata is not None or history_state is HistoryState.SUCCESS_CONFIRMED:
            return invalid
        return ExecutionClassification(
            ExecutionOutcome.NOT_EXECUTED, history_state is not HistoryState.NOT_RECORDED
        )
    if ddl_outcome is DDLOutcome.CONFIRMED_REJECTED:
        if metadata is not None or history_state is not HistoryState.RUNNING_CONFIRMED:
            return invalid
        return ExecutionClassification(ExecutionOutcome.DDL_REJECTED, True, "DDL_REJECTED")
    if history_state not in {HistoryState.RUNNING_CONFIRMED, HistoryState.SUCCESS_CONFIRMED}:
        return invalid
    if (
        type(metadata) is not TableStructureComparison
        or type(metadata.status) is not StructureComparisonStatus
        or type(metadata.differences) is not tuple
        or any(type(item) is not StructureDifference for item in metadata.differences)
    ):
        return ExecutionClassification(ExecutionOutcome.METADATA_UNVERIFIED, True)
    if metadata.status is StructureComparisonStatus.MATCH and not metadata.differences:
        return ExecutionClassification(
            ExecutionOutcome.COMPLETE if history_state is HistoryState.SUCCESS_CONFIRMED
            else ExecutionOutcome.DDL_VERIFIED,
            history_state is not HistoryState.SUCCESS_CONFIRMED,
        )
    if (
        metadata.status is StructureComparisonStatus.MISMATCH and metadata.differences
        and history_state is HistoryState.RUNNING_CONFIRMED
    ):
        return ExecutionClassification(
            ExecutionOutcome.DDL_METADATA_MISMATCH, True, "POST_VALIDATION_FAILED"
        )
    # Okuma/biçim hatası veya çelişkili metadata sonucu kesin yapı farkı değildir.
    return ExecutionClassification(ExecutionOutcome.METADATA_UNVERIFIED, True)


def classify_execution_exception(
    error: BaseException,
    *,
    ddl_submitted: bool,
    commit_in_progress: bool = False,
    history_state: HistoryState = HistoryState.NOT_RECORDED,
) -> ExecutionClassification:
    """İstisna metnini/kodunu incelemeden yalnızca işlem aşamasına göre karar ver.

    DDL çağrısına girildiyse gönderilmediği kanıtlanamaz; driver istisnası kesin
    ret olarak yorumlanmaz. Commit sırasında hata başarıyla commit edilmediğinin
    kanıtı değildir. Ham error saklanmaz, yazdırılmaz veya sonuca eklenmez.
    """
    if (
        not isinstance(error, BaseException) or type(ddl_submitted) is not bool
        or type(commit_in_progress) is not bool or type(history_state) is not HistoryState
    ):
        return ExecutionClassification(ExecutionOutcome.INVALID_EVIDENCE, True)
    return classify_execution_outcome(
        DDLOutcome.UNKNOWN if ddl_submitted else DDLOutcome.NOT_STARTED,
        history_state=HistoryState.COMMIT_UNKNOWN if commit_in_progress else history_state,
    )


def prepare_migration_content(migration: Migration, content: bytes) -> PreparedMigration:
    """Dosya I/O olmadan aynı baytlardan UTF-8, checksum ve SQL doğrulaması üret."""
    match = MIGRATION_NAME.fullmatch(migration.path.name)
    if match is None or int(match.group(1)) != migration.version:
        raise ValueError("Migration dosya adı ile versiyon uyuşmuyor.")
    checksum = hashlib.sha256(content).hexdigest()
    if checksum != migration.checksum:
        raise ValueError("Migration içeriği keşif anından sonra değişmiş; yeniden planlama gerekir.")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Migration dosyası geçerli UTF-8 olmalıdır.") from None
    if not text.strip():
        raise ValueError("Boş migration dosyası kabul edilmez.")
    definition = validate_create_table(text)
    return PreparedMigration(migration, content, text, definition)


def read_migration_for_apply(
    migration: Migration, directory: Path = MIGRATIONS_DIRECTORY
) -> PreparedMigration:
    """Tek içerik okuması yapan I/O sınırı; mevcut CLI tarafından çağrılmaz.

    Normal dosya/symlink yollarını denetler; kötü niyetli eşzamanlı dosya sistemi
    değişikliklerine karşı atomik sandbox sağlamaz. Henüz çalıştırma izni vermez.
    """
    try:
        root = directory.resolve(strict=True)
        path = migration.path.resolve(strict=True)
        if not root.is_dir() or not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Migration dosyası migration klasörü içindeki normal bir dosya olmalıdır.")
        content = path.read_bytes()
    except (OSError, RuntimeError):
        raise ValueError("Migration dosyası güvenli biçimde çözümlenemedi veya okunamadı.") from None
    return prepare_migration_content(migration, content)


@dataclass(frozen=True)
class ApplyPlan:
    """Tek migration için değişmez plan; hedefin canlı doğrulaması çağırana aittir."""

    target_user: str
    target_pdb: str
    version: int
    script_name: str
    checksum: str
    target_table: str
    statement: str
    source_bytes: bytes = field(repr=False)
    comparison_snapshot: tuple[DatabasePlanEntry, ...]


def build_apply_plan(
    migrations: list[Migration],
    history: list[tuple],
    directory: Path = MIGRATIONS_DIRECTORY,
    *,
    target_user: str = "HR_CORE",
    target_pdb: str = "FREEPDB1",
) -> ApplyPlan | None:
    """Verilen geçmiş ve yerel dosyadan plan hazırla; Oracle'a bağlanma.

    Geçmişin güncelliği ve hedef oturum burada kanıtlanmaz. İleride kilit altında
    yeniden okunan geçmişle tekrar çağrılmalı; eski planla eşleşmeden ilerlenmemeli.
    """
    if (target_user, target_pdb) != ("HR_CORE", "FREEPDB1"):
        raise ValueError("Uygulama planının hedefi HR_CORE / FREEPDB1 olmalıdır.")
    entries = compare_migrations(migrations, history)
    eligibility = assess_apply_eligibility(entries)
    if eligibility.blockers:
        raise ValueError("Uygulama planı engellendi:\n- " + "\n- ".join(eligibility.blockers))
    if not eligibility.eligible or eligibility.next_migration is None:
        return None
    candidate = eligibility.next_migration
    migration = next(item for item in migrations if item.version == candidate.version)
    prepared = read_migration_for_apply(migration, directory)
    return ApplyPlan(
        target_user=target_user,
        target_pdb=target_pdb,
        version=migration.version,
        script_name=migration.path.name,
        checksum=migration.checksum,
        target_table=prepared.definition.table_name,
        statement=prepared.definition.statement,
        source_bytes=prepared.content,
        comparison_snapshot=tuple(entries),
    )


def validate_apply_plan_unchanged(approved: ApplyPlan, current: ApplyPlan | None) -> None:
    """I/O yapmadan tam plan eşitliğini denetle; değişiklikte yeni onay gerektir."""
    if current is None or approved != current:
        raise ValueError("Onaylanan uygulama planı değişmiş; yeniden planlama ve onay gerekir.")


class MigrationHistoryError(RuntimeError):
    """Gizli bilgi içermeyen geçmiş kayıt hatası; otomatik retry talimatı değildir."""


def _history_identity(connection, plan: ApplyPlan) -> dict[str, str | int]:
    """Geçmiş DML'si için kimliği doğrula; transaction veya hedef sorgusu yapma."""
    if connection.autocommit is not False:
        raise MigrationHistoryError("Geçmiş kaydı için autocommit=False gereklidir.")
    if (plan.target_user, plan.target_pdb) != ("HR_CORE", "FREEPDB1"):
        raise MigrationHistoryError("Geçmiş kaydının plan hedefi HR_CORE / FREEPDB1 olmalıdır.")
    match = MIGRATION_NAME.fullmatch(plan.script_name)
    if (
        type(plan.version) is not int or not 0 <= plan.version <= 9999
        or match is None or int(match.group(1)) != plan.version
        or len(plan.script_name) > 255
        or re.fullmatch(r"[0-9a-f]{64}", plan.checksum) is None
    ):
        raise MigrationHistoryError("Geçmiş kaydı için migration kimliği veya checksum geçersiz.")
    return {
        "version": plan.version,
        "script_name": plan.script_name,
        "checksum": plan.checksum,
    }


def _history_write_error(exc: Exception, operation: str) -> MigrationHistoryError:
    """Ham exception/SQL/bind değerlerini kullanmadan güvenli hata üret."""
    detail = exc.args[0] if exc.args else None
    code = getattr(detail, "full_code", "")
    if not isinstance(code, str) or re.fullmatch(r"(?:ORA|DPY|DPI)-[0-9]+", code) is None:
        code = "ORACLE_ERROR"
    if operation == "RUNNING INSERT" and (
        code == "ORA-00001" or getattr(detail, "code", None) == 1
    ):
        return MigrationHistoryError(
            "RUNNING INSERT çakışması: versiyon veya benzersiz anahtar zaten mevcut. "
            "Kayıt temizlenmedi; retry/repair yapılmadan işlem durdurulmalıdır."
        )
    return MigrationHistoryError(
        f"{operation} başarısız ({code}). Sonuç belirsiz olabilir; "
        "otomatik retry veya FAILED dönüşümü yapılmamalıdır."
    )


def insert_running_migration(connection, plan: ApplyPlan) -> None:
    """RUNNING ekle; commit/rollback yapma ve mevcut kaydı değiştirme.

    Çağıran hedef oturumu/onayı/kilidi önceden doğrulamalı ve INSERT sonrasında
    commit'i başarıyla tamamlamadan migration SQL'ini göndermemelidir. Commit
    hatası, işlemin kesin geri alındığı anlamına gelmez; otomatik retry yapılmaz.
    """
    params = _history_identity(connection, plan)
    params["status"] = "RUNNING"
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO HR_CORE.SCHEMA_MIGRATIONS "
                "(VERSION, SCRIPT_NAME, CHECKSUM, STATUS, STARTED_AT, FINISHED_AT, ERROR_MESSAGE) "
                "VALUES (:version, :script_name, :checksum, :status, SYSTIMESTAMP, NULL, NULL)",
                params,
            )
            affected = cursor.rowcount
    except Exception as exc:
        raise _history_write_error(exc, "RUNNING INSERT") from None
    if type(affected) is not int or affected != 1:
        raise MigrationHistoryError(
            "RUNNING INSERT tam olarak bir satırı etkilemedi; transaction çağıran tarafından "
            "değerlendirilmeli, otomatik tekrar denenmemelidir."
        )


def _finish_running_migration(
    connection, plan: ApplyPlan, status: str, error_message: str | None
) -> None:
    """Yalnızca eşleşen RUNNING kaydını bitir; transaction çağırana aittir."""
    if status not in {"SUCCESS", "FAILED"}:
        raise MigrationHistoryError("Desteklenmeyen geçmiş durum geçişi.")
    params = _history_identity(connection, plan)
    params.update(status=status, error_message=error_message)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE HR_CORE.SCHEMA_MIGRATIONS "
                "SET STATUS = :status, FINISHED_AT = SYSTIMESTAMP, ERROR_MESSAGE = :error_message "
                "WHERE VERSION = :version AND SCRIPT_NAME = :script_name "
                "AND CHECKSUM = :checksum AND STATUS = 'RUNNING'",
                params,
            )
            affected = cursor.rowcount
    except Exception as exc:
        raise _history_write_error(exc, f"{status} UPDATE") from None
    if type(affected) is not int or affected != 1:
        raise MigrationHistoryError(
            f"{status} UPDATE tam olarak bir eşleşen RUNNING kaydını etkilemedi. "
            "İşlem durdurulmalı; otomatik retry veya repair yapılmamalıdır."
        )


def mark_migration_success(connection, plan: ApplyPlan) -> None:
    """SQL ve son doğrulama kesin başarılıysa SUCCESS yaz; henüz commit etme.

    Hata, önceki DDL'nin geri alındığını göstermez. Çağıran bunu otomatik FAILED'e
    çevirmemeli veya migration SQL'ini tekrar göndermemelidir.
    """
    _finish_running_migration(connection, plan, "SUCCESS", None)


def mark_migration_failed(
    connection,
    plan: ApplyPlan,
    *,
    failure_reason: str,
    outcome_confirmed: bool = False,
) -> None:
    """Yalnızca sonucu kesin bilinen hata için FAILED yaz; commit/rollback yapma.

    outcome_confirmed=True çağıranın açık beyanıdır; bu yardımcı DDL sonucunu
    kendisi kanıtlayamaz. Ağ kopması, timeout, kesilen süreç veya SUCCESS yazma/
    commit belirsizliğinde çağrılmamalıdır. DDL_REJECTED, sunucunun kesin ret
    cevabını; POST_VALIDATION_FAILED, kesin tamamlanmış DDL sonrası doğrulama
    uyuşmazlığını ifade eder. Ham exception veya serbest hata metni kabul edilmez.
    """
    messages = {
        "DDL_REJECTED": "Migration DDL işlemi Oracle tarafından kesin olarak reddedildi.",
        "POST_VALIDATION_FAILED": (
            "DDL tamamlandı ancak son yapı doğrulaması başarısız oldu; "
            "kalıcı nesne etkileri manuel incelenmelidir."
        ),
    }
    if outcome_confirmed is not True:
        raise MigrationHistoryError("Sonucu kesin doğrulanmamış işlem FAILED olarak kaydedilemez.")
    if failure_reason not in messages:
        raise MigrationHistoryError("FAILED için desteklenen güvenli hata nedeni gereklidir.")
    _finish_running_migration(connection, plan, "FAILED", messages[failure_reason])


class ApplyApprovalError(ValueError):
    """Kullanıcı onayı alınamadığında uygulamanın durması gerektiğini belirtir."""


class ApplyApprovalInterrupted(ApplyApprovalError):
    """Terminalde Ctrl+C; yanlış onaydan farklı, ham hata içermeyen duruş."""


def request_apply_confirmation(plan: ApplyPlan) -> None:
    """Yalnızca terminal onayı al; bağlantı açma veya SQL çalıştırma."""
    if not sys.stdin.isatty():
        raise ApplyApprovalError("Uygulama için etkileşimli terminalde açık onay gerekir.")
    print(f"Hedef: {plan.target_user} / {plan.target_pdb}")
    print(f"Migration: V{plan.version:04d} | {plan.script_name}")
    print(f"SHA-256: {plan.checksum}")
    print(f"Hedef tablo: {plan.target_user}.{plan.target_table}")
    print("Tek migration planlanıyor; DDL implicit commit yapar ve rollback ile geri alınamaz.")
    print("İşlem DBMS_LOCK çağrıları ve migration geçmişine durum yazımı içerir.")
    confirmation = (
        f"APPLY {plan.target_user}/{plan.target_pdb} V{plan.version:04d} {plan.checksum}"
    )
    try:
        answer = input(f"Onaylamak için tam olarak '{confirmation}' yazın: ")
    except KeyboardInterrupt:
        raise ApplyApprovalInterrupted("Onay işlemi kullanıcı tarafından kesildi.") from None
    except (EOFError, OSError):
        raise ApplyApprovalError("Onay işlemi kesildi; uygulama durdurulmalıdır.") from None
    if answer != confirmation:
        raise ApplyApprovalError("Onay metni eşleşmedi; uygulama durdurulmalıdır.")


class MigrationLockError(RuntimeError):
    """Ham sürücü mesajı veya bağlantı bilgisi içermeyen kilit hatası."""


def allocate_migration_lock(connection) -> str:
    """Sabit kilit adı için handle al; ALLOCATE_UNIQUE Oracle tarafında commit yapar.

    Yalnızca açık onaydan sonra, bekleyen DML olmayan özel bağlantıda çağrılmalı.
    Bu fonksiyon kilit edinmez, yeni bağlantı açmaz veya geçmişe DML yazmaz.
    """
    try:
        with connection.cursor() as cursor:
            handle = cursor.var(str, size=128)
            cursor.callproc("SYS.DBMS_LOCK.ALLOCATE_UNIQUE", [MIGRATION_LOCK_NAME, handle])
            value = handle.getvalue()
    except Exception:
        raise MigrationLockError("DBMS_LOCK kilit kimliği alınamadı; işlem durdurulmalıdır.") from None
    if not isinstance(value, str) or not value or len(value) > 128:
        raise MigrationLockError("DBMS_LOCK geçerli bir kilit handle değeri döndürmedi.")
    return value


def request_migration_lock(connection, lock_handle: str) -> None:
    """Exclusive kilit iste; yalnızca 0 başarıdır. Commit/rollback kilidi bırakmaz."""
    try:
        with connection.cursor() as cursor:
            # callfunc parametreleri bind edilir: handle, X_MODE, timeout, release_on_commit.
            result = cursor.callfunc("SYS.DBMS_LOCK.REQUEST", int, [lock_handle, 6, 0, False])
    except Exception:
        raise MigrationLockError(
            "DBMS_LOCK REQUEST başarısız; kilit edinilmiş varsayılamaz. "
            "Oturumu kapatın ve otomatik tekrar denemeyin."
        ) from None
    if type(result) is not int or result != 0:
        explanations = {
            1: "timeout; başka bir oturum kilidi tutuyor olabilir",
            2: "deadlock", 3: "parametre hatası",
            4: "kilit bu oturumda zaten mevcut; beklenmeyen yaşam döngüsü",
            5: "geçersiz kilit handle değeri",
        }
        reason = explanations.get(result, "beklenmeyen dönüş değeri") if type(result) is int else "geçersiz dönüş türü"
        raise MigrationLockError(f"DBMS_LOCK REQUEST: {reason}; uygulama durdurulmalıdır.")


def release_migration_lock(connection, lock_handle: str) -> None:
    """Aynı oturumda kilidi bırak; ham sürücü hatasını kullanıcıya taşıma."""
    try:
        with connection.cursor() as cursor:
            result = cursor.callfunc("SYS.DBMS_LOCK.RELEASE", int, [lock_handle])
    except Exception:
        raise MigrationLockError(
            "DBMS_LOCK RELEASE başarısız; aynı oturumun bağlantısı kapatılmalıdır."
        ) from None
    if type(result) is not int or result != 0:
        raise MigrationLockError(
            "DBMS_LOCK RELEASE başarıyla doğrulanamadı; aynı oturumun bağlantısı kapatılmalıdır."
        )


@contextmanager
def migration_lock(connection):
    """Yalnızca kilit yaşam döngüsü; onay ve transaction yönetimi çağırana aittir.

    CLI tarafından çağrılmaz ve migration SQL'i yürütmez. Bağlantı bu kapsamın
    dışında yönetilmeli ve hata dahil her çıkışta sahibi tarafından kapatılmalı.
    REQUEST sonucu belirsizse yeni bağlantıda RELEASE denenmemelidir.
    """
    lock_handle = allocate_migration_lock(connection)
    request_migration_lock(connection, lock_handle)
    original_error: BaseException | None = None
    try:
        yield lock_handle
    except BaseException as exc:
        original_error = exc
        raise
    finally:
        try:
            release_migration_lock(connection, lock_handle)
        except BaseException as cleanup_error:
            if original_error is None:
                raise
            # İkincil temizlik hatası asıl uygulama hatasının yerini alamaz.
            message = (
                str(cleanup_error) if isinstance(cleanup_error, MigrationLockError)
                else "Kilit temizliği kesildi; aynı oturumun bağlantısı kapatılmalıdır."
            )
            if isinstance(original_error, _MigrationExecutionStopped):
                original_error.result = replace(
                    original_error.result, cleanup_failed=True,
                    interrupted=original_error.result.interrupted or _is_interruption(cleanup_error))
            original_error.add_note(message)
            try:
                print(f"Uyarı: {message}", file=sys.stderr)
            except BaseException as warning_error:
                # Terminal uyarısının kesilmesi de asıl aşamayı gizlememeli.
                if isinstance(original_error, _MigrationExecutionStopped):
                    original_error.result = replace(
                        original_error.result, cleanup_failed=True,
                        interrupted=original_error.result.interrupted or _is_interruption(warning_error))


class ExecutionPhase(Enum):
    LOCAL_VALIDATION = "LOCAL_VALIDATION"
    CONNECTION = "CONNECTION"
    INITIAL_PLAN = "INITIAL_PLAN"
    CONFIRMATION = "CONFIRMATION"
    LOCK = "LOCK"
    REVALIDATION = "REVALIDATION"
    PRECONDITIONS = "PRECONDITIONS"
    RUNNING_INSERT = "RUNNING_INSERT"
    RUNNING_COMMIT = "RUNNING_COMMIT"
    DDL = "DDL"
    METADATA = "METADATA"
    SUCCESS_UPDATE = "SUCCESS_UPDATE"
    SUCCESS_COMMIT = "SUCCESS_COMMIT"
    FAILED_UPDATE = "FAILED_UPDATE"
    FAILED_COMMIT = "FAILED_COMMIT"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True)
class MigrationExecutionResult:
    """SQL/exception/ayar içermeyen sonuç; classification bir yeniden yazma izni değil.

    success yalnızca history SUCCESS commit'i ve kaynak temizliği kesin başarılıysa
    True'dur. DDL_VERIFIED + success=False, örneğin SUCCESS UPDATE hatasını anlatır.
    Gelecek CLI exit_code/manual_review_required alanlarını esas almalıdır;
    classification yalnızca DDL/history kanıtını değerlendirir.
    failed_history_confirmed ve FAILED_CONFIRMED, kesinleşen FAILED commit'ini
    belirtir. COMMIT_UNKNOWN hangi son durumun kalıcılaştığını iddia etmez.
    """

    phase: ExecutionPhase
    ddl_outcome: DDLOutcome
    history_state: HistoryState
    classification: ExecutionClassification
    success: bool = False
    no_pending: bool = False
    failed_history_confirmed: bool = False
    interrupted: bool = False
    cleanup_failed: bool = False

    @property
    def manual_review_required(self) -> bool:
        """Toplam sonuç: kesin history gerçeği temizlik hatasını geçersiz kılmaz."""
        return self.cleanup_failed or self.classification.manual_review_required

    @property
    def exit_code(self) -> int:
        """Gelecek CLI bu alanı kullanmalı; yalnız classification yeterli değildir.

        Aday yokluğu normaldir. Kesinti, temizlik hatası ve inceleme gereksinimi
        hiçbir zaman sessiz başarıya dönüştürülmez. Retry izni üretmez.
        """
        return int(not ((self.success or self.no_pending) and not self.interrupted
                        and not self.manual_review_required))


class _MigrationExecutionStopped(Exception):
    """Kilit bağlamında güvenli asıl hata; sürücü hatası veya SQL taşımaz."""

    def __init__(self, result: MigrationExecutionResult):
        super().__init__("Migration yürütme durduruldu; sonuç bilgisi incelenmelidir.")
        self.result = result


def execute_one_migration(
    directory: Path = MIGRATIONS_DIRECTORY,
    *,
    oracledb=None,
    load_dotenv=None,
) -> MigrationExecutionResult:
    """Onaylı tek CREATE TABLE yürütücüsü; apply CLI tarafından bir kez çağrılır.

    Bu fonksiyonun tanımlanması canlı çalıştırma izni değildir. Testler driver ve
    ayar yükleyicisini mock eder. Çağrıldığında özel Thin bağlantısının sahibi
    olur; aynı bağlantı kilit, DML, tek DDL ve son doğrulama boyunca korunur.
    Autoreconnect/pool/retry yoktur. ALLOCATE_UNIQUE, onaydan sonra ve DML'den
    önce çalışır. DDL implicit commit'leri ile history commit'leri atomik değildir.
    Kilidi kullanmayan dış oturumların DDL'si ve süreç çökmesi engellenemez.
    """
    phase = ExecutionPhase.LOCAL_VALIDATION
    ddl = DDLOutcome.NOT_STARTED
    history = HistoryState.NOT_RECORDED
    metadata = None
    connection = None
    failed_confirmed = False
    commit_in_progress = False
    result = None

    def snapshot(*, success=False, no_pending=False, interrupted=False):
        # DDL kesin başarılıysa metadata/history hatasını DDL_UNKNOWN'a düşürme.
        classification = classify_execution_outcome(ddl, metadata=metadata, history_state=history)
        return MigrationExecutionResult(
            phase, ddl, history, classification, success, no_pending,
            failed_confirmed, interrupted)

    try:
        migrations = collect_migrations(directory)
        # Bağlantıdan önce tüm yerel kaynakların yol/UTF-8/tek statement denetimi.
        for migration in migrations:
            read_migration_for_apply(migration, directory)
        phase = ExecutionPhase.CONNECTION
        if oracledb is None:
            import oracledb
        if load_dotenv is None:
            from dotenv import load_dotenv
        settings, port = load_connection_settings(load_dotenv)
        connection = open_oracle_connection(settings, port, oracledb)
        if connection.autocommit is not False:
            raise ValueError("Executor için autocommit=False gereklidir.")
        validate_target_session(connection)
        phase = ExecutionPhase.INITIAL_PLAN
        approved = build_apply_plan(migrations, read_history_from_connection(connection), directory)
        if approved is None:
            result = snapshot(no_pending=True)
        else:
            phase = ExecutionPhase.CONFIRMATION
            request_apply_confirmation(approved)
            phase = ExecutionPhase.LOCK
            with migration_lock(connection):
                try:
                    phase = ExecutionPhase.REVALIDATION
                    validate_target_session(connection)
                    current = build_apply_plan(
                        collect_migrations(directory), read_history_from_connection(connection), directory)
                    validate_apply_plan_unchanged(approved, current)
                    # Onaylı baytlardan tekrar saf doğrulama; diskten yeniden SQL alma.
                    prepared = prepare_migration_content(
                        Migration(approved.version, Path(approved.script_name), approved.checksum),
                        approved.source_bytes)
                    definition = prepared.definition
                    if (definition.statement != approved.statement
                        or definition.table_name != approved.target_table):
                        raise ValueError("Onaylanan SQL modeli tutarsız.")
                    phase = ExecutionPhase.PRECONDITIONS
                    validate_create_table_preconditions(connection, definition)
                    phase = ExecutionPhase.RUNNING_INSERT
                    # INSERT istisnası, istemcinin satır sonucunu bildiği anlamına gelmez.
                    history = HistoryState.RUNNING_POSSIBLE
                    insert_running_migration(connection, approved)
                    phase = ExecutionPhase.RUNNING_COMMIT
                    history = HistoryState.COMMIT_UNKNOWN
                    commit_in_progress = True
                    connection.commit()
                    history = HistoryState.RUNNING_CONFIRMED
                    commit_in_progress = False
                    phase = ExecutionPhase.DDL
                    validate_target_session(connection)
                    if connection.autocommit is not False:
                        raise ValueError("Executor autocommit durumu değişmiş.")
                    with connection.cursor() as cursor:
                        # Tek yürütme noktası: çağrıdan önce belirsiz, dönüşte kesin başarılı.
                        ddl = DDLOutcome.UNKNOWN
                        cursor.execute(approved.statement)
                        ddl = DDLOutcome.SUCCEEDED
                    phase = ExecutionPhase.METADATA
                    columns = read_table_column_metadata(connection, approved.target_table)
                    primary_key = read_table_primary_key_metadata(connection, approved.target_table)
                    safety = read_table_safety_metadata(connection, approved.target_table)
                    metadata = compare_table_structure(definition, columns, primary_key, safety=safety)
                    decision = classify_execution_outcome(ddl, metadata=metadata, history_state=history)
                    if decision.can_mark_success:
                        phase = ExecutionPhase.SUCCESS_UPDATE
                        mark_migration_success(connection, approved)
                        phase = ExecutionPhase.SUCCESS_COMMIT
                        history = HistoryState.COMMIT_UNKNOWN
                        commit_in_progress = True
                        connection.commit()
                        history = HistoryState.SUCCESS_CONFIRMED
                        commit_in_progress = False
                        phase = ExecutionPhase.COMPLETE
                        result = snapshot(success=True)
                    elif decision.can_mark_failed:
                        phase = ExecutionPhase.FAILED_UPDATE
                        mark_migration_failed(connection, approved,
                                              failure_reason=decision.failed_reason, outcome_confirmed=True)
                        phase = ExecutionPhase.FAILED_COMMIT
                        history = HistoryState.COMMIT_UNKNOWN
                        commit_in_progress = True
                        connection.commit()
                        history = HistoryState.FAILED_CONFIRMED
                        failed_confirmed = True
                        commit_in_progress = False
                        raise _MigrationExecutionStopped(snapshot())
                    else:
                        raise _MigrationExecutionStopped(snapshot())
                except _MigrationExecutionStopped:
                    raise
                except BaseException as error:
                    # Generic execute hatası kesin ret değildir; FAILED yazılmaz.
                    # Commit öncesinde UNKNOWN zaten kurulmuştur; burada da korunur.
                    if commit_in_progress:
                        history = HistoryState.COMMIT_UNKNOWN
                    raise _MigrationExecutionStopped(snapshot(
                        interrupted=_is_interruption(error) or isinstance(error, ApplyApprovalInterrupted))) from None
    except _MigrationExecutionStopped as stopped:
        result = stopped.result
        if getattr(stopped, "__notes__", ()):
            result = replace(result, cleanup_failed=True)
    except BaseException as error:
        # Kilit alınamaması, onay/ayar hatası veya tek başına RELEASE hatası.
        # DDL/history kesinliği korunur; ham error metni veya traceback raporlanmaz.
        if commit_in_progress:
            history = HistoryState.COMMIT_UNKNOWN
        completed_before_cleanup = result is not None
        interrupted = _is_interruption(error) or isinstance(error, ApplyApprovalInterrupted)
        if isinstance(error, ConnectionSetupError):
            interrupted = error.setup_interrupted or error.cleanup_interrupted
        result = snapshot(interrupted=interrupted)
        if isinstance(error, ConnectionSetupError) and error.cleanup_failed:
            result = replace(result, cleanup_failed=True)
        if completed_before_cleanup:
            result = replace(result, cleanup_failed=True)
    finally:
        if connection is not None:
            try:
                # Belirsiz işlemde rollback çağrısı "geri alındı" kanıtı değildir.
                # Özel oturum kapatılır; RUNNING temizleme veya DDL rollback yoktur.
                connection.close()
            except BaseException as error:
                if result is None:
                    result = snapshot()
                result = replace(result, success=False, cleanup_failed=True,
                                 interrupted=result.interrupted or _is_interruption(error))
    return result


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


def show_apply() -> int:
    """Mevcut executor'ı bir kez çağır; toplam sonuç sözleşmesini CLI'ye taşı."""
    try:
        result = execute_one_migration()
    except BaseException:
        # Beklenmeyen dış hata da SQL, sır veya traceback olarak gösterilmez.
        print("Uygulama sonucu alınamadı; manuel inceleme gerekiyor. Otomatik tekrar denemeyin.",
              file=sys.stderr)
        return 1

    code = result.exit_code
    stream = sys.stdout if code == 0 else sys.stderr
    if code == 0 and result.success:
        print("Uygulama başarılı: tek migration doğrulandı ve SUCCESS kaydı kesinleşti.", file=stream)
    elif code == 0 and result.no_pending:
        print("Uygulanacak PENDING migration yok; hiçbir migration uygulanmadı.", file=stream)
    else:
        print(f"Uygulama tamamlanamadı. Aşama: {result.phase.value}; "
              f"DDL: {result.ddl_outcome.value}; geçmiş: {result.history_state.value}.", file=stream)
    if result.cleanup_failed:
        print("Kilit bırakma veya bağlantı temizliği başarısız.", file=stream)
    if result.interrupted:
        print("İşlem kesildi.", file=stream)
    if result.failed_history_confirmed:
        print("FAILED kaydı kesinleşti; oluşturulmuş tablo kalıcı olabilir.", file=stream)
    if result.manual_review_required:
        print("Manuel inceleme gerekiyor; otomatik tekrar denemeyin.", file=stream)
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migration dosyalarını incele, Oracle geçmişini göster veya onayla tek migration uygula."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="Dosya adlarını, versiyonları ve checksum'ları doğrula.")
    commands.add_parser("plan", help="Dosyaları sayısal versiyon sırasıyla ve checksum'larıyla göster.")
    commands.add_parser("status", help="Oracle migration geçmişini yalnızca SELECT ile göster.")
    commands.add_parser("db-plan", help="Yerel dosyaları Oracle geçmişiyle salt okunur karşılaştır.")
    commands.add_parser("apply", help="Gerçek terminalde açık onayla tek uygun migration uygula.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "apply":
        return show_apply()
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
