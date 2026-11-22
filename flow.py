# -*- coding: utf-8 -*-
"""
工作流引擎（6 节点）
------------------------------------------------------------
N1 安全护栏预检  gate        → 急诊式提问 / 涉电诈骗话术，命中即置顶处置卡
N2 意图识别与槽位抽取 intent  → 6 类意图 + 功率/时长/电量/插座/导线等槽位
N3 知识库检索     retrieve   → BM25 检索 top-k，带出处与片段
N4 插件工具调用   tool       → 按意图调度 5 个自建工具
N5 受控答案生成   generate   → DeepSeek 流式生成（无 Key 时本地模板兜底）
N6 合规审核       compliance → 来源标注、免责声明、危险建议拦截
"""
import hashlib
import re
import time

import config
import kb as kbmod
import llm as llmmod
import tools as toolmod

# ---------------------------------------------------------------- 回答缓存
# 同一个问题（去标点、去空格后相同）且检索结果与插件结果一致时，
# TTL 内直接复用上一次的大模型输出，不再发起 API 请求。
_LLM_CACHE = {}


def _cache_key(query, hits, tool_result):
    norm = re.sub(r"[\s，。？！、,.?!；;：:]+", "", query).lower()
    cids = ",".join(h["cid"] for h in hits)
    tname = (tool_result or {}).get("tool", "-")
    raw = "%s|%s|%s" % (norm, cids, tname)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _cache_get(key):
    if not config.CACHE.get("enable"):
        return None
    item = _LLM_CACHE.get(key)
    if not item:
        return None
    if time.time() - item["ts"] > config.CACHE["ttl"]:
        _LLM_CACHE.pop(key, None)
        return None
    return item["text"]


def _cache_put(key, text):
    if not config.CACHE.get("enable") or not text:
        return
    if len(_LLM_CACHE) >= config.CACHE["max_items"]:
        oldest = min(_LLM_CACHE.items(), key=lambda kv: kv[1]["ts"])[0]
        _LLM_CACHE.pop(oldest, None)
    _LLM_CACHE[key] = {"ts": time.time(), "text": text}


def cache_stats():
    return {"enabled": config.CACHE.get("enable", False),
            "items": len(_LLM_CACHE),
            "ttl": config.CACHE.get("ttl")}

# ---------------------------------------------------------------- 节点定义
NODES = [
    {"id": "gate", "name": "安全护栏预检", "index": 1,
     "desc": "识别触电／火灾等急诊式提问与涉电诈骗话术，命中即置顶处置卡"},
    {"id": "intent", "name": "意图识别与槽位抽取", "index": 2,
     "desc": "6 类意图分类；抽取功率、时长、电量、插座电流、导线截面等槽位"},
    {"id": "retrieve", "name": "知识库检索", "index": 3,
     "desc": "BM25 + 口语同义词扩展召回，返回切片与出处（必备节点）"},
    {"id": "tool", "name": "插件工具调用", "index": 4,
     "desc": "按意图调度电费测算／载流量校核／跳闸诊断／反诈核验"},
    {"id": "generate", "name": "受控答案生成", "index": 5,
     "desc": "DeepSeek 在检索资料与适老化表达约束下生成回答，流式输出"},
    {"id": "compliance", "name": "合规审核与来源标注", "index": 6,
     "desc": "校验来源标注、追加免责声明、拦截危险建议"},
]

INTENT_LABEL = {
    "emergency": "用电急救／火灾",
    "fraud": "涉电诈骗",
    "trip": "跳闸停电",
    "bill": "电费电价",
    "load": "大功率电器与线路",
    "safety": "选购与日常安全",
    "other": "综合咨询",
}

