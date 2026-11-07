"""Ortam değişkenleriyle Oracle bağlantısını yalnızca SELECT kullanarak kontrol et.

Gerekli değişkenler: DB_HOST, DB_PORT, DB_SERVICE, DB_USER, DB_PASSWORD.
Proje kökündeki .env dosyası yüklenir; mevcut ortam değişkenleri önceliklidir.
python-oracledb Thin modu kullanılır; Oracle Instant Client gerekmez.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env", override=False)

    variable_names = ("DB_HOST", "DB_PORT", "DB_SERVICE", "DB_USER", "DB_PASSWORD")
    settings = {name: os.environ.get(name, "") for name in variable_names}
    missing = [name for name, value in settings.items() if not value.strip()]
    if missing:
        print(
            "Hata: Gerekli ortam değişkenleri eksik veya boş: " + ", ".join(missing),
            file=sys.stderr,
        )
        return 1

    try:
        port = int(settings["DB_PORT"])
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        print("Hata: DB_PORT, 1 ile 65535 arasında bir tam sayı olmalıdır.", file=sys.stderr)
        return 1

    try:
        import oracledb
    except ImportError:
        print(
            "Hata: python-oracledb bağımlılığı bulunamadı. "
            "Onay alındıktan sonra requirements.txt bağımlılıkları kurulmalıdır.",
            file=sys.stderr,
        )
        return 1

    try:
        params = oracledb.ConnectParams(
            host=settings["DB_HOST"],
            port=port,
            service_name=settings["DB_SERVICE"],
        )
        with oracledb.connect(
            user=settings["DB_USER"],
            password=settings["DB_PASSWORD"],
            params=params,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT SYS_CONTEXT('USERENV', 'SESSION_USER'), "
                    "SYS_CONTEXT('USERENV', 'CON_NAME') FROM DUAL"
                )
                row = cursor.fetchone()
                if row is None:
                    print("Hata: Bağlantı sorgusu sonuç döndürmedi.", file=sys.stderr)
                    return 1
                username, pdb_name = row
        print("Oracle bağlantısı başarılı.")
        print(f"Oracle kullanıcı adı: {username}")
        print(f"PDB adı: {pdb_name}")
        return 0
    except oracledb.Error as exc:
        error = exc.args[0] if exc.args else None
        error_code = getattr(error, "full_code", "")
        explanations = {
            "ORA-01017": "Kullanıcı adı veya parola geçersiz.",
            "ORA-28000": "Oracle kullanıcı hesabı kilitli.",
            "ORA-28001": "Oracle kullanıcı parolasının süresi dolmuş.",
            "ORA-12514": "İstenen servis listener tarafından tanınmıyor; DB_SERVICE değerini kontrol edin.",
            "ORA-12541": "Oracle listener erişilemiyor; container, host ve port bilgilerini kontrol edin.",
            "DPY-6001": "İstenen servis bulunamadı; DB_SERVICE değerini kontrol edin.",
            "DPY-6005": "Oracle sunucusuna erişilemiyor; container, ağ, host ve port bilgilerini kontrol edin.",
        }
        explanation = explanations.get(
            error_code,
            "Oracle bağlantısı veya SELECT sorgusu başarısız oldu. "
            "Bağlantı ayarlarını ve veritabanının erişilebilirliğini kontrol edin.",
        )
        # Ham hata metnini yazdırmayarak bağlantı bilgilerinin sızmasını önle.
        label = f" ({error_code})" if error_code else ""
        print(f"Hata{label}: {explanation}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
