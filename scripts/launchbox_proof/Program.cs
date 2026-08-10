// Executes the LaunchBox plugin's import logic against a live ROMarr export.
//
//   launchbox_proof.exe <export.xml>
//
// The XML comes from /api/v1/frontend/export?format=launchbox on a real
// install; Import() is the plugin's own private method, reached by
// reflection because changing the plugin's surface for the sake of the
// proof would mean proving something else.

using System;
using System.IO;
using System.Reflection;
using Unbroken.LaunchBox.Plugins;
using Unbroken.LaunchBox.Plugins.Data;

internal static class Program
{
    private static int _failures;

    private static void Check(string step, bool ok, string detail = "")
    {
        if (!ok) _failures++;
        Console.WriteLine($"  [{(ok ? "PASS" : "FAIL")}] {step}" +
                          (detail == "" ? "" : $" -- {detail}"));
    }

    private static int Main(string[] args)
    {
        Console.WriteLine($"LaunchBox plugin proof -- {DateTime.UtcNow:s}Z");
        var xml = File.ReadAllText(args[0]);
        var manager = new FakeDataManager();
        PluginHelper.DataManager = manager;

        var plugin = new ROMarr.ROMarrSyncMenuItem();
        Check("the plugin implements ISystemMenuItemPlugin",
              plugin is ISystemMenuItemPlugin);

        var import = typeof(ROMarr.ROMarrSyncMenuItem).GetMethod(
            "Import", BindingFlags.NonPublic | BindingFlags.Static);
        var added = (int)import.Invoke(null, new object[] { xml });
        Check("Import() adds games from the live export XML", added > 0,
              $"{added} games, {manager.Platforms.Count} platforms auto-created");
        Check("every game carries a path and a platform",
              added > 0 && Array.TrueForAll(manager.GetAllGames(), g =>
                  !string.IsNullOrWhiteSpace(g.ApplicationPath) &&
                  !string.IsNullOrWhiteSpace(g.Platform)));
        Check("Save() was called once the import had something to keep",
              manager.Saves == 1);

        var again = (int)import.Invoke(null, new object[] { xml });
        Check("a second import adds nothing (the dedupe the README promises)",
              again == 0, $"library still {manager.Games.Count}");

        Console.WriteLine(_failures == 0
            ? "All LaunchBox proofs passed."
            : $"{_failures} proof(s) FAILED.");
        return _failures == 0 ? 0 : 1;
    }
}
