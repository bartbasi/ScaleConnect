"""Replay check: do the fake scale's replies match the real scale's replies?

Usage:
    python tests/fake_scale/replay_check.py CAPTURE_PATH [--tshark PATH] [-v]

CAPTURE_PATH is a .pcapng file or a folder holding them. Real captures are
Tor-rey traffic and stay out of the repo, so the path is always an argument.
If tshark or the captures are missing, the script says SKIPPED and exits 0.

For every TCP stream to port 50036 it takes the messages the client sent,
sends the same bytes to a fake scale on localhost, and compares the fake's
reply to the reply the real scale gave, byte for byte. Where the real scale
gave no reply, it checks the fake gave none either, and that the connection
was closed by the scale when the capture shows the scale closing it.

Messages come in two groups.
  Library messages: RI, bX01XX, GP, GPF0, CX, LP and the Ok that asks for the
  next read block (and SetHORA and SetFECHA, which ScaleLink sends). The fake
  must match these. A difference fails the run.
  Lab probes: anything else (E, SP, DP, LH, LT, LG, junk). The fake does not
  copy these on purpose: the library must never send them, so the fake treats
  them as a test failure. They are counted and listed by kind only. Their
  replies are never printed or kept, because one of them (LG) returns values
  that look like passwords.

For LP the fake's table is loaded from the blocks the real scale sent in the
same stream, because the real table is not in this repo.

Exit code: 0 if every library message matched (or the run was skipped), 1 if
any library message differed.
"""

from __future__ import annotations

import argparse
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fake_scale.server import FakeScale  # noqa: E402
from fake_scale.wire import split_clock, split_framed  # noqa: E402

PORT = 50036
DEFAULT_TSHARK = r"C:\Program Files\Wireshark\tshark.exe"
LIBRARY_KINDS = ("RI", "bX01XX", "GP", "GPF0", "CX", "LP", "Ok", "SetHORA", "SetFECHA")
_BLOCK_END = re.compile(rb"\t\n.\r.\r")


def kind_of(msg: bytes) -> str:
    """The kind of a client message, or 'other' for lab probes."""
    if msg.startswith(b"SetHORA,"):
        return "SetHORA"
    if msg.startswith(b"SetFECHA,"):
        return "SetFECHA"
    text = msg.split(b"\t\n", 1)[0]
    if text == b"RI":
        return "RI"
    if text == b"bX01XX":
        return "bX01XX"
    if text == b"GPF0":
        return "GPF0"
    if text == b"CX":
        return "CX"
    if text == b"Ok":
        return "Ok"
    if text.startswith(b"LP"):
        return "LP"
    if text.startswith(b"GP") and text[2:4].isdigit():
        return "GP"
    return "other"


@dataclass
class Result:
    capture: str
    stream: int
    index: int
    kind: str
    verdict: str  # match, mismatch, not_comparable
    request: bytes = b""  # kept for library kinds only
    real: bytes = b""
    fake: bytes = b""
    note: str = ""

    @property
    def is_library(self) -> bool:
        return self.kind in LIBRARY_KINDS


@dataclass
class Report:
    skipped: str = ""
    results: list[Result] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    def count(self, verdict: str, library: bool | None = None) -> int:
        return sum(
            1 for r in self.results
            if r.verdict == verdict and (library is None or r.is_library == library)
        )

    @property
    def mismatches(self) -> list[Result]:
        """Mismatches on library messages. These are the ones that matter."""
        return [r for r in self.results if r.verdict == "mismatch" and r.is_library]


# ---------- reading captures ----------

def find_tshark(explicit: str | None) -> str | None:
    for cand in (explicit, DEFAULT_TSHARK, shutil.which("tshark")):
        if cand and Path(cand).exists():
            return cand
    return None


def _tshark(tshark: str, capture: Path, display_filter: str, fields: list[str]) -> list[list[str]]:
    cmd = [tshark, "-r", str(capture), "-Y", display_filter, "-T", "fields"]
    for f in fields:
        cmd += ["-e", f]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return [line.split("\t") for line in out.stdout.splitlines()]


