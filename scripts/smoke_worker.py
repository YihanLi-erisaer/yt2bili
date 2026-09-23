"""Check source/frozen workers from an unrelated cwd with isolated user data."""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument("--frozen")
args = parser.parse_args()
root = Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory(prefix="yt2bili-smoke-") as folder:
    command = [str(Path(args.frozen).resolve())] if args.frozen else [sys.executable, "-u", "-m", "yt2bili.desktop_worker"]
    command += ["--data-dir", folder, "--resources", str(root)]
    environment = {**os.environ, "PYTHONPATH": str(root) + os.pathsep + os.environ.get("PYTHONPATH", ""), "PYTHONIOENCODING": "utf-8"}
    methods = ["system.health", "settings.get", "tasks.list", "system.diagnostics"]
    requests = "".join(json.dumps({"protocol_version": 2, "request_id": str(i), "method": name, "params": {}}) + "\n"
                       for i, name in enumerate(methods))
    result = subprocess.run(command, cwd=folder, env=environment, input=requests, text=True,
                            encoding="utf-8", capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr[-3000:])
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    responses = {item["request_id"]: item for item in messages if "request_id" in item}
    assert len(responses) == len(methods), messages
    assert all("error" not in item for item in responses.values()), responses
    assert responses["0"]["result"]["protocol_version"] == 2
    assert not responses["2"]["result"]["items"]
    print(json.dumps({"worker": "frozen" if args.frozen else "source", "protocol": "passed", "responses": len(responses)}))
    result = subprocess.run(command + ["--self-test"], cwd=folder, env=environment, text=True,
                            encoding="utf-8", capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr[-3000:])
    report = json.loads(result.stdout.strip())
    assert report["ok"] and not report["submitted"]
    print(json.dumps(report))
