using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Net.Sockets;
using System.Text.Json;
using System.Runtime.InteropServices;
using System.Reflection;
using System.Text.RegularExpressions;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace ROMarr.Desktop;

public sealed class MainWindow : Form
{
    readonly TableLayoutPanel shell = new() { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, Margin = Padding.Empty, Padding = Padding.Empty };
    readonly Panel browserHost = new() { Dock = DockStyle.Fill, Margin = Padding.Empty, BackColor = Color.FromArgb(15, 17, 24) };
    readonly FlowLayoutPanel connectionOptions = new() { Dock = DockStyle.Fill, AutoSize = true, AutoScroll = true, Padding = new Padding(12), Margin = Padding.Empty, WrapContents = true, BackColor = Color.FromArgb(26, 29, 39) };
    readonly Button connectionToggle = new() { Text = "Connections", AutoSize = true, AccessibleName = "Show connection settings" };
    readonly ToolTip tips = new();
    readonly TextBox dataPath = new() { Width = 280 };
    readonly TextBox libraryPath = new() { Width = 280 };
    readonly NumericUpDown port = new() { Minimum = 1024, Maximum = 65535, Value = 6868, Width = 78 };
    readonly Button start = new() { Text = "Start ROMarr", AutoSize = true };
    readonly TextBox serverAddress = new() { Width = 280, PlaceholderText = "https://romarr.example.com" };
    readonly Button connect = new() { Text = "Connect to server", AutoSize = true };
    bool remoteConnected;
    bool remoteSessionActive;
    bool remoteTrustPending;
    long remoteSessionVersion;
    readonly HashSet<string> signInOrigins = new(StringComparer.OrdinalIgnoreCase);
    string? resumedSignInUrl;
    TaskCompletionSource? remoteLoadReady;
    string preferredMode = "local";
    string savedServerOrigin = "";
    readonly Button stop = new() { Text = "Stop", AutoSize = true, Enabled = false };
    readonly Label status = new() { Text = "Choose a connection to get started", Dock = DockStyle.Fill, AutoSize = false, AutoEllipsis = true, TextAlign = ContentAlignment.MiddleLeft, Margin = new Padding(12, 0, 12, 0), AccessibleName = "Current ROMarr connection" };
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

    /// <summary>Creates the native standalone and server-management window.</summary>
    public MainWindow(string? smokeRoot)
    {
        this.smokeRoot = smokeRoot;
        Text = "ROMarr"; Width = 1280; Height = 880; MinimumSize = new Size(640, 480);
        AutoScaleMode = AutoScaleMode.Dpi; AutoScaleDimensions = new SizeF(96, 96);
        BackColor = Color.FromArgb(15, 17, 24); ForeColor = Color.FromArgb(225, 228, 238);
        Font = new Font("Segoe UI", 10f);
        StartPosition = FormStartPosition.CenterScreen;
        var appData = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "ROMarr");
        preferenceFile = Path.Combine(appData, "desktop.json");
        dataPath.Text = smokeRoot is null ? appData : Path.Combine(smokeRoot, "config");
        libraryPath.Text = smokeRoot is null ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Games", "ROMs") : Path.Combine(smokeRoot, "roms");
        if (smokeRoot is null) LoadPreferences();

