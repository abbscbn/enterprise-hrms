"""İstemciye yalnız sabit mesaj/kod taşıyan uygulama hataları."""
import oracledb
from fastapi import Request
from fastapi.responses import JSONResponse
from ..db.oracle import OracleConnectionError


class ApplicationError(RuntimeError):
    status_code = 500
    code = "internal_error"
    message = "İşlem tamamlanamadı."

    def __init__(self):
        super().__init__(self.message)


class NotFound(ApplicationError):
    status_code = 404
    code = "not_found"
    message = "Kayıt bulunamadı."


class Conflict(ApplicationError):
    status_code = 409
    code = "conflict"
    message = "İşlem mevcut kayıtlar veya iş kurallarıyla çakışıyor."


class UniqueConflict(Conflict):
    code = "unique_conflict"
    message = "Benzersiz olması gereken değer zaten kullanılıyor."


class ReferenceConflict(Conflict):
    code = "reference_conflict"
    message = "İlişkili kayıt mevcut değil veya ilişki işleme izin vermiyor."


class LockConflict(Conflict):
    code = "lock_conflict"
    message = "Kayıt başka bir işlem tarafından kullanılıyor. İşlem otomatik tekrarlanmadı."


class CommitOutcomeUnknown(ApplicationError):
    code = "commit_outcome_unknown"
    message = "Kaydetme sonucu doğrulanamadı. Aynı isteği tekrar göndermeyin; manuel inceleme gerekiyor."


class ConnectionCleanupError(ApplicationError):
    code = "connection_cleanup_failed"
    message = "Bağlantı kapatılması doğrulanamadı. Yazma tamamlanmış olabilir; isteği tekrarlamayın, manuel inceleme gerekiyor."


class TransactionCleanupError(ApplicationError):
    code = "transaction_cleanup_failed"
    message = "İşlem iptalinin tamamlandığı doğrulanamadı; manuel inceleme gerekiyor."


def safe_error(error: Exception) -> ApplicationError:
    if isinstance(error, ApplicationError):
        return error
    if isinstance(error, OracleConnectionError) and error.cleanup_failed:
        return ConnectionCleanupError()
    if isinstance(error, oracledb.Error) and error.args:
        code = getattr(error.args[0], "code", None)
        if type(code) is int:
            if code == 20003:
                return NotFound()
            if code in (20001, 20002):
                return Conflict()
            if code == 1:
                return UniqueConflict()
            if code in (2291, 2292):
                return ReferenceConflict()
            if code in (54, 60, 30006):
                return LockConflict()
    return ApplicationError()


async def application_error_handler(request: Request, error: ApplicationError):
    return JSONResponse(status_code=error.status_code,
                        content={"error": {"code": error.code, "message": error.message}})


async def internal_error_handler(request: Request, error: Exception):
    return await application_error_handler(request, safe_error(error))
