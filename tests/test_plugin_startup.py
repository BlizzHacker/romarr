"""Trusted Python startup and actual Hub/seccomp behavior in throwaway children."""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import types

import pytest

from romarr import plugin_safety


def test_adapter_rewrites_only_the_expected_worker_and_keeps_global_popen(monkeypatch):
    calls = []
    original = subprocess.Popen
    fake = types.SimpleNamespace(Popen=lambda *a, **kw: calls.append((a, kw)), PIPE=-1)
    adapter = plugin_safety._BrokerSubprocess(fake, ["/trusted"])
    adapter.Popen([sys.executable, "-m", "rom_hub_sdk.runner"], cwd="/plugin", env={})
    assert calls[0][0][0][:6] == [sys.executable, "-I", "-S", "-X", "utf8", "-c"]
    assert calls[0][1] == {"cwd": "/plugin", "env": {}}
    assert adapter.PIPE == -1 and subprocess.Popen is original
    with pytest.raises(RuntimeError, match="unsupported ROM Hub worker command"):
        adapter.Popen([sys.executable, "-m", "some_other_runner"])
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform != "linux", reason="native seccomp proof needs Linux")
def test_actual_worker_blocks_startup_shadowing_network_and_exec(tmp_path):
    """A functioning search plugin cannot bypass the filter using its startup path."""
    pytest.importorskip("rom_hub.broker.host")
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    marker = tmp_path / "startup-ran"
    (plugin / "sitecustomize.py").write_text(
        "from pathlib import Path; Path(" + repr(str(marker)) + ").write_text('unsafe')")
    shadow = plugin / "rom_hub_sdk"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("raise RuntimeError('untrusted SDK shadow executed')")
    (plugin / "pydantic.py").write_text("raise RuntimeError('untrusted dependency shadow executed')")
    (plugin / "pyseccomp.py").write_text("raise RuntimeError('untrusted seccomp binding shadow executed')")
    (plugin / "probe_plugin.py").write_text(textwrap.dedent("""
        import json, os, socket, subprocess
        from rom_hub_sdk import SearchProvider, SearchResult
        class Search(SearchProvider):
            def search(self, query, platform, limit):
                checks = {}
                for name, call in [('socket', socket.socket), ('exec', lambda: subprocess.run(['/bin/true']))]:
                    try:
                        value = call()
                        if hasattr(value, 'close'): value.close()
                        checks[name] = False
                    except PermissionError:
                        checks[name] = True
                checks['no_parent_secrets'] = not any(k in os.environ for k in ['ROMARR_API_KEY','ROMM_PASSWORD','ROMM_USER'])
                checks['unicode_query'] = query == '日本語 café'
                checks['allowed_rpc'] = self.ctx.http.get('https://allowed.example/data').text == 'broker payload'
                return [SearchResult(source_id='proof', title=json.dumps(checks))]
    """))
    script = textwrap.dedent(r"""
        import json,sys
        from pathlib import Path
        from rom_hub.sandbox import probe
        if not probe()[0]:
            print(json.dumps({'skip': probe()[1]}));sys.exit(0)
        from rom_hub.broker.host import PluginProcess
        from rom_hub.manifest import parse_manifest
        from romarr.plugin_safety import install_trusted_startup
        import subprocess
        original = subprocess.Popen
        from romarr import hub
        assert hub._unsandboxed() is False
        install_trusted_startup()
        assert subprocess.Popen is original
        manifest=parse_manifest('[plugin]\nslug="proof"\nname="Proof"\nversion="1.0.0"\nrpp_version="1"\n[capabilities]\nsearch="probe_plugin:Search"\n[permissions]\nnetwork=["allowed.example"]\nromm_api=[]\n')
        class Fetcher:
            def get(self,url,params):return 200,'broker payload'
        with PluginProcess(Path(sys.argv[1]),manifest,{},Fetcher(),timeout=10,allow_unsandboxed=False) as process:
            assert process.sandboxed
            result=process.search('日本語 café',None,5)[0]
            print(result.title)
    """)
    env = dict(os.environ, ROMARR_API_KEY="parent-only-test-key", ROMM_PASSWORD="parent-only-password", ROMM_USER="parent-user")
    result = subprocess.run([sys.executable, "-c", script, str(plugin)],
                            capture_output=True, text=True, env=env, timeout=40)
    assert result.returncode == 0, result.stderr
    checks = json.loads(result.stdout)
    if "skip" in checks:
        pytest.skip(checks["skip"])
    assert checks == {"socket": True, "exec": True, "no_parent_secrets": True, "allowed_rpc": True, "unicode_query": True}
    assert not marker.exists(), "plugin startup hook ran before confinement"
