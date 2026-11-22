# 安心用电 · 长辈居家用电安全与电费防诈助手

> 电气工程及其自动化专业 · 付家豪（202627020323）
> 《人工智能通识》期末个人项目作业 —— 应用类作品

一个面向长辈（父母、祖辈）的居家用电安全与电费防诈 Web 应用。围绕长辈真实遇到的四件事：
**跳闸不知怎么办、电费看不懂、接到"催缴停电"诈骗电话、买电器怕不安全**。

- 前端：原生 HTML / CSS / JavaScript（零构建）
- 后端：Python 3 标准库（零 pip 依赖）
- 大模型：DeepSeek `deepseek-chat`（未配置 Key 时自动降级为本地受控模板引擎）
- 知识库：6 篇本专业文档 / 31 个切片 / 约 1.28 万字，BM25 检索
- 工作流：6 个节点（含知识库检索节点），5 个自建插件

---

## 一、30 秒跑起来

```bash
cd anxin-yongdian
python app.py
```

浏览器打开 <http://127.0.0.1:8000> 即可。**不需要 `pip install` 任何东西**，
只需要 Python 3.8+（自带 Pillow 用于导出二维码 PNG，若缺失二维码接口会提示，其余功能不受影响）。

想让大模型参与回答，二选一：

```bash
# 方式一：环境变量（推荐，部署时也用这个）
set DEEPSEEK_API_KEY=sk-xxxxxxxx        # Windows
export DEEPSEEK_API_KEY=sk-xxxxxxxx     # macOS / Linux

# 方式二：写进 config.json 的 llm.api_key
```

不配置也能完整演示：检索、插件计算、工作流、界面、历史记录全部可用，
只有"答案生成"改用本地受控模板引擎（规则化组织，非大模型生成）。

---

## 二、目录结构

```
anxin-yongdian/
├── app.py              # HTTP 服务与全部路由（纯标准库）
├── config.py           # 配置：大模型、电价档位、检索参数、免责声明
├── config.json         # 本地私密配置（含 API Key，勿公开）
├── kb.py               # 知识库加载 / 切片 / 中文 BM25 检索
├── flow.py             # 6 节点工作流引擎（护栏→意图→检索→插件→生成→合规）
├── tools.py            # 5 个自建插件（电费/载流量/跳闸/反诈/功率）
├── llm.py              # DeepSeek 流式客户端 + 失败降级
├── qrcode_gen.py       # 纯 Python 二维码生成器（ISO/IEC 18004，版本1~10）
├── selftest.py         # 12 条用例的一键自检
├── shot.py             # 用 CDP 驱动浏览器截图（取证用，非运行必需）
├── data/
│   ├── kb/*.md         # 6 篇知识库文档（每篇标注标准号与出处）
│   ├── sessions.json   # 会话历史（可清空）
│   └── access.log      # 联调日志
└── web/
    ├── index.html      # 单页应用：对话 / 工作流 / 知识库 / 自检 / 我的
    ├── style.css       # 适老化样式（大字、大按钮、高对比）
    └── app.js          # SSE 流式接收、节点进度、语音朗读与语音输入
```

---

## 三、接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 应用主页面 |
| POST | `/api/chat` | 对话（SSE 流式：节点进度 + 逐字输出 + 最终结构化结果） |
| GET | `/api/config` | 前端可见配置（**不含任何密钥**） |
| GET | `/api/kb` | 知识库文档清单与统计 |
| POST | `/api/kb/search` | 检索测试（返回切片、BM25 分数、出处） |
| GET | `/api/workflow` | 工作流节点与插件清单 |
| GET | `/api/selftest` | 一键自检（12 条用例） |
| GET | `/api/history?sid=` | 会话历史 |
| POST | `/api/history/clear` | 清空会话 |
| GET | `/api/logs` | 最近联调日志 |
| GET | `/api/qr?data=&scale=` | 生成二维码 PNG |
| GET | `/qr` | 二维码展示页（手机扫码入口） |
| GET | `/api/health` | 健康检查 |

