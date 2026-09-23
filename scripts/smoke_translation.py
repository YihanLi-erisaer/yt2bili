"""Exercise the packaged request worker against a local fake Ollama, offline."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from yt2bili.translation.config import DEFAULTS
from yt2bili.translation.runtime import manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen", type=Path)
    args = parser.parse_args()
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def send_json(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            self.send_json({"version":"test"} if self.path=="/api/version" else
                           {"models":[{"name":"qwen3:8b","digest":manifest()["model"]["digest"]}]})
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))));
            self.send_json({"done":True,"done_reason":"stop","message":{"content":json.dumps({"title":"工作流","description":"测试正文"})}})
    server = ThreadingHTTPServer(("127.0.0.1",0), Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as folder:
            payload={"provider":"local_llm","root":folder,"config":{**DEFAULTS,"local_llm_mode":"external",
                "local_llm_base_url":f"http://127.0.0.1:{server.server_port}"},
                "source":{"title":"Workflow","description":"Example","source_lang":"en"}}
            command = [str(args.frozen.resolve()),"--translation-request"] if args.frozen else [sys.executable,"-m","yt2bili.translation.worker"]
            reply=subprocess.run(command,input=json.dumps(payload),encoding="utf-8",capture_output=True,timeout=30,
                                 cwd=folder,env={**os.environ,"PYTHONPATH":str(root)})
            if reply.returncode:raise RuntimeError(reply.stderr[-1500:])
            value=json.loads(reply.stdout)
            assert value["result"]["title"]=="工作流",value
            assert requests[0]["think"] is False and requests[0]["format"]["additionalProperties"] is False
            print(json.dumps({"worker":"frozen" if args.frozen else "source","translation":"passed","external_network_used":False}))
    finally:
        server.shutdown();server.server_close();thread.join()


if __name__=="__main__":main()
