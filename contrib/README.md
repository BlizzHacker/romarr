# Frontend plugins

Plugins for [Playnite](https://playnite.link) and
[LaunchBox](https://www.launchbox-app.com), plus the reference for ES-DE.

## What is proven and what is not

Be clear about this before installing anything here.

**The ROMarr side is tested** — `romarr/frontends.py` and its 20 tests. Every
export format is a pure function from library rows to text, and the tests cover
the platform-name mapping, XML escaping, relative paths, missing files and an
empty library.

**The plugins in this directory are not.** LaunchBox plugins are compiled .NET
assemblies and Playnite extensions run inside Playnite; neither can be built or
executed in the environment ROMarr is developed in. The source is written
against each project's documented API, and it has not been run.

That is why the integration is built on the exports rather than on the plugins.
A plugin breaks when the host changes its API; a `gamelist.xml` has been read
the same way for fifteen years. **If a plugin here ever stops working, use the
export — it is the supported path.**

## The endpoint they all use

```
GET /api/v1/frontend/export?format=launchbox|gamelist|playnite
X-Api-Key: <your key>
```

`launchbox` and `gamelist` return XML, `playnite` returns JSON. Add
`&platform=<slug>` to export one system — which is required for `gamelist`,
since EmulationStation keeps one file per system directory.

## Playnite

`playnite/` is a **script extension**: PowerShell, no compilation, and it
installs by copying the folder.

1. Copy `playnite/ROMarr` into `%APPDATA%\Playnite\Extensions\`.
2. Restart Playnite.
3. **Extensions → ROMarr → Configure**, and paste your ROMarr URL and API key.
4. **Extensions → ROMarr → Sync library**.

Playnite's *Library* plugin interface — the one that makes ROMarr appear as a
library source alongside Steam and GOG — is C#-only. This uses a Generic
extension with a menu item instead, which imports the same games without
requiring a compiled assembly. That is a deliberate trade: something you can
install and read the source of, over something that needs a build toolchain.

## LaunchBox

Two ways in, and the second is the one to reach for first.

**Import the XML.** Export `format=launchbox`, then in LaunchBox use
**Tools → Import → LaunchBox XML**. Nothing to install and nothing to break.

**Or build the plugin.** `launchbox/` is a .NET class library against
`Unbroken.LaunchBox.Plugins`. It adds a *ROMarr → Sync* menu item.

```
cd contrib/launchbox
dotnet build -c Release
```

Copy the resulting DLL into `LaunchBox\Plugins\ROMarr\`. You will need
`Unbroken.LaunchBox.Plugins.dll` from your own LaunchBox installation as a
reference — it is not redistributable, which is the other reason this is not
the recommended path.

## ES-DE, Batocera, RetroPie, Recalbox

No plugin needed. These read a ROM directory and a `gamelist.xml`, which is
what ROMarr already writes:

```bash
curl -H "X-Api-Key: $KEY" \
  "http://romarr:6868/api/v1/frontend/export?format=gamelist&platform=snes" \
  > /roms/snes/gamelist.xml
```

ROMarr's `folder` library backend already writes the directory layout these
expect, so for most installs the gamelist is the only extra step.
