// Runs the real setup event loop with an injected worker. No installation,
// registry access, shortcut creation, external services, or network is performed.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using System.Web.Script.Serialization;

namespace CodeAgent.Installer
{
    internal sealed class AsyncScenarioReport
    {
        public string scenario, error, context;
        public bool passed, timedOut, errorRecovered, retried, lateProgressIgnored;
        public int progressCalls, statusChanges, percentageChanges, progressInvalidations, progressPaints, attempts, heartbeats;
        public long elapsedMilliseconds, workerMilliseconds, maximumHeartbeatGapMilliseconds;
        public string finalTitle, finalStatus, finalButton;
        public bool finalButtonEnabled, finalRunning;
        internal Stopwatch Clock;
    }

    internal static class InstallerAsyncVerification
    {
        private static readonly object reportLock = new object();
        private static readonly List<AsyncScenarioReport> reports = new List<AsyncScenarioReport>();
        private static string reportFile;
        private static volatile bool finished;

        [STAThread]
        private static int Main(string[] args)
        {
            reportFile = Path.GetFullPath(args[0]);
            bool baseline = args.Length > 1 && args[1] == "baseline";
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Control.CheckForIllegalCrossThreadCalls = true;
            // A broken dispatcher may starve WinForms timers. This watchdog
            // exits only this fixture process and saves counters without UI reads.
            Task.Run(delegate {
                Thread.Sleep(40000);
                if (finished) return;
                lock (reportLock) {
                    if (finished) return;
                    if (reports.Count > 0) {
                        AsyncScenarioReport active = reports[reports.Count - 1];
                        active.timedOut = true; active.error = "Fixture event loop exceeded the 40-second bound.";
                        if (active.Clock != null) active.elapsedMilliseconds = active.Clock.ElapsedMilliseconds;
                    }
                    Save();
                }
                Environment.Exit(3);
            });
            foreach (string scenario in baseline ? new string[] { "success-flood" }
                : new string[] { "success-flood", "failure-flood", "retry-after-failure", "success-without-ui-context" })
            {
                AsyncScenarioReport report = Run(scenario);
                if (!report.passed) break;
            }
            lock (reportLock) { finished = true; Save(); }
            return reports.TrueForAll(delegate(AsyncScenarioReport item) { return item.passed; }) ? 0 : 1;
        }

        private static void Save()
        {
            File.WriteAllText(reportFile, new JavaScriptSerializer().Serialize(reports), new System.Text.UTF8Encoding(false));
        }

