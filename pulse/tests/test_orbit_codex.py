import json
import subprocess
from types import SimpleNamespace

import pytest

from orbit.providers import FIELDS, ProviderError, Router


def test_codex_enabled_routes_auto_and_explicit_without_api_key(monkeypatch):
    monkeypatch.setenv('ORBIT_CODEX_ENABLED', '1')
    monkeypatch.setenv('ORBIT_CODEX_MODEL', 'verified-model')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('ORBIT_OPENAI_API_KEY', raising=False)
    router = Router({'BEDROCK_MODEL_ID': 'aws'})
    assert router.choose({'model': 'auto', 'goal': 'check my project'}) == ('codex', 'verified-model')
    assert router.choose({'model': 'codex', 'goal': 'fix tests'}) == ('codex', 'verified-model')
    assert router.choose({'model': 'aws', 'goal': 'inspect'}) == ('aws', 'aws')


def test_native_decision_uses_schema_and_no_execution_tools(tmp_path, monkeypatch):
    from orbit.codex_provider import decide
    monkeypatch.setenv('ORBIT_CODEX_HOME', str(tmp_path))
    monkeypatch.setenv('ORBIT_DISCORD_BOT_TOKEN', 'must-not-reach-child')
    calls = []
    decision = {field: '' for field in FIELDS}
    decision.update(action='runtime_health', arguments='{}')
    def run(args, **kwargs):
        calls.append((args, kwargs))
        schema = json.loads(open(args[args.index('--output-schema') + 1]).read())
        assert schema['additionalProperties'] is False
        warning = {'type':'item.completed','item':{'type':'error','message':'Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; enable `features.code_mode_host` and install `codex-code-mode-host`.'}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(warning)+'\n'+json.dumps({'type':'item.completed', 'item':{'type':'agent_message','text':json.dumps(decision)}})+'\n'+json.dumps({'type':'turn.completed'}), stderr='')
    monkeypatch.setattr(subprocess, 'run', run)
    result, metadata = decide({'task': {'goal':'inspect'}}, 'verified-model', 'instructions')
    assert result == decision and metadata['provider'] == 'codex'
    args, kwargs = calls[0]
    assert '--ignore-user-config' in args and '--strict-config' in args
    assert 'features.shell_tool=false' in args
    assert 'features.unified_exec=false' in args
    assert args[args.index('--sandbox') + 1] == 'read-only'
    assert 'ORBIT_DISCORD_BOT_TOKEN' not in kwargs['env']
    assert not list(tmp_path.glob('orbit-decision-*'))


def test_native_failure_never_exposes_process_output(monkeypatch, tmp_path):
    from orbit.codex_provider import decide
    monkeypatch.setenv('ORBIT_CODEX_HOME', str(tmp_path))
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1,stdout='',stderr='sensitive upstream error'))
    with pytest.raises(ProviderError, match='codex_request_failed') as exc:
        decide({}, 'model', 'instructions')
    assert 'sensitive' not in str(exc.value)


def test_codex_failure_falls_back_and_keeps_context(monkeypatch):
    monkeypatch.setenv('ORBIT_CODEX_ENABLED', '1')
    monkeypatch.setenv('ORBIT_CODEX_MODEL', 'verified-model')
    monkeypatch.setattr('orbit.codex_provider.decide', lambda *a: (_ for _ in ()).throw(ProviderError('codex_request_failed')))
    observed = []
    monkeypatch.setattr('orbit.providers.invoke_json', lambda c,i,p,s: (observed.append(p) or {'action':'runtime_health'}, {}))
    context = {'task': 'same context'}
    value, meta = Router({'BEDROCK_MODEL_ID':'aws'}).decide({'model':'codex','goal':'inspect'}, context)
    assert value['action'] == 'runtime_health' and meta['fallback_from'] == 'codex'
    assert observed == [context]


@pytest.mark.parametrize('events', [
    [{'type':'item.completed','item':{'type':'command_execution','command':'pwd'}}, {'type':'turn.completed'}],
    [{'type':'item.completed','item':{'type':'agent_message','text':'{"action":"finish"}'}}, {'type':'turn.completed'}],
    [{'type':'item.completed','item':{'type':'agent_message','text':'{}'}}],
])
def test_native_rejects_tools_partial_output_and_missing_completion(monkeypatch, tmp_path, events):
    from orbit.codex_provider import decide
    monkeypatch.setenv('ORBIT_CODEX_HOME',str(tmp_path))
    monkeypatch.setattr(subprocess,'run',lambda *a,**k: SimpleNamespace(returncode=0,stdout='\n'.join(json.dumps(e) for e in events),stderr=''))
    with pytest.raises(ProviderError):
        decide({},'model','instructions')
