// Runs the actual built installer assembly in a real WinForms event loop.
// The explicit target is installed/upgraded; this fixture never uninstalls it.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Windows.Forms;
using System.Web.Script.Serialization;
using Microsoft.Win32;

internal static class InstallerInstallationVerification
{
    private static volatile bool finished;
    [DllImport("advapi32.dll")] private static extern int RegOverridePredefKey(IntPtr key, IntPtr replacement);
    [STAThread]
    private static int Main(string[] args)
    {
        // This fixture loads the installer by reflection, bypassing its Main.
        AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling", false);
        AppContext.SetSwitch("Switch.System.IO.BlockLongPaths", false);
        try { return Execute(args); }
        catch (Exception error)
        {
            if (args.Length >= 3)
            {
                Directory.CreateDirectory(args[2]);
                File.WriteAllText(Path.Combine(args[2], "report.json"), new JavaScriptSerializer().Serialize(new { passed = false, error = error.ToString() }));
            }
            return 1;
        }
        finally { finished = true; }
    }

    private static int Execute(string[] args)
    {
        string installer = Path.GetFullPath(args[0]), target = Path.GetFullPath(args[1]), output = Path.GetFullPath(args[2]);
        Directory.CreateDirectory(output);
        // The override belongs only to this process. The product's real registry
        // and shortcuts remain in use by the user's running installation.
        string hivePath = @"Software\CodeAgent\InstallerVerification\" + Guid.NewGuid().ToString("N");
        using (RegistryKey isolatedHive = Registry.CurrentUser.CreateSubKey(hivePath))
        {
        foreach (string shellKey in new string[] { @"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders", @"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders" })
        using (RegistryKey folders = isolatedHive.CreateSubKey(shellKey))
            foreach (string name in new string[] { "Desktop", "Programs", "Local AppData", "AppData", "Personal" })
            {
                string folder = Path.Combine(output, "isolated-shell", name.Replace(' ', '-'));
                Directory.CreateDirectory(folder); folders.SetValue(name, folder, RegistryValueKind.String);
            }
        IntPtr currentUser = new IntPtr(unchecked((int)0x80000001));
        int overrideError = RegOverridePredefKey(currentUser, isolatedHive.Handle.DangerousGetHandle());
        if (overrideError != 0) throw new System.ComponentModel.Win32Exception(overrideError);
        try
        {
        JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 32 * 1024 * 1024 };
        string resultFile = Path.Combine(output, "install-result.json");
        if (File.Exists(resultFile)) throw new InvalidOperationException("Use a fresh verification output directory.");
        Assembly product = Assembly.LoadFrom(installer);
        Type commonType = product.GetType("CodeAgent.Installer.InstallCommon", true);
        string shortcutRoot = Path.Combine(output, "isolated-shortcuts");
        Directory.CreateDirectory(shortcutRoot);
        commonType.GetField("ShortcutLocations", BindingFlags.Static | BindingFlags.NonPublic).SetValue(null,
            (Func<string[]>)(delegate { return new string[] { Path.Combine(shortcutRoot, "Desktop", "Code-agent.lnk"), Path.Combine(shortcutRoot, "Programs", "Code-agent.lnk") }; }));
        string[] shortcuts = (string[])commonType.GetMethod("ShortcutPaths", BindingFlags.Static | BindingFlags.NonPublic).Invoke(null, null);
        foreach (string shortcut in shortcuts)
            if (!Path.GetFullPath(shortcut).StartsWith(output.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("Shortcut isolation failed; no installation was started: " + shortcut);
        Type windowType = product.GetType("CodeAgent.Installer.SetupWindow", true);
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        Control.CheckForIllegalCrossThreadCalls = true;
        Stopwatch elapsed = Stopwatch.StartNew();
        Dictionary<string, object> report = new Dictionary<string, object> { { "passed", false }, { "installer", installer }, { "target", target }, { "actualBuiltAssembly", true }, { "processIsolatedRegistry", hivePath }, { "isolatedShortcuts", shortcuts } };
        if (!target.StartsWith(Path.GetDirectoryName(output).TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase) || Directory.Exists(target))
            throw new InvalidOperationException("Verification requires a fresh sibling target in the output's parent directory.");
        Task.Run(delegate {
            System.Threading.Thread.Sleep(210000);
            if (finished) return;
            File.WriteAllText(Path.Combine(output, "report.json"), json.Serialize(new { passed = false, error = "Isolated installation fixture exceeded 210 seconds; only its own process was stopped.", target = target }));
            Environment.Exit(3);
        });
        using (Form form = (Form)Activator.CreateInstance(windowType, BindingFlags.Instance | BindingFlags.NonPublic, null, new object[] { target, false, resultFile }, null))
        {
            form.StartPosition = FormStartPosition.Manual; form.Location = new Point(-30000, -30000); form.ShowInTaskbar = false;
            Button start = (Button)form.Controls.Find("start-install", true)[0];
            Button cancel = (Button)form.Controls.Find("cancel-install", true)[0];
            Control title = form.Controls.Find("setup-title", true)[0], status = form.Controls.Find("install-status", true)[0];
            Control progress = form.Controls.Find("install-progress", true)[0];
            FieldInfo runningField = windowType.GetField("running", BindingFlags.Instance | BindingFlags.NonPublic);
            FieldInfo taskField = windowType.GetField("installTask", BindingFlags.Instance | BindingFlags.NonPublic);
            int heartbeats = 0, statusChanges = 0; long previous = 0, maximumGap = 0; bool started = false, timeout = false;
            status.TextChanged += delegate { statusChanges++; };
            System.Windows.Forms.Timer observe = new System.Windows.Forms.Timer { Interval = 200 };
            Action finish = delegate {
                observe.Stop();
                report["elapsedMilliseconds"] = elapsed.ElapsedMilliseconds;
                report["heartbeats"] = heartbeats; report["maximumHeartbeatGapMilliseconds"] = maximumGap;
                report["statusChanges"] = statusChanges; report["finalTitle"] = title.Text; report["finalButton"] = start.Text;
                report["buttonEnabled"] = start.Enabled; report["running"] = runningField.GetValue(form);
                using (Bitmap bitmap = new Bitmap(form.Width, form.Height)) { form.DrawToBitmap(bitmap, new Rectangle(Point.Empty, bitmap.Size)); bitmap.Save(Path.Combine(output, "completed.png"), ImageFormat.Png); }
                File.WriteAllText(Path.Combine(output, "report.json"), json.Serialize(report), new System.Text.UTF8Encoding(false));
                // Only this test window can be closed, and only after its worker
                // has finished. A failed assertion must not leave a frozen form.
                runningField.SetValue(form, false); form.Close();
            };
            observe.Tick += delegate {
                if (!started) return;
                heartbeats++; long now = elapsed.ElapsedMilliseconds; maximumGap = Math.Max(maximumGap, now - previous); previous = now;
                Task task = taskField.GetValue(form) as Task;
                if (now > 180000) timeout = true;
                bool busy = (bool)runningField.GetValue(form);
                if (!busy && start.Enabled)
                {
                    try {
                        if (timeout) throw new TimeoutException("Real GUI installation exceeded 180 seconds.");
                        if (title.Text != "安装完成" || start.Text != "完成" || !cancel.Enabled || progress.Visible)
                            throw new InvalidOperationException("Completion controls did not reach their final state: " + status.Text);
                        Dictionary<string, object> result = json.Deserialize<Dictionary<string, object>>(File.ReadAllText(resultFile));
                        if (!Convert.ToBoolean(result["ok"])) throw new InvalidOperationException("Installer returned failure.");
                        Dictionary<string, object> manifest = json.Deserialize<Dictionary<string, object>>(File.ReadAllText(Path.Combine(target, "installation-manifest.json")));
                        System.Collections.ICollection files = manifest["files"] as System.Collections.ICollection;
                        if (files == null || files.Count < 1000 || (!File.Exists(Path.Combine(target, "Code-agent.exe")) && !File.Exists(Path.Combine(target, "Code-agent Launcher.exe")))) throw new InvalidOperationException("Real payload is incomplete.");
                        using (RegistryKey registration = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\CodeAgent"))
                            if (registration == null || !String.Equals(Convert.ToString(registration.GetValue("InstallLocation")), target, StringComparison.OrdinalIgnoreCase))
                                throw new InvalidOperationException("Isolated registration was not created for this target.");
                        foreach (string shortcut in shortcuts) if (!File.Exists(shortcut)) throw new InvalidOperationException("Isolated shortcut was not created.");
                        object shell = commonType.GetMethod("Shell", BindingFlags.Static | BindingFlags.NonPublic).Invoke(null, null);
                        List<string> destinations = new List<string>();
                        try {
                            foreach (string shortcut in shortcuts) {
                                object link = commonType.GetMethod("Shortcut", BindingFlags.Static | BindingFlags.NonPublic).Invoke(null, new object[] { shell, shortcut });
                                try {
                                    string destination = Convert.ToString(commonType.GetMethod("Get", BindingFlags.Static | BindingFlags.NonPublic).Invoke(null, new object[] { link, "TargetPath" }));
                                    string expected = Path.Combine(target, File.Exists(Path.Combine(target, "Code-agent.exe")) ? "Code-agent.exe" : "Code-agent Launcher.exe");
                                    if (!String.Equals(destination, expected, StringComparison.OrdinalIgnoreCase)) throw new InvalidOperationException("Shortcut does not open the desktop application.");
                                    destinations.Add(destination);
                                } finally { commonType.GetMethod("Release", BindingFlags.Static | BindingFlags.NonPublic).Invoke(null, new object[] { link }); }
                            }
                        } finally { commonType.GetMethod("Release", BindingFlags.Static | BindingFlags.NonPublic).Invoke(null, new object[] { shell }); }
                        report["shortcutTargets"] = destinations;
                        report["files"] = files.Count; report["version"] = manifest["version"]; report["passed"] = true;
                    } catch (Exception error) { report["error"] = error.ToString(); }
                    finish();
                }
                else if (timeout && (task == null || task.IsCompleted)) { report["error"] = "Worker completed but UI stayed busy for 180 seconds."; finish(); }
            };
            form.Shown += delegate { form.BeginInvoke((Action)delegate { elapsed.Restart(); previous = 0; started = true; observe.Start(); start.PerformClick(); }); };
            Application.Run(form); observe.Dispose();
        }
        return Convert.ToBoolean(report["passed"]) ? 0 : 1;
        }
        finally { RegOverridePredefKey(currentUser, IntPtr.Zero); }
        }
    }
}
