# -*- coding: utf-8 -*-
"""
插件（工具）层
------------------------------------------------------------
作业要求"至少接入 1 个插件"，本应用自建 5 个电气工程专用工具，
由工作流的「插件工具调用」节点按意图与槽位自动调度：

  calc_bill      阶梯电价电费测算
  check_circuit  大功率电器—插座—导线—断路器 匹配校核
  diagnose_trip  跳闸故障分类诊断
  fraud_check    涉电类诈骗话术风险核验
  appliance_info 家电功率与安全提示查询

数值依据见 data/kb 下知识库文档与各工具内的 source 字段。
"""
import re

import config


# 铜芯 PVC 绝缘导线穿管敷设允许载流量（A，环境 30 ℃，参考 GB/T 16895.15—2002 载流量表并按穿管散热折减）
AMPACITY_TUBE = [
    (1.5, 13), (2.5, 18), (4, 24), (6, 31), (10, 43), (16, 57),
]

APPLIANCE_DB = {
    "电冰箱": (100, 250, "压缩机启动瞬时电流约为额定的 3～5 倍，须独立插座"),
    "冰箱": (100, 250, "压缩机启动瞬时电流约为额定的 3～5 倍，须独立插座"),
    "电视": (80, 200, "待机仍耗电，建议睡前关掉电源开关"),
    "液晶电视": (80, 200, "待机仍耗电，建议睡前关掉电源开关"),
    "洗衣机": (300, 500, "带烘干功能可达 2000 W"),
    "电饭煲": (500, 900, "保温状态仅几十瓦"),
    "微波炉": (1000, 1200, "看铭牌「输入功率」，不是「微波输出功率」"),
    "电磁炉": (1800, 2200, "属大功率电器，必须直插墙插或专线"),
    "电水壶": (1500, 1800, "烧水时间短但电流大，须直插墙插"),
    "电吹风": (1200, 2200, "高挡位接近 10 A，不要在卫生间插排上使用"),
    "电暖器": (1500, 2500, "长时间连续运行，最易造成线路过热"),
    "油汀": (1500, 2500, "长时间连续运行，最易造成线路过热"),
    "暖风机": (1500, 2000, "长时间连续运行，最易造成线路过热"),
    "电热毯": (60, 150, "小功率，但受潮后极易漏电，不得折叠使用"),
    "电热水器": (2000, 3000, "必须独立回路 + 30 mA 漏电保护"),
    "热水器": (2000, 3000, "必须独立回路 + 30 mA 漏电保护"),
    "空调": (1100, 2500, "制冷 1100 W 左右，制热含电辅热 2000～2500 W，须独立回路"),
    "抽油烟机": (200, 300, ""),
    "电熨斗": (1000, 2000, "高功率且需人守候，用完必须拔插头"),
    "电烤箱": (1500, 2000, "属大功率电器"),
    "净水器": (30, 80, ""),
    "路由器": (5, 15, "常年通电，建议加独立开关"),
}

# 涉电类诈骗高风险特征词
FRAUD_HIGH = [
    "转账", "汇款", "扫码支付", "扫码缴费", "打钱", "把钱", "微信转账", "支付宝转账",
    "验证码", "银行卡号", "银行卡密码", "支付密码", "屏幕共享", "远程协助", "共享屏幕",
    "马上停电", "立即停电", "今天停电", "两小时", "两个小时内", "今天下午", "否则停",
    "换表费", "上门收费", "保证金", "手续费", "退款", "退税", "补贴", "返现",
    "点击链接", "点开链接", "短信链接", "加我微信", "加个微信", "私下", "内部渠道",
]
FRAUD_MEDIUM = [
    "欠费", "催缴", "电表异常", "电表坏", "电表故障", "免费检修", "免费安检", "免费上门",
    "供电公司", "电力公司", "营业厅", "客服", "工号", "停你家的电", "停电通知",
]
FRAUD_SAFE = [
    "95598", "网上国网", "营业厅查询", "官方", "已经核实", "自己查", "挂断", "报警", "110", "96110",
]


# ---------------------------------------------------------------- 通用
def _rows(*pairs):
    return [{"k": k, "v": v} for k, v in pairs]


def _fmt(x, nd=2):
    try:
        return ("%." + str(nd) + "f") % float(x)
    except Exception:
        return str(x)


