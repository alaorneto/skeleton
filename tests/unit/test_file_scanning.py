"""Antivirus verdicts and the clamd INSTREAM protocol against a local fake clamd (#62)."""

import io
import socket
import struct
import threading

import pytest

from skeleton.files.scanning import ClamAV, ScannerUnavailable, parse_reply

EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def test_replies_map_to_states():
    assert parse_reply(b"stream: OK\0").clean
    infected = parse_reply(b"stream: Win.Test.EICAR_HDB-1 FOUND\0")
    assert (infected.clean, infected.signature) == (False, "Win.Test.EICAR_HDB-1")
    for reply in (b"", b"stream: INSTREAM size limit exceeded. ERROR\0", b"garbage"):
        with pytest.raises(ScannerUnavailable):
            parse_reply(reply)


class FakeClamd:
    """Speaks INSTREAM: reads length-prefixed chunks and flags the EICAR string."""

    def __init__(self):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.received = b""
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        connection, _ = self.server.accept()
        with connection:
            command = b""
            while not command.endswith(b"\0"):
                command += connection.recv(1)
            assert command == b"zINSTREAM\0"
            while True:
                (length,) = struct.unpack("!I", self._exact(connection, 4))
                if not length:
                    break
                self.received += self._exact(connection, length)
            verdict = b"stream: Eicar-Signature FOUND\0" if EICAR in self.received else b"stream: OK\0"
            connection.sendall(verdict)
        self.server.close()

    @staticmethod
    def _exact(connection, size):
        data = b""
        while len(data) < size:
            data += connection.recv(size - len(data))
        return data


def test_instream_flags_eicar_and_passes_clean_files():
    clamd = FakeClamd()
    assert not ClamAV(f"tcp://127.0.0.1:{clamd.port}").scan(io.BytesIO(b"prefix " + EICAR)).clean
    assert clamd.received == b"prefix " + EICAR
    clean = FakeClamd()
    assert ClamAV(f"tcp://127.0.0.1:{clean.port}").scan(io.BytesIO(b"x" * 200_000)).clean
    assert len(clean.received) == 200_000  # Sent in several chunks.


def test_unreachable_clamd_is_unavailable():
    unused = socket.socket()
    unused.bind(("127.0.0.1", 0))
    port = unused.getsockname()[1]
    unused.close()
    with pytest.raises(ScannerUnavailable):
        ClamAV(f"tcp://127.0.0.1:{port}", timeout=2).scan(io.BytesIO(b"x"))
