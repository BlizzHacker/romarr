using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Net.Sockets;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace ROMarr.Desktop;

public sealed class MainWindow : Form
{
    readonly TextBox dataPath = new() { Width = 280 };
    readonly TextBox libraryPath = new() { Width = 280 };
    readonly NumericUpDown port = new() { Minimum = 1024, Maximum = 65535, Value = 6868, Width = 78 };
    readonly Button start = new() { Text = "Start ROMarr", AutoSize = true };
    readonly Button stop = new() { Text = "Stop", AutoSize = true, Enabled = false };
    readonly Label status = new() { Text = "Choose your folders, then start ROMarr.", AutoSize = true, Padding = new Padding(8) };
    WebView2 browser = new() { Dock = DockStyle.Fill };
    readonly HttpClient http = new() { Timeout = TimeSpan.FromSeconds(4) };
    readonly string? smokeRoot;
    readonly string preferenceFile;
    Process? backend;
    bool busy;
    bool closing;
    string origin = "";
    string logPath = "";
    StreamWriter? log;
    Task? browserReady;
    string browserData = "";
    readonly List<Button> folderButtons = [];

    public MainWindow(string? smokeRoot)
    {
        this.smokeRoot = smokeRoot;
        Text = "ROMarr"; Width = 1280; Height = 880; MinimumSize = new Size(850, 600);
        StartPosition = FormStartPosition.CenterScreen;
        var appData = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "ROMarr");
        preferenceFile = Path.Combine(appData, "desktop.json");
        dataPath.Text = smokeRoot is null ? appData : Path.Combine(smokeRoot, "config");
        libraryPath.Text = smokeRoot is null ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Games", "ROMs") : Path.Combine(smokeRoot, "roms");
        if (smokeRoot is null) LoadPreferences();

        var menu = new MenuStrip();
        var file = new ToolStripMenuItem("ROMarr");
        file.DropDownItems.Add("Open data folder", null, (_,_) => OpenFolder(dataPath.Text));
        file.DropDownItems.Add("View desktop log", null, (_,_) => { if (File.Exists(logPath)) OpenExternal(logPath); });
        file.DropDownItems.Add("Exit", null, (_,_) => Close());
        var companion = new ToolStripMenuItem("Cartridge");
        companion.DropDownItems.Add("Get Cartridge for Windows", null, (_,_) => OpenExternal("https://github.com/BlizzHacker/RommForDesktop/releases/latest"));
        companion.DropDownItems.Add("Get Cartridge for Xbox", null, (_,_) => OpenExternal("https://apps.microsoft.com/detail/9MXC51W17LH4"));
        var help = new ToolStripMenuItem("Help");
        help.DropDownItems.Add("Getting started", null, (_,_) => OpenExternal("https://github.com/BlizzHacker/romarr/blob/main/docs/GETTING-STARTED.md"));
        help.DropDownItems.Add("ROM Hub plugins", null, (_,_) => { if (browser.CoreWebView2 is not null) browser.CoreWebView2.Navigate(origin + "/#hub"); });
        help.DropDownItems.Add("Roadmap", null, (_,_) => OpenExternal("https://github.com/BlizzHacker/romarr/blob/main/ROADMAP.md"));
        menu.Items.AddRange([file, companion, help]);
        MainMenuStrip = menu;

