"""Persistent reminders with a foreground scheduler and actual local delivery."""
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .tools import ToolError


def tables(store):
    store.connection.execute('''CREATE TABLE IF NOT EXISTS reminders (
        id TEXT PRIMARY KEY, job_id TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL)''')
    store.connection.commit()


def zone(name):
    if name=='UTC':
        return timezone.utc
    if re.fullmatch(r'[+-]\d\d:\d\d',name):
        hours,minutes=map(int,name[1:].split(':'))
        if hours>14 or minutes>59 or hours==14 and minutes:
            raise ToolError('invalid_timezone')
        return timezone(timedelta(minutes=(hours*60+minutes)*(1 if name[0]=='+' else -1)))
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError,ValueError):
        raise ToolError('timezone_unavailable',['installed IANA timezone data or explicit UTC offset']) from None


def prepare_reminder(args, current=None):
    # A complete ISO date and 24-hour time are required; no guessing AM/PM/date.
    if not re.fullmatch(r'\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:\d\d)?',args['at']):
        raise ToolError('ambiguous_reminder_time',['full date and explicit 24-hour time (resolve AM/PM)'])
    try:
        parsed=datetime.fromisoformat(args['at'].replace('Z','+00:00'))
    except ValueError:
        raise ToolError('invalid_reminder_time') from None
    tz=zone(args['timezone'])
    if parsed.tzinfo is None:
        possibilities=[]
        for fold in (0,1):
            candidate=parsed.replace(tzinfo=tz,fold=fold)
            if candidate.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)==parsed:
                possibilities.append(candidate)
        if not possibilities:
            raise ToolError('nonexistent_local_time')
        if len({value.utcoffset() for value in possibilities})>1 and 'fold' not in args:
            raise ToolError('ambiguous_dst_time',['fold 0 or 1 for repeated local time'])
        parsed=parsed.replace(tzinfo=tz,fold=args.get('fold',0))
    elif parsed.utcoffset()!=parsed.astimezone(tz).utcoffset():
        raise ToolError('timezone_offset_conflict')
    trigger=parsed.astimezone(timezone.utc)
    if trigger<=(current or datetime.now(timezone.utc)):
        raise ToolError('reminder_time_not_future',['future trigger time'])
    return {'message':args['message'],'timezone':args['timezone'],'trigger_utc':trigger.isoformat(),
            'resolved_local':trigger.astimezone(tz).isoformat(),'delivery_attempts':0}


def create_reminder(store, identifier, prepared, job_id='standalone'):
    tables(store)
    row=store.connection.execute('SELECT payload,state FROM reminders WHERE id=?',(identifier,)).fetchone()
    if row:
        stored=json.loads(row[0])
        if any(stored[key]!=prepared[key] for key in ('message','timezone','trigger_utc')):
            raise ToolError('incompatible_reminder_intent')
        return {**stored,'state':row[1],'creation_reused':True}
    payload={**prepared,'reminder_id':identifier,'created_at_utc':datetime.now(timezone.utc).isoformat(),
        'delivery_path':'foreground_scheduler_console_and_jsonl_receipt','notification_delivered':False}
    with store.connection:
        store.connection.execute('INSERT INTO reminders VALUES(?,?,?,?)',(identifier,job_id,json.dumps(payload),'scheduled'))
    return {**payload,'state':'scheduled','creation_reused':False}


def inspect(store, identifier=None):
    tables(store)
    if identifier is not None and not re.fullmatch(r'[a-f0-9]{32}',identifier):
        raise ToolError('invalid_reminder_id')
    rows=store.connection.execute('SELECT payload,state FROM reminders'+(' WHERE id=?' if identifier else ''),
                                  (identifier,) if identifier else ()).fetchall()
    if identifier and not rows:
        raise ToolError('reminder_not_found')
    return [{**json.loads(payload),'state':state} for payload,state in rows]


def cancel(store,identifier):
    value=inspect(store,identifier)[0]
    if value['state']=='scheduled':
        with store.connection:
            store.connection.execute("UPDATE reminders SET state='cancelled' WHERE id=? AND state='scheduled'",(identifier,))
    elif value['state']!='cancelled':
        raise ToolError('reminder_already_claimed_or_finished')
    return inspect(store,identifier)[0]


def deliver(value,log_path):
    """Delivery means an actual flushed local console notification plus durable receipt."""
    import os
    receipt={'reminder_id':value['reminder_id'],'message':value['message'],
        'delivered_at_utc':datetime.now(timezone.utc).isoformat(),'channel':'foreground_console'}
    # A scheduler process and attached/captured stdout must exist for this channel.
    print('CORTEX REMINDER: '+value['message']+' ['+value['reminder_id']+']',flush=True)
    path=Path(log_path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a',encoding='utf-8') as output:
        output.write(json.dumps(receipt)+'\n')
        output.flush()
        os.fsync(output.fileno())
    return receipt


def run_scheduler(store,log_path,seconds=60,poll=0.2,delivery=None):
    if type(seconds) not in (int,float) or not 0<seconds<=86400 or not 0<poll<=5:
        raise ToolError('invalid_scheduler_duration')
    from .tool_locks import file_lock
    tables(store)
    outcomes=[]
    with file_lock(Path(str(store.path)+'.scheduler.lock')):
        # Previous delivery died after claiming it. Do not resend an unknown notification.
        with store.connection:
            for payload, in store.connection.execute("SELECT payload FROM reminders WHERE state='delivering'").fetchall():
                value=json.loads(payload)
                value.update(delivery_outcome='unknown',error='scheduler_interrupted_in_delivery')
                store.connection.execute("UPDATE reminders SET state='delivery_failed',payload=? WHERE id=?",(json.dumps(value),value['reminder_id']))
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            current=datetime.now(timezone.utc)
            for value in inspect(store):
                if value['state']!='scheduled' or datetime.fromisoformat(value['trigger_utc'])>current:
                    continue
                value.pop('state')
                value['delivery_attempts']+=1
                with store.connection:
                    claimed=store.connection.execute("UPDATE reminders SET state='delivering',payload=? WHERE id=? AND state='scheduled'",
                        (json.dumps(value),value['reminder_id'])).rowcount
                if not claimed:
                    continue
                try:
                    receipt=(delivery or deliver)(value,log_path)
                    value.update(notification_delivered=True,receipt=receipt)
                    state='delivered'
                except (OSError,ValueError):
                    state='delivery_failed'
                    value.update(notification_delivered=False,error='local_notification_delivery_failed',delivery_outcome='unknown_or_failed')
                with store.connection:
                    store.connection.execute('UPDATE reminders SET state=?,payload=? WHERE id=?',(state,json.dumps(value),value['reminder_id']))
                outcomes.append({'reminder_id':value['reminder_id'],'state':state})
            time.sleep(min(poll,max(0,end-time.monotonic())))
    return {'outcomes':outcomes,'channel':'foreground_console','process_must_remain_running':True,
            'phone_alarm_integration':False}
