# Downloading STEM-Access

STEM-Access runs as a native app on your own computer — no account, no
server, no data leaving your machine except your own PDF conversions
(which use your own Anthropic API key, entered inside the app).

## 1. Get an Anthropic API key

You'll need your own key to convert documents — the app doesn't come with
one built in. Create one at
[console.anthropic.com](https://console.anthropic.com) before your first
conversion (you can also do this after installing).

## 2. Download

Go to the [Releases page](../../releases) and download the file for your
operating system:

| Your OS | Download |
|---|---|
| macOS | `stem-access-macos.zip` |
| Windows | `stem-access-windows.zip` |
| Linux | `stem-access-linux.zip` |

## 3. Install and open

### macOS

1. Unzip the download.
2. Drag `STEM-Access.app` to your `Applications` folder.
3. **Right-click** (not double-click) `STEM-Access.app` and choose **Open**, then click **Open** again in the dialog that appears.
   - This app isn't signed with a paid Apple Developer certificate, so a plain double-click will refuse to open it and just say it's "damaged" or from an "unidentified developer." Right-click → Open is a one-time step — after that, it opens normally like any other app.

### Windows

1. Unzip the download.
2. Open the `STEM-Access` folder and double-click `STEM-Access.exe`.
3. If Windows shows a blue "Windows protected your PC" screen, click **More info**, then **Run anyway**.
   - Same cause as the macOS step above — the app isn't signed with a paid code-signing certificate, so Windows flags it as unrecognized. This is also a one-time step.

### Linux

1. Unzip the download.
2. In a terminal, make the binary executable and run it:
   ```bash
   chmod +x STEM-Access/STEM-Access
   ./STEM-Access/STEM-Access
   ```

## 4. Use it

The app opens its own window with the same upload form as the web version:
paste in your Anthropic API key, choose a PDF, and convert. Your key is
remembered in the app for next time — it's never sent anywhere except
directly to Anthropic for your own conversions.

Converted documents are saved automatically to:

- macOS: `~/Library/Application Support/STEM-Access/uploads/`
- Windows: `%APPDATA%\STEM-Access\uploads\`
- Linux: `~/.local/share/STEM-Access/uploads/`

## Troubleshooting

| Problem | Fix |
|---|---|
| macOS says the app is "damaged" or can't be opened | Right-click the app → **Open**, don't double-click (see step 3 above) |
| macOS still says "can't be opened" after right-click → Open, or Terminal says `permission denied` when run directly | Run `chmod +x /Applications/STEM-Access.app/Contents/MacOS/STEM-Access`, then try again — the download lost its executable permission bit |
| Windows SmartScreen blocks it | Click **More info** → **Run anyway** (see step 3 above) |
| "That Anthropic API key was rejected" | Double-check the key was copied correctly from [console.anthropic.com](https://console.anthropic.com) |
| Nothing happens when I open it | On Linux, make sure the binary is executable (`chmod +x`); on any OS, try running it from a terminal to see the error output |