        var options = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, Padding = new Padding(10), WrapContents = true };
        options.Controls.Add(new Label { Text = "Data", AutoSize = true, Padding = new Padding(0,8,0,0) });
        options.Controls.Add(dataPath); options.Controls.Add(FolderButton(dataPath));
        options.Controls.Add(new Label { Text = "ROM library", AutoSize = true, Padding = new Padding(8,8,0,0) });
        options.Controls.Add(libraryPath); options.Controls.Add(FolderButton(libraryPath));
        options.Controls.Add(new Label { Text = "Port", AutoSize = true, Padding = new Padding(8,8,0,0) });
        options.Controls.Add(port); options.Controls.Add(start); options.Controls.Add(stop);
        options.Controls.Add(status);
        Controls.Add(browser); Controls.Add(options); Controls.Add(menu);
        start.Click += async (_,_) => await StartWithMessage();
        stop.Click += (_,_) => StopBackend();
        FormClosing += (_,_) => { closing = true; StopBackend(); http.Dispose(); };
        Shown += async (_,_) =>
        {
            if (smokeRoot is not null) await Smoke();
            else if (File.Exists(preferenceFile)) await StartWithMessage();
        };
    }

    Button FolderButton(TextBox field)
    {
        var button = new Button { Text = "Browse...", AutoSize = true };
        button.Click += (_,_) =>
        {
            using var picker = new FolderBrowserDialog { Description = "Choose a folder", UseDescriptionForTitle = true, SelectedPath = field.Text };
            if (picker.ShowDialog() == DialogResult.OK) field.Text = picker.SelectedPath;
        };
        folderButtons.Add(button);
        return button;
    }

    void LoadPreferences()
    {
        try
        {
            var value = JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(preferenceFile));
            if (value is null) return;
            if (value.TryGetValue("data", out var d)) dataPath.Text = d;
            if (value.TryGetValue("library", out var l)) libraryPath.Text = l;
            if (value.TryGetValue("port", out var p) && int.TryParse(p, out var n) && n >= 1024 && n <= 65535) port.Value = n;
        }
        catch (IOException) { }
        catch (JsonException) { }
    }

    void SavePreferences()
    {
        if (smokeRoot is not null) return;
        Directory.CreateDirectory(Path.GetDirectoryName(preferenceFile)!);
        var temp = preferenceFile + ".tmp";
        File.WriteAllText(temp, JsonSerializer.Serialize(new Dictionary<string, string> {
            ["data"] = dataPath.Text, ["library"] = libraryPath.Text, ["port"] = port.Value.ToString()
        }));
        File.Move(temp, preferenceFile, true);
    }

    async Task StartWithMessage()
    {
        try { await StartBackend(); }
        catch (Exception ex) { if (!closing) { status.Text = ex.Message; StopBackend(false); MessageBox.Show(this, ex.Message, "ROMarr could not start", MessageBoxButtons.OK, MessageBoxIcon.Error); } }
    }

    async Task StartBackend()
    {
        if (busy || backend is { HasExited: false }) return;
        busy = true; start.Enabled = false; status.Text = "Starting ROMarr...";
        try
        {
            var root = AppContext.BaseDirectory;
            var python = Path.Combine(root, "backend", "python.exe");
            var runtime = Path.Combine(root, "browser");
            var git = Path.Combine(root, "git", "cmd");
            if (!File.Exists(python) || !File.Exists(Path.Combine(runtime, "msedgewebview2.exe")))
                throw new IOException("This download is incomplete. Extract the entire ROMarr Windows ZIP before opening ROMarr.exe.");
            foreach (var folder in new[] { dataPath.Text, libraryPath.Text })
            {
                if (string.IsNullOrWhiteSpace(folder)) throw new IOException("Choose both a data folder and a ROM library folder.");
                Directory.CreateDirectory(folder);
                var check = Path.Combine(folder, ".romarr-write-check-" + Guid.NewGuid().ToString("N"));
                File.WriteAllText(check, ""); File.Delete(check);
            }
            var listener = new TcpListener(IPAddress.Loopback, (int)port.Value);
            try { listener.Start(); }
            catch (SocketException) { throw new IOException("That port is already in use. Choose another port, or close the other ROMarr window."); }
            finally { listener.Stop(); }
            origin = "http://127.0.0.1:" + port.Value;
            logPath = Path.Combine(dataPath.Text, "desktop.log");
            log = new StreamWriter(logPath, append: true) { AutoFlush = true };
            var info = new ProcessStartInfo(python)
            {
                WorkingDirectory = dataPath.Text, UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardOutput = true, RedirectStandardError = true
            };
            info.ArgumentList.Add("-m"); info.ArgumentList.Add("romarr");
            info.Environment["ROMARR_DATA"] = Path.Combine(dataPath.Text, "romarr.json");
            info.Environment["ROMARR_PORT"] = port.Value.ToString();
            info.Environment["ROMARR_HOST"] = "127.0.0.1";
            info.Environment["ROM_HUB_HOME"] = Path.Combine(dataPath.Text, "rom-hub");
            info.Environment["LIBRARY_KIND"] = "folder";
            info.Environment["LIBRARY_PATH"] = libraryPath.Text;
            info.Environment["PYTHONUTF8"] = "1";
            info.Environment["ROMARR_DESKTOP"] = "1";
            info.Environment["PATH"] = git + Path.PathSeparator + (info.Environment["PATH"] ?? "");
            backend = new Process { StartInfo = info };
            backend.OutputDataReceived += (_, e) => WriteLog(e.Data);
            backend.ErrorDataReceived += (_, e) => WriteLog(e.Data);
            backend.Start(); backend.BeginOutputReadLine(); backend.BeginErrorReadLine();
            for (var i = 0; i < 120; i++)
            {
                if (closing) return;
                if (backend is null || backend.HasExited) throw new IOException("ROMarr stopped while starting. Open the desktop log from the ROMarr menu for details.");
                try
                {
                    var health = await http.GetFromJsonAsync<Dictionary<string, bool>>(origin + "/api/health");
                    if (health?.GetValueOrDefault("ok") == true) break;
                }
                catch (HttpRequestException) { }
                catch (TaskCanceledException) { }
                if (i == 119) throw new IOException("ROMarr is taking too long to start. Check the desktop log.");
                await Task.Delay(500);
            }
            if (closing) return;
            var selectedBrowserData = Path.Combine(dataPath.Text, "browser-data");
            if (browser.CoreWebView2 is not null && browserData != selectedBrowserData)
            {
                Controls.Remove(browser); browser.Dispose();
                browser = new WebView2 { Dock = DockStyle.Fill };
                Controls.Add(browser); browser.SendToBack();
            }
            if (browser.CoreWebView2 is null)
            {
                PrepareWindows10Runtime(runtime);
                var environment = await CoreWebView2Environment.CreateAsync(runtime, selectedBrowserData);
                await browser.EnsureCoreWebView2Async(environment);
                browserData = selectedBrowserData;
                browser.CoreWebView2.Settings.AreDevToolsEnabled = false;
                browser.CoreWebView2.NewWindowRequested += (_, e) => { e.Handled = true; OpenWebLink(e.Uri); };
                browser.CoreWebView2.NavigationStarting += (_, e) =>
                {
                    if (!e.Uri.StartsWith(origin + "/", StringComparison.OrdinalIgnoreCase)) { e.Cancel = true; OpenWebLink(e.Uri); }
                };
            }
            var ready = new TaskCompletionSource();
            void Loaded(object? sender, CoreWebView2NavigationCompletedEventArgs e)
            {
                browser.NavigationCompleted -= Loaded;
                if (e.IsSuccess) ready.TrySetResult(); else ready.TrySetException(new IOException("The desktop interface could not load."));
            }
            browser.NavigationCompleted += Loaded;
            browser.CoreWebView2.Navigate(origin + "/");
            browserReady = ready.Task;
            await browserReady.WaitAsync(TimeSpan.FromSeconds(30));
            dataPath.Enabled = false; libraryPath.Enabled = false; port.Enabled = false;
            foreach (var button in folderButtons) button.Enabled = false;
            stop.Enabled = true; status.Text = "Running locally. Your library stays on this PC.";
            SavePreferences();
        }
        finally { busy = false; if (!closing) start.Enabled = backend is null || backend.HasExited; }
    }

    void WriteLog(string? line)
    {
        if (line is null) return;
        try { lock (this) { log?.WriteLine(line); } } catch (ObjectDisposedException) { }
    }

    void StopBackend(bool updateStatus = true)
    {
        try { if (backend is { HasExited: false }) { backend.Kill(entireProcessTree: true); backend.WaitForExit(10000); } }
        catch (InvalidOperationException) { }
        finally
        {
            backend?.Dispose(); backend = null;
            lock (this) { log?.Dispose(); log = null; }
            if (!closing) { foreach (var button in folderButtons) button.Enabled = true; dataPath.Enabled = true; libraryPath.Enabled = true; port.Enabled = true; start.Enabled = true; stop.Enabled = false; if (updateStatus) status.Text = "Stopped. Your files and settings are saved."; }
        }
    }

    static void PrepareWindows10Runtime(string runtime)
    {
        // Fixed WebView2 requires AppContainer read access on unpackaged Windows 10.
        // Packaged apps inherit this from Windows; touch only our own runtime folder.
        if (Environment.OSVersion.Version.Build >= 22000 ||
            AppContext.BaseDirectory.Contains("WindowsApps", StringComparison.OrdinalIgnoreCase)) return;
        foreach (var sid in new[] { "*S-1-15-2-1", "*S-1-15-2-2" })
        {
            var info = new ProcessStartInfo(Path.Combine(Environment.SystemDirectory, "icacls.exe"))
            { UseShellExecute = false, CreateNoWindow = true };
            info.ArgumentList.Add(runtime); info.ArgumentList.Add("/grant");
            info.ArgumentList.Add(sid + ":(OI)(CI)(RX)"); info.ArgumentList.Add("/T"); info.ArgumentList.Add("/Q");
            using var acl = Process.Start(info) ?? throw new IOException("Could not prepare the embedded interface.");
            acl.WaitForExit();
            if (acl.ExitCode != 0) throw new IOException("Windows could not grant the embedded browser read access. Extract ROMarr to a folder you own.");
        }
    }

    static void OpenFolder(string value) { if (Directory.Exists(value)) OpenExternal(value); }
    static void OpenExternal(string value) => Process.Start(new ProcessStartInfo(value) { UseShellExecute = true });
    static void OpenWebLink(string value) { if (Uri.TryCreate(value, UriKind.Absolute, out var u) && (u.Scheme == "https" || u.Scheme == "http")) OpenExternal(value); }

    async Task Smoke()
    {
        Directory.CreateDirectory(smokeRoot!);
        try
        {
            var key = Guid.NewGuid().ToString("N");
            Environment.SetEnvironmentVariable("ROMARR_API_KEY", key);
            port.Value = FreePort();
            await StartBackend();
            if (!IsHandleCreated || browser.CoreWebView2 is null) throw new Exception("The native window or embedded browser was not created.");
            await browser.ExecuteScriptAsync("document.getElementById('usekey').click(); document.getElementById('apikey').value=" + JsonSerializer.Serialize(key) + "; document.getElementById('f').requestSubmit();");
            var interfaceReady = false;
            for (var i = 0; i < 120; i++)
            {
                var result = await browser.ExecuteScriptAsync("!!document.getElementById('page') && typeof RENDER !== 'undefined'");
                if (result == "true") { interfaceReady = true; break; }
                await Task.Delay(250);
            }
            if (!interfaceReady) throw new Exception("The embedded interface did not sign in.");
            browser.CoreWebView2.Navigate(origin + "/#setup");
            var guideReady = false;
            for (var i = 0; i < 80; i++)
            {
                var result = await browser.ExecuteScriptAsync("document.body.innerText.includes('I already have ROMs') && document.body.innerText.includes('Find and download games')");
                if (result == "true") { guideReady = true; break; }
                await Task.Delay(250);
            }
            if (!guideReady) throw new Exception("The embedded setup guide did not render.");
            using (var screenshot = File.Create(Path.Combine(smokeRoot!, "smoke-ui.png")))
                await browser.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, screenshot);
            var anon = await http.GetAsync(origin + "/api/v1/config");
            if (anon.StatusCode != HttpStatusCode.Unauthorized) throw new Exception("Private API was not protected.");
            http.DefaultRequestHeaders.Add("X-Api-Key", key);
            var hub = await http.GetFromJsonAsync<JsonElement>(origin + "/api/v1/hub/catalogue");
            if (hub.GetProperty("items").GetArrayLength() == 0) throw new Exception("Bundled ROM Hub catalogue is unavailable.");
            using (var installer = new HttpClient { Timeout = TimeSpan.FromMinutes(3) })
            {
                installer.DefaultRequestHeaders.Add("X-Api-Key", key);
                var installed = await installer.PostAsync(origin + "/api/v1/hub/plugin",
                    new StringContent(JsonSerializer.Serialize(new { slug = "archive-org", action = "install" }),
                                      System.Text.Encoding.UTF8, "application/json"));
                var installedResult = await installed.Content.ReadFromJsonAsync<JsonElement>();
                if (!installed.IsSuccessStatusCode || !installedResult.GetProperty("ok").GetBoolean())
                    throw new Exception("The packaged Git/Python could not install a catalogue plugin: " + installedResult);
            }
            var installedHub = await http.GetFromJsonAsync<JsonElement>(origin + "/api/v1/hub/catalogue");
            if (!installedHub.GetProperty("items").EnumerateArray().Any(p => p.GetProperty("slug").GetString() == "archive-org" && p.GetProperty("installed").GetBoolean()))
                throw new Exception("The installed plugin was not present in the catalogue.");
            var put = await http.PutAsync(origin + "/api/v1/config",
                new StringContent(JsonSerializer.Serialize(new { preferred_regions = new[] { "EUR", "USA" } }),
                                  System.Text.Encoding.UTF8, "application/json"));
            put.EnsureSuccessStatusCode();
            StopBackend();
            await StartBackend();
            var config = await http.GetFromJsonAsync<JsonElement>(origin + "/api/v1/config");
            if (config.GetProperty("preferred_regions")[0].GetString() != "EUR") throw new Exception("Settings did not survive restart.");
            File.WriteAllText(Path.Combine(smokeRoot!, "smoke-proof.json"), JsonSerializer.Serialize(new {
                nativeWindow = true, embeddedInterface = true, bundledPython = true, bundledHub = true, pluginInstall = true,
                authentication = true, persistence = true, setupGuide = true, port = port.Value
            }, new JsonSerializerOptions { WriteIndented = true }));
            Environment.ExitCode = 0;
        }
        catch (Exception ex) { File.WriteAllText(Path.Combine(smokeRoot!, "smoke-error.txt"), ex.ToString()); Environment.ExitCode = 1; }
        finally { Close(); }
    }

    static int FreePort() { var listener = new TcpListener(IPAddress.Loopback, 0); listener.Start(); var n = ((IPEndPoint)listener.LocalEndpoint).Port; listener.Stop(); return n; }
}
