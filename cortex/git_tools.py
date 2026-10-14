"""Explicit repositories only; bounded staging and observed remote completion."""
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from urllib.parse import urlsplit
from .tools import ToolError,sha


def validate_target(name,target):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,31}',name) or type(target) is not dict or set(target)!={'path','branch','remote_url','allow_push'}:
        raise ToolError('invalid_repository_authority')
    if type(target['allow_push']) is not bool or any(type(target[x]) is not str or not target[x] for x in ('path','branch','remote_url')):
        raise ToolError('invalid_repository_authority')
    branch=target['branch']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]{0,100}',branch) or '..' in branch or '@{' in branch or branch.endswith(('/','.')):
        raise ToolError('invalid_explicit_branch')
    remote=target['remote_url']
    url=urlsplit(remote)
    if url.scheme=='https':
        if url.hostname!='github.com' or url.username or url.password or url.query or url.fragment or not re.fullmatch(r'/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?',url.path):
            raise ToolError('unsupported_or_credentialed_remote')
    elif not Path(remote).is_absolute() or not Path(remote).is_dir():
        raise ToolError('explicit_local_remote_required')
    return {**target,'path':str(Path(target['path']).resolve()),'remote_url':remote if url.scheme=='https' else str(Path(remote).resolve())}


def git(repo,*args,timeout=15,check=True):
    # Prevent hooks, signing prompts, pager, external diffs and interactive credentials.
    hooks=Path(repo)/'.git'/'cortex-empty-hooks'
    if not hooks.parent.is_dir(): raise ToolError('normal_git_checkout_required')
    hooks.mkdir(exist_ok=True)
    if any(hooks.iterdir()): raise ToolError('disabled_hook_directory_not_empty')
    env=dict(os.environ,GIT_TERMINAL_PROMPT='0',GIT_PAGER='cat')
    try:
        result=subprocess.run(['git','-c','core.hooksPath='+str(hooks),'-c','commit.gpgSign=false',
            '-c','credential.interactive=never',*args],cwd=repo,env=env,capture_output=True,text=True,
            encoding='utf-8',errors='replace',timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError('tool_outcome_unknown') from None
    except OSError:
        raise ToolError('git_unavailable') from None
    if check and result.returncode:
        raise ToolError('git_command_failed')
    return result


def head(repo):
    result=git(repo,'rev-parse','--verify','HEAD',check=False)
    return result.stdout.strip() if result.returncode==0 else None


def remote_head(repo,target):
    result=git(repo,'ls-remote','--refs',target['remote_url'],'refs/heads/'+target['branch'])
    lines=result.stdout.strip().splitlines()
    if not lines: return None
    if len(lines)!=1 or not re.fullmatch(r'[a-f0-9]{40,64}\trefs/heads/'+re.escape(target['branch']),lines[0]):
        raise ToolError('invalid_remote_observation')
    return lines[0].split('\t')[0]


def safe_files(repo,files):
    if len(set(files))!=len(files): raise ToolError('duplicate_intended_files')
    result=[]
    forbidden={'.git','.cortex','.codex','.aws','.ssh','runs','snapshots','__pycache__'}
    for name in files:
        path=PurePosixPath(name)
        if (path.is_absolute() or '\\' in name or ':' in name or path.as_posix()!=name or any(ord(x)<32 or ord(x)==127 for x in name)
                or any(x in ('..','.') or x.casefold() in forbidden for x in path.parts)
                or not path.parts or any(x.startswith('-') for x in path.parts)
                or any(x.lower().startswith('.env') or any(word in x.lower() for word in ('credential','secret','private_key')) for x in path.parts)
                or path.suffix.lower() in ('.sqlite3','.sqlite','.db','.pem','.key','.pyc')):
            raise ToolError('private_or_unsafe_file_excluded')
        actual=Path(repo).joinpath(*path.parts)
        if actual.is_symlink() or not actual.is_file() or not actual.resolve().is_relative_to(Path(repo).resolve()):
            raise ToolError('file_outside_repository_or_missing')
        data=actual.read_bytes()
        if len(data)>1000000 or b'PRIVATE KEY-----' in data or re.search(rb'(?im)(?:api[_-]?key|access[_-]?token|password)[\x22\x27]?\s*[:=]\s*[\x22\x27]?[^\s\x22\x27]{8,}',data):
            raise ToolError('credential_or_oversized_content_excluded')
        for key in ('OPENAI_API_KEY','ANTHROPIC_API_KEY','GH_TOKEN','GITHUB_TOKEN'):
            if os.environ.get(key) and os.environ[key].encode() in data: raise ToolError('credential_content_excluded')
        if git(repo,'check-ignore','--',name,check=False).returncode==0:
            raise ToolError('ignored_file_excluded')
        result.append({'path':name,'sha256':sha(data),'git_blob':git(repo,'hash-object','--path='+name,name).stdout.strip(),'size':len(data),
            'diff':git(repo,'diff','--no-ext-diff','--no-textconv','--',name).stdout[:12000],
            'new_content_preview':data[:2000].decode('utf-8',errors='replace') if git(repo,'ls-files','--error-unmatch','--',name,check=False).returncode else None})
    return result


def prepare_git(registry,name,args):
    target=registry.config['git_targets'].get(args['repository'])
    if not target: raise ToolError('repository_not_authorized',['explicit repository configuration'])
    repo=target['path']
    if git(repo,'rev-parse','--show-toplevel').stdout.strip().replace('\\','/').casefold()!=repo.replace('\\','/').casefold():
        raise ToolError('repository_root_mismatch')
    branch=git(repo,'symbolic-ref','--short','HEAD').stdout.strip()
    output={'repository':repo,'branch':branch,'head':head(repo),
        'status':git(repo,'status','--porcelain=v1').stdout,'remote_branch_head':remote_head(repo,target)}
    if name=='git_inspect':
        if args.get('files'):output['intended_changes']=safe_files(repo,args['files'])
        return output
    if not target['allow_push'] or args['branch']!=target['branch'] or branch!=target['branch']:
        raise ToolError('push_or_branch_not_authorized')
    git(repo,'check-ref-format','--branch',branch)
    if git(repo,'diff','--cached','--name-only').stdout.strip():
        raise ToolError('preexisting_staged_changes')
    if not 1<=len(args['message'].encode())<=500 or '\n' in args['message']:
        raise ToolError('invalid_commit_message')
    if git(repo,'status','--porcelain=v1','--',*args['files']).stdout.strip()=='':
        raise ToolError('no_intended_changes')
    output['files']=safe_files(repo,args['files'])
    return output


def execute_git(registry,name,operation,retry_unknown,persist):
    args=operation['arguments']; target=registry.config['git_targets'][args['repository']]
    if name=='git_inspect':
        return prepare_git(registry,name,args)
    from .tool_locks import file_lock
    repo=target['path']; prepared=operation['prepared']; identifier=operation['operation_id']
    with file_lock(Path(repo)/'.git'/'cortex-operation.lock'):
        current=head(repo)
        commit=operation.get('commit')
        trailer='Cortex-Operation: '+identifier
        if not commit and current!=prepared['head']:
            if trailer not in git(repo,'log','-1','--format=%B').stdout.splitlines():
                raise ToolError('repository_changed_after_intent')
            commit=current
        if commit:
            if head(repo)!=commit or trailer not in git(repo,'log','-1','--format=%B').stdout.splitlines():
                raise ToolError('operation_commit_not_current')
            parents=git(repo,'show','-s','--format=%P',commit).stdout.strip().split()
            if parents!=([prepared['head']] if prepared['head'] else []): raise ToolError('operation_commit_parent_mismatch')
            changed=set(git(repo,'diff-tree','--root','--no-commit-id','--name-only','-r',commit).stdout.splitlines())
            if changed!=set(args['files']): raise ToolError('operation_commit_files_mismatch')
        else:
            actual=safe_files(repo,args['files'])
            if [(x['path'],x['sha256']) for x in actual]!=[(x['path'],x['sha256']) for x in prepared['files']]:
                raise ToolError('intended_files_changed')
            staged=git(repo,'diff','--cached','--name-only').stdout.splitlines()
            if staged and set(staged)!=set(args['files']): raise ToolError('unrelated_staged_files')
            git(repo,'add','--',*args['files'])
            if set(git(repo,'diff','--cached','--name-only').stdout.splitlines())!=set(args['files']):
                raise ToolError('intended_stage_mismatch')
            git(repo,'commit','-m',args['message']+'\n\n'+trailer)
            commit=head(repo)
            persist(phase='commit_observed',commit=commit)
            parents=git(repo,'show','-s','--format=%P',commit).stdout.strip().split()
            changed=set(git(repo,'diff-tree','--root','--no-commit-id','--name-only','-r',commit).stdout.splitlines())
            if parents!=([prepared['head']] if prepared['head'] else []) or changed!=set(args['files']):
                raise ToolError('committed_changes_differ_from_intent')
        for item in prepared['files']:
            if git(repo,'rev-parse',commit+':'+item['path']).stdout.strip()!=item['git_blob']:
                raise ToolError('operation_commit_content_mismatch')
        observed=remote_head(repo,target)
        if observed==commit:
            return {'repository':repo,'branch':target['branch'],'commit':commit,'remote_commit':observed,
                'remote_verified':True,'push_reconciled':True,'intended_files':prepared['files']}
        if observed!=prepared['remote_branch_head']:
            raise ToolError('remote_changed_after_intent')
        attempts=operation.get('push_attempts',0)
        if attempts and not retry_unknown:
            raise ToolError('tool_outcome_unknown')
        if attempts>=2: raise ToolError('bounded_push_attempt_limit')
        persist(phase='before_push',commit=commit,push_attempts=attempts+1,remote_observed_before_repeat=observed)
        git(repo,'push','--no-force',target['remote_url'],commit+':refs/heads/'+target['branch'],timeout=30)
        observed=remote_head(repo,target)
        if observed!=commit: raise ToolError('remote_commit_not_verified')
        return {'repository':repo,'branch':target['branch'],'commit':commit,'remote_commit':observed,
            'remote_verified':True,'push_reconciled':False,'intended_files':prepared['files']}
