"""
Forge Runtime Manager V1 - safety-first runtime monitor.

Primary goals:
- Prevent system lockups with conservative emergency stops.
- Show RAM/VRAM and CPU swap pressure clearly.
- Recommend launch arguments only when they match the user's symptoms.
- Avoid invasive UI/DOM/runtime hooks unless they are proven useful.
"""

import gc
import html
import sys
import os
import re
import json
import time
import threading
import collections
import subprocess
from datetime import datetime
from pathlib import Path

import gradio as gr
import modules.scripts as scripts
from modules import script_callbacks, shared
from modules.shared import opts
from modules.ui_components import InputAccordion

try:
    import torch
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False

try:
    from backend import memory_management
    FORGE_MEMORY_AVAILABLE = True
except ImportError:
    FORGE_MEMORY_AVAILABLE = False

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

VERSION = "1.0.3"
DISPLAY_NAME = "Forge Runtime Manager"


# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------
EXTENSION_DIR = Path(__file__).parent.parent
WEBUI_DIR     = Path(__file__).parent.parent.parent.parent
BAT_FILE      = WEBUI_DIR / "webui-user.bat"
UPDATE_BAT_FILE = WEBUI_DIR.parent / "update.bat"
EXTENSIONS_DIR = WEBUI_DIR / "extensions"
TELEMETRY_FILE = EXTENSION_DIR / "telemetry.json"
BAT_BACKUP_FILE = WEBUI_DIR / "webui-user.bat.forge-runtime-manager.bak"


# -----------------------------------------------------------------------------
# Pressure States
# -----------------------------------------------------------------------------
PRESSURE_NORMAL   = "normal"
PRESSURE_ELEVATED = "elevated"
PRESSURE_HIGH     = "high"
PRESSURE_CRITICAL = "critical"

PRESSURE_CONFIG = {
    PRESSURE_NORMAL:   {"preview_fps": 60,  "label": "Normal",   "color": "#2ecc71"},
    PRESSURE_ELEVATED: {"preview_fps": 12,  "label": "Elevated", "color": "#f39c12"},
    PRESSURE_HIGH:     {"preview_fps": 6,   "label": "High",     "color": "#e67e22"},
    PRESSURE_CRITICAL: {"preview_fps": 1,   "label": "Critical", "color": "#e74c3c"},
}
PRESSURE_ORDER = [PRESSURE_NORMAL, PRESSURE_ELEVATED, PRESSURE_HIGH, PRESSURE_CRITICAL]

HYSTERESIS = {
    (PRESSURE_NORMAL,   PRESSURE_ELEVATED): (2.0, 1.0),
    (PRESSURE_ELEVATED, PRESSURE_HIGH):     (5.0, 3.0),
    (PRESSURE_HIGH,     PRESSURE_CRITICAL): (10.0, 6.0),
}

# -----------------------------------------------------------------------------
# Global telemetry
# -----------------------------------------------------------------------------
_pressure_state       = PRESSURE_NORMAL
_pressure_lock        = threading.Lock()
_swap_score           = 0.0
_swap_score_lock      = threading.Lock()
_swap_detected_count  = 0
_vram_history         = collections.deque(maxlen=8)
_gen_latency_history  = collections.deque(maxlen=20)
_last_generation_time = time.time()
_generation_count     = 0
_consecutive_stable   = 0
_idle_thread_running  = False

# Emergency Stop state
_emergency_low_ram_since = None
_emergency_stage1_active = False
_emergency_interrupted   = False

# Telemetry
_telemetry_events = collections.deque(maxlen=500)


# -----------------------------------------------------------------------------
# Telemetry
# -----------------------------------------------------------------------------
def log_event(event_type, data=None):
    _telemetry_events.append({
        "time": datetime.now().isoformat(),
        "type": event_type,
        "data": data or {}
    })


def export_telemetry():
    try:
        with open(TELEMETRY_FILE, "w", encoding="utf-8") as f:
            json.dump({"events": list(_telemetry_events),
                       "exported": datetime.now().isoformat()}, f, indent=2)
        return str(TELEMETRY_FILE)
    except Exception as e:
        return f"Export failed: {e}"


# -----------------------------------------------------------------------------
# Memory info
# -----------------------------------------------------------------------------
def get_vram_info():
    if not TORCH_AVAILABLE or not torch.cuda.is_available():
        return None
    try:
        free_bytes, total_bytes = torch.cuda.mem_get_info(0)
        used_bytes = total_bytes - free_bytes
        return (used_bytes / 1024**2, total_bytes / 1024**2, free_bytes / 1024**2)
    except Exception:
        return None


def get_ram_info():
    if not PSUTIL_AVAILABLE:
        return None
    try:
        mem = psutil.virtual_memory()
        return (mem.used / 1024**2, mem.total / 1024**2, mem.available / 1024**2)
    except Exception:
        return None


def get_free_ram_mb():
    ram = get_ram_info()
    return ram[2] if ram else 99999.0


def get_vram_ratio():
    vram = get_vram_info()
    if not vram:
        return 0.0
    used, total, _ = vram
    ratio = used / total
    _vram_history.append(ratio)
    return ratio


def get_vram_trend():
    if len(_vram_history) < 4:
        return 0
    recent = list(_vram_history)[-4:]
    slope = recent[-1] - recent[0]
    return 1 if slope > 0.05 else (-1 if slope < -0.05 else 0)


# -----------------------------------------------------------------------------
# Decaying swap score
# -----------------------------------------------------------------------------
def decay_swap_score(stable=False):
    global _swap_score, _consecutive_stable
    with _swap_score_lock:
        if stable:
            _consecutive_stable += 1
            decay = max(0.75, 0.90 - (_consecutive_stable * 0.03))
            _swap_score *= decay
        else:
            _consecutive_stable = 0
            _swap_score *= 0.97
        _swap_score = max(0.0, _swap_score)


def add_swap_event(count=1):
    global _swap_score, _consecutive_stable
    with _swap_score_lock:
        _swap_score += count * 2.0
        _consecutive_stable = 0
    log_event("swap_detected", {"count": count})


def get_swap_score():
    with _swap_score_lock:
        return _swap_score


# -----------------------------------------------------------------------------
# Pressure evaluation
# -----------------------------------------------------------------------------
def evaluate_pressure():
    free_ram   = get_free_ram_mb()
    vram_ratio = get_vram_ratio()
    swap_score = get_swap_score()
    vram_trend = get_vram_trend()
    current    = get_pressure_state()

    # RAM-based (absolute free MB)
    if free_ram < 800:    ram_state = PRESSURE_CRITICAL
    elif free_ram < 1500: ram_state = PRESSURE_HIGH
    elif free_ram < 3000: ram_state = PRESSURE_ELEVATED
    else:                 ram_state = PRESSURE_NORMAL

    # Swap-score-based
    if swap_score >= 10.0:   swap_state = PRESSURE_CRITICAL
    elif swap_score >= 5.0:  swap_state = PRESSURE_HIGH
    elif swap_score >= 2.0:  swap_state = PRESSURE_ELEVATED
    else:                    swap_state = PRESSURE_NORMAL

    # Predictive: VRAM rising
    vram_state = PRESSURE_ELEVATED if (vram_trend == 1 and vram_ratio > 0.70) else PRESSURE_NORMAL

    candidates  = [ram_state, swap_state, vram_state]
    target      = PRESSURE_ORDER[max(PRESSURE_ORDER.index(s) for s in candidates)]
    current_idx = PRESSURE_ORDER.index(current)
    target_idx  = PRESSURE_ORDER.index(target)

    if target_idx > current_idx:
        pair = (current, target)
        thresholds = HYSTERESIS.get(pair)
        if thresholds:
            escalate_thresh, _ = thresholds
            if swap_state == target or ram_state == target:
                return target
            elif swap_score >= escalate_thresh:
                return target
            return current
        return target
    elif target_idx < current_idx:
        pair = (target, current)
        thresholds = HYSTERESIS.get(pair)
        if thresholds:
            _, recover_thresh = thresholds
            if swap_score <= recover_thresh and ram_state != current:
                return target
            return current
        return target
    return current


