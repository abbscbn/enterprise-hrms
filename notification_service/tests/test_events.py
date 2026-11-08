import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from notification_service.app import Inbox, validate_event, process_delivery, create_app

EVENT={'event_id':'12345678-1234-4234-8234-123456789abc','event_type':'EMPLOYEE_CREATED','version':1,'employee_id':1,'occurred_at':'2026-11-08T00:00:00+00:00'}


class EventTests(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory())
        self.inbox=Inbox(str(Path(self.temp)/'inbox.sqlite'))
        self.channel=Mock()
        self.method=SimpleNamespace(delivery_tag=7,redelivered=False)

    def test_valid_event_and_persisted_idempotency(self):
        event=validate_event(json.dumps(EVENT).encode())
        self.assertTrue(self.inbox.process(event))
        reopened=Inbox(self.inbox.path)
        self.assertFalse(reopened.process(event))
        self.assertEqual(reopened.count(),1)

    def test_conflicting_duplicate_is_rejected(self):
        self.inbox.process(EVENT)
        with self.assertRaises(ValueError):self.inbox.process({**EVENT,'employee_id':2})
        self.assertEqual(self.inbox.count(),1)

    def test_invalid_payloads_rejected(self):
        for body in (b'not json',b'{}',json.dumps({**EVENT,'employee_id':True}).encode(),json.dumps({**EVENT,'version':True}).encode(),json.dumps({**EVENT,'password':'secret'}).encode(),json.dumps({**EVENT,'occurred_at':'2026-11-08'}).encode(),b'x'*4097):
            with self.subTest(length=len(body)),self.assertRaises((ValueError,TypeError,KeyError)):validate_event(body)

    def test_ack_after_inbox_commit_and_duplicate_ack(self):
        for _ in range(2):process_delivery(self.channel,self.method,None,json.dumps(EVENT).encode(),self.inbox)
        self.assertEqual(self.channel.basic_ack.call_count,2)
        self.assertEqual(self.inbox.count(),1)
        self.channel.basic_nack.assert_not_called()

    def test_invalid_event_dead_letters_without_requeue(self):
        process_delivery(self.channel,self.method,None,b'{}',self.inbox)
        self.channel.basic_nack.assert_called_once_with(delivery_tag=7,requeue=False)
        self.channel.basic_ack.assert_not_called()

    def test_storage_error_requeues_once_then_dead_letters(self):
        with patch.object(self.inbox,'process',side_effect=RuntimeError()):
            process_delivery(self.channel,self.method,None,json.dumps(EVENT).encode(),self.inbox)
            self.channel.basic_nack.assert_called_with(delivery_tag=7,requeue=True)
            self.method.redelivered=True
            process_delivery(self.channel,self.method,None,json.dumps(EVENT).encode(),self.inbox)
            self.channel.basic_nack.assert_called_with(delivery_tag=7,requeue=False)
        self.channel.basic_ack.assert_not_called()

    def test_health_reflects_consumer_state(self):
        state={'consumer_connected':False};client=create_app(self.inbox,state).test_client()
        self.assertEqual(client.get('/health').status_code,503)
        state['consumer_connected']=True
        self.assertEqual(client.get('/health').status_code,200)
        self.assertEqual(client.get('/health').json['processed'],0)

if __name__=='__main__':unittest.main()