@dataclass
class Burst:
    from_client: bool
    data: bytes


@dataclass
class Stream:
    id: int
    bursts: list[Burst]
    closed_by_scale: bool
    closed_by_client: bool = False


def read_streams(tshark: str, capture: Path) -> list[Stream]:
    rows = _tshark(
        tshark,
        capture,
        f"tcp.len>0 && tcp.port=={PORT} && !tcp.analysis.retransmission",
        ["tcp.stream", "tcp.dstport", "tcp.payload"],
    )
    closes = _tshark(
        tshark,
        capture,
        f"tcp.srcport=={PORT} && (tcp.flags.fin==1 || tcp.flags.reset==1)",
        ["tcp.stream"],
    )
    closed = {int(r[0]) for r in closes if r and r[0]}
    client_closes = _tshark(
        tshark,
        capture,
        f"tcp.dstport=={PORT} && (tcp.flags.fin==1 || tcp.flags.reset==1)",
        ["tcp.stream"],
    )
    client_closed = {int(r[0]) for r in client_closes if r and r[0]}
    streams: dict[int, list[Burst]] = {}
    for r in rows:
        if len(r) < 3 or not r[2]:
            continue
        sid = int(r[0])
        from_client = int(r[1]) == PORT
        data = bytes.fromhex(r[2].replace(":", ""))
        bursts = streams.setdefault(sid, [])
        if bursts and bursts[-1].from_client == from_client:
            bursts[-1].data += data
        else:
            bursts.append(Burst(from_client, data))
    return [Stream(sid, b, sid in closed, sid in client_closed) for sid, b in sorted(streams.items())]


def split_messages(data: bytes) -> list[bytes]:
    out = []
    while data:
        got = split_clock(data) if data.startswith((b"SetHORA,", b"SetFECHA,")) else split_framed(data)
        if got is None:
            out.append(data)  # incomplete, send as is
            break
        out.append(got[0])
        data = got[1]
    return out


def lp_records(server_data: bytes) -> list[tuple[str, ...]]:
    """Records from the LP blocks in a stream's server data, by id, each once."""
    seen: dict[int, tuple[str, ...]] = {}
    for m in re.finditer(rb"LP(\d\d)", server_data):
        pass
    i = 0
    while True:
        i = server_data.find(b"LP", i)
        if i < 0:
            break
        head = server_data[i + 2 : i + 4]
        if not head.isdigit() or int(head) == 0:
            i += 2
            continue
        count = int(head)
        end = _BLOCK_END.search(server_data, i)
        if end is None:
            break
        body = server_data[i + 4 : end.start() + 2]  # records, each ending TAB LF
        recs = [r for r in body.split(b"\t\n") if r]
        if len(recs) == count:
            for r in recs:
                fields = tuple(f.decode("latin-1") for f in r.split(b"\t"))
                if len(fields) == 20 and fields[0].isdigit():
                    seen.setdefault(int(fields[0]), fields)
        i = end.end()
    return [seen[k] for k in sorted(seen)]


# ---------- talking to the fake ----------

def reply_complete(buf: bytes, request: bytes) -> bool:
    if request.startswith((b"SetHORA,", b"SetFECHA,")):
        return buf.endswith(b"\n")
    if kind_of(request) in ("LP", "Ok") or (buf.startswith(b"LP")):
        return bool(_BLOCK_END.search(buf[-8:])) or buf.startswith(b"LP00End") and buf.endswith(b"\r")
    need = 2 if buf[1:2] == b"\r" else 1
    return buf.endswith(b"\r") and buf.count(b"\r") >= need


def exchange(sock: socket.socket, request: bytes, wait: float = 1.0) -> tuple[bytes, bool]:
    """Send one message. Returns (reply bytes, fake closed the connection)."""
    try:
        sock.sendall(request)
    except OSError:
        return b"", True
    sock.settimeout(wait)
    buf = b""
    while True:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            return buf, False
        except (ConnectionResetError, ConnectionAbortedError):
            return buf, True
        if not chunk:
            return buf, True
        buf += chunk
        if reply_complete(buf, request):
            return buf, False


