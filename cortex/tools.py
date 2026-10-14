"""Actual registered integrations; model tool declarations confer no authority."""
import hashlib
import json
from pathlib import Path
import re


class ToolError(ValueError):
    def __init__(self, category, missing=()):
        super().__init__(category)
        self.category, self.missing = category, list(missing)


DEFINITIONS = {
    'write_document': {'capabilities': ['file_write'], 'fields': {'filename': 'string', 'content_from': 'string'},
        'completion': 'readback_size_sha256', 'recovery': 'verify_owned_intent_path; never overwrite'},
    'reminder_create': {'capabilities': ['reminders'], 'fields': {'message': 'string', 'at': 'string', 'timezone': 'string'},
        'optional': {'fold': 'integer'}, 'completion': 'persistent_unique_schedule',
        'recovery': 'unique_operation_id; delivery is independently verified'},
    'git_inspect': {'capabilities': ['analysis', 'github'], 'fields': {'repository': 'string'},
        'optional': {'files':'array'},
        'completion': 'actual_repository_inspection', 'recovery': 'repeat read-only inspection'},
    'git_commit_push': {'capabilities': ['analysis', 'github'],
        'fields': {'repository': 'string', 'branch': 'string', 'files': 'array', 'message': 'string'},
        'completion': 'exact_commit_observed_on_remote_branch',
        'recovery': 'inspect operation commit and remote before authorized repeated push'}
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


class ToolRegistry:
    def __init__(self, config):
        allowed = {'version','output_dir','notification_log','git_targets','max_operations','event_requirements'}
        if type(config) is not dict or set(config)-allowed or type(config.get('version')) is not int or config.get('version') != 1:
            raise ToolError('invalid_tool_configuration')
        for key in ('output_dir','notification_log'):
            if type(config.get(key)) is not str or not config[key].strip():
                raise ToolError('missing_tool_configuration', [key])
        self.config = dict(config)
        self.config['output_dir'] = str(Path(config['output_dir']).resolve())
        self.config['notification_log'] = str(Path(config['notification_log']).resolve())
        cap = config.get('max_operations', 16)
        if type(cap) is not int or not 0 <= cap <= 100:
            raise ToolError('invalid_tool_operation_limit')
        self.config['max_operations'] = cap
        targets = config.get('git_targets', {})
        if type(targets) is not dict or len(targets)>8:
            raise ToolError('invalid_git_targets')
        if targets:
            from .git_tools import validate_target
        self.config['git_targets'] = {name:validate_target(name,target) for name,target in targets.items()}
        requirements = config.get('event_requirements', [])
        if type(requirements) is not list or len(requirements)>8 or any(type(x) is not str or not x.strip() or len(x.encode())>1000 for x in requirements):
            raise ToolError('invalid_event_requirements')
        self.config['event_requirements'] = requirements

    def validate(self, name, arguments):
        if name not in DEFINITIONS or type(arguments) is not dict:
            raise ToolError('unregistered_tool')
        spec = DEFINITIONS[name]
        required, optional = spec['fields'], spec.get('optional', {})
        if set(arguments)-set(required)-set(optional) or set(required)-set(arguments):
            raise ToolError('invalid_tool_argument_fields')
        for key, kind in (required | optional).items():
            if key not in arguments:
                continue
            value = arguments[key]
            if (kind=='string' and (type(value) is not str or not value.strip() or len(value.encode())>2000)
                    or kind=='integer' and type(value) is not int
                    or kind=='array' and (type(value) is not list or not 1<=len(value)<=20 or any(type(x) is not str or not x for x in value))):
                raise ToolError('invalid_tool_argument_type')
        if name=='write_document':
            filename = arguments['filename']
            if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,62}\.(?:md|txt)',filename)
                    or filename.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}):
                raise ToolError('invalid_document_filename')
        if name=='reminder_create' and arguments.get('fold') not in (None,0,1):
            raise ToolError('invalid_time_fold')
        return dict(arguments)

    def select(self, name, caps, arguments):
        self.validate(name, arguments)
        required = set(DEFINITIONS[name]['capabilities'])
        if set(caps) != required:
            raise ToolError('tool_capability_mismatch')
        if name.startswith('git_') and arguments['repository'] not in self.config['git_targets']:
            raise ToolError('repository_not_authorized', ['explicit repository configuration'])
        return {'kind':'registered_tool','tool_id':name,'capabilities':sorted(required),
            'decision_source':'rule','reason':'Exact actual integration and typed arguments; no model declaration used.'}

    def catalog(self):
        return DEFINITIONS


def prepare_document(registry, args, predecessors):
    identifier = args['content_from']
    rows = [row for row in predecessors if row['task_id']==identifier]
    if len(rows)!=1 or not rows[0]['completion_evidence'].get('checks_passed'):
        raise ToolError('accepted_content_required')
    output = rows[0]['output']
    if type(output) is not dict or type(output.get('text')) is not str:
        raise ToolError('accepted_text_required')
    data = output['text'].encode('utf-8')
    if not data or len(data)>16000:
        raise ToolError('invalid_document_content')
    root = Path(registry.config['output_dir'])
    path = root / args['filename']
    if path.parent.resolve()!=root.resolve() or path.exists() or path.is_symlink():
        raise ToolError('document_exists_or_unsafe_path')
    return {'path':str(path),'root':str(root.resolve()),'content':output['text'],'size':len(data),'sha256':sha(data)}


def write_document(prepared, recovery=False):
    import os
    path = Path(prepared['path'])
    expected = prepared['content'].encode('utf-8')
    path.parent.mkdir(parents=True,exist_ok=True)
    if 'root' in prepared and path.parent.resolve()!=Path(prepared['root']):
        raise ToolError('document_output_directory_changed')
    if path.is_symlink():
        raise ToolError('unsafe_document_symlink')
    if path.exists():
        if not recovery:
            raise ToolError('document_exists')
    else:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os,'O_NOFOLLOW',0), 0o600)
        with os.fdopen(descriptor,'wb') as output:
            output.write(expected)
            output.flush()
            os.fsync(output.fileno())
    actual = path.read_bytes()
    if actual!=expected or sha(actual)!=prepared['sha256']:
        raise ToolError('document_readback_failed')
    return {'path':str(path.resolve()),'size':len(actual),'sha256':sha(actual),'readback_verified':True,
        'completion_scope':'file_content_exists; factual correctness and event compliance not verified'}
