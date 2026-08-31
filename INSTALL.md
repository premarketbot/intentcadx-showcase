# Installing IntentCADX

IntentCADX is a Windows desktop app. It runs on your own machine and drives SolidWorks through COM.

## Before you start

You need three things.

- Windows 10 or 11, 64-bit.
- SolidWorks 2021 or newer, installed on the same machine.
- An Anthropic API key, on an account with credit. Create one at [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys) and add credit under [Billing](https://console.anthropic.com/settings/billing).

IntentCADX does not include API access and does not bill you. You pay Anthropic directly for what you use.

## Download and unzip

1. Go to the [Releases](../../releases) tab and download the latest `intentcad-...-win64.zip`.
2. Unzip the whole folder. Do not extract the .exe on its own.
3. You should end up with a folder named `intentcad`, containing `intentcad.exe` and a folder named `_internal`.

## Run it

Open the `intentcad` folder and run `intentcad.exe` from inside it.

Do not move or copy `intentcad.exe` out of that folder. It will not start on its own. Everything it needs sits in `_internal` beside it. If you want the app somewhere else, move the whole folder.

The first time you run it, Windows shows a blue "Windows protected your PC" screen. This build is not code signed. Click **More info**, then **Run anyway**.

## First launch

A setup screen asks for your Anthropic API key. Paste it there.

The key is stored encrypted on your own machine using Windows DPAPI. It is sent only to Anthropic's API. It is never written to a file in plain text.

Once the key is saved, later launches go straight to the app.

Start SolidWorks before you ask for geometry.

## Troubleshooting

Start here. Open a Command Prompt in the `intentcad` folder and run:

```
intentcad.exe --preflight
```

It prints ten environment checks, with a short reason for anything that failed. Send that output with any problem report. It contains no API key material.

What the common results mean:

- **WebView2 runtime is missing.** IntentCADX uses the Microsoft Edge WebView2 runtime to draw its window. Install it from [developer.microsoft.com/microsoft-edge/webview2](https://developer.microsoft.com/microsoft-edge/webview2/), then launch again.
- **SolidWorks is not reachable.** Start SolidWorks and run the check again. This one is a warning, not a failure. The app still starts without it.
- **An API key is configured, shown as a warning.** No key has been saved yet. Paste one on the setup screen.
- **The app UI is bundled, shown as failing.** The download is incomplete. Unzip the whole folder again.

If the window never appears, run the preflight command above. Run it from Command Prompt rather than PowerShell. IntentCADX is a windowed program, and PowerShell does not capture output from those.

## Uninstalling

Delete the folder.

Your saved key and local settings live in `%APPDATA%\intentCAD`. Delete that folder to remove them.