---

## 四、零成本部署（拿公网链接）

推荐 **Render**（免费实例即可，无需信用卡）：

1. 把 `anxin-yongdian` 整个目录推到一个 GitHub 仓库（**先把 `config.json` 里的 api_key 删掉**）。
2. 登录 <https://render.com> → **New** → **Web Service** → 选择该仓库。
3. 填写：
   - **Runtime**：`Python 3`
   - **Build Command**：`pip install pillow`（或留空，见下）
   - **Start Command**：`python app.py`
4. 在 **Environment** 里加一条：
   - Key：`DEEPSEEK_API_KEY`
   - Value：你的 Key
5. 完成后会拿到形如 `https://anxin-yongdian.onrender.com` 的地址。

> 仓库里已带 `render.yaml`，也可以在 Render 里选 **Blueprint** 一键读配置。
> 免费实例首次访问会冷启动约 30 秒，属正常现象。

拿到公网地址后，把说明书里的链接与二维码换成新地址：

```bash
cd deliverable/说明书源码
python make_qr.py "https://你的域名"
python run_build.py "https://你的域名"     # 重新生成说明书 DOCX + PDF
```

---

## 五、自检与测试

```bash
# 命令行跑 12 条用例（意图 / 检索 / 插件三层）
python selftest.py

# 或在浏览器里点「自检」页签的「开始自检」
```

自检强制走本地模板引擎，因此结果可重复：当前 12/12 通过，
意图识别 100%、知识库召回 100%、插件调度 100%。

---

## 六、安全与隐私

- 不收集姓名、住址、身份证号、银行卡号、手机号；无登录、无注册。
- 对话只写在本机 `data/sessions.json`，可在「我的」页一键清空。
- 联调日志只记录提问的前 40 个字。
- 部署时 Key 用环境变量注入，不写进代码、不进仓库。
- 无任何第三方统计、埋点或广告 SDK。

**注意**：`config.json` 内含私密 Key，请不要公开或提交到代码仓库。
若不慎泄露，请到 <https://platform.deepseek.com> 吊销并重新生成。

---

## 七、成本控制（省钱模式）

调用大模型是要花钱的，这个项目按"能不算就不算"来设计，一共五道省钱闸门：

| 闸门 | 做法 | 效果 |
|---|---|---|
| ① 回答缓存 | 同一个问题（去掉标点后相同）且检索结果一致时，24 小时内直接复用上次回答 | 课堂演示反复问同一句，**只花第一次的钱** |
| ② 本地引擎兜底 | 无 Key / 超时 / 限流时走本地受控模板引擎 | 这些情况**完全不产生费用** |
| ③ 输出限长 | `max_tokens` = 700，提示词里也限 450 字 | 输出费按比例下降 |
| ④ 上下文瘦身 | 每条检索资料截断 700 字，历史只带最近 2 轮 | 输入 token 明显减少 |
| ⑤ 零成本开关 | `config.json` 里把 `"enable"` 设为 `false` | 整个应用**零 API 费用**，功能只剩检索与计算 |

**注意**：说明书生成、12 条自检用例、33 次排版测页码**全部不调用大模型**，
所以反复重生成文档不会产生任何 API 费用。

估算参考（按 deepseek-chat 官方价，输入 ¥2／百万 tokens、输出 ¥8／百万 tokens）：
一次典型问答约 2000 输入 tokens + 500 输出 tokens ≈ **¥0.008／次**。
也就是说，一场 30 分钟的课堂演示就算问 40 个不同问题，成本约 **3 毛钱**。

---

## 八、免责声明

本应用提供的是通用用电安全常识与工程估算参考，
**不替代持证电工的现场检查与供电企业的正式答复**。
涉及资金与个人信息，请一律走官方渠道：网上国网 APP、95598、110、96110。
