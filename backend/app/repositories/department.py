from .common import CREATED_AT, inserted_id, record, updated_one
from ..core.errors import ApplicationError

FIELDS = ("id", "department_code", "department_name", "is_active", "created_at")
SELECT = f"SELECT ID, DEPARTMENT_CODE, DEPARTMENT_NAME, IS_ACTIVE, {CREATED_AT} FROM HR_CORE.DEPARTMENT"


class DepartmentRepository:
    def __init__(self, connection):
        self.connection = connection

    def get(self, identifier, *, for_update=False):
        with self.connection.cursor() as cursor:
            cursor.execute(SELECT + " WHERE ID=:id" + (" FOR UPDATE WAIT 5" if for_update else ""), {"id": identifier})
            return record(cursor.fetchone(), FIELDS)

    def list(self, page, size):
        with self.connection.cursor() as cursor:
            cursor.execute(SELECT + " ORDER BY ID OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY",
                           {"offset": (page - 1) * size, "limit": size})
            return [record(row, FIELDS) for row in cursor.fetchall()]

    def create(self, values):
        with self.connection.cursor() as cursor:
            output = cursor.var(int)
            cursor.execute("INSERT INTO HR_CORE.DEPARTMENT (DEPARTMENT_CODE, DEPARTMENT_NAME) "
                           "VALUES (:department_code, :department_name) RETURNING ID INTO :new_id",
                           {**values, "new_id": output})
            updated_one(cursor)
            return inserted_id(output)

    def update(self, identifier, values):
        if set(values) != {"department_name"}:
            raise ApplicationError()
        with self.connection.cursor() as cursor:
            cursor.execute("UPDATE HR_CORE.DEPARTMENT SET DEPARTMENT_NAME=:department_name WHERE ID=:id",
                           {**values, "id": identifier})
            updated_one(cursor)

    def deactivate(self, identifier):
        with self.connection.cursor() as cursor:
            cursor.execute("UPDATE HR_CORE.DEPARTMENT SET IS_ACTIVE=0 WHERE ID=:id", {"id": identifier})
            updated_one(cursor)