def update_pressure():
    new_state = evaluate_pressure()
    old_state = get_pressure_state()
    if old_state != new_state:
        print(f"[ForgeRuntimeManager] Pressure: {old_state}  {new_state} (score={get_swap_score():.1f})")
        log_event("pressure_change", {"from": old_state, "to": new_state})
    set_pressure_state(new_state)
    return new_state


def set_pressure_state(new_state):
    global _pressure_state
    with _pressure_lock:
        _pressure_state = new_state


def get_pressure_state():
    return _pressure_state


# -----------------------------------------------------------------------------
# Emergency Stop  3-stage
# -----------------------------------------------------------------------------
def _do_interrupt(reason):
    global _emergency_interrupted
    _emergency_interrupted = True
    log_event("emergency_stop", {"reason": reason, "free_ram_mb": get_free_ram_mb()})
    try:
        shared.state.interrupted = True
    except Exception:
        pass
    scaled_cleanup(PRESSURE_CRITICAL)
    print(f"[ForgeRuntimeManager]  EMERGENCY STOP: {reason}")


def check_emergency_stop():
    global _emergency_low_ram_since, _emergency_stage1_active, _emergency_interrupted

    free_ram = get_free_ram_mb()

    # Instant stop  no waiting
    instant_mb = getattr(opts, "forge_opt_emergency_instant_mb", 100)
    if free_ram < instant_mb and not _emergency_interrupted:
        _do_interrupt(f"RAM instant threshold ({free_ram:.0f} MB < {instant_mb} MB)")
        return

    # Stage 1  warning only
    stage1_mb = getattr(opts, "forge_opt_emergency_stage1_mb", 1000)
    if free_ram < stage1_mb and not _emergency_stage1_active:
        _emergency_stage1_active = True
        print(f"[ForgeRuntimeManager]  Stage 1: RAM low ({free_ram:.0f} MB free)")
        log_event("emergency_stage1", {"free_ram_mb": free_ram})
    elif free_ram >= stage1_mb:
        _emergency_stage1_active = False

    # Stage 2  sustained interrupt
    stage2_mb = getattr(opts, "forge_opt_emergency_stage2_mb", 350)
    stage2_s  = getattr(opts, "forge_opt_emergency_duration_s", 10)

    if free_ram < stage2_mb:
        if _emergency_low_ram_since is None:
            _emergency_low_ram_since = time.time()
        elif (time.time() - _emergency_low_ram_since >= stage2_s
              and not _emergency_interrupted):
            _do_interrupt(f"RAM sustained {stage2_s}s ({free_ram:.0f} MB < {stage2_mb} MB)")
    else:
        _emergency_low_ram_since = None
        _emergency_interrupted   = False


# -----------------------------------------------------------------------------
# Cleanup
# -----------------------------------------------------------------------------
def scaled_cleanup(state):
    if state == PRESSURE_NORMAL:
        pass
    elif state == PRESSURE_ELEVATED:
        gc.collect()
    elif state in (PRESSURE_HIGH, PRESSURE_CRITICAL):
        try:
            if FORGE_MEMORY_AVAILABLE:
                memory_management.soft_empty_cache()
            elif TORCH_AVAILABLE and torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
            gc.collect()
            log_event("cleanup", {"state": state})
        except Exception as e:
            print(f"[ForgeRuntimeManager] cleanup error: {e}")


def log_memory_state(label=""):
    vram = get_vram_info()
    ram  = get_ram_info()
    if vram:
        u, t, f = vram
        print(f"[ForgeRuntimeManager] {label} VRAM: {u:.0f}/{t:.0f} MB ({u/t*100:.1f}%)")
    if ram:
        u, t, f = ram
        print(f"[ForgeRuntimeManager] {label} RAM:  {u:.0f}/{t:.0f} MB ({f:.0f} MB free)")


# -----------------------------------------------------------------------------
# Stdout interceptor  CPU swap detection
# -----------------------------------------------------------------------------
class _SwapDetector:
    def __init__(self, original):
        self._original     = original
        self._swap_this_gen = 0

    def write(self, text):
        global _swap_detected_count
        if "CPU Swap Loaded" in text:
            _swap_detected_count += 1
            self._swap_this_gen += 1
            add_swap_event(1)
        self._original.write(text)

    def flush(self):
        self._original.flush()

    def __getattr__(self, name):
        return getattr(self._original, name)

    def reset_gen_count(self):
        n = self._swap_this_gen
        self._swap_this_gen = 0
        return n


def install_swap_detector():
    if not isinstance(sys.stdout, _SwapDetector):
        sys.stdout = _SwapDetector(sys.stdout)


def get_swap_detector():
    return sys.stdout if isinstance(sys.stdout, _SwapDetector) else None


# -----------------------------------------------------------------------------
# FP8 helpers
# -----------------------------------------------------------------------------
def is_fp8_active():
    if not TORCH_AVAILABLE:
        return False
    try:
        if shared.sd_model is None:
            return False
        if hasattr(shared.sd_model, 'forge_objects'):
            unet = getattr(shared.sd_model.forge_objects, 'unet', None)
            if unet and hasattr(unet, 'model'):
                for p in unet.model.parameters():
                    return 'float8' in str(p.dtype)
    except Exception:
        pass
    return False


def is_fp8_in_bat():
    try:
        if not BAT_FILE.exists():
            return False
        return "--unet-in-fp8-e4m3fn" in BAT_FILE.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False


def backup_bat_file():
    try:
        if not BAT_FILE.exists():
            return False, "webui-user.bat not found"
        if not BAT_BACKUP_FILE.exists():
            BAT_BACKUP_FILE.write_text(
                BAT_FILE.read_text(encoding="utf-8", errors="ignore"),
                encoding="utf-8",
            )
            log_event("bat_backup_created", {"path": str(BAT_BACKUP_FILE)})
        return True, str(BAT_BACKUP_FILE)
    except Exception as e:
        return False, str(e)


def add_fp8_to_bat():
    return add_arg_to_bat("--unet-in-fp8-e4m3fn")


def remove_fp8_from_bat():
    return remove_arg_from_bat("--unet-in-fp8-e4m3fn")


def should_recommend_fp8():
    vram = get_vram_info()
    if not vram:
        return False
    _, total, _ = vram
    return total < 10 * 1024 and _swap_detected_count > 0


# -----------------------------------------------------------------------------
# Startup install skip advisor
# -----------------------------------------------------------------------------

