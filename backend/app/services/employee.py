import os
from ..events.outbox import enqueue_employee_created
from ..core.errors import Conflict
from ..db.transactions import transaction
from ..repositories.department import DepartmentRepository
from ..repositories.employee import EmployeeRepository
from ..schemas.employee import EmployeeRead
from .common import read_operation, require


class EmployeeService:
    def __init__(self, connection):
        self.connection = connection
        self.departments = DepartmentRepository(connection)
        self.employees = EmployeeRepository(connection)

    def _lock_departments(self, identifiers):
        # Personel değişikliğinde önce personel, sonra departmanlar artan ID ile.
        # Create yalnız hedef departmanı kilitler. Departman pasifleştirme personel
        # satırı kilitlemez. DB dışı yazıcılar bu sözleşmeyi ayrıca izlemelidir.
        return {identifier: require(self.departments.get(identifier, for_update=True))
                for identifier in sorted(set(identifiers))}

    def _ensure_assignment(self, old_department, target):
        if os.environ.get("HRMS_PLSQL_ENABLED", "0") == "1":
            # Package güvenilir kural kaynağıdır; Python'da ikinci aktiflik kontrolü yok.
            self.departments.lock_for_assignment(old_department, target)
        else:
            departments = self._lock_departments([old_department, target])
            if not departments[target]["is_active"]:
                raise Conflict()

    def get(self, identifier):
        with read_operation():
            return EmployeeRead.model_validate(require(self.employees.get(identifier)))

    def list(self, page, size):
        with read_operation():
            return {"items": [EmployeeRead.model_validate(row) for row in self.employees.list(page, size)],
                    "page": page, "size": size}

    def create(self, data):
        with transaction(self.connection):
            self._ensure_assignment(data.department_id, data.department_id)
            identifier = self.employees.create(data.model_dump())
            result = EmployeeRead.model_validate(self.employees.get(identifier))
            if os.environ.get("HRMS_OUTBOX_ENABLED", "0") == "1":
                enqueue_employee_created(self.connection, result.id)
        return result

    def update(self, identifier, data):
        with transaction(self.connection):
            current = require(self.employees.get(identifier, for_update=True))
            changes = data.model_dump(exclude_unset=True)
            old_department = current["department_id"]
            target = changes.get("department_id", old_department)
            if "department_id" in changes:
                self._ensure_assignment(old_department, target)
            else:
                self._lock_departments([old_department])
            self.employees.update(identifier, changes)
            result = EmployeeRead.model_validate(self.employees.get(identifier))
        return result

    def deactivate(self, identifier):
        with transaction(self.connection):
            current = require(self.employees.get(identifier, for_update=True))
            self._lock_departments([current["department_id"]])
            if current["is_active"]:
                self.employees.deactivate(identifier)
            result = EmployeeRead.model_validate(self.employees.get(identifier))
        return result
