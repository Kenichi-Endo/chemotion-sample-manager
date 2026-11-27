"""Run Chemotion Sample Manager with ``python -m chemotion_sample_manager``."""


import logging
import os
import sys


def main():
    from chemotion_sample_manager.gui import SampleManager

    window = SampleManager()
    if sys.platform == "win32":
        executable = os.environ.get("CHEMOTION_SAMPLE_MANAGER_LAUNCHER")
        if executable:
            from chemotion_sample_manager.windows_taskbar import configure_taskbar

            try:
                configure_taskbar(window, executable)
            except OSError as error:
                logging.warning("Could not configure the Windows taskbar icon: %s", error)
    window.mainloop()


if __name__ == "__main__":
    main()
