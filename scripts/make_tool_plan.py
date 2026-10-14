"""Make a fresh supplied document/reminder plan; no model or tool execution."""
import argparse
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cortex.tools import ToolRegistry
from cortex.reminders import prepare_reminder,zone


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind',choices=('document','reminder'))
    parser.add_argument('--filename')
    parser.add_argument('--message')
    parser.add_argument('--at',help='Full ISO date and 24-hour time')
    parser.add_argument('--after-seconds',type=int)
    parser.add_argument('--timezone',help='Explicit UTC, fixed offset, or installed IANA zone')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    registry=ToolRegistry(json.loads((ROOT/'config/tools.example.json').read_text()))
    try:
        if args.kind=='document':
            if not args.filename or args.at or args.after_seconds is not None or args.message or args.timezone:
                parser.error('Document requires only --filename and --output')
            plan=json.loads((ROOT/'examples/real_tools_v1/draft_and_write.json').read_text())
            plan['steps'][1]['tool']['arguments']['filename']=args.filename
            registry.validate('write_document',plan['steps'][1]['tool']['arguments'])
        else:
            if args.filename or not args.message or not args.timezone or bool(args.at)==(args.after_seconds is not None):
                parser.error('Reminder requires message, explicit timezone and exactly one of --at/--after-seconds')
            if args.after_seconds is not None:
                if not 1<=args.after_seconds<=86400:parser.error('Relative demonstration time must be 1–86400 seconds')
                args.at=(datetime.now(timezone.utc)+timedelta(seconds=args.after_seconds)).astimezone(zone(args.timezone)).isoformat()
            arguments={'message':args.message,'at':args.at,'timezone':args.timezone}
            registry.validate('reminder_create',arguments)
            prepare_reminder(arguments)
            plan=json.loads((ROOT/'examples/real_tools_v1/reminder.json').read_text())
            plan['steps'][0]['tool']['arguments']=arguments
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x',encoding='utf-8') as output:json.dump(plan,output,indent=2); output.write('\n')
    except (ValueError,OSError) as error:parser.error(str(error))
    print(json.dumps({'plan':str(args.output.resolve()),'execution_performed':False}))


if __name__=='__main__':main()
