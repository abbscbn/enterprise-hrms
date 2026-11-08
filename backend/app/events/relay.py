"""At-least-once relay. DB commit exception => fatal stop; no automatic Oracle retry."""
import json
import os
import pika
from ..db.oracle import create_oracle_connection
from ..db.session import validate_target_session

from .broker import topology, broker_parameters, EXCHANGE, ROUTING_KEY


def relay_once(connection, channel):
    validate_target_session(connection)
    cursor = connection.cursor()
    try:
        cursor.arraysize = 1
        cursor.prefetchrows = 0
        cursor.execute('SELECT ID, EVENT_ID, PAYLOAD FROM HR_CORE.HRMS_OUTBOX '
                       'WHERE PUBLISHED_AT IS NULL AND ATTEMPTS < 10 ORDER BY ID FOR UPDATE SKIP LOCKED')
        row = cursor.fetchone()
        if row is None:
            connection.rollback()  # read/lock transaction only; no ambiguous write commit
            return False
        identifier, event_id, payload = row
        if hasattr(payload, 'read'):
            payload = payload.read()
        if type(payload) is not str or len(payload) > 4096:
            raise RuntimeError('Invalid stored event')
        event = json.loads(payload)
        if event.get('event_id') != event_id:
            raise RuntimeError('Invalid stored event identity')
        try:
            channel.basic_publish(exchange=EXCHANGE, routing_key=ROUTING_KEY, body=payload.encode(),
                properties=pika.BasicProperties(content_type='application/json', delivery_mode=2, message_id=event_id), mandatory=True)
        except Exception:
            cursor.execute('UPDATE HR_CORE.HRMS_OUTBOX SET ATTEMPTS=ATTEMPTS+1 WHERE ID=:id AND PUBLISHED_AT IS NULL', {'id': identifier})
            if cursor.rowcount != 1:
                raise RuntimeError('Outbox state conflict')
            connection.commit()  # exception bubbles out, worker stops: no retry of uncertain DB commit
            raise RuntimeError('Broker delivery unconfirmed; relay stopped; outbox retained') from None
        cursor.execute('UPDATE HR_CORE.HRMS_OUTBOX SET PUBLISHED_AT=SYSTIMESTAMP, ATTEMPTS=ATTEMPTS+1 '
                       'WHERE ID=:id AND PUBLISHED_AT IS NULL', {'id': identifier})
        if cursor.rowcount != 1:
            raise RuntimeError('Outbox state conflict')
        connection.commit()
        return True
    finally:
        cursor.close()


def main():
    database = broker = None
    try:
        broker = pika.BlockingConnection(broker_parameters())
        channel = broker.channel()
        topology(channel)
        channel.confirm_delivery()
        database = create_oracle_connection()
        while True:
            broker.process_data_events(time_limit=0)
            sent = relay_once(database, channel)
            if not sent:
                broker.process_data_events(time_limit=2)
    except BaseException:
        print('{"service":"outbox-relay","status":"stopped","manual_review_required":true}', flush=True)
        return 1
    finally:
        for resource in (database, broker):
            if resource is not None:
                if resource is broker and not broker.is_open:
                    continue
                try:
                    resource.close()
                except BaseException:
                    print('{"service":"outbox-relay","cleanup_failed":true}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
