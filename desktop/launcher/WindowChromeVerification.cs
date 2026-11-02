// Own-form native geometry/controls regression. Does not launch services, touch
// user profiles, or target any window outside this disposable fixture.
using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace CodeAgent.DesktopLauncher
{
    internal static class WindowChromeVerification
    {
        [DllImport("user32.dll")] private static extern IntPtr GetWindowLongPtr(IntPtr handle, int index);
        [DllImport("user32.dll")] private static extern IntPtr SendMessage(IntPtr handle, int message, IntPtr wParam, IntPtr lParam);
        private static readonly BindingFlags Hidden = BindingFlags.Instance | BindingFlags.NonPublic;
        private static readonly List<object> rows = new List<object>();
        private static string output;
        private static bool passed;

        [STAThread]
        private static int Main(string[] args)
        {
            output = Path.GetFullPath(args[0]); Directory.CreateDirectory(output);
            AppDomain.CurrentDomain.AssemblyResolve += delegate(object sender, ResolveEventArgs request) {
                string name = new AssemblyName(request.Name).Name;
                return name == "Microsoft.Web.WebView2.Core" || name == "Microsoft.Web.WebView2.WinForms" ? Assembly.LoadFrom(Path.Combine(args[1], name + ".dll")) : null;
            };
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            int stage = 0, exitCalls = 0;
            Rectangle normal = Rectangle.Empty;
            Point dragged = Point.Empty;
            int originalCaption = 0, originalSide = 0;
            using (EventWaitHandle activation = new EventWaitHandle(false, EventResetMode.AutoReset))
            using (LauncherWindow form = new LauncherWindow(output, "chrome-fixture", activation, null, null, 0, false))
            using (System.Windows.Forms.Timer inspect = new System.Windows.Forms.Timer { Interval = 140 })
            {
                form.StartPosition = FormStartPosition.Manual;
                Rectangle work = Screen.PrimaryScreen.WorkingArea;
                form.Location = new Point(work.Left + 30, work.Top + 30);
                form.Size = new Size(Math.Min(1050, work.Width - 60), Math.Min(720, work.Height - 60));
                form.ShowInTaskbar = false;
                form.CloseScriptOverride = delegate { return Task.FromResult("false"); };
                form.CloseExitOverride = delegate { exitCalls++; return Task.FromResult(new Dictionary<string, object>()); };
                form.CloseDecisionOverride = delegate { throw new Exception("A clean close must not need confirmation"); };
                inspect.Tick += delegate {
                    try
                    {
                        if (stage == 0)
                        {
                            Point client = form.PointToScreen(Point.Empty);
                            originalCaption = client.Y - form.Top; originalSide = client.X - form.Left;
                            Check(originalCaption > originalSide + 10, "placeholder-retains-system-caption", new { originalCaption, originalSide });
                            // Unsupported WebView2 retains the standard frame.
                            form.SetIntegratedFrame(true);
                            Check(!(bool)form.WindowChromeState()["integratedFrame"], "unsupported-runtime-retains-system-frame", null);
                            typeof(LauncherWindow).GetField("nativeDragRegions", Hidden).SetValue(form, true);
                            form.SetIntegratedFrame(true);
                            stage++; return;
                        }
                        if (stage == 1)
                        {
                            Point client = form.PointToScreen(Point.Empty);
                            int top = client.Y - form.Top, side = client.X - form.Left;
                            uint style = unchecked((uint)GetWindowLongPtr(form.Handle, -16).ToInt64());
                            Check(top == 0 && side > 0 && Math.Abs(side - originalSide) <= 2, "no-restored-top-band-native-side-border-retained", new { top, side, originalCaption, originalSide });
                            Check((style & 0x00040000) != 0 && (style & 0x00010000) != 0 && (style & 0x00080000) != 0,
                                "size-maximize-and-system-menu-styles-retained", new { style });
                            Check((style & 0x00C00000) == 0, "integrated-native-style-has-no-system-caption", null);
                            // The left sizing border is still native nonclient.
                            Point probe = new Point(form.Left + 1, form.Top + form.Height / 2);
                            IntPtr point = new IntPtr((probe.Y << 16) | (probe.X & 0xffff));
                            int hit = SendMessage(form.Handle, 0x0084, IntPtr.Zero, point).ToInt32();
                            Check(hit >= 10 && hit <= 17, "native-edge-hit-testing-retained", new { hit });
                            foreach (int edge in new int[] { 12, 13, 14 })
                            {
                                int x = edge == 13 ? form.Left + 1 : edge == 14 ? form.Right - 2 : form.Left + form.Width / 2;
                                Point topProbe = new Point(x, form.Top + 1);
                                int topHit = SendMessage(form.Handle, 0x0084, IntPtr.Zero, new IntPtr((topProbe.Y << 16) | (topProbe.X & 0xffff))).ToInt32();
                                Check(topHit == edge, "restored-top-resize-hit-" + edge, new { topHit, dpi = form.DeviceDpi });
                            }
                            foreach (int dpi in new int[] { 96, 120, 144, 192 })
                            {
                                Rectangle negative = new Rectangle(-1200, -800, 900, 600);
                                int grip = (int)Math.Ceiling(8.0 * dpi / 96.0);
                                Check(LauncherWindow.IntegratedTopResizeHit(negative, new Point(-800, -800 + grip - 1), dpi) == 12
                                    && LauncherWindow.IntegratedTopResizeHit(negative, new Point(-800, -800 + grip), dpi) == 0,
                                    "dpi-scaled-top-grip-" + dpi, new { dpi, grip });
                            }
                            normal = form.Bounds;
                            form.WindowChromeAction("window-toggle-maximize");
                            stage++; return;
                        }
                        if (stage == 2)
                        {
                            Rectangle area = Screen.FromControl(form).WorkingArea;
                            Rectangle visible = form.RectangleToScreen(form.ClientRectangle);
                            Check(form.WindowState == FormWindowState.Maximized && visible == area && form.ClientSize == area.Size,
                                "maximized-visible-client-matches-working-area", new { bounds = form.Bounds, client = visible, area });
                            Check((bool)form.WindowChromeState()["maximized"], "maximized-state-reply", null);
                            form.WindowChromeAction("window-toggle-maximize"); stage++; return;
                        }
                        if (stage == 3)
                        {
                            Check(form.WindowState == FormWindowState.Normal && form.Bounds == normal, "restore-retains-normal-bounds", new { normal, actual = form.Bounds });
                            for (int cycle = 0; cycle < 3; cycle++)
                            {
                                SendMessage(form.Handle, 0x0112, new IntPtr(0xf030), IntPtr.Zero); // SC_MAXIMIZE
                                SendMessage(form.Handle, 0x0112, new IntPtr(0xf120), IntPtr.Zero); // SC_RESTORE
                                Check(form.Bounds == normal && form.PointToScreen(Point.Empty).Y == form.Top,
                                    "system-menu-restore-without-size-drift-" + cycle, new { actual = form.Bounds });
                            }
                            form.WindowChromeAction("window-minimize"); stage++; return;
                        }
                        if (stage == 4)
                        {
                            Check(form.WindowState == FormWindowState.Minimized && (bool)form.WindowChromeState()["minimized"], "minimize-state-reply", null);
                            form.WindowState = FormWindowState.Normal; stage++; return;
                        }
                        if (stage == 5)
                        {
                            Check(form.Bounds == normal, "restore-after-minimize-retains-bounds", null);
                            form.WindowChromeAction("window-toggle-maximize"); stage++; return;
                        }
                        if (stage == 6)
                        {
                            // Native move starts, Windows restores and chooses
                            // the cursor-anchored location, then the move ends.
                            // Subsequent host callbacks must not alter it.
                            SendMessage(form.Handle, 0x0231, IntPtr.Zero, IntPtr.Zero);
                            form.WindowState = FormWindowState.Normal;
                            dragged = new Point(work.Left + 140, work.Top + 150);
                            form.Location = dragged;
                            SendMessage(form.Handle, 0x0232, IntPtr.Zero, IntPtr.Zero);
                            stage++; return;
                        }
                        if (stage == 7)
                        {
                            Check(form.Location == dragged && form.Size == normal.Size,
                                "native-drag-restore-retains-system-position-after-move-end", new { dragged, actual = form.Bounds, expectedSize = normal.Size });
                            form.SetIntegratedFrame(false);
                            Point client = form.PointToScreen(Point.Empty);
                            Check(client.Y - form.Top == originalCaption, "failure-placeholder-can-restore-system-caption", null);
                            typeof(LauncherWindow).GetField("workbenchReady", Hidden).SetValue(form, true);
                            inspect.Stop();
                            form.WindowChromeAction("window-close");
                            stage++; return;
                        }
                    }
                    catch (Exception error)
                    {
                        rows.Add(new { passed = false, error = error.ToString() });
                        inspect.Stop(); typeof(LauncherWindow).GetField("permitClose", Hidden).SetValue(form, true); form.Close();
                    }
                };
                form.Shown += delegate { inspect.Start(); };
                Application.Run(form);
                if (stage == 8) { Check(exitCalls == 1, "window-close-uses-existing-exit-pipeline-once", new { exitCalls }); passed = true; }
            }
            File.WriteAllText(Path.Combine(output, "report.json"), new JavaScriptSerializer().Serialize(new { passed, scenarios = rows }), new System.Text.UTF8Encoding(false));
            return passed ? 0 : 1;
        }

        private static void Check(bool condition, string name, object evidence)
        {
            rows.Add(new { scenario = name, passed = condition, evidence });
            if (!condition) throw new Exception(name);
        }
    }
}
