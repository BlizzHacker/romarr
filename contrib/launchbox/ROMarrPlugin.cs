// ROMarr -> LaunchBox
//
// A system menu item that pulls the library from ROMarr and adds anything
// LaunchBox does not already have.
//
// NOT BUILT OR RUN. LaunchBox plugins are compiled .NET assemblies referencing
// Unbroken.LaunchBox.Plugins.dll, which ships with LaunchBox and is not
// redistributable, so this cannot be compiled in ROMarr's own environment.
// It is written against the documented API and has not been executed.
//
// The supported path is the XML export -- Tools -> Import -> LaunchBox XML.
// See contrib/README.md.

using System;
using System.Collections.Generic;
using System.Linq;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Threading.Tasks;
using System.Windows.Forms;
using System.Xml.Linq;
using Unbroken.LaunchBox.Plugins;
using Unbroken.LaunchBox.Plugins.Data;

namespace ROMarr
{
    public class ROMarrSyncMenuItem : ISystemMenuItemPlugin
    {
        public string Caption => "Sync from ROMarr";
        public System.Drawing.Image IconImage => null;
        public bool ShowInLaunchBox => true;
        public bool ShowInBigBox => false;
        public bool AllowInBigBoxWhenLocked => false;

        public void OnSelected()
        {
            var settings = ROMarrSettings.Load();
            if (settings == null)
            {
                MessageBox.Show(
                    "ROMarr is not configured yet.\n\n" +
                    "Create romarr.json beside this plugin:\n" +
                    "{ \"url\": \"http://romarr:6868\", \"apiKey\": \"...\" }",
                    "ROMarr");
                return;
            }

            try
            {
                var xml = Fetch(settings).GetAwaiter().GetResult();
                var added = Import(xml);
                MessageBox.Show($"ROMarr: added {added} games.", "ROMarr");
            }
            catch (Exception exception)
            {
                // Surfaced rather than swallowed: a sync that silently does
                // nothing is indistinguishable from an empty library, and the
                // operator debugs the wrong end of it.
                MessageBox.Show("ROMarr sync failed:\n" + exception.Message, "ROMarr");
            }
        }

        private static async Task<string> Fetch(ROMarrSettings settings)
        {
            using (var client = new HttpClient())
            {
                client.Timeout = TimeSpan.FromMinutes(5);
                // The key is a header, never a query parameter. LaunchBox
                // writes its own logs and a key in a URL ends up in them.
                client.DefaultRequestHeaders.Add("X-Api-Key", settings.ApiKey);
                client.DefaultRequestHeaders.Accept.Add(
                    new MediaTypeWithQualityHeaderValue("application/xml"));

                var uri = settings.Url.TrimEnd('/') +
                          "/api/v1/frontend/export?format=launchbox";
                var response = await client.GetAsync(uri).ConfigureAwait(false);
                response.EnsureSuccessStatusCode();
                return await response.Content.ReadAsStringAsync().ConfigureAwait(false);
            }
        }

        private static int Import(string xml)
        {
            var document = XDocument.Parse(xml);
            var platforms = PluginHelper.DataManager.GetAllPlatforms()
                .ToDictionary(p => p.Name, StringComparer.OrdinalIgnoreCase);

            // Index what is already here by launch path. Re-running a sync
            // must update rather than duplicate -- an importer that doubles
            // the library on every run is the most common complaint about
            // tools in this shape.
            var known = new HashSet<string>(
                PluginHelper.DataManager.GetAllGames()
                    .Where(g => !string.IsNullOrWhiteSpace(g.ApplicationPath))
                    .Select(g => g.ApplicationPath),
                StringComparer.OrdinalIgnoreCase);

            var added = 0;
            foreach (var element in document.Descendants("Game"))
            {
                var path = (string)element.Element("ApplicationPath");
                var title = (string)element.Element("Title");
                var platformName = (string)element.Element("Platform");

                if (string.IsNullOrWhiteSpace(path) ||
                    string.IsNullOrWhiteSpace(title) ||
                    known.Contains(path))
                {
                    continue;
                }

                // A platform LaunchBox does not have is created rather than
                // skipped: dropping the game would lose it silently, and the
                // operator would have no idea which ones went missing.
                if (!platforms.ContainsKey(platformName))
                {
                    var created = PluginHelper.DataManager.AddNewPlatform(platformName);
                    platforms[platformName] = created;
                }

                var game = PluginHelper.DataManager.AddNewGame(title);
                game.ApplicationPath = path;
                game.Platform = platformName;
                game.Notes = (string)element.Element("Notes") ?? string.Empty;
                added++;
            }

            if (added > 0)
            {
                PluginHelper.DataManager.Save(true);
            }
            return added;
        }
    }

    internal class ROMarrSettings
    {
        public string Url { get; set; }
        public string ApiKey { get; set; }

        public static ROMarrSettings Load()
        {
            var path = System.IO.Path.Combine(
                System.IO.Path.GetDirectoryName(
                    System.Reflection.Assembly.GetExecutingAssembly().Location),
                "romarr.json");
            if (!System.IO.File.Exists(path)) return null;

            var text = System.IO.File.ReadAllText(path);
            var settings = new ROMarrSettings
            {
                Url = Between(text, "\"url\""),
                ApiKey = Between(text, "\"apiKey\""),
            };
            return string.IsNullOrWhiteSpace(settings.Url) ? null : settings;
        }

        // Deliberately not a JSON library. The file has two string fields and
        // adding a NuGet dependency to a plugin that must be dropped into
        // someone's LaunchBox folder as a single DLL is a poor trade.
        private static string Between(string text, string key)
        {
            var at = text.IndexOf(key, StringComparison.OrdinalIgnoreCase);
            if (at < 0) return null;
            var colon = text.IndexOf(':', at);
            if (colon < 0) return null;
            var open = text.IndexOf('"', colon);
            if (open < 0) return null;
            var close = text.IndexOf('"', open + 1);
            return close < 0 ? null : text.Substring(open + 1, close - open - 1);
        }
    }
}
