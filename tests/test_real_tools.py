"""Actual local file/SQLite/console/Git tests; model HTTP is mocked here."""
import contextlib
from datetime import datetime,timedelta,timezone
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cortex.job_store import JobStore
from cortex.jobs import new_job,resume_job
from cortex.tools import ToolRegistry,ToolError,prepare_document,write_document
from cortex.planning import Plan,PlanError
from cortex.reminders import prepare_reminder,create_reminder,inspect,cancel,run_scheduler
from cortex.git_tools import git,remote_head

ROOT=Path(__file__).resolve().parents[1]
HTTP='urllib.request.urlopen'


def reply(text):
    return io.BytesIO(json.dumps({'model':'mock','done':True,'message':{'content':text if isinstance(text,str) else json.dumps(text)},
                                 'prompt_eval_count':10,'eval_count':5}).encode())


def step(identifier,kind='text_draft',deps=(),tool=None):
    name=tool['name'] if tool else None
    caps={'write_document':['file_write'],'reminder_create':['reminders'],'git_inspect':['analysis','github'],
          'git_commit_push':['analysis','github']}.get(name,['document_draft'])
    return {'id':identifier,'task_type':'scheduling' if name=='reminder_create' else 'analysis' if name and name.startswith('git_') else 'documents',
        'goal':'Perform only this declared action.','execution_kind':kind,
        'required_inputs':['user_goal']+(['predecessor_outputs'] if deps else []),'missing_essential_information':[],
        'depends_on':list(deps),'required_capabilities':caps,
        'expected_output':'tool_result' if kind=='tool' else 'text_draft',
        'completion_checks':['tool_verified' if kind=='tool' else 'text_present'],'tool':tool}


def document_plan():
    return {'version':2,'steps':[step('draft'),step('save','tool',['draft'],
        {'name':'write_document','arguments':{'filename':'description.md','content_from':'draft'}})]}


class RealToolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.folder=Path(self.temp.name)
        self.store=JobStore(self.folder/'jobs.sqlite3')
        self.config={'version':1,'output_dir':str(self.folder/'output'),'notification_log':str(self.folder/'notifications.jsonl'),
                     'git_targets':{},'max_operations':8}
        self.models=json.loads((ROOT/'config/cortex.example.json').read_text())

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def job(self,plan=None,**kwargs):
        return new_job(self.store,'Draft and save a project description',self.models,plan=plan or document_plan(),
                       options={'tool_config':self.config,'request_contract':'isolated'},**kwargs)

    def test_actual_file_creation_readback_and_separate_accounting(self):
        with patch(HTTP,return_value=reply('Accepted project description')):
            result=self.job()
        self.assertEqual(result['status'],'drafted')
        output=result['step_results'][1]['output']
        self.assertEqual(Path(output['path']).read_text(),'Accepted project description')
        self.assertTrue(output['readback_verified'])
        self.assertEqual(result['tool_accounting']['operations_used'],1)
        self.assertEqual(result['budget']['model_calls_used'],1)
        self.assertEqual(result['budget']['work_units_used'],2)
        self.assertIn('event compliance',output['unchecked'])
        saved=self.store.read(result['job_id'])
        self.assertEqual(saved['tool_operations'][0]['state'],'completed')

    def test_traversal_reserved_names_and_other_extensions_rejected(self):
        registry=ToolRegistry(self.config)
        for name in ('../x.md','C:\\x.md','sub/x.md','CON.txt','nul.md','x.exe','.env.txt','x.md:extra'):
            with self.subTest(name=name),self.assertRaises(ToolError):
                registry.validate('write_document',{'filename':name,'content_from':'draft'})

    def test_existing_file_is_not_overwritten_even_if_identical(self):
        directory=Path(self.config['output_dir']); directory.mkdir()
        (directory/'description.md').write_text('Accepted')
        with patch(HTTP,return_value=reply('Accepted')):
            result=self.job()
        self.assertEqual(result['step_results'][1]['status'],'tool_failed')
        self.assertEqual((directory/'description.md').read_text(),'Accepted')

    def test_readback_detects_corruption(self):
        prepared={'path':str(self.folder/'x.txt'),'content':'expected','sha256':'incorrect','size':8}
        with self.assertRaises(ToolError): write_document(prepared)

    def test_rejected_draft_blocks_file_creation(self):
        from cortex.draft_checks import DraftPolicy
        with patch(HTTP,return_value=reply('$1')):
            result=self.job(policy=DraftPolicy.validate(max_repairs=0))
        self.assertEqual(result['step_results'][1]['status'],'blocked_dependency')
        self.assertFalse(Path(self.config['output_dir']).exists())
        self.assertEqual(result['tool_accounting']['operations_used'],0)

    def test_completed_file_after_interrupt_is_reconciled_without_regeneration(self):
        from cortex.tool_execution import write_document as real_write
        def interrupted(*args,**kwargs):
            real_write(*args,**kwargs)
            raise KeyboardInterrupt
        with patch(HTTP,return_value=reply('Accepted')),patch('cortex.tool_execution.write_document',side_effect=interrupted):
            before=self.job(max_model_calls=1)
        self.assertEqual(before['status'],'interrupted')
        with patch(HTTP) as http:
            after=resume_job(self.store,before['job_id'])
        http.assert_not_called()
        self.assertEqual(after['status'],'drafted')
        self.assertEqual(after['budget']['work_units_used'],2)
        self.assertEqual(after['tool_operations'][0]['reconciliations'],1)

    def test_partial_interrupted_file_is_not_overwritten(self):
        def interrupted(prepared,*args):
            path=Path(prepared['path']); path.parent.mkdir()
            path.write_text('partial'); raise KeyboardInterrupt
        with patch(HTTP,return_value=reply('Accepted')),patch('cortex.tool_execution.write_document',side_effect=interrupted):
            before=self.job()
        after=resume_job(self.store,before['job_id'])
        self.assertEqual(after['step_results'][1]['status'],'tool_failed')
        self.assertEqual((Path(self.config['output_dir'])/'description.md').read_text(),'partial')

    def test_tool_budget_and_work_budget_block_side_effect(self):
        for option in ('max_operations','work'):
            if option=='max_operations':self.config['max_operations']=0
            with patch(HTTP,return_value=reply('Accepted')):
                result=self.job(max_units=1 if option=='work' else 20)
            self.assertEqual(result['status'],'budget_exhausted')
            self.assertFalse(Path(self.config['output_dir']).exists())
            self.config['max_operations']=8

    def test_tool_contract_does_not_grant_unregistered_capability(self):
        with self.assertRaises(PlanError):Plan.validate(document_plan())
        invalid=document_plan(); invalid['steps'][1]['tool']['name']='phone_alarm'
        with self.assertRaises(PlanError):Plan.validate(invalid,tool_registry=ToolRegistry(self.config))
        invalid=document_plan(); invalid['steps'][1]['depends_on']=[]; invalid['steps'][1]['required_inputs']=['user_goal']
        with self.assertRaises(PlanError):Plan.validate(invalid,tool_registry=ToolRegistry(self.config))

    def test_missing_am_pm_is_visible_and_no_schedule_created(self):
        plan={'version':2,'steps':[step('notify','tool',tool={'name':'reminder_create','arguments':
            {'message':'Review','at':'7:00','timezone':'UTC'}})]}
        result=self.job(plan)
        self.assertEqual(result['status'],'needs_clarification')
        self.assertIn('AM/PM',result['step_results'][0]['missing_essential_information'][0])
        self.assertEqual(inspect(self.store),[])

    def test_timezone_resolution_and_offset_conflict(self):
        args={'message':'Review','at':'2099-01-01T12:00:00','timezone':'+05:30'}
        value=prepare_reminder(args)
        self.assertTrue(value['trigger_utc'].endswith('06:30:00+00:00'))
        with self.assertRaises(ToolError):prepare_reminder({**args,'at':'2099-01-01T12:00:00Z'})

    def test_real_console_delivery_receipt_and_no_duplicate(self):
        value=prepare_reminder({'message':'Actual local test notification','at':(datetime.now(timezone.utc)+timedelta(seconds=.15)).isoformat(),'timezone':'UTC'})
        identifier='a'*32
        create_reminder(self.store,identifier,value)
        duplicate=create_reminder(self.store,identifier,value)
        self.assertTrue(duplicate['creation_reused'])
        stdout=io.StringIO()
        with contextlib.redirect_stdout(stdout):run_scheduler(self.store,self.config['notification_log'],seconds=.4,poll=.03)
        self.assertIn('CORTEX REMINDER',stdout.getvalue())
        self.assertEqual(inspect(self.store,identifier)[0]['state'],'delivered')
        self.assertEqual(len(Path(self.config['notification_log']).read_text().splitlines()),1)
        run_scheduler(self.store,self.config['notification_log'],seconds=.01,poll=.01)
        self.assertEqual(len(Path(self.config['notification_log']).read_text().splitlines()),1)

    def test_delivery_failure_is_not_reported_as_delivered(self):
        value=prepare_reminder({'message':'Fail delivery','at':'2099-01-01T00:00:00','timezone':'UTC'})
        value['trigger_utc']='2000-01-01T00:00:00+00:00'
        create_reminder(self.store,'b'*32,value)
        with patch('cortex.reminders.deliver',side_effect=OSError):run_scheduler(self.store,self.config['notification_log'],seconds=.01,poll=.01)
        row=inspect(self.store,'b'*32)[0]
        self.assertEqual(row['state'],'delivery_failed'); self.assertFalse(row['notification_delivered'])

    def test_cancel_prevents_delivery(self):
        value=prepare_reminder({'message':'Cancel','at':'2099-01-01T00:00:00','timezone':'UTC'})
        create_reminder(self.store,'c'*32,value)
        self.assertEqual(cancel(self.store,'c'*32)['state'],'cancelled')
        run_scheduler(self.store,self.config['notification_log'],seconds=.01,poll=.01)
        self.assertFalse(Path(self.config['notification_log']).exists())

    def test_interrupted_delivery_is_unknown_and_not_resent(self):
        value=prepare_reminder({'message':'Unknown','at':'2099-01-01T00:00:00','timezone':'UTC'})
        create_reminder(self.store,'d'*32,value)
        with self.store.connection:self.store.connection.execute("UPDATE reminders SET state='delivering'")
        with patch('cortex.reminders.deliver') as delivery:run_scheduler(self.store,self.config['notification_log'],seconds=.01,poll=.01)
        delivery.assert_not_called()
        self.assertEqual(inspect(self.store,'d'*32)[0]['delivery_outcome'],'unknown')

    def test_interrupted_creation_reuses_stable_reminder_id(self):
        from cortex.reminders import create_reminder as actual
        plan={'version':2,'steps':[step('notify','tool',tool={'name':'reminder_create','arguments':
            {'message':'Review','at':'2099-01-01T00:00:00','timezone':'UTC'}})]}
        def interrupted(*args,**kwargs):actual(*args,**kwargs); raise KeyboardInterrupt
        with patch('cortex.reminders.create_reminder',side_effect=interrupted):before=self.job(plan)
        identifier=inspect(self.store)[0]['reminder_id']
        after=resume_job(self.store,before['job_id'])
        self.assertEqual(after['status'],'completed')
        self.assertEqual(len(inspect(self.store)),1)
        self.assertEqual(after['step_results'][0]['output']['reminder_id'],identifier)

    def repository(self):
        repo=self.folder/'repo'; remote=self.folder/'remote.git'
        for args in (['init','--initial-branch','demo',str(repo)],['init','--bare',str(remote)]):
            result=subprocess.run(['git',*args],capture_output=True); self.assertEqual(result.returncode,0)
        for key,value in [('user.name','Cortex Test'),('user.email','cortex-test@example.invalid')]:git(repo,'config',key,value)
        (repo/'description.md').write_text('Intended content\n')
        (repo/'notes.txt').write_text('Unrelated notes\n')
        self.config['git_targets']={'demo':{'path':str(repo),'branch':'demo','remote_url':str(remote),'allow_push':True}}
        plan={'version':2,'steps':[step('push','tool',tool={'name':'git_commit_push','arguments':
            {'repository':'demo','branch':'demo','files':['description.md'],'message':'Add description'}})]}
        return repo,remote,plan

    def test_actual_disposable_remote_push_and_exact_files(self):
        repo,remote,plan=self.repository()
        result=self.job(plan)
        self.assertEqual(result['status'],'completed')
        output=result['step_results'][0]['output']
        self.assertTrue(output['remote_verified'])
        self.assertEqual(output['commit'],remote_head(str(repo),self.config['git_targets']['demo']))
        self.assertEqual(git(repo,'ls-tree','--name-only','HEAD').stdout.strip(),'description.md')
        self.assertIn('?? notes.txt',git(repo,'status','--porcelain').stdout)

    def test_inspection_shows_intended_changes_before_any_commit(self):
        repo,remote,plan=self.repository()
        plan['steps'][0]['tool']={'name':'git_inspect','arguments':{'repository':'demo','files':['description.md']}}
        result=self.job(plan)
        self.assertEqual(result['status'],'completed')
        self.assertIn('Intended content',result['step_results'][0]['output']['intended_changes'][0]['new_content_preview'])
        self.assertIsNone(remote_head(str(repo),self.config['git_targets']['demo']))
        self.assertEqual(git(repo,'diff','--cached','--name-only').stdout.strip(),'')

    def test_credentials_and_generated_private_data_excluded(self):
        repo,remote,plan=self.repository()
        for name,content in [('.env','key=secret'),('.CORTEX/private.txt','private'),('jobs.sqlite3','private'),('description.md','{"api_key":"SECRET123456789"}')]:
            (repo/name).parent.mkdir(parents=True,exist_ok=True)
            (repo/name).write_text(content)
            plan['steps'][0]['tool']['arguments']['files']=[name]
            result=self.job(plan)
            self.assertEqual(result['status'],'plan_failed')
            self.assertIsNone(remote_head(str(repo),self.config['git_targets']['demo']))

    def test_preexisting_staged_changes_and_unauthorized_branch_blocked(self):
        repo,remote,plan=self.repository()
        git(repo,'add','--','notes.txt')
        result=self.job(plan); self.assertEqual(result['status'],'plan_failed')
        self.assertEqual(git(repo,'diff','--cached','--name-only').stdout.strip(),'notes.txt')
        self.config['git_targets']['demo']['allow_push']=False
        result=self.job(plan); self.assertEqual(result['status'],'plan_failed')

    def test_ambiguous_push_reconciles_remote_without_repeating_push(self):
        repo,remote,plan=self.repository()
        from cortex.git_tools import git as actual
        pushes=[]
        def ambiguous(repository,*args,**kwargs):
            result=actual(repository,*args,**kwargs)
            if args[0]=='push':pushes.append(args); raise ToolError('tool_outcome_unknown')
            return result
        with patch('cortex.git_tools.git',side_effect=ambiguous):before=self.job(plan)
        self.assertEqual(before['status'],'interrupted')
        with patch('cortex.git_tools.git',side_effect=ambiguous):after=resume_job(self.store,before['job_id'])
        self.assertEqual(after['status'],'completed'); self.assertEqual(len(pushes),1)
        self.assertTrue(after['step_results'][0]['output']['push_reconciled'])

    def test_remote_verification_mismatch_prevents_completion(self):
        repo,remote,plan=self.repository()
        with patch('cortex.git_tools.remote_head',return_value=None):result=self.job(plan)
        self.assertEqual(result['status'],'plan_failed')
        self.assertIsNone(result['step_results'][0]['output'])

    def test_isolated_full_model_path_and_preserved_missing_requirements(self):
        classification={'task_type':'coding','required_capabilities':['code_generation'],'missing_essential_information':[],
                        'needs_decomposition':False,'uncertain':False}
        proposal={'version':1,'steps':[{k:v for k,v in step('code').items() if k!='tool'}]}
        proposal['steps'][0].update(task_type='coding',required_capabilities=['code_generation'])
        with patch(HTTP,side_effect=[reply(classification),reply(proposal),reply('def add(a,b): return a+b')]) as http:
            result=new_job(self.store,'Draft add(a,b)',self.models,options={'request_contract':'isolated','classifier_mode':'ai','planning':'always'})
        self.assertEqual(result['status'],'drafted'); self.assertEqual(http.call_count,3)
        first=json.loads(http.call_args_list[0].args[0].data)
        self.assertNotIn('deadline_days',first['messages'][0]['content'])
        proposal['steps'][0]['required_inputs'].append('constraints')
        with patch(HTTP,side_effect=[reply(classification),reply(proposal)]) as http:
            result=new_job(self.store,'Draft add(a,b)',self.models,options={'request_contract':'isolated','classifier_mode':'ai','planning':'always'})
        self.assertEqual(result['status'],'needs_clarification'); self.assertEqual(http.call_count,2)

    def test_actual_scheduler_lock_excludes_another_owner(self):
        from cortex.tool_locks import file_lock
        with file_lock(Path(str(self.store.path)+'.scheduler.lock')):
            with self.assertRaises(ToolError):run_scheduler(self.store,self.config['notification_log'],seconds=.01)

    def test_tool_operation_budget_cannot_reset_on_resume(self):
        from cortex.job_store import JobError
        proposal=document_plan()
        proposal['steps'].append(step('notify','tool',tool={'name':'reminder_create','arguments':
            {'message':'Review','at':'2099-01-01T00:00:00','timezone':'UTC'}}))
        with patch(HTTP,return_value=reply('Accepted')):before=self.job(proposal,pause_after=2)
        data=self.store.read(before['job_id']); data['tool_operations']=[]
        self.store.save(data,{'event':'test_counter_reset'})
        with self.assertRaises(JobError):resume_job(self.store,before['job_id'])

    def test_model_planner_can_select_actual_registered_document_tool(self):
        classification={'task_type':'documents','required_capabilities':['document_draft','file_write'],
            'missing_essential_information':[],'needs_decomposition':True,'uncertain':False}
        with patch(HTTP,side_effect=[reply(classification),reply(document_plan()),reply('Accepted')]) as http:
            result=new_job(self.store,'Draft a description and save description.md',self.models,
                options={'tool_config':self.config,'request_contract':'isolated','classifier_mode':'ai','planning':'always'})
        self.assertEqual(result['status'],'drafted'); self.assertEqual(http.call_count,3)
        self.assertEqual(result['tool_accounting']['operations_used'],1)
        self.assertEqual(result['budget']['work_units_used'],4)


if __name__=='__main__':unittest.main()