def tier_breakdown(kwh):
    """阶梯电价分段累加。返回 (总电费, 明细列表)。"""
    tiers = config.TARIFF["tiers"]
    remaining, prev, total, detail = float(kwh), 0.0, 0.0, []
    for t in tiers:
        upper = t["upper"] if t["upper"] else float("inf")
        span = min(remaining, upper - prev)
        if span > 0:
            amount = span * float(t["price"])
            total += amount
            detail.append({
                "name": t["name"],
                "range": "%s～%s 度" % (_int(prev), "以上" if t["upper"] is None else _int(upper)),
                "kwh": round(span, 2),
                "price": float(t["price"]),
                "amount": round(amount, 2),
            })
        remaining -= span
        prev = upper
        if remaining <= 0:
            break
    return total, detail


def _int(x):
    try:
        return int(x)
    except Exception:
        return 0


def recommend_wire(current_a, factor=1.25):
    """按穿管载流量选最小可用截面。"""
    need = current_a * factor
    for mm2, amp in AMPACITY_TUBE:
        if amp >= need:
            return mm2, amp
    return AMPACITY_TUBE[-1][0], AMPACITY_TUBE[-1][1]


# ---------------------------------------------------------------- 工具 1：电费测算
def calc_bill(slots):
    power_w = slots.get("power_w")
    kwh = slots.get("kwh")
    hours = slots.get("hours")
    days = slots.get("days") or 30
    detail_source = ""

    if kwh is None:
        if power_w and hours:
            kwh = float(power_w) / 1000.0 * float(hours) * float(days)
            detail_source = "按 %s W × %s 小时/天 × %s 天 折算" % (
                _int(power_w), _fmt(hours, 1), _int(days))
        else:
            return None
    total, detail = tier_breakdown(kwh)
    avg = total / float(kwh) if kwh else 0
    rows = [{"k": "计算电量", "v": "%s 度（千瓦时）" % _fmt(kwh, 2)}]
    if detail_source:
        rows.append({"k": "折算依据", "v": detail_source})
    for d in detail:
        rows.append({"k": d["name"] + "（%s）" % d["range"],
                     "v": "%s 度 × %s 元 = %s 元" % (_fmt(d["kwh"], 2), _fmt(d["price"], 2), _fmt(d["amount"], 2))})
    rows.append({"k": "合计电费", "v": "%s 元" % _fmt(total, 2)})
    rows.append({"k": "平均单价", "v": "%s 元/度" % _fmt(avg, 3)})
    rows.append({"k": "电价依据", "v": config.TARIFF["source"]})
    tip = "以上为按示例电价的估算值，实际账单以「网上国网」APP 或供电营业厅为准。"
    if kwh and float(kwh) > 260:
        tip += " 本月电量已进入第三档，每多用一度都按最高档计价，建议错峰使用大功率电器。"
    elif kwh and float(kwh) > 180:
        tip += " 本月电量已进入第二档，超出 180 度的部分单价上升。"
    return {
        "tool": "calc_bill",
        "title": "阶梯电价电费测算",
        "ok": True,
        "rows": rows,
        "advice": tip,
        "source": "发改价格〔2011〕2617号；" + config.TARIFF["source"],
    }


