// Build-time verification only. This source is not linked into the application
// or installer. Every worker owns its ZIP seek state, SHA state and bounded IO.
using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace CodeAgent.Build
{
    public sealed class PayloadVerificationResult
    {
        public int Files, Workers, MaxConcurrentFiles;
        public long ComparedBytes, PeakWorkingSetBytes;
        public double Seconds;
    }

    public static class PayloadArchiveVerifier
    {
        private sealed class Worker : IDisposable
        {
            internal readonly byte[] Buffer = new byte[1024 * 1024];
            internal FileStream Source;
            internal ZipArchive Archive;
            internal SHA256 Hasher;

            internal Worker(string archivePath)
            {
                try
                {
                    Source = new FileStream(archivePath, FileMode.Open, FileAccess.Read, FileShare.Read,
                                            128 * 1024, FileOptions.RandomAccess);
                    Archive = new ZipArchive(Source, ZipArchiveMode.Read, true, Encoding.UTF8);
                    Hasher = SHA256.Create();
                }
                catch { Dispose(); throw; }
            }

            internal byte[] Digest(Stream stream, long expectedLength, CancellationToken cancellation)
            {
                Hasher.Initialize();
                long readBytes = 0;
                int read;
                while ((read = stream.Read(Buffer, 0, Buffer.Length)) != 0)
                {
                    cancellation.ThrowIfCancellationRequested();
                    readBytes += read;
                    Hasher.TransformBlock(Buffer, 0, read, Buffer, 0);
                }
                if (readBytes != expectedLength) throw new InvalidDataException("Payload stream length changed during verification.");
                Hasher.TransformFinalBlock(Buffer, 0, 0);
                return Hasher.Hash;
            }

            internal void Verify(string name, string path, long size, CancellationToken cancellation)
            {
                cancellation.ThrowIfCancellationRequested();
                ZipArchiveEntry entry = Archive.GetEntry(name);
                if (entry == null) throw new InvalidDataException("Reusable archive membership differs from staged payload: " + name);
                if (entry.Length != size) throw new InvalidDataException("Reusable archive length differs from staging: " + name);
                if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
                    throw new InvalidDataException("Staged payload cannot contain links: " + name);
                byte[] archiveDigest, stagedDigest;
                using (Stream content = entry.Open()) archiveDigest = Digest(content, size, cancellation);
                using (FileStream staged = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read,
                                                         128 * 1024, FileOptions.SequentialScan))
                {
                    if (staged.Length != size) throw new InvalidDataException("Reusable archive length differs from staging: " + name);
                    stagedDigest = Digest(staged, size, cancellation);
                }
                int difference = 0;
                for (int index = 0; index < archiveDigest.Length; index++) difference |= archiveDigest[index] ^ stagedDigest[index];
                if (difference != 0) throw new InvalidDataException("Reusable archive content differs from staging: " + name);
            }

            public void Dispose()
            {
                if (Hasher != null) Hasher.Dispose();
                if (Archive != null) Archive.Dispose();
                if (Source != null) Source.Dispose();
            }
        }

        public static PayloadVerificationResult Verify(string archivePath, string[] names, string[] paths, long[] sizes, int workerCount)
        {
            if (names == null || paths == null || sizes == null || names.Length != paths.Length || names.Length != sizes.Length)
                throw new ArgumentException("Mismatched payload verification arrays.");
            if (workerCount < 1 || workerCount > 4) throw new ArgumentOutOfRangeException("workerCount", "Use one to four bounded workers.");
            workerCount = Math.Min(workerCount, Math.Max(1, names.Length));
            int next = -1, completed = 0, active = 0, peakActive = 0;
            long comparedBytes = 0;
            Exception failure = null;
            object progressLock = new object();
            Stopwatch timer = Stopwatch.StartNew();
            using (CancellationTokenSource cancellation = new CancellationTokenSource())
            using (CountdownEvent ready = new CountdownEvent(workerCount))
            using (ManualResetEventSlim start = new ManualResetEventSlim(false))
            {
                Task[] tasks = new Task[workerCount];
                for (int slot = 0; slot < tasks.Length; slot++)
                {
                    tasks[slot] = Task.Factory.StartNew(delegate
                    {
                        Worker worker = null;
                        bool signalled = false;
                        try
                        {
                            worker = new Worker(archivePath);
                            ready.Signal(); signalled = true;
                            start.Wait(cancellation.Token);
                            while (true)
                            {
                                cancellation.Token.ThrowIfCancellationRequested();
                                int index = Interlocked.Increment(ref next);
                                if (index >= names.Length) break;
                                int now = Interlocked.Increment(ref active);
                                int observed;
                                do { observed = peakActive; }
                                while (now > observed && Interlocked.CompareExchange(ref peakActive, now, observed) != observed);
                                try { worker.Verify(names[index], paths[index], sizes[index], cancellation.Token); }
                                finally { Interlocked.Decrement(ref active); }
                                long bytes = Interlocked.Add(ref comparedBytes, sizes[index] * 2);
                                int count = Interlocked.Increment(ref completed);
                                if (count % 5000 == 0)
                                {
                                    lock (progressLock)
                                        Console.WriteLine("{\"stage\":\"verify-content\",\"files\":" + count + ",\"totalFiles\":" + names.Length +
                                                          ",\"comparedBytes\":" + bytes + ",\"workers\":" + workerCount + "}");
                                }
                            }
                        }
                        catch (OperationCanceledException) { }
                        catch (Exception error)
                        {
                            Interlocked.CompareExchange(ref failure, error, null);
                            cancellation.Cancel();
                        }
                        finally
                        {
                            if (!signalled) ready.Signal();
                            if (worker != null) worker.Dispose();
                        }
                    }, CancellationToken.None, TaskCreationOptions.LongRunning, TaskScheduler.Default);
                }
                ready.Wait();
                start.Set();
                Task.WaitAll(tasks);
            }
            if (failure != null) throw new InvalidDataException(failure.Message, failure);
            if (completed != names.Length) throw new InvalidDataException("Not every payload file completed content verification.");
            return new PayloadVerificationResult { Files = completed, Workers = workerCount, MaxConcurrentFiles = peakActive,
                ComparedBytes = comparedBytes, PeakWorkingSetBytes = Process.GetCurrentProcess().PeakWorkingSet64,
                Seconds = timer.Elapsed.TotalSeconds };
        }
    }
}