# Known extension-specific environment flags that can reduce startup dependency
# checks. These are shown only when the matching extension appears to exist.
STARTUP_SKIP_FLAGS = [
    {
        "name": "IIB_SKIP_OPTIONAL_DEPS",
        "value": "1",
        "label": "Optional dependency skip flag",
        "short_desc": "May reduce repeated optional dependency checks during startup.",
        "long_desc": (
            "Adds a known startup skip flag for a detected extension. Use this "
            "when the extension already works on your setup and you want to avoid "
            "repeated optional dependency checks at launch. Optional features may "
            "not be installed automatically afterward."
        ),
        "extension_dir": "sd-webui-infinite-image-browsing",
    },
]


def read_bat_env_vars():
    """Returns environment variables set in webui-user.bat."""
    try:
        if not BAT_FILE.exists():
            return {}
        content = BAT_FILE.read_text(encoding="utf-8", errors="ignore")
        env = {}
        for line in content.splitlines():
            if line.strip().lower().startswith("rem"):
                continue
            m = re.match(r'^\s*set\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$', line, re.IGNORECASE)
            if m:
                env[m.group(1).upper()] = m.group(2)
        return env
    except Exception:
        return {}


def detected_startup_skip_flags():
    detected = []
    for flag in STARTUP_SKIP_FLAGS:
        ext_dir = flag.get("extension_dir")
        if ext_dir and not (EXTENSIONS_DIR / ext_dir).exists():
            continue
        detected.append(flag)
    return detected


def recommended_startup_skip_flags():
    env = read_bat_env_vars()
    recs = []
    for flag in detected_startup_skip_flags():
        if env.get(flag["name"].upper()) == flag["value"]:
            continue
        recs.append(flag["name"])
    return recs


def add_env_to_bat(name, value):
    """Add a single set NAME=value line to webui-user.bat."""
    try:
        if not BAT_FILE.exists():
            return False, "webui-user.bat not found"
        content = BAT_FILE.read_text(encoding="utf-8", errors="ignore")
        if re.search(rf'(?im)^\s*set\s+{re.escape(name)}\s*=', content):
            return True, f"{name} already present"
        ok, backup_msg = backup_bat_file()
        if not ok:
            return False, f"Backup failed: {backup_msg}"

        line_to_add = f"set {name}={value}"
        commandline_pattern = r'(?im)^(?![ \t]*(?:rem\b|::))([ \t]*set[ \t]+COMMANDLINE_ARGS\b.*)$'
        if re.search(commandline_pattern, content):
            new_content = re.sub(
                commandline_pattern,
                lambda m: line_to_add + "\n" + m.group(1),
                content,
                count=1,
            )
        else:
            suffix = "" if content.endswith(("\n", "\r")) else "\n"
            new_content = content + suffix + line_to_add + "\n"

        BAT_FILE.write_text(new_content, encoding="utf-8")
        log_event("env_added", {"name": name, "value": value})
        return True, f"Added {name}={value}"
    except Exception as e:
        return False, str(e)


def build_startup_skip_html():
    env = read_bat_env_vars()
    detected = detected_startup_skip_flags()
    recommended = set(recommended_startup_skip_flags())
    lines = []

    active = [flag for flag in detected if env.get(flag["name"].upper()) == flag["value"]]
    if active:
        lines.append("<div style='font-family:monospace;font-size:11px;margin-bottom:6px'>")
        lines.append("<span style='color:#888'>Active startup skips:</span> ")
        for flag in active:
            lines.append(f"<span style='color:#2ecc71;margin-right:6px' "
                         f"title='{html_escape(flag['short_desc'])}'>OK {html_escape(flag['name'])}</span>")
        lines.append("</div>")

    recs = [flag for flag in detected if flag["name"] in recommended]
    if recs:
        lines.append("<div style='font-family:monospace;font-size:11px;margin-top:8px'>")
        lines.append("<span style='color:#888'>Startup install suggestions:</span>")
        for flag in recs:
            lines.append(
                f"<details style='margin-top:5px;border:1px solid #333;border-radius:3px;padding:4px 6px'>"
                f"<summary style='cursor:pointer;list-style:none;display:flex;align-items:center'>"
                f"<span style='color:#3498db;margin-right:6px'>OPT</span>"
                f"<b style='color:#ddd'>{html_escape(flag['name'])}={html_escape(flag['value'])}</b>"
                f"<span style='color:#888;margin-left:8px;font-size:10px'>{html_escape(flag['short_desc'])}</span>"
                f"</summary>"
                f"<div style='color:#888;padding:4px 0 2px 16px;font-size:10px;line-height:1.5'>"
                f"{html_escape(flag['long_desc'])}</div>"
                f"</details>"
            )
        lines.append("</div>")
    elif not active:
        lines.append("<div style='color:#888;font-family:monospace;font-size:11px;margin-top:8px'>"
                     "No known startup install skip flags detected for this setup.</div>")

    return "".join(lines)


def add_startup_skip_flag(flag_name):
    for flag in STARTUP_SKIP_FLAGS:
        if flag["name"] == flag_name:
            return add_env_to_bat(flag["name"], flag["value"])
    return False, f"Unknown startup flag: {flag_name}"


# -----------------------------------------------------------------------------
# Launch Arguments Advisor
# Reads bat file, compares against known-good args for Forge
# Shows only what's relevant to THIS system's situation
# -----------------------------------------------------------------------------

# Arguments that LOOK valid but DO NOTHING in Forge
# Forge removed these  showing them as "useless" helps users clean up
FORGE_REMOVED_ARGS = {
    "--medvram":       "Removed in Forge  has no effect. Forge manages VRAM automatically.",
    "--lowvram":       "Removed in Forge  has no effect. Forge manages VRAM automatically.",
    "--medvram-sdxl":  "Removed in Forge  has no effect. Forge manages VRAM automatically.",
    "--no-half":       "Removed in Forge  has no effect.",
    "--no-half-vae":   "Removed in Forge  has no effect.",
    "--precision":     "Removed in Forge  has no effect.",
    "--upcast-sampling": "Removed in Forge  has no effect.",
}

# Arguments that still work in Forge, with context-aware display rules
# Each entry: (arg, label, short_desc, long_desc, show_when_fn, conflicts_with)
# show_when_fn: function() -> bool, True = show this recommendation
FORGE_VALID_ARGS = [
    (
        "--unet-in-fp8-e4m3fn",
        "FP8 (e4m3fn) - halve model VRAM",
        "Stores model weights in FP8. Cuts VRAM usage ~50%. Minimal quality difference.",
        "Best for RTX 30/40 series. Recommended when CPU swap is detected. "
        "Incompatible with --unet-in-fp8-e5m2 (use one or the other).",
        lambda: _swap_detected_count > 0,
        ["--unet-in-fp8-e5m2"],
    ),
    (
        "--unet-in-fp8-e5m2",
        "FP8 (e5m2) - alternative FP8 variant",
        "Alternative FP8 format with wider range but less precision.",
        "Use if e4m3fn causes artifacts on your specific model. "
        "Less common than e4m3fn. Incompatible with --unet-in-fp8-e4m3fn.",
        lambda: False,  # Only show if user explicitly looks  not auto-recommended
        ["--unet-in-fp8-e4m3fn"],
    ),
    (
        "--always-offload-from-vram",
        "Always offload from VRAM",
        "Unloads model from VRAM after each generation. Frees VRAM between runs.",
        "Makes first image of each session slower (model must reload). "
        "Useful if you run other GPU apps alongside Forge, or have very little VRAM. "
        "Note: Forge's README says this makes things slower but less risky.",
        lambda: get_swap_score() >= 8.0 or get_pressure_state() == PRESSURE_CRITICAL,
        [],
    ),
    (
        "--cuda-malloc",
        "CUDA malloc async",
        "Uses cudaMallocAsync  faster, lower fragmentation GPU allocator.",
        "Recommended for RTX 30/40 series with recent drivers. "
        "May cause issues on very old drivers (pre-2021). "
        "Generally safe and beneficial.",
        lambda: True,
        [],
    ),
    (
        "--opt-channelslast",
        "Channel-last memory format",
        "Optimizes memory layout for NVIDIA convolution operations.",
        "Small performance gain on NVIDIA GPUs. No downside on modern hardware. "
        "Has no effect on AMD GPUs.",
        lambda: True,
        [],
    ),
    (
        "--disable-nan-check",
        "Disable NaN check",
        "Skips NaN validation in generated images. Slightly faster.",
        "Safe to enable if you rarely get corrupted/black images. "
        "If you do get black images, disable this first before investigating.",
        lambda: False,
        [],
    ),
]


