# Vobot App Gallery listing

Values for the [App Gallery](https://app.myvobot.com/) form (Profile → Create). The name must stay equal to `NAME` in `agent_sessions/__init__.py`.

| Field | Value |
|---|---|
| Name | Agent Sessions |
| Logo | `docs/logo.png` |
| Screenshots | `docs/screenshot-list.png`, `docs/screenshot-setup.png` |
| Tags | Productivity |
| Minimal OS version | 1.2.6 |
| Repository | https://github.com/rybnico/vobot-agent-sessions |
| Contact | https://github.com/rybnico/vobot-agent-sessions/issues |

## Description

```text
Shows your running Claude Code sessions on the Mini Dock: title, project, status, and whether a session is waiting for your answer.

- A session that needs you turns the header into an orange banner, pulses its row, lights the ambient light orange and wakes the screen.
- Statuses: Needs you, Working, Shell (background command running), Idle.
- Turn the encoder to scroll; the selected row scrolls long titles.

Requires the free bridge on the computer where you use Claude Code (macOS or Linux, Python 3). Setup guide: https://github.com/rybnico/vobot-agent-sessions
The app finds the bridge on your network automatically.

Not affiliated with Anthropic. Claude Code is a trademark of Anthropic.
```

## Releasing a version

1. Bump `version` in `agent_sessions/manifest.yml` and test the app on a device with `./deploy.sh <device>`.
2. Copy the app folder to a neutral path first, e.g. `rm -rf /Users/Shared/agent_sessions && cp -R agent_sessions /Users/Shared/`: the bundler compiles with absolute paths, and the compiled files keep the source path (with your user name) for error messages. Then open Vobot's Dock App Bundler ([downloads](https://dock.myvobot.com/developer/guides/publishing-guide/)), select `/Users/Shared/agent_sessions` and save the `.vbt` (max. 650 KB). Check with `strings` on the `.mpy` files in the package that no personal path is left. The bundler is unsigned; on macOS open it once via right-click → Open.
3. In the App Gallery, open Profile → Agent Sessions → Upload, enter the version and release notes, and upload the `.vbt`. Vobot reviews each upload (up to 15 working days).
