using System;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;

namespace CodeAgent.Installer
{
    // Large installers keep the ZIP outside PE resources. Only the small PE
    // image is loaded; ZIP reads stay seekable, bounded and 64-bit throughout.
    internal static class PayloadSource
    {
        internal const int FooterSize = 64;
        private static readonly byte[] Magic = Encoding.ASCII.GetBytes("CodeAgentZipV1\0\0");
        private const string InvalidPayload = "安装包已损坏或不完整，请重新下载安装包。";

        internal static Stream Open()
        {
            Assembly assembly = Assembly.GetExecutingAssembly();
            Stream embedded = assembly.GetManifestResourceStream("CodeAgent.Payload.zip");
            if (embedded != null) return embedded;
            FileStream source = new FileStream(assembly.Location, FileMode.Open, FileAccess.Read, FileShare.Read);
            try
            {
                Stream payload = OpenAppended(source, false);
                if (payload == null) throw new InvalidDataException(InvalidPayload);
                return payload;
            }
            catch { source.Dispose(); throw; }
        }

        // The caller retains ownership when no footer is present. On success
        // the returned stream owns source unless leaveOpen was requested.
        internal static Stream OpenAppended(Stream source, bool leaveOpen)
        {
            if (source == null) throw new ArgumentNullException("source");
            if (!source.CanRead || !source.CanSeek) throw new ArgumentException("A readable seekable stream is required.", "source");
            long fileLength = source.Length;
            if (fileLength < FooterSize) return null;
            source.Position = fileLength - FooterSize;
            byte[] footer = new byte[FooterSize];
            ReadExactly(source, footer);
            for (int index = 0; index < Magic.Length; index++)
                if (footer[index] != Magic[index]) return null;

            ulong rawOffset = ReadUInt64(footer, 16), rawLength = ReadUInt64(footer, 24);
            // Exact adjacency rejects overflow, overlap, extra suffix bytes
            // and ZIPs whose central-directory offsets escape the payload.
            if (rawOffset > Int64.MaxValue || rawLength > Int64.MaxValue || rawOffset < FooterSize || rawLength < 22)
                throw new InvalidDataException(InvalidPayload);
            long offset = (long)rawOffset, length = (long)rawLength;
            if (length > fileLength - FooterSize || offset != fileLength - FooterSize - length)
                throw new InvalidDataException(InvalidPayload);

            BoundedPayloadStream window = new BoundedPayloadStream(source, offset, length, leaveOpen);
            try
            {
                byte[] digest;
                using (SHA256 hasher = SHA256.Create()) digest = hasher.ComputeHash(window);
                int difference = 0;
                for (int index = 0; index < digest.Length; index++) difference |= digest[index] ^ footer[32 + index];
                if (difference != 0) throw new InvalidDataException(InvalidPayload);
                window.Position = 0;
                return window;
            }
            catch { window.Dispose(); throw; }
        }

        private static ulong ReadUInt64(byte[] bytes, int offset)
        {
            ulong value = 0;
            for (int index = 7; index >= 0; index--) value = (value << 8) | bytes[offset + index];
            return value;
        }

        private static void ReadExactly(Stream source, byte[] bytes)
        {
            int offset = 0;
            while (offset < bytes.Length)
            {
                int read = source.Read(bytes, offset, bytes.Length - offset);
                if (read <= 0) throw new EndOfStreamException(InvalidPayload);
                offset += read;
            }
        }
    }

    internal sealed class BoundedPayloadStream : Stream
    {
        private readonly Stream source;
        private readonly long start, length;
        private readonly bool leaveOpen;
        private long position;
        private bool disposed;

        internal BoundedPayloadStream(Stream source, long start, long length, bool leaveOpen)
        {
            if (source == null) throw new ArgumentNullException("source");
            if (!source.CanRead || !source.CanSeek || start < 0 || length < 0 || start > source.Length || length > source.Length - start)
                throw new ArgumentException("Invalid payload stream bounds.");
            this.source = source; this.start = start; this.length = length; this.leaveOpen = leaveOpen;
        }

        private void CheckOpen() { if (disposed) throw new ObjectDisposedException("BoundedPayloadStream"); }
        public override bool CanRead { get { return !disposed && source.CanRead; } }
        public override bool CanSeek { get { return !disposed && source.CanSeek; } }
        public override bool CanWrite { get { return false; } }
        public override long Length { get { CheckOpen(); return length; } }
        public override long Position { get { CheckOpen(); return position; } set { Seek(value, SeekOrigin.Begin); } }

        public override int Read(byte[] buffer, int offset, int count)
        {
            CheckOpen();
            if (buffer == null) throw new ArgumentNullException("buffer");
            if (offset < 0 || count < 0 || offset > buffer.Length - count) throw new ArgumentOutOfRangeException();
            int available = (int)Math.Min((long)count, length - position);
            if (available == 0) return 0;
            source.Position = start + position;
            int read = source.Read(buffer, offset, available);
            if (read <= 0) throw new EndOfStreamException("安装包已被截断，请重新下载安装包。");
            position += read;
            return read;
        }

        public override long Seek(long offset, SeekOrigin origin)
        {
            CheckOpen();
            long basis;
            switch (origin)
            {
                case SeekOrigin.Begin: basis = 0; break;
                case SeekOrigin.Current: basis = position; break;
                case SeekOrigin.End: basis = length; break;
                default: throw new ArgumentOutOfRangeException("origin");
            }
            if (offset < -basis || offset > length - basis) throw new IOException("Seek would escape the installer payload.");
            position = basis + offset;
            return position;
        }

        public override void Flush() { CheckOpen(); }
        public override void SetLength(long value) { throw new NotSupportedException(); }
        public override void Write(byte[] buffer, int offset, int count) { throw new NotSupportedException(); }
        protected override void Dispose(bool disposing)
        {
            if (!disposed)
            {
                disposed = true;
                if (disposing && !leaveOpen) source.Dispose();
            }
            base.Dispose(disposing);
        }
    }
}