def recommended_launch_args():
    current_args = read_bat_args()
    recs = []
    for arg, label, short_desc, long_desc, show_when_fn, conflicts in FORGE_VALID_ARGS:
        if arg in current_args:
            continue
        if any(c in current_args for c in conflicts):
            continue
        try:
            if show_when_fn():
                recs.append(arg)
        except Exception:
            pass
    return recs


def read_bat_args():
    """Returns set of arguments currently in webui-user.bat."""
    try:
        if not BAT_FILE.exists():
            return set()
        content = BAT_FILE.read_text(encoding="utf-8", errors="ignore")
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("rem") or stripped.startswith("::"):
                continue
            m = re.match(r'^[ \t]*set[ \t]+COMMANDLINE_ARGS[ \t]*=[ \t]*(.*)$', line, re.IGNORECASE)
            if m:
                args_part = m.group(1)
                return set(re.findall(r'--[\w-]+', args_part))
        return set()
    except Exception:
        return set()


def add_arg_to_bat(arg):
    """Add a single argument to COMMANDLINE_ARGS in bat file."""
    try:
        if not BAT_FILE.exists():
            return False, "webui-user.bat not found"
        content = BAT_FILE.read_text(encoding="utf-8", errors="ignore")
        if arg in read_bat_args():
            return True, f"{arg} already present"
        ok, backup_msg = backup_bat_file()
        if not ok:
            return False, f"Backup failed: {backup_msg}"
        commandline_pattern = r'(?im)^(?![ \t]*(?:rem\b|::))([ \t]*set[ \t]+COMMANDLINE_ARGS[ \t]*=[ \t]*)(.*)$'
        new_content, count = re.subn(
            commandline_pattern,
            lambda m: m.group(1) + (m.group(2).rstrip() + f" {arg}").strip(),
            content,
            count=1,
        )
        if count == 0:
            return False, "COMMANDLINE_ARGS line not found"
        BAT_FILE.write_text(new_content, encoding="utf-8")
        log_event("arg_added", {"arg": arg})
        return True, f"Added {arg}"
    except Exception as e:
        return False, str(e)


def remove_arg_from_bat(arg):
    """Remove a single argument from bat file."""
    try:
        if not BAT_FILE.exists():
            return False, "webui-user.bat not found"
        if arg not in read_bat_args():
            return True, f"{arg} not present"
        ok, backup_msg = backup_bat_file()
        if not ok:
            return False, f"Backup failed: {backup_msg}"
        content = BAT_FILE.read_text(encoding="utf-8", errors="ignore")
        def remove_from_commandline(match):
            prefix, args_text = match.group(1), match.group(2)
            args = [item for item in args_text.split() if item != arg]
            return prefix + " ".join(args)

        commandline_pattern = r'(?im)^(?![ \t]*(?:rem\b|::))([ \t]*set[ \t]+COMMANDLINE_ARGS[ \t]*=[ \t]*)(.*)$'
        new_content, count = re.subn(
            commandline_pattern,
            remove_from_commandline,
            content,
            count=1,
        )
        if count == 0:
            return False, "COMMANDLINE_ARGS line not found"
        BAT_FILE.write_text(new_content, encoding="utf-8")
        log_event("arg_removed", {"arg": arg})
        return True, f"Removed {arg}"
    except Exception as e:
        return False, str(e)


def build_launch_args_html():
    """
    Build launch arguments advisor panel.
    - Shows removed/useless args found in bat (with remove button hint)
    - Shows recommended args not yet in bat (context-aware)
    - Shows active args as green checkmarks
    - Details hidden behind <details> tag  not cluttering the view
    """
    current_args = read_bat_args()
    vram = get_vram_info()
    lines = []

    #  Warn about args that do nothing in Forge 
    useless_found = [(arg, desc) for arg, desc in FORGE_REMOVED_ARGS.items()
                     if arg in current_args]
    if useless_found:
        lines.append("<div style='background:#2c1810;border:1px solid #e74c3c;border-radius:4px;"
                     "padding:6px 8px;margin-bottom:8px;font-family:monospace;font-size:11px'>")
        lines.append("<b style='color:#e74c3c'>Inactive arguments detected</b> "
                     "<span style='color:#888'>- these do nothing in Forge:</span><br>")
        for arg, desc in useless_found:
            lines.append(f"<details style='margin-top:3px'>"
                         f"<summary style='cursor:pointer;color:#f39c12'>{arg}</summary>"
                         f"<div style='color:#888;padding:2px 0 2px 8px'>{desc}</div>"
                         f"</details>")
        lines.append("<div style='color:#888;margin-top:4px;font-size:10px'>"
                     "Consider removing these from webui-user.bat.</div>")
        lines.append("</div>")

    # Build recommendations from symptom-based rules only.
    recommended_args = set(recommended_launch_args())
    recs = []
    for arg, label, short_desc, long_desc, show_when_fn, conflicts in FORGE_VALID_ARGS:
        if arg in recommended_args:
            recs.append((arg, label, short_desc, long_desc))

    #  Render active 
    if current_args & {a for a, *_ in FORGE_VALID_ARGS}:
        lines.append("<div style='font-family:monospace;font-size:11px;margin-bottom:6px'>")
        lines.append("<span style='color:#888'>Active:</span> ")
        for arg, label, *_ in FORGE_VALID_ARGS:
            if arg in current_args:
                lines.append(f"<span style='color:#2ecc71;margin-right:6px' "
                              f"title='{label}'>OK {arg}</span>")
        lines.append("</div>")

    #  Render recommendations 
    if recs:
        lines.append("<div style='font-family:monospace;font-size:11px'>")
        lines.append("<span style='color:#888'>Suggested for your setup:</span>")
        for arg, label, short_desc, long_desc in recs:
            lines.append(
                f"<details style='margin-top:5px;border:1px solid #333;border-radius:3px;padding:4px 6px'>"
                f"<summary style='cursor:pointer;list-style:none;display:flex;align-items:center'>"
                f"<span style='color:#f39c12;margin-right:6px'>REC</span>"
                f"<b style='color:#ddd'>{arg}</b>"
                f"<span style='color:#888;margin-left:8px;font-size:10px'>{short_desc}</span>"
                f"</summary>"
                f"<div style='color:#888;padding:4px 0 2px 16px;font-size:10px;line-height:1.5'>"
                f"{long_desc}</div>"
                f"</details>"
            )
        lines.append("</div>")
    elif not useless_found:
        lines.append("<div style='color:#2ecc71;font-family:monospace;font-size:11px'>"
                     "No additional arguments recommended for your current setup.</div>")

    if not lines:
        lines.append("<div style='color:#aaa;font-family:monospace;font-size:11px'>"
                     "Could not read webui-user.bat</div>")

    return "".join(lines)