# ---------------------------------------------------------------- 工具 2：载流量匹配
def check_circuit(slots):
    power_w = slots.get("power_w")
    wire_mm2 = slots.get("wire_mm2")

    # 反向查询：用户只知道导线截面，问"能带多大功率"
    if not power_w and wire_mm2:
        amp = AMPACITY_TUBE[-1][1]
        for mm2, a in AMPACITY_TUBE:
            if abs(mm2 - float(wire_mm2)) < 1e-6:
                amp = a
                break
        safe_w = amp * 220.0
        rec = max(0.6, 0.8)
        return {
            "tool": "check_circuit",
            "title": "导线载流量反查（能带多大功率）",
            "ok": True,
            "risk": False,
            "rows": [
                {"k": "导线截面", "v": "%s mm² 铜芯" % _fmt(wire_mm2, 1)},
                {"k": "穿管载流量", "v": "约 %s A（环境 30 ℃）" % _int(amp)},
                {"k": "理论可带功率", "v": "约 %s W（220 V，电阻性负载）" % _int(safe_w)},
                {"k": "建议长期使用", "v": "不超过 %s W，留 %d%% 裕量" % (
                    _int(safe_w * rec), int(round((1 - rec) * 100)))},
                {"k": "配断路器", "v": "C%s 左右（须 ≤ 导线载流量）" % (int(amp / 5 + 1) * 5)},
            ],
            "problems": [],
            "advice": "载流量随敷设方式、环境温度、并列回路数变化，穿管或多回路并行时要打折；"
                      "导线再粗，接头松动一样会发热起火，接线端子务必压紧。",
            "source": "GB/T 16895.15—2002 布线系统载流量；GB 50054—2011 导体选择与保护配合",
        }

    if not power_w:
        return None

    socket_a = slots.get("socket_a")
    wire_mm2 = slots.get("wire_mm2")
    breaker_a = slots.get("breaker_a")
    cos_phi = 0.85 if slots.get("motor") else 1.0

    current = float(power_w) / (220.0 * cos_phi)
    rows = [
        {"k": "负载功率", "v": "%s W" % _int(power_w)},
        {"k": "估算电流", "v": "%s A（220 V，功率因数 %s）" % (_fmt(current, 1), _fmt(cos_phi, 2))},
    ]
    problems, advice = [], []

    if socket_a:
        limit_w = float(socket_a) * 220.0
        rows.append({"k": "插头插座", "v": "%s A／额定约 %s W" % (_int(socket_a), _int(limit_w))})
        if float(power_w) > limit_w:
            problems.append("负载超过 %s A 插座的额定容量" % _int(socket_a))
            advice.append("必须改用 16 A 专用插座与独立回路，不得经插排或普通 10 A 插座供电。")
        else:
            advice.append("该电器插在 %s A 插座上功率不超标，但仍要确认不是与其它大功率电器共用插排。" % _int(socket_a))

    rec_mm2, rec_amp = recommend_wire(current)
    rows.append({"k": "建议导线截面", "v": "%s mm²（穿管载流量 %s A，留 1.25 倍裕量）" % (rec_mm2, _int(rec_amp))})

    if wire_mm2:
        amp = None
        for mm2, a in AMPACITY_TUBE:
            if abs(mm2 - float(wire_mm2)) < 1e-6:
                amp = a
                break
        if amp is None:
            amp = AMPACITY_TUBE[-1][1]
        rows.append({"k": "现有导线", "v": "%s mm²／穿管载流量约 %s A" % (_fmt(wire_mm2, 1), _int(amp))})
        if current > amp:
            problems.append("现有导线载流量不足（%s A < 计算电流 %s A）" % (_int(amp), _fmt(current, 1)))
            advice.append("导线会长期过热，绝缘老化后易引发电气火灾，必须更换为 %s mm² 及以上。" % rec_mm2)
        else:
            advice.append("现有导线截面满足该负载，继续使用时注意接头与端子是否发热。")

    if breaker_a:
        rows.append({"k": "断路器", "v": "C%s" % _int(breaker_a)})
        if current > float(breaker_a) * 1.05:
            problems.append("断路器额定电流小于负载计算电流，会频繁过载跳闸")
            advice.append("应换用不小于 C%s 的断路器，并同步确认导线截面足够。" % (int(current * 1.25 / 5 + 1) * 5))
        elif wire_mm2 and float(breaker_a) > recommend_wire(current)[1] * 1.45:
            problems.append("断路器额定值可能大于导线允许载流量，过载时保护可能不动作")
            advice.append("存在「开关没跳、导线先烧」的风险，应减小断路器额定值或加粗导线。")
        else:
            advice.append("断路器与负载、导线的配合基本合理（I_b ≤ I_n ≤ I_z）。")

    return {
        "tool": "check_circuit",
        "title": "大功率电器用电安全校核",
        "ok": True,            # 工具执行成功；problems 非空表示发现安全隐患
        "risk": bool(problems),
        "rows": rows,
        "problems": problems,
        "advice": " ".join(advice) if advice else "参数不足，建议补充插座电流与导线截面。",
        "source": "GB 50054—2011 导体选择与保护配合；GB/T 16895.15—2002 载流量；GB 2099.1—2021 插头插座",
    }


