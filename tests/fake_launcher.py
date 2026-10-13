"""A real .exe for the fake player on Windows.

The runner starts players through a Job Object with CreateProcessW and, by design, refuses .bat and .cmd files (no shell). So the
fake player cannot be a batch file there. This compiles, once per machine, a tiny launcher with the .NET Framework compiler that ships
with Windows; it runs the command listed in the `.args` file beside it (one argument per line) plus its own arguments, waits, and
returns the exit code. That exercises the production launch path: an .exe, started owned, killed with its children on timeout.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

SOURCE = r'''
using System;
using System.Diagnostics;
using System.IO;
using System.Text;

class FakePlayerLauncher
{
    static string Quote(string s) { return "\"" + s.Replace("\"", "\\\"") + "\""; }

    static int Main(string[] arguments)
    {
        var self = System.Reflection.Assembly.GetExecutingAssembly().Location;
        var lines = File.ReadAllLines(Path.ChangeExtension(self, ".args"));
        var line = new StringBuilder();
        for (var i = 1; i < lines.Length; i++) line.Append(Quote(lines[i])).Append(' ');
        foreach (var a in arguments) line.Append(Quote(a)).Append(' ');
        var start = new ProcessStartInfo(lines[0], line.ToString()) { UseShellExecute = false };
        using (var process = Process.Start(start))
        {
            process.WaitForExit();
            return process.ExitCode;
        }
    }
}
'''


def find_csc():
    windir = os.environ.get('WINDIR', r'C:\Windows')
    for framework in ('Framework64', 'Framework'):
        path = Path(windir) / 'Microsoft.NET' / framework / 'v4.0.30319' / 'csc.exe'
        if path.is_file():
            return path
    return None


def launcher():
    """Path of the compiled launcher (built on first use), or None when the compiler is missing."""
    csc = find_csc()
    if csc is None:
        return None
    folder = Path(tempfile.gettempdir()) / 'unity-bench-fake-launcher'
    exe = folder / 'FakePlayerLauncher.exe'
    if not exe.is_file():
        folder.mkdir(exist_ok=True)
        source = folder / 'FakePlayerLauncher.cs'
        source.write_text(SOURCE, encoding='utf-8')
        subprocess.run([str(csc), '/nologo', '/target:exe', f'/out:{exe}', str(source)], check=True, capture_output=True)
    return exe


def write(output_dir, command):
    """UnityBenchPlayer.exe in `output_dir` that runs `command` (a list of strings) with the launcher's own arguments appended."""
    exe = output_dir / 'UnityBenchPlayer.exe'
    shutil.copy2(launcher(), exe)
    exe.with_suffix('.args').write_text('\n'.join(command) + '\n', encoding='utf-8')
    return exe


def compile_exe(name, source):
    """Compile `source` (C#) to `<new temp dir>/<name>` and return its path, or None when the compiler is missing."""
    csc = find_csc()
    if csc is None:
        return None
    folder = Path(tempfile.mkdtemp(prefix='unity-bench-fake-'))
    (folder / 'Program.cs').write_text(source, encoding='utf-8')
    exe = folder / name
    subprocess.run([str(csc), '/nologo', '/target:exe', f'/out:{exe}', str(folder / 'Program.cs')], check=True, capture_output=True)
    return exe
