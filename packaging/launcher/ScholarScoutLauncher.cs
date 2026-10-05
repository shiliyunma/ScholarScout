// ScholarScout 启动器（.NET Framework / csc 编译，无第三方依赖）
//
// 设计要点 —— 为什么这样就能"不因版本更替失效"：
//   这个 exe **不包含任何业务代码**，它只做三件事：找到项目根 → 找到 .venv 的解释器 →
//   把 scripts\scout.py --desktop 拉起来。所以项目代码怎么改、依赖怎么升，
//   都不用重新编译这个 exe。反过来，如果把整个应用 freeyeze 进 exe（PyInstaller 之类），
//   每次改代码都得重新打包，那才是"会因为版本更替失效"。
//
// 出错时会弹窗，并把 stderr 尾巴与"下一步该跑什么"一起给出来 —— 双击没反应是最糟的体验。
//
// 用法：双击即用。参数会原样透传给 scout.py（例：ScholarScout.exe --toolsets core,research）

using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

internal static class ScholarScoutLauncher
{
    private const string WindowTitle = "ScholarScout 启动器";
    //: 找不到配置也不在项目里时的兜底路径（安装位置固定的话不用改）
    private const string DefaultProject = @"D:\AI+教育毕设毕设\ScholarScout";
    private const string ConfigName = "ScholarScout.launcher.ini";
    //: 启动后等这么久：进程若在此期间退出且非 0，就认为启动失败并把日志弹出来
    private const int FailureGraceMs = 5000;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

    private const uint MB_ICONERROR = 0x10;
    private const uint MB_OK = 0x0;

    public static int Main(string[] args)
    {
        // 输出编码固定 UTF-8：本程序是 GUI 子系统，但 --diagnose 会把结果写到管道/控制台，
        // 中文 Windows 默认走 GBK，把中文输出搞成乱码（真踩过）。
        try
        {
            Console.OutputEncoding = Encoding.UTF8;
        }
        catch (IOException)
        {
        }
        catch (System.Security.SecurityException)
        {
        }

        string exeDir = Path.GetDirectoryName(System.Reflection.Assembly.GetExecutingAssembly().Location);

        // 诊断与手动指定项目：给"双击没反应"提供一条不用猜的路
        string forcedProject = null;
        bool diagnose = false;
        string[] passthrough = FilterOwnArgs(args, out forcedProject, out diagnose);

        string project = ResolveProject(exeDir, forcedProject);

        if (diagnose)
        {
            return Diagnose(exeDir, project, forcedProject);
        }

        if (project == null)
        {
            Fail(
                "找不到项目目录。\r\n\r\n"
                + "请在启动器旁边新建配置文件 " + ConfigName + "，写一行：\r\n"
                + "    project=D:\\路径\\到\\ScholarScout\r\n\r\n"
                + "（必须是个含 scripts\\scout.py 的目录）"
                + "\r\n\r\n想知道启动器在找什么，跑：ScholarScout.exe --diagnose");
            return 2;
        }

        string python = FindPython(project);
        if (python == null)
        {
            Fail(
                "项目里没有可用的 Python 解释器：\r\n    " + Path.Combine(project, @".venv\Scripts\python.exe")
                + "\r\n\r\n请先在项目里建虚拟环境并装依赖：\r\n"
                + "    uv venv --python 3.11\r\n"
                + "    .venv\\Scripts\\python.exe scripts\\install_deps.py");
            return 3;
        }

        string script = Path.Combine(project, "scripts", "scout.py");
        string logPath = Path.Combine(project, "run", "launcher.log");
        try
        {
            Directory.CreateDirectory(Path.Combine(project, "run"));
        }
        catch (IOException)
        {
            // 日志写不进去不影响启动
        }

        ProcessStartInfo info = new ProcessStartInfo();
        info.FileName = python;
        info.Arguments = Quote(script) + " --desktop" + ArgsToCommandLine(passthrough);
        info.WorkingDirectory = project;
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.RedirectStandardError = true;
        info.RedirectStandardOutput = false;
        // 中文路径与中文输出：强制 UTF-8，避免 GBK 解码炸掉（见 docs/03 §9）
        info.EnvironmentVariables["PYTHONUTF8"] = "1";
        info.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";

        StringBuilder errors = new StringBuilder();
        Process process;
        try
        {
            process = new Process();
            process.StartInfo = info;
            process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e)
            {
                if (e.Data != null)
                {
                    lock (errors) { errors.AppendLine(e.Data); }
                }
            };
            process.Start();
            process.BeginErrorReadLine();
        }
        catch (Exception ex)
        {
            Fail("启动子进程失败：\r\n" + ex.Message);
            return 4;
        }

