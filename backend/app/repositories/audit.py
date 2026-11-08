from uuid import uuid4

def write(connection,actor,action,entity_type,entity_id=None,outcome='SUCCESS',request_id=None):
    with connection.cursor() as cursor:
        cursor.execute('INSERT INTO HR_CORE.HRMS_AUDIT (ACTOR_ID,ACTION,ENTITY_TYPE,ENTITY_ID,OUTCOME,REQUEST_ID) '
            'VALUES (:actor,:action,:entity_type,:entity_id,:outcome,:request_id)',
            {'actor':actor.id if actor else None,'action':action,'entity_type':entity_type,'entity_id':entity_id,
             'outcome':outcome,'request_id':request_id or (actor.request_id if actor else str(uuid4()))})