def apply_selected_args(selected_args_json):
    """Apply a list of arguments to bat file. Called from JS checkbox selection."""
    try:
        args = json.loads(selected_args_json) if selected_args_json else []
        results = []
        for arg in args:
            ok, msg = add_arg_to_bat(arg)
            results.append(f"{'OK' if ok else 'WARN'}: {msg}")
        return (f"<div style='color:#2ecc71;font-family:monospace;font-size:11px'>"
                f"{'<br>'.join(results)}<br>"
                f"<span style='color:#888'>Restart Forge to apply.</span></div>")
    except Exception as e:
        return f"<div style='color:#e74c3c;font-family:monospace;font-size:11px'>Error: {e}</div>"


# -----------------------------------------------------------------------------
# Maintenance checks (read-only)
# -----------------------------------------------------------------------------
def html_escape(value):
    return html.escape(str(value), quote=True)


def read_file_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def git_candidates():
    candidates = []
    env_git = os.environ.get("GIT")
    if env_git:
        candidates.append(Path(env_git))
    candidates.append(WEBUI_DIR.parent / "system" / "git" / "bin" / "git.exe")
    candidates.append("git")
    return candidates


def run_git(args, timeout=5):
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    last_error = "git not found"
    for candidate in git_candidates():
        try:
            cmd = [str(candidate), "-C", str(WEBUI_DIR)] + list(args)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=creationflags,
            )
            return result.returncode, result.stdout.strip(), result.stderr.strip(), str(candidate)
        except FileNotFoundError as e:
            last_error = str(e)
        except Exception as e:
            last_error = str(e)
    return None, "", last_error, ""


def maintenance_badge(label, state, detail):
    colors = {
        "ok": "#2ecc71",
        "warn": "#f39c12",
        "bad": "#e74c3c",
        "info": "#3498db",
    }
    color = colors.get(state, "#888")
    return (
        "<div style='border:1px solid #333;border-radius:3px;padding:5px 7px;"
        "margin-top:5px;font-family:monospace;font-size:11px'>"
        f"<b style='color:{color}'>{html_escape(label)}</b>"
        f"<span style='color:#999;margin-left:8px'>{html_escape(detail)}</span>"
        "</div>"
    )


def build_maintenance_html():
    lines = [
        "<div style='font-family:monospace;font-size:11px;line-height:1.45'>",
        "<div style='color:#888;margin-bottom:5px'>Read-only checks. This does not pull, update, or edit files.</div>",
    ]

    code, branch, err, git_used = run_git(["branch", "--show-current"])
    _, commit, _, _ = run_git(["rev-parse", "--short", "HEAD"])
    status_code, status, status_err, _ = run_git(["status", "--short"])
    if code == 0 and commit:
        dirty_count = len([line for line in status.splitlines() if line.strip()])
        if dirty_count:
            detail = f"{branch or 'unknown'} @ {commit}; {dirty_count} local change(s). Update should be manual."
            lines.append(maintenance_badge("Git worktree", "warn", detail))
        else:
            detail = f"{branch or 'unknown'} @ {commit}; clean. git: {git_used}"
            lines.append(maintenance_badge("Git worktree", "ok", detail))
    else:
        lines.append(maintenance_badge("Git worktree", "warn", err or status_err or "Unable to read git status."))

    update_text = read_file_text(UPDATE_BAT_FILE)
    if not update_text:
        lines.append(maintenance_badge("Update script", "warn", "update.bat not found or unreadable."))
    elif "reset --hard" in update_text:
        lines.append(maintenance_badge("Update script", "bad", "Contains git reset --hard. Do not use until reviewed."))
    elif "pull --ff-only" in update_text and "Local changes detected" in update_text:
        lines.append(maintenance_badge("Update script", "ok", "Safe mode: refuses local changes and never resets automatically."))
    else:
        lines.append(maintenance_badge("Update script", "info", "No reset --hard found, but script is not the known safe template."))

    bat_text = read_file_text(BAT_FILE)
    current_args = read_bat_args()
    if "--skip-install" in current_args:
        lines.append(maintenance_badge("Startup installs", "ok", "--skip-install is active in webui-user.bat."))
    else:
        lines.append(maintenance_badge("Startup installs", "warn", "--skip-install is not active; extensions may run install checks on startup."))

    active_skip_flags = [
        flag["name"] for flag in detected_startup_skip_flags()
        if read_bat_env_vars().get(flag["name"].upper()) == flag["value"]
    ]
    if active_skip_flags:
        detail = f"Known optional install skip active: {', '.join(active_skip_flags)}"
        lines.append(maintenance_badge("Startup skip flags", "ok", detail))

    install_scripts = []
    try:
        install_scripts = sorted(
            p.parent.name for p in EXTENSIONS_DIR.glob("*/install.py")
            if p.is_file()
        )
    except Exception:
        pass
    if install_scripts:
        detail = f"{len(install_scripts)} extension install.py file(s): {', '.join(install_scripts[:6])}"
        if len(install_scripts) > 6:
            detail += ", ..."
        lines.append(maintenance_badge("Extension installers", "info", detail))

    if PSUTIL_AVAILABLE:
        lines.append(maintenance_badge("RAM monitor", "ok", "psutil is available."))
    else:
        lines.append(maintenance_badge("RAM monitor", "warn", "psutil is missing; RAM pressure checks are limited."))

    lines.append("</div>")
    return "".join(lines)