EMERGENCY_RE = re.compile(r"(触电|电死|电到|被电|电击|电伤了|电晕|着火|起火|火警|冒烟|爆炸|烧伤)")
FRAUD_RE = re.compile(r"(骗|诈骗|催缴|欠费|转账|验证码|银行卡|上门收|换表费|报销|补贴|退款|退税|短信链接|陌生电话|冒充)")
TRIP_RE = re.compile(r"(跳闸|跳了|没电|断电|停电|闸|空开|短路|送不上|推不上去)")
BILL_RE = re.compile(r"(电费|多少钱|电价|几度|度电|用电量|抄表|账单|省钱|费电|电表走)")
LOAD_RE = re.compile(r"(电暖器|油汀|暖风机|空调|热水器|电磁炉|微波炉|电水壶|电吹风|插排|插线板|插座|功率|瓦|带得动|接得|几平方|载流量|超载|烧断)")
SAFETY_RE = re.compile(r"(买|选购|挑|怎么选|CCC|3C|能效|老电器|用了[0-9一二三四五六七八九十]+年|安全|危险|危害|注意|预防|保养|地线|接地|标志|认证)")

CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "半": 0.5}


# ---------------------------------------------------------------- 工具函数
def _cn2num(s):
    if s in CN_NUM:
        return float(CN_NUM[s])
    if s.startswith("十") and len(s) == 1:
        return 10.0
    if "十" in s:
        a, _, b = s.partition("十")
        return float((CN_NUM.get(a, 1) if a else 1) * 10 + (CN_NUM.get(b, 0) if b else 0))
    return None


def _num(tok):
    if tok is None:
        return None
    tok = tok.strip()
    if re.fullmatch(r"\d+(\.\d+)?", tok):
        return float(tok)
    if re.fullmatch(r"[零一二两三四五六七八九十半]+", tok):
        return _cn2num(tok)
    return None


NUM = r"(\d+(?:\.\d+)?|[零一二两三四五六七八九十半]+)"


# ---------------------------------------------------------------- N2 意图与槽位
def classify(text):
    if EMERGENCY_RE.search(text):
        return "emergency"
    if FRAUD_RE.search(text):
        return "fraud"
    if TRIP_RE.search(text):
        return "trip"
    if BILL_RE.search(text):
        return "bill"
    # 选购/安全类提问优先于"大功率电器"类，避免"买电暖器看哪些标志"被判成负载计算
    if SAFETY_RE.search(text):
        return "safety"
    if LOAD_RE.search(text):
        return "load"
    return "other"


def extract_slots(text):
    slots = {"text": text}
    motor = re.search(r"(空调|冰箱|洗衣机|抽油烟机|电扇|电机|水泵)", text)

    m = re.search(NUM + r"\s*(千瓦|kw|KW|Kw)", text)
    if m:
        v = _num(m.group(1))
        if v:
            slots["power_w"] = v * 1000
    if "power_w" not in slots:
        m = re.search(NUM + r"\s*(w|W|瓦)", text)
        if m:
            v = _num(m.group(1))
            if v:
                slots["power_w"] = v

    m = re.search(NUM + r"\s*(度|千瓦时|kwh|KWH|kWh)", text)
    if m:
        v = _num(m.group(1))
        if v:
            slots["kwh"] = v

    m = re.search(NUM + r"\s*个?\s*(小时|钟头|h)", text)
    if m:
        v = _num(m.group(1))
        if v:
            slots["hours"] = v
    elif "一晚上" in text or "一整晚" in text:
        slots["hours"] = 8
    elif "整晚" in text:
        slots["hours"] = 9

    # 「一天开6小时」里的"一天"是每日频次，不是总天数，故只认阿拉伯数字的「N天」
    m = re.search(r"(\d+(?:\.\d+)?)\s*天", text)
    if m:
        slots["days"] = float(m.group(1))
    elif "一个月" in text or ("月" in text and "这月" in text):
        slots["days"] = 30
    elif "半个月" in text:
        slots["days"] = 15

    m = re.search(r"(\d+)\s*(a|A|安)(?![a-zA-Z])", text)
    if m:
        slots["socket_a"] = float(m.group(1))
    if "插座" in text and "socket_a" not in slots and re.search(r"(普通|10\s*安)", text):
        slots["socket_a"] = 10.0

    m = re.search(NUM + r"\s*(平方毫米|平方|mm2|㎜²|mm²)", text)
    if m:
        v = _num(m.group(1))
        if v:
            slots["wire_mm2"] = v

    m = re.search(r"[cC]\s*(\d{1,3})", text)
    if m:
        slots["breaker_a"] = float(m.group(1))

    if motor:
        slots["motor"] = True
        slots["appliance"] = motor.group(1)
    return slots


