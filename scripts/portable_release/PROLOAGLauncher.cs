using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

namespace PROLOAGLauncher
{
    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            string releaseDir = AppDomain.CurrentDomain.BaseDirectory;
            string logDir = Path.Combine(releaseDir, "logs");
            string logPath = Path.Combine(logDir, "launcher_" + DateTime.Now.ToString("yyyyMMdd") + ".log");

            try
            {
                Directory.CreateDirectory(logDir);
                Log(logPath, "Launcher started.");

                string pythonExe = Path.Combine(releaseDir, "runtime", "python311", "python.exe");
                string mainPy = Path.Combine(releaseDir, "app", "main.py");
                string launcherScript = Path.Combine(releaseDir, "run_portable_app.ps1");

                AssertFile(pythonExe, "Bundled Python was not found.");
                AssertFile(mainPy, "Streamlit app entry point was not found.");
                AssertFile(launcherScript, "Portable startup script was not found.");

                if (IsCheckMode(args))
                {
                    Log(logPath, "Launcher check completed.");
                    return 0;
                }

                string powershellExe = ResolvePowerShell();
                var startInfo = new ProcessStartInfo
                {
                    FileName = powershellExe,
                    Arguments = "-NoProfile -ExecutionPolicy Bypass -NoExit -File " + Quote(launcherScript),
                    WorkingDirectory = releaseDir,
                    UseShellExecute = true,
                    WindowStyle = ProcessWindowStyle.Normal
                };

                Process.Start(startInfo);
                Log(logPath, "PowerShell startup script launched.");
                return 0;
            }
            catch (Exception ex)
            {
                try
                {
                    Directory.CreateDirectory(logDir);
                    Log(logPath, "Launcher failed.");
                    Log(logPath, ex.ToString());
                }
                catch
                {
                }

                MessageBox.Show(
                    "Startup failed. Please check the logs folder next to this launcher.",
                    "PROLOAG Launcher",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
                return 1;
            }
        }

        private static bool IsCheckMode(string[] args)
        {
            if (args == null)
            {
                return false;
            }

            foreach (string arg in args)
            {
                if (string.Equals(arg, "--check", StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }

            return false;
        }

        private static void AssertFile(string path, string message)
        {
            if (!File.Exists(path))
            {
                throw new FileNotFoundException(message, path);
            }
        }

        private static string ResolvePowerShell()
        {
            string systemDir = Environment.GetFolderPath(Environment.SpecialFolder.System);
            string windowsPowerShell = Path.Combine(systemDir, "WindowsPowerShell", "v1.0", "powershell.exe");
            if (File.Exists(windowsPowerShell))
            {
                return windowsPowerShell;
            }

            return "powershell.exe";
        }

        private static string Quote(string value)
        {
            return "\"" + value.Replace("\"", "\\\"") + "\"";
        }

        private static void Log(string logPath, string message)
        {
            string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message + Environment.NewLine;
            File.AppendAllText(logPath, line, new UTF8Encoding(false));
        }
    }
}
