#!/usr/bin/env python3
"""SDB transport using Python's standard library.

One stream per connection, with fixed timeouts and size limits.
"""
import argparse
import base64
import socket
import struct
import sys
import time
from pathlib import Path

from tv_config import resolve_host


class SDBError(RuntimeError):
    pass


class DirectSDB:
    def __init__(self, host, port=26101, timeout=15):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock = None
        self.remote = None
        self.buffer = bytearray()
        self.closed = False
        self.deadline = None

    def __enter__(self):
        self.deadline = time.monotonic() + max(90, self.timeout + 30)
        # Samsung occasionally resets a fresh handshake while disposing the
        # previous connection, especially immediately after boot. Only retry
        # here, before ANY OPEN/service/job request can have been submitted.
        for attempt in range(4):
            try:
                self.sock = socket.create_connection((self.host, self.port), min(5, self.timeout))
                self.send(b'CNXN', 0x01000000, 4096, b'host::\x00')
                command, _, _, _ = self.receive()
                if command != b'CNXN':
                    raise SDBError(f'Unexpected handshake: {command!r}')
                return self
            except (ConnectionResetError, BrokenPipeError):
                if self.sock:
                    self.sock.close()
                if attempt == 3:
                    raise
                time.sleep(.2 * (2 ** attempt))
            except BaseException:
                if self.sock:
                    self.sock.close()
                raise

    def __exit__(self, *exc):
        if self.sock:
            self.sock.close()
            time.sleep(.04)  # let sdbd dispose its single-client transport

    def exact(self, n):
        result = bytearray()
        while len(result) < n:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError('SDB total deadline exceeded')
            self.sock.settimeout(min(left, self.timeout))
            block = self.sock.recv(n - len(result))
            if not block:
                raise SDBError('SDB connection closed early')
            result.extend(block)
        return bytes(result)

    def send(self, command, a0, a1, payload=b''):
        word = int.from_bytes(command, 'little')
        self.sock.sendall(struct.pack('<6I', word, a0, a1, len(payload),
                                      sum(payload) & 0xffffffff, word ^ 0xffffffff) + payload)

    def receive(self):
        word, a0, a1, size, checksum, magic = struct.unpack('<6I', self.exact(24))
        if magic != word ^ 0xffffffff or size > 1024 * 1024:
            raise SDBError('Invalid SDB packet header')
        payload = self.exact(size)
        if (sum(payload) & 0xffffffff) != checksum:
            raise SDBError('SDB payload checksum mismatch')
        return word.to_bytes(4, 'little'), a0, a1, payload

    def open(self, service):
        self.send(b'OPEN', 1, 0, service.encode() + b'\x00')
        command, remote, local, data = self.receive()
        if command != b'OKAY' or local != 1:
            raise SDBError(f'Service rejected: {service!r}, {command!r}, ids={remote}/{local}, reply={data[:512]!r}')
        self.remote = remote

    def packet(self):
        command, remote, local, data = self.receive()
        valid_remote = remote == self.remote or (command == b'CLSE' and remote == 0)
        if local != 1 or not valid_remote:
            raise SDBError(f'Stream ID mismatch: {command!r} {remote}/{local}, expected {self.remote}/1')
        if command == b'WRTE':
            self.send(b'OKAY', 1, remote)
            self.buffer.extend(data)
        elif command == b'CLSE':
            self.send(b'CLSE', 1, remote)
            self.closed = True
        elif command != b'OKAY':
            raise SDBError(f'Unexpected stream packet: {command!r}')
        return command

    def write(self, data):
        if len(data) > 4096:
            raise SDBError('Request too large')
        self.send(b'WRTE', 1, self.remote, data)
        while self.packet() != b'OKAY':
            if self.closed:
                raise SDBError('Stream closed before request acknowledged')

    def read(self, n):
        while len(self.buffer) < n:
            if self.closed:
                raise SDBError('Stream ended before complete reply')
            self.packet()
        result = bytes(self.buffer[:n])
        del self.buffer[:n]
        return result

    def execute_sdk(self, command):
        """Run through the existing appinstall command path; NO exit status/output.

        Samsung closes this service without an OKAY packet even when it runs.
        Return elapsed time only. Not a success indicator.
        """
        encoded = base64.b64encode(command.encode()).decode('ascii')
        service = f'shell:0 appinstall tpk new2.tpk`printf${{IFS}}%s${{IFS}}{encoded}|base64${{IFS}}-d|bash`.tpk'
        started = time.monotonic()
        self.send(b'OPEN', 1, 0, service.encode() + b'\x00')
        while True:
            op, remote, local, data = self.receive()
            if local != 1:
                raise SDBError('Unexpected command stream ID')
            if op == b'CLSE':
                return time.monotonic() - started
            if op == b'WRTE':
                self.send(b'OKAY', 1, remote)
            elif op != b'OKAY':
                raise SDBError(f'Unexpected command response: {op!r}')

    def service(self, name, limit=1024 * 1024):
        self.open(name)
        while not self.closed:
            self.packet()
            if len(self.buffer) > limit:
                raise SDBError('Service output exceeds limit')
        return bytes(self.buffer)

    def push(self, data, remote_path, mode=0o600):
        self.open('sync:')
        target = (remote_path + ',' + str(mode | 0o100000)).encode()
        self.write(b'SEND' + struct.pack('<I', len(target)) + target)
        for offset in range(0, len(data), 4000):
            chunk = data[offset:offset + 4000]
            self.write(b'DATA' + struct.pack('<I', len(chunk)) + chunk)
        self.write(b'DONE' + struct.pack('<I', int(time.time())))
        status, size = struct.unpack('<4sI', self.read(8))
        if status == b'FAIL' and size <= 4096:
            raise SDBError(self.read(size).decode(errors='replace'))
        if status != b'OKAY' or size != 0:
            raise SDBError(f'Unexpected sync push response: {status!r}, {size}')

    def pull(self, remote_path, limit=4 * 1024 * 1024):
        self.open('sync:')
        path = remote_path.encode()
        self.write(b'RECV' + struct.pack('<I', len(path)) + path)
        result = bytearray()
        while True:
            command, size = struct.unpack('<4sI', self.read(8))
            if command == b'DONE':
                return bytes(result)
            if size > limit or len(result) + size > limit:
                raise SDBError('File exceeds pull limit')
            if command == b'FAIL':
                raise SDBError(self.read(size).decode(errors='replace'))
            if command != b'DATA':
                raise SDBError(f'Unexpected sync reply: {command!r}')
            result.extend(self.read(size))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tv', help='TV address; otherwise TV_HOST or local config.json')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('ping')
    service = sub.add_parser('service')
    service.add_argument('name')
    pull = sub.add_parser('pull')
    pull.add_argument('remote')
    pull.add_argument('local')
    args = parser.parse_args()
    try:
        host = resolve_host(args.tv)
        with DirectSDB(host) as client:
            if args.command == 'ping':
                print(f'SDB handshake OK: {host}:26101 (direct TCP)')
            elif args.command == 'service':
                sys.stdout.buffer.write(client.service(args.name))
            else:
                data = client.pull(args.remote)
                Path(args.local).write_bytes(data)
                print(f'Pulled {len(data)} bytes via direct SDB to {args.local}')
    except (SDBError, OSError, ValueError, TimeoutError) as error:
        print(f'SDB: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
