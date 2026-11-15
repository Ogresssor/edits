"""Inspect/continue only the crash-recovery dialog of an AE script job."""
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
import subprocess
import time
from pathlib import Path

u=C.windll.user32
CALLBACK=C.WINFUNCTYPE(W.BOOL,W.HWND,W.LPARAM)
u.GetWindowTextW.argtypes=[W.HWND,W.LPWSTR,C.c_int]
u.GetWindowTextLengthW.argtypes=[W.HWND]
u.GetClassNameW.argtypes=[W.HWND,W.LPWSTR,C.c_int]
u.GetWindowThreadProcessId.argtypes=[W.HWND,C.POINTER(W.DWORD)]
u.IsWindowVisible.argtypes=[W.HWND]
u.PostMessageW.argtypes=[W.HWND,W.UINT,W.WPARAM,W.LPARAM]
u.EnumWindows.argtypes=[CALLBACK,W.LPARAM]
u.EnumChildWindows.argtypes=[W.HWND,CALLBACK,W.LPARAM]


def text(hwnd):
    buf=C.create_unicode_buffer(u.GetWindowTextLengthW(hwnd)+1)
    u.GetWindowTextW(hwnd,buf,len(buf))
    return buf.value


def main(click,pid,capture,point):
    ps="Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'AfterFX.exe' -and $_.CommandLine -match ' -r' } | Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress"
    p=subprocess.run(['powershell','-NoProfile','-Command',ps],capture_output=True,text=True,check=True)
    found=json.loads(p.stdout) if p.stdout.strip() else []
    owners={found} if isinstance(found,int) else set(found)
    if pid:
        assert pid in owners,'PID is not an AE script process'
        owners={pid}
    if click or point: assert pid,'An explicitly verified script PID is required for clicking'
    dialogs=[]
    def visit(hwnd,_):
        pid=W.DWORD(); u.GetWindowThreadProcessId(hwnd,C.byref(pid))
        cls=C.create_unicode_buffer(128); u.GetClassNameW(hwnd,cls,len(cls))
        if pid.value in owners and u.IsWindowVisible(hwnd) and cls.value=='#32770':
            children=[]
            def child(h,_):
                t=text(h)
                if t: children.append((h,t))
                return True
            u.EnumChildWindows(hwnd,CALLBACK(child),0)
            dialogs.append((hwnd,children))
        return True
    u.EnumWindows(CALLBACK(visit),0)
    clicked=0
    for hwnd,children in dialogs:
        labels=[t for _,t in children]
        print('AE SCRIPT DIALOG',labels,flush=True)
        if point:
            u.SetForegroundWindow.argtypes=[W.HWND]
            u.GetForegroundWindow.restype=W.HWND
            u.GetWindowRect.argtypes=[W.HWND,C.POINTER(W.RECT)]
            current=C.windll.kernel32.GetCurrentThreadId()
            fg=u.GetForegroundWindow()
            thread=u.GetWindowThreadProcessId(fg,None)
            u.AttachThreadInput.argtypes=[W.DWORD,W.DWORD,W.BOOL]
            attached=bool(u.AttachThreadInput(current,thread,True)) if thread!=current else False
            try:
                u.BringWindowToTop.argtypes=[W.HWND]
                u.BringWindowToTop(hwnd);u.SetForegroundWindow(hwnd);time.sleep(.2)
            finally:
                if attached:u.AttachThreadInput(current,thread,False)
            rect=W.RECT(); assert u.GetWindowRect(hwnd,C.byref(rect))
            # Coordinates are supplied only after visually checking this exact recovery dialog.
            assert (rect.right-rect.left,rect.bottom-rect.top)==(739,365)
            assert tuple(point)==(652,313),'Only the inspected Continue button is supported'
            if u.GetForegroundWindow()==hwnd:
                u.SetCursorPos(rect.left+point[0],rect.top+point[1])
                u.mouse_event(0x0002,0,0,0,0); u.mouse_event(0x0004,0,0,0,0)
            else:
                pt=W.POINT(rect.left+point[0],rect.top+point[1])
                u.ScreenToClient.argtypes=[W.HWND,C.POINTER(W.POINT)]
                assert u.ScreenToClient(hwnd,C.byref(pt))
                packed=(pt.y<<16)|(pt.x&0xffff)
                assert u.PostMessageW(hwnd,0x0201,1,packed)
                assert u.PostMessageW(hwnd,0x0202,0,packed)
            print('INSPECTED CONTINUE BUTTON CLICKED',flush=True)
        if capture:
            from PIL import ImageGrab
            u.SetForegroundWindow.argtypes=[W.HWND]
            u.GetForegroundWindow.restype=W.HWND
            u.GetWindowRect.argtypes=[W.HWND,C.POINTER(W.RECT)]
            u.SetForegroundWindow(hwnd)
            time.sleep(.4)
            rect=W.RECT(); assert u.GetWindowRect(hwnd,C.byref(rect))
            dest=Path(__file__).resolve().parents[1]/'review/s03_stories_batch2/ae_recovery_dialog.png'
            if u.GetForegroundWindow()==hwnd:
                ImageGrab.grab(bbox=(rect.left,rect.top,rect.right,rect.bottom)).save(dest)
            else:
                # Capture only this verified AE dialog, without other desktop windows.
                from PIL import Image
                g=C.windll.gdi32
                u.GetWindowDC.argtypes=[W.HWND];u.GetWindowDC.restype=W.HDC
                g.CreateCompatibleDC.argtypes=[W.HDC];g.CreateCompatibleDC.restype=W.HDC
                g.CreateCompatibleBitmap.argtypes=[W.HDC,C.c_int,C.c_int];g.CreateCompatibleBitmap.restype=W.HBITMAP
                g.SelectObject.argtypes=[W.HDC,W.HANDLE];g.SelectObject.restype=W.HANDLE
                u.PrintWindow.argtypes=[W.HWND,W.HDC,W.UINT]
                g.GetBitmapBits.argtypes=[W.HBITMAP,W.LONG,C.c_void_p]
                g.DeleteObject.argtypes=[W.HANDLE];g.DeleteDC.argtypes=[W.HDC]
                u.ReleaseDC.argtypes=[W.HWND,W.HDC]
                width,height=rect.right-rect.left,rect.bottom-rect.top
                dc=u.GetWindowDC(hwnd);mem=g.CreateCompatibleDC(dc)
                bmp=g.CreateCompatibleBitmap(dc,width,height);old=g.SelectObject(mem,bmp)
                try:
                    assert u.PrintWindow(hwnd,mem,2),'Unable to capture AE dialog'
                    buf=C.create_string_buffer(width*height*4)
                    assert g.GetBitmapBits(bmp,len(buf),buf)
                    Image.frombuffer('RGB',(width,height),buf.raw,'raw','BGRX',0,1).save(dest)
                finally:
                    g.SelectObject(mem,old);g.DeleteObject(bmp);g.DeleteDC(mem);u.ReleaseDC(hwnd,dc)
            print('DIALOG SCREENSHOT',str(dest),'RECT',rect.left,rect.top,rect.right,rect.bottom,flush=True)
        recovery=any('восстанов' in t.lower() or 'recovery' in t.lower() for t in labels)
        if click and recovery:
            buttons=[h for h,t in children if t.replace('&','').strip().lower() in {'продолжить','continue'}]
            if len(buttons)==1:
                assert u.PostMessageW(buttons[0],0x00F5,0,0)
                clicked+=1
                print('CONTINUE CLICKED',flush=True)
    if click and clicked!=1:
        raise RuntimeError(f'Expected one known recovery Continue button; clicked {clicked}')


if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--click',action='store_true'); p.add_argument('--pid',type=int)
    p.add_argument('--capture',action='store_true')
    p.add_argument('--point',type=int,nargs=2)
    args=p.parse_args(); main(args.click,args.pid,args.capture,args.point)
