# SPDX-License-Identifier: GPL-3.0-only
"""OS ownership identities, kept separate so trust decisions can be tested."""
from __future__ import annotations

import os
from pathlib import Path


def _windows_ids(sid) -> frozenset[str]:
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    text = wintypes.LPWSTR()
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise OSError("owner lookup failed")
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    try:
        identities = {text.value.casefold()}
    finally:
        kernel.LocalFree(text)
    name, domain = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
    n, d, kind = wintypes.DWORD(256), wintypes.DWORD(256), wintypes.DWORD()
    advapi.LookupAccountSidW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR,
                                       ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
                                       ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    if advapi.LookupAccountSidW(None, sid, name, ctypes.byref(n), domain, ctypes.byref(d), ctypes.byref(kind)):
        identities.add((domain.value + "\\" + name.value).casefold())
    return frozenset(identities)


def owner_ids(path: Path) -> frozenset[str]:
    if os.name != "nt":
        return frozenset({str(path.stat().st_uid)})
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    sid, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p)]
    if advapi.GetNamedSecurityInfoW(str(path), 1, 1, ctypes.byref(sid), None, None, None,
                                  ctypes.byref(descriptor)):
        raise OSError("owner lookup failed")
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    try:
        return _windows_ids(sid)
    finally:
        kernel.LocalFree(descriptor)


def current_owner_ids() -> frozenset[str]:
    if os.name != "nt":
        return frozenset({str(os.geteuid())})
    return _windows_current_owner_ids()


ADMINISTRATORS_SID = "s-1-5-32-544"


def _windows_is_admin() -> bool:
    """Check enabled membership, not merely membership of the account's groups."""
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    sid = ctypes.c_void_p()
    advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi.CheckTokenMembership.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    if not advapi.ConvertStringSidToSidW(ADMINISTRATORS_SID, ctypes.byref(sid)):
        raise OSError("owner lookup failed")
    try:
        member = wintypes.BOOL()
        if not advapi.CheckTokenMembership(None, sid, ctypes.byref(member)):
            raise OSError("owner lookup failed")
        return bool(member.value)
    finally:
        kernel.LocalFree(sid)


def _windows_current_owner_ids() -> frozenset[str]:
    identities = _windows_user_ids()
    if _windows_is_admin():
        identities |= {ADMINISTRATORS_SID}
    return frozenset(identities)


def _windows_user_ids() -> frozenset[str]:
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise OSError("owner lookup failed")
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise OSError("owner lookup failed")
        return _windows_ids(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0])
    finally:
        kernel.CloseHandle(token)