# -----------------------------------------------------------------------------
# Status panel HTML
# -----------------------------------------------------------------------------
def build_status_html():
    state  = get_pressure_state()
    config = PRESSURE_CONFIG[state]
    color  = config["color"]
    label  = config["label"]
    vram   = get_vram_info()
    ram    = get_ram_info()
    score  = get_swap_score()
    trend  = get_vram_trend()
    trend_icon = "up" if trend == 1 else "down" if trend == -1 else "stable"
    lines  = []

    # Emergency banner
    if _emergency_interrupted:
        lines.append("<div style='background:#3a0000;border:2px solid #e74c3c;border-radius:4px;"
                     "padding:7px;margin-bottom:8px;font-family:monospace'>"
                     "<b style='color:#e74c3c'>EMERGENCY STOP</b> - Generation interrupted to protect system.<br>"
                     "<span style='color:#aaa;font-size:11px'>Restart generation when ready.</span></div>")
    elif _emergency_stage1_active:
        lines.append("<div style='background:#2c1810;border:1px solid #e67e22;border-radius:4px;"
                     "padding:7px;margin-bottom:8px;font-family:monospace'>"
                     "<b style='color:#e67e22'>RAM Warning</b> - Low free RAM, approaching emergency threshold.</div>")

    # Pressure indicator
    lines.append(f"<div style='border-left:3px solid {color};padding:5px 10px;margin-bottom:8px;"
                 f"background:rgba(0,0,0,0.15);font-family:monospace'>"
                 f"<span style='color:{color};font-weight:bold'>{label}</span>"
                 f"<span style='color:#666;font-size:11px;margin-left:8px'>"
                 f"FPS cap: {config['preview_fps']} | Swap score: {score:.1f} | VRAM: {trend_icon}</span></div>")

    # VRAM
    if vram:
        u, t, f = vram
        pct = u/t*100
        c = "#e74c3c" if pct > 85 else "#f39c12" if pct > 70 else "#2ecc71"
        lines.append(f"<div style='font-family:monospace;font-size:12px;margin-bottom:3px'>"
                     f"<span style='color:#888'>VRAM:</span> <b>{u:.0f}/{t:.0f} MB</b> "
                     f"<span style='color:{c}'>({pct:.0f}%)</span></div>")

    # RAM
    if ram:
        u, t, f = ram
        c = "#e74c3c" if f < 1500 else "#f39c12" if f < 3000 else "#2ecc71"
        warn = " - low!" if f < 1500 else ""
        lines.append(f"<div style='font-family:monospace;font-size:12px;margin-bottom:3px'>"
                     f"<span style='color:#888'>RAM: </span> <b>{u:.0f}/{t:.0f} MB</b> "
                     f"<span style='color:{c}'>({f:.0f} MB free{warn})</span></div>")

    # Recovery
    if _consecutive_stable > 0 and score > 0:
        lines.append(f"<div style='font-family:monospace;font-size:11px;color:#888;margin-bottom:3px'>"
                     f"Recovering: {_consecutive_stable} stable gen(s), score decaying</div>")

    # Gen stats
    if _generation_count > 0:
        avg = sum(_gen_latency_history) / max(len(_gen_latency_history), 1)
        sc  = "#e74c3c" if _swap_detected_count > 0 else "#2ecc71"
        lines.append(f"<div style='font-family:monospace;font-size:12px;color:#888'>"
                     f"Gens: <b style='color:#fff'>{_generation_count}</b> | "
                     f"Avg: <b style='color:#fff'>{avg:.1f}s</b> | "
                     f"CPU swaps: <b style='color:{sc}'>{_swap_detected_count}</b></div>")

    # FP8 status
    if is_fp8_active():
        lines.append("<div style='font-family:monospace;font-size:12px;margin-top:6px;"
                     "background:#0d2b1a;border:1px solid #2ecc71;border-radius:4px;padding:5px'>"
                     "<span style='color:#2ecc71'>FP8 active</span> - model VRAM halved</div>")
    elif should_recommend_fp8():
        lines.append("<div style='font-family:monospace;font-size:12px;margin-top:6px;"
                     "background:#2c1810;border:1px solid #e74c3c;border-radius:4px;padding:6px'>"
                     "<b style='color:#e74c3c'>CPU Swap Detected</b><br>"
                     "Add to webui-user.bat: <code>--unet-in-fp8-e4m3fn</code><br>"
                     "<span style='color:#aaa;font-size:11px'>Or use the FP8 section below.</span></div>")

    # RAM critical
    if ram and ram[2] < 1000:
        lines.append("<div style='font-family:monospace;font-size:12px;margin-top:6px;"
                     "background:#2c1810;border:1px solid #e74c3c;border-radius:4px;padding:6px'>"
                     "<b style='color:#e74c3c'>RAM Critical</b> - less than 1GB free, risk of crash</div>")

    return f"<div>{''.join(lines)}</div>"


# -----------------------------------------------------------------------------
# State JSON for JS (minimal  just FPS limit)
# -----------------------------------------------------------------------------
def get_state_json():
    state  = get_pressure_state()
    config = PRESSURE_CONFIG[state]
    return json.dumps({
        "state":       state,
        "preview_fps": config["preview_fps"],
        "swap_count":  _swap_detected_count,
        "fp8_active":  is_fp8_active(),
        "emergency_interrupted": _emergency_interrupted,
    })


def get_runtime_state():
    return {
        "state": get_pressure_state(),
        "emergency_interrupted": _emergency_interrupted,
        "interrupted": bool(getattr(shared.state, "interrupted", False)),
        "stopping_generation": bool(getattr(shared.state, "stopping_generation", False)),
        "job": getattr(shared.state, "job", ""),
    }


def on_app_started(_demo, app):
    app.add_api_route("/forge-runtime-manager/state", get_runtime_state, methods=["GET"])


