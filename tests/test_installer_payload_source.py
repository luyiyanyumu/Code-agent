"""Exercise the real C# payload reader with native PE files and 64-bit streams."""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import shutil
import struct
import subprocess
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSC = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
PS = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(os.name != "nt" or not CSC.is_file() or not PS, reason="Windows .NET Framework required")
MAGIC = b"CodeAgentZipV1\0\0"

HARNESS = r'''
using System;
using System.IO;
using System.IO.Compression;
using CodeAgent.Installer;

internal static class PayloadHarness
{
    static void Assert(bool value) { if (!value) throw new Exception("Assertion failed"); }
    static void Rejected(Action check)
    {
        try { check(); } catch (IOException) { return; }
        throw new Exception("Out-of-bounds seek was accepted");
    }
    static void Check(Stream payload)
    {
        Assert(payload != null && payload.CanRead && payload.CanSeek && !payload.CanWrite);
        long length = payload.Length;
        payload.Seek(-2, SeekOrigin.End);
        byte[] tail = new byte[8];
        Assert(payload.Read(tail, 0, 8) == 2 && payload.Read(tail, 0, 8) == 0);
        payload.Position = 0;
        Rejected(delegate { payload.Seek(-1, SeekOrigin.Begin); });
        Rejected(delegate { payload.Seek(1, SeekOrigin.End); });
        Rejected(delegate { payload.Seek(Int64.MaxValue, SeekOrigin.End); });
        Rejected(delegate { payload.Seek(Int64.MinValue, SeekOrigin.Current); });
        try { payload.Write(tail, 0, 1); throw new Exception("Writable payload"); }
        catch (NotSupportedException) { }
        using (ZipArchive archive = new ZipArchive(payload, ZipArchiveMode.Read, true))
        using (StreamReader text = new StreamReader(archive.GetEntry("payload.txt").Open()))
            Assert(text.ReadToEnd() == "hello from portable payload");
        Assert(payload.Length == length);
    }
    static int Main(string[] args)
    {
        try
        {
            if (args[0] == "self")
            {
                using (Stream payload = PayloadSource.Open())
                using (ZipArchive archive = new ZipArchive(payload, ZipArchiveMode.Read))
                using (StreamReader text = new StreamReader(archive.GetEntry("payload.txt").Open()))
                    Assert(text.ReadToEnd() == "hello from portable payload");
                Console.WriteLine("OK self"); return 0;
            }
            if (args[0] == "virtual")
            {
                long offset = Int64.Parse(args[2]);
                using (VirtualFile source = new VirtualFile(offset, File.ReadAllBytes(args[1])))
                {
                    using (Stream payload = PayloadSource.OpenAppended(source, true)) Check(payload);
                    Assert(source.CanRead && source.BytesRead < 20000);
                    Console.WriteLine("OK virtual " + offset + " bytes-read=" + source.BytesRead);
                }
                return 0;
            }
            using (FileStream source = File.OpenRead(args[1]))
            {
                Stream payload = PayloadSource.OpenAppended(source, true);
                if (payload == null) return 3;
                using (payload) Check(payload);
                Assert(source.CanRead);
            }
            Console.WriteLine("OK file"); return 0;
        }
        catch (Exception error) { Console.WriteLine(error.GetType().Name + ": " + error.Message); return 9; }
    }
    // A multi-GiB logical PE prefix requires no allocation or disk space. The
    // reader must seek over it and touch only the small ZIP/footer tail.
    sealed class VirtualFile : Stream
    {
        readonly long start; readonly byte[] tail; long position; bool closed;
        internal long BytesRead;
        internal VirtualFile(long start, byte[] tail) { this.start = start; this.tail = tail; }
        public override bool CanRead { get { return !closed; } }
        public override bool CanSeek { get { return !closed; } }
        public override bool CanWrite { get { return false; } }
        public override long Length { get { return start + tail.LongLength; } }
        public override long Position { get { return position; } set { position = value; } }
        public override int Read(byte[] buffer, int offset, int count)
        {
            int read = (int)Math.Min((long)count, Length - position);
            for (int i = 0; i < read; i++)
            {
                long absolute = position + i;
                buffer[offset + i] = absolute < start ? (byte)0 : tail[(int)(absolute - start)];
            }
            position += read; BytesRead += read; return read;
        }
        public override long Seek(long offset, SeekOrigin origin)
        { return position = (origin == SeekOrigin.Begin ? 0 : origin == SeekOrigin.Current ? position : Length) + offset; }
        public override void Flush() { }
        public override void SetLength(long value) { throw new NotSupportedException(); }
        public override void Write(byte[] value, int offset, int count) { throw new NotSupportedException(); }
        protected override void Dispose(bool disposing) { closed = true; base.Dispose(disposing); }
    }
}
'''


