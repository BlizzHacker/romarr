namespace ROMarr.Desktop;

internal static class Program
{
    [STAThread]
    static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        string? smoke = args.Length == 2 && args[0] == "--smoke-test" ? args[1] : null;
        Application.Run(new MainWindow(smoke));
    }
}
