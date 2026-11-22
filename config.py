# -*- coding: utf-8 -*-
"""
安心用电 · 全局配置
------------------------------------------------------------
所有可调参数集中在此，方便按当地电价政策与服务器环境修改。
"""
import os
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data", "kb")
WEB_DIR = os.path.join(BASE_DIR, "web")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

APP_NAME = "安心用电"
APP_SUBTITLE = "长辈居家用电安全与电费防诈助手"
APP_VERSION = "1.3.0"

# ---------------- 服务 ----------------
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

# ---------------- 大模型（DeepSeek） ----------------
# Key 的读取顺序：环境变量 DEEPSEEK_API_KEY > config.json 的 llm.api_key
LLM = {
    "provider": "DeepSeek",
    "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
    "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
    "temperature": 0.3,          # 偏低，保证工程类回答稳定、少编造
    "max_tokens": 700,           # 回答本来就限 450 字，700 足够，省输出费
    "timeout": 60,
    "enable": True,              # False 时强制走本地受控模板生成（零 API 成本）
    "history_turns": 2,          # 带入提示词的历史轮数，越少越省钱
    "context_chars": 700,        # 每条检索资料截断长度
}

# ---------------- 回答缓存（省钱关键） ----------------
# 相同（去掉标点后相同）的问题在 TTL 内直接复用上次的大模型回答，
# 不再发起 API 请求。课堂演示反复问同一句时尤其有用。
CACHE = {
    "enable": True,
    "ttl": int(os.environ.get("ANXIN_CACHE_TTL", "86400")),   # 秒，默认 1 天
    "max_items": 200,
}

# ---------------- 公网限流（防止别人刷爆 API 额度） ----------------
RATE = {
    "enable": True,
    "per_ip_per_min": 12,      # 单个 IP 每分钟最多提问次数
    "global_per_day": int(os.environ.get("ANXIN_DAILY_CAP", "400")),  # 全天总量上限
}

# ---------------- 知识库与检索 ----------------
RETRIEVAL = {
    "top_k": 4,                  # 默认召回条数
    "chunk_max_chars": 700,      # 切片最大长度
    "chunk_overlap": 80,         # 相邻切片重叠字数
    "min_score": 0.35,           # 低于该分数视为未命中
    "bm25_k1": 1.5,
    "bm25_b": 0.75,
}

# ---------------- 居民阶梯电价（示例值，须按当地公布价格修改） ----------------
# 档位结构依据：发改价格〔2011〕2617 号《关于居民生活用电试行阶梯电价的指导意见》
# 单价仅为演示用示例值，实际以当地省级价格主管部门现行文件为准。
TARIFF = {
    "region": "示例地区（请按当地价格文件修改）",
    "unit": "元/千瓦时",
    "tiers": [
        {"name": "第一档", "upper": 180, "price": 0.51},
        {"name": "第二档", "upper": 260, "price": 0.56},
        {"name": "第三档", "upper": None, "price": 0.81},
    ],
    "source": "档位结构依据发改价格〔2011〕2617号；单价为示例值，以当地供电营业厅公布为准",
}

# ---------------- 适老化与安全 ----------------
UI = {
    "font_scale_default": 1.0,
    "tts_default": True,         # 默认开启语音朗读
    "max_history": 50,           # 单个会话保留的对话轮数
}

# ---------------- 合规 ----------------
COMPLIANCE = {
    "disclaimer": "本应用提供的是通用用电安全常识与工程估算参考，不替代持证电工的现场检查与供电企业的正式答复。",
    "emergency_numbers": [
        {"name": "急救", "number": "120"},
        {"name": "火警", "number": "119"},
        {"name": "报警", "number": "110"},
        {"name": "反诈专线", "number": "96110"},
        {"name": "供电服务", "number": "95598"},
    ],
}


def _load_file_config():
    """读取 config.json（可选文件，优先级低于环境变量）。"""
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:      # 配置损坏不应导致服务起不来
        print("[config] config.json 解析失败，已忽略：%s" % e)
        return {}


def bootstrap():
    """合并 config.json 配置；环境变量已存在的项不被覆盖。"""
    cfg = _load_file_config()

    llm_cfg = cfg.get("llm", {})
    if llm_cfg.get("api_key") and not os.environ.get("DEEPSEEK_API_KEY"):
        LLM["api_key"] = llm_cfg["api_key"]
    if llm_cfg.get("model"):
        LLM["model"] = llm_cfg["model"]
    if llm_cfg.get("base_url"):
        LLM["base_url"] = llm_cfg["base_url"]
    if "temperature" in llm_cfg:
        LLM["temperature"] = llm_cfg["temperature"]
    if "enable" in llm_cfg:
        LLM["enable"] = bool(llm_cfg["enable"])

    if cfg.get("tariff"):
        TARIFF.update(cfg["tariff"])
    if cfg.get("retrieval"):
        RETRIEVAL.update(cfg["retrieval"])
    if cfg.get("ui"):
        UI.update(cfg["ui"])
    return cfg


def llm_ready():
    return bool(LLM["api_key"]) and bool(LLM["enable"])


def public_config():
    """只暴露可以给前端看的字段，绝不返回 api_key。"""
    return {
        "app_name": APP_NAME,
        "subtitle": APP_SUBTITLE,
        "version": APP_VERSION,
        "llm_enabled": llm_ready(),
        "llm_model": LLM["model"] if llm_ready() else "本地受控模板引擎",
        "cache_enabled": CACHE.get("enable", False),
        "cache_ttl": CACHE.get("ttl"),
        "retrieval": RETRIEVAL,
        "tariff": TARIFF,
        "emergency_numbers": COMPLIANCE["emergency_numbers"],
        "disclaimer": COMPLIANCE["disclaimer"],
    }