        Thread.Sleep(FailureGraceMs);
        if (process.HasExited && process.ExitCode != 0)
        {
            string tail = errors.ToString();
            try
            {
                File.WriteAllText(logPath, tail, new UTF8Encoding(false));
            }
            catch (IOException)
            {
            }
            Fail(BuildFailureMessage(process.ExitCode, tail, logPath));
            return process.ExitCode;
        }

        // 正常路径：窗口已经起来了，启动器功成身退（子进程继续活着）
        return 0;
    }

    /// <summary>项目根：--project &gt; 配置文件 &gt; 环境变量 &gt; 逐级往上找 scripts\scout.py &gt; 内置默认值。</summary>
    private static string ResolveProject(string exeDir, string forced)
    {
        if (IsProject(forced)) return forced;
        if (!string.IsNullOrEmpty(forced)) return null; // 显式指定了但不对 → 明确失败，别偷偷换目录

        string fromConfig = ReadConfigProject(exeDir);
        if (IsProject(fromConfig)) return fromConfig;

        string fromEnv = Environment.GetEnvironmentVariable("SCHOLARSCOUT_HOME");
        if (IsProject(fromEnv)) return fromEnv;

        // exe 就放在项目里（例如 dist\）时，往上找几级也能找到
        DirectoryInfo dir = new DirectoryInfo(exeDir);
        for (int depth = 0; dir != null && depth < 4; depth++)
        {
            if (IsProject(dir.FullName)) return dir.FullName;
            dir = dir.Parent;
        }

        if (IsProject(DefaultProject)) return DefaultProject;
        return null;
    }

    /// <summary>把启动器自己的参数摘出来，其余原样透传给 scout.py。</summary>
    private static string[] FilterOwnArgs(string[] args, out string forcedProject, out bool diagnose)
    {
        forcedProject = null;
        diagnose = false;
        if (args == null) return new string[0];
        System.Collections.Generic.List<string> rest = new System.Collections.Generic.List<string>();
        for (int i = 0; i < args.Length; i++)
        {
            string arg = args[i] ?? "";
            if (arg == "--diagnose")
            {
                diagnose = true;
            }
            else if (arg == "--project" && i + 1 < args.Length)
            {
                forcedProject = args[i + 1];
                i++;
            }
            else
            {
                rest.Add(arg);
            }
        }
        return rest.ToArray();
    }

    /// <summary>打印解析结果并退出（不弹窗、不开窗口）—— "双击没反应"时的第一诊断手段。</summary>
    private static int Diagnose(string exeDir, string project, string forced)
    {
        StringBuilder sink = new StringBuilder();
        SayAndRemember("启动器诊断", sink);
        SayAndRemember("  启动器位置：" + exeDir, sink);
        SayAndRemember("  --project  ：" + (string.IsNullOrEmpty(forced) ? "（未指定）" : forced), sink);
        SayAndRemember("  项目根     ：" + (project == null ? "（没找到）" : project), sink);

        int code;
        if (project == null)
        {
            SayAndRemember("  → 修法：在启动器旁写 " + ConfigName + "，内容 project=<项目路径>", sink);
            code = 2;
        }
        else
        {
            string python = FindPython(project);
            SayAndRemember("  解释器     ：" + (python == null ? "（没找到）" : python), sink);
            SayAndRemember("  入口脚本   ：" + Path.Combine(project, "scripts", "scout.py"), sink);
            if (python == null)
            {
                code = 3;
            }
            else
            {
                string probe = RunProbe(python, project, "import webview; print('ok')");
                SayAndRemember("  pywebview  ：" + (probe == "ok" ? "可用" : probe), sink);
                if (probe != "ok")
                {
                    SayAndRemember("  → 修法：.venv\\Scripts\\python.exe scripts\\install_deps.py", sink);
                    code = 2;
                }
                else
                {
                    code = 0;
                }
            }
        }

        // 终端编码人人不同（GBK/UTF-8 都有），文件永远可读
        try
        {
            Directory.CreateDirectory(exeDir);
            File.WriteAllText(
                Path.Combine(exeDir, "launcher-diagnose.txt"), sink.ToString(), new UTF8Encoding(false)
            );
        }
        catch (Exception)
        {
        }
        return code;
    }

    /// <summary>跑一句 python 探测并返回输出（诊断用）。</summary>
    private static string RunProbe(string python, string workdir, string code)
    {
        try
        {
            ProcessStartInfo info = new ProcessStartInfo();
            info.FileName = python;
            info.Arguments = "-c \"" + code.Replace("\"", "\\\"") + "\"";
            info.WorkingDirectory = workdir;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.RedirectStandardOutput = true;
            info.RedirectStandardError = true;
            info.StandardOutputEncoding = Encoding.UTF8;
            info.StandardErrorEncoding = Encoding.UTF8;
            Process process = Process.Start(info);
            string output = process.StandardOutput.ReadToEnd().Trim();
            string error = process.StandardError.ReadToEnd().Trim();
            process.WaitForExit(20000);
            if (output.Length > 0) return output;
            if (error.IndexOf("ModuleNotFoundError") >= 0 || error.IndexOf("ImportError") >= 0)
            {
                return "缺失（ModuleNotFoundError）";
            }
            return error.Length > 0 ? error : "（无输出）";
        }
        catch (Exception ex)
        {
            return "探测失败：" + ex.Message;
        }
    }

    private static bool IsProject(string path)
    {
        if (string.IsNullOrEmpty(path)) return false;
        try
        {
            return File.Exists(Path.Combine(path, "scripts", "scout.py"));
        }
        catch (ArgumentException)
        {
            return false;
        }
    }

    private static string ReadConfigProject(string exeDir)
    {
        string path;
        try
        {
            path = Path.Combine(exeDir, ConfigName);
        }
        catch (ArgumentException)
        {
            return null;
        }
        if (!File.Exists(path)) return null;
        try
        {
            string[] lines = File.ReadAllLines(path, Encoding.UTF8);
            for (int i = 0; i < lines.Length; i++)
            {
                string line = lines[i].Trim();
                if (line.Length == 0 || line.StartsWith("#") || line.StartsWith(";")) continue;
                int eq = line.IndexOf('=');
                if (eq <= 0) continue;
                string key = line.Substring(0, eq).Trim().ToLowerInvariant();
                if (key != "project") continue;
                string value = line.Substring(eq + 1).Trim().Trim('"');
                if (value.Length > 0) return value;
            }
        }
        catch (IOException)
        {
            return null;
        }
        return null;
    }

    private static string FindPython(string project)
    {
        string venv = Path.Combine(project, @".venv\Scripts\python.exe");
        if (File.Exists(venv)) return venv;
        // 退一步：venv 不在预期位置时，用 PATH 上的 python（仍然给用户留活路）
        string[] pathDirs = Environment.GetEnvironmentVariable("PATH").Split(';');
        for (int i = 0; i < pathDirs.Length; i++)
        {
            if (pathDirs[i].Trim().Length == 0) continue;
            try
            {
                string candidate = Path.Combine(pathDirs[i].Trim(), "python.exe");
                if (File.Exists(candidate)) return candidate;
            }
            catch (ArgumentException)
            {
            }
        }
        return null;
    }

    private static string BuildFailureMessage(int exitCode, string stderr, string logPath)
    {
        StringBuilder sb = new StringBuilder();
        sb.AppendLine("ScholarScout 启动失败（退出码 " + exitCode + "）。");
        sb.AppendLine();
        if (exitCode == 2)
        {
            sb.AppendLine("看起来是缺 pywebview（桌面壳需要它）。");
            sb.AppendLine("在项目里跑一次：");
            sb.AppendLine("    .venv\\Scripts\\python.exe scripts\\install_deps.py");
        }
        else
        {
            sb.AppendLine("它说：");
        }
        sb.AppendLine();
        string tail = stderr == null ? "" : stderr.Trim();
        if (tail.Length > 1200) tail = tail.Substring(tail.Length - 1200);
        sb.AppendLine(tail.Length > 0 ? tail : "（没有错误输出，可看日志）");
        if (logPath != null)
        {
            sb.AppendLine();
            sb.AppendLine("完整输出已写入：" + logPath);
        }
        return sb.ToString();
    }

    private static string Quote(string value)
    {
        return "\"" + value + "\"";
    }

    private static string ArgsToCommandLine(string[] args)
    {
        if (args == null || args.Length == 0) return "";
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < args.Length; i++)
        {
            string arg = args[i] ?? "";
            sb.Append(' ');
            if (arg.IndexOf(' ') >= 0 || arg.IndexOf('"') >= 0)
            {
                sb.Append('"').Append(arg.Replace("\"", "\\\"")).Append('"');
            }
            else
            {
                sb.Append(arg);
            }
        }
        return sb.ToString();
    }

    /// <summary>静默模式：把错误写到 stderr 并返回退出码，不弹窗（自动化/排查用）。</summary>
    private static bool Silent()
    {
        return Environment.GetEnvironmentVariable("SCHOLARSCOUT_LAUNCHER_SILENT") == "1";
    }

    /// <summary>打印一行。GUI 子系统的进程在没有控制台时 Console 可能不可用 —— 吞掉异常别让它成为新的报错源。</summary>
    /// <remarks>
    /// 用**显式的 UTF-8 StreamWriter** 写标准输出：GUI 子系统里设 ``Console.OutputEncoding``
    /// 常常不生效（没有控制台句柄），中文会按本地 GBK 编出来 → 在 UTF-8 终端里全是乱码（真踩过）。
    /// 输出被重定向到管道时同样有效；完全没人接（双击）时写入是空操作。
    /// </remarks>
    private static void Say(string line)
    {
        try
        {
            if (Writer == null)
            {
                Writer = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false));
                Writer.AutoFlush = true;
            }
            Writer.WriteLine(line);
        }
        catch (IOException)
        {
        }
        catch (ObjectDisposedException)
        {
        }
    }

    private static StreamWriter Writer;

    /// <summary>把诊断结果同时落一份文件（终端编码各人不同，文件永远是 UTF-8）。</summary>
    private static void SayAndRemember(string line, StringBuilder sink)
    {
        Say(line);
        sink.AppendLine(line);
    }

    private static void Fail(string message)
    {
        if (Silent())
        {
            try
            {
                StreamWriter error = new StreamWriter(
                    Console.OpenStandardError(), new UTF8Encoding(false)
                );
                error.AutoFlush = true;
                error.WriteLine(message);
            }
            catch (IOException)
            {
            }
            catch (ObjectDisposedException)
            {
            }
            return;
        }
        try
        {
            MessageBoxW(IntPtr.Zero, message, WindowTitle, MB_OK | MB_ICONERROR);
        }
        catch (Exception)
        {
            // 弹窗都失败就只剩退出码了
        }
    }
}
