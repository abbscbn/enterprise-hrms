"""Transaction sınırı service'e, bağlantı yaşam döngüsü dependency'ye aittir."""
from contextlib import contextmanager
from .session import validate_target_session
from ..core.errors import ApplicationError, CommitOutcomeUnknown, TransactionCleanupError, safe_error


@contextmanager
def transaction(connection):
    if connection.autocommit is not False:
        raise ApplicationError()
    try:
        validate_target_session(connection)
        yield
    except BaseException as error:
        try:
            connection.rollback()
        except BaseException:
            if isinstance(error, Exception):
                raise TransactionCleanupError() from None
            error.add_note("Rollback doğrulanamadı; manuel inceleme gerekiyor.")
        if isinstance(error, Exception):
            raise safe_error(error) from None
        raise
    # Bu aşamadaki her hata belirsizdir. Rollback/retry sonucu kanıtlayamaz.
    try:
        connection.commit()
    except BaseException:
        raise CommitOutcomeUnknown() from None
