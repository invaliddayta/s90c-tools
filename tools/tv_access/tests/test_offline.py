"""Offline tests: no TV, network access, credentials or personal config needed."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sdb_direct import DirectSDB, SDBError
from tv_config import resolve_host
from tv_sdb import KNOWN_GOOD, build_package, main, parse_probe


class FakeSocket:
    def __init__(self, data=b''):
        self.data=bytearray(data); self.sent=[]
    def recv(self,n):
        n=min(n,3); result=bytes(self.data[:n]); del self.data[:n]; return result
    def sendall(self,data): self.sent.append(data)
    def settimeout(self,value): pass


def frame(command, a0=2, a1=1, data=b''):
    word=int.from_bytes(command,'little')
    return struct.pack('<6I',word,a0,a1,len(data),sum(data)&0xffffffff,word^0xffffffff)+data


def client(data=b''):
    c=DirectSDB('not-a-real-tv.invalid'); c.sock=FakeSocket(data)
    c.deadline=time.monotonic()+5; c.remote=2
    return c


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'config.json'
        self.env=patch.dict(os.environ,{'TV_ACCESS_CONFIG':str(self.path)},clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
    def test_explicit_host(self):
        os.environ['TV_HOST']='environment.invalid'
        self.assertEqual(resolve_host('explicit.invalid'),'explicit.invalid')
    def test_environment_over_file(self):
        self.path.write_text('{"tv_host":"file.invalid"}')
        os.environ['TV_HOST']='environment.invalid'
        self.assertEqual(resolve_host(),'environment.invalid')
    def test_file(self):
        self.path.write_text('{"tv_host":"file.invalid"}')
        self.assertEqual(resolve_host(),'file.invalid')
    def test_no_personal_default(self):
        with self.assertRaises(ValueError): resolve_host()
    def test_invalid_configuration(self):
        for value in ('not-json','[]','{"tv_host":false}','{"tv_host":"bad host"}'):
            self.path.write_text(value)
            with self.subTest(value=value), self.assertRaises(ValueError): resolve_host()
    def test_invalid_host(self):
        for host in ('','https://tv.invalid','bad\x00host'):
            with self.subTest(host=host), self.assertRaises(ValueError): resolve_host(host)
    def test_missing_host_never_connects(self):
        with patch('sys.argv',['tv-access','--probe']), patch('tv_sdb.DirectSDB') as connect, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(),125); connect.assert_not_called()


class ProtocolTests(unittest.TestCase):
    def test_fragmented_receive(self):
        c=client(frame(b'WRTE',data=b'hello'))
        self.assertEqual(c.receive(),(b'WRTE',2,1,b'hello'))
    def test_invalid_magic(self):
        data=bytearray(frame(b'OKAY')); data[20]^=1
        with self.assertRaises(SDBError): client(data).receive()
    def test_invalid_checksum(self):
        data=bytearray(frame(b'WRTE',data=b'hello')); data[-1]^=1
        with self.assertRaises(SDBError): client(data).receive()
    def test_oversized_packet(self):
        data=bytearray(frame(b'WRTE')); data[12:16]=struct.pack('<I',1048577)
        with self.assertRaises(SDBError): client(data).receive()
    def test_eof(self):
        with self.assertRaises(SDBError): client(b'').receive()
    def test_close_remote_zero(self):
        c=client(frame(b'CLSE',a0=0)); self.assertEqual(c.packet(),b'CLSE'); self.assertTrue(c.closed)
    def test_cross_stream_rejected(self):
        with self.assertRaises(SDBError): client(frame(b'WRTE',a1=99)).packet()
    def test_appinstall_close_is_not_exit_status(self):
        c=client(frame(b'CLSE',a0=0)); elapsed=c.execute_sdk('true')
        self.assertGreaterEqual(elapsed,0)
        request=c.sock.sent[0][24:]
        self.assertTrue(request.startswith(b'shell:0 appinstall tpk '))
        self.assertNotIn(b'shell:0 0 appinstall',request)
        self.assertIn(base64.b64encode(b'true'),request)
        self.assertEqual(len(c.sock.sent),1) # No command replay.
    def test_send_checksum(self):
        c=client(); c.send(b'WRTE',1,2,b'abc')
        self.assertEqual(c.sock.sent[0],frame(b'WRTE',a0=1,a1=2,data=b'abc'))
    def test_sync_send_length_mode_and_chunk_size(self):
        c=client(); writes=[]
        c.open=lambda service:self.assertEqual(service,'sync:')
        c.write=writes.append
        c.read=lambda n:b'OKAY'+struct.pack('<I',0)
        c.push(b'x'*8100,'/staging/file',0o644)
        name=b'/staging/file,'+str(0o100644).encode()
        self.assertEqual(writes[0],b'SEND'+struct.pack('<I',len(name))+name)
        self.assertEqual([len(p) for p in writes[1:-1]],[4008,4008,108])
        self.assertEqual(writes[-1][:4],b'DONE')


class ProbeTests(unittest.TestCase):
    SAMPLE = (b'uid=0\narch=armv7l\nkernel=5.4.77\ntizen=7.0.0\n'
              b'build=T-PTMCDEUC-REL-202408210126\n'
              b'have:/usr/bin/dotnet\nhave:/usr/sbin/sdbd-tarlauncher\n')

    def test_parse_full_report(self):
        info, present, missing = parse_probe(self.SAMPLE)
        self.assertEqual(info['uid'], '0')
        self.assertEqual(info['arch'], 'armv7l')
        self.assertEqual(info['build'], 'T-PTMCDEUC-REL-202408210126')
        self.assertEqual(present, ['/usr/bin/dotnet', '/usr/sbin/sdbd-tarlauncher'])
        self.assertEqual(missing, [])

    def test_known_good_build_recognized(self):
        info, _, _ = parse_probe(self.SAMPLE)
        self.assertIn(info['build'], KNOWN_GOOD)

    def test_missing_binary_reported(self):
        _, present, missing = parse_probe(b'uid=0\nmissing:/usr/sbin/sdbd-tarlauncher\n')
        self.assertEqual(present, [])
        self.assertEqual(missing, ['/usr/sbin/sdbd-tarlauncher'])

    def test_quoted_values_and_noise(self):
        info, _, _ = parse_probe(b'tizen="7.0.0"\ngarbage line\nbuild=\n')
        self.assertEqual(info['tizen'], '7.0.0')
        self.assertEqual(info['build'], '')
        self.assertNotIn('garbage line', info)


class PackageTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('openssl'),'OpenSSL required for package-signing test')
    def test_archive_and_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            script=b'printf test\n'
            public,passwd,archive,signature=build_package(tmp,script)
            self.assertEqual(passwd.read_text(),'owner:x:0:0:owner:/home/owner:/bin/sh\n')
            with tarfile.open(archive) as tar:
                self.assertEqual(tar.getnames(),['launch.sh'])
                self.assertEqual(tar.getmember('launch.sh').mode,0o600)
                self.assertEqual(tar.extractfile('launch.sh').read(),script)
            result=subprocess.run(['openssl','dgst','-sha256','-verify',str(public),'-signature',str(signature),str(archive)],capture_output=True)
            self.assertEqual(result.returncode,0)


if __name__=='__main__': unittest.main()
