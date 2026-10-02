"""Official signed-in Codex CLI as a bounded decision provider, not executor."""
import json
import os
from pathlib import Path
import subprocess
import tempfile


def decide(payload, model, instructions):
    from .providers import FIELDS, ProviderError
    home = Path(os.environ['ORBIT_CODEX_HOME'])
    # Credentials are managed by official Codex login, never read into the prompt.
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'SSL_CERT_FILE') if key in os.environ}
    env['CODEX_HOME'] = str(home)
    schema = {'type': 'object', 'properties': {key: {'type': 'string'} for key in FIELDS},
              'required': list(FIELDS), 'additionalProperties': False}
    with tempfile.TemporaryDirectory(prefix='orbit-decision-', dir=home) as directory:
        schema_path = Path(directory) / 'schema.json'
        schema_path.write_text(json.dumps(schema), encoding='utf-8')
        args = [os.environ.get('ORBIT_CODEX_BIN', '/usr/bin/codex'), 'exec',
                '--ignore-user-config', '--ignore-rules', '--strict-config', '--ephemeral',
                '--skip-git-repo-check', '--sandbox', 'read-only', '--json',
                '-C', directory, '--model', model, '--output-schema', str(schema_path)]
        # No connectors, browser, shell, vision, agents, hooks or discovery in this
        # process. Actual actions only return through Orbit's allowlisted tools.
        for feature in ('shell_tool', 'unified_exec', 'shell_snapshot', 'apps', 'plugins',
                        'browser_use', 'browser_use_external', 'computer_use', 'view_image',
                        'multi_agent', 'hooks', 'workspace_dependencies', 'tool_suggest',
                        'code_mode', 'code_mode_host', 'image_generation'):
            args += ['-c', f'features.{feature}=false']
        args += ['-c', 'web_search="disabled"', '-c', 'agents.enabled=false',
                 '-c', 'model_reasoning_effort="low"',
                 '-c', 'developer_instructions=' + json.dumps(instructions + '\nDo not use native tools. Return only the next Orbit decision.'), '-']
        try:
            result = subprocess.run(args, input=json.dumps(payload), text=True,
                                    capture_output=True, timeout=65, env=env)
        except (OSError, subprocess.TimeoutExpired):
            raise ProviderError('codex_transport_failure') from None
        if result.returncode != 0:
            raise ProviderError('codex_request_failed')
        messages, completed, usage = [], False, {}
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                raise ProviderError('invalid_codex_stream') from None
            if event.get('type') == 'item.completed':
                item = event.get('item', {})
                if item.get('type') == 'agent_message':
                    messages.append(item.get('text', ''))
                elif item.get('type') == 'error' and item.get('message') == 'Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; enable `features.code_mode_host` and install `codex-code-mode-host`.':
                    # CLI 0.150.1 emits this startup warning when we deliberately
                    # disable its tool host. It is not a failed model turn.
                    continue
                elif item.get('type') != 'reasoning':
                    raise ProviderError('unexpected_native_tool')
            elif event.get('type') == 'turn.completed':
                completed, usage = True, event.get('usage', {})
            elif event.get('type') in {'error', 'turn.failed'}:
                raise ProviderError('codex_request_failed')
        if not completed or len(messages) != 1:
            raise ProviderError('incomplete_codex_decision')
        try:
            decision = json.loads(messages[0])
            if set(decision) != set(FIELDS) or any(not isinstance(decision[key], str) for key in FIELDS):
                raise ValueError()
        except (ValueError, TypeError):
            raise ProviderError('invalid_codex_decision') from None
        return decision, {'provider': 'codex', 'model': model, 'usage': usage}
