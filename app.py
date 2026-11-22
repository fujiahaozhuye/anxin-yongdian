# -*- coding: utf-8 -*-
"""
安心用电 · Web 服务（纯标准库，无需 pip 安装任何依赖）
------------------------------------------------------------
启动：  python app.py
访问：  http://127.0.0.1:8000

路由：
  GET  /                 应用主页面
  GET  /static/<file>    静态资源
  POST /api/chat         对话（SSE 流式，推送工作流节点进度 + 逐字输出）
  GET  /api/config       前端可见配置（不含任何密钥）
  GET  /api/kb           知识库文档清单与统计
  POST /api/kb/search    检索测试（用于知识库召回效果验证）
  GET  /api/workflow     工作流节点与插件清单
  GET  /api/history      会话历史
  POST /api/history/clear 清空会话
  GET  /api/qr           生成访问二维码 PNG
  GET  /qr               二维码展示页
  GET  /api/health       健康检查
  GET  /api/logs         最近联调日志
"""
import json
import os
import sys
import time
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
config.bootstrap()

import flow
import kb as kbmod
import tools as toolmod
import qrcode_gen

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
SESSION_FILE = os.path.join(DATA_DIR, "sessions.json")
LOG_FILE = os.path.join(DATA_DIR, "access.log")
if not os.path.isdir(DATA_DIR):
    os.makedirs(DATA_DIR)

_LOCK = threading.Lock()
_SESSIONS = {}
_LOGS = []

# ---- 公网限流状态 ----
_RATE_IP = {}                                  # ip -> [时间戳]
_RATE_GLOBAL = {"day": "", "count": 0}


def rate_check(ip):
    """返回 (是否放行, 提示语)。防止公网暴露时被刷爆 API 额度。"""
    if not config.RATE.get("enable"):
        return True, ""
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    with _LOCK:
        if _RATE_GLOBAL["day"] != today:
            _RATE_GLOBAL["day"] = today
            _RATE_GLOBAL["count"] = 0
        if _RATE_GLOBAL["count"] >= config.RATE["global_per_day"]:
            return False, "今天的提问额度已用完，请明天再来，或直接拨打 95598 / 110 / 120。"
        stamps = [t for t in _RATE_IP.get(ip, []) if now - t < 60]
        if len(stamps) >= config.RATE["per_ip_per_min"]:
            _RATE_IP[ip] = stamps
            return False, "您问得有点快，请等一分钟再问，我一条一条答。"
        stamps.append(now)
        _RATE_IP[ip] = stamps
        _RATE_GLOBAL["count"] += 1
        if len(_RATE_IP) > 2000:                # 简单防内存膨胀
            for k in list(_RATE_IP)[:1000]:
                _RATE_IP.pop(k, None)
    return True, ""

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".woff2": "font/woff2",
}


# ---------------------------------------------------------------- 会话持久化
def load_sessions():
    global _SESSIONS
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                _SESSIONS = json.load(f)
        except Exception:
            _SESSIONS = {}


def save_sessions():
    try:
        tmp = SESSION_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_SESSIONS, f, ensure_ascii=False)
        os.replace(tmp, SESSION_FILE)
    except Exception as e:
        print("[warn] 会话保存失败：%s" % e)


