"""Check default bundled routing and real editor sessions without host tools."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def emit(stage, **details):
    print(json.dumps({'stage': stage, **details}, ensure_ascii=False), flush=True)


async def verify(args, report):
    from code_agent.codeintel.lsp import LspManager
    from code_agent.codeintel.lsp_catalog import catalog, detect, merged_servers
    from code_agent.codeintel.uri import normalize_uri

    definitions = merged_servers({})
    detected = detect(config={})
    expected = {item.id for item in catalog()}
    assert len(expected) == 28
    assert {row['id'] for row in detected if row['installed']} == expected, detected
    for row in definitions:
        assert Path(row['command']).resolve().is_relative_to(args.app_root / 'runtimes'), row['id']
        if row['id'] != 'jdtls':
            assert row.get('source') == 'bundled', row
    report['detectedServices'] = len(expected)
    report['appRoot'] = str(args.app_root)
    runtime = json.loads((args.app_root/'desktop-runtime.json').read_text(encoding='utf-8-sig'))
    registry = (args.app_root/runtime['languageTools']).resolve()
    assert registry.is_relative_to(args.app_root/'runtimes')
    report['registrySha256'] = hashlib.sha256(registry.read_bytes()).hexdigest()
    report['checks'].append('all catalog commands resolve inside relocated application with system-only PATH')
    emit('detected', services=len(expected))
    manager = LspManager(config_path=args.output/'empty-lsp.json', documentation_path=args.output/'documentation.db')

    async def diagnostics(session, uri, *, errors, after=None):
        deadline = time.monotonic()+40
        normalized = normalize_uri(uri)
        while True:
            values = [items for key, items in session.diagnostics.items() if normalize_uri(key) == normalized]
            error_count = sum(int(item.get('severity', 1)) == 1 for items in values for item in items)
            if values and bool(error_count) == errors and (after is None or session.published > after):
                return error_count
            assert time.monotonic() < deadline, {'server': session.server_id, 'errorsExpected': errors,
                'published': session.published, 'diagnostics': values, 'stderr': session.client.stderr_tail(10)}
            await asyncio.sleep(.1)

    cases = [
        ('typescript', 'typescript', 'demo.ts', 'const value: string = 42;\nvalue.toUpperCase();\n', 1, 8),
        ('php', 'php', 'demo.php', '<?php\nfunction twice(int $value): int { return $value * 2; }\n$result = twice(3);\n', 2, 12),
        ('json', 'json', 'demo.json', '{"name": }\n', None, None),
    ]
    try:
        for language, expected_server, name, text, line, char in cases:
            project = args.output / language
            project.mkdir()
            file = project/name
            file.write_text(text, encoding='utf-8')
            started = time.monotonic()
            emit('start', language=language)
            session = await manager.session_for({'id':language,'root_path':str(project)}, language)
            assert session.server_id == expected_server, session.server_id
            await manager.open_document(session, file.as_uri(), text, lang_id=language)
            if line is not None:
                deadline = time.monotonic()+30
                while True:
                    hover = await session.client.request('textDocument/hover', {'textDocument':{'uri':file.as_uri()}, 'position':{'line':line,'character':char}}, timeout=20)
                    if hover:
                        break
                    assert time.monotonic()<deadline, {'language':language,'missingHover':True}
                    await asyncio.sleep(.2)
                emit('hover', language=language)
            if language == 'php':
                # Exercise the upstream no-declarations edge case too. The
                # separate function document above verifies native semantics;
                # this plain document must still get syntax errors on open.
                await manager.close_document(session, file.as_uri())
                plain = project/'plain.php'
                invalid = '<?php\n$result = ;\n'
                plain.write_text(invalid, encoding='utf-8')
                await manager.open_document(session, plain.as_uri(), invalid, lang_id=language)
                await diagnostics(session, plain.as_uri(), errors=True)
                published = session.published
                await manager.change_document(session, plain.as_uri(), '<?php\n$result = 6;\n')
                await diagnostics(session, plain.as_uri(), errors=False, after=published)
                assert plain.read_text(encoding='utf-8') == invalid
                await manager.close_document(session, plain.as_uri())
                features = ['nativeHover', 'plainSyntaxErrorOnOpen', 'didChangeClearsDiagnostics']
            else:
                await diagnostics(session, file.as_uri(), errors=True)
                published = session.published
                fixed = text.replace('= 42', '= "hello"') if language == 'typescript' else '{"name": "valid"}\n'
                await manager.change_document(session, file.as_uri(), fixed)
                await diagnostics(session, file.as_uri(), errors=False, after=published)
                features = (['standardLibraryHover'] if line is not None else []) + ['errorOnOpen', 'didChangeClearsDiagnostics']
                await manager.close_document(session, file.as_uri())
            assert file.read_text(encoding='utf-8') == text
            report['checks'].append({'language':language,'automaticServer':expected_server,'features':features,'sourceUnchanged':True,'seconds':round(time.monotonic()-started,2)})
            emit('passed', language=language)
            await manager.stop(session.key)
    finally:
        await manager.stop_all()
    assert not manager.status()['sessions'] and not manager.status()['starting']
    report['passed'] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.app_root, args.output = args.app_root.resolve(), args.output.resolve()
    args.output.mkdir(parents=True)
    sys.dont_write_bytecode = True
    for key in list(os.environ):
        if key.upper().startswith(('CODE_AGENT_', 'CH_AGENT_', 'OPENAI_', 'DEEPSEEK_', 'PYTHONPATH', 'NODE_', 'JAVA_', 'JDK_', 'GOROOT', 'GOPATH', 'RUST', 'CARGO', 'DOTNET_', 'SWIFT', 'SDKROOT', 'PHP')):
            del os.environ[key]
    home = args.output/'user-data'
    home.mkdir()
    roaming, local = home/'AppData/Roaming', home/'AppData/Local'
    roaming.mkdir(parents=True)
    local.mkdir(parents=True)
    os.environ.update(CODE_AGENT_HOME=str(home), HOME=str(home), USERPROFILE=str(home), APPDATA=str(roaming), LOCALAPPDATA=str(local), CODE_AGENT_PROVIDER='offline',
                      PATH=os.pathsep.join((str(Path(os.environ['SystemRoot'])/'System32'), os.environ['SystemRoot'])))
    sys.path.insert(0,str(args.app_root))
    os.chdir(args.output)
    report = {'passed':False,'checks':[],'modelRequests':0}
    try:
        asyncio.run(verify(args,report))
    except Exception:
        import traceback
        report['error'] = traceback.format_exc()
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