# -----------------------------------------------------------------------------
# Script class
# -----------------------------------------------------------------------------
class ForgeRuntimeManagerScript(scripts.Script):
    sorting_priority = 99  # Near the bottom, after other extensions

    def title(self):
        return DISPLAY_NAME

    def show(self, is_img2img):
        return scripts.AlwaysVisible

    def ui(self, is_img2img):
        # Use InputAccordion like forge_freeu  cleaner, no nested accordions
        with InputAccordion(False, label=f"Forge Runtime Manager v{VERSION}",
                            elem_id="forge_runtime_manager_main"):

            #  Status 
            status_html = gr.HTML(
                value="<div style='color:#aaa;font-family:monospace;font-size:12px;padding:4px'>"
                      "Generate an image to see memory stats.</div>",
                label="",
                elem_id="forge_runtime_manager_status"
            )
            # Hidden state placeholder; V1 avoids automatic textbox sync with Gradio.
            state_box = gr.Textbox(
                value="",
                visible=False, elem_id="forge_runtime_manager_state_json", label=""
            )
            refresh_btn = gr.Button(
                "Refresh Status", size="sm",
                elem_id="forge_runtime_manager_refresh"
            )
            refresh_btn.click(fn=build_status_html, outputs=status_html)

            gr.HTML("<hr style='border-color:#333;margin:8px 0'>")

            #  FP8 
            gr.HTML("<div style='font-size:12px;color:#aaa;margin-bottom:6px' "
                    "title='Adds or removes --unet-in-fp8-e4m3fn in webui-user.bat. "
                    "This can reduce model VRAM use, but you must restart Forge to apply it.'>"
                    "<b style='color:#ddd'>FP8 Mode</b> - manual bat edit, restart required</div>")

            fp8_pending = is_fp8_in_bat() and not is_fp8_active()
            fp8_st = ("Running" if is_fp8_active()
                      else "Set in bat, restart to apply" if fp8_pending
                      else "Off")
            fp8_status = gr.HTML(
                value=f"<div style='font-family:monospace;font-size:11px;color:#888;margin-bottom:6px'>"
                      f"Status: {fp8_st} | bat: {'yes' if is_fp8_in_bat() else 'no'}</div>")

            with gr.Row():
                fp8_on  = gr.Button("Add FP8 arg",  size="sm", variant="primary",
                                    elem_id="forge_runtime_manager_fp8_on")
                fp8_off = gr.Button("Remove FP8 arg", size="sm",
                                    elem_id="forge_runtime_manager_fp8_off")
                fp8_restart = gr.Button("Restart UI", size="sm",
                                        elem_id="forge_runtime_manager_restart")
            fp8_msg = gr.HTML(value="")

            def do_fp8_on():
                ok, msg = add_fp8_to_bat()
                lines = [f"Bat: {msg}"]
                lines.append("Restart Forge to apply FP8.")
                fp8_now = is_fp8_active(); bat_now = is_fp8_in_bat()
                st = f"{'Running' if fp8_now else 'Pending restart' if bat_now else 'Off'} | bat: {'yes' if bat_now else 'no'}"
                color = "#2ecc71" if ok else "#e74c3c"
                return (f"<div style='color:{color};font-family:monospace;font-size:11px'>{'<br>'.join(lines)}</div>",
                        f"<div style='font-family:monospace;font-size:11px;color:#888'>Status: {st}</div>")

            def do_fp8_off():
                ok, msg = remove_fp8_from_bat()
                lines = [f"Bat: {msg}"]
                lines.append("Restart Forge to apply the change.")
                fp8_now = is_fp8_active(); bat_now = is_fp8_in_bat()
                st = f"{'Running' if fp8_now else 'Off'} | bat: {'yes' if bat_now else 'no'}"
                color = "#f39c12" if ok else "#e74c3c"
                return (f"<div style='color:{color};font-family:monospace;font-size:11px'>{'<br>'.join(lines)}</div>",
                        f"<div style='font-family:monospace;font-size:11px;color:#888'>Status: {st}</div>")

            def do_restart():
                try:
                    shared.state.request_restart()
                    return "<div style='color:#2ecc71;font-family:monospace;font-size:11px'>Restart queued</div>"
                except Exception as e:
                    return f"<div style='color:#e74c3c;font-family:monospace;font-size:11px'>Failed: {e}</div>"

            fp8_on.click(fn=do_fp8_on,  outputs=[fp8_msg, fp8_status])
            fp8_off.click(fn=do_fp8_off, outputs=[fp8_msg, fp8_status])
            fp8_restart.click(fn=do_restart, outputs=fp8_msg, _js="restart_reload")

            gr.HTML("<hr style='border-color:#333;margin:8px 0'>")

            #  Launch Arguments Advisor 
            gr.HTML("<div style='font-size:12px;color:#aaa;margin-bottom:6px' "
                    "title='Shows which launch arguments in webui-user.bat are useful, useless in Forge, or recommended for your setup. Click any argument to expand details.'>"
                    "<b style='color:#ddd'>Launch Arguments</b>  bat file advisor</div>")
            args_html = gr.HTML(value=build_launch_args_html())

            # Checkboxes for recommended args (built dynamically)
            # We use a simple textbox to pass selected args as JSON to Python
            args_select = gr.CheckboxGroup(
                choices=recommended_launch_args(),
                value=[],
                label="",
                elem_id="forge_runtime_manager_args_select",
            )
            with gr.Row():
                args_refresh = gr.Button("Refresh", size="sm")
                args_apply   = gr.Button("Add selected to bat file", size="sm",
                                         variant="primary")
            args_msg = gr.HTML(value="")

            def do_args_refresh():
                return build_launch_args_html(), gr.update(choices=recommended_launch_args(), value=[])

            def do_args_apply(selected):
                if not selected:
                    return "<div style='color:#888;font-family:monospace;font-size:11px'>No arguments selected.</div>"
                results = []
                all_ok = True
                for arg in selected:
                    ok, msg = add_arg_to_bat(arg)
                    all_ok = all_ok and ok
                    results.append(f"{'OK' if ok else 'WARN'}: {msg}")
                results.append("Restart Forge to apply.")
                color = "#2ecc71" if all_ok else "#e74c3c"
                return f"<div style='color:{color};font-family:monospace;font-size:11px'>{'<br>'.join(results)}</div>"

            args_refresh.click(fn=do_args_refresh, outputs=[args_html, args_select])
            args_apply.click(fn=do_args_apply, inputs=[args_select], outputs=args_msg)

            gr.HTML("<div style='font-size:12px;color:#aaa;margin:8px 0 6px' "
                    "title='Detects known extension startup skip flags. These can reduce repeated optional dependency checks when the extension already works on your setup.'>"
                    "<b style='color:#ddd'>Startup Install Checks</b> - optional skip flags</div>")
            startup_skip_html = gr.HTML(value=build_startup_skip_html())
            startup_skip_select = gr.CheckboxGroup(
                choices=recommended_startup_skip_flags(),
                value=[],
                label="",
                elem_id="forge_runtime_manager_startup_skip_select",
            )
            with gr.Row():
                startup_skip_refresh = gr.Button("Refresh startup checks", size="sm")
                startup_skip_apply = gr.Button("Add selected startup skip flags", size="sm")
            startup_skip_msg = gr.HTML(value="")

            def do_startup_skip_refresh():
                return (
                    build_startup_skip_html(),
                    gr.update(choices=recommended_startup_skip_flags(), value=[]),
                )

            def do_startup_skip_apply(selected):
                if not selected:
                    return "<div style='color:#888;font-family:monospace;font-size:11px'>No startup skip flags selected.</div>"
                results = []
                all_ok = True
                for flag_name in selected:
                    ok, msg = add_startup_skip_flag(flag_name)
                    all_ok = all_ok and ok
                    results.append(f"{'OK' if ok else 'WARN'}: {msg}")
                results.append("Restart Forge to apply.")
                color = "#2ecc71" if all_ok else "#e74c3c"
                return f"<div style='color:{color};font-family:monospace;font-size:11px'>{'<br>'.join(results)}</div>"

            startup_skip_refresh.click(
                fn=do_startup_skip_refresh,
                outputs=[startup_skip_html, startup_skip_select],
            )
            startup_skip_apply.click(
                fn=do_startup_skip_apply,
                inputs=[startup_skip_select],
                outputs=startup_skip_msg,
            )

            gr.HTML("<hr style='border-color:#333;margin:8px 0'>")

            #  Maintenance 
            gr.HTML("<div style='font-size:12px;color:#aaa;margin-bottom:6px' "
                    "title='Read-only checks for local Forge health: git state, update script safety, startup install flags, and extension installers.'>"
                    "<b style='color:#ddd'>Maintenance</b> - local health checks</div>")
            maintenance_html = gr.HTML(value=build_maintenance_html())
            maintenance_refresh = gr.Button("Refresh Maintenance Check", size="sm")
            maintenance_refresh.click(fn=build_maintenance_html, outputs=maintenance_html)

            gr.HTML("<hr style='border-color:#333;margin:8px 0'>")

            #  Emergency Stop Settings (inline, no reload needed) 
            gr.HTML("<div style='font-size:12px;color:#aaa;margin-bottom:6px' "
                    "title='Controls when Forge Runtime Manager stops generation to protect your system. Changes apply immediately; no restart needed.'>"
                    "<b style='color:#ddd'>Emergency Stop</b> - RAM thresholds (apply immediately)</div>")

            with gr.Row():
                emg_instant = gr.Slider(
                    label="Instant stop (MB free)",
                    minimum=50, maximum=300, step=25,
                    value=getattr(opts, "forge_opt_emergency_instant_mb", 100),
                    info="Interrupt immediately  no delay  when free RAM drops below this"
                )
                emg_stage1 = gr.Slider(
                    label="Warning threshold (MB free)",
                    minimum=500, maximum=4000, step=100,
                    value=getattr(opts, "forge_opt_emergency_stage1_mb", 1000),
                    info="Show warning banner when free RAM drops below this"
                )
            with gr.Row():
                emg_stage2 = gr.Slider(
                    label="Interrupt threshold (MB free)",
                    minimum=200, maximum=1000, step=50,
                    value=getattr(opts, "forge_opt_emergency_stage2_mb", 350),
                    info="Interrupt generation when RAM stays below this for N seconds"
                )
                emg_duration = gr.Slider(
                    label="Sustained seconds before interrupt",
                    minimum=5, maximum=60, step=5,
                    value=getattr(opts, "forge_opt_emergency_duration_s", 10),
                    info="How long RAM must stay below interrupt threshold before stopping"
                )
            emg_msg = gr.HTML(value="")

            def do_emg_apply(instant, stage1, stage2, duration):
                try:
                    opts.forge_opt_emergency_instant_mb = int(instant)
                    opts.forge_opt_emergency_stage1_mb  = int(stage1)
                    opts.forge_opt_emergency_stage2_mb  = int(stage2)
                    opts.forge_opt_emergency_duration_s = int(duration)
                    opts.save(shared.config_filename)
                    log_event("emergency_settings_changed", {
                        "instant": instant, "stage1": stage1,
                        "stage2": stage2, "duration": duration
                    })
                    return ("<div style='color:#2ecc71;font-family:monospace;font-size:11px'>"
                            "Thresholds saved; effective immediately</div>")
                except Exception as e:
                    return f"<div style='color:#e74c3c;font-family:monospace;font-size:11px'>Failed: {e}</div>"

            emg_save = gr.Button("Save thresholds", size="sm", variant="primary")
            emg_save.click(
                fn=do_emg_apply,
                inputs=[emg_instant, emg_stage1, emg_stage2, emg_duration],
                outputs=emg_msg
            )

            gr.HTML("<hr style='border-color:#333;margin:8px 0'>")

            #  Telemetry 
            export_btn = gr.Button("Export Telemetry Log", size="sm")
            export_msg = gr.HTML(value="")
            def do_export():
                result = export_telemetry()
                color = "#e74c3c" if str(result).startswith("Export failed") else "#2ecc71"
                return f"<div style='color:{color};font-family:monospace;font-size:11px'>Exported: {html_escape(result)}</div>"

            export_btn.click(
                fn=do_export,
                outputs=export_msg
            )

        return [status_html, state_box, fp8_status, fp8_msg,
                args_html, args_select, args_msg,
                maintenance_html, emg_msg, export_msg]

    def before_process(self, p, *args):
        global _last_generation_time
        _last_generation_time = time.time()
        check_emergency_stop()
        if getattr(opts, "forge_opt_log_memory", False):
            log_memory_state("Before:")

    def postprocess_image(self, p, pp, *args):
        check_emergency_stop()
        if getattr(opts, "forge_opt_vram_cleanup", True):
            interval  = getattr(opts, "forge_opt_cleanup_interval", 1)
            img_index = getattr(p, "_forge_opt_count", 0) + 1
            p._forge_opt_count = img_index
            if img_index % max(interval, 1) == 0:
                state = update_pressure()
                if state != PRESSURE_NORMAL:
                    scaled_cleanup(state)

    def postprocess(self, p, processed, *args):
        global _last_generation_time, _generation_count

        # Skip ADetailer sub-generations (no prompt attribute set properly)
        if not getattr(p, 'prompt', None):
            return

        elapsed = time.time() - _last_generation_time
        _last_generation_time = time.time()
        _generation_count += 1
        _gen_latency_history.append(elapsed)

        detector  = get_swap_detector()
        gen_swaps = detector.reset_gen_count() if detector else 0
        decay_swap_score(stable=(gen_swaps == 0))

        state = update_pressure()
        if getattr(opts, "forge_opt_vram_cleanup", True):
            scaled_cleanup(state)

        log_event("generation_end", {
            "duration_s": round(elapsed, 1),
            "swaps": gen_swaps,
            "state": state,
        })

        if getattr(opts, "forge_opt_log_memory", False):
            log_memory_state("After: ")