        var menu = new ContextMenuStrip { BackColor = Color.FromArgb(26, 29, 39), ForeColor = ForeColor, ShowImageMargin = false };
        menu.Renderer = new ToolStripProfessionalRenderer(new ShellMenuColors());
        var file = new ToolStripMenuItem("ROMarr");
        file.DropDownItems.Add("Open data folder", null, (_,_) => OpenFolder(dataPath.Text));
        file.DropDownItems.Add("View desktop log", null, (_,_) => { if (File.Exists(logPath)) OpenExternal(logPath); });
        file.DropDownItems.Add("Exit", null, (_,_) => Close());
        var companion = new ToolStripMenuItem("Cartridge");
        companion.DropDownItems.Add("Auto-install Cartridge Player", null, (_,_) => RunCartridgeAction("/api/v1/cartridge/install"));
        companion.DropDownItems.Add("Launch Cartridge Player", null, (_,_) => LaunchCartridge());
        companion.DropDownItems.Add("Plug & Play Setup (All Emulators)", null, (_,_) => RunCartridgeAction("/api/v1/emulators/plug-and-play"));
        companion.DropDownItems.Add(new ToolStripSeparator());
        companion.DropDownItems.Add("Get Cartridge for Windows", null, (_,_) => OpenExternal("https://github.com/BlizzHacker/romarr/releases/download/v1.8.0/Cartridge-Windows-Portable-0.2.0-x64.exe"));
        companion.DropDownItems.Add("Get Cartridge for Xbox", null, (_,_) => OpenExternal("https://apps.microsoft.com/detail/9MXC51W17LH4"));
        companion.DropDownItems.Add("Get Cartridge for Android", null, (_,_) => OpenExternal("https://github.com/BlizzHacker/Cartridge/releases"));
        var help = new ToolStripMenuItem("Help");
        help.DropDownItems.Add("Getting started", null, (_,_) => OpenExternal("https://github.com/BlizzHacker/romarr/blob/main/docs/GETTING-STARTED.md"));
        help.DropDownItems.Add("ROM Hub plugins", null, (_,_) => OpenHubPage());
        help.DropDownItems.Add("Roadmap", null, (_,_) => OpenExternal("https://github.com/BlizzHacker/romarr/blob/main/ROADMAP.md"));
        menu.Items.AddRange([file, companion, help]);
        menu.Items.Add("Reload server view", null, (_,_) => { if (browser.CoreWebView2 is not null) browser.Reload(); });
        var menuButton = new Button { Text = "Menu ▾", AutoSize = true, AccessibleName = "ROMarr app menu" };
        menuButton.Click += (_,_) => menu.Show(menuButton, new Point(0, menuButton.Height));
        connectionToggle.Click += (_,_) => SetConnectionOptionsVisible(!connectionOptions.Visible);
        var header = new TableLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, ColumnCount = 4, RowCount = 1, Padding = new Padding(12, 6, 12, 6), Margin = Padding.Empty, MinimumSize = new Size(0, 50), BackColor = BackColor };
        header.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        header.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        header.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        header.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        var brand = new Label { Text = "ROMarr", AutoSize = true, Anchor = AnchorStyles.Left, ForeColor = Color.FromArgb(242, 163, 60), Font = new Font("Segoe UI", 13, FontStyle.Bold), Margin = Padding.Empty };
        header.Controls.Add(brand, 0, 0); header.Controls.Add(status, 1, 0); header.Controls.Add(connectionToggle, 2, 0); header.Controls.Add(menuButton, 3, 0);
        StyleShellButton(connectionToggle); StyleShellButton(menuButton);
        status.TextChanged += (_,_) => { tips.SetToolTip(status, status.Text); status.AccessibleDescription = status.Text; };
        var options = connectionOptions;
        options.Controls.Add(new Label { Text = "Data", AutoSize = true, Padding = new Padding(0,8,0,0) });
        options.Controls.Add(dataPath); options.Controls.Add(FolderButton(dataPath));
        options.Controls.Add(new Label { Text = "ROM library", AutoSize = true, Padding = new Padding(8,8,0,0) });
        options.Controls.Add(libraryPath); options.Controls.Add(FolderButton(libraryPath));
        options.Controls.Add(new Label { Text = "Port", AutoSize = true, Padding = new Padding(8,8,0,0) });
        options.Controls.Add(port); options.Controls.Add(start); options.Controls.Add(stop);
        options.Controls.Add(new Label { Text = "Or server", AutoSize = true, Padding = new Padding(8,8,0,0) });
        options.Controls.Add(serverAddress); options.Controls.Add(connect);
        foreach (Control control in options.Controls)
        {
            control.Margin = new Padding(4, 5, 4, 5);
            if (control is Button button) StyleShellButton(button);
            else if (control is TextBox text) { text.BackColor = Color.FromArgb(17, 20, 28); text.ForeColor = ForeColor; text.BorderStyle = BorderStyle.FixedSingle; }
            else if (control is NumericUpDown number) { number.BackColor = Color.FromArgb(17, 20, 28); number.ForeColor = ForeColor; }
            else control.ForeColor = ForeColor;
        }
        shell.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        shell.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        shell.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        browserHost.Controls.Add(browser);
        shell.Controls.Add(header, 0, 0); shell.Controls.Add(options, 0, 1); shell.Controls.Add(browserHost, 0, 2);
        Controls.Add(shell);
        Resize += (_,_) => { connectionOptions.MaximumSize = new Size(Math.Max(320, ClientSize.Width), Math.Max(120, ClientSize.Height / 2)); };
        SetConnectionOptionsVisible(!File.Exists(preferenceFile));
        start.Click += async (_,_) => await StartWithMessage();
        stop.Click += (_,_) => { if (remoteConnected) DisconnectServer(); else StopBackend(); };
        connect.Click += async (_,_) => await ConnectServerWithMessage();
        FormClosing += (_,_) => { closing = true; ClearRemoteSignIn(); StopBackend(); http.Dispose(); };
        Shown += async (_,_) =>
        {
            if (smokeRoot is not null) await Smoke();
            else if (File.Exists(preferenceFile))
            {
                if (preferredMode == "remote") await ConnectServerWithMessage();
                else await StartWithMessage();
            }
        };
    }

    [DllImport("dwmapi.dll", EntryPoint = "DwmSetWindowAttribute")]
    static extern int SetWindowTheme(IntPtr window, int attribute, ref int value, int size);

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        try { var dark = 1; SetWindowTheme(Handle, 20, ref dark, sizeof(int)); }
        catch (DllNotFoundException) { }
        catch (EntryPointNotFoundException) { }
    }

    /// <summary>Keeps connection details out of the server viewport until requested.</summary>
    void SetConnectionOptionsVisible(bool visible)
    {
        connectionOptions.Visible = visible;
        connectionToggle.Text = visible ? "Hide connections" : "Connections";
        connectionToggle.AccessibleName = visible ? "Hide connection settings" : "Show connection settings";
        shell.PerformLayout(); browserHost.PerformLayout();
    }

    static void StyleShellButton(Button button)
    {
        button.FlatStyle = FlatStyle.Flat;
        button.BackColor = Color.FromArgb(32, 37, 49); button.ForeColor = Color.FromArgb(225, 228, 238);
        button.FlatAppearance.BorderColor = Color.FromArgb(65, 73, 92);
        button.FlatAppearance.MouseOverBackColor = Color.FromArgb(49, 57, 74);
        button.Padding = new Padding(8, 3, 8, 3); button.MinimumSize = new Size(0, 34);
        button.Cursor = Cursors.Hand;
    }

    sealed class ShellMenuColors : ProfessionalColorTable
    {
        public override Color ToolStripDropDownBackground => Color.FromArgb(26, 29, 39);
        public override Color ImageMarginGradientBegin => ToolStripDropDownBackground;
        public override Color ImageMarginGradientMiddle => ToolStripDropDownBackground;
        public override Color ImageMarginGradientEnd => ToolStripDropDownBackground;
        public override Color MenuItemSelected => Color.FromArgb(49, 57, 74);
        public override Color MenuItemBorder => Color.FromArgb(65, 73, 92);
        public override Color MenuBorder => Color.FromArgb(65, 73, 92);
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

    /// <summary>Loads local preferences, accepting only credential-free server origins.</summary>
    void LoadPreferences()
    {
        try
        {
            var value = JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(preferenceFile));
            if (value is null) return;
            if (value.TryGetValue("server", out var server) && !string.IsNullOrWhiteSpace(server))
            {
                try { serverAddress.Text = savedServerOrigin = RemoteServerUri.Normalize(server); }
                catch (ArgumentException) { }
            }
            if (value.TryGetValue("mode", out var mode) && mode == "remote") preferredMode = "remote";
            if (value.TryGetValue("data", out var d)) dataPath.Text = d;
            if (value.TryGetValue("library", out var l)) libraryPath.Text = l;
            if (value.TryGetValue("port", out var p) && int.TryParse(p, out var n) && n >= 1024 && n <= 65535) port.Value = n;
        }
        catch (IOException) { }
        catch (JsonException) { }
    }

    /// <summary>Saves folders and the last validated server origin without credentials.</summary>
    void SavePreferences()
    {
        if (smokeRoot is not null) return;
        Directory.CreateDirectory(Path.GetDirectoryName(preferenceFile)!);
        var temp = preferenceFile + ".tmp";
        File.WriteAllText(temp, JsonSerializer.Serialize(new Dictionary<string, string> {
            ["server"] = savedServerOrigin, ["mode"] = preferredMode,
            ["data"] = dataPath.Text, ["library"] = libraryPath.Text, ["port"] = port.Value.ToString()
        }));
        File.Move(temp, preferenceFile, true);
    }

    async Task StartWithMessage()
    {
        try { await StartBackend(); }
        catch (Exception ex) { if (!closing) { status.Text = ex.Message; StopBackend(false); MessageBox.Show(this, ex.Message, "ROMarr could not start", MessageBoxButtons.OK, MessageBoxIcon.Error); } }
    }

    /// <summary>Starts the packaged backend on loopback and opens its isolated interface.</summary>
    async Task StartBackend()
    {
        if (busy || backend is { HasExited: false }) return;
        if (remoteSessionActive || remoteConnected) DisconnectServer();
        ClearRemoteSignIn();
        preferredMode = "local"; stop.Text = "Stop";
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
                    var health = await http.GetFromJsonAsync<JsonElement>(origin + "/api/health");
                    if (health.TryGetProperty("ok", out var healthy) && healthy.ValueKind == JsonValueKind.True) break;
                }
                catch (HttpRequestException) { }
                catch (TaskCanceledException) { }
                if (i == 119) throw new IOException("ROMarr is taking too long to start. Check the desktop log.");
                await Task.Delay(500);
            }
            if (closing) return;
            var selectedBrowserData = Path.Combine(dataPath.Text, "browser-data");
            await EnsureBrowser(runtime, selectedBrowserData);
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
            stop.Enabled = true; status.Text = "This PC · library stored locally";
            SetConnectionOptionsVisible(false);
            SavePreferences();
        }
        finally { busy = false; if (!closing) start.Enabled = backend is null || backend.HasExited; }
    }

    /// <summary>Creates an isolated embedded browser profile for the selected server.</summary>
    async Task EnsureBrowser(string runtime, string selectedBrowserData)
    {
            if (browser.CoreWebView2 is not null && browserData != selectedBrowserData)
            {
                browserHost.Controls.Remove(browser); browser.Dispose();
                browser = new WebView2 { Dock = DockStyle.Fill };
                browserHost.Controls.Add(browser);
            }
            if (browser.CoreWebView2 is null)
            {
                PrepareWindows10Runtime(runtime);
                var environment = await CoreWebView2Environment.CreateAsync(runtime, selectedBrowserData);
                await browser.EnsureCoreWebView2Async(environment);
                browserData = selectedBrowserData;
                browser.CoreWebView2.Settings.AreDevToolsEnabled = false;
                browser.CoreWebView2.NewWindowRequested += (_, e) => { e.Handled = true; OpenWebLink(e.Uri); };
                var selectedBrowser = browser;
                browser.CoreWebView2.NavigationStarting += (_, e) => HandleRemoteNavigation(selectedBrowser, e);
                browser.CoreWebView2.NavigationCompleted += (_, e) =>
                {
                    if (e.IsSuccess && remoteSessionActive && ReferenceEquals(browser, selectedBrowser)) UpdateRemoteStatus();
                };
            }
    }

    /// <summary>Extracts only a credential-free HTTPS authority for trust prompts.</summary>
    static string? SignInOrigin(string value)
    {
        if (!Uri.TryCreate(value, UriKind.Absolute, out var address) || address.Scheme != Uri.UriSchemeHttps
            || string.IsNullOrEmpty(address.Host) || !string.IsNullOrEmpty(address.UserInfo)) return null;
        return address.GetLeftPart(UriPartial.Authority);
    }

    /// <summary>Allows sign-in redirects and their same-origin forms, never ordinary external links.</summary>
    static bool IsSignInContinuation(bool remote, bool redirected, bool currentSignInOrigin, string? destination)
        => remote && destination is not null && (redirected || currentSignInOrigin);

    /// <summary>Invalidates outstanding prompts and forgets per-session sign-in approvals.</summary>
    void ClearRemoteSignIn()
    {
        remoteSessionVersion++;
        remoteSessionActive = false; remoteTrustPending = false;
        signInOrigins.Clear(); resumedSignInUrl = null;
        remoteLoadReady?.TrySetCanceled(); remoteLoadReady = null;
    }

    /// <summary>Rejects callbacks belonging to a closed or previously selected server view.</summary>
    bool CurrentRemoteSession(long version, string server, WebView2 selectedBrowser)
        => !closing && remoteSessionActive && version == remoteSessionVersion
            && origin == server && ReferenceEquals(browser, selectedBrowser);

    /// <summary>Displays only the authority of the page currently visible in the native browser.</summary>
    void UpdateRemoteStatus()
    {
        if (!remoteSessionActive || browser.CoreWebView2 is null) return;
        if (!Uri.TryCreate(browser.CoreWebView2.Source, UriKind.Absolute, out var current)
            || (current.Scheme != Uri.UriSchemeHttp && current.Scheme != Uri.UriSchemeHttps)) return;
        var authority = current.GetLeftPart(UriPartial.Authority);
        status.Text = authority.Equals(origin, StringComparison.OrdinalIgnoreCase)
            ? current.Authority + " · Connected"
            : current.Authority + " · Sign in to your server";
    }

    /// <summary>Keeps explicitly approved HTTPS SSO redirects inside the selected remote profile.</summary>
    void HandleRemoteNavigation(WebView2 selectedBrowser, CoreWebView2NavigationStartingEventArgs e)
    {
        if (!ReferenceEquals(browser, selectedBrowser)) { e.Cancel = true; return; }
        if (e.Uri == "about:blank" || e.Uri.StartsWith(origin + "/", StringComparison.OrdinalIgnoreCase)) return;
        var destination = SignInOrigin(e.Uri);
        var current = SignInOrigin(selectedBrowser.CoreWebView2.Source);
        var continuation = IsSignInContinuation(remoteSessionActive, e.IsRedirected,
            current is not null && signInOrigins.Contains(current) && current.Equals(destination, StringComparison.OrdinalIgnoreCase), destination);
        if (resumedSignInUrl == e.Uri && remoteSessionActive && destination is not null && signInOrigins.Contains(destination))
        {
            resumedSignInUrl = null; return;
        }
        if (continuation && destination is not null && signInOrigins.Contains(destination)) return;
        e.Cancel = true;
        if (!continuation || destination is null) { OpenWebLink(e.Uri); return; }
        if (remoteTrustPending) return;
        remoteTrustPending = true;
        var version = remoteSessionVersion;
        var server = origin;
        var redirectUrl = e.Uri;
        // A WebView2 event cannot host a modal nested message loop. Defer the native prompt.
        BeginInvoke((Action)(() =>
        {
            if (!CurrentRemoteSession(version, server, selectedBrowser)) return;
            var approved = MessageBox.Show(this,
                "This server redirected sign-in to:\n\n" + destination
                + "\n\nOnly continue if this is your trusted sign-in provider. Allow this HTTPS origin in this server view until disconnect?",
                "Allow sign-in provider?", MessageBoxButtons.YesNo, MessageBoxIcon.Question,
                MessageBoxDefaultButton.Button2) == DialogResult.Yes;
            if (!CurrentRemoteSession(version, server, selectedBrowser)) return;
            remoteTrustPending = false;
            if (!approved)
            {
                status.Text = "Sign-in provider was not approved: " + destination;
                remoteLoadReady?.TrySetException(new IOException("The sign-in provider was not approved."));
                return;
            }
            signInOrigins.Add(destination);
            resumedSignInUrl = redirectUrl;
            status.Text = "Opening sign-in page: " + destination + ". Selected server: " + server;
            selectedBrowser.CoreWebView2.Navigate(redirectUrl);
        }));
    }

    /// <summary>Checks redirect policy without trusting a real provider or bypassing TLS in CI.</summary>
    static void ProveRemoteSignInPolicy()
    {
        var https = SignInOrigin("https://login.example.test/authorize?secret=fixture");
        if (https != "https://login.example.test" || SignInOrigin("http://login.example.test/") is not null
            || SignInOrigin("https://user:password@login.example.test/") is not null
            || IsSignInContinuation(false, true, false, https)
            || IsSignInContinuation(true, false, false, https)
            || IsSignInContinuation(true, true, false, null)
            || !IsSignInContinuation(true, true, false, https)
            || !IsSignInContinuation(true, false, true, https))
            throw new Exception("Remote sign-in trust policy failed.");
        if (MatchesRemoteNavigation(null, 5) || MatchesRemoteNavigation(6, 5)
            || !MatchesRemoteNavigation(6, 6))
            throw new Exception("Stale remote navigation completion was accepted.");
    }

    /// <summary>Requires the completion event to belong to this connection's latest permitted navigation.</summary>
    static bool MatchesRemoteNavigation(ulong? expected, ulong actual)
        => expected.HasValue && expected.Value == actual;

    /// <summary>Opens plugins only while a valid local or connected remote server view is active.</summary>
    bool OpenHubPage()
    {
        if (browser.CoreWebView2 is null || (!remoteConnected && backend is not { HasExited: false })
            || !Uri.TryCreate(origin, UriKind.Absolute, out var server)
            || (server.Scheme != Uri.UriSchemeHttp && server.Scheme != Uri.UriSchemeHttps)) return false;
        browser.CoreWebView2.Navigate(server.GetLeftPart(UriPartial.Authority) + "/#hub");
        return true;
    }

    async void RunCartridgeAction(string endpoint)
    {
        if (string.IsNullOrEmpty(origin) || (!remoteConnected && backend is not { HasExited: false }))
        {
            MessageBox.Show(this, "Start ROMarr or connect to a server before running Cartridge setup.", "Cartridge", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }
        try
        {
            status.Text = "Running Cartridge action...";
            using var client = new HttpClient { Timeout = TimeSpan.FromMinutes(2) };
            var response = await client.PostAsync(origin + endpoint, null);
            var content = await response.Content.ReadAsStringAsync();
            if (response.IsSuccessStatusCode)
            {
                status.Text = "Cartridge action completed.";
                MessageBox.Show(this, "Completed successfully:\n\n" + content, "Cartridge", MessageBoxButtons.OK, MessageBoxIcon.Information);
            }
            else
            {
                status.Text = "Cartridge action failed.";
                MessageBox.Show(this, "Failed (" + (int)response.StatusCode + "):\n\n" + content, "Cartridge", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        }
        catch (Exception ex)
        {
            status.Text = "Cartridge action error: " + ex.Message;
            MessageBox.Show(this, "Could not run Cartridge action: " + ex.Message, "Cartridge", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    async void LaunchCartridge()
    {
        if (string.IsNullOrEmpty(origin) || (!remoteConnected && backend is not { HasExited: false }))
        {
            MessageBox.Show(this, "Start ROMarr or connect to a server before launching Cartridge.", "Cartridge", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
            var response = await client.PostAsync(origin + "/api/v1/cartridge/launch", null);
            var content = await response.Content.ReadAsStringAsync();
            if (!response.IsSuccessStatusCode)
            {
                MessageBox.Show(this, "Could not launch Cartridge:\n\n" + content, "Cartridge", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, "Could not launch Cartridge: " + ex.Message, "Cartridge", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    /// <summary>Connects to a user-selected server without starting a local backend.</summary>
    async Task ConnectServerWithMessage()
    {
        if (busy) return;
        var transitioned = false;
        try
        {
            var target = RemoteServerUri.Normalize(serverAddress.Text);
            var runtime = Path.Combine(AppContext.BaseDirectory, "browser");
            if (!File.Exists(Path.Combine(runtime, "msedgewebview2.exe")))
                throw new IOException("Extract the whole ROMarr Windows ZIP before connecting.");
            busy = true; connect.Enabled = false; start.Enabled = false;
            transitioned = true; StopBackend(false);
            ClearRemoteSignIn();
            origin = target; remoteSessionActive = true;
            var sessionVersion = remoteSessionVersion;
            var profile = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(target)));
            await EnsureBrowser(runtime, Path.Combine(dataPath.Text, "remote-browser-data", profile));
            serverAddress.Text = savedServerOrigin = target;
            if (!CurrentRemoteSession(sessionVersion, target, browser)) return;
            var selectedBrowser = browser;
            var ready = new TaskCompletionSource(); remoteLoadReady = ready;
            ulong? expectedNavigation = null;
            void RemoteStarting(object? sender, CoreWebView2NavigationStartingEventArgs e)
            {
                if (!CurrentRemoteSession(sessionVersion, target, selectedBrowser) || e.Cancel) return;
                var signIn = SignInOrigin(e.Uri);
                if (e.Uri.StartsWith(target + "/", StringComparison.OrdinalIgnoreCase)
                    || (signIn is not null && signInOrigins.Contains(signIn)))
                    expectedNavigation = e.NavigationId;
            }
            void RemoteLoaded(object? sender, CoreWebView2NavigationCompletedEventArgs e)
            {
                if (!CurrentRemoteSession(sessionVersion, target, selectedBrowser)
                    || !MatchesRemoteNavigation(expectedNavigation, e.NavigationId)) return;
                if (!e.IsSuccess && e.WebErrorStatus == CoreWebView2WebErrorStatus.OperationCanceled) return;
                if (e.IsSuccess)
                {
                    var displayed = selectedBrowser.CoreWebView2.Source;
                    var displayedSignIn = SignInOrigin(displayed);
                    if (displayed.StartsWith(target + "/", StringComparison.OrdinalIgnoreCase)
                        || (displayedSignIn is not null && signInOrigins.Contains(displayedSignIn))) ready.TrySetResult();
                }
                else ready.TrySetException(new IOException("The server page could not load. Check the address and network connection."));
            }
            selectedBrowser.CoreWebView2.NavigationStarting += RemoteStarting;
            selectedBrowser.NavigationCompleted += RemoteLoaded;
            try
            {
                browser.CoreWebView2.Navigate(target + "/");
                var remaining = TimeSpan.FromSeconds(30);
                while (!ready.Task.IsCompleted)
                {
                    var prompting = remoteTrustPending;
                    var elapsed = Stopwatch.StartNew();
                    await Task.WhenAny(ready.Task, Task.Delay(250));
                    if (!prompting && !remoteTrustPending) remaining -= elapsed.Elapsed;
                    if (remaining <= TimeSpan.Zero) throw new IOException("The server page took too long to load.");
                }
                await ready.Task;
            }
            finally
            {
                selectedBrowser.CoreWebView2.NavigationStarting -= RemoteStarting;
                selectedBrowser.NavigationCompleted -= RemoteLoaded;
                if (ReferenceEquals(remoteLoadReady, ready)) remoteLoadReady = null;
            }
            if (!CurrentRemoteSession(sessionVersion, target, selectedBrowser)) return;
            remoteConnected = true; preferredMode = "remote";
            stop.Text = "Disconnect"; stop.Enabled = true;
            UpdateRemoteStatus();
            SetConnectionOptionsVisible(false);
            SavePreferences();
        }
        catch (Exception ex)
        {
            if (!closing) { if (transitioned) DisconnectServer(); MessageBox.Show(this, ex.Message, "Could not connect to ROMarr", MessageBoxButtons.OK, MessageBoxIcon.Error); }
        }
        finally { busy = false; if (!closing) { connect.Enabled = true; start.Enabled = backend is null || backend.HasExited; } }
    }

    /// <summary>Closes the remote view without stopping or changing the remote server.</summary>
    void DisconnectServer()
    {
        ClearRemoteSignIn();
        remoteConnected = false; origin = "";
        browser.CoreWebView2?.Navigate("about:blank");
        stop.Text = "Stop"; stop.Enabled = false;
        status.Text = "Disconnected · your server is still running";
        SetConnectionOptionsVisible(true);
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

    /// <summary>Proves server mode without a local backend, then checks disconnect leaves it running.</summary>
    async Task ProveRemoteView()
    {
        var remoteOrigin = "http://127.0.0.1:" + FreePort();
        using var listener = new HttpListener();
        listener.Prefixes.Add(remoteOrigin + "/"); listener.Start();
        var worker = Task.Run(async () =>
        {
            while (listener.IsListening)
            {
                try
                {
                    var context = await listener.GetContextAsync();
                    var bytes = System.Text.Encoding.UTF8.GetBytes("<!doctype html><title>ROMarr remote fixture</title><p>Remote server is running</p>");
                    context.Response.ContentType = "text/html";
                    context.Response.ContentLength64 = bytes.Length;
                    await context.Response.OutputStream.WriteAsync(bytes);
                    context.Response.Close();
                }
                catch (HttpListenerException) { break; }
                catch (ObjectDisposedException) { break; }
            }
        });
        try
        {
            serverAddress.Text = remoteOrigin;
            await ConnectServerWithMessage();
            if (!remoteConnected || backend is not null) throw new Exception("Server mode started a local backend or failed to connect.");
            var rendered = await browser.ExecuteScriptAsync("document.body.innerText.includes('Remote server is running')");
            if (rendered != "true") throw new Exception("The native remote view did not display the selected server.");
            DisconnectServer();
            if (OpenHubPage()) throw new Exception("The disconnected ROM Hub action attempted navigation.");
            // Reconnect before about:blank can finish; its completion must not finish this attempt.
            await ConnectServerWithMessage();
            if (!remoteConnected || !browser.CoreWebView2.Source.StartsWith(remoteOrigin + "/", StringComparison.OrdinalIgnoreCase))
                throw new Exception("An old blank-page navigation completed the fresh connection.");
            DisconnectServer();
            if (!(await http.GetStringAsync(remoteOrigin + "/")).Contains("Remote server is running"))
                throw new Exception("Disconnect stopped the remote fixture.");
        }
        finally { listener.Stop(); await worker.WaitAsync(TimeSpan.FromSeconds(5)); }
        await StartBackend();
    }

    /// <summary>Compares numeric package identities, treating an omitted revision as zero.</summary>
    static bool SameVersionIdentity(string? backendVersion, Version? nativeVersion, string? nativeRelease = null)
    {
        if (backendVersion?.Contains('-') == true)
        {
            if (!Regex.IsMatch(backendVersion, @"^\d+\.\d+\.\d+-(alpha|beta|rc)\.\d+$")
                || nativeRelease?.Split('+')[0] != backendVersion) return false;
            backendVersion = backendVersion.Split('-')[0];
        }
        if (!Version.TryParse(backendVersion, out var backendVersionNumber) || nativeVersion is null
            || backendVersionNumber.Build < 0 || nativeVersion.Build < 0) return false;
        return backendVersionNumber.Major == nativeVersion.Major
            && backendVersionNumber.Minor == nativeVersion.Minor
            && backendVersionNumber.Build == nativeVersion.Build
            && Math.Max(backendVersionNumber.Revision, 0) == Math.Max(nativeVersion.Revision, 0);
    }

    /// <summary>Proves three/four-component identity equivalence without dropping nonzero revisions.</summary>
    static void ProveVersionIdentity()
    {
        if (!SameVersionIdentity("1.10.0", new Version(1, 10, 0, 0))
            || !SameVersionIdentity("1.10.0.0", new Version(1, 10, 0))
            || !SameVersionIdentity("1.10.0.4", new Version(1, 10, 0, 4))
            || SameVersionIdentity("1.10.0", new Version(1, 10, 0, 4))
            || SameVersionIdentity("1.10.0.4", new Version(1, 10, 0, 5))
            || SameVersionIdentity("1.10.1", new Version(1, 10, 0, 0))
            || SameVersionIdentity("invalid", new Version(1, 10, 0)))
            throw new Exception("Package version identity comparison failed.");
    }

    /// <summary>Exercises the actual packaged window, backend, browser and server modes.</summary>
    async Task Smoke()
    {
        Directory.CreateDirectory(smokeRoot!);
        try
        {
            ProveRemoteSignInPolicy();
            ProveVersionIdentity();
            var key = Guid.NewGuid().ToString("N");
            Environment.SetEnvironmentVariable("ROMARR_API_KEY", key);
            port.Value = FreePort();
            await StartBackend();
            if (!IsHandleCreated || browser.CoreWebView2 is null) throw new Exception("The native window or embedded browser was not created.");
            await browser.ExecuteScriptAsync("document.getElementById('usekey').click(); document.getElementById('apikey').value=" + JsonSerializer.Serialize(key) + "; document.getElementById('f').requestSubmit();");
            var interfaceLoaded = false;
            for (var i = 0; i < 120; i++)
            {
                var result = await browser.ExecuteScriptAsync("!!document.getElementById('page') && typeof RENDER !== 'undefined'");
                if (result == "true") { interfaceLoaded = true; break; }
                await Task.Delay(250);
            }
            if (!interfaceLoaded) throw new Exception("The embedded interface did not load after sign-in.");
            await browser.ExecuteScriptAsync("window.__romarrSmokeAuthProbe='pending'; fetch('/api/v1/config',{cache:'no-store'}).then(r=>window.__romarrSmokeAuthProbe=r.ok?'ok':'denied').catch(()=>window.__romarrSmokeAuthProbe='failed');");
            var authenticated = false;
            for (var i = 0; i < 120; i++)
            {
                var result = await browser.ExecuteScriptAsync("window.__romarrSmokeAuthProbe === 'ok'");
                if (result == "true") { authenticated = true; break; }
                await Task.Delay(250);
            }
            if (!authenticated) throw new Exception("The embedded interface did not authenticate its API session.");
            await browser.ExecuteScriptAsync("location.hash='setup'");
            var guideReady = false;
            for (var i = 0; i < 80; i++)
            {
                var result = await browser.ExecuteScriptAsync("document.body.innerText.includes('I already have ROMs') && document.body.innerText.includes('Find and download games')");
                if (result == "true") { guideReady = true; break; }
                await Task.Delay(250);
            }
            if (!guideReady) throw new Exception("The embedded setup guide did not render.");
            await ProveShellLayout();
            using (var screenshot = File.Create(Path.Combine(smokeRoot!, "smoke-ui.png")))
                await browser.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, screenshot);
            var anon = await http.GetAsync(origin + "/api/v1/config");
            if (anon.StatusCode != HttpStatusCode.Unauthorized) throw new Exception("Private API was not protected.");
            http.DefaultRequestHeaders.Add("X-Api-Key", key);
            var versionInfo = await http.GetFromJsonAsync<JsonElement>(origin + "/api/v1/stats");
            if (!SameVersionIdentity(versionInfo.GetProperty("version").GetString(), typeof(MainWindow).Assembly.GetName().Version,
                typeof(MainWindow).Assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion))
                throw new Exception("The packaged backend version does not match the native application.");
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
            await ProveRemoteView();
            File.WriteAllText(Path.Combine(smokeRoot!, "smoke-proof.json"), JsonSerializer.Serialize(new {
                nativeWindow = true, shellLayout = true, embeddedInterface = true, bundledPython = true, bundledHub = true, pluginInstall = true,
                authentication = true, persistence = true, setupGuide = true, versionIdentity = true, remoteView = true, remoteDisconnectKeepsServer = true, remoteSignInPolicy = true, port = port.Value
            }, new JsonSerializerOptions { WriteIndented = true }));
            Environment.ExitCode = 0;
        }
        catch (Exception ex) { File.WriteAllText(Path.Combine(smokeRoot!, "smoke-error.txt"), ex.ToString()); Environment.ExitCode = 1; }
        finally { Close(); }
    }

    /// <summary>Checks the real browser never sits underneath the native toolbar or settings.</summary>
    async Task ProveShellLayout()
    {
        if (!SameVersionIdentity("1.11.0-beta.2", new Version(1,11,0,0), "1.11.0-beta.2+fixture")
            || SameVersionIdentity("1.11.0-beta.2", new Version(1,11,0,0), "1.11.0-beta.3"))
            throw new Exception("Preview package release identity did not match its backend.");
        var original = ClientSize;
        try
        {
            foreach (var size in new[] { new Size(640, 480), new Size(850, 600), new Size(1280, 800) })
            foreach (var expanded in new[] { false, true })
            {
                ClientSize = size;
                connectionOptions.MaximumSize = new Size(ClientSize.Width, ClientSize.Height / 2);
                SetConnectionOptionsVisible(expanded);
                PerformLayout(); shell.PerformLayout(); browserHost.PerformLayout();
                await Task.Delay(100);
                var header = shell.GetControlFromPosition(0, 0)!;
                var lowerEdge = expanded ? connectionOptions.Bottom : header.Bottom;
                if (browserHost.Top < lowerEdge || browserHost.Bottom > shell.ClientSize.Height + 1
                    || browserHost.Height < 80 || browser.Bounds != browserHost.ClientRectangle)
                    throw new Exception("Native controls overlap or clip the embedded server view.");
                var dimensions = JsonSerializer.Deserialize<JsonElement>(await browser.ExecuteScriptAsync("({height:innerHeight,dpr:devicePixelRatio})"));
                if (Math.Abs(dimensions.GetProperty("height").GetDouble() * dimensions.GetProperty("dpr").GetDouble() - browser.ClientSize.Height) > 3)
                    throw new Exception("Web content viewport does not match the visible browser area.");
            }
        }
        finally { ClientSize = original; SetConnectionOptionsVisible(false); }
    }

    static int FreePort() { var listener = new TcpListener(IPAddress.Loopback, 0); listener.Start(); var n = ((IPEndPoint)listener.LocalEndpoint).Port; listener.Stop(); return n; }
}
