"""Version 2 adds typed tool calls; version 1 remains the frozen contract."""
import copy
import json
from .planning import Plan, PlanError, PLAN_SCHEMA, STEP_SCHEMA
from .tools import ToolError, DEFINITIONS


def schema():
    result = copy.deepcopy(PLAN_SCHEMA)
    result['properties']['version']['enum'] = [2]
    step = result['properties']['steps']['items']
    step['properties']['execution_kind']['enum'].append('tool')
    step['properties']['expected_output']['enum'].append('tool_result')
    step['properties']['completion_checks']['items']['enum'].append('tool_verified')
    step['properties']['tool'] = {'anyOf':[{'type':'null'},{'type':'object','additionalProperties':False,
        'properties':{'name':{'type':'string','enum':list(DEFINITIONS)},'arguments':{'type':'object'}},'required':['name','arguments']}]}
    step['required'].append('tool')
    return result


def validate(payload, registry, classification=None, supplier_context=False):
    if registry is None or type(payload) is not dict or set(payload)!={'version','steps'} or payload['version']!=2 or type(payload['steps']) is not list:
        raise PlanError('tool_contract_requires_registry')
    projected = {'version':1,'steps':[]}
    for step in payload['steps']:
        if type(step) is not dict or set(step)!=set(STEP_SCHEMA['required'])|{'tool'}:
            raise PlanError('invalid_tool_step_fields')
        old = {key:value for key,value in step.items() if key!='tool'}
        if step['execution_kind']=='tool':
            tool = step['tool']
            if type(tool) is not dict or set(tool)!={'name','arguments'} or step['expected_output']!='tool_result' or step['completion_checks']!=['tool_verified']:
                raise PlanError('invalid_tool_execution_claim')
            try:
                registry.validate(tool['name'],tool['arguments'])
                if set(step['required_capabilities'])!=set(DEFINITIONS[tool['name']]['capabilities']):
                    raise ToolError('tool_capability_mismatch')
            except (ToolError,KeyError,TypeError):
                raise PlanError('invalid_registered_tool_arguments') from None
            if tool['name']=='write_document' and tool['arguments']['content_from'] not in step['depends_on']:
                raise PlanError('document_requires_accepted_predecessor')
            old.update(execution_kind='unsupported',expected_output='none',completion_checks=['unsupported'])
        elif step['tool'] is not None:
            raise PlanError('tool_claim_on_worker_step')
        projected['steps'].append(old)
    base = Plan.validate(projected,classification,supplier_context)
    canonical = json.dumps(payload,sort_keys=True,ensure_ascii=False)
    if len(canonical.encode())>24000:
        raise PlanError('plan_size_limit')
    return Plan(canonical,base.order)
