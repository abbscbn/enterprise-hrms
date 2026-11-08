from fastapi import APIRouter,Depends
from ..security.dependencies import get_current_user
from ..db.dependencies import get_db_connection
from ..services.employee import EmployeeService
from ..services.department import DepartmentService
from ..schemas.employee import EmployeeRead,EmployeeUpdate
from ..core.errors import NotFound

router=APIRouter(tags=['profiles and organization'])

@router.get('/me/profile',response_model=EmployeeRead)
def profile(actor=Depends(get_current_user),connection=Depends(get_db_connection,scope='function')):
    if actor.employee_id is None:raise NotFound()
    return EmployeeService(connection,actor).get(actor.employee_id)

@router.patch('/me/profile',response_model=EmployeeRead)
def update_profile(data:EmployeeUpdate,actor=Depends(get_current_user),connection=Depends(get_db_connection,scope='function')):
    if actor.employee_id is None:raise NotFound()
    return EmployeeService(connection,actor).update(actor.employee_id,data)

@router.get('/department-hierarchy')
def hierarchy(actor=Depends(get_current_user),connection=Depends(get_db_connection,scope='function')):
    result=DepartmentService(connection,actor).list(1,1000)
    return {'items':result['items'],'truncated':len(result['items'])==1000}
