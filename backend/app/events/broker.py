import os
import pika

EXCHANGE = 'hrms.events'
QUEUE = 'hrms.employee_created'
ROUTING_KEY = 'employee.created'


def topology(channel):
    channel.exchange_declare(exchange=EXCHANGE, exchange_type='direct', durable=True)
    channel.exchange_declare(exchange='hrms.dead', exchange_type='direct', durable=True)
    channel.queue_declare(queue='hrms.dead.employee_created', durable=True)
    channel.queue_bind(queue='hrms.dead.employee_created', exchange='hrms.dead', routing_key=ROUTING_KEY)
    channel.queue_declare(queue=QUEUE, durable=True,
                         arguments={'x-dead-letter-exchange': 'hrms.dead', 'x-dead-letter-routing-key': ROUTING_KEY})
    channel.queue_bind(queue=QUEUE, exchange=EXCHANGE, routing_key=ROUTING_KEY)


def broker_parameters():
    return pika.ConnectionParameters(host=os.environ.get('RABBITMQ_HOST', 'localhost'),
        port=int(os.environ.get('RABBITMQ_PORT', '5672')),
        credentials=pika.PlainCredentials(os.environ['RABBITMQ_USER'], os.environ['RABBITMQ_PASSWORD']),
        heartbeat=30, blocked_connection_timeout=10, socket_timeout=10, connection_attempts=1)