# -----------------------------------------------------------------------------
# Model + idle hooks
# -----------------------------------------------------------------------------
def on_model_loaded(sd_model):
    print("[ForgeRuntimeManager] Model loaded")
    state = update_pressure()
    scaled_cleanup(state)
    log_event("model_loaded", {"fp8": is_fp8_active()})


def _idle_monitor():
    global _idle_thread_running
    _idle_thread_running = True
    while True:
        time.sleep(30)

        if get_swap_score() > 0:
            decay_swap_score(stable=True)
            update_pressure()

        check_emergency_stop()

        idle_min = getattr(opts, "forge_opt_idle_unload_minutes", 0)
        if idle_min > 0:
            elapsed = (time.time() - _last_generation_time) / 60
            if elapsed >= idle_min:
                try:
                    if FORGE_MEMORY_AVAILABLE and shared.sd_model is not None:
                        print(f"[ForgeRuntimeManager] Idle {elapsed:.1f} min  unloading")
                        memory_management.unload_all_models()
                        scaled_cleanup(PRESSURE_HIGH)
                        log_event("idle_unload", {"minutes": round(elapsed, 1)})
                except Exception as e:
                    print(f"[ForgeRuntimeManager] idle error: {e}")


def on_before_ui():
    print(f"[ForgeRuntimeManager v{VERSION}] Initialized")
    install_swap_detector()
    if not _idle_thread_running:
        t = threading.Thread(target=_idle_monitor, daemon=True)
        t.start()
    log_memory_state("Startup:")
    log_event("startup", {"fp8_in_bat": is_fp8_in_bat()})


# -----------------------------------------------------------------------------
# Settings
# -----------------------------------------------------------------------------
def on_ui_settings():
    section = ("forge_runtime_manager", DISPLAY_NAME)

    opts.add_option("forge_opt_vram_cleanup", shared.OptionInfo(
        True, "Enable pressure-aware VRAM cleanup (only when system is under pressure)",
        section=section))

    opts.add_option("forge_opt_cleanup_interval", shared.OptionInfo(
        1, "Check pressure every N images in batch",
        gr.Slider, {"minimum": 1, "maximum": 10, "step": 1}, section=section))

    opts.add_option("forge_opt_idle_unload_minutes", shared.OptionInfo(
        0, "Auto-unload model from VRAM after N minutes idle (0 = disabled)",
        gr.Slider, {"minimum": 0, "maximum": 60, "step": 1}, section=section))

    opts.add_option("forge_opt_log_memory", shared.OptionInfo(
        False, "Log VRAM/RAM to console before/after each generation", section=section))

    opts.add_option("forge_opt_emergency_instant_mb", shared.OptionInfo(
        100, "Emergency: instant stop when free RAM drops below this (MB)  no delay",
        gr.Slider, {"minimum": 50, "maximum": 300, "step": 25}, section=section))

    opts.add_option("forge_opt_emergency_stage1_mb", shared.OptionInfo(
        1000, "Emergency Stage 1: show warning when free RAM below this (MB)",
        gr.Slider, {"minimum": 500, "maximum": 4000, "step": 100}, section=section))

    opts.add_option("forge_opt_emergency_stage2_mb", shared.OptionInfo(
        350, "Emergency Stage 2: interrupt generation when RAM stays below this (MB)",
        gr.Slider, {"minimum": 200, "maximum": 1000, "step": 50}, section=section))

    opts.add_option("forge_opt_emergency_duration_s", shared.OptionInfo(
        10, "Emergency Stage 2: how many seconds before interrupt fires",
        gr.Slider, {"minimum": 5, "maximum": 60, "step": 5}, section=section))


# -----------------------------------------------------------------------------
# Register callbacks
# -----------------------------------------------------------------------------
script_callbacks.on_model_loaded(on_model_loaded)
script_callbacks.on_ui_settings(on_ui_settings)
script_callbacks.on_before_ui(on_before_ui)
script_callbacks.on_app_started(on_app_started)
