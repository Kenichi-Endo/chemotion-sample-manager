using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class Launcher
{
    [STAThread]
    private static int Main(string[] args)
    {
        bool setupOnly = args.Length == 1 && args[0] == "--setup-only";
        string directory = Path.GetDirectoryName(Application.ExecutablePath);
        string script = Path.GetFullPath(Path.Combine(directory, @"..\..\scripts\start-windows.cmd"));

        try
        {
            if (!File.Exists(script))
                throw new FileNotFoundException("Keep Chemotion Sample Manager.exe in the project’s launchers\\windows folder.", script);

            var start = new ProcessStartInfo
            {
                FileName = Path.Combine(Environment.SystemDirectory, "cmd.exe"),
                Arguments = "/d /s /c \"\"%CHEMOTION_LAUNCH_SCRIPT%\"" + (setupOnly ? " --setup-only" : "") + "\"",
                WorkingDirectory = directory,
                UseShellExecute = false
            };
            start.EnvironmentVariables["CHEMOTION_LAUNCH_SCRIPT"] = script;
            using (Process process = Process.Start(start))
            {
                process.WaitForExit();
                return process.ExitCode;
            }
        }
        catch (Exception error)
        {
            if (!setupOnly)
                MessageBox.Show(error.Message, "Chemotion Sample Manager could not start",
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
