# -*- coding: utf-8 -*-
"""
用 CDP（Chrome DevTools Protocol）驱动 Edge 无头模式截图。
相比 --screenshot 参数，这里可以「等待页面真正渲染完成」再截图，
适合本项目流式输出的对话界面。
"""
import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


# ---------------------------------------------------------------- WebSocket
class WS(object):
    def __init__(self, url, timeout=30):
        # ws://host:port/path
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, hostport, key)
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        if b"101" not in buf.split(b"\r\n")[0]:
            raise RuntimeError("WebSocket 握手失败：%r" % buf[:120])
        self._buf = b""

    def send(self, text):
        data = text.encode("utf-8")
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def _read(self, n):
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise IOError("连接已关闭")
            out += chunk
        return out

    def recv(self):
        while True:
            b0, b1 = self._read(2)
            opcode = b0 & 0x0F
            fin = b0 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(length)
            if opcode == 0x9:                       # ping
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if opcode == 0x8:                       # close
                raise IOError("对端关闭")
            if opcode in (0x1, 0x2, 0x0):
                self._buf += payload
                if fin:
                    msg = self._buf
                    self._buf = b""
                    return msg.decode("utf-8", "ignore")


class CDP(object):
    def __init__(self, ws_url):
        self.ws = WS(ws_url)
        self._id = 0
        self._pending = []

    def call(self, method, params=None, timeout=40):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})
            self._pending.append(msg)
        raise TimeoutError(method)

    def eval(self, expr):
        r = self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": False})
        return r.get("result", {}).get("value")


def wait_port(port, timeout=25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port, timeout=1) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("调试端口未就绪")


def launch_edge(port, profile):
    return subprocess.Popen(
        [EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--force-device-scale-factor=1",
         "--remote-debugging-port=%d" % port,
         "--user-data-dir=%s" % profile,
         "--no-first-run", "--no-default-browser-check",
         "--disable-extensions", "--disable-background-networking",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def capture(port, url, out_png, width=1280, height=900,
            ready_expr="document.readyState==='complete'", max_wait=60,
            min_h=600, max_h=6000, scroll_sel=None):
    tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port,
                                             timeout=5).read().decode())
    page = None
    for t in tabs:
        if t.get("type") == "page":
            page = t
            break
    if page is None:
        raise RuntimeError("没有可用的页面目标")
    cdp = CDP(page["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    cdp.call("Emulation.setDeviceMetricsOverride", {
        "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
    cdp.call("Page.navigate", {"url": url})

    t0 = time.time()
    ok = False
    while time.time() - t0 < max_wait:
        try:
            if cdp.eval(ready_expr):
                ok = True
                break
        except Exception:
            pass
        time.sleep(0.4)
    time.sleep(0.9)     # 等动画/字体稳定

    if scroll_sel:
        try:
            cdp.eval("(function(){var e=document.querySelector('%s');"
                     "if(e){e.scrollIntoView({block:'center'});return true;}return false;})()"
                     % scroll_sel)
        except Exception:
            pass
        time.sleep(0.6)
        shot = cdp.call("Page.captureScreenshot", {"format": "png"})
        with open(out_png, "wb") as f:
            f.write(base64.b64decode(shot["data"]))
        try:
            cdp.ws.sock.close()
        except Exception:
            pass
        return ok, height, os.path.getsize(out_png)

    h = cdp.eval("Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
    h = int(h or height)
    h = max(min_h, min(h, max_h))
    cdp.call("Emulation.setDeviceMetricsOverride", {
        "width": width, "height": h, "deviceScaleFactor": 1, "mobile": False})
    time.sleep(0.35)
    shot = cdp.call("Page.captureScreenshot", {
        "format": "png", "captureBeyondViewport": True,
        "clip": {"x": 0, "y": 0, "width": width, "height": h, "scale": 1}})
    with open(out_png, "wb") as f:
        f.write(base64.b64decode(shot["data"]))
    try:
        cdp.ws.sock.close()
    except Exception:
        pass
    return ok, h, os.path.getsize(out_png)


if __name__ == "__main__":
    # 供测试：python tools_cdp_shot.py <url> <out.png> [width] [height] [ready_expr]
    url = sys.argv[1]
    out = sys.argv[2]
    w = int(sys.argv[3]) if len(sys.argv) > 3 else 1280
    h = int(sys.argv[4]) if len(sys.argv) > 4 else 900
    expr = sys.argv[5] if len(sys.argv) > 5 else "document.readyState==='complete'"
    port = 9333
    prof = os.path.join(os.environ.get("TEMP", "."), "edge_cdp_prof")
    p = launch_edge(port, prof)
    try:
        wait_port(port)
        print(capture(port, url, out, w, h, expr))
    finally:
        p.terminate()
