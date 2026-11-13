# Official builds and support

ROMarr is maintained by **BlizzHacker / MOVE WEIGHT**. Use these sources when you want a build supported by this project.

| Item | Official source |
| --- | --- |
| Project and issues | [BlizzHacker/romarr](https://github.com/BlizzHacker/romarr) |
| Website | [romarr.app](https://romarr.app) |
| Releases and Windows packages | [ROMarr releases](https://github.com/BlizzHacker/romarr/releases) |
| Container images | `ghcr.io/blizzhacker/romarr` |
| Unraid Community Applications | [ROMarr](https://ca.unraid.net/apps/romarr-04jslct0riva81?q=romarr) |
| Unraid template | [BlizzHacker/cartridge-unraid: templates/romarr.xml](https://github.com/BlizzHacker/cartridge-unraid/blob/main/templates/romarr.xml) |
| ROM Hub | [BlizzHacker/rom-hub](https://github.com/BlizzHacker/rom-hub) |

## ROMarrNG is a separate project

The [ROMarrNG Unraid listing](https://ca.unraid.net/apps/romarrng-0ix7ta406cq2i0?q=romarr) installs `ghcr.io/snapetech-org/romarrng:latest` from its own template. It is an independently maintained fork. It is **not an official ROMarr release**, is **not affiliated with or endorsed by the ROMarr maintainers**, and is **not supported by this project**.

Send problems with that package to its maintainer. If you can reproduce a problem on an up-to-date official ROMarr build, we welcome a report here. Similar names, screenshots or inherited documentation do not establish official support.

## Check an Unraid installation

Open the container's Edit page and check **Repository**. The official image begins with `ghcr.io/blizzhacker/romarr`. Compare the template's project and support links with the official sources above.

Before replacing a fork, stop its container and back up its appdata and settings. Use a separate appdata directory for the official installation until you have checked compatibility. Pointing different projects at the same settings directory can damage data.

## Attribution and branding

ROMarr's code remains under the [MIT license](../LICENSE), including its permission to fork and redistribute with the required copyright and license notices. A fork does not receive official support or endorsement.

We do not approve of removed attribution, misleading claims of authorship, or names, logos and package descriptions that imply a third-party build is an official ROMarr release. Derivative projects should identify their maintainer, describe their relationship to upstream clearly, preserve required notices, and provide their own support links.

This policy does not change the MIT license or claim that a properly attributed fork is prohibited. The official distribution and support links above are the reference for this project's users.
