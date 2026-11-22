# -*- coding: utf-8 -*-
"""
大模型接入（DeepSeek）+ 可用性兜底
------------------------------------------------------------
- 有 API Key 时：调用 DeepSeek /chat/completions（SSE 流式），
  把知识库检索结果与插件结果作为上下文，要求模型"只依据给定资料回答"。
- 无 Key 或调用失败时：自动降级为本地受控模板生成，功能不中断。
  这一设计保证课堂现场演示不依赖网络与额度。
"""
import json
import urllib.request
import urllib.error

import config


class LLMError(Exception):
    pass


def _payload(messages, stream):
    return {
        "model": config.LLM["model"],
        "messages": messages,
        "temperature": config.LLM["temperature"],
        "max_tokens": config.LLM["max_tokens"],
        "stream": stream,
    }


def stream_chat(messages):
    """流式调用，逐段 yield 文本。失败抛 LLMError，由上层降级。"""
    if not config.llm_ready():
        raise LLMError("未配置 API Key")
    url = config.LLM["base_url"].rstrip("/") + "/chat/completions"
    body = json.dumps(_payload(messages, True)).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + config.LLM["api_key"])
    try:
        with urllib.request.urlopen(req, timeout=config.LLM["timeout"]) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    yield piece
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        raise LLMError("HTTP %s %s" % (e.code, detail))
    except Exception as e:
        raise LLMError(str(e))


def chat(messages):
    """非流式调用，返回完整文本（用于自检与离线批量测试）。"""
    if not config.llm_ready():
        raise LLMError("未配置 API Key")
    url = config.LLM["base_url"].rstrip("/") + "/chat/completions"
    body = json.dumps(_payload(messages, False)).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + config.LLM["api_key"])
    try:
        with urllib.request.urlopen(req, timeout=config.LLM["timeout"]) as resp:
            obj = json.loads(resp.read().decode("utf-8", "ignore"))
        return obj["choices"][0]["message"]["content"]
    except Exception as e:
        raise LLMError(str(e))
