from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from uuid import UUID
import pika
from flask import Flask, jsonify

from backend.app.events.broker import topology, broker_parameters, QUEUE


def validate_event(body):
    if len(body) > 4096:
        raise ValueError('Invalid event')
    event = json.loads(body)
    if type(event) is not dict or set(event) != {'event_id', 'event_type', 'version', 'employee_id', 'occurred_at'}:
        raise ValueError('Invalid event')
    if str(UUID(event['event_id'])) != event['event_id'] or event['event_type'] != 'EMPLOYEE_CREATED':
        raise ValueError('Invalid event')
    if type(event['version']) is not int or event['version'] != 1:
        raise ValueError('Invalid event')
    if type(event['employee_id']) is not int or not 0 < event['employee_id'] <= 9999999999999999999:
        raise ValueError('Invalid event')
    if type(event['occurred_at']) is not str or datetime.fromisoformat(event['occurred_at']).utcoffset() is None:
        raise ValueError('Invalid event')
    return event


class Inbox:
    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS inbox '
                '(event_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, employee_id TEXT NOT NULL, processed_at TEXT NOT NULL)')

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def process(self, event):
        fingerprint = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT fingerprint FROM inbox WHERE event_id=?', (event['event_id'],)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise ValueError('Conflicting duplicate')
                return False
            connection.execute('INSERT INTO inbox VALUES (?,?,?,?)',
                (event['event_id'], fingerprint, str(event['employee_id']), event['occurred_at']))
        return True

    def has(self, event_id):
        with self.connect() as connection:
            return connection.execute('SELECT 1 FROM inbox WHERE event_id=?', (event_id,)).fetchone() is not None

    def count(self):
        with self.connect() as connection:
            return connection.execute('SELECT COUNT(*) FROM inbox').fetchone()[0]


def process_delivery(channel, method, properties, body, inbox):
    try:
        event = validate_event(body)
        fresh = inbox.process(event)
    except (ValueError, TypeError, KeyError, UnicodeError):
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return
    except Exception:
        # First transient failure requeues once; redelivery failure goes to DLQ.
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=not method.redelivered)
        return
    print(json.dumps({'service': 'notifications', 'event_id': event['event_id'],
                      'employee_id': event['employee_id'], 'status': 'processed' if fresh else 'duplicate'}), flush=True)
    channel.basic_ack(delivery_tag=method.delivery_tag)


def create_app(inbox=None, state=None):
    app = Flask(__name__)
    state = state if state is not None else {'consumer_connected': False}
    @app.get('/health')
    def health():
        return jsonify(service='hrms-notifications', status='ok' if state['consumer_connected'] else 'degraded',
                       consumer_connected=state['consumer_connected'], processed=inbox.count() if inbox else 0), (200 if state['consumer_connected'] else 503)
    @app.get('/receipts/<event_id>')
    def receipt(event_id):
        try:
            if str(UUID(event_id)) != event_id:
                raise ValueError()
        except ValueError:
            return jsonify(error='invalid event id'), 400
        return jsonify(processed=inbox.has(event_id) if inbox else False)
    return app


def main():
    inbox = Inbox(os.environ.get('INBOX_PATH', '/var/lib/hrms-events/inbox.sqlite'))
    state = {'consumer_connected': False}
    def consume():
        while True:
            connection = None
            try:
                connection = pika.BlockingConnection(broker_parameters())
                channel = connection.channel()
                topology(channel)
                channel.basic_qos(prefetch_count=1)
                channel.basic_consume(queue=QUEUE, on_message_callback=lambda ch, method, props, body:
                                      process_delivery(ch, method, props, body, inbox))
                state['consumer_connected'] = True
                channel.start_consuming()
            except Exception:
                print('{"service":"notifications","status":"broker_disconnected"}', flush=True)
            finally:
                state['consumer_connected'] = False
                if connection is not None and connection.is_open:
                    connection.close()
            time.sleep(3)  # broker reconnect only; no Oracle connection exists in this service
    threading.Thread(target=consume, daemon=True).start()
    create_app(inbox, state).run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
