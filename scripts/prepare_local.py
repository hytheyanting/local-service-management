#!/usr/bin/env python3
"""Initialize missing local files without overwriting an existing registry."""
import json
import pathlib
import sys

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from build_config import generate


def main():
    path=ROOT/'services.json'
    try:
        with path.open('x') as file:file.write('{}\n')
        path.chmod(0o600)
    except FileExistsError:
        pass
    records=json.loads(path.read_text())
    if not isinstance(records,dict):raise ValueError('services.json must contain a JSON object')
    (ROOT/'state').mkdir(exist_ok=True,mode=0o700)
    (ROOT/'logs').mkdir(exist_ok=True,mode=0o700)
    generate(records)
    print('Local files ready. Existing service registrations were preserved.')
    print('Start the UI: python3 ui_server.py')
    print('Refresh status in another terminal: python3 sync.py')


if __name__=='__main__':main()