# ---------------------------------------------------------------- 工具 3：跳闸诊断
def diagnose_trip(slots):
    text = slots.get("text", "")
    signals = []
    if re.search(r"(一送|一合|马上|立刻|马上就|刚送).{0,4}(跳|掉)", text) or "送不上" in text or "推不上去" in text:
        signals.append("immediate")
    if re.search(r"(用.{0,6}(就|后|一会儿)|开.{0,4}(就|后)).{0,3}(跳|掉)", text):
        signals.append("after_load")
    if re.search(r"(糊|焦|臭|冒烟|火花|响)", text):
        signals.append("smell")
    if re.search(r"(漏保|漏电保护|试验按钮|T按钮)。{0,6}(按|跳)", text):
        signals.append("rcd_test")
    if re.search(r"(热水器|电热毯|电饭煲|电水壶|潮湿|卫生间|受潮)", text):
        signals.append("wet")
    if re.search(r"(空调|电暖器|油汀|电磁炉|同时|一起用)", text):
        signals.append("overload")
    if re.search(r"(整栋|整单元|整个小区|邻居家也没电|对面楼)", text):
        signals.append("outage")

    rows, causes, steps = [], [], []
    if "outage" in signals:
        rows.append({"k": "判断", "v": "邻居家也没电 → 属于外部停电，不是自家故障"})
        causes.append("供电侧停电或线路检修")
        steps = [
            "先看窗外邻居家是否有电，确认是否整栋停电；",
            "拨打 95598 或打开「网上国网」APP 查询停电公告与抢修进度；",
            "停电期间拔掉电暖器、电热水器等大功率电器插头，避免来电瞬间冲击；",
            "不要自行打开电表箱或攀爬电杆，等待供电人员处理。",
        ]
    else:
        if "smell" in signals:
            rows.append({"k": "判断", "v": "有焦糊味／冒烟／火花 → 高度疑似短路或接触不良发热"})
            causes.append("短路或接线端子接触不良")
            steps = [
                "立即断开总开关，不要反复强送；",
                "拔掉该回路上所有电器插头，闻到焦味时不要再合闸；",
                "请联系持证电工上门检查，必要时拨打 119；",
                "未经电工检查绝缘，不得恢复送电。",
            ]
        elif "immediate" in signals:
            rows.append({"k": "判断", "v": "一送就跳 → 优先怀疑漏电（漏保动作）或线路短路"})
            causes.append("回路存在漏电或短路")
            steps = [
                "先拔掉该回路所有电器插头，再合闸；能送上说明是电器漏电；",
                "逐个插回电器，插到哪只跳就是哪只的问题，该电器立即停用送修；",
                "拔完仍跳闸，说明是线路问题，须请持证电工查线，不得短接漏保；",
                "潮湿季节重点查卫生间插座、电热水器、电热毯。",
            ]
        elif "after_load" in signals or "overload" in signals:
            rows.append({"k": "判断", "v": "用一会儿才跳 → 多为过载，空开按额定电流动作"})
            causes.append("同一回路同时使用多件大功率电器，电流超过空开额定值")
            steps = [
                "确认这一路上有哪些电器在同时工作，把功率相加与插座容量比对；",
                "错开使用：电暖器、电磁炉、电水壶不要同时开；",
                "大功率电器改为独立回路或 16 A 专用插座，不要经插排；",
                "若必须扩容，请电工核算导线截面后再换断路器，不能只换大开关。",
            ]
        else:
            rows.append({"k": "判断", "v": "信息不足，先按通用排查法定位"})
            causes.append("过载／漏电／短路，需现场判定")
            steps = [
                "看是带试验按钮的那只开关跳（漏保，多为漏电），还是普通空开跳（多为过载／短路）；",
                "拔掉该回路所有电器再送电，能送上就是电器问题，送不上就是线路问题；",
                "每月按一次漏保试验按钮，按了不跳说明漏保已失效，需更换；",
                "任何情况下都不要用铜丝代替熔丝、不要短接漏保。",
            ]
        if "wet" in signals:
            rows.append({"k": "重点怀疑", "v": "涉水电器或潮湿场所（卫生间、热水器、电热毯受潮）漏电"})
        if "rcd_test" in signals:
            rows.append({"k": "重点怀疑", "v": "提及漏保试验按钮，注意判据：按下应立刻跳闸，不跳即失效"})

    return {
        "tool": "diagnose_trip",
        "title": "跳闸故障诊断",
        "ok": True,
        "rows": rows,
        "causes": causes,
        "steps": steps,
        "source": "GB/T 6829—2017 剩余电流动作保护电器；GB 50054—2011 低压配电设计规范；GB/T 16895.21—2011 电击防护",
    }