# ---------------------------------------------------------------- 人设提示词
PERSONA_SYSTEM = """你是"安心用电"——一位专为长辈服务的居家用电安全顾问。
【角色】你是电气工程及其自动化专业的注册电气工程师，现在用最朴素的话给60岁以上的叔叔阿姨、爷爷奶奶讲用电的事。
【目标】让长辈看完就知道"现在马上做什么"，避免触电、电气火灾和被"电费"名义骗钱，同时看得懂、记得住、敢操作。
【技能】住宅低压配电、剩余电流保护（漏保）、导线载流量与断路器配合、家用电器功率核算、居民阶梯电价核算、涉电类电信诈骗识别、触电急救与电气火灾处置。
【约束】
1. 只依据【参考资料】与【工具结果】回答，资料里没有的不要编；确实不确定时明确说"这个需要请持证电工现场看"。
2. 先说最要紧的一步（断电、挂断电话、拨打120等），再说原因，最后给1～2条预防建议。
3. 句子短、不用生僻术语；必须用术语时，紧跟一句括号解释。
4. 不给出需要带电作业、拆配电箱、短接漏保之类的危险指导；不替代持证电工与供电企业答复。
5. 涉及钱与个人信息时，一律提醒走官方渠道（95598、网上国网、110、96110）。
6. 回答末尾用【资料出处】列出用到的资料标题与章节，格式如：GB/T 6829—2017《剩余电流动作保护电器（RCD）的一般要求》。
7. 全文控制在 450 字以内，用「」「」分段，不要用 Markdown 星号和井号。"""


def build_messages(user_text, history, hits, tool_result, intent):
    ctx = []
    ctx_len = int(config.LLM.get("context_chars", 700))
    for i, h in enumerate(hits, 1):
        ctx.append("【资料%d】《%s》%s\n%s" % (i, h["doc_title"], h["section"], h["text"][:ctx_len]))
    context_block = "\n\n".join(ctx) if ctx else "（本次未检索到相关资料）"

    tool_block = "（本次未调用工具）"
    if tool_result:
        lines = ["工具：%s" % tool_result.get("title", "")]
        for r in tool_result.get("rows", []):
            lines.append("- %s：%s" % (r.get("k"), r.get("v")))
        if tool_result.get("problems"):
            lines.append("发现的问题：" + "；".join(tool_result["problems"]))
        if tool_result.get("causes"):
            lines.append("可能原因：" + "；".join(tool_result["causes"]))
        if tool_result.get("steps"):
            lines.append("建议步骤：" + " ".join(tool_result["steps"]))
        if tool_result.get("advice"):
            lines.append("提示：" + tool_result["advice"])
        if tool_result.get("source"):
            lines.append("工具依据：" + tool_result["source"])
        tool_block = "\n".join(lines)

    msgs = [{"role": "system", "content": PERSONA_SYSTEM}]
    keep = int(config.LLM.get("history_turns", 2)) * 2       # 只带最近 N 轮，控制输入费用
    for m in history[-keep:] if keep > 0 else []:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            msgs.append({"role": m["role"], "content": m["content"][:600]})
    msgs.append({
        "role": "user",
        "content": "【用户问题】%s\n【识别到的意图】%s\n\n【参考资料】\n%s\n\n【工具结果】\n%s\n\n"
                   "请按约束回答，末尾必须给出【资料出处】。"
                   % (user_text, INTENT_LABEL.get(intent, intent), context_block, tool_block),
    })
    return msgs


