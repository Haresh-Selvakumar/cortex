"""Write-ahead tool intent and evidence, separate from model accounting."""
import json
from .tools import ToolError, prepare_document, write_document, sha


def execute_tool(registry, step, predecessors, session, event):
    call = step['tool']
    selected = registry.select(call['name'],step['required_capabilities'],call['arguments'])
    event['routing'].append(selected)
    event['tool_intent'] = call
    data = session.data
    operations = data.setdefault('tool_operations', [])
    operation_id = sha((data['job_id']+':'+step['id']).encode())[:32]
    fingerprint = sha(json.dumps(call,sort_keys=True).encode())
    existing = next((row for row in operations if row['operation_id']==operation_id),None)
    if existing and existing['intent_sha256']!=fingerprint:
        raise ToolError('incompatible_tool_intent')
    recovery = existing is not None
    if existing and existing['state']=='failed':
        raise ToolError(existing['error'])
    if not existing:
        if len(operations)>=registry.config['max_operations']:
            raise ToolError('tool_operation_budget_exhausted')
        session.set_scope('step:'+step['id']+':tool','tool',step['id'],True)
        if not session.budget.reserve(1):
            raise ToolError('tool_work_budget_exhausted')
        existing = {'operation_id':operation_id,'task_id':step['id'],'tool_id':call['name'],
            'arguments':call['arguments'],'intent_sha256':fingerprint,'state':'preparing','output':None,
            'model_calls':0,'synthetic_work_units':1,'reconciliations':0}
        operations.append(existing)
        session.save('tool_intent_reserved',operation_id=operation_id,tool_id=call['name'])
    if existing['state']=='completed':
        return existing['output']
    try:
        if 'prepared' not in existing:
            if call['name']=='write_document':
                prepared = prepare_document(registry,call['arguments'],predecessors)
            elif call['name']=='reminder_create':
                from .reminders import prepare_reminder
                prepared = prepare_reminder(call['arguments'])
            else:
                from .git_tools import prepare_git
                prepared = prepare_git(registry,call['name'],call['arguments'])
                if call['name']=='git_commit_push':
                    declarations={s['id']:s for s in data['plan']['steps']}
                    for predecessor in predecessors:
                        declared=declarations[predecessor['task_id']].get('tool')
                        if declared and declared['name']=='git_inspect' and declared['arguments'].get('files'):
                            reviewed=predecessor['output']
                            keys=('repository','branch','head','remote_branch_head')
                            if (any(prepared[k]!=reviewed[k] for k in keys) or
                                [(x['path'],x['sha256'],x['git_blob']) for x in prepared['files']]!=
                                [(x['path'],x['sha256'],x['git_blob']) for x in reviewed['intended_changes']]):
                                raise ToolError('reviewed_git_intent_changed')
            existing['prepared'] = prepared
        existing['state']='in_flight'
        session.step_saved(event,'running')
        session.save('before_tool_execution',operation_id=operation_id)
        if recovery:
            existing['reconciliations']+=1
            session.save('tool_reconciliation',operation_id=operation_id)
        if call['name']=='write_document':
            output = write_document(existing['prepared'],recovery)
            output['event_requirements'] = registry.config['event_requirements']
            output['unchecked'] = ['factual correctness','event compliance']
            output['missing_information'] = [] if registry.config['event_requirements'] else ['event requirements not supplied']
        elif call['name']=='reminder_create':
            from .reminders import create_reminder
            output = create_reminder(session.store,operation_id,existing['prepared'],data['job_id'])
        else:
            from .git_tools import execute_git
            def persist_phase(**fields):
                existing.update(fields)
                session.save('git_phase_saved',operation_id=operation_id,phase=fields.get('phase'))
            output = execute_git(registry,call['name'],existing,session.retry_interrupted,persist_phase)
    except KeyboardInterrupt:
        existing['state']='unknown'
        event.update(status='interrupted',reason='Tool execution interrupted; reconcile side effect before reuse.')
        session.step_saved(event,'interrupted')
        session.save('tool_interrupted_unknown',operation_id=operation_id)
        raise
    except ToolError as exc:
        existing.update(state='unknown' if exc.category=='tool_outcome_unknown' else 'failed',error=exc.category)
        session.save('after_tool_execution',operation_id=operation_id,outcome=existing['state'])
        raise
    except OSError:
        existing.update(state='unknown',error='tool_outcome_unknown')
        session.save('after_tool_execution',operation_id=operation_id,outcome='unknown')
        raise ToolError('tool_outcome_unknown') from None
    existing.update(state='completed',output=output)
    session.save('after_tool_execution',operation_id=operation_id,outcome='completed')
    return output
