import os
import subprocess


_ORIGINAL_POPEN = subprocess.Popen
_POLICY_INSTALLED = False


def install_hidden_subprocess_policy():
    """Keep FFmpeg/FFprobe child processes invisible inside the render worker.

    This changes only Windows process presentation. Commands, arguments, timing,
    stdout/stderr handling and the video/audio pipeline remain untouched.
    """
    global _POLICY_INSTALLED
    if _POLICY_INSTALLED or os.name != "nt":
        return

    def hidden_popen(*args, **kwargs):
        creationflags = int(kwargs.get("creationflags", 0))
        kwargs["creationflags"] = creationflags | subprocess.CREATE_NO_WINDOW
        kwargs.setdefault("stdin", subprocess.DEVNULL)
        return _ORIGINAL_POPEN(*args, **kwargs)

    subprocess.Popen = hidden_popen
    _POLICY_INSTALLED = True
