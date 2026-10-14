"""Dispatch new commands while preserving positional legacy supplier usage."""
import sys
from . import cli, product_cli


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ('web', 'web-reminders'):
        from .web import main as web_main
        return web_main(args)
    if args and args[0] == "request":
        return product_cli.main(args[1:])
    if args and args[0] == "job":
        from .job_cli import main as job_main
        return job_main(args[1:])
    if args and args[0] == 'reminder':
        from .reminder_cli import main as reminder_main
        return reminder_main(args[1:])
    return cli.main(args)
