from ..enterprise import service as enterprise
import os
from ..events.outbox import enqueue_employee_created
from ..core.errors import Conflict
from ..db.transactions import transaction
from ..repositories.department import DepartmentRepository
from ..repositories.employee import EmployeeRepository
from ..schemas.employee import EmployeeRead
from .common import read_operation, require


class EmployeeService:
    def __init__(self, connection, actor=None):
        self.actor = actor
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
        enterprise.authorize(self.connection,self.actor,identifier)
        with read_operation():
            return EmployeeRead.model_validate(enterprise.enrich(self.connection,"EMPLOYEE",require(self.employees.get(identifier))))

    def list(self, page, size, **filters):
        with read_operation():
            return {"items": [EmployeeRead.model_validate(enterprise.enrich(self.connection,"EMPLOYEE",row)) for row in self.employees.list(page, size, **filters)],
                    "page": page, "size": size}

    def create(self, data):
        with transaction(self.connection):
            enterprise.lock(self.connection)
            self._ensure_assignment(data.department_id, data.department_id)
            base,extra = enterprise.split_values("EMPLOYEE",data.model_dump())
            identifier = self.employees.create(base)
            enterprise.save(self.connection,"EMPLOYEE",identifier,extra,self.actor,creating=True)
            enterprise.record(self.connection,self.actor,"CREATE","EMPLOYEE",identifier)
            result = EmployeeRead.model_validate(enterprise.enrich(self.connection,"EMPLOYEE",self.employees.get(identifier)))
            if os.environ.get("HRMS_OUTBOX_ENABLED", "0") == "1":
                enqueue_employee_created(self.connection, result.id)
        return result

    def update(self, identifier, data):
        enterprise.authorize(self.connection,self.actor,identifier,data.model_dump(exclude_unset=True))
        with transaction(self.connection):
            enterprise.lock(self.connection)
            current = require(self.employees.get(identifier, for_update=True))
            changes,extra = enterprise.split_values("EMPLOYEE",data.model_dump(exclude_unset=True))
            old_department = current["department_id"]
            target = changes.get("department_id", old_department)
            if "department_id" in changes:
                self._ensure_assignment(old_department, target)
            else:
                self._lock_departments([old_department])
            if extra.get('employment_status') in ('ACTIVE','ON_LEAVE') and not current['is_active']:
                raise Conflict()
            if changes:self.employees.update(identifier, changes)
            if extra.get('employment_status')=='TERMINATED' and current['is_active']:
                self.employees.deactivate(identifier)
            enterprise.save(self.connection,'EMPLOYEE',identifier,extra,self.actor)
            enterprise.record(self.connection,self.actor,'UPDATE','EMPLOYEE',identifier)
            result = EmployeeRead.model_validate(enterprise.enrich(self.connection,"EMPLOYEE",self.employees.get(identifier)))
        return result

    def deactivate(self, identifier):
        with transaction(self.connection):
            enterprise.lock(self.connection)
            current = require(self.employees.get(identifier, for_update=True))
            self._lock_departments([current["department_id"]])
            if current["is_active"]:
                self.employees.deactivate(identifier)
            enterprise.save(self.connection,'EMPLOYEE',identifier,{'employment_status':'TERMINATED'},self.actor)
            enterprise.record(self.connection,self.actor,'DEACTIVATE','EMPLOYEE',identifier)
            result = EmployeeRead.model_validate(enterprise.enrich(self.connection,"EMPLOYEE",self.employees.get(identifier)))
        return result
