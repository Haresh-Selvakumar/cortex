"""Foreground notification delivery; creation is an explicitly configured tool plan."""
import argparse
import json
from pathlib import Path
from .job_store import JobStore
from .reminders import inspect,cancel,run_scheduler


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('inspect','cancel','run'))
    parser.add_argument('reminder_id',nargs='?')
    parser.add_argument('--db',type=Path,default=Path('.cortex/jobs.sqlite3'))
    parser.add_argument('--notification-log',type=Path,default=Path('.cortex/notifications.jsonl'))
    parser.add_argument('--seconds',type=float,default=60)
    args=parser.parse_args(argv)
    if not args.db.exists(): parser.error('Database does not exist')
    store=JobStore(args.db)
    try:
        if args.action=='inspect': result=inspect(store,args.reminder_id)
        elif args.action=='cancel': result=cancel(store,args.reminder_id or '')
        else:
            if args.reminder_id: parser.error('Run processes due reminders in this database; no reminder ID')
            result=run_scheduler(store,str(args.notification_log.resolve()),args.seconds)
    except (ValueError,OSError):
        parser.error('Invalid reminder, timezone, state or scheduler lock')
    finally: store.close()
    print(json.dumps(result,indent=2))
    return 2 if args.action=='run' and any(row['state']=='delivery_failed' for row in result['outcomes']) else 0