# ---------------------------------------------------------------- N5 本地兜底生成
def compose_local(user_text, intent, hits, tool_result):
    out = []
    if intent == "emergency":
        out.append("【先说最要紧的】先断电！拉下家里的总开关或拔掉插头，绝对不要直接用手去拉人。")
        out.append("【接着马上做】1）确认人有没有反应、有没有呼吸；2）立刻拨打 120；"
                   "3）如果没反应没呼吸，马上做胸外按压：两乳头连线中点，双手叠压，深度 5～6 厘米，每分钟 100～120 次，按压 30 次吹气 2 次；"
                   "4）火已经起来就先撤离再打 119，电气火灾只能用干粉或二氧化碳灭火器，不能用水。")
        out.append("【原因】电击最危险的是引起心跳骤停，抢救黄金时间只有 4 分钟，所以先断电、先呼救、先按压。")
    elif intent == "fraud":
        out.append("【先说最要紧的】先挂电话，不要争辩、不要按对方说的操作手机。")
        out.append("【接着马上做】1）不点对方发的链接、不扫二维码、不开屏幕共享；"
                   "2）自己拨 95598 核实是不是真欠费，或让子女打开「网上国网」看账单；"
                   "3）已经把验证码或银行卡号说出去了，立刻打 110 报警并联系银行冻结；4）拿不准就打 96110 反诈专线。")
        out.append("【记住一句话】电费只能走官方渠道，供电公司绝不会让你往个人账户转钱，也绝不会要你的验证码和密码。")
    elif intent == "trip":
        out.append("【先说最要紧的】先看清楚是哪只开关跳了：带试验按钮（T）的那只是漏保，跳它多半是漏电；普通空开跳多半是过载或短路。")
        out.append("【接着马上做】1）拔掉这一路所有电器插头再合闸，能送上就是电器的问题；"
                   "2）一个个插回去，插到哪只跳就是哪只坏，那只立刻停用；"
                   "3）拔完还是跳，说明是线路问题，要请持证电工，不能自己拆配电箱；"
                   "4）有焦糊味、冒烟、火花，立刻断开总闸并拨打 119，不要再送电。")
        out.append("【千万别做】不要用铜丝铁丝代替熔丝，不要把漏保短接或者绑住不让它跳——那等于把保命的东西拆了。")
    elif intent == "bill":
        out.append("【先算清楚】用电量（度）＝功率（千瓦）× 使用小时。比如 2 千瓦的电暖器开 5 小时就是 10 度电。")
        out.append("【为什么贵】居民阶梯电价是按档累加算的：第一档最便宜，超过第一档的部分才按第二档价格，超过第二档的部分按第三档价格，"
                   "不是所有度数都按最贵的算，但用得越多平均单价越高。")
        out.append("【省钱的办法】1）电器别长时间待机，机顶盒路由器的待机一个月也能耗十几度；"
                   "2）电暖器、电热水器集中时段用，不要整夜开；3）关注账单是否进入更高档位；"
                   "4）有峰谷分时电价的地区，把洗衣、充电放到谷段时间。")
    elif intent == "load":
        out.append("【先给结论】大功率电器（2000 瓦以上）一定要插墙上的 16 安专用插座，不能走插排，也不能和小电器共用。")
        out.append("【怎么判断】普通插座是 10 安，满打满算 2200 瓦；16 安插座是 3520 瓦。"
                   "2.5 平方铜线穿管大约能带 18 安、4 平方约 24 安，超过导线载流量就会发热，这是电气火灾最常见的起因。")
        out.append("【记住三句】一处插排不带两件热电器；大功率走专线；把开关换大不代表安全，可能只是把火烧进了墙里。")
    else:
        out.append("【先说结论】家里用电，第一道防线是 30 毫安、0.1 秒动作的漏电保护器，第二道才是空开和导线。")

    # 把插件算出来的具体数值写进正文，避免"只有表格没有结论"
    if tool_result:
        key_rows = [r for r in tool_result.get("rows", []) if r.get("k") not in ("计算依据",)]
        if tool_result.get("tool") == "calc_bill":
            d = {r["k"]: r["v"] for r in key_rows}
            out.append("【算给你看】%s，按阶梯电价分段累加，合计约 %s，平均 %s。" % (
                d.get("计算电量", ""), d.get("合计电费", ""), d.get("平均单价", "")))
        elif tool_result.get("tool") == "check_circuit":
            d = {r["k"]: r["v"] for r in key_rows}
            out.append("【核一遍】按 %s 估算电流 %s；%s。%s" % (
                d.get("负载功率", ""), d.get("估算电流", ""),
                d.get("建议导线截面", ""),
                "发现的问题：" + "；".join(tool_result.get("problems", [])) if tool_result.get("problems") else "参数配合合理。"))
        elif tool_result.get("steps") and intent not in ("emergency", "fraud", "trip"):
            out.append("【建议步骤】" + " ".join(tool_result["steps"][:4]))

    # 用检索到的片段补一条"依据"
    if hits:
        best = hits[0]
        out.append("【依据】《%s》%s里的要点：%s" % (best["doc_title"], best["section"], best["snippet"]))
    return "\n".join(out)


