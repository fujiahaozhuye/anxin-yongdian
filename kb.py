# -*- coding: utf-8 -*-
"""
知识库：加载 / 切片 / 中文 BM25 检索
------------------------------------------------------------
设计说明（使用说明书中"知识库切片方式与召回设置"一节的实现依据）：

1. 切片：按 Markdown 二级标题（##）切分章节；单章节超过 chunk_max_chars 时，
   按自然段二次切分并保留 chunk_overlap 字重叠，避免答案被切断。
2. 分词：中文无空格，采用「单字 + 相邻二元组（bigram）」混合切分，
   西文与数字按整词切分。bigram 保证专业术语（如"漏电保护器"）的区分度，
   单字保证口语提问（如"跳闸"）也能召回。
3. 召回：BM25（k1=1.5, b=0.75）+ 口语同义词查询扩展。
   分数低于 min_score 视为未命中，转由工具或通用话术兜底，避免"硬凑答案"。
"""
import os
import re
import math
from collections import Counter

import config

CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
SEG_RE = re.compile(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+")
SENT_SPLIT_RE = re.compile(r"[。！？；\n]")

# 口语 → 规范术语 的查询扩展词典。
# 目的：长辈说的"跳闸""没电了""闸刀"，要能召回"剩余电流动作保护器""断路器"章节。
SYNONYMS = {
    "跳闸": ["断路器", "漏电保护", "动作", "脱扣", "停电"],
    "跳了": ["跳闸", "断路器", "漏电保护"],
    "没电": ["停电", "断电", "断电故障"],
    "断电": ["停电", "跳闸"],
    "闸刀": ["开关", "断路器"],
    "闸": ["开关", "断路器"],
    "空开": ["空气开关", "断路器", "微型断路器"],
    "漏保": ["漏电保护器", "剩余电流动作保护", "RCD"],
    "漏电": ["剩余电流", "漏电保护", "电击"],
    "插排": ["插座", "排插", "拖线板", "插线板"],
    "排插": ["插座", "插排"],
    "插线板": ["插座", "插排"],
    "拖线板": ["插座", "插排"],
    "插座": ["插头", "额定电流"],
    "电线": ["导线", "载流量", "截面"],
    "线": ["导线", "载流量"],
    "几平方": ["截面", "mm²", "导线"],
    "粗": ["截面", "载流量"],
    "烧": ["过热", "短路", "火灾"],
    "糊味": ["焦", "过热", "接触不良"],
    "发烫": ["过热", "温升"],
    "电费": ["阶梯电价", "电价", "用电量", "千瓦时"],
    "多少钱": ["电价", "电费", "核算"],
    "几度电": ["千瓦时", "用电量", "功率"],
    "涨价": ["阶梯电价", "加价"],
    "暖器": ["电暖器", "功率", "过载"],
    "取暖": ["电暖器", "电热毯", "功率"],
    "热水器": ["大功率", "独立回路", "漏电保护"],
    "空调": ["独立回路", "压缩机", "启动电流"],
    "微波炉": ["大功率", "输入功率"],
    "电磁炉": ["大功率"],
    "电热毯": ["折叠", "受潮", "漏电"],
    "买": ["选购", "CCC", "能效标识"],
    "挑": ["选购", "能效", "认证"],
    "骗": ["电信网络诈骗", "反诈", "涉电诈骗"],
    "诈骗": ["电信网络诈骗", "反诈", "转账"],
    "电话": ["来电", "核实", "95598"],
    "催缴": ["欠费", "停电", "诈骗"],
    "欠费": ["电费", "催缴", "诈骗"],
    "停电通知": ["停电", "诈骗"],
    "上门": ["上门服务", "工牌", "核实"],
    "修": ["检修", "持证电工"],
    "触电": ["电击", "急救", "断电"],
    "电到": ["触电", "电击"],
    "电死": ["触电", "电击", "急救"],
    "着火": ["电气火灾", "灭火", "断电"],
    "起火": ["电气火灾", "灭火"],
    "灭火": ["干粉灭火器", "断电"],
    "接地": ["PE线", "等电位", "保护接地"],
    "地线": ["PE线", "接地"],
    "三孔": ["接地", "PE线"],
    "老房子": ["老旧线路", "铝芯", "绝缘老化"],
    "安全": ["电击防护", "安全用电"],
    "怎么办": ["处置", "步骤"],
    "急救": ["心肺复苏", "120", "触电急救"],
}


def tokenize(text):
    """中文单字 + 二元组 + 西文整词。"""
    text = (text or "").lower()
    tokens = []
    for seg in SEG_RE.findall(text):
        if CJK_RE.fullmatch(seg):
            tokens.extend(seg)                                   # 单字
            if len(seg) >= 2:
                tokens.extend(seg[i:i + 2] for i in range(len(seg) - 1))  # 二元组
        else:
            tokens.append(seg)
    return tokens


def expand_query(query):
    """口语同义词扩展，只作用于查询侧，不污染文档索引。"""
    extra = []
    for key, words in SYNONYMS.items():
        if key in query:
            extra.extend(words)
    if not extra:
        return query
    return query + " " + " ".join(extra)


class Chunk(object):
    __slots__ = ("cid", "doc_title", "doc_file", "section", "text", "order", "length")

    def __init__(self, cid, doc_title, doc_file, section, text, order):
        self.cid = cid
        self.doc_title = doc_title
        self.doc_file = doc_file
        self.section = section
        self.text = text
        self.order = order
        self.length = len(text)

    def as_dict(self):
        return {
            "cid": self.cid,
            "doc_title": self.doc_title,
            "doc_file": self.doc_file,
            "section": self.section,
            "length": self.length,
        }


class KnowledgeBase(object):
    def __init__(self, data_dir=None, retrieval=None):
        self.data_dir = data_dir or config.DATA_DIR
        self.cfg = dict(config.RETRIEVAL)
        if retrieval:
            self.cfg.update(retrieval)
        self.docs = []      # [{file, title, chars, chunks}]
        self.chunks = []    # [Chunk]
        self.tf = []        # [Counter]
        self.df = Counter()
        self.avgdl = 1.0
        self.postings = {}  # token -> [chunk index]
        self.load()

    # ---------------- 加载与切片 ----------------
    def load(self):
        self.docs, self.chunks, self.tf, self.df = [], [], [], Counter()
        files = sorted(f for f in os.listdir(self.data_dir) if f.lower().endswith(".md"))
        order = 0
        for fn in files:
            path = os.path.join(self.data_dir, fn)
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            title = self._doc_title(raw, fn)
            body = self._strip_quote(raw)
            pieces = self._split_sections(body, fn, title)
            start = len(self.chunks)
            for section, text in pieces:
                order += 1
                self.chunks.append(
                    Chunk("D%d-C%d" % (len(self.docs) + 1, order), title, fn,
                          section, text, order)
                )
            self.docs.append({
                "file": fn,
                "title": title,
                "chars": len(raw),
                "chunks": len(self.chunks) - start,
                "sections": [s for s, _ in pieces],
            })

        for ch in self.chunks:
            counter = Counter(tokenize(ch.text))
            self.tf.append(counter)
            for token in counter:
                self.df[token] += 1
                self.postings.setdefault(token, []).append(len(self.tf) - 1)
        N = max(len(self.chunks), 1)
        self.avgdl = sum(len(c.text) for c in self.chunks) / float(N) if self.chunks else 1.0
        return self

    @staticmethod
    def _doc_title(raw, fn):
        m = re.search(r"^#\s+(.+)$", raw, re.M)
        return m.group(1).strip() if m else os.path.splitext(fn)[0]

    @staticmethod
    def _strip_quote(raw):
        """去掉开头的引用块（出处说明），避免出处文字干扰检索结果。"""
        lines = raw.splitlines()
        out = []
        for ln in lines:
            if ln.startswith(">"):
                continue
            out.append(ln)
        return "\n".join(out).strip()

    def _split_sections(self, body, fn, title):
        # 去掉首行一级标题，避免生成只含标题的碎片切片
        body = re.sub(r"^\s*#\s+.+?\n", "", body, count=1).strip()
        parts = re.split(r"^##\s+", body, flags=re.M)
        pieces = []
        for idx, seg in enumerate(parts):
            seg = seg.strip()
            if not seg:
                continue
            lines = seg.split("\n", 1)
            section = lines[0].strip() if len(lines) > 1 else "概述"
            content = lines[1].strip() if len(lines) > 1 else seg
            if not content:
                continue
            for sub in self._split_long(content):
                pieces.append((section, sub))
        if not pieces:
            pieces = [("全文", body)]
        return pieces

    def _split_long(self, content):
        limit = int(self.cfg["chunk_max_chars"])
        overlap = int(self.cfg["chunk_overlap"])
        if len(content) <= limit:
            return [content]
        paras = [p.strip() for p in content.split("\n\n") if p.strip()]
        out, buf = [], ""
        for p in paras:
            if len(buf) + len(p) + 2 <= limit:
                buf = (buf + "\n\n" + p).strip()
            else:
                if buf:
                    out.append(buf)
                    tail = buf[-overlap:] if overlap else ""
                    buf = (tail + "\n\n" + p).strip()
                else:
                    out.append(p[:limit])
                    buf = p[limit - overlap:]
        if buf:
            out.append(buf)
        return out

    # ---------------- 检索 ----------------
    def _idf(self, token):
        N = max(len(self.chunks), 1)
        df = self.df.get(token, 0)
        return math.log(1.0 + (N - df + 0.5) / (df + 0.5))

    def search(self, query, top_k=None):
        top_k = int(top_k or self.cfg["top_k"])
        expanded = expand_query(query)
        q_tokens = set(tokenize(expanded))
        if not q_tokens or not self.chunks:
            return []
        cand = set()
        for t in q_tokens:
            cand.update(self.postings.get(t, []))
        k1 = float(self.cfg["bm25_k1"])
        b = float(self.cfg["bm25_b"])
        scored = []
        for i in cand:
            counter = self.tf[i]
            dl = len(self.chunks[i].text)
            score = 0.0
            for t in q_tokens:
                tf = counter.get(t, 0)
                if not tf:
                    continue
                idf = self._idf(t)
                denom = tf + k1 * (1 - b + b * dl / self.avgdl)
                score += idf * tf * (k1 + 1) / denom
            if score > 0:
                # 归一化：除以查询词数，便于用统一阈值判断是否命中
                scored.append((score / math.sqrt(len(q_tokens)), i))
        scored.sort(reverse=True)
        results = []
        for norm_score, i in scored[:top_k]:
            ch = self.chunks[i]
            results.append({
                "cid": ch.cid,
                "doc_title": ch.doc_title,
                "doc_file": ch.doc_file,
                "section": ch.section,
                "score": round(norm_score, 4),
                "text": ch.text,
                "snippet": self.best_sentence(ch.text, q_tokens, 160),
            })
        return results

    def hit(self, query, top_k=None):
        """与 search 相同，但附加是否达到命中阈值的判定。"""
        res = self.search(query, top_k)
        thr = float(self.cfg["min_score"])
        return {"query": query, "threshold": thr, "hit": bool(res) and res[0]["score"] >= thr,
                "results": res}

    @staticmethod
    def best_sentence(text, q_tokens, limit=160):
        """抽取与查询重合度最高的一句，作为引用片段（去掉 Markdown 标记与表格行）。"""
        cleaned = re.sub(r"\*\*|^#+\s*|^>\s*", "", text, flags=re.M)
        parts = []
        for seg in SENT_SPLIT_RE.split(cleaned):
            seg = seg.strip()
            if len(seg) < 8:
                continue
            if seg.startswith("|") or set(seg) <= set("|-: "):
                continue          # 跳过表格行与分隔线，避免引用片段里出现"|...|"竖线
            parts.append(seg)
        if not parts:
            return re.sub(r"\*\*", "", text)[:limit]
        scored = []
        for s in parts:
            st = set(tokenize(s))
            scored.append((len(st & q_tokens), -len(s), s))
        scored.sort(reverse=True)
        best = scored[0][2]
        return best[:limit] + ("…" if len(best) > limit else "")

    # ---------------- 统计 ----------------
    def stats(self):
        return {
            "docs": len(self.docs),
            "chunks": len(self.chunks),
            "chars": sum(d["chars"] for d in self.docs),
            "avg_chunk": round(self.avgdl, 1),
            "vocab": len(self.df),
            "settings": {
                "chunk_max_chars": self.cfg["chunk_max_chars"],
                "chunk_overlap": self.cfg["chunk_overlap"],
                "top_k": self.cfg["top_k"],
                "min_score": self.cfg["min_score"],
                "bm25": "k1=%.2f, b=%.2f" % (float(self.cfg["bm25_k1"]), float(self.cfg["bm25_b"])),
            },
        }


_KB = None


def get_kb():
    global _KB
    if _KB is None:
        _KB = KnowledgeBase()
    return _KB


if __name__ == "__main__":
    kb = get_kb()
    print(kb.stats())
    for q in ["家里跳闸了怎么办", "电暖器费电吗", "接到催缴电费的电话"]:
        r = kb.hit(q)
        print("\nQ:", q, "命中:", r["hit"])
        for x in r["results"][:2]:
            print("  %.3f %s / %s" % (x["score"], x["doc_title"][:18], x["section"]))
