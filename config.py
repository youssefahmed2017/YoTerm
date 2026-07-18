"""YoTerm settings.

Settings live in a real Python file (``~/.yoterm_config.py``) rather than JSON
or INI, the same way .vimrc and .bashrc do it: the file you hand-edit *is* the
file the GUI writes, so neither way of editing is second-class.

The file defines a ``YTConfig`` dataclass whose field **defaults** are your
settings. Loading layers whatever fields that class provides over the schema
below, so a config written by an older YoTerm keeps working when new settings
appear, and a hand-written file only has to mention what it wants to change.

Field metadata drives the GUI (label, help text, choices, ranges), so adding a
setting here makes it appear in the settings dialog with no extra UI code.
"""

import importlib.machinery
import importlib.util
import os
import sys
from dataclasses import dataclass, field, fields

# Preferred first. The GUI always writes the '.py' one -- the extension is what
# makes an editor syntax-highlight it -- but a file without it still loads.
CONFIG_PATHS = [
    os.path.expanduser("~/.yoterm_config.py"),
    os.path.expanduser("~/.yoterm_config"),
]

CURSOR_STYLES = ["Vertical", "Block", "Underline"]
SHELLS = ["cmd.exe", "powershell.exe", "pwsh.exe"]

# Friendly names -> what term.py calls them.
_SHAPES = {"Vertical": "bar", "Block": "block", "Underline": "underline"}


@dataclass
class YTConfig:
    shell: str = field(
        default="cmd.exe",
        metadata={
            "label": "Shell",
            "choices": SHELLS,
            "help": "Used for new tabs — a shell that's already running can't be swapped.",
        },
    )

    cursor: bool = field(
        default=True,
        metadata={"label": "Show cursor"},
    )

    smooth_blink: bool = field(
        default=False,
        metadata={
            "label": "Smooth cursor blink",
            "help": "Fade the caret in and out instead of blinking on/off.",
        },
    )

    cursor_style: str = field(
        default="Vertical",
        metadata={"label": "Cursor style", "choices": CURSOR_STYLES},
    )

    text_blink: bool = field(
        default=False,
        metadata={
            "label": "Enable blink",
            "help": "Animate text marked blinking (SGR 5). Off, it just renders "
                    "steadily — which is what most terminals do.",
        },
    )

    font_size: int = field(
        default=24,
        metadata={
            "label": "Font size",
            "min": 8,
            "max": 72,
            "help": "Ctrl+= and Ctrl+- zoom for this session; Ctrl+0 comes back here.",
        },
    )

    def shape(self):
        """cursor_style as term.py's name for it."""
        return _SHAPES.get(self.cursor_style, "bar")


def config_path():
    """The config file we'd read, or where we'd create one."""
    for path in CONFIG_PATHS:
        if os.path.exists(path):
            return path
    return CONFIG_PATHS[0]


def _validate(config):
    """Clamp/repair anything the file got wrong, and say what was wrong.

    A hand-edited file is allowed to be wrong; it must never take the terminal
    down with it.
    """
    problems = []
    for spec in fields(config):
        value, default = getattr(config, spec.name), spec.default
        choices = spec.metadata.get("choices")

        if isinstance(default, bool):
            if not isinstance(value, bool):
                problems.append("%s must be True or False" % spec.name)
                setattr(config, spec.name, default)
        elif choices:
            if value not in choices:
                problems.append("%s must be one of: %s"
                                % (spec.name, ", ".join(choices)))
                setattr(config, spec.name, default)
        elif isinstance(default, int):
            # bool is an int subclass, so check it didn't sneak through.
            if isinstance(value, bool) or not isinstance(value, int):
                problems.append("%s must be a whole number" % spec.name)
                setattr(config, spec.name, default)
            else:
                low, high = spec.metadata.get("min"), spec.metadata.get("max")
                if low is not None and value < low:
                    setattr(config, spec.name, low)
                elif high is not None and value > high:
                    setattr(config, spec.name, high)
        elif not isinstance(value, str):
            problems.append("%s must be text" % spec.name)
            setattr(config, spec.name, default)
    return "; ".join(problems) or None


MODULE_NAME = ".yoterm_config"


def _import_config(path):
    """Import the config file as a real module.

    Running it as a proper module rather than exec-ing source into a bare dict
    means it gets a real __name__ and __file__, tracebacks point at the user's
    file, and anything that resolves a class through sys.modules (dataclasses
    included) behaves the way it would in a normal import.

    The loader is spelled out because the preferred config path has no '.py'
    suffix to infer one from.
    """
    loader = importlib.machinery.SourceFileLoader(MODULE_NAME, path)
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path,
                                                  loader=loader)
    if spec is None or spec.loader is None:
        raise ImportError("not loadable as Python")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)   # don't leave a half-built module
        raise
    return module


def load(path=None):
    """Return (config, problem). `problem` is None when all was well.

    A broken config must never stop the terminal from starting, so any failure
    falls back to defaults and reports why rather than raising.
    """
    path = path or config_path()
    config = YTConfig()
    if not os.path.exists(path):
        return config, None

    try:
        module = _import_config(path)
    except Exception as exc:
        return config, "%s: %s" % (os.path.basename(path), exc)

    user_class = getattr(module, "YTConfig", None)
    if user_class is None:
        return config, "%s defines no YTConfig class" % os.path.basename(path)
    try:
        user = user_class()
    except Exception as exc:
        return config, "%s: YTConfig() failed: %s" % (os.path.basename(path), exc)

    # Layer the user's values over the schema: a field they don't mention keeps
    # our default, so old configs survive new settings.
    for spec in fields(config):
        if hasattr(user, spec.name):
            setattr(config, spec.name, getattr(user, spec.name))
    return config, _validate(config)


_HEADER = '''"""YoTerm settings.

Edit this by hand, or use Settings in YoTerm (Ctrl+,) -- the GUI rewrites this
same file. Your settings are the *defaults* of the dataclass below; anything
you delete falls back to YoTerm's built-in default.
"""

from dataclasses import dataclass, field


@dataclass
class YTConfig:
'''


def _annotation(value):
    return {bool: "bool", int: "int", str: "str"}.get(type(value), "object")


def save(config, path=None):
    """Write `config` back out as the same dataclass a human would write."""
    path = path or CONFIG_PATHS[0]
    chunks = [_HEADER]
    for spec in fields(config):
        value = getattr(config, spec.name)
        chunks.append("    %s: %s = field(\n" % (spec.name, _annotation(value)))
        chunks.append("        default=%r,\n" % (value,))
        # dict() so it reprs as a literal rather than a mappingproxy.
        chunks.append("        metadata=%r,\n" % (dict(spec.metadata),))
        chunks.append("    )\n\n")
    text = "".join(chunks).rstrip() + "\n"

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path