def payload_zip() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("payload.txt", "hello from portable payload")
    return stream.getvalue()


def footer(payload: bytes, offset: int = 64, length: int | None = None) -> bytes:
    return MAGIC + struct.pack("<QQ", offset, len(payload) if length is None else length) + hashlib.sha256(payload).digest()


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    directory = tmp_path_factory.mktemp("payload-source")
    source = directory / "PayloadHarness.cs"
    source.write_text(HARNESS, encoding="utf-8")
    executable = directory / "PayloadHarness.exe"
    arguments = [str(CSC), "/nologo", "/target:exe", "/platform:x64", f"/out:{executable}",
                 "/reference:System.IO.Compression.dll", str(ROOT / "desktop/installer/PayloadSource.cs"), str(source)]
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return directory, executable, arguments


def invoke(executable: Path, *args) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(executable), *map(str, args)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)


def test_bounded_zip_and_three_gib_offset_do_not_read_prefix(harness, tmp_path):
    _, executable, _ = harness
    data = payload_zip()
    blob = tmp_path / "small payload.exe"
    blob.write_bytes(b"MZ" + b"\0" * 62 + data + footer(data))
    result = invoke(executable, "file", blob)
    assert result.returncode == 0, result.stdout + result.stderr
    offset = 3 * 1024**3 + 17
    tail = tmp_path / "virtual file tail.bin"
    tail.write_bytes(data + footer(data, offset))
    result = invoke(executable, "virtual", tail, offset)
    assert result.returncode == 0 and str(offset) in result.stdout, result.stdout + result.stderr


@pytest.mark.parametrize("fault", ["hash", "offset", "length", "huge-offset", "huge-length", "zero-length", "truncated-footer", "truncated-zip", "extra-suffix"])
def test_rejects_corruption_truncation_and_overflow(harness, tmp_path, fault):
    _, executable, _ = harness
    data = payload_zip()
    trailer = bytearray(footer(data))
    if fault == "hash":
        trailer[-1] ^= 1
    if fault == "offset":
        trailer[16:24] = struct.pack("<Q", 65)
    if fault == "length":
        trailer[24:32] = struct.pack("<Q", len(data) + 1)
    if fault == "huge-offset":
        trailer[16:24] = struct.pack("<Q", 2**64 - 1)
    if fault == "huge-length":
        trailer[24:32] = struct.pack("<Q", 2**64 - 1)
    if fault == "zero-length":
        trailer[24:32] = b"\0" * 8
    if fault == "truncated-footer":
        trailer = trailer[:-1]
    if fault == "truncated-zip":
        data = data[:-10]
    blob = tmp_path / "invalid.exe"
    blob.write_bytes(b"MZ" + b"\0" * 62 + data + trailer + (b"extra" if fault == "extra-suffix" else b""))
    result = invoke(executable, "file", blob)
    assert result.returncode in (3, 9), result.stdout + result.stderr


def test_real_native_exe_uses_streamed_overlay_and_embedded_fallback(harness, tmp_path):
    _, _, base_arguments = harness
    archive = tmp_path / "payload.zip"
    archive.write_bytes(payload_zip())
    embedded = tmp_path / "embedded.exe"
    options = [a for a in base_arguments[:-2] if not a.startswith("/out:")]
    arguments = options + [f"/out:{embedded}", f"/resource:{archive},CodeAgent.Payload.zip"] + base_arguments[-2:]
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    result = invoke(embedded, "self")
    assert result.returncode == 0, result.stdout + result.stderr
    appended = tmp_path / "appended.exe"
    arguments = options + [f"/out:{appended}"] + base_arguments[-2:]
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    helpers = (ROOT / "scripts/build-windows-installer.ps1").read_text(encoding="utf-8-sig").split("# Build starts here.", 1)[0]
    driver = tmp_path / "append production payload.ps1"
    driver.write_text(helpers + "\nAdd-AppendedPayload -ExecutablePath $Root -ArchivePath $ReusePayloadArchive | ConvertTo-Json -Compress\n", encoding="utf-8-sig")
    result = subprocess.run([PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(driver),
                             "-PayloadDirectory", str(tmp_path), "-Root", str(appended), "-ReusePayloadArchive", str(archive)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    content = appended.read_bytes()
    assert content[-64:-48] == MAGIC
    offset, length = struct.unpack("<QQ", content[-48:-32])
    assert content[offset:offset + length] == archive.read_bytes()
    assert content[-32:] == hashlib.sha256(archive.read_bytes()).digest()
    result = invoke(appended, "self")
    assert result.returncode == 0, result.stdout + result.stderr
