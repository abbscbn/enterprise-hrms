import oracledb

from ..core.config import load_oracle_settings


class OracleConnectionError(RuntimeError):
    """Ham driver mesajı taşımayan bağlantı/yaşam döngüsü hatası."""

    def __init__(self, message: str, *, cleanup_failed: bool = False):
        super().__init__(message)
        self.cleanup_failed = cleanup_failed


def create_oracle_connection() -> oracledb.Connection:
    """Açık çağrıda özel Thin bağlantı aç; kapatma sorumluluğu çağırana aittir.

    Pool, SQL, oturum değiştirme ve otomatik transaction yönetimi yoktur.
    Kullanım sonrası kapatma için get_db_connection dependency'si tercih edilir.
    """
    settings = load_oracle_settings()
    try:
        if not oracledb.is_thin_mode():
            raise OracleConnectionError("Backend yalnızca Oracle Thin modunu destekler.")
        params = oracledb.ConnectParams(
            host=settings.host, port=settings.port, service_name=settings.service, retry_count=0,
        )
        connection = oracledb.connect(user=settings.user, password=settings.password, params=params)
    except OracleConnectionError:
        raise
    except Exception:
        raise OracleConnectionError("Oracle bağlantısı kurulamadı.") from None

    try:
        connection.autocommit = False
    except BaseException as setup_error:
        cleanup_failed = False
        try:
            connection.close()
        except BaseException:
            cleanup_failed = True
        if isinstance(setup_error, Exception):
            raise OracleConnectionError("Oracle bağlantısı hazırlanamadı.",
                                        cleanup_failed=cleanup_failed) from None
        if cleanup_failed:
            setup_error.add_note("Kurulum kesildi ve Oracle bağlantısının kapatılması doğrulanamadı.")
        raise
    return connection