def read_to_end(sock: socket.socket, wait: float = 0.6) -> bytes:
    """After the client half closes, whatever the scale still sends."""
    sock.settimeout(wait)
    buf = b""
    while True:
        try:
            chunk = sock.recv(65536)
        except (socket.timeout, ConnectionResetError, ConnectionAbortedError):
            return buf
        if not chunk:
            return buf
        buf += chunk


def serial_from(streams: list[Stream]) -> str | None:
    for s in streams:
        for b in s.bursts:
            if not b.from_client and b.data.startswith(b"CX00\t"):
                parts = b.data.split(b"\t")
                if len(parts) > 1:
                    return parts[1].decode("latin-1")
    return None


def replay_capture(tshark: str, capture: Path, report: Report) -> None:
    streams = read_streams(tshark, capture)
    if not streams:
        report.notes.append(f"{capture.name}: no traffic to port {PORT}")
        return
    serial = serial_from(streams)
    opts = dict(allow_clock=True, allow_lp_params=True, serial=serial or "FAKE-000001",
                post_write_refusal_seconds=0.0)
    fake = FakeScale(**opts).start()
    loose = FakeScale(strict_sequence=False, **opts).start()
    try:
        for st in streams:
            bursts = st.bursts
            if not bursts:
                continue
            mid_stream = False
            if not bursts[0].from_client:
                report.notes.append(
                    f"{capture.name} stream {st.id}: begins with a scale reply, so the capture "
                    "started mid stream. That reply was not compared."
                )
                bursts = bursts[1:]
                mid_stream = True
            if not bursts:
                continue
            first = kind_of(split_messages(bursts[0].data)[0])
            if first not in ("RI", "bX01XX", "CX", "SetHORA", "SetFECHA", "LP"):
                mid_stream = True
            target = loose if mid_stream else fake
            is_read = any(kind_of(split_messages(b.data)[0]) == "LP" for b in bursts if b.from_client)
            if is_read:
                target.set_table(lp_records(b"".join(b.data for b in bursts if not b.from_client)))
            sock = socket.create_connection(target.address, timeout=2)
            index = 0
            try:
                last_client = max(i for i, b in enumerate(bursts) if b.from_client)
                for bi, burst in enumerate(bursts):
                    if not burst.from_client:
                        continue
                    nxt = bursts[bi + 1] if bi + 1 < len(bursts) else None
                    real = nxt.data if nxt is not None and not nxt.from_client else b""
                    msgs = split_messages(burst.data)
                    kinds = [kind_of(m) for m in msgs]
                    kind = kinds[0]
                    fake_reply = b""
                    fake_closed = False
                    for m in msgs:
                        r, closed = exchange(sock, m)
                        fake_reply += r
                        fake_closed = fake_closed or closed
                        if closed:
                            break
                    if is_read and bi == last_client and not fake_closed and st.closed_by_client:
                        # The PC closed its side. The real scale may still send the
                        # block again and LP00End, so listen after a half close.
                        try:
                            sock.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                        fake_reply += read_to_end(sock)
                    index += 1
                    res = judge(capture.name, st, index, kind, burst.data, real, fake_reply,
                                fake_closed, nxt is None, half_closed=False)
                    if kind not in LIBRARY_KINDS:
                        res.request = res.real = res.fake = b""  # never keep probe bytes
                    report.results.append(res)
                    if fake_closed:
                        break
            finally:
                sock.close()
        for v in fake.unexpected_violations() + loose.unexpected_violations():
            report.violations.append(f"{capture.name}: {v}")
    finally:
        fake.stop()
        loose.stop()


