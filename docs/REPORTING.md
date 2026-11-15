# Report a file or ask for help

Open **Activity → Reports & Help**. You can flag a suspicious file, report something that is not a game, or ask for help without diagnosing the problem yourself.

## Report and scan

Enter the file’s path inside your configured library, such as `snes/My game.sfc`, choose what seems wrong and describe what you noticed. Click **Report and scan**. ROMarr records your claim locally, copies a private snapshot and computes its SHA-256 fingerprint. Your original stays in place while ROMarr reviews the snapshot.

The review uses configured scanners and an available local Microsoft Defender installation. On Linux, configure a ClamAV address under **Settings → Media Management**. The report can use that address even when automatic import scanning is off. Other enabled services keep their existing configuration requirements. ClamAV receives snapshot bytes through its configured scanner connection; VirusTotal checks the hash and does not upload the file.

Reports distinguish:

- **Needs review / scanner unavailable:** no usable completed verdict. This is not a clean result.
- **No detections:** completed checks found no detections. Scanners have limits and can miss threats; this does not prove the file is safe, playable or the right game.
- **Scanner reported a detection:** the report records the engine, signature, timestamp and fingerprint of the scanned snapshot.
- **Hash reputation flagged:** a configured reputation service reported a concern; this is separate from a fresh local antivirus detection.
- **Antivirus test signature:** an EICAR test signature was reported. That identifies the standard scanner test, not evidence that malicious code executed. It does not rule out other content.

A “not a game” or nuisance report still needs human review. ROMarr does not treat popularity or a user accusation as proof of malware. Two reviews can run at once, with a 4 GiB limit per snapshot. Files outside the library, symlinked paths and internal quarantine storage are refused. Interrupted scans remain unverified.

If antivirus already removed a file, use the help form with the alert name and steps instead. Do not restore it merely to create a report.

## Quarantine and restore

After reviewing the evidence, choose **Quarantine file** if you want it moved out of the normal library. ROMarr first checks that its bytes still match the report. It moves the file into the library’s private `.romarr-file-quarantine` folder and keeps a durable record. Nothing is automatically deleted.

**Restore file** checks the stored bytes and refuses to overwrite an existing destination. A changed source or an unsupported filesystem operation leaves the file for review rather than replacing another file. Keep the report state directory and library together in backups. Quarantine is an organizational safeguard, not an operating-system sandbox.

## Submit an issue from ROMarr

Use the help form for bugs and suggestions, or choose **Prepare GitHub report** on a file report. A shared file report includes its file name, category, SHA-256 and scanner evidence. Local notes, absolute paths and file bytes are excluded by default.

Click **Review on GitHub** to open a prefilled issue, sign in with your own account and submit it there. You can edit the draft first. ROMarr never needs your GitHub password, and nothing is published automatically.

Operators who want direct submission can explicitly supply `ROMARR_GITHUB_ISSUES_TOKEN` to the server with permission to create issues in `BlizzHacker/romarr`. The app then offers **Submit to GitHub** with a confirmation checkbox. Every authenticated user of that instance can submit using that configured account, so enable it only for users you trust. The token stays on the server; ROMarr does not reuse a developer’s `GH_TOKEN` or distribute project-maintainer credentials. Direct submissions are limited to one per 30 seconds.

Before publishing, remove credentials and private information from the text. You do not need to upload a game, BIOS or suspected malicious file to ask for help.

## Defender alerts from development tests

Some earlier Windows test runs intentionally wrote EICAR fixtures in pytest temporary folders, including tests named `test_an_infected_download_is_refused` and `test_off_by_default`. Defender correctly recognized those test bytes. Ordinary Windows runs now skip tests that write the real antivirus fixture; the disabled-scanner test uses ordinary harmless bytes. Linux CI retains the relevant scanner-protocol coverage.

Release packages bundle application code, not the pytest test tree. Keep Defender enabled; ROMarr does not require a blanket exclusion. If an alert points somewhere else, record its exact name and path before assuming it is the same fixture.
