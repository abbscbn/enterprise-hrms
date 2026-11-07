"""SCHEMA_MIGRATIONS yapısını kontrol et; yalnızca onayla bootstrap uygula."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SQL_FILE = PROJECT_ROOT / "database/bootstrap/001__create_schema_migrations.sql"
TABLE_NAME = "SCHEMA_MIGRATIONS"
CONFIRMATION = "HR_CORE.SCHEMA_MIGRATIONS OLUSTUR"

# Genel amaçlı SQL ayrıştırıcısı yerine yalnızca onaylı tablo tanımına izin ver.
EXPECTED_SQL = """
CREATE TABLE HR_CORE.SCHEMA_MIGRATIONS (
    VERSION NUMBER(4, 0) NOT NULL,
    SCRIPT_NAME VARCHAR2(255 CHAR) NOT NULL,
    CHECKSUM VARCHAR2(64 CHAR) NOT NULL,
    STATUS VARCHAR2(7 CHAR) NOT NULL,
    STARTED_AT TIMESTAMP(6) WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    FINISHED_AT TIMESTAMP(6) WITH TIME ZONE,
    ERROR_MESSAGE CLOB,
    CONSTRAINT PK_SCHEMA_MIGRATIONS PRIMARY KEY (VERSION),
    CONSTRAINT CK_SCHEMA_MIGRATIONS_STATUS
        CHECK (STATUS IN ('RUNNING', 'SUCCESS', 'FAILED'))
)
"""
SQL_TOKEN = re.compile(
    r"(?P<ignored>\s+|--[^\r\n]*|/\*.*?\*/)"
    r"|(?P<literal>'(?:[^']|'')*')"
    r"|(?P<word>[A-Za-z_][A-Za-z_0-9$#]*)"
    r"|(?P<other>[0-9]+|[(),.;])",
    re.DOTALL,
)


def sql_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    position = 0
    while position < len(text):
        match = SQL_TOKEN.match(text, position)
        if match is None:
            raise ValueError("Bootstrap SQL dosyasında izin verilmeyen içerik var.")
        position = match.end()
        if match.lastgroup != "ignored":
            token = match.group()
            tokens.append(token.upper() if match.lastgroup == "word" else token)
    return tokens


def read_create_statement() -> tuple[str, str]:
    content = SQL_FILE.read_bytes()
    tokens = sql_tokens(content.decode("utf-8"))
    if tokens and tokens[-1] == ";":
        tokens.pop()
    if tokens != sql_tokens(EXPECTED_SQL):
        raise ValueError(
            "Bootstrap SQL dosyası onaylı CREATE TABLE tanımıyla eşleşmiyor. "
            "Ek SQL/PLSQL veya farklı tablo tanımı çalıştırılamaz."
        )
    # Doğrulanan dosyanın token'ları kullanılır; yorumlar ve son ';' gönderilmez.
    return " ".join(tokens), hashlib.sha256(content).hexdigest()


EXPRESSION_TOKEN = re.compile(
    r"(?P<space>\s+)"
    r"|(?P<literal>'(?:[^']|'')*')"
    r'|(?P<quoted>"(?:[^"]|"")*")'
    r"|(?P<word>[A-Za-z_][A-Za-z_0-9$#]*)"
    r"|(?P<symbol>[(),])"
)


def normalize_expression(value: str | None) -> tuple[tuple[str, str], ...]:
    """Literal içeriğini koru; yalnızca ifadeyi saran dengeli parantezleri kaldır."""
    text = value or ""
    tokens: list[tuple[str, str]] = []
    position = 0
    depth = 0
    while position < len(text):
        match = EXPRESSION_TOKEN.match(text, position)
        if match is None:
            raise ValueError("Metadata ifadesi desteklenen dar SQL formatına uymuyor.")
        position = match.end()
        kind, token = match.lastgroup, match.group()
        if kind == "space":
            continue
        if kind == "word":
            token = token.upper()
        elif kind == "quoted":
            identifier = token[1:-1].replace('""', '"')
            # "STATUS" ile STATUS eşdeğerdir; "status" ayrı bir tanımlayıcıdır.
            if re.fullmatch(r"[A-Z_][A-Z_0-9$#]*", identifier):
                kind, token = "word", identifier
        elif kind == "symbol":
            if token == "(":
                depth += 1
            elif token == ")":
                depth -= 1
                if depth < 0:
                    raise ValueError("Metadata ifadesinde dengesiz parantez var.")
        tokens.append((kind, token))
    if depth:
        raise ValueError("Metadata ifadesinde dengesiz parantez var.")

    while tokens and tokens[0] == ("symbol", "(") and tokens[-1] == ("symbol", ")"):
        depth = 0
        for index, item in enumerate(tokens):
            if item == ("symbol", "("):
                depth += 1
            elif item == ("symbol", ")"):
                depth -= 1
            if depth == 0:
                break
        if index != len(tokens) - 1:
            break
        tokens = tokens[1:-1]
    return tuple(tokens)


def table_exists(cursor) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM USER_TABLES WHERE TABLE_NAME = :table_name",
        table_name=TABLE_NAME,
    )
    return cursor.fetchone()[0] == 1


def validate_table(cursor) -> None:
    """Salt okunur metadata sorgularıyla beklenen yapıyı doğrula."""
    cursor.execute(
        "SELECT COLUMN_NAME, DATA_TYPE, DATA_PRECISION, DATA_SCALE, "
        "CHAR_LENGTH, CHAR_USED, NULLABLE, DATA_DEFAULT, "
        "HIDDEN_COLUMN, VIRTUAL_COLUMN, IDENTITY_COLUMN "
        "FROM USER_TAB_COLS WHERE TABLE_NAME = :table_name ORDER BY COLUMN_ID",
        table_name=TABLE_NAME,
    )
    rows = cursor.fetchall()
    expected = {
        "VERSION": ("NUMBER", 4, 0, None, "N", None),
        "SCRIPT_NAME": ("VARCHAR2", None, None, 255, "N", None),
        "CHECKSUM": ("VARCHAR2", None, None, 64, "N", None),
        "STATUS": ("VARCHAR2", None, None, 7, "N", None),
        "STARTED_AT": ("TIMESTAMP(6) WITH TIME ZONE", None, 6, None, "N", "SYSTIMESTAMP"),
        "FINISHED_AT": ("TIMESTAMP(6) WITH TIME ZONE", None, 6, None, "Y", None),
        "ERROR_MESSAGE": ("CLOB", None, None, None, "Y", None),
    }
    errors: list[str] = []
    actual_names = {row[0] for row in rows}
    for name in sorted(set(expected) - actual_names):
        errors.append(f"{name}: beklenen kolon eksik.")
    for name in sorted(actual_names - set(expected)):
        errors.append(f"{name}: beklenmeyen ek kolon.")
    for name, kind, precision, scale, length, semantics, nullable, default, hidden, virtual, identity in rows:
        if name not in expected:
            continue
        exp_kind, exp_precision, exp_scale, exp_length, exp_nullable, exp_default = expected[name]
        fields = [
            ("DATA_TYPE", kind, exp_kind),
            ("DATA_PRECISION", precision, exp_precision),
            ("NULLABLE", nullable, exp_nullable),
            ("HIDDEN_COLUMN", hidden, "NO"),
            ("VIRTUAL_COLUMN", virtual, "NO"),
            ("IDENTITY_COLUMN", identity, "NO"),
        ]
        if exp_scale is not None:
            fields.append(("DATA_SCALE", scale, exp_scale))
        if exp_length is not None:
            fields.extend([
                ("CHAR_LENGTH", length, exp_length),
                ("CHAR_USED", semantics, "C"),
            ])
        for field, actual, wanted in fields:
            if actual != wanted:
                errors.append(f"{name}.{field}: beklenen {wanted!r}, bulunan {actual!r}.")
        try:
            default_matches = normalize_expression(default) == normalize_expression(exp_default)
        except ValueError:
            default_matches = False
        if not default_matches:
            # Varsayılan ifadeler hassas literal içerebilir; ham içerikleri yazdırma.
            errors.append(f"{name}.DATA_DEFAULT: beklenen varsayılan ifadeyle eşleşmiyor.")

    cursor.execute(
        "SELECT CONSTRAINT_NAME, CONSTRAINT_TYPE, STATUS, VALIDATED, "
        "DEFERRABLE, SEARCH_CONDITION_VC "
        "FROM USER_CONSTRAINTS WHERE TABLE_NAME = :table_name",
        table_name=TABLE_NAME,
    )
    constraints = cursor.fetchall()
    primary_keys = [row for row in constraints if row[1] == "P"]
    if len(primary_keys) != 1:
        errors.append("Tam olarak bir primary key bulunmalıdır.")
    else:
        cursor.execute(
            "SELECT COLUMN_NAME FROM USER_CONS_COLUMNS "
            "WHERE TABLE_NAME = :table_name AND CONSTRAINT_NAME = :constraint_name "
            "ORDER BY POSITION",
            table_name=TABLE_NAME,
            constraint_name=primary_keys[0][0],
        )
        if [row[0] for row in cursor.fetchall()] != ["VERSION"]:
            errors.append("Primary key yalnızca VERSION kolonunu içermelidir.")

    status_checks = 0
    mandatory = {name for name, spec in expected.items() if spec[4] == "N"}
    not_null_checks = {normalize_expression(f"{name} IS NOT NULL") for name in mandatory}
    expected_status = normalize_expression("STATUS IN ('RUNNING', 'SUCCESS', 'FAILED')")
    for name, kind, status, validated, deferrable, condition in constraints:
        if status != "ENABLED" or validated != "VALIDATED" or deferrable != "NOT DEFERRABLE":
            errors.append(f"{name}: constraint etkin, doğrulanmış ve ertelenemez olmalıdır.")
        if kind == "C":
            try:
                expression = normalize_expression(condition)
            except ValueError:
                errors.append(f"{name}.SEARCH_CONDITION_VC: desteklenmeyen veya bozuk CHECK ifadesi.")
                continue
            if expression == expected_status:
                status_checks += 1
            elif expression not in not_null_checks:
                errors.append(f"{name}.SEARCH_CONDITION_VC: beklenmeyen CHECK constraint tanımı.")
        elif kind != "P":
            errors.append(f"{name}: beklenmeyen constraint türü.")
    if status_checks != 1:
        errors.append("RUNNING, SUCCESS ve FAILED değerlerini sınırlayan CHECK constraint farklı veya eksik.")

    if errors:
        raise ValueError("Tablo yapısı beklenen tanımla eşleşmiyor:\n- " + "\n- ".join(errors))


def run(args, oracledb, load_dotenv) -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
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
    if not oracledb.is_thin_mode():
        raise ValueError("Bu araç yalnızca python-oracledb Thin modunda çalışır.")

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
            user, pdb = cursor.fetchone()
            if user != "HR_CORE" or pdb != "FREEPDB1":
                raise ValueError("Hedef oturum HR_CORE / FREEPDB1 olmalıdır; işlem durduruldu.")
            print("Hedef: HR_CORE / FREEPDB1")
            if table_exists(cursor):
                validate_table(cursor)
                print("SCHEMA_MIGRATIONS mevcut ve yapısı uygun. Hiçbir değişiklik yapılmadı.")
                return 0

            statement, checksum = read_create_statement()
            print("SCHEMA_MIGRATIONS bulunamadı. Plan: onaylı bootstrap tablosunu oluşturmak.")
            print(f"SQL dosyası: {SQL_FILE.relative_to(PROJECT_ROOT)}")
            print(f"SHA-256: {checksum}")
            if not args.apply:
                print("Yalnızca kontrol ve planlama tamamlandı; CREATE TABLE çalıştırılmadı.")
                return 0

            print("DDL implicit commit yapar; başarılı CREATE TABLE rollback ile geri alınamaz.")
            if not sys.stdin.isatty():
                raise ValueError("--apply için etkileşimli terminalde açık onay gereklidir.")
            answer = input(f"Onaylamak için tam olarak '{CONFIRMATION}' yazın: ")
            if answer != CONFIRMATION:
                print("Onay verilmedi; tablo oluşturulmadı.")
                return 1
            # Kullanıcı onayından sonra yalnızca bellekte doğrulanmış statement çalışır.
            # Ayrı bağlantıda daha önce DML yapılmadığı için implicit commit veri taşımaz.
            if table_exists(cursor):
                validate_table(cursor)
                print("Tablo bu sırada oluşturulmuş ve yapısı uygun; değişiklik yapılmadı.")
                return 0
            cursor.execute(statement)
            try:
                validate_table(cursor)
            except (ValueError, oracledb.Error):
                print(
                    "CREATE TABLE tamamlandı ancak son yapı doğrulaması başarısız. "
                    "DDL geri alınmadı; tabloyu silmeden manuel inceleyin.",
                    file=sys.stderr,
                )
                raise
            print("SCHEMA_MIGRATIONS oluşturuldu ve yapısı doğrulandı.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SCHEMA_MIGRATIONS bootstrap kontrolü; varsayılan mod salt okunurdur."
    )
    parser.add_argument(
        "--apply", action="store_true", help="Tablo eksikse terminal onayıyla oluştur."
    )
    args = parser.parse_args()
    try:
        import oracledb
        from dotenv import load_dotenv
    except ImportError:
        print("Hata: python-oracledb ve python-dotenv bağımlılıkları gereklidir.", file=sys.stderr)
        return 1
    try:
        return run(args, oracledb, load_dotenv)
    except oracledb.Error as exc:
        # Ham sürücü hata mesajları bağlantı bilgileri içerebilir; yazdırılmaz.
        detail = exc.args[0] if exc.args else None
        code = getattr(detail, "full_code", "")
        if not re.fullmatch(r"(?:ORA|DPY|DPI)-[0-9]+", code or ""):
            code = "ORACLE_ERROR"
        messages = {
            "ORA-01017": "Kullanıcı adı veya parola geçersiz.",
            "ORA-01031": "Gerekli yetki bulunmuyor; otomatik yetki değişikliği yapılmadı.",
            "ORA-00955": "Aynı isimde bir nesne mevcut veya eşzamanlı oluşturuldu; manuel inceleyin.",
            "ORA-01950": "Tablespace kotası yetersiz; otomatik kota değişikliği yapılmadı.",
            "ORA-28000": "Oracle kullanıcı hesabı kilitli.",
            "ORA-28001": "Oracle kullanıcı parolasının süresi dolmuş.",
            "ORA-12514": "Oracle servisi bulunamadı; servis ayarını kontrol edin.",
            "ORA-12541": "Oracle listener erişilemiyor.",
            "DPY-6001": "Oracle servisi bulunamadı.",
            "DPY-6005": "Oracle sunucusuna erişilemiyor; container ve bağlantı ayarlarını kontrol edin.",
        }
        print(
            f"Hata ({code}): " + messages.get(
                code, "Oracle işlemi başarısız. Bağlantı, metadata erişimi ve tablo durumunu inceleyin."
            ),
            file=sys.stderr,
        )
        return 1
    except ValueError as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("Hata: Dosya okuma veya terminal işlemi başarısız; yol ve erişimi kontrol edin.", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nİşlem iptal edildi; varsa tamamlanan DDL geri alınmaz.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
