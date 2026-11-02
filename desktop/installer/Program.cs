using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Win32;

namespace CodeAgent.Installer
{
    internal sealed class InstallResult
    {
        internal string Root, Version, Backup;
        internal int Files;
    }

    internal static class SetupProgram
    {
        internal static string DefaultTarget { get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "Code-agent"); } }

        [STAThread]
        private static int Main(string[] args)
        {
            InstallCommon.EnableLongPaths();
            bool silent = InstallCommon.Has(args, "--silent");
            string resultFile = null;
            try
            {
                resultFile = InstallCommon.Argument(args, "--result-file", null);
                InstallCommon.CheckArgs(args, "--silent", "--dir", "--no-launch", "--result-file");
                string target = InstallCommon.Argument(args, "--dir", ExistingTarget() ?? DefaultTarget);
                if (silent)
                {
                    InstallResult result = Install(target, delegate { });
                    InstallCommon.Result(resultFile, true, "Code-agent 已安装：" + result.Root);
                    return 0;
                }
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                using (SetupWindow window = new SetupWindow(target, !InstallCommon.Has(args, "--no-launch"), resultFile))
                {
                    Application.Run(window);
                    return window.ExitCode;
                }
            }
            catch (Exception error)
            {
                InstallCommon.Result(resultFile, false, error.Message);
                if (!silent) MessageBox.Show(error.Message, "Code-agent 安装失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }
        }

        private static string ExistingTarget()
        {
            using (RegistryKey key = Registry.CurrentUser.OpenSubKey(InstallCommon.RegistryPath))
            {
                string path = key == null ? null : Convert.ToString(key.GetValue("InstallLocation", null));
                return String.IsNullOrEmpty(path) ? null : path;
            }
        }

        private static string RootKey(string root)
        {
            using (SHA256 hash = SHA256.Create())
                return BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(root.ToLowerInvariant()))).Replace("-", "").ToLowerInvariant().Substring(0, 32);
        }

        internal static InstallResult Install(string selected, Action<int, string> progress)
        {
            string target = InstallCommon.ValidateTarget(selected);
            string registered = ExistingTarget();
            if (!String.IsNullOrEmpty(registered) && Directory.Exists(registered) && !InstallCommon.Equal(registered, target))
                throw new InvalidOperationException("Code-agent 已安装在其他目录。请选择原安装目录升级，或先卸载旧安装。\r\n" + registered);
            Dictionary<string, object> previous = null;
            if (Directory.Exists(target) && Directory.GetFileSystemEntries(target).Length != 0) previous = InstallCommon.ReadManifest(target);
            string parent = Path.GetDirectoryName(target);
            if (String.IsNullOrEmpty(parent)) throw new InvalidOperationException("安装路径无效。");
            InstallCommon.NoLinks(parent);
            Directory.CreateDirectory(parent);
            string stage = Path.Combine(parent, ".Code-agent-install-" + Guid.NewGuid().ToString("N"));
            string backup = null;
            List<string> files = new List<string>();
            bool moved = false, owns = false;
            using (Mutex mutex = new Mutex(false, "Local\\CodeAgentDesktopLauncher-" + RootKey(target)))
            {
                try
                {
                    try { owns = mutex.WaitOne(0); } catch (AbandonedMutexException) { owns = true; }
                    if (!owns) throw new RunningApplicationException(target, "检测到 Code-agent 窗口仍在运行。可点击“停止后台并重试”；窗口内的未保存内容和运行中任务仍会受到保护。");
                    InstallCommon.EnsureIdle(target);
                    Directory.CreateDirectory(stage);
                    long totalBytes = 0;
                    using (Stream stream = PayloadSource.Open())
                    {
                        if (stream == null) throw new InvalidOperationException("安装包缺少程序文件，请重新下载安装包。");
                        using (ZipArchive archive = new ZipArchive(stream, ZipArchiveMode.Read))
                        {
                            long total = archive.Entries.Sum(entry => entry.Length);
                            long completed = 0;
                            HashSet<string> unique = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                            foreach (ZipArchiveEntry entry in archive.Entries)
                            {
                                string relative = entry.FullName.Replace('\\', '/').TrimEnd('/');
                                if (relative.Length == 0) continue;
                                string destination = InstallCommon.FilePath(stage, relative);
                                int unixType = (entry.ExternalAttributes >> 16) & 0xF000;
                                if (unixType == 0xA000 || (entry.ExternalAttributes & (int)FileAttributes.ReparsePoint) != 0)
                                    throw new InvalidOperationException("安装包不能包含文件链接。");
                                if (entry.FullName.EndsWith("/", StringComparison.Ordinal)) { Directory.CreateDirectory(destination); continue; }
                                if (relative == InstallCommon.ManifestName || !unique.Add(relative)) throw new InvalidOperationException("安装包包含重复或保留的文件路径。");
                                Directory.CreateDirectory(Path.GetDirectoryName(destination));
                                files.Add(relative);
                                using (Stream input = entry.Open())
                                using (FileStream output = new FileStream(destination, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                                {
                                    byte[] buffer = new byte[128 * 1024];
                                    int read;
                                    while ((read = input.Read(buffer, 0, buffer.Length)) > 0)
                                    {
                                        output.Write(buffer, 0, read); completed += read;
                                        progress(total > 0 ? (int)Math.Min(94, completed * 94 / total) : 0, "正在安装 " + relative);
                                    }
                                    if (output.Length != entry.Length) throw new InvalidOperationException("安装文件校验失败：" + relative);
                                }
                                totalBytes += entry.Length;
                            }
                        }
                    }
                    string runtimeManifest = Path.Combine(stage, "desktop-runtime.json");
                    if (!File.Exists(runtimeManifest) || !File.Exists(InstallCommon.MainExecutable(stage)) || !File.Exists(Path.Combine(stage, "Code-agent Uninstall.exe")))
                        throw new InvalidOperationException("安装包缺少运行环境或应用文件，请重新下载。");
                    Dictionary<string, object> runtime = InstallCommon.Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(runtimeManifest, Encoding.UTF8));
                    object schema, versionValue;
                    if (!runtime.TryGetValue("schemaVersion", out schema) || Convert.ToInt32(schema) != 1 || !runtime.TryGetValue("version", out versionValue))
                        throw new InvalidOperationException("安装包运行环境清单无效。");
                    string version = Convert.ToString(versionValue);
                    foreach (string key in new string[] { "node", "python", "webview2" })
                    {
                        object value;
                        if (!runtime.TryGetValue(key, out value)) throw new InvalidOperationException("安装包运行环境清单缺少 " + key);
                        string required = InstallCommon.FilePath(stage, Convert.ToString(value));
                        if (key == "webview2") required = Path.Combine(required, "msedgewebview2.exe");
                        if (!File.Exists(required)) throw new InvalidOperationException("安装包运行环境缺失：" + key);
                    }
                    GrantBrowserReadAccess(InstallCommon.FilePath(stage, Convert.ToString(runtime["webview2"])));
                    Dictionary<string, object> manifest = new Dictionary<string, object>
                    {
                        { "schemaVersion", 1 }, { "appId", InstallCommon.AppId }, { "version", version },
                        { "installRoot", target }, { "files", files }, { "shortcuts", InstallCommon.ShortcutPaths() }
                    };
                    File.WriteAllText(Path.Combine(stage, InstallCommon.ManifestName), InstallCommon.Json.Serialize(manifest), new UTF8Encoding(false));
                    progress(95, "正在创建应用入口…");
                    InstallCommon.EnsureIdle(target);
                    if (Directory.Exists(target))
                    {
                        if (Directory.GetFileSystemEntries(target).Length == 0) Directory.Delete(target);
                        else { backup = target + ".backup-" + DateTime.Now.ToString("yyyyMMddHHmmss") + "-" + Guid.NewGuid().ToString("N").Substring(0, 6); Directory.Move(target, backup); }
                    }
                    Directory.Move(stage, target); moved = true;
                    try
                    {
                        InstallCommon.CreateShortcuts(target);
                        InstallCommon.Register(target, version, totalBytes);
                    }
                    catch
                    {
                        // Roll back the application directory before attempting to
                        // restore registration. User data is outside both folders.
                        Directory.Move(target, stage); moved = false;
                        if (backup != null) { Directory.Move(backup, target); backup = null; }
                        if (previous != null)
                        {
                            InstallCommon.CreateShortcuts(target);
                            InstallCommon.Register(target, Convert.ToString(previous["version"]), 0);
                        }
                        else InstallCommon.RemoveRegistration(target);
                        throw;
                    }
                    progress(100, "安装完成");
                    return new InstallResult { Root = target, Version = version, Backup = backup, Files = files.Count };
                }
                catch
                {
                    if (!moved && backup != null && Directory.Exists(backup) && !Directory.Exists(target))
                    {
                        Directory.Move(backup, target);
                        backup = null;
                    }
                    throw;
                }
                finally
                {
                    if (!moved && Directory.Exists(stage))
                    {
                        // Delete only files created from validated archive entries.
                        // Unknown files and any links cause preservation/refusal.
                        try { InstallCommon.RemoveOwnedFiles(stage, new Dictionary<string, object> { { "files", files } }); } catch { }
                    }
                    if (owns) mutex.ReleaseMutex();
                }
            }
        }

        internal static void StopForUpgrade(string selected)
        {
            string target = InstallCommon.ValidateTarget(selected);
            InstallCommon.ReadManifest(target);
            string node = Path.Combine(target, "runtimes", "node", "node.exe");
            InstallCommon.NoLinks(node);
            if (!File.Exists(node)) throw new InvalidOperationException("未找到已安装的运行环境。请先退出旧版应用并安全停止后台服务。");
            foreach (Process app in Process.GetProcesses())
            {
                try
                {
                    string executable = app.MainModule.FileName;
                    if (InstallCommon.Equal(executable, Path.Combine(target, "Code-agent.exe")) || InstallCommon.Equal(executable, Path.Combine(target, "Code-agent Launcher.exe")))
                        if (app.MainWindowHandle != IntPtr.Zero) app.CloseMainWindow();
                }
                catch (System.ComponentModel.Win32Exception) { }
                catch (InvalidOperationException) { }
                finally { app.Dispose(); }
            }
            using (Mutex mutex = new Mutex(false, "Local\\CodeAgentDesktopLauncher-" + RootKey(target)))
            {
                bool owns = false;
                try
                {
                    DateTime deadline = DateTime.UtcNow.AddSeconds(45);
                    do { try { owns = mutex.WaitOne(250); } catch (AbandonedMutexException) { owns = true; } } while (!owns && DateTime.UtcNow < deadline);
                    if (!owns) throw new RunningApplicationException(target, "应用窗口尚未退出。请处理未保存内容或运行中任务，再点击“停止后台并重试”。");
                    string temporary = Path.Combine(Path.GetTempPath(), "Code-agent-upgrade-" + Guid.NewGuid().ToString("N"));
                    string script = Path.Combine(temporary, "code-agent-maintenance.mjs");
                    Directory.CreateDirectory(temporary); InstallCommon.NoLinks(temporary);
                    bool exited = false;
                    try
                    {
                        using (Stream source = Assembly.GetExecutingAssembly().GetManifestResourceStream("CodeAgent.Maintenance.mjs"))
                        {
                            if (source == null) throw new InvalidOperationException("安装包缺少升级维护组件，请重新下载安装包。");
                            using (FileStream output = new FileStream(script, FileMode.CreateNew, FileAccess.Write, FileShare.None)) source.CopyTo(output);
                        }
                        ProcessStartInfo start = new ProcessStartInfo {
                            FileName = node, Arguments = InstallCommon.Quote(script) + " --root " + InstallCommon.Quote(target) + " --action stop",
                            WorkingDirectory = target, UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden,
                            RedirectStandardOutput = true, RedirectStandardError = true, StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
                        };
                        using (Process helper = Process.Start(start))
                        {
                            Task<string> output = helper.StandardOutput.ReadToEndAsync(), errors = helper.StandardError.ReadToEndAsync();
                            exited = helper.WaitForExit(180000);
                            if (!exited) throw new InvalidOperationException("安全停止仍在进行，请稍候再重试。安装尚未开始，也没有强制终止服务。");
                            string text = output.GetAwaiter().GetResult().Trim(), error = errors.GetAwaiter().GetResult().Trim();
                            Dictionary<string, object> result = null;
                            try { result = InstallCommon.Json.Deserialize<Dictionary<string, object>>(text); } catch (ArgumentException) { }
                            object ok, message;
                            if (helper.ExitCode != 0 || result == null || !result.TryGetValue("ok", out ok) || !Convert.ToBoolean(ok))
                                throw new InvalidOperationException(result != null && result.TryGetValue("message", out message) ? Convert.ToString(message) : String.IsNullOrEmpty(error) ? "无法安全停止旧版后台，请查看应用状态并重试。" : error);
                        }
                        InstallCommon.EnsureIdle(target);
                    }
                    finally
                    {
                        if (exited) { try { File.Delete(script); Directory.Delete(temporary, false); } catch (IOException) { } catch (UnauthorizedAccessException) { } }
                    }
                }
                finally { if (owns) mutex.ReleaseMutex(); }
            }
        }

        private static void GrantBrowserReadAccess(string directory)
        {
            // The fixed WebView2 runtime needs these read/execute permissions
            // for its AppContainer processes on Windows 10. Scope them only to
            // the browser binaries, never the user's model or session data.
            InstallCommon.NoLinks(directory);
            DirectorySecurity security = Directory.GetAccessControl(directory);
            foreach (string sid in new string[] { "S-1-15-2-1", "S-1-15-2-2" })
                security.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid), FileSystemRights.ReadAndExecute,
                    InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
            Directory.SetAccessControl(directory, security);
        }
    }

    internal sealed class SetupWindow : Form
    {
        private readonly TextBox directory;
        private readonly Button browse, install, cancel;
        private readonly Label title, summary, status, statusTitle, percentage;
        private readonly SetupProgress progress;
        private readonly CheckBox launch;
        private readonly SetupSurface pathBorder, feedback;
        private readonly SetupGlyph statusGlyph;
        private readonly ToolTip tips = new ToolTip();
        private readonly System.Windows.Forms.Timer refresh = new System.Windows.Forms.Timer { Interval = 100 };
        private readonly object progressLock = new object();
        private readonly string resultFile;
        private bool preview;
        private bool running;
        private bool progressPending;
        private int pendingValue;
        private string pendingMessage;
        private string stopRequiredRoot;
        private bool stoppingForUpgrade;
        private bool stopCompleted;
        private Task<InstallResult> installTask;
        private InstallResult installed;
        internal Func<string, Action<int, string>, InstallResult> InstallOperation = SetupProgram.Install;
        internal Action<string> StopOperation = SetupProgram.StopForUpgrade;
        internal int ExitCode = 0;

        internal SetupWindow(string target, bool shouldLaunch, string resultFile)
        {
            SuspendLayout();
            this.resultFile = resultFile;
            Text = "Code-agent 安装程序";
            Name = "code-agent-setup";
            Font = SetupFonts.Create(9.5F, FontStyle.Regular);
            ForeColor = Color.FromArgb(31, 42, 61);
            BackColor = Color.FromArgb(246, 248, 252);
            AutoScaleDimensions = new SizeF(96F, 96F);
            AutoScaleMode = AutoScaleMode.Dpi;
            ClientSize = new Size(780, 520);
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = false; MinimizeBox = false;
            try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }

            TableLayoutPanel shell = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, Margin = Padding.Empty, Padding = Padding.Empty };
            shell.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            shell.RowStyles.Add(new RowStyle(SizeType.Absolute, 56));
            shell.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            shell.RowStyles.Add(new RowStyle(SizeType.Absolute, 82));

            SetupSurface header = new SetupSurface { Dock = DockStyle.Fill, FillColor = Color.White, BorderColor = Color.FromArgb(226, 232, 241), BorderBottomOnly = true, Padding = new Padding(28, 0, 28, 0), Margin = Padding.Empty };
            TableLayoutPanel brand = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 4, RowCount = 1, Margin = Padding.Empty };
            brand.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 28));
            brand.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            brand.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            brand.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            brand.Controls.Add(new SetupGlyph { Kind = "brand", Size = new Size(28, 28), Anchor = AnchorStyles.None, Margin = Padding.Empty }, 0, 0);
            brand.Controls.Add(new Label { Text = "Code-agent", AutoSize = true, Anchor = AnchorStyles.Left, Font = SetupFonts.Create(10.5F, FontStyle.Bold), Margin = new Padding(10, 0, 0, 0) }, 1, 0);
            string version = FileVersionInfo.GetVersionInfo(Assembly.GetExecutingAssembly().Location).ProductVersion;
            if (version == "0.0.0.0") version = null;
            brand.Controls.Add(new Label { Text = "Windows x64" + (String.IsNullOrEmpty(version) ? "" : "  ·  v" + version), AutoSize = true, Anchor = AnchorStyles.Right, ForeColor = Color.FromArgb(112, 124, 143), Font = SetupFonts.Create(8.5F, FontStyle.Regular), Margin = Padding.Empty }, 3, 0);
            header.Controls.Add(brand);
            shell.Controls.Add(header, 0, 0);

            Panel bodyScroll = new Panel { Dock = DockStyle.Fill, AutoScroll = true, Padding = new Padding(28, 22, 28, 16), Margin = Padding.Empty };
            TableLayoutPanel body = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 1, RowCount = 4, Margin = Padding.Empty };
            body.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            for (int row = 0; row < 4; row++) body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            TableLayoutPanel intro = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 1, RowCount = 2, Margin = new Padding(0, 0, 0, 13) };
            intro.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            statusGlyph = new SetupGlyph { Kind = "success", Size = new Size(27, 27), Visible = false, Margin = new Padding(0, 0, 9, 0) };
            title = new Label { Name = "setup-title", Text = "安装 Code-agent", AutoSize = true, Font = SetupFonts.Create(15F, FontStyle.Bold), Margin = new Padding(0, 0, 0, 5) };
            summary = new Label { Text = "将完整编码工作台安装到此电脑，准备好即可开始。", AutoSize = true, ForeColor = Color.FromArgb(102, 115, 135), Margin = Padding.Empty };
            FlowLayoutPanel titleRow = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, WrapContents = false, Margin = Padding.Empty, Padding = Padding.Empty };
            titleRow.Controls.Add(statusGlyph); titleRow.Controls.Add(title); intro.Controls.Add(titleRow, 0, 0); intro.Controls.Add(summary, 0, 1);
            body.Controls.Add(intro, 0, 0);

            FlowLayoutPanel components = new FlowLayoutPanel { Name = "included-components", Dock = DockStyle.Top, AutoSize = true, WrapContents = true, Margin = new Padding(0, 0, 0, 15), Padding = Padding.Empty };
            foreach (string component in new string[] { "前端工作台", "智能体后端", "Node.js · Python · Git", "内置 WebView2" })
                components.Controls.Add(new Label { Text = component, AutoSize = true, BackColor = Color.FromArgb(235, 239, 247), ForeColor = Color.FromArgb(85, 102, 130), Font = SetupFonts.Create(8.5F, FontStyle.Regular), Padding = new Padding(8, 4, 8, 4), Margin = new Padding(0, 0, 7, 0) });
            body.Controls.Add(components, 0, 1);

            SetupSurface locationCard = new SetupSurface { Name = "path-card", Dock = DockStyle.Top, AutoSize = true, FillColor = Color.White, BorderColor = Color.FromArgb(222, 228, 239), Padding = new Padding(17, 14, 17, 13), Margin = new Padding(0, 0, 0, 13) };
            TableLayoutPanel locationLayout = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 1, RowCount = 4, Margin = Padding.Empty };
            locationLayout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            locationLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            locationLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 41));
            locationLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            locationLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            locationLayout.Controls.Add(new Label { Text = "安装位置", AutoSize = true, Font = SetupFonts.Create(9.5F, FontStyle.Bold), Margin = new Padding(0, 0, 0, 8) }, 0, 0);
            pathBorder = new SetupSurface { Dock = DockStyle.Fill, BorderColor = Color.FromArgb(215, 223, 237), FillColor = Color.FromArgb(252, 253, 255), Padding = new Padding(11, 4, 4, 4), Margin = Padding.Empty };
            TableLayoutPanel pathRow = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1, Margin = Padding.Empty };
            pathRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100)); pathRow.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 86));
            directory = new TextBox { Name = "install-path", Text = target, BorderStyle = BorderStyle.None, BackColor = pathBorder.FillColor, ForeColor = Color.FromArgb(50, 65, 88), Anchor = AnchorStyles.Left | AnchorStyles.Right, Margin = new Padding(0, 0, 8, 0), AccessibleName = "安装位置" };
            browse = new SetupButton { Text = "更改…", Dock = DockStyle.Fill, Margin = Padding.Empty, AccessibleName = "更改安装位置" };
            pathRow.Controls.Add(directory, 0, 0); pathRow.Controls.Add(browse, 1, 0); pathBorder.Controls.Add(pathRow); locationLayout.Controls.Add(pathBorder, 0, 1);
            locationLayout.Controls.Add(new Label { Text = "仅为当前用户安装，无需管理员权限。", AutoSize = true, ForeColor = Color.FromArgb(112, 125, 145), Font = SetupFonts.Create(8.5F, FontStyle.Regular), Margin = new Padding(0, 8, 0, 9) }, 0, 2);
            Label dataNotice = new Label { Text = "会话与模型配置独立保存，更新或卸载会保留用户数据。", AutoSize = true, ForeColor = Color.FromArgb(88, 109, 145), Font = SetupFonts.Create(8.5F, FontStyle.Regular), Margin = Padding.Empty };
            locationLayout.Controls.Add(dataNotice, 0, 3); locationCard.Controls.Add(locationLayout); body.Controls.Add(locationCard, 0, 2);

            feedback = new SetupSurface { Name = "progress-section", Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, Visible = false, FillColor = Color.FromArgb(237, 242, 255), BorderColor = Color.FromArgb(211, 221, 250), Padding = new Padding(13, 10, 13, 10), Margin = Padding.Empty };
            TableLayoutPanel feedbackLayout = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 2, RowCount = 3, Margin = Padding.Empty };
            feedbackLayout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100)); feedbackLayout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 52));
            for (int row = 0; row < 3; row++) feedbackLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            statusTitle = new Label { Name = "feedback-title", Text = "正在准备安装", AutoSize = true, Dock = DockStyle.Fill, Font = SetupFonts.Create(9F, FontStyle.Bold), ForeColor = Color.FromArgb(57, 82, 145), Margin = Padding.Empty };
            percentage = new Label { Text = "0%", AutoSize = true, ForeColor = Color.FromArgb(53, 93, 251), Font = SetupFonts.Create(9F, FontStyle.Bold), Margin = Padding.Empty };
            progress = new SetupProgress { Name = "install-progress", Dock = DockStyle.Top, Height = 5, Minimum = 0, Maximum = 100, Margin = new Padding(0, 7, 0, 6) };
            status = new Label { Name = "install-status", Text = "", Dock = DockStyle.Top, AutoSize = true, ForeColor = Color.FromArgb(99, 117, 153), Font = SetupFonts.Create(8.5F, FontStyle.Regular), Margin = new Padding(0, 4, 0, 0) };
            feedbackLayout.Controls.Add(statusTitle, 0, 0); feedbackLayout.Controls.Add(percentage, 1, 0); feedbackLayout.Controls.Add(progress, 0, 1); feedbackLayout.SetColumnSpan(progress, 2); feedbackLayout.Controls.Add(status, 0, 2); feedbackLayout.SetColumnSpan(status, 2);
            feedback.Controls.Add(feedbackLayout); body.Controls.Add(feedback, 0, 3); bodyScroll.Controls.Add(body); shell.Controls.Add(bodyScroll, 0, 1);

            SetupSurface footer = new SetupSurface { Dock = DockStyle.Fill, FillColor = Color.White, BorderColor = Color.FromArgb(226, 232, 241), BorderTopOnly = true, Padding = new Padding(28, 20, 28, 20), Margin = Padding.Empty };
            TableLayoutPanel actions = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 4, RowCount = 1, Margin = Padding.Empty };
            actions.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100)); actions.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 83)); actions.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 9)); actions.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 148));
            launch = new CheckBox { Name = "launch-after-install", Text = "完成后打开 Code-agent", Checked = shouldLaunch, AutoSize = true, Anchor = AnchorStyles.Left, Margin = Padding.Empty, Font = SetupFonts.Create(9F, FontStyle.Regular) };
            cancel = new SetupButton { Name = "cancel-install", Text = "取消", Dock = DockStyle.Fill, Margin = Padding.Empty };
            install = new SetupButton { Name = "start-install", Primary = true, Text = "开始安装", Dock = DockStyle.Fill, Margin = Padding.Empty };
            actions.Controls.Add(launch, 0, 0); actions.Controls.Add(cancel, 1, 0); actions.Controls.Add(install, 3, 0); footer.Controls.Add(actions); shell.Controls.Add(footer, 0, 2); Controls.Add(shell);

            browse.Click += delegate { using (FolderBrowserDialog dialog = new FolderBrowserDialog { Description = "选择 Code-agent 安装文件夹", SelectedPath = directory.Text }) if (dialog.ShowDialog(this) == DialogResult.OK) directory.Text = dialog.SelectedPath; };
            directory.Enter += delegate { pathBorder.BorderColor = Color.FromArgb(80, 115, 248); pathBorder.Invalidate(); };
            directory.Leave += delegate { pathBorder.BorderColor = Color.FromArgb(215, 223, 237); pathBorder.Invalidate(); };
            directory.TextChanged += delegate
            {
                tips.SetToolTip(directory, directory.Text);
                if (!running && stopRequiredRoot != null)
                {
                    stopRequiredRoot = null;
                    ShowState("ready", 0, "");
                }
            };
            tips.SetToolTip(directory, directory.Text);
            cancel.Click += delegate { Close(); };
            install.Click += OnInstall;
            refresh.Tick += PollInstallation;
            launch.CheckedChanged += delegate { if (installed != null) install.Text = launch.Checked ? "打开 Code-agent" : "完成"; };
            FormClosing += delegate(object sender, FormClosingEventArgs args) { if (running) args.Cancel = true; };
            AcceptButton = install; CancelButton = cancel;
            Shown += delegate
            {
                directory.SelectionStart = 0; directory.SelectionLength = 0; ActiveControl = install; install.Focus();
                Rectangle working = Screen.FromControl(this).WorkingArea;
                if (Width > working.Width - 24 || Height > working.Height - 24) Size = new Size(Math.Min(Width, working.Width - 24), Math.Min(Height, working.Height - 24));
            };
            ResumeLayout(true);
        }

        private void OnInstall(object sender, EventArgs args)
        {
            if (preview || running) return;
            if (installed != null)
            {
                try
                {
                    if (launch.Checked) Process.Start(new ProcessStartInfo { FileName = InstallCommon.MainExecutable(installed.Root), WorkingDirectory = installed.Root, UseShellExecute = false });
                    Close();
                }
                catch (Exception error) { ShowState("error", 0, "安装已完成，但暂时无法打开程序。" + error.Message); install.Text = "重试打开"; }
                return;
            }
            try
            {
                string target = directory.Text;
                bool stopFirst = stopRequiredRoot != null && InstallCommon.Equal(stopRequiredRoot, target);
                running = true; stoppingForUpgrade = stopFirst;
                lock (progressLock) { progressPending = false; stopCompleted = !stopFirst; }
                ShowState("running", 0, stopFirst ? "正在安全停止旧版后台；有任务执行时将保留后台并提示。" : "正在准备程序文件与内置运行环境…");
                if (stopFirst) { statusTitle.Text = "正在安全停止旧版后台"; install.Text = "正在停止…"; }
                installTask = Task.Run(delegate
                {
                    if (stopFirst)
                    {
                        StopOperation(target);
                        lock (progressLock) { stopCompleted = true; }
                    }
                    return InstallOperation(target, UpdateProgress);
                });
                refresh.Start();
            }
            catch (Exception error)
            {
                running = false; stoppingForUpgrade = false;
                ShowFailure(error);
                InstallCommon.Result(resultFile, false, error.Message);
            }
        }

        private void PollInstallation(object sender, EventArgs args)
        {
            Task<InstallResult> task = installTask;
            if (task == null || IsDisposed) return;
            if (task.IsCompleted)
            {
                // Observe the real result on the UI thread. A progress report of
                // 100 alone is not proof that commit and lock cleanup finished.
                refresh.Stop(); installTask = null; running = false;
                lock (progressLock)
                {
                    progressPending = false;
                    if (stoppingForUpgrade && stopCompleted) stopRequiredRoot = null;
                }
                stoppingForUpgrade = false;
                try
                {
                    installed = task.GetAwaiter().GetResult();
                    stopRequiredRoot = null;
                    ExitCode = 0;
                    ShowState("success", 100, "桌面和开始菜单已创建快捷方式，可以开始使用。");
                    InstallCommon.Result(resultFile, true, "Code-agent 已安装：" + installed.Root);
                }
                catch (Exception error)
                {
                    ShowFailure(error);
                    InstallCommon.Result(resultFile, false, error.Message);
                }
                return;
            }
            int value; string message;
            lock (progressLock)
            {
                if (stoppingForUpgrade && stopCompleted)
                {
                    stoppingForUpgrade = false; stopRequiredRoot = null;
                    statusTitle.Text = "正在安装程序文件"; install.Text = "正在安装…";
                    status.Text = "正在准备程序文件与内置运行环境…";
                }
                if (!progressPending) return;
                value = pendingValue; message = pendingMessage; progressPending = false;
            }
            progress.Value = Math.Max(0, Math.Min(99, value)); percentage.Text = progress.Value + "%";
            status.Text = value >= 100 ? "正在确认安装结果…" : message;
            tips.SetToolTip(status, status.Text); status.AccessibleDescription = status.Text;
        }

        private void ShowFailure(Exception error)
        {
            RunningApplicationException occupied = error as RunningApplicationException;
            if (occupied != null)
            {
                try { stopRequiredRoot = InstallCommon.Equal(occupied.Root, directory.Text) ? occupied.Root : null; }
                catch { stopRequiredRoot = null; }
            }
            ExitCode = 1;
            ShowState("error", 0, error.Message);
        }

        private void ShowState(string state, int value, string message)
        {
            bool busy = state == "running", success = state == "success", error = state == "error";
            title.Text = success ? "安装完成" : busy ? "正在安装 Code-agent" : "安装 Code-agent";
            summary.Text = success ? "Code-agent 已准备就绪。" : "将完整编码工作台安装到此电脑，准备好即可开始。";
            statusGlyph.Kind = "success"; statusGlyph.Visible = success;
            directory.Enabled = !busy && !success; browse.Enabled = !busy && !success;
            launch.Enabled = !busy; cancel.Enabled = !busy; install.Enabled = !busy;
            cancel.Text = success ? "关闭" : "取消";
            install.Text = success ? (launch.Checked ? "打开 Code-agent" : "完成") : busy ? "正在安装…" : error ? (stopRequiredRoot != null ? "停止后台并重试" : "重试安装") : "开始安装";
            feedback.Visible = busy || success || error;
            feedback.FillColor = error ? Color.FromArgb(255, 245, 246) : success ? Color.FromArgb(238, 249, 243) : Color.FromArgb(237, 242, 255);
            feedback.BorderColor = error ? Color.FromArgb(244, 211, 216) : success ? Color.FromArgb(204, 233, 218) : Color.FromArgb(211, 221, 250);
            statusTitle.Text = error ? (stopRequiredRoot != null ? "需要停止旧版后台" : "安装未完成") : success ? "已成功安装" : "正在安装程序文件";
            statusTitle.ForeColor = error ? Color.FromArgb(174, 55, 72) : success ? Color.FromArgb(24, 119, 80) : Color.FromArgb(57, 82, 145);
            status.Text = message; tips.SetToolTip(status, message);
            status.AccessibleDescription = message;
            percentage.Visible = busy; progress.Visible = busy;
            progress.Value = Math.Max(0, Math.Min(100, value)); percentage.Text = progress.Value + "%";
            feedback.Invalidate(); PerformLayout();
        }

        internal void ApplyPreviewState(string state) { ApplyPreviewState(state, 46); }
        internal void ApplyPreviewState(string state, int value)
        {
            if (state != "ready" && state != "running" && state != "success" && state != "error") throw new ArgumentException("Unknown preview state.");
            preview = true;
            ShowState(state, value, state == "running" ? "正在安装 runtimes / python / Lib / site-packages / fastapi…"
                : state == "success" ? "桌面和开始菜单已创建快捷方式，可以开始使用。"
                : state == "error" ? "无法写入所选目录。请选择其他位置，然后重试安装。" : "");
        }

        private void UpdateProgress(int value, string message)
        {
            // Extraction may report tens of thousands of blocks. Keep just the
            // latest value; queuing a UI callback per block can starve completion.
            lock (progressLock)
            {
                pendingValue = value; pendingMessage = message; progressPending = true;
            }
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing) { refresh.Stop(); refresh.Dispose(); tips.Dispose(); }
            base.Dispose(disposing);
        }
    }

    internal static class SetupFonts
    {
        private static readonly string Family = FindFamily();
        private static string FindFamily()
        {
            using (System.Drawing.Text.InstalledFontCollection installed = new System.Drawing.Text.InstalledFontCollection())
            {
                if (installed.Families.Any(font => font.Name == "Microsoft YaHei UI")) return "Microsoft YaHei UI";
                if (installed.Families.Any(font => font.Name == "Segoe UI")) return "Segoe UI";
            }
            return SystemFonts.MessageBoxFont.FontFamily.Name;
        }
        internal static Font Create(float size, FontStyle style) { return new Font(Family, size, style, GraphicsUnit.Point); }
    }

    internal sealed class SetupSurface : Panel
    {
        private Color fillColor = Color.White;
        internal Color FillColor { get { return fillColor; } set { fillColor = value; BackColor = value; Invalidate(); } }
        internal Color BorderColor = Color.FromArgb(222, 228, 239);
        internal bool BorderTopOnly, BorderBottomOnly;
        internal SetupSurface() { DoubleBuffered = true; BackColor = Color.White; SetStyle(ControlStyles.ResizeRedraw, true); }
        protected override void OnPaintBackground(PaintEventArgs args) { args.Graphics.Clear(FillColor); }
        protected override void OnPaint(PaintEventArgs args)
        {
            base.OnPaint(args);
            using (Pen pen = new Pen(BorderColor))
                if (BorderTopOnly) args.Graphics.DrawLine(pen, 0, 0, Width, 0);
                else if (BorderBottomOnly) args.Graphics.DrawLine(pen, 0, Height - 1, Width, Height - 1);
                else args.Graphics.DrawRectangle(pen, 0, 0, Math.Max(0, Width - 1), Math.Max(0, Height - 1));
        }
    }

    internal sealed class SetupButton : Button
    {
        internal bool Primary;
        private bool hover, down;
        internal SetupButton()
        {
            FlatStyle = FlatStyle.Flat; FlatAppearance.BorderSize = 0; Cursor = Cursors.Hand;
            UseVisualStyleBackColor = false;
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
            Font = SetupFonts.Create(9.5F, FontStyle.Regular);
        }
        private Color CanvasColor()
        {
            Control ancestor = Parent;
            while (ancestor != null)
            {
                Color color = ancestor.BackColor;
                if (color.A == 255) return color;
                ancestor = ancestor.Parent;
            }
            return SystemColors.Control;
        }
        protected override void OnPaintBackground(PaintEventArgs args)
        {
            // Owner-drawn Button buffers do not inherit a painted parent in a
            // real HWND repaint. Clear the full surface before rounding it.
            args.Graphics.Clear(CanvasColor());
        }
        protected override void OnMouseEnter(EventArgs args) { hover = true; base.OnMouseEnter(args); Invalidate(); }
        protected override void OnMouseLeave(EventArgs args) { hover = false; down = false; base.OnMouseLeave(args); Invalidate(); }
        protected override void OnMouseDown(MouseEventArgs args) { down = true; base.OnMouseDown(args); Invalidate(); }
        protected override void OnMouseUp(MouseEventArgs args) { down = false; base.OnMouseUp(args); Invalidate(); }
        protected override void OnEnabledChanged(EventArgs args) { base.OnEnabledChanged(args); Invalidate(); }
        protected override void OnPaint(PaintEventArgs args)
        {
            args.Graphics.Clear(CanvasColor());
            Color fill = Primary ? !Enabled ? Color.FromArgb(174, 190, 240) : down ? Color.FromArgb(35, 70, 210) : hover ? Color.FromArgb(40, 79, 230) : Color.FromArgb(53, 93, 251)
                : down ? Color.FromArgb(233, 238, 247) : hover ? Color.FromArgb(244, 247, 252) : Color.White;
            args.Graphics.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            Rectangle rect = new Rectangle(1, 1, Math.Max(1, Width - 3), Math.Max(1, Height - 3));
            int radius = Math.Max(4, (int)(6 * args.Graphics.DpiX / 96F));
            using (System.Drawing.Drawing2D.GraphicsPath path = new System.Drawing.Drawing2D.GraphicsPath())
            {
                int diameter = Math.Min(radius * 2, Math.Min(rect.Width, rect.Height));
                path.AddArc(rect.X, rect.Y, diameter, diameter, 180, 90); path.AddArc(rect.Right - diameter, rect.Y, diameter, diameter, 270, 90);
                path.AddArc(rect.Right - diameter, rect.Bottom - diameter, diameter, diameter, 0, 90); path.AddArc(rect.X, rect.Bottom - diameter, diameter, diameter, 90, 90); path.CloseFigure();
                using (Brush brush = new SolidBrush(fill)) args.Graphics.FillPath(brush, path);
                using (Pen pen = new Pen(Primary ? fill : Color.FromArgb(214, 223, 237))) args.Graphics.DrawPath(pen, path);
            }
            TextRenderer.DrawText(args.Graphics, Text, Font, rect, Primary ? Color.White : Enabled ? Color.FromArgb(75, 91, 116) : Color.FromArgb(158, 168, 186), TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.SingleLine);
            if (Focused && ShowFocusCues) ControlPaint.DrawFocusRectangle(args.Graphics, Rectangle.Inflate(rect, -4, -4), Primary ? Color.White : Color.FromArgb(53, 93, 251), fill);
        }
    }

    internal sealed class SetupGlyph : Control
    {
        internal string Kind = "brand";
        internal SetupGlyph() { SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.SupportsTransparentBackColor, true); BackColor = Color.Transparent; }
        protected override void OnPaint(PaintEventArgs args)
        {
            args.Graphics.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            if (Kind == "success")
            {
                using (Brush brush = new SolidBrush(Color.FromArgb(224, 243, 233))) args.Graphics.FillEllipse(brush, 0, 0, Width - 1, Height - 1);
                using (Pen pen = new Pen(Color.FromArgb(26, 136, 88), Math.Max(2, Width / 13F))) args.Graphics.DrawLines(pen, new PointF[] { new PointF(Width * .27F, Height * .51F), new PointF(Width * .43F, Height * .67F), new PointF(Width * .74F, Height * .34F) });
                return;
            }
            args.Graphics.ScaleTransform(Width / 40F, Height / 40F);
            using (Brush brush = new SolidBrush(Color.FromArgb(53, 93, 251))) args.Graphics.FillPolygon(brush, new PointF[] { new PointF(20, 2), new PointF(36, 11), new PointF(36, 29), new PointF(20, 38), new PointF(4, 29), new PointF(4, 11) });
            using (Brush white = new SolidBrush(Color.White))
            {
                args.Graphics.FillPolygon(white, new PointF[] { new PointF(18, 10), new PointF(9, 15), new PointF(9, 25), new PointF(18, 30), new PointF(18, 24), new PointF(14, 22), new PointF(14, 18), new PointF(18, 16) });
                args.Graphics.FillPolygon(white, new PointF[] { new PointF(22, 10), new PointF(22, 16), new PointF(26, 18), new PointF(26, 22), new PointF(22, 24), new PointF(22, 30), new PointF(31, 25), new PointF(31, 15) });
            }
        }
    }

    internal sealed class SetupProgress : Control
    {
        internal int Minimum = 0, Maximum = 100;
        private int value;
        internal int Value { get { return value; } set { this.value = Math.Max(Minimum, Math.Min(Maximum, value)); AccessibleName = "安装进度 " + this.value + "%"; Invalidate(); } }
        internal SetupProgress() { SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true); }
        protected override void OnPaint(PaintEventArgs args)
        {
            args.Graphics.Clear(Color.FromArgb(217, 226, 248));
            float portion = Maximum > Minimum ? (Value - Minimum) / (float)(Maximum - Minimum) : 0;
            using (Brush brush = new SolidBrush(Color.FromArgb(53, 93, 251))) args.Graphics.FillRectangle(brush, 0, 0, Width * portion, Height);
        }
    }
}
