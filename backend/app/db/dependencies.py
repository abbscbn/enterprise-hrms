from collections.abc import Iterator

import oracledb

from .oracle import OracleConnectionError, create_oracle_connection


def get_db_connection() -> Iterator[oracledb.Connection]:
    """Depends(get_db_connection) için istek kapsamlı bağlantı; commit/rollback yapmaz."""
    connection = create_oracle_connection()
    original_error: BaseException | None = None
    try:
        yield connection
    except BaseException as error:
        original_error = error
        raise
    finally:
        try:
            connection.close()
        except BaseException as cleanup_error:
            if original_error is not None:
                original_error.add_note("Oracle bağlantısı kapatılamadı; bağlantı yaşam döngüsü incelenmelidir.")
            elif isinstance(cleanup_error, Exception):
                raise OracleConnectionError("Oracle bağlantısı kapatılamadı.", cleanup_failed=True) from None
            else:
                raise
