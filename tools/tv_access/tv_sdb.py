#!/usr/bin/env python3
"""Run one bounded root command over SDB using Chris's archive bootstrap."""
import argparse
import contextlib
import fcntl
import io
import os
from pathlib import Path
import secrets
import shlex
import struct
import subprocess
import sys
import tarfile
import tempfile
import time

from sdb_direct import DirectSDB, SDBError
from tv_config import resolve_host

HERE = Path(__file__).resolve().parent
TOOLS = '/home/owner/share/tmp/sdk_tools'


@contextlib.contextmanager
def exclusive():
    cache = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'tv-sdb'
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(cache / 'lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        end = time.monotonic() + 15
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > end:
                    raise RuntimeError('Another tv-access SDB command holds the local lock')
                time.sleep(.1)
        yield
    finally:
        os.close(fd)


KNOWN_GOOD = {
    'T-PTMCDEUC-REL-202408210126': (
        'tested configuration: Samsung QE77S90C (2023), firmware family '
        'T-PTMCDEUC-2000.2, verified 2026-10-09'),
}

# Read-only device/capability report. Success of this job through the bootstrap
# is itself the end-to-end proof: handshake, appinstall path, dotnet and
# tarlauncher all had to work for the report to come back with uid=0.
PROBE_COMMAND = (
    'echo "uid=$(id -u)"; echo "arch=$(uname -m)"; echo "kernel=$(uname -r)"; '
    'echo "tizen=$(sed -n \'s/^VERSION=//p\' /etc/os-release 2>/dev/null | head -1)"; '
    'echo "build=$(sed -n \'s/^BUILD_ID=//p\' /etc/os-release /etc/info.ini 2>/dev/null | head -1)"; '
    'for f in /usr/bin/dotnet /usr/sbin/sdbd-tarlauncher; do '
    '[ -e "$f" ] && echo "have:$f" || echo "missing:$f"; done'
)


def parse_probe(stdout):
    """Parse PROBE_COMMAND output into (info dict, present list, missing list)."""
    info, present, missing = {}, [], []
    for line in stdout.decode(errors='replace').splitlines():
        if line.startswith('have:'):
            present.append(line[5:])
        elif line.startswith('missing:'):
            missing.append(line[8:])
        elif '=' in line:
            key, _, value = line.partition('=')
            info[key.strip()] = value.strip().strip('"')
    return info, present, missing


def build_package(work, launch):
    """Build the signed launch archive consumed by the vendored bootstrap.

    Private signing key is per-job, kept in a 0700 temporary directory, never
    sent to the TV, and deleted with the directory. This is not a device-wide
    credential or a modification to the TV's global user/key databases.
    """
    work = Path(work)
    private, public = work/'private.pem', work/'public.pem'
    passwd, archive, signature = work/'passwd', work/'package.tar.gz', work/'package.tar.gz.signature'
    for args in [('genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', str(private)),
                 ('pkey', '-in', str(private), '-pubout', '-out', str(public))]:
        subprocess.run(['openssl', *args], check=True, capture_output=True)
    passwd.write_text('owner:x:0:0:owner:/home/owner:/bin/sh\n')
    with tarfile.open(archive, 'w:gz') as out:
        member = tarfile.TarInfo('launch.sh')
        member.size, member.mode, member.mtime = len(launch), 0o600, 0
        out.addfile(member, io.BytesIO(launch))
    subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(private), '-out', str(signature), str(archive)],
                   check=True, capture_output=True)
    return public, passwd, archive, signature


