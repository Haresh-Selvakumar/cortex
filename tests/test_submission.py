"""Submission defaults, public restrictions and restart checks. Inventory responses are mocked."""
from datetime import datetime,timedelta,timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cortex.web_config import load_config
from cortex.web_service import BrowserService,BrowserError
from cortex.web import schedule
from test_web import MODELS,TOOLS,wait


class SubmissionTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()

    def test_installed_defaults_outside_repository(self):
        current=Path.cwd()
        try:
            os.chdir(self.root)
            self.assertEqual(load_config(None,'tools.example.json')['git_targets'],{})
            self.assertTrue(all(not row['enabled'] for row in load_config(None,'cortex.public.offline.json')['models']))
            with self.assertRaises(FileNotFoundError):load_config('explicit-missing.json','cortex.example.json')
        finally:os.chdir(current)

    def test_public_offline_no_model_attempts_and_owner_access_denied(self):
        public=BrowserService(self.root,load_config(None,'cortex.public.offline.json'),TOOLS,True,True)
        with self.assertRaises(BrowserError):public.session(bootstrap=public.bootstrap)
        sid,_,_=public.session()
        with patch('urllib.request.urlopen') as http:
            caps=public.metadata()['capabilities']
            self.assertFalse(caps['document']['available']);self.assertTrue(caps['supplier']['available'])
            with self.assertRaises(BrowserError):public.prepare(sid,{'request':'Draft description','mode':'document','filename':'test.md'})
            self.assertEqual(http.call_count,0)

    def test_public_inventory_failure_marks_ai_unavailable_without_inference(self):
        public=BrowserService(self.root,MODELS,TOOLS,True,True)
        sid,_,_=public.session()
        with patch('urllib.request.urlopen',side_effect=TimeoutError) as http:
            self.assertFalse(public.metadata()['capabilities']['request']['available'])
            with self.assertRaises(BrowserError):public.prepare(sid,{'request':'Draft a function'})
            self.assertEqual(http.call_count,1) # Cached bounded inventory only, no inference/retry.

    def test_public_permitted_inventory_lists_models_not_competence(self):
        public=BrowserService(self.root,MODELS,TOOLS,True,True)
        response=io.BytesIO(json.dumps({'models':[{'name':'qwen2.5:3b-instruct'},{'name':'qwen2.5:7b-instruct'}]}).encode())
        with patch('urllib.request.urlopen',return_value=response):
            metadata=public.metadata()
        self.assertTrue(metadata['capabilities']['document']['available'])
        self.assertEqual(metadata['capabilities']['document']['reason'],'permitted_model_listed')
        self.assertEqual(metadata['repositories'],{})

    def test_pending_reminder_survives_cold_service_and_scheduler_restart(self):
        public=BrowserService(self.root,MODELS,TOOLS,True)
        sid,_,token=public.session()
        at=(datetime.now(timezone.utc)+timedelta(seconds=.8)).isoformat()
        job=wait(public,sid,public.prepare(sid,{'mode':'reminder','request':'Restart test','message':'Fresh restart evidence',
            'at':at,'timezone':'UTC','max_calls':0}))
        wait(public,sid,public.resume(sid,job,{}))
        with patch('sys.stdout',new_callable=io.StringIO):schedule(self.root,.1)
        restarted=BrowserService(self.root,MODELS,TOOLS,True)
        self.assertEqual(restarted.session(token)[0],sid)
        with patch('sys.stdout',new_callable=io.StringIO) as console:schedule(self.root,1.1)
        self.assertIn('CORTEX REMINDER: Fresh restart evidence',console.getvalue())
        self.assertEqual(restarted.reminder_list(sid)[0]['state'],'delivered')
        with patch('sys.stdout',new_callable=io.StringIO) as console:schedule(self.root,.1)
        self.assertEqual(console.getvalue(),'')
        self.assertEqual(restarted.reminder_list(sid)[0]['delivery_attempts'],1)


if __name__=='__main__':unittest.main()
