// Build with the Windows .NET Framework compiler; keep this source C# 5 compatible.
// The WebView2 SDK assemblies and loader are embedded in the one-file launcher.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace CodeAgent.DesktopLauncher
{
    internal static class Bootstrap
    {
        [STAThread]
        private static int Main(string[] args)
        {
            AppDomain.CurrentDomain.AssemblyResolve += ResolveEmbeddedAssembly;
            try
            {
                string root = Path.GetFullPath(Argument(args, "--root", AppDomain.CurrentDomain.BaseDirectory)).TrimEnd(Path.DirectorySeparatorChar);
                if (!File.Exists(Path.Combine(root, "bin", "code-agent-desktop-service.mjs")))
                    throw new InvalidOperationException("未找到 Code-agent 程序文件。请重新安装应用后重试。");
                string diagnosticsFile = Argument(args, "--diagnostics-file", null);
                string diagnosticExit = Argument(args, "--diagnostics-exit-after-ms", null);
                int diagnosticExitMs = 0;
                if (diagnosticExit != null && (String.IsNullOrEmpty(diagnosticsFile) || !Int32.TryParse(diagnosticExit, out diagnosticExitMs)
                    || diagnosticExitMs < 1000 || diagnosticExitMs > 120000))
                    throw new ArgumentException("自动退出诊断仅可在提供诊断文件时使用，时间须为 1000 到 120000 毫秒。");
                string key = Hash(Encoding.UTF8.GetBytes(root.ToLowerInvariant())).Substring(0, 32);
                bool created;
                using (Mutex mutex = new Mutex(false, "Local\\CodeAgentDesktopLauncher-" + key))
                using (EventWaitHandle activate = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\CodeAgentDesktopLauncherActivate-" + key, out created))
                {
                    bool owns = false;
                    try { owns = mutex.WaitOne(0); }
                    catch (AbandonedMutexException) { owns = true; }
                    if (!owns) { activate.Set(); return 0; }
                    try
                    {
                        string cache = ExtractNativeLoader();
                        return NativeApplication.Run(root, key, cache, activate,
                            diagnosticsFile, Argument(args, "--diagnostics-port", null), diagnosticExitMs);
                    }
                    finally { mutex.ReleaseMutex(); }
                }
            }
            catch (Exception error)
            {
                MessageBox.Show("Code-agent 未能打开：\r\n" + error.Message, "Code-agent", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }
        }

        private static string Argument(string[] args, string name, string fallback)
        {
            for (int index = 0; index < args.Length; index++)
                if (args[index] == name)
                {
                    if (index + 1 >= args.Length || args[index + 1].StartsWith("--", StringComparison.Ordinal))
                        throw new ArgumentException(name + " 缺少参数。");
                    return args[index + 1];
                }
            return fallback;
        }

        private static Assembly ResolveEmbeddedAssembly(object sender, ResolveEventArgs args)
        {
            string name = new AssemblyName(args.Name).Name;
            string resource = name == "Microsoft.Web.WebView2.Core" ? "CodeAgent.WebView2.Core.dll"
                : name == "Microsoft.Web.WebView2.WinForms" ? "CodeAgent.WebView2.WinForms.dll" : null;
            if (resource == null) return null;
            return Assembly.Load(Resource(resource));
        }

        internal static byte[] Resource(string name)
        {
            using (Stream source = Assembly.GetExecutingAssembly().GetManifestResourceStream(name))
            {
                if (source == null) throw new InvalidOperationException("Code-agent 缺少内置组件：" + name);
                using (MemoryStream destination = new MemoryStream()) { source.CopyTo(destination); return destination.ToArray(); }
            }
        }

        internal static string Hash(byte[] bytes)
        {
            using (SHA256 hash = SHA256.Create()) return BitConverter.ToString(hash.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant();
        }

        private static string ExtractNativeLoader()
        {
            byte[] bytes = Resource("CodeAgent.WebView2Loader.dll");
            string digest = Hash(bytes);
            string directory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Code-agent", "Launcher", "WebView2", digest);
            Directory.CreateDirectory(directory);
            string path = Path.Combine(directory, "WebView2Loader.dll");
            if (File.Exists(path))
            {
                if (Hash(File.ReadAllBytes(path)) != digest)
                    throw new InvalidOperationException("本地运行组件校验失败，请删除后重新打开：" + directory);
            }
            else
            {
                string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
                File.WriteAllBytes(temporary, bytes);
                try { File.Move(temporary, path); }
                catch (IOException)
                {
                    if (!File.Exists(path) || Hash(File.ReadAllBytes(path)) != digest) throw;
                }
                finally { if (File.Exists(temporary)) File.Delete(temporary); }
            }
            return directory;
        }
    }

    internal static class NativeApplication
    {
        [DllImport("user32.dll")]
        private static extern bool SetProcessDPIAware();

        // A separate non-inlined method prevents the CLR from resolving SDK types
        // before Bootstrap has installed the embedded assembly resolver.
        [MethodImpl(MethodImplOptions.NoInlining)]
        internal static int Run(string root, string key, string loaderDirectory, EventWaitHandle activate, string diagnosticsFile, string diagnosticsPort, int diagnosticsExitMs)
        {
            CoreWebView2Environment.SetLoaderDllFolderPath(loaderDirectory);
            try { SetProcessDPIAware(); } catch (EntryPointNotFoundException) { }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new LauncherWindow(root, key, activate, diagnosticsFile, diagnosticsPort, diagnosticsExitMs));
            return 0;
        }
    }

    internal sealed class LauncherWindow : Form
    {
        private readonly string root;
        private readonly string key;
        private readonly string runtimeDirectory;
        private readonly string diagnosticsFile;
        private readonly string diagnosticsPort;
        private readonly int diagnosticsExitMs;
        private readonly string bundledNode;
        private readonly string bundledWebView;
        private readonly EventWaitHandle activate;
        private readonly Label loading;
        private readonly Button retry;
        private readonly Button logs;
        private readonly Panel placeholder;
        private readonly System.Windows.Forms.Timer activationTimer;
        private System.Windows.Forms.Timer diagnosticsExitTimer;
        private readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 4 * 1024 * 1024 };
        private readonly List<Uri> serviceOrigins = new List<Uri>();
        private readonly List<WorkspaceWindow> workspaces = new List<WorkspaceWindow>();
        private Process controller;
        private DateTime controllerCreated;
        private string handshakePath;
        private string nonce;
        private Uri controllerUrl;
        private Uri workspaceUrl;
        private WebView2 webView;
        private CoreWebView2Environment environment;
        private bool initializing;
        private bool closing;
        private bool permitClose;
        private bool workbenchReady;
        private bool integratedFrame;
        private bool nativeDragRegions;
        private bool clientChromeReady;
        private bool convertingRestoredClientBounds;
        private int nativeSizeState;
        private TaskCompletionSource<bool> closeReply;
        private string closeRequestId;
        private bool closePending;
        private readonly StatusStrip closeStatus;
        private readonly ToolStripStatusLabel closeStatusText;
        private readonly ToolStripButton cancelClose;
        private CancellationTokenSource closeCancellation;
        private bool exitStarted;
        private bool? closeStartedEnabled;
        internal Func<string, Task<string>> CloseScriptOverride = null;
        internal Action<string> CloseRequestOverride = null;
        internal Func<Task<Dictionary<string, object>>> CloseExitOverride = null;
        internal Func<string, string, bool> CloseDecisionOverride = null;
        internal int CloseExitTimeoutMilliseconds = 90000;

        [DllImport("user32.dll")]
        private static extern bool SetForegroundWindow(IntPtr handle);
        [DllImport("user32.dll")]
        private static extern bool IsWindowEnabled(IntPtr handle);
        [DllImport("user32.dll")]
        private static extern bool IsZoomed(IntPtr handle);
        [DllImport("user32.dll")]
        private static extern uint GetDpiForWindow(IntPtr handle);
        [DllImport("user32.dll")]
        private static extern bool ReleaseCapture();
        [DllImport("user32.dll")]
        private static extern IntPtr SendMessage(IntPtr handle, int message, IntPtr wParam, IntPtr lParam);
        [DllImport("user32.dll")]
        private static extern bool SetWindowPos(IntPtr handle, IntPtr after, int x, int y, int width, int height, uint flags);

        [StructLayout(LayoutKind.Sequential)]
        private struct NativeRect { public int Left, Top, Right, Bottom; }
        [StructLayout(LayoutKind.Sequential)]
        private struct NativeNcCalc { public NativeRect Current, Previous, Client; public IntPtr Position; }
        [StructLayout(LayoutKind.Sequential)]
        private struct NativePoint { public int X, Y; }
        [StructLayout(LayoutKind.Sequential)]
        private struct NativeMinMax { public NativePoint Reserved, MaximumSize, MaximumPosition, MinimumTrackSize, MaximumTrackSize; }

        internal LauncherWindow(string root, string key, EventWaitHandle activate, string diagnosticsFile, string diagnosticsPort, int diagnosticsExitMs, bool initializeOnShow = true)
        {
            this.root = root;
            this.key = key;
            this.activate = activate;
            this.diagnosticsFile = diagnosticsFile;
            this.diagnosticsPort = diagnosticsPort;
            this.diagnosticsExitMs = diagnosticsExitMs;
            string bundleManifest = Path.Combine(root, "desktop-runtime.json");
            if (File.Exists(bundleManifest))
            {
                Dictionary<string, object> bundle = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(bundleManifest, Encoding.UTF8));
                object schema, node, browser;
                if (!bundle.TryGetValue("schemaVersion", out schema) || Convert.ToInt32(schema) != 1
                    || !bundle.TryGetValue("node", out node) || !bundle.TryGetValue("webview2", out browser))
                    throw new InvalidOperationException("安装包运行环境清单无效，请重新安装。");
                bundledNode = ResolveBundledPath(root, Convert.ToString(node), false);
                bundledWebView = ResolveBundledPath(root, Convert.ToString(browser), true);
                if (!File.Exists(Path.Combine(bundledWebView, "msedgewebview2.exe")))
                    throw new InvalidOperationException("安装包缺少内置界面运行环境，请重新安装。");
            }
            runtimeDirectory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Code-agent", "Launcher", key);
            Directory.CreateDirectory(runtimeDirectory);
            Text = "Code-agent";
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(1380, 880);
            MinimumSize = new Size(960, 640);
            AutoScaleDimensions = new SizeF(96F, 96F);
            AutoScaleMode = AutoScaleMode.Dpi;
            Font = new Font("Microsoft YaHei UI", 10);
            BackColor = Color.FromArgb(245, 247, 251);
            // Keep the sizeable/system-menu styles. Only the duplicate caption
            // is removed once the real workbench has its own window controls.
            FormBorderStyle = FormBorderStyle.Sizable;
            try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }

            placeholder = new Panel { Dock = DockStyle.Fill, BackColor = BackColor };
            loading = new Label { Text = "Code-agent\r\n\r\n正在准备工作台…", AutoSize = false, TextAlign = ContentAlignment.MiddleCenter, Dock = DockStyle.Fill, ForeColor = Color.FromArgb(45, 57, 79) };
            FlowLayoutPanel actions = new FlowLayoutPanel { Dock = DockStyle.Bottom, Height = 84, FlowDirection = FlowDirection.LeftToRight, Padding = new Padding(28, 12, 0, 0) };
            retry = new Button { Text = "重试", Width = 100, Height = 36, Visible = false };
            logs = new Button { Text = "打开日志目录", Width = 150, Height = 36, Visible = false };
            actions.Controls.Add(retry);
            actions.Controls.Add(logs);
            placeholder.Controls.Add(loading);
            placeholder.Controls.Add(actions);
            Controls.Add(placeholder);
            closeStatusText = new ToolStripStatusLabel { Spring = true, TextAlign = ContentAlignment.MiddleLeft };
            cancelClose = new ToolStripButton("取消关闭");
            closeStatus = new StatusStrip { Visible = false, SizingGrip = false, BackColor = Color.FromArgb(235, 241, 255), ForeColor = Color.FromArgb(42, 65, 112), AccessibleName = "退出状态" };
            closeStatus.Items.Add(closeStatusText); closeStatus.Items.Add(cancelClose); Controls.Add(closeStatus);
            cancelClose.Click += delegate { if (closing && !exitStarted && closeCancellation != null) closeCancellation.Cancel(); else if (!closing) closeStatus.Visible = false; };
            retry.Click += async delegate { await InitializeAsync(); };
            logs.Click += delegate { OpenLogDirectory(); };
            Shown += async delegate
            {
                Rectangle area = Screen.FromControl(this).WorkingArea;
                MinimumSize = new Size(Math.Min(MinimumSize.Width, area.Width - 16), Math.Min(MinimumSize.Height, area.Height - 16));
                if (Width > area.Width - 24 || Height > area.Height - 24)
                {
                    Size = new Size(Math.Min(Width, area.Width - 24), Math.Min(Height, area.Height - 24));
                    Location = new Point(area.Left + (area.Width - Width) / 2, area.Top + (area.Height - Height) / 2);
                }
                if (initializeOnShow) await InitializeAsync();
            };
            FormClosing += OnClosingAsync;
            Resize += delegate { PublishWindowState(); };
            LocationChanged += delegate { if (workbenchReady) WriteDiagnostics("ready", null); };
            Activated += delegate { PublishWindowState(); };
            Deactivate += delegate { PublishWindowState(); };
            activationTimer = new System.Windows.Forms.Timer { Interval = 300 };
            activationTimer.Tick += delegate
            {
                if (!activate.WaitOne(0)) return;
                if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
                Show(); Activate(); SetForegroundWindow(Handle);
            };
            activationTimer.Start();
            WriteDiagnostics("opening", null);
        }

        internal Dictionary<string, object> WindowChromeState()
        {
            Rectangle work = Screen.FromControl(this).WorkingArea;
            return new Dictionary<string, object> {
                { "integratedTitlebar", nativeDragRegions }, { "nativeDragRegions", nativeDragRegions },
                { "maximized", WindowState == FormWindowState.Maximized },
                { "minimized", WindowState == FormWindowState.Minimized },
                { "active", Form.ActiveForm == this }, { "integratedFrame", integratedFrame },
                { "bounds", RectangleState(Bounds) }, { "clientBounds", RectangleState(RectangleToScreen(ClientRectangle)) }, { "clientSize", new Dictionary<string, object> { { "width", ClientSize.Width }, { "height", ClientSize.Height } } },
                { "workingArea", RectangleState(work) }
            };
        }

        private static Dictionary<string, object> RectangleState(Rectangle rectangle)
        {
            return new Dictionary<string, object> { { "x", rectangle.X }, { "y", rectangle.Y }, { "width", rectangle.Width }, { "height", rectangle.Height } };
        }

        protected override CreateParams CreateParams
        {
            get
            {
                CreateParams parameters = base.CreateParams;
                // Keep THICKFRAME, SYSMENU and MIN/MAX boxes. Removing CAPTION
                // here also informs WinForms' own SizeFromClientSize/restore
                // calculations, so no queued size fix can interrupt a native
                // drag loop or overwrite Windows' cursor-anchored position.
                if (integratedFrame) parameters.Style &= ~0x00C00000;
                return parameters;
            }
        }

        public new FormWindowState WindowState
        {
            get { return base.WindowState; }
            set
            {
                bool previous = convertingRestoredClientBounds;
                if (integratedFrame && value == FormWindowState.Normal && base.WindowState != FormWindowState.Normal) convertingRestoredClientBounds = true;
                try { base.WindowState = value; }
                finally { convertingRestoredClientBounds = previous; }
            }
        }

        protected override void SetBoundsCore(int x, int y, int width, int height, BoundsSpecified specified)
        {
            // Form's private restore converter adds a symmetric native frame to
            // its saved client height. Match our top-only client extension in
            // that same synchronous conversion, without repairing location or
            // posting a later bounds change into the native move loop.
            if (integratedFrame && convertingRestoredClientBounds && (specified & BoundsSpecified.Height) != 0 && width == Width)
            {
                int topInset = base.SizeFromClientSize(Size.Empty).Height / 2;
                if (topInset > 0 && height == Height + topInset) height -= topInset;
            }
            base.SetBoundsCore(x, y, width, height, specified);
        }

        internal Dictionary<string, object> WindowChromeAction(string method)
        {
            if (method == "window-state")
            {
                clientChromeReady = true;
                if (workbenchReady) SetIntegratedFrame(true);
            }
            if (method == "window-minimize") WindowState = FormWindowState.Minimized;
            else if (method == "window-toggle-maximize")
                WindowState = WindowState == FormWindowState.Maximized ? FormWindowState.Normal : FormWindowState.Maximized;
            else if (method == "window-close")
            {
                // The normal FormClosing path flushes/checks editor drafts and
                // retains the existing cancellation and service-exit behavior.
                BeginInvoke((Action)delegate { Close(); });
            }
            else if (method == "window-drag")
            {
                ReleaseCapture();
                SendMessage(Handle, 0x00A1, new IntPtr(2), IntPtr.Zero); // WM_NCLBUTTONDOWN / HTCAPTION
            }
            else if (method != "window-state") throw new InvalidOperationException("不支持的窗口操作。");
            PublishWindowState();
            return WindowChromeState();
        }

        private void PublishWindowState()
        {
            if (closing || IsDisposed || webView == null || webView.CoreWebView2 == null) return;
            try { webView.CoreWebView2.PostWebMessageAsJson(json.Serialize(new Dictionary<string, object> {
                { "channel", "code-agent-desktop" }, { "event", "window-state" }, { "state", WindowChromeState() }
            })); }
            catch (InvalidOperationException) { }
            if (workbenchReady) WriteDiagnostics("ready", null);
        }

        internal void SetIntegratedFrame(bool enabled)
        {
            enabled = enabled && nativeDragRegions;
            if (integratedFrame == enabled) return;
            integratedFrame = enabled;
            if (IsHandleCreated) UpdateStyles();
            if (IsHandleCreated) SetWindowPos(Handle, IntPtr.Zero, 0, 0, 0, 0, 0x0037); // FRAMECHANGED/NOMOVE/NOSIZE/NOZORDER/NOACTIVATE
            PublishWindowState();
        }

        protected override void WndProc(ref Message message)
        {
            if (integratedFrame && message.Msg == 0x0084 && !IsZoomed(Handle)) // WM_NCHITTEST
            {
                long packed = message.LParam.ToInt64();
                Point pointer = new Point(unchecked((short)(packed & 0xffff)), unchecked((short)((packed >> 16) & 0xffff)));
                int hit = IntegratedTopResizeHit(Bounds, pointer, NativeWindowDpi());
                if (hit != 0) { message.Result = new IntPtr(hit); return; }
            }
            if (integratedFrame && message.Msg == 0x0083 && message.LParam != IntPtr.Zero) // WM_NCCALCSIZE
            {
                bool full = message.WParam != IntPtr.Zero;
                NativeRect before = full ? ((NativeNcCalc)Marshal.PtrToStructure(message.LParam, typeof(NativeNcCalc))).Current
                    : (NativeRect)Marshal.PtrToStructure(message.LParam, typeof(NativeRect));
                base.WndProc(ref message);
                NativeRect client = full ? ((NativeNcCalc)Marshal.PtrToStructure(message.LParam, typeof(NativeNcCalc))).Current
                    : (NativeRect)Marshal.PtrToStructure(message.LParam, typeof(NativeRect));
                // The restored THICKFRAME otherwise paints a blank sizing band
                // above the web titlebar. Extend only its top; native side and
                // bottom borders and the system's restore geometry stay intact.
                if (IsZoomed(Handle)) client = before;
                else client.Top = before.Top;
                if (full)
                {
                    NativeNcCalc calculated = (NativeNcCalc)Marshal.PtrToStructure(message.LParam, typeof(NativeNcCalc));
                    calculated.Current = client; Marshal.StructureToPtr(calculated, message.LParam, false);
                }
                else Marshal.StructureToPtr(client, message.LParam, false);
                message.Result = IntPtr.Zero;
                return;
            }
            bool previousConversion = convertingRestoredClientBounds;
            if (integratedFrame && message.Msg == 0x0112) // WM_SYSCOMMAND
            {
                long command = message.WParam.ToInt64() & 0xfff0;
                if (command == 0xf120 || command == 0xf010 && IsZoomed(Handle)) convertingRestoredClientBounds = true; // SC_RESTORE / maximized SC_MOVE
            }
            if (message.Msg == 0x0005) // WM_SIZE
            {
                int nextState = message.WParam.ToInt32();
                if (integratedFrame && nextState == 0 && nativeSizeState != 0) convertingRestoredClientBounds = true;
                nativeSizeState = nextState;
            }
            try { base.WndProc(ref message); }
            finally { convertingRestoredClientBounds = previousConversion; }
            if (integratedFrame && message.Msg == 0x0024 && message.LParam != IntPtr.Zero) // WM_GETMINMAXINFO
            {
                NativeMinMax limits = (NativeMinMax)Marshal.PtrToStructure(message.LParam, typeof(NativeMinMax));
                Screen screen = Screen.FromHandle(Handle);
                Rectangle work = screen.WorkingArea, monitor = screen.Bounds;
                limits.MaximumPosition.X = work.Left - monitor.Left;
                limits.MaximumPosition.Y = work.Top - monitor.Top;
                limits.MaximumSize.X = work.Width;
                limits.MaximumSize.Y = work.Height;
                Marshal.StructureToPtr(limits, message.LParam, false);
            }
        }

        internal static int IntegratedTopResizeHit(Rectangle bounds, Point pointer, int dpi)
        {
            int grip = Math.Max(1, (int)Math.Ceiling(8.0 * Math.Max(96, dpi) / 96.0));
            if (pointer.Y < bounds.Top || pointer.Y >= bounds.Top + grip || pointer.X < bounds.Left || pointer.X >= bounds.Right) return 0;
            if (pointer.X < bounds.Left + grip) return 13; // HTTOPLEFT
            if (pointer.X >= bounds.Right - grip) return 14; // HTTOPRIGHT
            return 12; // HTTOP
        }

        private int NativeWindowDpi()
        {
            try { uint dpi = GetDpiForWindow(Handle); if (dpi > 0) return (int)dpi; }
            catch (EntryPointNotFoundException) { }
            return DeviceDpi;
        }

        private async Task InitializeAsync()
        {
            if (initializing || closing) return;
            initializing = true;
            retry.Visible = false;
            logs.Visible = false;
            placeholder.Visible = true;
            workbenchReady = false;
            clientChromeReady = false;
            SetIntegratedFrame(false);
            loading.Text = "Code-agent\r\n\r\n正在启动应用服务…";
            try
            {
                if (controller != null && !controller.HasExited && controllerUrl != null)
                    try { await DesktopRpcAsync("exit", null); } catch (Exception) { }
                StopFailedController();
                if (webView != null) { Controls.Remove(webView); webView.Dispose(); webView = null; }
                if (bundledWebView == null)
                {
                    string runtimeVersion;
                    try { runtimeVersion = CoreWebView2Environment.GetAvailableBrowserVersionString(); }
                    catch (WebView2RuntimeNotFoundException) { throw new InvalidOperationException("未找到 Microsoft Edge WebView2 运行组件。请安装 WebView2 Runtime 后点击重试。"); }
                    if (String.IsNullOrEmpty(runtimeVersion)) throw new InvalidOperationException("未找到 Microsoft Edge WebView2 运行组件。请安装 WebView2 Runtime 后点击重试。");
                }

                nonce = Guid.NewGuid().ToString("N");
                handshakePath = Path.Combine(runtimeDirectory, "handshake-" + nonce + ".json");
                ProcessStartInfo info = new ProcessStartInfo
                {
                    FileName = bundledNode ?? ResolveNode(),
                    Arguments = Quote(Path.Combine(root, "bin", "code-agent-desktop-service.mjs")) + " --root " + Quote(root)
                        + " --handshake-file " + Quote(handshakePath) + " --parent-pid " + Process.GetCurrentProcess().Id
                        + " --nonce " + nonce,
                    WorkingDirectory = root,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WindowStyle = ProcessWindowStyle.Hidden,
                    RedirectStandardInput = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    StandardOutputEncoding = Encoding.UTF8,
                    StandardErrorEncoding = Encoding.UTF8
                };
                if (bundledNode != null)
                {
                    info.EnvironmentVariables["CODE_AGENT_NODE"] = bundledNode;
                    if (String.IsNullOrEmpty(info.EnvironmentVariables["CODE_AGENT_HOME"]))
                        info.EnvironmentVariables["CODE_AGENT_HOME"] = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Code-agent", "data");
                }
                controller = new Process { StartInfo = info, EnableRaisingEvents = true };
                controller.OutputDataReceived += OnControllerOutput;
                controller.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs args) { if (args.Data != null) AppendHostLog(args.Data); };
                if (!controller.Start()) throw new InvalidOperationException("无法启动本地服务控制程序。");
                controllerCreated = controller.StartTime;
                controller.BeginOutputReadLine();
                controller.BeginErrorReadLine();
                Dictionary<string, object> handshake = await Task.Run(async delegate
                {
                    DateTime deadline = DateTime.UtcNow.AddSeconds(30);
                    while (DateTime.UtcNow < deadline)
                    {
                        if (controller.HasExited) throw new InvalidOperationException("本地服务控制程序已退出，请查看日志后重试。");
                        if (File.Exists(handshakePath))
                        {
                            string content = File.ReadAllText(handshakePath, Encoding.UTF8);
                            if (content.Length > 65536) throw new InvalidOperationException("应用服务握手数据无效。");
                            return new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(content);
                        }
                        await Task.Delay(100);
                    }
                    throw new TimeoutException("应用服务准备超时，请查看日志后重试。");
                });
                ValidateHandshake(handshake);
                if (File.Exists(handshakePath)) File.Delete(handshakePath);
                CoreWebView2EnvironmentOptions options = new CoreWebView2EnvironmentOptions();
                if (!String.IsNullOrEmpty(diagnosticsPort))
                {
                    int port;
                    if (!Int32.TryParse(diagnosticsPort, out port) || port < 1024 || port > 65535) throw new ArgumentException("诊断端口无效。");
                    options.AdditionalBrowserArguments = "--remote-debugging-address=127.0.0.1 --remote-debugging-port=" + port;
                }
                environment = await CoreWebView2Environment.CreateAsync(bundledWebView, Path.Combine(runtimeDirectory, "WebView2Profile"), options);
                webView = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = BackColor };
                Controls.Add(webView);
                placeholder.BringToFront();
                await webView.EnsureCoreWebView2Async(environment);
                ConfigureWebView(webView, true);
                await webView.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync("window.__CODE_AGENT_DESKTOP__={integrated:true,integratedTitlebar:" + (nativeDragRegions ? "true" : "false") + ",nativeDragRegions:" + (nativeDragRegions ? "true" : "false") + "};document.addEventListener('DOMContentLoaded',function(){document.documentElement.dataset.desktop='true';});");
                webView.CoreWebView2.NavigationCompleted += delegate(object sender, CoreWebView2NavigationCompletedEventArgs args)
                {
                    if (args.IsSuccess)
                    {
                        Uri current;
                        if (!Uri.TryCreate(webView.CoreWebView2.Source, UriKind.Absolute, out current) || !SameOrigin(current, workspaceUrl)) return;
                        workbenchReady = true;
                        placeholder.Visible = false;
                        webView.BringToFront();
                        if (clientChromeReady) SetIntegratedFrame(true);
                        WriteDiagnostics("ready", null);
                        if (diagnosticsExitMs > 0 && diagnosticsExitTimer == null)
                        {
                            diagnosticsExitTimer = new System.Windows.Forms.Timer { Interval = diagnosticsExitMs };
                            diagnosticsExitTimer.Tick += delegate { diagnosticsExitTimer.Stop(); Close(); };
                            diagnosticsExitTimer.Start();
                        }
                    }
                    else ShowFailure("工作台加载失败：" + args.WebErrorStatus);
                };
                await WaitForWorkspaceAsync();
                if (!closing && !IsDisposed) webView.CoreWebView2.Navigate(workspaceUrl.AbsoluteUri);
            }
            catch (Exception error) { ShowFailure(error.Message); }
            finally { initializing = false; }
        }

        private void ValidateHandshake(Dictionary<string, object> handshake)
        {
            object value;
            if (!handshake.TryGetValue("nonce", out value) || !String.Equals(Convert.ToString(value), nonce, StringComparison.Ordinal)
                || !handshake.TryGetValue("pid", out value) || Convert.ToInt32(value) != controller.Id
                || !handshake.TryGetValue("root", out value) || !String.Equals(Path.GetFullPath(Convert.ToString(value)).TrimEnd('\\'), root.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase)
                || !handshake.TryGetValue("url", out value)) throw new InvalidOperationException("应用服务进程身份校验失败。");
            Uri address;
            if (!Uri.TryCreate(Convert.ToString(value), UriKind.Absolute, out address) || address.Scheme != "http" || address.Host != "127.0.0.1"
                || !String.IsNullOrEmpty(address.UserInfo) || address.AbsolutePath != "/" || !String.IsNullOrEmpty(address.Query) || !String.IsNullOrEmpty(address.Fragment))
                throw new InvalidOperationException("应用服务返回了无效的本机地址。");
            controllerUrl = address;
            serviceOrigins.Clear();
            // Only the two local origins supplied by this verified controller
            // may become workbench windows, including isolated installed ports.
            object urls;
            if (handshake.TryGetValue("serviceUrls", out urls))
            {
                System.Collections.IEnumerable list = urls as System.Collections.IEnumerable;
                if (list != null) foreach (object entry in list)
                {
                    Uri uri;
                    if (Uri.TryCreate(Convert.ToString(entry), UriKind.Absolute, out uri) && IsLocalService(uri)
                        && uri.AbsolutePath == "/" && String.IsNullOrEmpty(uri.Query) && String.IsNullOrEmpty(uri.Fragment)
                        && !serviceOrigins.Exists(existing => SameOrigin(existing, uri))) serviceOrigins.Add(uri);
                }
            }
            if (serviceOrigins.Count != 2) throw new InvalidOperationException("应用服务地址清单无效。");
            if (!handshake.TryGetValue("workspaceUrl", out value)
                || !Uri.TryCreate(Convert.ToString(value), UriKind.Absolute, out workspaceUrl)
                || !AllowedService(workspaceUrl) || !SameOrigin(workspaceUrl, serviceOrigins[1]) || workspaceUrl.AbsolutePath != "/"
                || !String.IsNullOrEmpty(workspaceUrl.Query) || !String.IsNullOrEmpty(workspaceUrl.Fragment))
                throw new InvalidOperationException("工作台地址校验失败。");
        }

        private async Task<Dictionary<string, object>> DesktopRpcAsync(string method, object parameters, int timeoutMilliseconds = 185000, CancellationToken cancellation = default(CancellationToken))
        {
            if (controllerUrl == null || controller == null || controller.HasExited) throw new InvalidOperationException("应用服务管理进程已退出，请重新打开 Code-agent。");
            using (DesktopRpcClient client = new DesktopRpcClient())
            {
                client.Encoding = Encoding.UTF8;
                client.Headers[HttpRequestHeader.ContentType] = "application/json; charset=utf-8";
                client.Headers["X-Desktop-Token"] = nonce;
                string body = json.Serialize(new Dictionary<string, object> { { "method", method }, { "params", parameters ?? new Dictionary<string, object>() } });
                try
                {
                    string response = await NativeAsyncDeadline.Run(client.UploadStringTaskAsync(new Uri(controllerUrl, "api/desktop/rpc"), "POST", body), timeoutMilliseconds,
                        "应用服务暂未响应，请检查运行状态后重试。", client.CancelAsync, cancellation);
                    return json.Deserialize<Dictionary<string, object>>(response);
                }
                catch (WebException error)
                {
                    if (error.Response != null)
                    {
                        using (WebResponse response = error.Response)
                        using (StreamReader reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8))
                        {
                            string text = reader.ReadToEnd();
                            try
                            {
                                Dictionary<string, object> data = json.Deserialize<Dictionary<string, object>>(text);
                                object message;
                                if (data.TryGetValue("error", out message)) throw new InvalidOperationException(Convert.ToString(message));
                            }
                            catch (ArgumentException) { }
                        }
                    }
                    throw new InvalidOperationException("无法连接应用服务管理进程：" + error.Message);
                }
            }
        }

        private async Task WaitForWorkspaceAsync()
        {
            DateTime deadline = DateTime.UtcNow.AddSeconds(120);
            while (!closing && !IsDisposed && DateTime.UtcNow < deadline)
            {
                Dictionary<string, object> state = await DesktopRpcAsync("status", null);
                object value;
                Dictionary<string, object> operation = state.TryGetValue("operation", out value) ? value as Dictionary<string, object> : null;
                if (operation != null && operation.TryGetValue("state", out value) && Convert.ToString(value) == "failed")
                    throw new InvalidOperationException(Convert.ToString(operation["message"]));
                int healthy = 0;
                System.Collections.IEnumerable services = state.TryGetValue("services", out value) ? value as System.Collections.IEnumerable : null;
                if (services != null) foreach (object item in services)
                {
                    Dictionary<string, object> service = item as Dictionary<string, object>;
                    if (service != null && service.TryGetValue("state", out value) && Convert.ToString(value) == "healthy") healthy++;
                }
                Dictionary<string, object> desktop = state.TryGetValue("desktop", out value) ? value as Dictionary<string, object> : null;
                bool settled = desktop == null || !desktop.TryGetValue("ready", out value) || Convert.ToBoolean(value);
                if (healthy == 2 && settled) return;
                loading.Text = "Code-agent\r\n\r\n正在准备工作台…\r\n应用服务 " + healthy + " / 2 已就绪";
                await Task.Delay(500);
            }
            if (!closing && !IsDisposed) throw new TimeoutException("工作台准备超时。可重试启动，或打开日志查看原因。");
        }

        private static bool IsLocalService(Uri uri)
        {
            return uri.Scheme == "http" && uri.Host == "127.0.0.1" && String.IsNullOrEmpty(uri.UserInfo) && uri.Port >= 1024 && uri.Port <= 65535;
        }

        private static bool SameOrigin(Uri left, Uri right)
        {
            return left != null && right != null && left.Scheme == right.Scheme && left.Host == right.Host && left.Port == right.Port && String.IsNullOrEmpty(left.UserInfo);
        }

        private bool AllowedService(Uri uri)
        {
            foreach (Uri service in serviceOrigins) if (SameOrigin(uri, service)) return true;
            return false;
        }

        private void ConfigureWebView(WebView2 view, bool primary)
        {
            CoreWebView2 core = view.CoreWebView2;
            core.Settings.AreDevToolsEnabled = !String.IsNullOrEmpty(diagnosticsPort);
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.AreBrowserAcceleratorKeysEnabled = false;
            // Set before navigation: WebView2's CSS caption regions preserve
            // native move/double-click/system-menu behavior inside the web UI.
            if (primary)
            {
                try { core.Settings.IsNonClientRegionSupportEnabled = true; nativeDragRegions = true; }
                catch (NotImplementedException) { nativeDragRegions = false; }
            }
            core.NewWindowRequested += delegate(object sender, CoreWebView2NewWindowRequestedEventArgs args)
            {
                args.Handled = true;
                OpenWorkspace(args.Uri);
            };
            core.NavigationStarting += delegate(object sender, CoreWebView2NavigationStartingEventArgs args)
            {
                if (args.Uri == "about:blank") return;
                Uri target;
                if (!Uri.TryCreate(args.Uri, UriKind.Absolute, out target)) { args.Cancel = true; return; }
                if (primary && SameOrigin(target, workspaceUrl)) return;
                if (!primary && AllowedService(target)) return;
                args.Cancel = true;
                if (AllowedService(target)) OpenWorkspace(target.AbsoluteUri);
            };
            core.WebMessageReceived += async delegate(object sender, CoreWebView2WebMessageReceivedEventArgs args)
            {
                if (!primary) return;
                Uri source;
                if (!Uri.TryCreate(args.Source, UriKind.Absolute, out source) || !SameOrigin(source, workspaceUrl)) return;
                string id = null;
                try
                {
                    if (args.WebMessageAsJson.Length > 32768) return;
                    Dictionary<string, object> message = json.Deserialize<Dictionary<string, object>>(args.WebMessageAsJson);
                    object value;
                    if (!message.TryGetValue("channel", out value) || Convert.ToString(value) != "code-agent-desktop"
                        || !message.TryGetValue("id", out value)) return;
                    id = Convert.ToString(value);
                    if (String.IsNullOrEmpty(id) || id.Length > 96 || !message.TryGetValue("method", out value)) return;
                    string method = Convert.ToString(value);
                    Dictionary<string, object> parameters = message.TryGetValue("params", out value) ? value as Dictionary<string, object> : null;
                    if (method == "close-pending")
                    {
                        AcknowledgeClosePending(id);
                        return;
                    }
                    if (method == "close-response")
                    {
                        if (closeReply != null && id == closeRequestId && parameters != null && parameters.TryGetValue("allow", out value) && value is bool)
                            closeReply.TrySetResult((bool)value);
                        return;
                    }
                    if (closing) throw new InvalidOperationException("应用正在退出，请稍候。");
                    if (method == "window-state" || method == "window-minimize" || method == "window-toggle-maximize"
                        || method == "window-drag" || method == "window-close")
                    {
                        ReplyDesktop(core, id, WindowChromeAction(method), null); return;
                    }
                    if (method != "status" && method != "action" && method != "logs" && method != "exit")
                        throw new InvalidOperationException("不支持的桌面操作。");
                    if (method == "exit")
                    {
                        ReplyDesktop(core, id, new Dictionary<string, object> { { "closing", true } }, null); Close(); return;
                    }
                    Dictionary<string, object> result = await DesktopRpcAsync(method, parameters);
                    ReplyDesktop(core, id, result, null);
                    object action, target;
                    if (method == "action" && parameters != null && parameters.TryGetValue("action", out action) && Convert.ToString(action) == "restart"
                        && parameters.TryGetValue("target", out target) && (Convert.ToString(target) == "all" || Convert.ToString(target) == "frontend"))
                    {
                        await WaitForWorkspaceAsync();
                        if (!closing && !IsDisposed) core.Reload();
                    }
                }
                catch (ArgumentException) { }
                catch (Exception error) { if (id != null) ReplyDesktop(core, id, null, error.Message); }
            };
            core.DownloadStarting += delegate(object sender, CoreWebView2DownloadStartingEventArgs args)
            {
                using (SaveFileDialog dialog = new SaveFileDialog())
                {
                    dialog.Title = "保存日志文件";
                    dialog.FileName = Path.GetFileName(args.ResultFilePath);
                    dialog.Filter = "日志和文本文件|*.log;*.txt|所有文件|*.*";
                    if (dialog.ShowDialog(this) != DialogResult.OK) { args.Cancel = true; return; }
                    args.ResultFilePath = dialog.FileName;
                    args.Handled = true;
                }
            };
        }

        private void ReplyDesktop(CoreWebView2 core, string id, object result, string error)
        {
            if (IsDisposed || closing) return;
            try { core.PostWebMessageAsJson(json.Serialize(new Dictionary<string, object> { { "channel", "code-agent-desktop" }, { "id", id }, { "ok", error == null }, { "result", result }, { "error", error } })); }
            catch (InvalidOperationException) { }
        }

        private async void OpenWorkspace(string value)
        {
            if (closing || IsDisposed) return;
            Uri target;
            if (!Uri.TryCreate(value, UriKind.Absolute, out target) || !AllowedService(target))
            {
                AppendHostLog("已拒绝打开非本机工作台地址。");
                return;
            }
            if (SameOrigin(target, workspaceUrl))
            {
                if (webView != null && webView.CoreWebView2 != null && webView.CoreWebView2.Source != target.AbsoluteUri) webView.CoreWebView2.Navigate(target.AbsoluteUri);
                if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
                Show(); Activate(); SetForegroundWindow(Handle); return;
            }
            foreach (WorkspaceWindow existing in workspaces)
                if (!existing.IsDisposed && SameOrigin(existing.Address, target))
                {
                    existing.Navigate(target);
                    if (existing.WindowState == FormWindowState.Minimized) existing.WindowState = FormWindowState.Normal;
                    existing.Show(); existing.Activate(); SetForegroundWindow(existing.Handle);
                    return;
                }
            WorkspaceWindow window = new WorkspaceWindow(target, Icon, SameOrigin(target, serviceOrigins[0]));
            workspaces.Add(window);
            window.Show();
            try
            {
                await window.InitializeAsync(environment);
                ConfigureWebView(window.View, false);
                window.Navigate(target);
                WriteDiagnostics("ready", null);
            }
            catch (Exception error) { window.ShowFailure(error.Message); }
        }

        private void OnControllerOutput(object sender, DataReceivedEventArgs args)
        {
            if (args.Data == null) return;
            try
            {
                if (args.Data.Length <= 16384 && args.Data.StartsWith("{", StringComparison.Ordinal))
                {
                    Dictionary<string, object> message = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(args.Data);
                    object kind, url;
                    if (message.TryGetValue("event", out kind) && Convert.ToString(kind) == "open" && message.TryGetValue("url", out url))
                    {
                        string destination = Convert.ToString(url);
                        if (!IsDisposed && IsHandleCreated) BeginInvoke(new Action(delegate { OpenWorkspace(destination); }));
                        return;
                    }
                }
            }
            catch (ArgumentException) { }
            catch (InvalidOperationException) { }
            AppendHostLog(args.Data);
        }

        private static string ResolveNode()
        {
            string configured = Environment.GetEnvironmentVariable("CODE_AGENT_NODE");
            if (!String.IsNullOrEmpty(configured) && Path.IsPathRooted(configured) && File.Exists(configured)) return configured;
            foreach (string item in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';'))
            {
                try
                {
                    string candidate = Path.Combine(item.Trim().Trim('"'), "node.exe");
                    if (File.Exists(candidate)) return candidate;
                }
                catch (ArgumentException) { }
            }
            foreach (string directory in new string[] { Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86) })
            {
                string candidate = Path.Combine(directory, "nodejs", "node.exe");
                if (File.Exists(candidate)) return candidate;
            }
            throw new InvalidOperationException("未找到 Node.js。请安装 Node.js 22.14 或更新版本后重试。");
        }

        private static string ResolveBundledPath(string root, string relative, bool directory)
        {
            if (String.IsNullOrWhiteSpace(relative) || Path.IsPathRooted(relative)) throw new InvalidOperationException("安装包运行环境路径无效。");
            string full = Path.GetFullPath(Path.Combine(root, relative.Replace('/', Path.DirectorySeparatorChar)));
            string prefix = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            if (!full.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) || (directory ? !Directory.Exists(full) : !File.Exists(full)))
                throw new InvalidOperationException("安装包运行环境缺失，请重新安装。");
            return full;
        }

        private static string Quote(string value)
        {
            // Match CommandLineToArgvW, including paths ending with a backslash.
            StringBuilder result = new StringBuilder("\"");
            int slashes = 0;
            foreach (char character in value)
            {
                if (character == '\\') { slashes++; continue; }
                if (character == '"') result.Append('\\', slashes * 2 + 1).Append(character);
                else result.Append('\\', slashes).Append(character);
                slashes = 0;
            }
            result.Append('\\', slashes * 2).Append('"');
            return result.ToString();
        }

        private void ShowFailure(string message)
        {
            if (closing || IsDisposed) return;
            SetIntegratedFrame(false);
            placeholder.Visible = true;
            placeholder.BringToFront();
            loading.Text = "工作台暂时无法打开\r\n\r\n" + message;
            retry.Visible = true;
            logs.Visible = true;
            AppendHostLog(message);
            WriteDiagnostics("failed", message);
        }

        private void OpenLogDirectory()
        {
            string home = Environment.GetEnvironmentVariable("CODE_AGENT_HOME");
            string directory = bundledNode == null ? Path.Combine(root, "logs")
                : Path.Combine(String.IsNullOrEmpty(home) ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Code-agent", "data") : home, "logs");
            Directory.CreateDirectory(directory);
            Process.Start(new ProcessStartInfo { FileName = "explorer.exe", Arguments = Quote(directory), UseShellExecute = false });
        }

        private void AppendHostLog(string message)
        {
            try
            {
                string path = Path.Combine(runtimeDirectory, "desktop-host.log");
                if (File.Exists(path) && new FileInfo(path).Length > 1024 * 1024) File.WriteAllText(path, "", Encoding.UTF8);
                File.AppendAllText(path, "[" + DateTimeOffset.Now.ToString("o") + "] " + message.Substring(0, Math.Min(message.Length, 16384)) + Environment.NewLine, Encoding.UTF8);
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }

        private void WriteDiagnostics(string state, string error)
        {
            if (String.IsNullOrEmpty(diagnosticsFile)) return;
            try
            {
                int windows = 0;
                foreach (WorkspaceWindow window in workspaces) if (!window.IsDisposed) windows++;
                string output = Path.GetFullPath(diagnosticsFile);
                Directory.CreateDirectory(Path.GetDirectoryName(output));
                File.WriteAllText(output, json.Serialize(new Dictionary<string, object>
                {
                    { "state", state }, { "pid", Process.GetCurrentProcess().Id }, { "root", root },
                    { "url", controllerUrl == null ? null : controllerUrl.AbsoluteUri }, { "controllerPid", controller == null ? 0 : controller.Id },
                    { "workspaceUrl", workspaceUrl == null ? null : workspaceUrl.AbsoluteUri }, { "mainViewUrl", webView == null || webView.CoreWebView2 == null ? null : webView.CoreWebView2.Source }, { "integratedWorkbench", true },
                    { "bundledRuntime", bundledNode != null }, { "nodeExecutable", controller == null ? bundledNode : controller.StartInfo.FileName },
                    { "webViewRuntimeDirectory", bundledWebView }, { "webViewRuntimeVersion", environment == null ? null : environment.BrowserVersionString },
                    { "windowHandle", IsHandleCreated ? Handle.ToInt64() : 0 }, { "windowEnabled", IsHandleCreated && IsWindowEnabled(Handle) },
                    { "integratedTitlebar", integratedFrame }, { "nativeDragRegions", nativeDragRegions },
                    { "windowState", WindowState.ToString() }, { "clientWidth", ClientSize.Width }, { "clientHeight", ClientSize.Height },
                    { "closePending", closePending }, { "closeStatusVisible", closeStatus.Visible },
                    { "bounds", RectangleState(Bounds) }, { "clientBounds", RectangleState(RectangleToScreen(ClientRectangle)) }, { "workingArea", RectangleState(Screen.FromControl(this).WorkingArea) },
                    { "closeStartedEnabled", closeStartedEnabled }, { "workspaceWindows", windows }, { "error", error }
                }), Encoding.UTF8);
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }

        private void StopFailedController()
        {
            if (controller == null) return;
            try
            {
                if (!controller.HasExited) { controller.StandardInput.WriteLine("shutdown"); controller.StandardInput.Flush(); }
            }
            catch (IOException) { }
            catch (InvalidOperationException) { }
            // Parent monitoring is the final cleanup path; no process-tree kill.
            controller.Dispose(); controller = null;
            if (!String.IsNullOrEmpty(handshakePath) && File.Exists(handshakePath)) File.Delete(handshakePath);
        }

        private void ShowCloseProgress(string message, bool cancellable)
        {
            if (IsDisposed) return;
            closeStatusText.Text = message; cancelClose.Text = closing ? "取消关闭" : "返回工作台";
            cancelClose.Enabled = !closing || (cancellable && !exitStarted);
            closeStatus.Visible = true; closeStatus.BringToFront(); closeStatus.Update();
        }

        internal void AcknowledgeClosePending(string requestId)
        {
            if (!closing || exitStarted || closeReply == null || requestId != closeRequestId) return;
            closePending = true;
            // The web dialog already supplies both confirmation and cancellation.
            // A second native strip duplicates it and changes the workspace size.
            closeStatus.Visible = false;
            WriteDiagnostics("closing-confirmation", null);
        }

        private bool ConfirmCloseDecision(string message, string title)
        {
            return CloseDecisionOverride != null ? CloseDecisionOverride(message, title)
                : MessageBox.Show(this, message, title, MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2) == DialogResult.Yes;
        }

        private async Task<string> ExecuteCloseScriptAsync(string script, CancellationToken cancellation)
        {
            Task<string> operation = CloseScriptOverride != null ? CloseScriptOverride(script) : webView.CoreWebView2.ExecuteScriptAsync(script);
            return await NativeAsyncDeadline.Run(operation, 5000, "工作区暂未响应，无法确认未保存的内容。", null, cancellation);
        }

        private async Task<bool> ConfirmWorkbenchCloseAsync(CancellationToken cancellation)
        {
            if (!workbenchReady || (CloseScriptOverride == null && (webView == null || webView.CoreWebView2 == null))) return true;
            try
            {
                string ready = await ExecuteCloseScriptAsync("Boolean(window.__CODE_AGENT_DESKTOP__&&window.__CODE_AGENT_DESKTOP__.closeReady)", cancellation);
                if (ready == "true")
                {
                    closeRequestId = Guid.NewGuid().ToString("N");
                    closeReply = new TaskCompletionSource<bool>();
                    closePending = false;
                    if (CloseRequestOverride != null) CloseRequestOverride(closeRequestId);
                    else webView.CoreWebView2.PostWebMessageAsJson(json.Serialize(new Dictionary<string, object> { { "channel", "code-agent-desktop" }, { "event", "before-close" }, { "id", closeRequestId } }));
                    try { return await NativeAsyncDeadline.Run(closeReply.Task, 10000, "工作区尚未确认退出。", null, cancellation); }
                    catch (TimeoutException) { }
                    if (closePending)
                    {
                        // The frontend has acknowledged the request and may be
                        // waiting for a person to read its unsaved-file dialog.
                        try { return await NativeAsyncDeadline.Run(closeReply.Task, 120000, "工作区尚未确认退出。", null, cancellation); }
                        catch (TimeoutException) { return false; }
                    }
                    return ConfirmCloseDecision("工作台暂未响应，无法确认是否有未保存的内容。仍要退出吗？", "退出 Code-agent");
                }
                // Preserve the editor's existing beforeunload protection when
                // older frontend assets are restored from a previous install.
                string dirty = await ExecuteCloseScriptAsync("(function(){var e=new Event('beforeunload',{cancelable:true});window.dispatchEvent(e);return e.defaultPrevented;})()", cancellation);
                return dirty != "true" || ConfirmCloseDecision("存在尚未保存的修改。仍要退出 Code-agent 吗？", "退出 Code-agent");
            }
            catch (OperationCanceledException) { throw; }
            catch (Exception)
            {
                return ConfirmCloseDecision("工作台暂时无响应，可能存在未保存的内容。仍要退出吗？", "退出 Code-agent");
            }
            finally { closeReply = null; closeRequestId = null; closePending = false; }
        }

        private async void OnClosingAsync(object sender, FormClosingEventArgs args)
        {
            if (permitClose) return;
            args.Cancel = true;
            if (closing) { closeStatus.BringToFront(); return; }
            closing = true;
            if (!String.IsNullOrEmpty(diagnosticsFile)) closeStartedEnabled = IsHandleCreated && IsWindowEnabled(Handle);
            exitStarted = false;
            closeCancellation = new CancellationTokenSource();
            bool failed = false;
            try
            {
                closeStatus.Visible = false;
                WriteDiagnostics("closing-workspace", null);
                Task<bool> confirmation = ConfirmWorkbenchCloseAsync(closeCancellation.Token);
                // Fast checks and an acknowledged web dialog need no extra bar.
                // A stalled workspace still exposes a cancellable native fallback.
                if (await Task.WhenAny(confirmation, Task.Delay(350, closeCancellation.Token)) != confirmation && !closePending && !closeCancellation.IsCancellationRequested)
                    ShowCloseProgress("正在检查工作区…", true);
                if (!await confirmation) return;
                exitStarted = true;
                ShowCloseProgress("正在安全退出应用服务，请稍候…", false);
                WriteDiagnostics("closing-services", null);
                Process owned = controller;
                if (CloseExitOverride != null || (owned != null && !owned.HasExited))
                {
                    try
                    {
                        if (CloseExitOverride != null) await NativeAsyncDeadline.Run(CloseExitOverride(), CloseExitTimeoutMilliseconds, "服务退出尚未确认。", null, CancellationToken.None);
                        else await DesktopRpcAsync("exit", null, CloseExitTimeoutMilliseconds);
                    }
                    catch (Exception error)
                    {
                        if (!ConfirmCloseDecision(error.Message + "\r\n\r\n仍关闭窗口吗？尚未结束的服务可能继续收尾。\r\n选择“否”返回工作台，服务状态可能已经变化。", "退出 Code-agent")) return;
                    }
                    // Exit stops only this desktop's verified services. The
                    // legacy shutdown then releases its controller connection.
                    if (owned != null && !owned.HasExited)
                    {
                        owned.StandardInput.WriteLine("shutdown"); owned.StandardInput.Flush();
                        await Task.Run(delegate { owned.WaitForExit(5000); });
                    }
                }
                activationTimer.Stop();
                if (diagnosticsExitTimer != null) { diagnosticsExitTimer.Stop(); diagnosticsExitTimer.Dispose(); }
                WriteDiagnostics("closed", null);
                foreach (WorkspaceWindow workspace in workspaces.ToArray()) if (!workspace.IsDisposed) workspace.Close();
                if (!String.IsNullOrEmpty(handshakePath) && File.Exists(handshakePath)) try { File.Delete(handshakePath); } catch (IOException) { }
                if (webView != null) webView.Dispose();
                permitClose = true; Close();
            }
            catch (OperationCanceledException) { }
            catch (Exception error) { failed = true; AppendHostLog("退出未完成：" + error.Message); WriteDiagnostics("close-failed", error.Message); }
            finally
            {
                closing = false; exitStarted = false;
                if (closeCancellation != null) { closeCancellation.Dispose(); closeCancellation = null; }
                if (!permitClose && !IsDisposed) { if (failed) ShowCloseProgress("退出未完成，可再次点击关闭重试。", true); else closeStatus.Visible = false; PublishWindowState(); }
            }
        }
    }

    internal static class NativeAsyncDeadline
    {
        internal static async Task<T> Run<T>(Task<T> operation, int milliseconds, string message, Action cancel, CancellationToken cancellation)
        {
            using (CancellationTokenSource delayCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellation))
            {
                Task deadline = Task.Delay(milliseconds, delayCancellation.Token);
                if (await Task.WhenAny(operation, deadline) == operation) { delayCancellation.Cancel(); return await operation; }
                try { if (cancel != null) cancel(); } catch (Exception) { }
                ObserveFailure(operation);
                cancellation.ThrowIfCancellationRequested();
                throw new TimeoutException(message);
            }
        }

        private static void ObserveFailure<T>(Task<T> operation)
        {
            operation.ContinueWith(delegate(Task<T> failed) { var observed = failed.Exception; }, CancellationToken.None,
                TaskContinuationOptions.OnlyOnFaulted | TaskContinuationOptions.ExecuteSynchronously, TaskScheduler.Default);
        }
    }

    internal sealed class DesktopRpcClient : WebClient
    {
        protected override WebRequest GetWebRequest(Uri address)
        {
            WebRequest request = base.GetWebRequest(address);
            request.Timeout = 185000;
            HttpWebRequest http = request as HttpWebRequest;
            if (http != null) { http.ReadWriteTimeout = 185000; http.AllowAutoRedirect = false; http.Proxy = null; }
            return request;
        }
    }

    internal sealed class WorkspaceWindow : Form
    {
        internal readonly WebView2 View;
        internal Uri Address;
        private readonly Label loading;

        internal WorkspaceWindow(Uri address, Icon icon, bool backend)
        {
            Address = address;
            Text = backend ? "Code-agent · 后端接口" : "Code-agent · 工作台";
            StartPosition = FormStartPosition.CenterParent;
            ClientSize = new Size(1440, 960);
            MinimumSize = new Size(900, 640);
            AutoScaleMode = AutoScaleMode.Dpi;
            Icon = icon;
            BackColor = Color.FromArgb(245, 247, 251);
            loading = new Label { Text = "正在打开工作台…", Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleCenter };
            View = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = BackColor };
            Controls.Add(View);
            Controls.Add(loading);
            FormClosed += delegate { View.Dispose(); };
        }

        internal async Task InitializeAsync(CoreWebView2Environment environment)
        {
            await View.EnsureCoreWebView2Async(environment);
            View.CoreWebView2.NavigationCompleted += delegate(object sender, CoreWebView2NavigationCompletedEventArgs args)
            {
                if (args.IsSuccess) { loading.Visible = false; View.BringToFront(); }
                else ShowFailure("页面暂时无法加载，请通过工作台的应用状态检查服务。");
            };
        }

        internal void Navigate(Uri address)
        {
            Address = address;
            if (View.CoreWebView2 != null) View.CoreWebView2.Navigate(address.AbsoluteUri);
        }

        internal void ShowFailure(string message)
        {
            loading.Text = message;
            loading.Visible = true;
            loading.BringToFront();
        }
    }
}
