import json
from datetime import datetime, timezone
from uuid import uuid4
from ..core.errors import ApplicationError


def enqueue_employee_created(connection, employee_id):
    event_id = str(uuid4())
    event = {'event_id': event_id, 'event_type': 'EMPLOYEE_CREATED', 'version': 1,
             'employee_id': employee_id, 'occurred_at': datetime.now(timezone.utc).isoformat()}
    with connection.cursor() as cursor:
        cursor.execute('INSERT INTO HR_CORE.HRMS_OUTBOX (EVENT_ID, EVENT_TYPE, PAYLOAD, ATTEMPTS) '
                       'VALUES (:event_id, :event_type, :payload, :attempts)',
                       {'event_id': event_id, 'event_type': event['event_type'],
                        'payload': json.dumps(event, separators=(',', ':')), 'attempts': 0})
        if cursor.rowcount != 1:
            raise ApplicationError()
    return event_id