# ---------------------------------------------------------------- 工具 4：反诈核验
def fraud_check(slots):
    text = slots.get("text", "") or ""
    high = [w for w in FRAUD_HIGH if w in text]
    medium = [w for w in FRAUD_MEDIUM if w in text]
    safe = [w for w in FRAUD_SAFE if w in text]

    if high:
        level, label = 3, "高风险：极可能是诈骗"
    elif len(medium) >= 2:
        level, label = 2, "中高风险：需立即核实"
    elif medium:
        level, label = 1, "需谨慎：请先核实来源"
    elif safe:
        level, label = 0, "你已经在用正确的核实方式"
    else:
        level, label = 0, "未发现明显诈骗特征"

    rows = [{"k": "风险等级", "v": label}]
    if high:
        rows.append({"k": "命中高危话术", "v": "、".join(high[:6])})
    if medium:
        rows.append({"k": "命中可疑要素", "v": "、".join(medium[:6])})
    if safe:
        rows.append({"k": "已出现正确做法", "v": "、".join(safe[:5])})

    if level >= 2:
        steps = [
            "马上挂断电话，不要争辩、不要解释家里情况；",
            "不要点对方发来的任何链接，不要扫对方提供的二维码，不要开屏幕共享；",
            "用自己的电话拨 95598 核实是否真的欠费，或让子女打开「网上国网」看账单；",
            "已经转账或泄露了验证码，立即拨 110 报警，并联系银行冻结账户；",
            "拿不准就打 96110 反诈专线咨询。",
        ]
    elif level == 1:
        steps = [
            "先不要按对方说的操作，把电话挂掉；",
            "用自己记得的官方号码回拨核实：供电服务 95598；",
            "任何要求提供验证码、银行卡号、密码的，一律拒绝。",
        ]
    else:
        steps = [
            "电费只在「网上国网」APP、95598 指引的官方渠道、供电营业厅缴纳；",
            "供电企业不会要求向个人账户转账，不会索要验证码与密码。",
        ]

    return {
        "tool": "fraud_check",
        "title": "涉电诈骗风险核验",
        "ok": True,
        "level": level,
        "rows": rows,
        "steps": steps,
        "source": "《中华人民共和国反电信网络诈骗法》（2022）；国家反诈中心《防范电信网络诈骗宣传手册》；供电服务 95598 官方渠道",
    }


# ---------------------------------------------------------------- 工具 5：家电功率查询
def appliance_info(slots):
    text = slots.get("text", "") or ""
    for key, (lo, hi, note) in APPLIANCE_DB.items():
        if key in text:
            i1 = round(lo / 220.0, 1)
            i2 = round(hi / 220.0, 1)
            rows = _rows(
                ("电器", key),
                ("典型功率", "%d～%d W" % (lo, hi)),
                ("估算电流", "%s～%s A（220 V）" % (i1, i2)),
                ("每小时耗电", "%s～%s 度" % (_fmt(lo / 1000.0, 2), _fmt(hi / 1000.0, 2))),
                ("所需插座", "16 A 专用" if hi >= 2000 else ("10 A 插座即可" if hi < 1800 else "10 A 上限附近，建议专用")),
            )
            return {
                "tool": "appliance_info",
                "title": "%s 功率与用电信息" % key,
                "ok": True,
                "rows": rows,
                "advice": note or "注意插头与插座额定电流，不要与大功率电器共用插排。",
                "source": "GB 4706.1—2005 家用和类似用途电器的安全；家电铭牌标称值",
            }
    return None


# ---------------------------------------------------------------- 注册表
REGISTRY = {
    "calc_bill": calc_bill,
    "check_circuit": check_circuit,
    "diagnose_trip": diagnose_trip,
    "fraud_check": fraud_check,
    "appliance_info": appliance_info,
}


def call(name, slots):
    fn = REGISTRY.get(name)
    if not fn:
        return None
    try:
        return fn(slots or {})
    except Exception as e:
        return {"tool": name, "title": name, "ok": False,
                "rows": [{"k": "错误", "v": str(e)}], "advice": "工具执行异常，已回退到知识库回答。"}


def manifest():
    """给前端展示的插件清单。"""
    return [
        {"name": "calc_bill", "label": "阶梯电价电费测算", "desc": "按功率×时长或直接输入电量，分段累加算出电费"},
        {"name": "check_circuit", "label": "大功率电器安全校核", "desc": "校核插座额定电流、导线载流量与断路器配合"},
        {"name": "diagnose_trip", "label": "跳闸故障诊断", "desc": "区分过载／漏电／短路／外部停电四类原因"},
        {"name": "fraud_check", "label": "涉电诈骗风险核验", "desc": "按反诈法与国家反诈中心话术库判定风险等级"},
        {"name": "appliance_info", "label": "家电功率查询", "desc": "查常见家电典型功率、电流与插座要求"},
    ]