        private static AsyncScenarioReport Run(string scenario)
        {
            AsyncScenarioReport row = new AsyncScenarioReport { scenario = scenario };
            lock (reportLock) { reports.Add(row); }
            Stopwatch elapsed = new Stopwatch();
            row.Clock = elapsed;
            using (SetupWindow form = new SetupWindow(Path.Combine(Path.GetDirectoryName(reportFile), "never-installed-" + scenario), false, null))
            {
                form.StartPosition = FormStartPosition.Manual;
                form.Location = new System.Drawing.Point(-30000, -30000);
                form.ShowInTaskbar = false;
                Control title = form.Controls.Find("setup-title", true)[0];
                Control status = form.Controls.Find("install-status", true)[0];
                Control percentage = FindPercentage(form);
                Control progress = form.Controls.Find("install-progress", true)[0];
                Button button = (Button)form.Controls.Find("start-install", true)[0];
                Button cancel = (Button)form.Controls.Find("cancel-install", true)[0];
                System.Reflection.FieldInfo running = typeof(SetupWindow).GetField("running", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
                status.TextChanged += delegate { row.statusChanges++; };
                if (percentage != null) percentage.TextChanged += delegate { row.percentageChanges++; };
                progress.Invalidated += delegate { row.progressInvalidations++; };
                progress.Paint += delegate { row.progressPaints++; };
                Action<int, string> lastProgress = null;
                form.InstallOperation = delegate(string target, Action<int, string> update) {
                    int attempt = Interlocked.Increment(ref row.attempts);
                    lastProgress = update;
                    Stopwatch worker = Stopwatch.StartNew();
                    try {
                        int count = scenario == "retry-after-failure" && attempt == 1 ? 1200 : 18000;
                        for (int index = 0; index < count; index++) {
                            Interlocked.Increment(ref row.progressCalls);
                            update(index * 94 / count, "正在安装 runtimes/python/Lib/site-packages/fixture/" + index + "/api.py");
                            if (index % 600 == 0) Thread.Sleep(8);
                        }
                        if (scenario == "failure-flood" || (scenario == "retry-after-failure" && attempt == 1))
                            throw new IOException("异步安装验证注入故障");
                        Interlocked.Increment(ref row.progressCalls); update(100, "安装完成");
                        return new InstallResult { Root = target, Version = "0.10.2", Files = 5747 };
                    }
                    finally { Interlocked.Exchange(ref row.workerMilliseconds, worker.ElapsedMilliseconds); }
                };
                System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer { Interval = 25 };
                long lastHeartbeat = 0, successAt = -1;
                string successStatus = null;
                bool closed = false;
                Action close = delegate {
                    if (closed) return;
                    closed = true; timer.Stop();
                    row.elapsedMilliseconds = elapsed.ElapsedMilliseconds;
                    row.finalTitle = title.Text; row.finalStatus = status.Text;
                    row.finalButton = button.Text; row.finalButtonEnabled = button.Enabled;
                    row.finalRunning = (bool)running.GetValue(form);
                    // Only this fixture's own form is closed, even on failure.
                    running.SetValue(form, false); form.Close();
                };
                timer.Tick += delegate {
                    try {
                        long now = elapsed.ElapsedMilliseconds;
                        row.heartbeats++;
                        row.maximumHeartbeatGapMilliseconds = Math.Max(row.maximumHeartbeatGapMilliseconds, now - lastHeartbeat);
                        lastHeartbeat = now;
                        bool busy = (bool)running.GetValue(form);
                        if (!busy && button.Enabled && button.Text == "重试安装") {
                            if (!status.Text.Contains("异步安装验证注入故障")) throw new InvalidOperationException("Failure details were lost.");
                            if (!cancel.Enabled) throw new InvalidOperationException("Cancel button was not restored after failure.");
                            row.errorRecovered = true;
                            if (scenario == "retry-after-failure" && !row.retried) {
                                row.retried = true; button.PerformClick(); return;
                            }
                            if (scenario != "failure-flood") throw new InvalidOperationException("Unexpected install failure.");
                            row.passed = true; close(); return;
                        }
                        if (!busy && button.Enabled && title.Text == "安装完成") {
                            if (button.Text != "完成" || !cancel.Enabled) throw new InvalidOperationException("Success controls were not restored.");
                            if (successAt < 0) {
                                successAt = now; successStatus = status.Text;
                                Task.Run(delegate { Thread.Sleep(60); lastProgress(37, "LATE-PROGRESS-MUST-BE-IGNORED"); });
                            }
                            if (now - successAt > 350) {
                                row.lateProgressIgnored = status.Text == successStatus && !progress.Visible;
                                if (!row.lateProgressIgnored) throw new InvalidOperationException("Late progress overwrote final success.");
                                if (scenario == "retry-after-failure" && (!row.errorRecovered || row.attempts != 2))
                                    throw new InvalidOperationException("Retry did not execute a new successful operation.");
                                row.passed = true; close(); return;
                            }
                        }
                        if (now > 12000) throw new TimeoutException("UI did not finish within 12 seconds.");
                    }
                    catch (Exception error) { row.error = error.ToString(); close(); }
                };
                form.Shown += delegate {
                    form.BeginInvoke((Action)delegate {
                        row.context = SynchronizationContext.Current == null ? null : SynchronizationContext.Current.GetType().FullName;
                        elapsed.Start(); timer.Start();
                        SynchronizationContext original = SynchronizationContext.Current;
                        try {
                            if (scenario == "success-without-ui-context") SynchronizationContext.SetSynchronizationContext(null);
                            button.PerformClick();
                        }
                        finally { SynchronizationContext.SetSynchronizationContext(original); }
                    });
                };
                Application.Run(form);
                timer.Dispose();
            }
            lock (reportLock) { Save(); }
            return row;
        }

        private static Control FindPercentage(Control root)
        {
            foreach (Control child in root.Controls) {
                if (child is Label && child.Text == "0%") return child;
                Control found = FindPercentage(child); if (found != null) return found;
            }
            return null;
        }
    }
}
