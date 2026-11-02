// Exercises the native close event loop with controlled scripts/RPCs. It never
// starts Code-agent services and only closes its own test forms/listener.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace CodeAgent.DesktopLauncher
{
    internal static class NativeCloseVerification
    {
        private static readonly List<object> rows = new List<object>();
        private static int failures;
        private static string output;
        private static volatile bool finished;
        private static readonly BindingFlags Hidden = BindingFlags.Instance | BindingFlags.NonPublic;

        [STAThread]
        private static int Main(string[] args)
        {
            output = Path.GetFullPath(args[0]); Directory.CreateDirectory(output);
            AppDomain.CurrentDomain.AssemblyResolve += delegate(object sender, ResolveEventArgs request) {
                string name = new AssemblyName(request.Name).Name;
                return name == "Microsoft.Web.WebView2.Core" || name == "Microsoft.Web.WebView2.WinForms" ? Assembly.LoadFrom(Path.Combine(args[1], name + ".dll")) : null;
            };
            Task.Run(delegate { Thread.Sleep(60000); if (!finished) { File.WriteAllText(Path.Combine(output, "watchdog.json"), "{\"passed\":false,\"reason\":\"close fixture watchdog\"}"); Environment.Exit(3); } });
            try
            {
                Task.Run(async delegate {
                    int cancelled = 0; bool timedOut = false;
                    try { await NativeAsyncDeadline.Run(new TaskCompletionSource<int>().Task, 30, "deadline", delegate { cancelled++; }, CancellationToken.None); }
                    catch (TimeoutException) { timedOut = true; }
                    if (!timedOut || cancelled != 1) throw new Exception("Unfinished task did not time out/cancel.");
                    if (await NativeAsyncDeadline.Run(Task.FromResult(7), 100, "deadline", null, CancellationToken.None) != 7) throw new Exception("Normal result changed.");
                }).GetAwaiter().GetResult();
                rows.Add(new { scenario = "bounded-deadline-and-normal-result", passed = true });
                foreach (string scenario in new string[] { "normal", "dirty-cancel-retry", "web-dialog-cancel", "web-dialog-allow", "script-never-completes", "cancel-before-exit", "repeated-close", "rpc-never-completes", "unexpected-error-retry", "actual-http-deadline" }) Run(scenario);
            }
            catch (Exception error) { failures++; rows.Add(new { scenario = "fixture", passed = false, error = error.ToString() }); }
            finished = true;
            File.WriteAllText(Path.Combine(output, "report.json"), new JavaScriptSerializer().Serialize(new { passed = failures == 0, scenarios = rows }), new System.Text.UTF8Encoding(false));
            return failures == 0 ? 0 : 1;
        }

        private static bool Flag(LauncherWindow form, string name) { return (bool)typeof(LauncherWindow).GetField(name, Hidden).GetValue(form); }
        private static void Set(LauncherWindow form, string name, object value) { typeof(LauncherWindow).GetField(name, Hidden).SetValue(form, value); }

        private static void Run(string scenario)
        {
            Stopwatch elapsed = Stopwatch.StartNew();
            int exits = 0, decisions = 0, heartbeats = 0, repeats = 0;
            bool cancelled = false, sawProgress = false, verified = false, webDialogResolved = false;
            Exception error = null;
            TaskCompletionSource<Dictionary<string, object>> release = new TaskCompletionSource<Dictionary<string, object>>();
            using (EventWaitHandle activate = new EventWaitHandle(false, EventResetMode.AutoReset))
            using (LauncherWindow form = new LauncherWindow(output, "close-" + Process.GetCurrentProcess().Id + scenario, activate, null, null, 0, false))
            using (System.Windows.Forms.Timer inspect = new System.Windows.Forms.Timer { Interval = 50 })
            {
                form.StartPosition = FormStartPosition.Manual; form.Location = new System.Drawing.Point(-30000, -30000); form.ShowInTaskbar = false;
                if (scenario == "rpc-never-completes") form.CloseExitTimeoutMilliseconds = 200;
                Set(form, "workbenchReady", true);
                form.CloseScriptOverride = delegate(string script) {
                    if (scenario.StartsWith("web-dialog-", StringComparison.Ordinal)) return Task.FromResult("true");
                    if (scenario == "script-never-completes" || scenario == "cancel-before-exit") return new TaskCompletionSource<string>().Task;
                    return Task.FromResult(scenario == "dirty-cancel-retry" && !script.StartsWith("Boolean", StringComparison.Ordinal) ? "true" : "false");
                };
                form.CloseDecisionOverride = delegate { decisions++; return false; };
                form.CloseExitOverride = delegate { exits++; return scenario == "repeated-close" || scenario == "rpc-never-completes" ? release.Task : Task.FromResult(new Dictionary<string, object>()); };
                if (scenario == "unexpected-error-retry") form.CloseDecisionOverride = delegate { throw new IOException("injected close decision error"); };
                if (scenario == "unexpected-error-retry") form.CloseScriptOverride = delegate { throw new IOException("injected script error"); };
                StatusStrip status = (StatusStrip)typeof(LauncherWindow).GetField("closeStatus", Hidden).GetValue(form);
                ToolStripButton cancel = (ToolStripButton)typeof(LauncherWindow).GetField("cancelClose", Hidden).GetValue(form);
                int initialContentHeight = 0;
                if (scenario.StartsWith("web-dialog-", StringComparison.Ordinal)) form.CloseRequestOverride = delegate(string id) {
                    form.AcknowledgeClosePending("stale-request");
                    if (Flag(form, "closePending")) throw new Exception("A stale pending message changed close state.");
                    form.AcknowledgeClosePending(id);
                };
                inspect.Tick += delegate {
                    heartbeats++; sawProgress |= status.Visible;
                    try
                    {
                        if (elapsed.ElapsedMilliseconds > 22000) throw new TimeoutException("Scenario close did not finish.");
                        if (scenario.StartsWith("web-dialog-", StringComparison.Ordinal) && !webDialogResolved && Flag(form, "closePending"))
                        {
                            if (status.Visible || !form.Enabled || form.Controls[0].Height != initialContentHeight) throw new Exception("Web confirmation gained a duplicate native bar or changed content height.");
                            if (elapsed.ElapsedMilliseconds < 600) return;
                            form.Close(); // A repeated close cannot add a native strip over the web dialog.
                            if (status.Visible) throw new Exception("Repeated close showed a duplicate native bar.");
                            webDialogResolved = true;
                            ((TaskCompletionSource<bool>)typeof(LauncherWindow).GetField("closeReply", Hidden).GetValue(form)).TrySetResult(scenario == "web-dialog-allow");
                            if (scenario == "web-dialog-allow") verified = true;
                            return;
                        }
                        if (scenario == "cancel-before-exit" && !cancelled && cancel.Enabled && status.Visible) { cancelled = true; cancel.PerformClick(); return; }
                        if (scenario == "repeated-close" && Flag(form, "closing"))
                        {
                            if (++repeats <= 3) { form.Close(); if (exits != 1) throw new Exception("Repeated close duplicated exit RPC."); return; }
                            verified = exits == 1 && sawProgress; release.TrySetResult(new Dictionary<string, object>()); return;
                        }
                        if (!Flag(form, "closing") && !Flag(form, "permitClose") && scenario != "normal" && scenario != "actual-http-deadline" && scenario != "repeated-close")
                        {
                            if (scenario == "rpc-never-completes" && (exits != 1 || elapsed.ElapsedMilliseconds < 150)) throw new Exception("Exit RPC did not obey the injected deadline.");
                            if (scenario == "script-never-completes" && (decisions != 1 || exits != 0 || elapsed.ElapsedMilliseconds < 4500)) throw new Exception("Unresponsive JavaScript bypassed dirty protection.");
                            if (scenario == "dirty-cancel-retry" && (decisions != 1 || exits != 0)) throw new Exception("Dirty cancel did not preserve the window.");
                            if (scenario == "web-dialog-cancel" && (!webDialogResolved || decisions != 0 || exits != 0 || status.Visible)) throw new Exception("Web cancel did not preserve the window without a native prompt.");
                            if (scenario == "cancel-before-exit" && (!cancelled || exits != 0)) throw new Exception("Cancel ran a server exit.");
                            verified = true;
                            form.CloseScriptOverride = delegate { return Task.FromResult("false"); };
                            form.CloseRequestOverride = null;
                            form.CloseDecisionOverride = delegate { return true; };
                            form.CloseExitOverride = delegate { exits++; return Task.FromResult(new Dictionary<string, object>()); };
                            form.Close();
                        }
                    }
                    catch (Exception failure) { error = failure; Set(form, "permitClose", true); form.Close(); }
                };
                form.Shown += delegate {
                    form.BeginInvoke((Action)(async delegate {
                        elapsed.Restart(); initialContentHeight = form.Controls[0].Height; inspect.Start();
                        if (scenario == "actual-http-deadline")
                        {
                            TcpListener listener = new TcpListener(IPAddress.Loopback, 0); listener.Start();
                            Task<TcpClient> accept = listener.AcceptTcpClientAsync();
                            try
                            {
                                Set(form, "controller", Process.GetCurrentProcess()); Set(form, "controllerUrl", new Uri("http://127.0.0.1:" + ((IPEndPoint)listener.LocalEndpoint).Port + "/")); Set(form, "nonce", "test-only-nonce");
                                Task<Dictionary<string, object>> rpc = (Task<Dictionary<string, object>>)typeof(LauncherWindow).GetMethod("DesktopRpcAsync", Hidden).Invoke(form, new object[] { "status", null, 150, CancellationToken.None });
                                try { await rpc; throw new Exception("Hanging HTTP unexpectedly completed."); } catch (TimeoutException) { verified = true; }
                            }
                            catch (Exception failure) { error = failure; }
                            finally { if (accept.IsCompleted && accept.Status == TaskStatus.RanToCompletion) accept.Result.Dispose(); listener.Stop(); Set(form, "controller", null); }
                        }
                        form.Close();
                    }));
                };
                Application.Run(form); inspect.Stop();
                if (error == null && (!Flag(form, "permitClose") || Flag(form, "closing") || (scenario != "normal" && !verified))) error = new Exception("Close gate was not released or the scenario was not exercised.");
                if (scenario == "normal" && exits != 1) error = new Exception("Normal close did not perform exactly one exit.");
            }
            if (error != null) failures++;
            rows.Add(new { scenario = scenario, passed = error == null, elapsedMilliseconds = elapsed.ElapsedMilliseconds, exitCalls = exits, decisions = decisions, heartbeats = heartbeats, sawProgress = sawProgress, error = error == null ? null : error.ToString() });
        }
    }
}