def execute(host, command, timeout):
    # Connect before generating/staging anything. Only the transport's safe
    # pre-request handshake retries apply; never fall back to the old callback.
    with DirectSDB(host):
        pass
    job = 'archive-root-' + secrets.token_hex(8)
    directory = TOOLS + '/' + job
    port = 18000 + secrets.randbelow(4000)
    reply_key = secrets.token_bytes(32)  # per-job only; never printed or persisted locally
    helper = HERE / 'build/SdbJob.dll'
    if not helper.is_file():
        raise RuntimeError('Missing build/SdbJob.dll; run the documented build step')
    launch = (f'D={shlex.quote(directory)}\n'
              'if ! mkdir "$D/run.once" 2>/dev/null; then exit 0; fi\n'
              f'(nohup /usr/bin/dotnet "$D/SdbJob.dll" "$D" {port} {timeout} '
              '< /dev/null > "$D/job.log" 2>&1 &)\n'
              'exit 0\n').encode()
    with tempfile.TemporaryDirectory(prefix='tv-sdb-job-') as work:
        public, passwd, archive, signature = build_package(work, launch)
        payloads = HERE / 'bootstrap'
        runtime = (payloads / 'SamsungTvArchiveRoot.runtimeconfig.json').read_bytes()
        files = [
            ('SamsungTvArchiveRoot.dll', (payloads / 'SamsungTvArchiveRoot.dll').read_bytes(), 0o644),
            ('SamsungTvArchiveRoot.runtimeconfig.json', runtime, 0o644),
            ('SdbJob.dll', helper.read_bytes(), 0o644),
            ('SdbJob.runtimeconfig.json', runtime, 0o644),
            ('public.pem', Path(public).read_bytes(), 0o644),
            ('passwd', Path(passwd).read_bytes(), 0o644),
            ('command.txt', command.encode(), 0o600),
            ('reply.key', reply_key, 0o600),
        ]
        with DirectSDB(host) as client:
            client.execute_sdk(f'mkdir -p {directory} {TOOLS}/on-demand')
        for name, data, mode in files:
            with DirectSDB(host) as client:
                client.push(data, directory + '/' + name, mode)
        for suffix, local_path in [('.tar.gz', archive), ('.tar.gz.signature', signature)]:
            with DirectSDB(host) as client:
                client.push(Path(local_path).read_bytes(), TOOLS + '/on-demand/' + job + suffix, 0o644)
    # This service has no exit code. Only an authenticated TSR1 response with uid=0
    # counts as success. Never re-run this request merely because retrieval fails.
    with DirectSDB(host, timeout=30) as client:
        client.execute_sdk(f'/usr/bin/dotnet {directory}/SamsungTvArchiveRoot.dll {job} > {directory}/bootstrap.log 2>&1')
    attach_deadline = time.monotonic() + 30
    last_error = None
    while time.monotonic() < attach_deadline:
        connection = DirectSDB(host, timeout=timeout + 20)
        try:
            connection.__enter__()
            connection.open('tcp:' + str(port))
        except (SDBError, OSError, TimeoutError) as error:
            connection.__exit__(None, None, None)
            last_error = error
            time.sleep(.5)
            continue
        try:
            connection.write(reply_key)
            magic, code, outlen, errlen, flags, uid = struct.unpack('<4siIIII', connection.read(24))
            if magic != b'TSR1' or uid != 0 or outlen > 1048576 or errlen > 1048576:
                raise SDBError('Invalid root-job response')
            stdout = connection.read(outlen)
            stderr = connection.read(errlen)
            try:
                connection.write(b'ACK1')
            except (SDBError, OSError):
                # The complete authenticated result was already received. A lost
                # cleanup ACK must not turn it into an invitation to re-run.
                print('tv-access: result received; cleanup ACK uncertain (job expires)', file=sys.stderr)
            return code, stdout, stderr, flags
        finally:
            connection.__exit__(None, None, None)
    raise RuntimeError(f'No verified response for job {job}; command NOT retried. Last error: {last_error}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tv', help='TV address; otherwise TV_HOST or local config.json')
    parser.add_argument('--timeout', type=int, default=45, help='command deadline in seconds (1–300)')
    parser.add_argument('--probe', action='store_true',
                        help='read-only end-to-end check: SDB, root chain, device/firmware support')
    parser.add_argument('command', nargs='?', help='one quoted shell command')
    args = parser.parse_args()
    if not 1 <= args.timeout <= 300:
        parser.error('--timeout must be 1–300')
    if not args.probe and args.command is None:
        parser.error('supply one quoted shell command or --probe')
    if args.command is not None and (len(args.command.encode()) > 65536 or '\x00' in args.command):
        parser.error('command must be NUL-free and no more than 64 KiB')
    try:
        host = resolve_host(args.tv)
        with exclusive():
            if args.probe:
                code, stdout, stderr, _flags = execute(host, PROBE_COMMAND, 30)
                if code != 0:
                    print('tv-access: probe command failed on the TV', file=sys.stderr)
                    sys.stderr.buffer.write(stderr)
                    return 125
                info, present, missing = parse_probe(stdout)
                build = info.get('build', '') or 'unknown'
                print('SDB handshake + root chain verified on ' + host + ':26101')
                print('Device: uid {uid}, Tizen {tizen}, kernel {kernel}, {arch}'.format(
                    uid=info.get('uid', '?'), tizen=info.get('tizen', '?'),
                    kernel=info.get('kernel', '?'), arch=info.get('arch', '?')))
                print('Firmware build: ' + build)
                for path in present:
                    print('  present: ' + path)
                for path in missing:
                    print('  MISSING: ' + path)
                if missing:
                    print('tv-access: unsupported device/firmware — required binaries missing',
                          file=sys.stderr)
                    return 125
                known = KNOWN_GOOD.get(build)
                if known:
                    print('Assessment: ' + known)
                else:
                    print('Assessment: the root chain WORKS on this device, but this firmware '
                          'build is untested — treat as experimental and report your result.')
                return 0
            code, stdout, stderr, flags = execute(host, args.command, args.timeout)
        sys.stdout.buffer.write(stdout)
        sys.stderr.buffer.write(stderr)
        if flags & 1: print('tv-access: command timed out; process group killed', file=sys.stderr)
        if flags & 6: print('tv-access: output capped at 1 MiB per stream', file=sys.stderr)
        if flags & 8: print('tv-access: background children retained output pipes and were terminated', file=sys.stderr)
        return code if 0 <= code <= 255 else 1
    except KeyboardInterrupt:
        print('tv-access: interrupted; staged job may still run until its deadline', file=sys.stderr)
        return 130
    except (SDBError, OSError, RuntimeError, ValueError, TimeoutError, subprocess.CalledProcessError) as error:
        print('tv-access: ' + str(error), file=sys.stderr)
        return 125


if __name__ == '__main__':
    sys.exit(main())
