from .common import CREATED_AT, inserted_id, integer, record, updated_one
from ..core.errors import ApplicationError

FIELDS = ("id", "employee_no", "first_name", "last_name", "email", "hire_date", "department_id", "is_active", "created_at")
SELECT = f"SELECT ID, EMPLOYEE_NO, FIRST_NAME, LAST_NAME, EMAIL, HIRE_DATE, DEPARTMENT_ID, IS_ACTIVE, {CREATED_AT} FROM HR_CORE.EMPLOYEE"
# SQL tanımlayıcıları yalnız bu sabit allowlist'ten gelir; istemci metni SQL'e eklenmez.
UPDATE_COLUMNS = {"first_name": "FIRST_NAME", "last_name": "LAST_NAME", "email": "EMAIL",
                  "hire_date": "HIRE_DATE", "department_id": "DEPARTMENT_ID"}


class EmployeeRepository:
    def __init__(self, connection):
        self.connection = connection

    def get(self, identifier, *, for_update=False):
        with self.connection.cursor() as cursor:
            cursor.execute(SELECT + " WHERE ID=:id" + (" FOR UPDATE WAIT 5" if for_update else ""), {"id": identifier})
            return record(cursor.fetchone(), FIELDS)

    def list(self, page, size, search=None, department_id=None, status=None):
        sql=SELECT
        params={"offset":(page-1)*size,"limit":size};conditions=[]
        if status:
            sql+=' LEFT JOIN HR_CORE.EMPLOYEE_PROFILE P ON P.EMPLOYEE_ID=ID'
            conditions.append("COALESCE(P.EMPLOYMENT_STATUS,CASE WHEN IS_ACTIVE=1 THEN 'ACTIVE' ELSE 'TERMINATED' END)=:status")
            params['status']=status
        if department_id is not None:conditions.append('DEPARTMENT_ID=:department_id');params['department_id']=department_id
        if search:
            conditions.append("INSTR(UPPER(FIRST_NAME||' '||LAST_NAME||' '||EMPLOYEE_NO||' '||EMAIL),UPPER(:search))>0")
            params['search']=search
        if conditions:sql+=' WHERE '+' AND '.join(conditions)
        with self.connection.cursor() as cursor:
            cursor.execute(sql + " ORDER BY ID OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY",params)
            return [record(row,FIELDS) for row in cursor.fetchall()]

    def has_active(self, department_id):
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM HR_CORE.EMPLOYEE WHERE DEPARTMENT_ID=:department_id AND IS_ACTIVE=1",
                           {"department_id": department_id})
            row = cursor.fetchone()
            if not isinstance(row, (tuple, list)) or len(row) != 1:
                raise ApplicationError()
            count = integer(row[0])
            if count < 0:
                raise ApplicationError()
            return count > 0

    def create(self, values):
        with self.connection.cursor() as cursor:
            output = cursor.var(int)
            cursor.execute("INSERT INTO HR_CORE.EMPLOYEE (EMPLOYEE_NO, FIRST_NAME, LAST_NAME, EMAIL, HIRE_DATE, DEPARTMENT_ID) "
                           "VALUES (:employee_no, :first_name, :last_name, :email, :hire_date, :department_id) RETURNING ID INTO :new_id",
                           {**values, "new_id": output})
            updated_one(cursor)
            return inserted_id(output)

    def update(self, identifier, values):
        if not values or not set(values).issubset(UPDATE_COLUMNS):
            raise ApplicationError()
        assignments = ", ".join(f"{column}=:{key}" for key, column in UPDATE_COLUMNS.items() if key in values)
        with self.connection.cursor() as cursor:
            cursor.execute(f"UPDATE HR_CORE.EMPLOYEE SET {assignments} WHERE ID=:id", {**values, "id": identifier})
            updated_one(cursor)

    def deactivate(self, identifier):
        with self.connection.cursor() as cursor:
            cursor.execute("UPDATE HR_CORE.EMPLOYEE SET IS_ACTIVE=0 WHERE ID=:id", {"id": identifier})
            updated_one(cursor)