def judge(name: str, st: Stream, index: int, kind: str, request: bytes, real: bytes, fake: bytes,
          fake_closed: bool, last_burst: bool, half_closed: bool = False) -> Result:
    def res(verdict: str, note: str = "") -> Result:
        return Result(name, st.id, index, kind, verdict, request, real, fake, note)

    if real:
        if fake == real:
            return res("match")
        return res("mismatch", "fake closed the connection" if fake_closed and not fake else "")
    # The real scale said nothing.
    if fake:
        return res("mismatch", "real scale gave no reply, fake did")
    if last_burst and st.closed_by_scale and not fake_closed:
        return res("mismatch", "real scale closed the connection, fake did not")
    if last_burst and not st.closed_by_scale and not fake_closed:
        return res("not_comparable", "capture ends here, so the real scale may simply not have replied yet")
    if last_burst and not st.closed_by_scale and fake_closed:
        return res("mismatch", "fake closed the connection, capture shows no close by the scale")
    return res("match" if fake_closed or not last_burst else "not_comparable")


# ---------- running ----------

def run(path: str | Path | None, tshark: str | None = None) -> Report:
    report = Report()
    if not path:
        report.skipped = "no capture path given"
        return report
    p = Path(path)
    if not p.exists():
        report.skipped = f"capture path {p} does not exist"
        return report
    ts = find_tshark(tshark)
    if ts is None:
        report.skipped = "tshark was not found"
        return report
    files = [p] if p.is_file() else sorted(p.glob("*.pcapng")) + sorted(p.glob("*.pcap"))
    if not files:
        report.skipped = f"no capture files in {p}"
        return report
    for f in files:
        replay_capture(ts, f, report)
    return report


def describe(r: Result) -> str:
    """Where a library mismatch differs, without dumping the replies."""
    a, b = r.real, r.fake
    n = next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b)))
    lo = max(0, n - 8)
    return (
        f"request {r.request[:12]!r}, real {len(a)} bytes, fake {len(b)} bytes, first difference "
        f"at byte {n}: real {a[lo:n + 12]!r} fake {b[lo:n + 12]!r}"
    )


def print_report(report: Report, verbose: bool = False) -> None:
    if report.skipped:
        print(f"SKIPPED: {report.skipped}")
        return
    by_cap: dict[str, list[Result]] = {}
    for r in report.results:
        by_cap.setdefault(r.capture, []).append(r)
    for cap, rs in by_cap.items():
        lib = [r for r in rs if r.is_library]
        m = sum(r.verdict == "match" for r in lib)
        x = sum(r.verdict == "mismatch" for r in lib)
        n = sum(r.verdict == "not_comparable" for r in lib)
        other = len(rs) - len(lib)
        print(f"{cap}: {len(lib)} library messages, {m} match, {x} mismatch, {n} not comparable; "
              f"{other} lab probes not copied")
    for r in report.results:
        if r.is_library and (r.verdict == "mismatch" or (verbose and r.verdict == "not_comparable")):
            print(f"  {r.verdict.upper()} {r.kind} {r.capture} stream {r.stream} message {r.index}: {r.note}")
            print(f"    {describe(r)}")
    probes: dict[str, int] = {}
    for r in report.results:
        if not r.is_library:
            probes[r.verdict] = probes.get(r.verdict, 0) + 1
    if probes:
        print("  Lab probes (E, S, D, L other than LP, junk) are not copied by design: "
              + ", ".join(f"{k} x{v}" for k, v in sorted(probes.items())))
    seen = set()
    for n in report.notes:
        if n not in seen:
            seen.add(n)
            print(f"  note: {n}")
    if report.violations:
        counts: dict[str, int] = {}
        for v in report.violations:
            code = v.split("[", 1)[1].split("]", 1)[0]
            counts[code] = counts.get(code, 0) + 1
        print("  The fake flagged these as outside the library protocol, which is expected for "
              "lab probes: " + ", ".join(f"{k} x{n}" for k, n in sorted(counts.items())))
    print(
        f"TOTAL library messages: {sum(r.is_library for r in report.results)}, "
        f"{report.count('match', True)} match, {report.count('mismatch', True)} mismatch, "
        f"{report.count('not_comparable', True)} not comparable"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("captures", nargs="?", help="a .pcapng file or a folder of them")
    ap.add_argument("--tshark", help="path to tshark")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    report = run(args.captures, args.tshark)
    print_report(report, args.verbose)
    if report.skipped:
        return 0
    return 1 if report.mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