# ---------------------------------------------------------------- 主流程
def run(user_text, history=None, emit=None, top_k=None, use_llm=True):
    """执行工作流。emit(event) 用于向前端推送进度（SSE）。"""
    history = history or []
    emit = emit or (lambda e: None)
    trace = []

    def node_start(nid):
        emit({"type": "node", "id": nid, "status": "run",
              "index": next(n["index"] for n in NODES if n["id"] == nid)})

    def node_done(nid, detail, extra=None):
        info = {"index": next(n["index"] for n in NODES if n["id"] == nid), "detail": detail}
        if extra:
            info.update(extra)
        trace.append({"id": nid, "detail": detail})
        emit({"type": "node", "id": nid, "status": "ok", "info": info})

    kb = kbmod.get_kb()

    # ---- N1 安全护栏预检
    node_start("gate")
    need_emergency = bool(EMERGENCY_RE.search(user_text))
    need_fraud = bool(FRAUD_RE.search(user_text))
    gate_flags = []
    if need_emergency:
        gate_flags.append("急诊")
    if need_fraud:
        gate_flags.append("涉诈")
    node_done("gate", "安全护栏：%s" % ("命中" + "／".join(gate_flags) if gate_flags else "未触发，正常受理"))

    # ---- N2 意图识别与槽位
    node_start("intent")
    intent = classify(user_text)
    slots = extract_slots(user_text)
    slot_brief = "、".join("%s=%s" % (k, v) for k, v in slots.items() if k != "text") or "无结构化槽位"
    node_done("intent", "意图：%s；槽位：%s" % (INTENT_LABEL.get(intent, intent), slot_brief))

    # ---- N3 知识库检索
    node_start("retrieve")
    hit_res = kb.hit(user_text, top_k)
    hits = hit_res["results"]
    top_score = hits[0]["score"] if hits else 0.0
    node_done(
        "retrieve",
        "检索命中：%d 条切片，最高分 %.2f（阈值 %.2f）%s" % (
            len(hits), top_score, hit_res["threshold"], "" if hit_res["hit"] else "，低于阈值转工具兜底"),
        {"hits": [{"doc": h["doc_title"], "section": h["section"], "score": h["score"]} for h in hits]},
    )

    # ---- N4 插件工具调用
    node_start("tool")
    tool_result = None
    if need_fraud or intent == "fraud":
        tool_result = toolmod.call("fraud_check", slots)
    if intent == "trip":
        tool_result = toolmod.call("diagnose_trip", slots) or tool_result
    if intent in ("bill",) or slots.get("kwh") or (slots.get("power_w") and slots.get("hours")):
        r = toolmod.call("calc_bill", slots)
        if r:
            tool_result = r
    if intent == "load" or slots.get("socket_a") or slots.get("wire_mm2"):
        r = toolmod.call("check_circuit", slots)
        if r:
            tool_result = r
        elif intent == "load":
            r = toolmod.call("appliance_info", slots)
            if r:
                tool_result = r
    if intent == "safety" and not tool_result:
        tool_result = toolmod.call("appliance_info", slots)
    # 兜底：只要提到具体电器，就查一次功率信息，避免"什么工具都没用"的空回答
    if not tool_result:
        tool_result = toolmod.call("appliance_info", slots)
    if tool_result and any(r.get("k") == "错误" for r in tool_result.get("rows", [])):
        tool_result = None
    node_done("tool", "插件调用：%s" % (tool_result["title"] if tool_result else "本问无需计算，跳过"))

    # ---- N5 答案生成
    node_start("generate")
    answer_parts = []
    llm_used = False
    llm_error = ""
    cached = False
    ckey = _cache_key(user_text, hits, tool_result) if use_llm else None

    if ckey and _cache_get(ckey):
        cached_text = _cache_get(ckey)
        emit({"type": "generate_start", "engine": "回答缓存（本次未消耗 API）"})
        step = 18
        for i in range(0, len(cached_text), step):
            piece = cached_text[i:i + step]
            answer_parts.append(piece)
            emit({"type": "delta", "text": piece})
        llm_used = True
        cached = True
    elif config.llm_ready() and use_llm:
        try:
            messages = build_messages(user_text, history, hits, tool_result, intent)
            first = True
            for piece in llmmod.stream_chat(messages):
                if first:
                    first = False
                    emit({"type": "generate_start", "engine": "DeepSeek " + config.LLM["model"]})
                answer_parts.append(piece)
                emit({"type": "delta", "text": piece})
            llm_used = bool("".join(answer_parts).strip())
            if not llm_used:
                raise llmmod.LLMError("模型返回为空")
            _cache_put(ckey, "".join(answer_parts))     # 存下来，下次同样问题不再花钱
        except Exception as e:
            llm_error = str(e)[:120]
            answer_parts = []
            llm_used = False
            emit({"type": "fallback", "reason": llm_error})

    if not llm_used:
        emit({"type": "generate_start", "engine": "本地受控模板引擎"})
        local = compose_local(user_text, intent, hits, tool_result)
        for i in range(0, len(local), 18):
            piece = local[i:i + 18]
            answer_parts.append(piece)
            emit({"type": "delta", "text": piece})

    answer = "".join(answer_parts).strip()
    if cached:
        engine_name = "回答缓存（省一次 API 调用）"
    elif llm_used:
        engine_name = "DeepSeek " + config.LLM["model"]
    else:
        engine_name = "本地受控模板引擎"
    node_done("generate", "生成引擎：%s%s" % (
        engine_name, "（已自动降级）" if llm_error else ""))

    # ---- N6 合规审核
    node_start("compliance")
    checks = []
    if "出处" not in answer and "依据" not in answer and hits:
        src = "；".join("《%s》%s" % (h["doc_title"], h["section"]) for h in hits[:2])
        answer += "\n\n【资料出处】" + src
        checks.append("补标注来源")
    bad = [w for w in ("铜丝代替", "铁丝代替", "短接漏保", "拆配电箱") if w in answer]
    unsafe_ctx = [w for w in bad if not re.search(w + r"[^。]{0,20}(不要|不能|严禁|禁止|不得)", answer)]
    if unsafe_ctx:
        answer += "\n\n【安全提醒】涉及配电箱、熔丝、漏电保护器的操作请交给持证电工，切勿自行带电作业。"
        checks.append("拦截危险建议")
    if intent != "emergency":
        answer += "\n\n" + config.COMPLIANCE["disclaimer"]
        checks.append("追加免责声明")
    checks.append("来源标注完整")
    node_done("compliance", "合规审核通过：%s" % "、".join(checks))

    cards = []
    if need_emergency:
        cards.append({
            "type": "emergency",
            "title": "这是紧急情况，请按顺序做",
            "items": ["切断电源或拔掉插头，绝不用手直接拉人",
                      "拨打 120（火警 119）",
                      "无呼吸立即胸外按压：30:2，深度 5～6 cm，频率 100～120 次/分"],
            "phones": config.COMPLIANCE["emergency_numbers"][:3],
        })
    if need_fraud:
        cards.append({
            "type": "fraud",
            "title": "先挂断，再用官方号码自己核实",
            "items": ["不点链接、不扫码、不开屏幕共享",
                      "不向任何人提供验证码、银行卡号、密码",
                      "自己拨 95598 查电费，被骗就打 110 / 96110"],
            "phones": [config.COMPLIANCE["emergency_numbers"][3], config.COMPLIANCE["emergency_numbers"][4]],
        })

    return {
        "answer": answer,
        "intent": intent,
        "intent_label": INTENT_LABEL.get(intent, intent),
        "slots": {k: v for k, v in slots.items() if k != "text"},
        "sources": [{
            "doc_title": h["doc_title"],
            "section": h["section"],
            "doc_file": h["doc_file"],
            "score": h["score"],
            "snippet": h["snippet"],
        } for h in hits],
        "tool": tool_result,
        "cards": cards,
        "node_trace": trace,
        "engine": engine_name,
        "cached": cached,
        "llm_error": llm_error,
        "notice": None if config.llm_ready() else "当前未配置大模型 Key，正在使用本地受控模板引擎回答。",
    }
