"""Task-owned native Builder execution context; lower/judge are unchanged."""
from contextlib import ExitStack,contextmanager
import hashlib,json
from pathlib import Path
from . import direct_harbor_builder as native


@contextmanager
def execution(config, run, credential, *, base_url='https://api.deepseek.com/v1', proxy='http://127.0.0.1:7890'):
    config=Path(config);run=Path(run)
    initial=json.loads(config.read_text())
    providers={agent['env']['CODEX_CONFIG_TOML_PATH'] for agent in initial['agents']}
    if len(providers)!=1:
        raise ValueError('expected one native Builder provider config')
    provider=Path(providers.pop())
    if not provider.is_relative_to(run):
        raise ValueError('native Builder provider config must belong to this run')
    compose=run/'builder_task/environment/docker-compose.yaml'
    value=json.loads(compose.read_text())
    value['services']['main'].setdefault('environment',{}).pop('AGENTSWE_BUILDER_BROKER_TOKEN',None)
    compose.write_text(json.dumps(value,indent=2)+'\n')
    native.write_provider(provider,base_url)
    if (run/'readiness_current_binding.json').is_file():
        import sys
        sys.path.insert(0, '@@AGENTSWE_EDITING_CONTROL@@')
        from readiness_binding import configure_native_no_replay
        receipt = configure_native_no_replay(provider)
        (run/'readiness_native_retry_policy.json').write_text(json.dumps(receipt, indent=2) + '\n')
    auth_path=None
    try:
        with ExitStack() as stack:
            proxy_proof=stack.enter_context(native.existing_proxy_for_builder(config,compose,proxy,provider_url=base_url)) if proxy else None
            identity=stack.enter_context(native.direct_auth(config,credential))
            configured=json.loads(config.read_text());auth_path=Path(configured['agents'][0]['env']['CODEX_AUTH_JSON_PATH'])
            identity.update({'provider_base_url':base_url,'model':'deepseek-flash','reasoning_effort':'max',
                'proxy':proxy_proof,'task_adapter_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'native_helper_source_sha256':hashlib.sha256(Path(native.__file__).read_bytes()).hexdigest(),
                'authorization':'Builder Codex authenticates to the provider directly with the provider key (native auth.json), as in the paper Editing protocol; lower-agent and Result-judge credentials stay behind their brokers.'})
            (run/'builder_transport.json').write_text(json.dumps(identity,indent=2)+'\n')
            yield identity
    finally:
        (run/'builder_native_stats.json').write_text(json.dumps(native.native_stats(run),indent=2)+'\n')
        cleaned=json.loads(config.read_text())
        complete=(auth_path is None or not auth_path.exists()) and all('CODEX_AUTH_JSON_PATH' not in a.get('env',{}) for a in cleaned['agents'])
        (run/'builder_direct_cleanup.json').write_text(json.dumps({'complete':complete,'auth_tmpfs_removed':auth_path is None or not auth_path.exists(),'config_auth_path_removed':all('CODEX_AUTH_JSON_PATH' not in a.get('env',{}) for a in cleaned['agents']),'builder_broker_started':False},indent=2)+'\n')
        if not complete:raise RuntimeError('native Builder auth cleanup incomplete')
