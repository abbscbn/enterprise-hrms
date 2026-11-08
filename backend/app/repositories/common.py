from datetime import date, datetime
from decimal import Decimal
from ..core.errors import ApplicationError

# Oracle timezone offset'i sürücünün datetime eşlemesine bırakılmaz.
CREATED_AT = '''TO_CHAR(CREATED_AT, 'YYYY-MM-DD"T"HH24:MI:SS.FF6TZH:TZM') AS CREATED_AT'''


def integer(value):
    if type(value) not in (int, Decimal) or value != int(value):
        raise ApplicationError()
    return int(value)


def record(row, fields):
    if row is None:
        return None
    if not isinstance(row, (tuple, list)) or len(row) != len(fields):
        raise ApplicationError()
    result = dict(zip(fields, row))
    for name in ("id", "department_id"):
        if name in result:
            result[name] = integer(result[name])
    active = integer(result["is_active"])
    if active not in (0, 1):
        raise ApplicationError()
    result["is_active"] = bool(active)
    timestamp = result["created_at"]
    if type(timestamp) is str:
        timestamp = datetime.fromisoformat(timestamp)
    if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None:
        raise ApplicationError()
    result["created_at"] = timestamp
    if "hire_date" in result:
        value = result["hire_date"]
        if type(value) is datetime:
            if value.time().isoformat() != "00:00:00":
                raise ApplicationError()
            value = value.date()
        if type(value) is not date:
            raise ApplicationError()
        result["hire_date"] = value
    return result


def inserted_id(variable):
    values = variable.getvalue()
    if type(values) is not list or len(values) != 1:
        raise ApplicationError()
    value = integer(values[0])
    if value <= 0:
        raise ApplicationError()
    return value


def updated_one(cursor):
    if type(cursor.rowcount) is not int or cursor.rowcount != 1:
        raise ApplicationError()
