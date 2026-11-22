# -*- coding: utf-8 -*-
"""
一键自检：跑一组固定用例，检查「意图识别 / 知识库检索 / 插件调度」三层是否正确。
不调用大模型（生成层用本地模板），因此结果稳定、可重复，适合作功能测试取证。
"""
import flow
import kb as kbmod
import tools as toolmod

CASES = [
    # id, 问题, 期望意图, 期望工具, 期望知识库文档关键字
    ("T01", "我家跳闸了，怎么分不清是过载还是漏电？", "trip", "diagnose_trip", "配电"),
    ("T02", "一用电暖器就跳闸，是过载吗？", "trip", "diagnose_trip", "配电"),
    ("T03", "有人触电了，我应该怎么做？", "emergency", None, "触电急救"),
    ("T04", "刚才有人打电话说我欠费386元下午停电，让我转账", "fraud", "fraud_check", "诈骗"),
    ("T05", "自称供电所的人上门收换表费，是真的吗？", "fraud", "fraud_check", "诈骗"),
    ("T06", "电暖器2000瓦一天开6小时，一个月电费多少？", "bill", "calc_bill", "电价"),
    ("T07", "普通10安插座能插2500瓦的电暖器吗？", "load", "check_circuit", "载流量"),
    ("T08", "2.5平方的铜线能带多大功率？", "load", "check_circuit", "载流量"),
    ("T09", "买电暖器要看哪些标志才安全？", "safety", "appliance_info", "家电"),
    ("T10", "家里电热毯折叠着用有没有危险？", "safety", "appliance_info", "家电"),
    ("T11", "漏电保护器上的试验按钮要多久按一次？", "other", None, "配电"),
    # T12 未给出功率/电量，按设计不触发计算插件，由知识库直接回答"电费为何上涨"
    ("T12", "电费突然比上个月高了一倍是怎么回事？", "bill", None, "电价"),
]


def run_suite(only=None):
    kb = kbmod.get_kb()
    rows = []
    n_intent = n_retr = n_tool = 0
    n_tool_cases = 0
    for cid, q, exp_intent, exp_tool, kb_kw in CASES:
        if only and cid not in only:
            continue
        res = flow.run(q, use_llm=False)
        intent_ok = (res["intent"] == exp_intent)
        src_text = "".join(s["doc_title"] for s in res["sources"])
        retr_ok = bool(res["sources"]) and (kb_kw in src_text)
        actual_tool = (res["tool"] or {}).get("tool")
        if exp_tool:
            n_tool_cases += 1
            tool_ok = (actual_tool == exp_tool)
            n_tool += 1 if tool_ok else 0
        else:
            tool_ok = True
        n_intent += 1 if intent_ok else 0
        n_retr += 1 if retr_ok else 0
        rows.append({
            "id": cid, "q": q,
            "intent": res["intent_label"], "intent_ok": intent_ok,
            "top_doc": res["sources"][0]["doc_title"] if res["sources"] else "—",
            "top_score": res["sources"][0]["score"] if res["sources"] else 0,
            "retr_ok": retr_ok,
            "tool": actual_tool or "—", "tool_ok": tool_ok,
            "exp_intent": exp_intent, "exp_tool": exp_tool or "—",
        })
    total = len(rows)
    return {
        "total": total,
        "intent_rate": round(n_intent * 100.0 / total, 1) if total else 0,
        "retrieval_rate": round(n_retr * 100.0 / total, 1) if total else 0,
        "tool_rate": round(n_tool * 100.0 / max(n_tool_cases, 1), 1),
        "passed": sum(1 for r in rows if r["intent_ok"] and r["retr_ok"] and r["tool_ok"]),
        "rows": rows,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run_suite(), ensure_ascii=False, indent=1)[:3000])
