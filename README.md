# Forge Runtime Manager

Safety-focused runtime monitor for Stable Diffusion WebUI Forge.

Forge Runtime Manager helps make memory pressure easier to see and gives Forge
a chance to interrupt generation before the whole system becomes unresponsive.
It is designed for local Forge installs where RAM, VRAM, CPU swap, and launch
arguments can make the difference between a recoverable slow generation and a
full desktop lockup.

This extension is not a benchmark optimizer. It does not replace Forge's memory
manager, patch the model loader, or promise faster generation. Its main goal is
to keep the UI usable and make risky runtime states visible.

![Preview]()

## Features

- RAM and VRAM status panel inside the Forge UI
- Memory pressure states: normal, elevated, high, and critical
- CPU swap detection based on Forge console output
- Emergency stop when free system RAM drops below user-defined thresholds
- Conservative launch argument advisor for common Forge memory options
- Startup install skip suggestions for known extension dependency checks
- Optional FP8 launch argument helper for `webui-user.bat`
- Read-only maintenance checks for git state, update script safety, startup
  install behavior, and extension `install.py` files
- Telemetry export for troubleshooting recent runtime events

## Emergency Stop

The emergency stop feature watches available system RAM and uses Forge's normal
interrupt state when memory drops too low.

There are three user-controlled thresholds:

- Warning threshold: shows a warning when free RAM is low
- Sustained interrupt threshold: interrupts generation if RAM stays low for a
  configured number of seconds
- Instant interrupt threshold: interrupts immediately when RAM becomes critically
  low

The goal is to stop a dangerous generation before Windows becomes too slow to
click Forge's own interrupt button.

## Launch Argument Advisor

Forge Runtime Manager reads `webui-user.bat` and shows which launch arguments
are active, which ones are likely useless in modern Forge, and which ones may be
worth considering for the current system state.

The extension only writes to `webui-user.bat` after the user explicitly clicks a
button to add or remove an argument. Before writing, it creates a backup:

```text
webui-user.bat.forge-runtime-manager.bak
```

Recommended arguments are intentionally conservative. For example, FP8 is
recommended after memory pressure or CPU swap symptoms are detected, not simply
because a GPU has a certain amount of VRAM.

## Startup Install Checks

Some extensions run optional dependency checks during Forge startup. When Forge
Runtime Manager detects a known opt-out flag for an installed extension, it can
suggest adding that flag to `webui-user.bat`.

These suggestions are optional. They can reduce repeated startup dependency
checks when an extension already works on your setup, but optional features may
not be installed automatically afterward.

## FP8 Helper

The FP8 helper can add or remove:

```text
--unet-in-fp8-e4m3fn
```

This can reduce model VRAM usage on supported NVIDIA GPUs, especially RTX 30/40
series cards. The helper edits the launch argument only; Forge still needs to be
restarted before the change takes effect.

## Maintenance Checks

The Maintenance section is read-only. It checks local Forge health without
pulling updates or changing files:

- current git branch and commit
- whether the Forge worktree has local changes
- whether `update.bat` contains risky commands such as `git reset --hard`
- whether `--skip-install` is active
- whether extension `install.py` files may run startup dependency checks
- whether `psutil` is available for RAM monitoring

## Installation

Clone or copy this repository into your Forge `extensions` folder:

```text
webui/extensions/forge-runtime-manager
```

Then restart Forge.

## Optional Dependency

`psutil` is recommended for accurate RAM monitoring:

```text
pip install psutil
```

If `psutil` is not installed, VRAM-related features may still work, but RAM
pressure checks will be limited.

## Notes

- This extension is intended for Stable Diffusion WebUI Forge.
- It does not download models or install dependencies automatically.
- It does not modify model files, LoRAs, VAEs, or generated images.
- It only edits `webui-user.bat` when the user clicks an explicit apply button.
- Telemetry export writes a local `telemetry.json` file inside the extension
  folder.

## License

MIT License. See [LICENSE](LICENSE).
