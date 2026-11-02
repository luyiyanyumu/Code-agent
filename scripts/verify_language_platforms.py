"""Real Swift/PHP LSP behavior with packaged tools and isolated user state."""
import argparse,asyncio,ctypes,json,os,sys,time
from pathlib import Path


async def verify(args,report):
    for name in list(os.environ):
        if name.upper().startswith(('CODE_AGENT_','CH_AGENT_','OPENAI_','DEEPSEEK_','PHP','SWIFT','SDKROOT','INCLUDE','LIB','PYTHONPATH')):
            os.environ.pop(name,None)
    os.environ['PATH']=os.pathsep.join([str(Path(os.environ['SystemRoot'])/'System32'),os.environ['SystemRoot']])
    os.environ['CODE_AGENT_HOME']=str(args.output/'user-data')
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    ctypes.windll.kernel32.SetErrorMode(0x8003)
    sys.path.insert(0,str(args.app_root))
    from code_agent.codeintel.bundled_lsp import _launch
    from code_agent.extensions.jsonrpc_stdio import JsonRpcStdio
    from code_agent.codeintel.lsp import client_capabilities
    registry=json.loads((args.registry or args.app_root/'runtimes/language-servers/platform-servers.json').read_text())
    for raw in registry['servers']:
        if args.server and raw['id']!=args.server:continue
        sid=raw['id'];launch=_launch(args.app_root,raw)
        project=args.output/sid;project.mkdir()
        if sid=='php':
            text='<?php\nfunction twice(int $value): int { return $value * 2; }\n$result = twice(3);\n'
            file=project/'example.php';broken='<?php\n$result = ;\n'
        else:
            text='import Foundation\nlet greeting: String = "hello"\nprint(greeting.uppercased())\n'
            file=project/'example.swift';broken='import Foundation\nlet number: Int = "wrong"\n'
        file.write_text(text)
        diagnostics={};messages=[]
        client=JsonRpcStdio(launch['command'],launch['args'],cwd=str(project),env=launch['env'],framing='content-length',name='portable-'+sid)
        def notification(message):
            messages.append(message)
            if message.get('method')=='textDocument/publishDiagnostics':diagnostics[message['params']['uri']]=message['params'].get('diagnostics',[])
        client.on_notification=notification
        client.on_request=lambda message:[{} for _ in message.get('params',{}).get('items',[])] if message.get('method')=='workspace/configuration' else None
        started=time.monotonic()
        print(json.dumps({'starting':sid}),flush=True)
        try:
            await client.start()
            result=await client.request('initialize',{'processId':os.getpid(),'rootUri':project.as_uri(),'workspaceFolders':[{'uri':project.as_uri(),'name':sid}],'capabilities':client_capabilities(),'initializationOptions':launch.get('initialization_options',{})},timeout=45)
            await client.notify('initialized',{})
            await client.notify('textDocument/didOpen',{'textDocument':{'uri':file.as_uri(),'languageId':sid,'version':1,'text':text}})
            line,char=(2,12) if sid=='php' else (2,9)
            hover=None
            until=time.monotonic()+35
            while time.monotonic()<until:
                hover=await client.request('textDocument/hover',{'textDocument':{'uri':file.as_uri()},'position':{'line':line,'character':char}},timeout=60)
                if hover:break
                await asyncio.sleep(.25)
            assert hover,{'server':sid,'hover':hover,'stderr':client.stderr_tail()}
            if sid=='swift':
                assert not any(row.get('severity')==1 for row in diagnostics.get(file.as_uri(),[])),diagnostics
            diagnostics.pop(file.as_uri(),None)
            await client.notify('textDocument/didChange',{'textDocument':{'uri':file.as_uri(),'version':2},'contentChanges':[{'text':broken}]})
            until=time.monotonic()+35
            while time.monotonic()<until and not any(row.get('severity')==1 for row in diagnostics.get(file.as_uri(),[])):
                await asyncio.sleep(.2)
            errors=[row for row in diagnostics.get(file.as_uri(),[]) if row.get('severity')==1]
            assert errors,{'server':sid,'noErrors':diagnostics,'stderr':client.stderr_tail()}
            await client.notify('textDocument/didChange',{'textDocument':{'uri':file.as_uri(),'version':3},'contentChanges':[{'text':text}]})
            until=time.monotonic()+35
            while time.monotonic()<until and any(row.get('severity')==1 for row in diagnostics.get(file.as_uri(),[])):
                await asyncio.sleep(.2)
            assert not any(row.get('severity')==1 for row in diagnostics.get(file.as_uri(),[])),diagnostics
            report['checks'].append({'server':sid,'hover':True,'diagnosticErrors':len(errors),'diagnosticsCleared':True,'initializeSeconds':round(time.monotonic()-started,2),'diskUnchanged':file.read_text()==text})
            assert file.read_text()==text
            print(json.dumps({'passed':sid}),flush=True)
        finally:
            await client.stop()
    report['passed']=True


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--registry',type=Path);parser.add_argument('--server',choices=['php','swift'])
    args=parser.parse_args();args.app_root=args.app_root.resolve();args.output=args.output.resolve();args.output.mkdir(parents=True)
    report={'passed':False,'checks':[],'modelRequests':0}
    try:asyncio.run(verify(args,report))
    except Exception:
        import traceback
        report['error']=traceback.format_exc()
    (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
