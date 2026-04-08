import sys

IS_WINDOWS = sys.platform == "win32"


def default_shell() -> str:
    """Return the platform-appropriate default shell path."""
    if IS_WINDOWS:
        return "powershell.exe"
    return "/bin/bash"
