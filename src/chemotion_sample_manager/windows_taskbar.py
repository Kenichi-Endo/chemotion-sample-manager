"""Associate the source-linked app window with its Windows launcher."""

import ctypes
from ctypes import wintypes
from pathlib import Path
from uuid import UUID


class _PropertyKey(ctypes.Structure):
    _fields_ = [
        ("format_id", ctypes.c_ubyte * 16),
        ("property_id", wintypes.DWORD),
    ]


class _PropertyValue(ctypes.Union):
    _fields_ = [
        ("string", wintypes.LPWSTR),
        ("storage", ctypes.c_void_p * 2),
    ]


class _PropVariant(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.WORD),
        ("reserved", wintypes.WORD * 3),
        ("value", _PropertyValue),
    ]


def _guid(value):
    return (ctypes.c_ubyte * 16).from_buffer_copy(UUID(value).bytes_le)


def _set_taskbar_properties(window, executable):
    """Tell Explorer how to pin and relaunch the Python-owned Tk window."""
    get_ancestor = ctypes.windll.user32.GetAncestor
    get_ancestor.argtypes = (wintypes.HWND, wintypes.UINT)
    get_ancestor.restype = wintypes.HWND
    hwnd = get_ancestor(window.winfo_id(), 2)  # GA_ROOT gets Tk's outer HWND.

    if not hwnd:
        raise ctypes.WinError()

    interface_id = _guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")  # IPropertyStore
    shell = ctypes.OleDLL("shell32")
    get_store = shell.SHGetPropertyStoreForWindow
    store = ctypes.c_void_p()
    get_store.argtypes = (wintypes.HWND, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    get_store(hwnd, ctypes.byref(interface_id), ctypes.byref(store))

    vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    release = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[2])
    set_value = ctypes.WINFUNCTYPE(
        ctypes.HRESULT, ctypes.c_void_p,
        ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant),
    )(vtable[6])
    app_model = _guid("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3")
    icon = Path(executable).parents[2] / "scripts" / "Chemotion Sample Manager.ico"
    icon_resource = f"{icon if icon.is_file() else executable},0"
    try:
        # Explorer reads the relaunch properties when the identity is set.
        for property_id, text in (
            (2, f'"{executable}"'),
            (3, icon_resource),
            (4, Path(executable).stem),
            (5, "EndoGroup.ChemotionSampleManager"),
        ):
            key = _PropertyKey(app_model, property_id)
            value = _PropVariant()
            value.type = 31  # VT_LPWSTR
            value.value.string = text
            result = set_value(store, ctypes.byref(key), ctypes.byref(value))
            if result < 0:
                raise ctypes.WinError(result)
    finally:
        release(store)


def configure_taskbar(window, executable):
    window.update_idletasks()
    ole = ctypes.OleDLL("ole32")
    ole.OleInitialize(None)
    try:
        _set_taskbar_properties(window, executable)
    finally:
        ole.OleUninitialize()
