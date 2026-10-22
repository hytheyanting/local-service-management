#!/usr/bin/env python3
"""Generate per-user LaunchAgent files; never installs or loads them."""
import argparse
import pathlib
import plistlib
import sys

ROOT=pathlib.Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--include-olivetin',action='store_true')
    args=parser.parse_args()
    if args.include_olivetin and not (ROOT/'vendor/OliveTin-darwin-arm64/OliveTin').is_file():
        parser.error('Install the official OliveTin release under vendor/OliveTin-darwin-arm64 first')
    output=ROOT/'launchagents';output.mkdir(exist_ok=True)
    (ROOT/'logs').mkdir(exist_ok=True,mode=0o700)
    components={'ui':'ui_server.py','registry':'sync.py'}
    if args.include_olivetin:components['olivetin']='supervisor.py'
    for name,script in components.items():
        label='io.localservices.'+name
        value=dict(Label=label,ProgramArguments=[sys.executable,'-u',str(ROOT/script)],WorkingDirectory=str(ROOT),RunAtLoad=True,KeepAlive=True,
            EnvironmentVariables={'PATH':'/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
            StandardOutPath=str(ROOT/'logs'/(label+'.stdout.log')),StandardErrorPath=str(ROOT/'logs'/(label+'.stderr.log')))
        destination=output/(label+'.plist');destination.write_bytes(plistlib.dumps(value))
        print(destination)
    print('Generated only. Review and install these files yourself; nothing was started.')


if __name__=='__main__':main()
