using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

namespace CodeAgent.Installer
{
    internal sealed class RunningApplicationException : InvalidOperationException
    {
        internal readonly string Root;
        internal RunningApplicationException(string root, string message) : base(message) { Root = root; }
    }

    internal static class InstallCommon
    {
        internal const string AppId = "CodeAgent";
        internal const string RegistryPath = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\CodeAgent";
        internal const string ManifestName = "installation-manifest.json";
        internal static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 32 * 1024 * 1024 };

        internal static void EnableLongPaths()
        {
            // csc-built .NET Framework executables otherwise retain the old
            // 260-character compatibility limit even with longPathAware.
            AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling", false);
            AppContext.SetSwitch("Switch.System.IO.BlockLongPaths", false);
        }

        internal static string Canonical(string path)
        {
            string full = Path.GetFullPath(path);
            return full.Length > Path.GetPathRoot(full).Length ? full.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar) : full;
        }
        internal static bool Equal(string left, string right) { return String.Equals(Canonical(left), Canonical(right), StringComparison.OrdinalIgnoreCase); }
        internal static bool Inside(string path, string root) { return Canonical(path).StartsWith(Canonical(root).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase); }
        internal static string Quote(string value) { return "\"" + value.TrimEnd('\\').Replace("\"", "\\\"") + "\""; }

        internal static string Argument(string[] args, string name, string fallback)
        {
            for (int i = 0; i < args.Length; i++) if (args[i] == name)
            {
                if (i + 1 >= args.Length || args[i + 1].StartsWith("--", StringComparison.Ordinal)) throw new ArgumentException(name + " 缺少参数。");
                return args[i + 1];
            }
            return fallback;
        }

        internal static bool Has(string[] args, string name) { return Array.IndexOf(args, name) >= 0; }
        internal static void CheckArgs(string[] args, params string[] allowed)
        {
            HashSet<string> options = new HashSet<string>(allowed, StringComparer.Ordinal);
            for (int i = 0; i < args.Length; i++)
            {
                if (!options.Contains(args[i])) throw new ArgumentException("未知安装参数：" + args[i]);
                if (args[i] == "--dir" || args[i] == "--remove-root" || args[i] == "--wait-pid" || args[i] == "--result-file")
                {
                    if (++i >= args.Length || args[i].StartsWith("--", StringComparison.Ordinal)) throw new ArgumentException("安装参数缺少值。");
                }
            }
        }

        internal static void NoLinks(string path)
        {
            string current = Path.GetFullPath(path);
            while (!String.IsNullOrEmpty(current))
            {
                if ((Directory.Exists(current) || File.Exists(current)) && (File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                    throw new InvalidOperationException("安装路径不能包含链接或联接目录：" + current);
                string parent = Path.GetDirectoryName(current.TrimEnd('\\', '/'));
                if (String.IsNullOrEmpty(parent) || String.Equals(parent, current, StringComparison.OrdinalIgnoreCase)) break;
                current = parent;
            }
        }

        internal static string FilePath(string root, string relative)
        {
            if (String.IsNullOrWhiteSpace(relative) || Path.IsPathRooted(relative) || relative.IndexOf(':') >= 0)
                throw new InvalidOperationException("安装文件路径无效。");
            string[] pieces = relative.Replace('\\', '/').Split('/');
            if (pieces.Any(piece => piece == ".." || piece == "." || piece.Length == 0)) throw new InvalidOperationException("安装文件路径无效。");
            foreach (string piece in pieces)
            {
                string stem = piece.Split('.')[0].ToUpperInvariant();
                if (piece.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0 || piece.EndsWith(".", StringComparison.Ordinal) || piece.EndsWith(" ", StringComparison.Ordinal)
                    || new string[] { "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9" }.Contains(stem))
                    throw new InvalidOperationException("安装文件名无效。");
            }
            string path = Path.GetFullPath(Path.Combine(root, relative.Replace('/', Path.DirectorySeparatorChar)));
            if (!Inside(path, root)) throw new InvalidOperationException("安装文件超出目标目录。");
            NoLinks(Path.GetDirectoryName(path));
            return path;
        }

        internal static string ValidateTarget(string target)
        {
            if (!Path.IsPathRooted(target)) throw new InvalidOperationException("请选择完整的安装路径。");
            target = Canonical(target);
            string drive = Canonical(Path.GetPathRoot(target));
            string profile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            string data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Code-agent", "data");
            if (Equal(target, drive) || Equal(target, profile) || Equal(target, Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData))
                || Equal(target, Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles))
                || Equal(target, Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86))
                || Equal(target, Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory))
                || Equal(target, Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments))
                || Equal(target, Environment.GetFolderPath(Environment.SpecialFolder.Windows))
                || Inside(target, Environment.GetFolderPath(Environment.SpecialFolder.Windows))
                || Equal(target, data) || Inside(data, target) || Inside(target, data))
                throw new InvalidOperationException("该目录不能用作应用安装目录，请选择独立的 Code-agent 文件夹。");
            NoLinks(target);
            return target;
        }

        internal static Dictionary<string, object> ReadManifest(string root)
        {
            string path = Path.Combine(root, ManifestName);
            if (!File.Exists(path) || new FileInfo(path).Length > 32 * 1024 * 1024) throw new InvalidOperationException("未找到有效的 Code-agent 安装清单。");
            Dictionary<string, object> manifest = Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(path, Encoding.UTF8));
            object value;
            if (!manifest.TryGetValue("appId", out value) || Convert.ToString(value) != AppId
                || !manifest.TryGetValue("schemaVersion", out value) || Convert.ToInt32(value) != 1
                || !manifest.TryGetValue("installRoot", out value) || !Equal(Convert.ToString(value), root)
                || !manifest.ContainsKey("files")) throw new InvalidOperationException("Code-agent 安装身份校验失败。");
            return manifest;
        }

        internal static void EnsureIdle(string root)
        {
            foreach (Process process in Process.GetProcesses())
            {
                try
                {
                    if (process.Id == Process.GetCurrentProcess().Id) continue;
                    ProcessModule module = process.MainModule;
                    if (module == null) continue;
                    string executable = module.FileName;
                    if (Inside(executable, root)) throw new RunningApplicationException(root, "检测到旧版后台服务仍在运行。可点击“停止后台并重试”安全停止服务后继续；正在执行的任务会受到保护。");
                }
                catch (System.ComponentModel.Win32Exception) { }
                catch (RunningApplicationException) { throw; }
                catch (InvalidOperationException error)
                {
                    if (error.Message.StartsWith("Code-agent", StringComparison.Ordinal)) throw;
                }
                finally { process.Dispose(); }
            }
        }

        internal static Func<string[]> ShortcutLocations = DefaultShortcutPaths;

        internal static string[] ShortcutPaths() { return ShortcutLocations(); }

        internal static string MainExecutable(string root) { return Path.Combine(root, File.Exists(Path.Combine(root, "Code-agent.exe")) ? "Code-agent.exe" : "Code-agent Launcher.exe"); }
        private static bool OwnExecutable(string path, string root) { return Equal(path, Path.Combine(root, "Code-agent.exe")) || Equal(path, Path.Combine(root, "Code-agent Launcher.exe")); }

        private static string[] DefaultShortcutPaths()
        {
            return new string[] {
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "Code-agent.lnk"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), "Code-agent", "Code-agent.lnk")
            };
        }

        internal static object Shell() { return Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell", true)); }
        internal static object Shortcut(object shell, string path) { return shell.GetType().InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { path }); }
        internal static void Set(object target, string name, object value) { target.GetType().InvokeMember(name, BindingFlags.SetProperty, null, target, new object[] { value }); }
        internal static object Get(object target, string name) { return target.GetType().InvokeMember(name, BindingFlags.GetProperty, null, target, null); }
        internal static void Release(object value) { if (value != null && System.Runtime.InteropServices.Marshal.IsComObject(value)) System.Runtime.InteropServices.Marshal.FinalReleaseComObject(value); }

        internal static void CreateShortcuts(string root)
        {
            object shell = null;
            try
            {
                shell = Shell();
                foreach (string path in ShortcutPaths())
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(path));
                    object link = null;
                    try
                    {
                        link = Shortcut(shell, path);
                        if (File.Exists(path))
                        {
                            string current = Convert.ToString(Get(link, "TargetPath"));
                            if (!String.IsNullOrEmpty(current) && !OwnExecutable(current, root))
                                throw new InvalidOperationException("发现同名的其他快捷方式，未覆盖：" + path);
                        }
                        Set(link, "TargetPath", MainExecutable(root));
                        Set(link, "WorkingDirectory", root);
                        Set(link, "Description", "Code-agent 编码智能体");
                        Set(link, "IconLocation", MainExecutable(root) + ",0");
                        link.GetType().InvokeMember("Save", BindingFlags.InvokeMethod, null, link, null);
                    }
                    finally { Release(link); }
                }
            }
            finally { Release(shell); }
        }

        internal static void Register(string root, string version, long bytes)
        {
            using (RegistryKey key = Registry.CurrentUser.CreateSubKey(RegistryPath))
            {
                key.SetValue("DisplayName", "Code-agent"); key.SetValue("DisplayVersion", version); key.SetValue("Publisher", "Code-agent");
                key.SetValue("InstallLocation", root); key.SetValue("DisplayIcon", MainExecutable(root));
                key.SetValue("UninstallString", Quote(Path.Combine(root, "Code-agent Uninstall.exe")));
                key.SetValue("QuietUninstallString", Quote(Path.Combine(root, "Code-agent Uninstall.exe")) + " --silent");
                key.SetValue("EstimatedSize", (int)Math.Min(Int32.MaxValue, bytes / 1024), RegistryValueKind.DWord);
                key.SetValue("NoModify", 1, RegistryValueKind.DWord); key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
                key.SetValue("URLInfoAbout", "https://github.com/luyiyanyumu/Code-agent");
            }
        }

        internal static void RemoveRegistration(string root)
        {
            using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RegistryPath))
            {
                if (key != null && Equal(Convert.ToString(key.GetValue("InstallLocation", "")), root)) Registry.CurrentUser.DeleteSubKeyTree(RegistryPath, false);
            }
            object shell = null;
            try
            {
                shell = Shell();
                foreach (string path in ShortcutPaths()) if (File.Exists(path))
                {
                    object link = null;
                    try { link = Shortcut(shell, path); if (OwnExecutable(Convert.ToString(Get(link, "TargetPath")), root)) File.Delete(path); }
                    finally { Release(link); }
                }
            }
            finally { Release(shell); }
        }

        internal static void RemoveOwnedFiles(string root, Dictionary<string, object> manifest)
        {
            HashSet<string> files = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            System.Collections.IEnumerable listed = manifest["files"] as System.Collections.IEnumerable;
            if (listed == null) throw new InvalidOperationException("安装文件清单无效。");
            foreach (object entry in listed)
            {
                string path = FilePath(root, Convert.ToString(entry));
                if (!files.Add(path)) throw new InvalidOperationException("安装文件清单包含重复项。");
                if (File.Exists(path) && (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0) throw new InvalidOperationException("安装文件被替换为链接，已停止卸载。");
            }
            HashSet<string> directories = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (string path in files)
            {
                if (File.Exists(path)) File.Delete(path);
                string directory = Path.GetDirectoryName(path);
                while (Inside(directory, root)) { directories.Add(directory); directory = Path.GetDirectoryName(directory); }
            }
            foreach (string directory in directories.OrderByDescending(value => value.Length))
                if (Directory.Exists(directory) && Directory.GetFileSystemEntries(directory).Length == 0) Directory.Delete(directory);
            File.Delete(Path.Combine(root, ManifestName));
            if (Directory.Exists(root) && Directory.GetFileSystemEntries(root).Length == 0) Directory.Delete(root);
        }

        internal static void Result(string path, bool ok, string message)
        {
            if (String.IsNullOrEmpty(path)) return;
            Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path)));
            File.WriteAllText(path, Json.Serialize(new Dictionary<string, object> { { "ok", ok }, { "message", message } }), Encoding.UTF8);
        }
    }

    internal static class UninstallProgram
    {
        [STAThread]
        private static int Main(string[] args)
        {
            InstallCommon.EnableLongPaths();
            bool silent = InstallCommon.Has(args, "--silent");
            string resultFile = null;
            try
            {
                resultFile = InstallCommon.Argument(args, "--result-file", null);
                InstallCommon.CheckArgs(args, "--silent", "--remove-root", "--wait-pid", "--result-file");
                string removeRoot = InstallCommon.Argument(args, "--remove-root", null);
                string root = InstallCommon.ValidateTarget(removeRoot ?? AppDomain.CurrentDomain.BaseDirectory);
                Dictionary<string, object> manifest = InstallCommon.ReadManifest(root);
                if (removeRoot == null)
                {
                    if (!silent && MessageBox.Show("卸载 Code-agent？\r\n\r\n应用文件和快捷方式会移除，会话、模型配置和项目文件会保留。请先退出应用并停止保留的后台服务。", "卸载 Code-agent", MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2) != DialogResult.Yes) return 0;
                    InstallCommon.EnsureIdle(root);
                    string temporary = Path.Combine(Path.GetTempPath(), "Code-agent-uninstall-" + Guid.NewGuid().ToString("N"));
                    Directory.CreateDirectory(temporary);
                    string helper = Path.Combine(temporary, "Code-agent Uninstall.exe");
                    File.Copy(Assembly.GetExecutingAssembly().Location, helper);
                    ProcessStartInfo start = new ProcessStartInfo
                    {
                        FileName = helper, WorkingDirectory = temporary, UseShellExecute = false, CreateNoWindow = true,
                        Arguments = "--remove-root " + InstallCommon.Quote(root) + " --wait-pid " + Process.GetCurrentProcess().Id + (silent ? " --silent" : "")
                            + (resultFile == null ? "" : " --result-file " + InstallCommon.Quote(Path.GetFullPath(resultFile)))
                    };
                    Process.Start(start);
                    return 0;
                }
                int waitPid;
                if (Int32.TryParse(InstallCommon.Argument(args, "--wait-pid", "0"), out waitPid) && waitPid > 0)
                {
                    try { using (Process parent = Process.GetProcessById(waitPid)) if (!parent.WaitForExit(15000)) throw new InvalidOperationException("卸载程序尚未退出，请稍后重试。"); }
                    catch (ArgumentException) { }
                }
                InstallCommon.EnsureIdle(root);
                InstallCommon.RemoveOwnedFiles(root, manifest);
                InstallCommon.RemoveRegistration(root);
                InstallCommon.Result(resultFile, true, "Code-agent 已卸载，用户数据已保留。");
                if (!silent) MessageBox.Show("Code-agent 已卸载。\r\n\r\n会话、模型配置和项目文件已保留。", "Code-agent", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return 0;
            }
            catch (Exception error)
            {
                InstallCommon.Result(resultFile, false, error.Message);
                if (!silent) MessageBox.Show(error.Message, "无法卸载 Code-agent", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }
        }
    }
}
