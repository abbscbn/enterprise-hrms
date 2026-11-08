from ..enterprise import service as enterprise
import os
from ..core.errors import Conflict
from ..db.transactions import transaction
from ..repositories.department import DepartmentRepository
from ..repositories.employee import EmployeeRepository
from ..schemas.department import DepartmentRead
from .common import read_operation, require


class DepartmentService:
    def __init__(self, connection, actor=None):
        self.actor = actor
        self.connection = connection
        self.departments = DepartmentRepository(connection)
        self.employees = EmployeeRepository(connection)

    def get(self, identifier):
        with read_operation():
            return DepartmentRead.model_validate(enterprise.enrich(self.connection,"DEPARTMENT",require(self.departments.get(identifier))))

    def list(self, page, size, **filters):
        with read_operation():
            return {"items": [DepartmentRead.model_validate(enterprise.enrich(self.connection,"DEPARTMENT",row)) for row in self.departments.list(page, size, **filters)],
                    "page": page, "size": size}

    def create(self, data):
        with transaction(self.connection):
            enterprise.lock(self.connection)
            base,extra = enterprise.split_values("DEPARTMENT",data.model_dump())
            identifier = self.departments.create(base)
            enterprise.save(self.connection,"DEPARTMENT",identifier,extra,self.actor,creating=True)
            enterprise.record(self.connection,self.actor,"CREATE","DEPARTMENT",identifier)
            result = DepartmentRead.model_validate(enterprise.enrich(self.connection,"DEPARTMENT",self.departments.get(identifier)))
        return result

    def update(self, identifier, data):
        with transaction(self.connection):
            enterprise.lock(self.connection)
            require(self.departments.get(identifier, for_update=True))
            base,extra = enterprise.split_values('DEPARTMENT',data.model_dump(exclude_unset=True))
            if base:self.departments.update(identifier,base)
            enterprise.save(self.connection,'DEPARTMENT',identifier,extra,self.actor)
            enterprise.record(self.connection,self.actor,'UPDATE','DEPARTMENT',identifier)
            result = DepartmentRead.model_validate(enterprise.enrich(self.connection,"DEPARTMENT",self.departments.get(identifier)))
        return result

    def deactivate(self, identifier):
        with transaction(self.connection):
            enterprise.lock(self.connection)
            current = require(self.departments.get(identifier, for_update=True))
            # Aynı departman kilidi personel ataması/taşınması tarafından da tutulur.
            # COUNT personel satırı kilitlemez; ters kilit sırası oluşturmaz.
            if os.environ.get("HRMS_PLSQL_ENABLED", "0") == "1":
                self.departments.assert_no_active_employees(identifier)
            elif self.employees.has_active(identifier):
                raise Conflict()
            if current["is_active"]:
                self.departments.deactivate(identifier)
            enterprise.record(self.connection,self.actor,'DEACTIVATE','DEPARTMENT',identifier)
            result = DepartmentRead.model_validate(enterprise.enrich(self.connection,"DEPARTMENT",self.departments.get(identifier)))
        return result
