"""Restore a verified ordinary QQ window without launching another QQ process."""
import os
from pathlib import Path


def focus_existing_qq(executable):
    if os.name!='nt':return False
    import ctypes
    from ctypes import wintypes as w
    user=ctypes.WinDLL('user32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    callback=ctypes.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
    kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
    kernel.CloseHandle.argtypes=[w.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes=[w.HANDLE,w.DWORD,w.LPWSTR,ctypes.POINTER(w.DWORD)]
    kernel.GetProcessTimes.argtypes=[w.HANDLE,*([ctypes.POINTER(w.FILETIME)]*4)]
    user.GetWindowThreadProcessId.argtypes=[w.HWND,ctypes.POINTER(w.DWORD)]
    user.GetWindowTextW.argtypes=[w.HWND,w.LPWSTR,ctypes.c_int]
    user.GetClassNameW.argtypes=[w.HWND,w.LPWSTR,ctypes.c_int]
    user.ShowWindow.argtypes=[w.HWND,ctypes.c_int];user.SetForegroundWindow.argtypes=[w.HWND]
    user.BringWindowToTop.argtypes=[w.HWND];user.IsWindowVisible.argtypes=[w.HWND]
    user.EnumWindows.argtypes=[callback,w.LPARAM]
    expected=os.path.normcase(str(Path(executable).resolve()));candidates=[];processes={}

    def identify(pid):
        if pid in processes:return processes[pid]
        handle=kernel.OpenProcess(0x1000,False,pid)
        result=None
        if handle:
            try:
                buffer=ctypes.create_unicode_buffer(32768);size=w.DWORD(len(buffer))
                if kernel.QueryFullProcessImageNameW(handle,0,buffer,ctypes.byref(size)) and os.path.normcase(buffer.value)==expected:
                    created=w.FILETIME();exited=w.FILETIME();system=w.FILETIME();used=w.FILETIME()
                    if kernel.GetProcessTimes(handle,ctypes.byref(created),ctypes.byref(exited),ctypes.byref(system),ctypes.byref(used)):
                        result=(created.dwHighDateTime<<32)|created.dwLowDateTime
            finally:kernel.CloseHandle(handle)
        processes[pid]=result;return result

    @callback
    def visit(hwnd,_):
        title=ctypes.create_unicode_buffer(256);kind=ctypes.create_unicode_buffer(128)
        user.GetWindowTextW(hwnd,title,len(title));user.GetClassNameW(hwnd,kind,len(kind))
        if title.value!='QQ':return True
        pid=w.DWORD();user.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        created=identify(pid.value)
        if created is not None:candidates.append((created,int(hwnd)))
        return True

    user.EnumWindows(visit,0)
    if not candidates:return False
    # QQ supports multiple clients. Restore the earliest existing client rather
    # than create another login session merely to show a window.
    _,hwnd=min(candidates)
    user.ShowWindow(hwnd,9);user.BringWindowToTop(hwnd);user.SetForegroundWindow(hwnd)
    return bool(user.IsWindowVisible(hwnd))
