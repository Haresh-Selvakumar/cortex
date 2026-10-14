"""Small standard-library HTTP UI. Default is authenticated loopback owner access."""
import argparse
import contextlib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit, parse_qs

from .web_service import BrowserService, BrowserError

ASSETS=Path(__file__).parent/'web_assets'


def handler(service,origin):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass  # URLs contain job/session data; no raw request logging.

        def send(self,status,body,kind='application/json; charset=utf-8',cookie=None,filename=None):
            content=json.dumps(body).encode() if kind.startswith('application/json') else body
            self.send_response(status)
            self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(content)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if cookie:self.send_header('Set-Cookie','cortex_session='+cookie+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400'+('; Secure' if origin.startswith('https:') else ''))
            if filename:self.send_header('Content-Disposition','attachment; filename="'+filename+'"')
            self.end_headers();self.wfile.write(content)

        def identity(self):
            cookie=SimpleCookie();cookie.load(self.headers.get('Cookie',''))
            token=cookie.get('cortex_session')
            if not token:raise BrowserError('Session required')
            sid,csrf,fresh=service.session(token.value)
            if fresh:raise BrowserError('Session expired')
            return sid,csrf

        def read_body(self):
            length=self.headers.get('Content-Length','')
            if not length.isdigit() or not 1<=int(length)<=64000:raise BrowserError('JSON body must be 1–64000 bytes')
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise BrowserError('JSON content type required')
            value=json.loads(self.rfile.read(int(length)))
            if type(value) is not dict:raise BrowserError('JSON object required')
            return value

        def check_host(self):
            if self.headers.get('Host')!=urlsplit(origin).netloc:raise BrowserError('Unexpected host')

        def do_GET(self):
            try:
                self.check_host()
                url=urlsplit(self.path);path=url.path
                static={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/app.css':('app.css','text/css; charset=utf-8')}
                if path=='/healthz':
                    return self.send(200,{'status':'ready','access':'public' if service.public else 'owner',
                        'model_connectivity':'not_checked_by_health_endpoint'})
                if path in static:
                    name,kind=static[path];return self.send(200,(ASSETS/name).read_bytes(),kind)
                sid,_=self.identity()
                if path=='/api/meta':value=service.metadata()
                elif path=='/api/examples/supplier':
                    from importlib.resources import files
                    value=json.loads(files('cortex').joinpath('default_config','supplier_task.json').read_text(encoding='utf-8'))
                elif path=='/api/jobs':value=service.listing(sid)
                elif path=='/api/reminders':value=service.reminder_list(sid)
                elif path.startswith('/api/operations/'):value=service.operation_view(sid,path.split('/')[-1])
                elif path.startswith('/api/jobs/'):
                    parts=path.split('/')
                    if len(parts)==6 and parts[4]=='files':
                        name,content=service.download(sid,parts[3],parts[5]);return self.send(200,content,'application/octet-stream',filename=name)
                    if len(parts)!=4:raise BrowserError('Unknown route')
                    value=service.inspect(sid,parts[3])
                else:raise BrowserError('Unknown route')
                self.send(200,value)
            except (ValueError,OSError,KeyError,TypeError) as exc:
                self.send(400,{'error':str(exc) if isinstance(exc,BrowserError) or not service.public else 'Request rejected'})

        def do_POST(self):
            try:
                self.check_host()
                if self.headers.get('Origin')!=origin:raise BrowserError('Same-origin request required')
                path=urlsplit(self.path).path;body=self.read_body()
                if path=='/api/session':
                    if set(body)-{'owner'}:raise BrowserError('Invalid session request')
                    cookie=SimpleCookie();cookie.load(self.headers.get('Cookie',''))
                    token=cookie.get('cortex_session')
                    _,csrf,fresh=service.session(token.value if token else None,body.get('owner'))
                    return self.send(200,{'csrf':csrf,'public':service.public},cookie=fresh)
                sid,csrf=self.identity()
                if not secrets.compare_digest(self.headers.get('X-Cortex-CSRF',''),csrf):raise BrowserError('CSRF token required')
                if path=='/api/prepare':value=service.prepare(sid,body)
                elif path.startswith('/api/jobs/') and path.endswith('/resume'):
                    value=service.resume(sid,path.split('/')[3],body)
                elif path.startswith('/api/reminders/') and path.endswith('/cancel'):
                    if body:raise BrowserError('Cancel takes no arguments')
                    value=service.reminder_cancel(sid,path.split('/')[3])
                else:raise BrowserError('Unknown route')
                self.send(200,value)
            except (ValueError,OSError,KeyError,TypeError) as exc:
                self.send(400,{'error':str(exc) if isinstance(exc,BrowserError) or not service.public else 'Request rejected'})
    return Handler


def schedule(root,seconds):
    from .job_store import JobStore
    from .reminders import run_scheduler
    import sqlite3
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        with contextlib.closing(sqlite3.connect(Path(root)/'browser.sqlite')) as db:
            ids=[r[0] for r in db.execute('SELECT id FROM sessions')]
        for sid in ids:
            folder=Path(root)/'sessions'/sid
            if not (folder/'jobs.sqlite').exists():continue
            store=JobStore(folder/'jobs.sqlite')
            try:run_scheduler(store,folder/'notifications.jsonl',seconds=min(.1,max(.001,end-time.monotonic())),poll=.05)
            finally:store.close()
        time.sleep(.2)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['web','web-reminders'])
    parser.add_argument('--state-dir',type=Path,default=Path('.cortex/browser'))
    parser.add_argument('--config',type=Path)
    parser.add_argument('--tools-config',type=Path)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--public',action='store_true')
    parser.add_argument('--origin',help='Exact HTTPS reverse-proxy origin for public mode')
    parser.add_argument('--seconds',type=float,default=3600)
    args=parser.parse_args(argv)
    if args.command=='web-reminders':
        if not 0<args.seconds<=86400:parser.error('Scheduler seconds must be 1–86400')
        print('Foreground browser reminder scheduler; console notification channel.',flush=True)
        try:schedule(args.state_dir.resolve(),args.seconds)
        except KeyboardInterrupt:pass
        return 0
    if not args.public and args.host not in ('127.0.0.1','localhost','::1'):parser.error('Owner mode binds loopback only')
    if not 1<=args.port<=65535:parser.error('Invalid port')
    if args.public and (not args.origin or not args.origin.startswith('https://') or urlsplit(args.origin).path or urlsplit(args.origin).query or urlsplit(args.origin).fragment or urlsplit(args.origin).username):
        parser.error('Public mode requires an exact HTTPS --origin behind a TLS reverse proxy')
    from .web_config import load_config
    config=load_config(args.config,'cortex.public.offline.json' if args.public else 'cortex.example.json')
    tool_config=load_config(args.tools_config,'tools.example.json')
    service=BrowserService(args.state_dir,config,tool_config,args.public,check_models=args.public)
    origin=args.origin if args.public else 'http://'+args.host+':'+str(args.port)
    server=ThreadingHTTPServer((args.host,args.port),handler(service,origin))
    server.daemon_threads=True
    print('Cortex browser: '+origin+('/' if args.public else '/#owner='+service.bootstrap),flush=True)
    print('Interactive engine; scheduler is a separate foreground process. Public deployment needs an isolated account/container.',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
    return 0
