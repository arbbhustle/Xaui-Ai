"""Manual local HTTP/Android contract probe. TEST_DATA seeding lives only in tests.

python -B -m backend.tests.mobile_http_probe --output <temporary-directory>
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import json
import socket
import sys
import threading
import time

import httpx
import uvicorn

sys.path.insert(0,str(Path(__file__).parent))
from backend.mobile.api import create_app
from backend.mobile.runtime import Runtime
from test_mobile_api import seed, ENDPOINTS
from test_phase1 import NOW


def run(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    results={}
    for scenario in ('empty','fixture'):
        directory=output/scenario;directory.mkdir(exist_ok=True)
        database=directory/'probe.sqlite3'
        if database.exists():raise ValueError('USE_NEW_PROBE_DIRECTORY')
        if scenario=='fixture':
            with Runtime(database) as runtime:seed(runtime)
        app=create_app(database,clock=lambda:NOW)
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        try:
            for _ in range(200):
                if server.started:break
                if not thread.is_alive():raise RuntimeError('LOCAL_SERVER_START_FAILED')
                time.sleep(.05)
            with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False) as client:
                for endpoint in ENDPOINTS:
                    response=client.get(endpoint);response.raise_for_status()
                    payload=response.json()
                    (directory/(endpoint[1:]+'.json')).write_text(json.dumps(payload,allow_nan=False),encoding='utf-8')
                    results[scenario+endpoint]={'status':response.status_code,'bytes':len(response.content)}
            assert app.state.runtime.replay['status']==('PASSED' if scenario=='fixture' else 'NO_DECISIONS_YET')
        finally:
            server.should_exit=True;thread.join(timeout=15);sock.close()
            if thread.is_alive():raise RuntimeError('LOCAL_SERVER_SHUTDOWN_FAILED')
    print(json.dumps(results,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True)
    run(parser.parse_args().output)
