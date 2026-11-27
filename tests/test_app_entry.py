"""Check package and installed-command entry points without opening the GUI."""
import runpy
import sys

import pytest

from chemotion_sample_manager import __main__ as entry_point, gui


@pytest.mark.parametrize("entry", ["command", "module"])
def test_entry_starts_gui_event_loop(monkeypatch, entry):
    started = []

    class Window:
        def mainloop(self):
            started.append(True)

    monkeypatch.setattr(gui, "SampleManager", Window)
    if entry == "command":
        entry_point.main()
    else:
        monkeypatch.delitem(sys.modules, "chemotion_sample_manager.__main__")
        runpy.run_module("chemotion_sample_manager", run_name="__main__")

    assert started == [True]


def test_source_linked_windows_entry_configures_taskbar_before_mainloop(monkeypatch):
    from chemotion_sample_manager import windows_taskbar

    calls = []
    executable = r"C:\Shared Apps\Chemotion Sample Manager.exe"

    class Window:
        def mainloop(self):
            calls.append("mainloop")

    monkeypatch.setattr(entry_point.sys, "platform", "win32")
    monkeypatch.setenv("CHEMOTION_SAMPLE_MANAGER_LAUNCHER", executable)
    monkeypatch.setattr(gui, "SampleManager", Window)
    monkeypatch.setattr(
        windows_taskbar, "configure_taskbar",
        lambda window, launcher: calls.append((type(window), launcher)),
    )

    entry_point.main()

    assert calls == [(Window, executable), "mainloop"]
