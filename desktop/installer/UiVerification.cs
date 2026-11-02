// UI-only fixture. It renders the real setup controls without running installation.
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Windows.Forms;
using System.Web.Script.Serialization;

namespace CodeAgent.Installer
{
    internal static class InstallerUiVerification
    {
        [STAThread]
        private static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            string output = Path.GetFullPath(args[0]);
            Directory.CreateDirectory(output);
            List<object> results = new List<object>();
            bool valid = true;
            foreach (int dpi in new int[] { 96, 144, 192 })
            foreach (string state in new string[] { "ready", "running", "success", "error" })
            using (SetupWindow form = new SetupWindow(@"C:\Users\演示用户\AppData\Local\Programs\Code-agent", true, null))
            {
                form.ApplyPreviewState(state, 46);
                form.StartPosition = FormStartPosition.Manual;
                form.Location = new Point(-30000, -30000);
                form.ShowInTaskbar = false;
                form.Show();
                Application.DoEvents();
                float currentDpi;
                using (Graphics graphics = form.CreateGraphics()) currentDpi = graphics.DpiX;
                List<Control> controls = Walk(form).ToList();
                Dictionary<Control, Font> fonts = controls.ToDictionary(control => control, control => control.Font);
                form.AutoScaleMode = AutoScaleMode.None;
                form.MinimumSize = Size.Empty;
                form.MaximumSize = Size.Empty;
                float ratio = dpi / currentDpi;
                form.Scale(new SizeF(ratio, ratio));
                foreach (Control control in controls)
                {
                    Font original = fonts[control];
                    control.Font = new Font(original.FontFamily, original.SizeInPoints * dpi / 72f, original.Style, GraphicsUnit.Pixel);
                }
                form.PerformLayout();
                Application.DoEvents();
                List<object> important = new List<object>();
                List<string> failures = new List<string>();
                foreach (Control control in Walk(form))
                {
                    if (String.IsNullOrEmpty(control.Name) || !control.Visible) continue;
                    if (control.Parent != null && (control.Left < -2 || control.Top < -2 || control.Right > control.Parent.ClientSize.Width + 2 || control.Bottom > control.Parent.ClientSize.Height + 2))
                        failures.Add(control.Name + " exceeds its parent");
                    if (control.Name == "feedback-title" || control.Name == "install-status")
                    {
                        Size textSize = TextRenderer.MeasureText(control.Text, control.Font, new Size(Math.Max(1, control.Width), Int32.MaxValue), TextFormatFlags.WordBreak);
                        if (textSize.Height > control.Height + 2) failures.Add(control.Name + " clips its text");
                        if (control.Name == "feedback-title" && control.Height > control.Font.Height * 1.7F) failures.Add(control.Name + " wraps its heading");
                    }
                    if (control.Name == "setup-title" || control.Name == "install-path" || control.Name == "path-card" || control.Name == "progress-section" || control.Name == "start-install" || control.Name == "cancel-install")
                        important.Add(new { name = control.Name, bounds = control.Bounds.ToString(), font = control.Font.Name, pixels = control.Font.Size, enabled = control.Enabled, text = control.Text });
                }
                if (failures.Count != 0) valid = false;
                string path = Path.Combine(output, "installer-" + state + "-" + dpi + ".png");
                using (Bitmap image = new Bitmap(form.Width, form.Height))
                {
                    form.DrawToBitmap(image, new Rectangle(Point.Empty, image.Size));
                    image.Save(path, ImageFormat.Png);
                }
                results.Add(new { state = state, dpi = dpi, nativeDpi = currentDpi, size = form.ClientSize.ToString(), failures = failures, controls = important, screenshot = path, previewOnly = true });
                form.Close();
            }
            File.WriteAllText(Path.Combine(output, "layout-report.json"), new JavaScriptSerializer { MaxJsonLength = 1024 * 1024 }.Serialize(results));
            return valid ? 0 : 1;
        }

        private static IEnumerable<Control> Walk(Control root)
        {
            yield return root;
            foreach (Control child in root.Controls)
                foreach (Control descendant in Walk(child)) yield return descendant;
        }
    }
}
