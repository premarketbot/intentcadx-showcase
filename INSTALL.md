# Installing IntentCADX

IntentCADX is a Windows desktop app. It runs on your own machine and drives SolidWorks through COM.

## Before you start

You need three things.

- Windows 10 or 11, 64-bit.
- SolidWorks 2021 or newer, installed on the same machine.
- An API key. You have two options, and you only need one.

**Free — OpenRouter.** Create a key at [openrouter.ai/keys](https://openrouter.ai/keys). IntentCADX runs free models only on OpenRouter and refuses to send a request to a paid one, so a key with no credit on it cannot be billed. This costs nothing.

Two things to know. OpenRouter shares its free models across all its users and rate-limits them, so a busy moment means a slower reply — IntentCADX retries on another free model by itself. And free models are weaker than Claude at multi-step geometry, so expect more retries and more wrong first attempts.

**Paid — Anthropic.** Create a key at [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys) and add credit under [Billing](https://console.anthropic.com/settings/billing). Noticeably better results. You pay Anthropic directly for what you use.

IntentCADX does not include API access and does not bill you either way.

## Download and unzip

1. Go to the [Releases](../../releases) tab and download the latest `intentcad-...-win64.zip`.
2. **Right-click the .zip, choose Properties, tick Unblock, click OK.** Windows tags anything downloaded from the internet, and File Explorer copies that tag onto every file it extracts. The app clears the tag itself on startup, so this is belt and braces — but it takes two seconds and avoids the whole problem.
3. Unzip the whole folder. Do not extract the .exe on its own.
4. You should end up with a folder named `intentcad`, containing `intentcad.exe` and a folder named `_internal`.

## Run it

Open the `intentcad` folder and run `intentcad.exe` from inside it.

Do not move or copy `intentcad.exe` out of that folder. It will not start on its own. Everything it needs sits in `_internal` beside it. If you want the app somewhere else, move the whole folder.

The first time you run it, Windows shows a blue "Windows protected your PC" screen. This build is not code signed. Click **More info**, then **Run anyway**.

## First launch

A setup screen opens by itself. It offers **Free (OpenRouter)** and **Anthropic (your key)**. Pick one and paste the key.

On the free option you can also choose which free model to use. The list is checked against OpenRouter's live catalogue every time, because free models get withdrawn often — two of six disappeared in a single week during testing. If the one you picked goes away, IntentCADX quietly moves you to one that still exists.

The key is checked the moment you paste it, so a typo is caught here rather than halfway through your first part. It is stored encrypted on your own machine using Windows DPAPI, sent only to the provider you chose, and never written to a file in plain text.

Once the key is saved, later launches go straight to the app.

Start SolidWorks before you ask for geometry.

## Troubleshooting

Start here. Open a Command Prompt in the `intentcad` folder and run:

```
intentcad.exe --preflight
```

It prints eleven environment checks, with a short reason for anything that failed, and ends by naming the log file. Send that output with any problem report. It contains no API key material.

Because IntentCADX is a windowed program, your prompt comes back *before* the output appears. That is normal, not a hang.

What the common results mean:

- **WebView2 runtime is missing.** IntentCADX uses the Microsoft Edge WebView2 runtime to draw its window. Install it from [developer.microsoft.com/microsoft-edge/webview2](https://developer.microsoft.com/microsoft-edge/webview2/), then launch again.
- **SolidWorks is not reachable.** Start SolidWorks and run the check again. This one is a warning, not a failure. The app still starts without it.
- **An API key is configured, shown as a warning.** No key has been saved yet. Paste one on the setup screen.
- **An error mentioning `Python.Runtime.dll`.** Windows blocked the files. Right-click the .zip, Properties, tick Unblock, click OK, then extract it again and run from the new folder.
- **The app UI is bundled, shown as failing.** The download is incomplete. Unzip the whole folder again.

If the window never appears, read the log:

```
%APPDATA%\intentCAD\logs\intentcad.log
```

Paste that path into File Explorer's address bar. When a launch fails before the window opens, this is the only record of why — there is no console for it to have printed to. There is a second file beside it, `intentcad-console.log`, usually empty; if it is not, send that too. Neither contains API key material.

## Uninstalling

Delete the folder.

Your saved key and local settings live in `%APPDATA%\intentCAD`. Delete that folder to remove them.
