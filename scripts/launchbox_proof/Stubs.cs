// A reconstruction of the eight members of the LaunchBox plugin API that
// contrib/launchbox/ROMarrPlugin.cs touches, plus an in-memory DataManager so
// the plugin's import logic can be EXECUTED, not just compiled.
//
// Unbroken.LaunchBox.Plugins.dll ships with LaunchBox and is not
// redistributable, so this cannot link against the real assembly. What this
// proves, precisely: the plugin compiles, and its import/dedupe/platform
// logic behaves against a faithful double. What it cannot prove: that the
// real assembly's signatures match this reconstruction. That gap is stated
// in docs/PROOF.md rather than papered over.

using System;
using System.Collections.Generic;
using System.Linq;

namespace Unbroken.LaunchBox.Plugins
{
    public interface ISystemMenuItemPlugin
    {
        string Caption { get; }
        System.Drawing.Image IconImage { get; }
        bool ShowInLaunchBox { get; }
        bool ShowInBigBox { get; }
        bool AllowInBigBoxWhenLocked { get; }
        void OnSelected();
    }

    public static class PluginHelper
    {
        public static Data.IDataManager DataManager { get; set; }
    }
}

namespace Unbroken.LaunchBox.Plugins.Data
{
    public interface IGame
    {
        string Title { get; set; }
        string ApplicationPath { get; set; }
        string Platform { get; set; }
        string Notes { get; set; }
    }

    public interface IPlatform
    {
        string Name { get; set; }
    }

    public interface IDataManager
    {
        IGame[] GetAllGames();
        IPlatform[] GetAllPlatforms();
        IGame AddNewGame(string title);
        IPlatform AddNewPlatform(string name);
        void Save(bool updateUi);
    }

    // -- the in-memory double ------------------------------------------------

    public class FakeGame : IGame
    {
        public string Title { get; set; }
        public string ApplicationPath { get; set; }
        public string Platform { get; set; }
        public string Notes { get; set; }
    }

    public class FakePlatform : IPlatform
    {
        public string Name { get; set; }
    }

    public class FakeDataManager : IDataManager
    {
        public readonly List<IGame> Games = new List<IGame>();
        public readonly List<IPlatform> Platforms = new List<IPlatform>();
        public int Saves;

        public IGame[] GetAllGames() => Games.ToArray();
        public IPlatform[] GetAllPlatforms() => Platforms.ToArray();

        public IGame AddNewGame(string title)
        {
            var game = new FakeGame { Title = title };
            Games.Add(game);
            return game;
        }

        public IPlatform AddNewPlatform(string name)
        {
            var platform = new FakePlatform { Name = name };
            Platforms.Add(platform);
            return platform;
        }

        public void Save(bool updateUi) => Saves++;
    }
}