def append_log(entry):
    line = "%s | %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), entry)
    _LOGS.append(line)
    del _LOGS[:-500]
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 请求处理
class Handler(BaseHTTPRequestHandler):
    server_version = "AnXinYongDian/" + config.APP_VERSION
    protocol_version = "HTTP/1.1"

    # ---------- 工具方法 ----------
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data, ctype, code=200, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path):
        if not os.path.isfile(path):
            self._send_json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            data = f.read()
        self._send_bytes(data, MIME.get(ext, "application/octet-stream"))

    def log_message(self, fmt, *args):
        # 默认会把每条访问打到 stderr，改成简洁的联调日志
        append_log("%s %s" % (self.client_address[0], fmt % args))

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------- SSE ----------
    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

    def _sse(self, obj):
        try:
            payload = "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
            self.wfile.write(payload.encode("utf-8"))
            self.wfile.flush()
        except Exception:
            raise IOError("客户端已断开")

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                self._send_file(os.path.join(config.WEB_DIR, "index.html"))
            elif path.startswith("/static/"):
                rel = path[len("/static/"):].replace("..", "")
                self._send_file(os.path.join(config.WEB_DIR, rel))
            elif path == "/api/health":
                self._send_json({"ok": True, "app": config.APP_NAME,
                                 "version": config.APP_VERSION,
                                 "llm": config.llm_ready()})
            elif path == "/api/config":
                self._send_json(config.public_config())
            elif path == "/api/kb":
                kb = kbmod.get_kb()
                self._send_json({"stats": kb.stats(), "docs": kb.docs})
            elif path == "/api/workflow":
                self._send_json({"nodes": flow.NODES,
                                 "plugins": toolmod.manifest(),
                                 "engine": "DeepSeek " + config.LLM["model"]
                                 if config.llm_ready() else "本地受控模板引擎"})
            elif path == "/api/history":
                sid = (qs.get("sid") or [""])[0]
                with _LOCK:
                    msgs = list(_SESSIONS.get(sid, []))
                self._send_json({"sid": sid, "messages": msgs})
            elif path == "/api/logs":
                self._send_json({"logs": _LOGS[-80:]})
            elif path == "/api/selftest":
                import selftest
                r = selftest.run_suite()
                append_log("一键自检 | %d 条用例 | 意图 %.1f%% | 检索 %.1f%% | 工具 %.1f%%"
                           % (r["total"], r["intent_rate"], r["retrieval_rate"], r["tool_rate"]))
                self._send_json(r)
            elif path == "/api/qr":
                data = (qs.get("data") or ["http://127.0.0.1:%d" % config.PORT])[0]
                scale = int((qs.get("scale") or ["10"])[0])
                scale = max(4, min(scale, 20))
                matrix, ver, mask, ec = qrcode_gen.make_matrix(data, "M")
                png = qrcode_gen.to_png_bytes(matrix, scale=scale, border=4)
                self._send_bytes(png, "image/png")
            elif path == "/qr":
                self._qr_page(qs)
            elif path == "/favicon.ico":
                self._send_bytes(b"", "image/x-icon")
            else:
                self._send_json({"error": "not found", "path": path}, 404)
        except Exception:
            append_log("ERROR GET %s\n%s" % (path, traceback.format_exc()))
            self._send_json({"error": "internal error"}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        body = self._read_body()

        if path == "/api/chat":
            self._handle_chat(body)
        elif path == "/api/kb/search":
            kb = kbmod.get_kb()
            q = (body.get("query") or "").strip()
            top_k = int(body.get("top_k") or config.RETRIEVAL["top_k"])
            if not q:
                self._send_json({"error": "empty query"}, 400)
                return
            res = kb.hit(q, top_k)
            append_log("检索测试 | q=%s | 命中=%s | top=%.3f"
                       % (q, res["hit"], res["results"][0]["score"] if res["results"] else 0))
            self._send_json(res)
        elif path == "/api/history/clear":
            sid = body.get("sid") or ""
            with _LOCK:
                _SESSIONS.pop(sid, None)
                save_sessions()
            self._send_json({"ok": True})
        elif path == "/api/feedback":
            sid = body.get("sid") or ""
            text = (body.get("text") or "")[:500]
            append_log("用户反馈 | sid=%s | %s" % (sid, text))
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found"}, 404)

    # ---------- 对话主流程 ----------
    def _handle_chat(self, body):
        sid = body.get("sid") or "anonymous"
        text = (body.get("text") or "").strip()
        if not text:
            self._send_json({"error": "empty text"}, 400)
            return
        if len(text) > 500:
            text = text[:500]

        with _LOCK:
            history = list(_SESSIONS.get(sid, []))

        # 公网限流：在开始推流之前判断，避免白占一次连接
        ip = self.client_address[0]
        allowed, tip = rate_check(ip)
        if not allowed:
            append_log("限流 | ip=%s | %s" % (ip, tip))
            self._send_json({"error": "rate_limited", "message": tip}, 429)
            return

        self._start_sse()
        t0 = time.time()
        result = {}
        try:
            self._sse({"type": "ack", "sid": sid, "ts": int(time.time() * 1000)})

            def emit(ev):
                self._sse(ev)

            result = flow.run(text, history, emit)

            with _LOCK:
                msgs = _SESSIONS.setdefault(sid, [])
                msgs.append({"role": "user", "content": text,
                             "time": time.strftime("%Y-%m-%d %H:%M:%S")})
                msgs.append({"role": "assistant", "content": result.get("answer", ""),
                             "intent": result.get("intent_label"),
                             "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "sources": result.get("sources", []),
                             "tool": result.get("tool"),
                             "cards": result.get("cards", [])})
                del msgs[:-config.UI["max_history"] * 2]
                save_sessions()

            cost = int((time.time() - t0) * 1000)
            append_log("对话 | sid=%s | 意图=%s | 引擎=%s | 工具=%s | %dms | q=%s"
                       % (sid, result.get("intent"), result.get("engine"),
                          (result.get("tool") or {}).get("tool", "-"), cost, text[:40]))

            self._sse({"type": "final", "payload": result, "cost_ms": cost})
        except IOError:
            pass
        except Exception:
            err = traceback.format_exc()
            append_log("ERROR chat\n" + err)
            try:
                self._sse({"type": "error", "message": "服务端处理异常，已记录日志。",
                           "detail": err.strip().splitlines()[-1][:200]})
            except Exception:
                pass

    # ---------- 二维码页 ----------
    def _qr_page(self, qs):
        url = (qs.get("url") or [""])[0]
        if not url:
            host = self.headers.get("Host") or ("127.0.0.1:%d" % config.PORT)
            url = "http://" + host + "/"
        html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>安心用电 · 访问二维码</title>
<style>
body{{font-family:"Microsoft YaHei",sans-serif;background:#f4f7fb;text-align:center;
margin:0;padding:40px 16px;color:#1c2b3a}}
h1{{font-size:34px;margin:8px 0 4px}}
p{{font-size:22px;color:#41546a;margin:6px 0 22px}}
img{{width:360px;height:360px;background:#fff;padding:16px;border-radius:18px;
box-shadow:0 8px 28px rgba(20,40,70,.14)}}
a{{display:inline-block;margin-top:22px;font-size:22px;color:#0b6bcb;word-break:break-all}}
</style></head><body>
<h1>安心用电 · 长辈居家用电安全与电费防诈助手</h1>
<p>请用手机微信/相机扫描下方二维码，即可在手机上打开本应用</p>
<img src="/api/qr?scale=12&data={q}" alt="访问二维码">
<div><a href="{u}">{u}</a></div>
<p style="margin-top:26px;font-size:18px;color:#6b7c90">
电气工程及其自动化 · 付家豪 · 202627020323</p>
</body></html>""".format(q=__import__("urllib.parse", fromlist=["quote"]).quote(url, safe=""), u=url)
        self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")


def main():
    load_sessions()
    # 启动时把历史日志尾部载入内存，便于「联调日志」页直接查看
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                tail = f.readlines()[-120:]
            _LOGS.extend(x.rstrip("\n") for x in tail if x.strip())
        except Exception:
            pass
    kb = kbmod.get_kb()
    st = kb.stats()
    host, port = config.HOST, config.PORT
    append_log("服务启动 | %s:%d | 知识库 %d 篇 %d 切片 | 大模型 %s"
               % (host, port, st["docs"], st["chunks"],
                  "已启用(" + config.LLM["model"] + ")" if config.llm_ready() else "未配置，使用本地引擎"))
    print("=" * 66)
    print("  安心用电 · %s   v%s" % (config.APP_SUBTITLE, config.APP_VERSION))
    print("  知识库：%d 篇文档 / %d 个切片 / %d 字" % (st["docs"], st["chunks"], st["chars"]))
    print("  大模型：%s" % ("DeepSeek " + config.LLM["model"] if config.llm_ready()
                            else "未配置 API Key，自动使用本地受控模板引擎"))
    print("  监听：  http://%s:%d" % ("127.0.0.1" if host == "0.0.0.0" else host, port))
    print("  二维码：http://127.0.0.1:%d/qr" % port)
    print("=" * 66)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
