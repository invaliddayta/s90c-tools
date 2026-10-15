#!/usr/bin/env python3
"""Opt-in live tests. Creates/removes only one named /tmp counter; never reboots."""
import argparse
import pathlib
import secrets
import subprocess
import time

root = pathlib.Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True, help='explicit consent to contact your TV')
    parser.add_argument('--tv', required=True, help='IP/hostname of a TV you own/administer')
    args = parser.parse_args()

    def call(command, timeout=15):
        start = time.monotonic()
        result = subprocess.run([str(root/'tv-access'), '--tv', args.tv, '--timeout', str(timeout), command],
                                capture_output=True, timeout=100)
        return result, time.monotonic()-start

    r,t = call('id -u')
    assert r.returncode == 0 and r.stdout == b'0\n', (r.returncode,r.stdout,r.stderr)
    print(f'PASS root uid, {t:.2f}s', flush=True)
    r,t = call("printf 'stdout-check'; printf 'stderr-check' >&2; exit 7")
    assert (r.returncode,r.stdout,r.stderr) == (7,b'stdout-check',b'stderr-check'), (r.returncode,r.stdout,r.stderr)
    print(f'PASS stdout/stderr/exit 7, {t:.2f}s', flush=True)
    r,t = call('head -c 65536 /dev/zero')
    assert r.returncode == 0 and r.stdout == b'\0'*65536, (r.returncode,len(r.stdout),r.stderr)
    print(f'PASS binary output 64 KiB, {t:.2f}s', flush=True)
    r,t = call('sleep 5', timeout=1)
    assert r.returncode == 124 and b'process group killed' in r.stderr, (r.returncode,r.stdout,r.stderr)
    print(f'PASS bounded timeout/process-group kill, {t:.2f}s', flush=True)
    p = '/tmp/tv-sdb-smoke-'+secrets.token_hex(8)
    r,t = call('printf x >> '+p)
    assert r.returncode == 0, (r.returncode,r.stdout,r.stderr)
    time.sleep(1)
    r,t = call('wc -c < '+p+'; rm -f '+p)
    assert r.returncode == 0 and r.stdout.strip() == b'1', (r.returncode,r.stdout,r.stderr)
    print('PASS exactly-once job despite repeated appinstall evaluation', flush=True)


if __name__ == '__main__':
    main()
