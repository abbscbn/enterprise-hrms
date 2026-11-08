import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class OracleConfigurationError(ValueError):
    """Değerleri veya dosya içeriğini göstermeyen konfigürasyon hatası."""


@dataclass(frozen=True)
class OracleSettings:
    host: str = field(repr=False)
    port: int = field(repr=False)
    service: str = field(repr=False)
    user: str = field(repr=False)
    password: str = field(repr=False)


def load_oracle_settings() -> OracleSettings:
    """Yalnız bağlantı istendiğinde çağrılır; import sırasında ayar okunmaz."""
    try:
        load_dotenv(PROJECT_ROOT / ".env", override=False)
    except Exception:
        raise OracleConfigurationError("Oracle ortam yapılandırması yüklenemedi.") from None

    names = ("DB_HOST", "DB_PORT", "DB_SERVICE", "DB_USER", "DB_PASSWORD")
    values = {name: os.environ.get(name, "") for name in names}
    missing = [name for name, value in values.items() if not value.strip()]
    if missing:
        raise OracleConfigurationError("Eksik veya boş ortam değişkenleri: " + ", ".join(missing))
    try:
        port = int(values["DB_PORT"])
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise OracleConfigurationError("DB_PORT, 1 ile 65535 arasında tam sayı olmalıdır.") from None

    return OracleSettings(values["DB_HOST"], port, values["DB_SERVICE"],
                          values["DB_USER"], values["DB_PASSWORD"])
