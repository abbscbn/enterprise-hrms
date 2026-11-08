from ..core.errors import Conflict
from ..db.transactions import transaction
from ..repositories.department import DepartmentRepository
from ..repositories.employee import EmployeeRepository
from ..schemas.department import DepartmentRead
from .common import read_operation, require


class DepartmentService:
    def __init__(self, connection):
        self.connection = connection
        self.departments = DepartmentRepository(connection)
        self.employees = EmployeeRepository(connection)

    def get(self, identifier):
        with read_operation():
            return DepartmentRead.model_validate(require(self.departments.get(identifier)))

    def list(self, page, size):
        with read_operation():
            return {"items": [DepartmentRead.model_validate(row) for row in self.departments.list(page, size)],
                    "page": page, "size": size}

    def create(self, data):
        with transaction(self.connection):
            identifier = self.departments.create(data.model_dump())
            result = DepartmentRead.model_validate(self.departments.get(identifier))
        return result

    def update(self, identifier, data):
        with transaction(self.connection):
            require(self.departments.get(identifier, for_update=True))
            self.departments.update(identifier, data.model_dump(exclude_unset=True))
            result = DepartmentRead.model_validate(self.departments.get(identifier))
        return result

    def deactivate(self, identifier):
        with transaction(self.connection):
            current = require(self.departments.get(identifier, for_update=True))
            # Aynı departman kilidi personel ataması/taşınması tarafından da tutulur.
            # COUNT personel satırı kilitlemez; ters kilit sırası oluşturmaz.
            if self.employees.has_active(identifier):
                raise Conflict()
            if current["is_active"]:
                self.departments.deactivate(identifier)
            result = DepartmentRead.model_validate(self.departments.get(identifier))
        return result
