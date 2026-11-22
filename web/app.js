/* 安心用电 · 前端交互
   要点：流式接收、工作流节点实时高亮、语音朗读、语音输入、来源可追溯 */

(function () {
  'use strict';

  var $ = function (s) { return document.querySelector(s); };
  var state = {
    sid: 's' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7),
    busy: false,
    voice: true,
    big: false,
    lastTrace: null,
    cfg: null
  };

  /* ---------------- 工具 ---------------- */
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function toast(msg, ms) {
    var t = document.querySelector('.toast');
    if (!t) { t = document.createElement('div'); t.className = 'toast'; document.body.appendChild(t); }
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(t._tm);
    t._tm = setTimeout(function () { t.classList.remove('show'); }, ms || 1800);
  }

  // 把【小标题】转成加粗引导句
  function rich(text) {
    var s = esc(text);
    s = s.replace(/【([^】]{1,20})】/g, '<span class="lead">$1</span>');
    s = s.replace(/\*\*([^*]{1,60})\*\*/g, '<b>$1</b>');
    return s;
  }

  function renderAnswer(text) {
    var lines = String(text || '').split(/\n+/).filter(function (l) { return l.trim(); });
    if (!lines.length) return '<p></p>';
    return lines.map(function (l) {
      return '<p>' + rich(l.trim()) + '</p>';
    }).join('');
  }

  /* ---------------- 朗读 ---------------- */
  function speak(text) {
    if (!state.voice || !window.speechSynthesis) return;
    try {
      window.speechSynthesis.cancel();
      var clean = String(text).replace(/【[^】]*】/g, '').replace(/\*\*/g, '')
        .replace(/https?:\/\/\S+/g, '').replace(/\s+/g, ' ');
      var u = new SpeechSynthesisUtterance(clean.slice(0, 900));
      u.lang = 'zh-CN';
      u.rate = 0.92;
      u.pitch = 1.0;
      window.speechSynthesis.speak(u);
    } catch (e) { /* 语音不可用不影响文字 */ }
  }

  /* ---------------- 标签页 ---------------- */
  function switchView(name) {
    document.querySelectorAll('.tab').forEach(function (b) {
      b.classList.toggle('active', b.dataset.view === name);
    });
    document.querySelectorAll('.view').forEach(function (v) {
      v.classList.toggle('active', v.id === 'view-' + name);
    });
    if (name === 'kb') { loadKB(); }
    if (name === 'workflow') { loadWorkflow(); }
    if (name === 'test') { /* 等待用户点开始自检 */ }
    if (name === 'mine') { loadMine(); }
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  document.getElementById('tabs').addEventListener('click', function (e) {
    var b = e.target.closest('.tab');
    if (b) switchView(b.dataset.view);
  });

  /* ---------------- 工作流进度条 ---------------- */
  var NODE_NAMES = ['安全护栏预检', '意图识别', '知识库检索', '插件工具调用', '答案生成', '合规审核'];

  function buildFlowbar() {
    var box = $('#flow-nodes');
    box.innerHTML = NODE_NAMES.map(function (n, i) {
      return '<span class="flow-chip" data-i="' + (i + 1) + '">' + (i + 1) + '. ' + n + '</span>';
    }).join('');
  }

  function flowReset() {
    document.querySelectorAll('.flow-chip').forEach(function (c) {
      c.classList.remove('run', 'ok');
    });
    $('#flow-tip').textContent = '正在处理…';
  }

  function flowSet(index, cls, tip) {
    var c = document.querySelector('.flow-chip[data-i="' + index + '"]');
    if (c) { c.classList.remove('run', 'ok'); c.classList.add(cls); }
    if (tip) $('#flow-tip').textContent = tip;
  }

  /* ---------------- 卡片渲染 ---------------- */
  function renderComplianceCards(cards) {
    if (!cards || !cards.length) return '';
    return cards.map(function (c) {
      var cls = c.type === 'fraud' ? 'fraud' : '';
      var phones = (c.phones || []).map(function (p) {
        return '<a href="tel:' + esc(p.number) + '">' + esc(p.name) + ' ' + esc(p.number) + '</a>';
      }).join('');
      return '<div class="alert-card ' + cls + '">' +
        '<h4>' + esc(c.title) + '</h4>' +
        '<ol>' + (c.items || []).map(function (i) { return '<li>' + rich(i) + '</li>'; }).join('') + '</ol>' +
        (phones ? '<div class="phone-row">' + phones + '</div>' : '') +
        '</div>';
    }).join('');
  }

  function renderToolCard(t) {
    if (!t || !t.rows || !t.rows.length) return '';
    var risk = (t.problems && t.problems.length) || t.level >= 2;
    var rows = t.rows.map(function (r) {
      return '<tr><td>' + esc(r.k) + '</td><td>' + rich(r.v) + '</td></tr>';
    }).join('');
    var note = '';
    if (t.problems && t.problems.length) {
      note = '<div class="tf-note">⚠ 发现的问题：' + t.problems.map(esc).join('；') + '</div>';
    }
    if (t.causes && t.causes.length) {
      note += '<div class="tf-note">可能原因：' + t.causes.map(esc).join('；') + '</div>';
    }
    if (t.advice) { note += '<div class="tf-note">' + rich(t.advice) + '</div>'; }
    if (t.source) { note += '<div class="tf-note">依据：' + esc(t.source) + '</div>'; }
    return '<div class="tool-card' + (risk ? ' risk' : '') + '">' +
      '<h4>🔧 ' + esc(t.title || '工具结果') + '</h4>' +
      '<table>' + rows + '</table>' + note + '</div>';
  }

  function renderSources(sources) {
    if (!sources || !sources.length) return '';
    var items = sources.map(function (s) {
      return '<div class="src-item"><b>《' + esc(s.doc_title) + '》</b> ' + esc(s.section) +
        ' <span class="score">BM25 ' + s.score + '</span>' +
        '<div class="snip">' + esc(s.snippet) + '</div></div>';
    }).join('');
    return '<details class="sources"><summary>参考资料（' + sources.length + ' 条，可展开看原文片段）</summary>' +
      items + '</details>';
  }

  /* ---------------- 消息 ---------------- */
  function addUser(text) {
    var el = document.createElement('div');
    el.className = 'msg user';
    el.innerHTML = '<div class="avatar">我</div><div class="bubble"><p>' + esc(text) + '</p></div>';
    $('#messages').appendChild(el);
    scrollBottom();
  }

  function addBot() {
    var el = document.createElement('div');
    el.className = 'msg bot';
    el.innerHTML = '<div class="avatar">安</div><div class="bubble typing" id="cur"><p></p></div>';
    $('#messages').appendChild(el);
    scrollBottom();
    return el.querySelector('#cur');
  }

  function scrollBottom() {
    setTimeout(function () { window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }); }, 30);
  }

  /* ---------------- 对话请求（SSE） ---------------- */
  function send(text) {
    if (state.busy) return;
    text = (text || '').trim();
    if (!text) return;

    state.busy = true;
    $('#btn-send').disabled = true;
    $('#welcome').hidden = true;
    $('#flowbar').hidden = false;
    flowReset();
    addUser(text);
    var bubble = addBot();
    var acc = '';
    var engine = '';

    fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sid: state.sid, text: text })
    }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      var reader = res.body.getReader();
      var decoder = new TextDecoder('utf-8');
      var buf = '';

      function pump() {
        return reader.read().then(function (r) {
          if (r.done) return finish();
          buf += decoder.decode(r.value, { stream: true });
          var parts = buf.split('\n\n');
          buf = parts.pop();
          parts.forEach(handleEvent);
          return pump();
        });
      }

      function handleEvent(raw) {
        var line = raw.trim();
        if (!line || line.indexOf('data:') !== 0) return;
        var ev;
        try { ev = JSON.parse(line.slice(5).trim()); } catch (e) { return; }

        if (ev.type === 'node') {
          if (ev.status === 'run') {
            flowSet(ev.index, 'run', '正在执行第 ' + ev.index + ' 步…');
          } else if (ev.status === 'ok') {
            flowSet(ev.index, 'ok', (ev.info && ev.info.detail) || '');
          }
        } else if (ev.type === 'generate_start') {
          engine = ev.engine || '';
          if (engine) $('#flow-tip').textContent = '正在生成回答（' + engine + '）…';
        } else if (ev.type === 'delta') {
          acc += ev.text;
          bubble.innerHTML = renderAnswer(acc);
          bubble.classList.add('typing');
          scrollBottom();
        } else if (ev.type === 'fallback') {
          toast('大模型暂不可用，已自动切换本地引擎');
        } else if (ev.type === 'final') {
          paintFinal(ev.payload, bubble);
          finish(ev.payload);
        } else if (ev.type === 'error') {
          bubble.innerHTML = '<p>抱歉，服务出了点问题：' + esc(ev.message || '未知错误') + '</p>';
          finish();
        }
      }

      function paintFinal(p, b) {
        b.classList.remove('typing');
        var html = renderAnswer(p.answer || acc);
        html += renderComplianceCards(p.cards);
        html += renderToolCard(p.tool);
        html += renderSources(p.sources);
        html += '<div class="disclaimer">意图：' + esc(p.intent_label || '') +
          ' ｜ 引擎：' + esc(p.engine || '') + '</div>';
        b.innerHTML = html;
        speak((p.answer || acc));
        scrollBottom();
      }

      function finish(payload) {
        state.busy = false;
        $('#btn-send').disabled = false;
        $('#flow-tip').textContent = '本次处理完成 ✓';
        if (payload && payload.node_trace) {
          state.lastTrace = payload;
          renderTrace(payload);
        }
        setTimeout(function () { $('#flowbar').hidden = true; }, 2600);
      }

      return pump();
    }).catch(function (err) {
      bubble.classList.remove('typing');
      bubble.innerHTML = '<p>网络或服务异常：' + esc(err.message) + '</p><p>请检查网络连接后重试。</p>';
      finish();
    });
  }

  /* ---------------- 发送入口 ---------------- */
  $('#btn-send').addEventListener('click', function () {
    var v = $('#input').value;
    $('#input').value = '';
    $('#input').style.height = 'auto';
    send(v);
  });

  $('#input').addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      var v = $('#input').value;
      $('#input').value = '';
      send(v);
    }
  });

  $('#input').addEventListener('input', function () {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 140) + 'px';
  });

  document.querySelectorAll('.quick').forEach(function (b) {
    b.addEventListener('click', function () { send(b.dataset.q); });
  });

  /* ---------------- 语音输入 ---------------- */
  var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  var recog = null, recording = false;

  $('#btn-mic').addEventListener('click', function () {
    if (!SR) {
      toast('当前浏览器不支持语音输入，请用 Chrome / Edge');
      return;
    }
    if (recording) { recog && recog.stop(); return; }
    recog = new SR();
    recog.lang = 'zh-CN';
    recog.interimResults = false;
    recog.maxAlternatives = 1;
    recog.onstart = function () {
      recording = true;
      $('#btn-mic').classList.add('rec');
      toast('请说话…');
    };
    recog.onresult = function (e) {
      var t = e.results[0][0].transcript;
      $('#input').value = ($('#input').value + ' ' + t).trim();
      $('#input').focus();
    };
    recog.onerror = function (e) {
      toast('语音识别失败：' + e.error);
    };
    recog.onend = function () {
      recording = false;
      $('#btn-mic').classList.remove('rec');
    };
    try { recog.start(); } catch (e) { toast('无法启动麦克风'); }
  });

  /* ---------------- 顶栏按钮 ---------------- */
  $('#btn-font').addEventListener('click', function () {
    state.big = !state.big;
    document.body.classList.toggle('big', state.big);
    this.classList.toggle('on', state.big);
  });

  $('#btn-voice').addEventListener('click', function () {
    state.voice = !state.voice;
    this.classList.toggle('on', state.voice);
    this.textContent = '朗读 ' + (state.voice ? '开' : '关');
    if (!state.voice && window.speechSynthesis) window.speechSynthesis.cancel();
  });

  /* ---------------- 工作流页 ---------------- */
  function loadWorkflow() {
    fetch('/api/workflow').then(function (r) { return r.json(); }).then(function (d) {
      $('#wf-nodes').innerHTML = d.nodes.map(function (n) {
        var tag = n.id === 'retrieve' ? '<span class="wf-tag">必备节点</span>' :
          (n.id === 'tool' ? '<span class="wf-tag">插件</span>' : '');
        return '<div class="wf-item"><div class="wf-idx">' + n.index + '</div>' +
          '<div><h3>' + esc(n.name) + tag + '</h3><p>' + esc(n.desc) + '</p></div></div>';
      }).join('');
      $('#plugin-list').innerHTML = d.plugins.map(function (p) {
        return '<div class="plugin"><h3>' + esc(p.label) + '</h3>' +
          '<code>' + esc(p.name) + '</code><p>' + esc(p.desc) + '</p></div>';
      }).join('');
    }).catch(function () {});
    if (state.lastTrace) renderTrace(state.lastTrace);

    fetch('/api/logs').then(function (r) { return r.json(); }).then(function (d) {
      var el = document.getElementById('wf-log');
      if (el) el.textContent = (d.logs || []).slice(-28).join('\n') || '暂无日志';
    }).catch(function () {});
  }

  /* ---------------- 自检页 ---------------- */
  function runSelftest() {
    document.getElementById('test-go').disabled = true;
    document.getElementById('test-summary').innerHTML = '<div class="kb-empty">正在跑 12 条用例…</div>';
    document.getElementById('test-table').innerHTML = '';
    fetch('/api/selftest').then(function (r) { return r.json(); }).then(function (d) {
      document.getElementById('test-go').disabled = false;
      document.getElementById('test-summary').innerHTML =
        '<div class="stat"><b>' + d.passed + '/' + d.total + '</b>用例全通过</div>' +
        '<div class="stat"><b>' + d.intent_rate + '%</b>意图识别准确率</div>' +
        '<div class="stat"><b>' + d.retrieval_rate + '%</b>知识库召回达标率</div>' +
        '<div class="stat"><b>' + d.tool_rate + '%</b>插件调度正确率</div>';
      var rows = d.rows.map(function (r) {
        function m(ok) { return ok ? '<span class="pass">✔</span>' : '<span class="fail">✘</span>'; }
        return '<tr><td>' + r.id + '</td><td class="qcell">' + esc(r.q) + '</td>' +
          '<td>' + m(r.intent_ok) + ' ' + esc(r.intent) + '</td>' +
          '<td>' + m(r.retr_ok) + ' ' + esc(r.top_doc) + ' <span class="sc">' + r.top_score + '</span></td>' +
          '<td>' + m(r.tool_ok) + ' ' + esc(r.tool) + '</td></tr>';
      }).join('');
      document.getElementById('test-table').innerHTML =
        '<table class="test-table"><thead><tr><th>编号</th><th>用例问题</th><th>意图</th>' +
        '<th>知识库首位召回</th><th>插件</th></tr></thead><tbody>' + rows + '</tbody></table>';
    }).catch(function () {
      document.getElementById('test-go').disabled = false;
      document.getElementById('test-summary').innerHTML = '<div class="kb-empty">自检服务异常</div>';
    });
  }
  var tg = document.getElementById('test-go');
  if (tg) tg.addEventListener('click', runSelftest);

  function renderTrace(p) {
    var lines = [];
    lines.push('用户问题 → ' + (p._q || '（见对话）'));
    lines.push('意图分类 → ' + (p.intent_label || ''));
    lines.push('生成引擎 → ' + (p.engine || ''));
    lines.push('插件工具 → ' + ((p.tool && p.tool.title) || '未调用'));
    lines.push('');
    (p.node_trace || []).forEach(function (t, i) {
      lines.push('[' + (i + 1) + '] ' + t.id + '  ✔ ' + t.detail);
    });
    lines.push('');
    lines.push('召回切片：');
    (p.sources || []).forEach(function (s) {
      lines.push('  · ' + s.doc_title + ' / ' + s.section + '  BM25=' + s.score);
    });
    $('#wf-trace').textContent = lines.join('\n');
  }

  /* ---------------- 知识库页 ---------------- */
  function loadKB() {
    fetch('/api/kb').then(function (r) { return r.json(); }).then(function (d) {
      var s = d.stats;
      $('#kb-stats').innerHTML =
        '<div class="stat"><b>' + s.docs + '</b>份专业文档</div>' +
        '<div class="stat"><b>' + s.chunks + '</b>个切片</div>' +
        '<div class="stat"><b>' + s.chars + '</b>字</div>' +
        '<div class="stat"><b>' + s.avg_chunk + '</b>平均切片长度</div>' +
        '<div class="stat"><b>' + s.vocab + '</b>词表规模</div>' +
        '<div class="stat"><b>' + s.settings.top_k + '</b>召回条数</div>';
      $('#kb-docs').innerHTML = d.docs.map(function (doc) {
        return '<div class="kb-doc"><h3>' + esc(doc.title) + '</h3>' +
          '<div class="meta">' + esc(doc.file) + ' ｜ ' + doc.chars + ' 字 ｜ 切成 ' + doc.chunks + ' 个切片</div>' +
          '<div class="secs">' + (doc.sections || []).map(function (x) {
            return '<span>' + esc(x) + '</span>';
          }).join('') + '</div></div>';
      }).join('');
    }).catch(function () {});
  }

  $('#kb-go').addEventListener('click', doKBSearch);
  $('#kb-q').addEventListener('keydown', function (e) { if (e.key === 'Enter') doKBSearch(); });

  function doKBSearch() {
    var q = $('#kb-q').value.trim();
    if (!q) { toast('先输一个问题'); return; }
    $('#kb-results').innerHTML = '<div class="kb-empty">检索中…</div>';
    fetch('/api/kb/search', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: q })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.results || !d.results.length) {
        $('#kb-results').innerHTML = '<div class="kb-empty">没有召回结果，换一种说法试试。</div>';
        return;
      }
      $('#kb-results').innerHTML = d.results.map(function (x, i) {
        return '<div class="kb-hit"><div class="top"><span>第 ' + (i + 1) + ' 名 · 《' +
          esc(x.doc_title) + '》' + esc(x.section) + '</span>' +
          '<span class="score-b">BM25 ' + x.score + '</span></div>' +
          '<div class="snip">' + esc(x.snippet) + '</div></div>';
      }).join('') + '<div class="kb-empty">判定阈值 ' + d.threshold +
        '，本次最高分 ' + (d.results[0] ? d.results[0].score : 0) +
        '，' + (d.hit ? '命中知识库 ✓' : '低于阈值 → 转工具或通用话术') + '</div>';
    }).catch(function () {
      $('#kb-results').innerHTML = '<div class="kb-empty">检索服务异常</div>';
    });
  }

  /* ---------------- 我的页 ---------------- */
  function loadMine() {
    fetch('/api/history?sid=' + encodeURIComponent(state.sid))
      .then(function (r) { return r.json(); }).then(function (d) {
        var list = (d.messages || []).filter(function (m) { return m.role === 'user'; });
        if (!list.length) {
          $('#history-list').innerHTML = '<div class="hist-item">还没有对话记录。</div>';
          return;
        }
        $('#history-list').innerHTML = list.slice().reverse().map(function (m) {
          return '<div class="hist-item"><div class="q">' + esc(m.content) + '</div>' +
            '<div class="t">' + esc(m.time || '') + '</div></div>';
        }).join('');
      }).catch(function () {});

    var url = location.origin + location.pathname;
    $('#mine-url').textContent = url;
    $('#qr-img').src = '/api/qr?scale=10&data=' + encodeURIComponent(url);
    $('#qr-note').textContent = url;
    fetch('/api/config').then(function (r) { return r.json(); }).then(function (c) {
      $('#mine-engine').textContent = c.llm_model;
    }).catch(function () {});
  }

  $('#btn-clear').addEventListener('click', function () {
    if (!confirm('确定清空本机对话历史吗？')) return;
    fetch('/api/history/clear', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sid: state.sid })
    }).then(function () {
      $('#messages').innerHTML = '';
      $('#welcome').hidden = false;
      state.sid = 's' + Date.now().toString(36);
      loadMine();
      toast('已清空');
    });
  });

  /* ---------------- 初始化 ---------------- */
  buildFlowbar();
  fetch('/api/config').then(function (r) { return r.json(); }).then(function (c) {
    state.cfg = c;
    var chip = $('#engine-chip');
    chip.textContent = c.llm_enabled ? ('已接入 ' + c.llm_model) : '本地知识引擎';
    $('#mine-engine').textContent = c.llm_model;
    document.title = c.app_name + ' · ' + c.subtitle;
  }).catch(function () {
    $('#engine-chip').textContent = '离线模式';
  });

  // 支持通过 URL 参数预置状态（便于分享、课堂演示与截图取证）
  //   ?q=问题        直接提问
  //   ?view=kb       打开指定标签页
  //   ?kbq=问题      在知识库页自动执行一次检索测试
  var params = new URLSearchParams(location.search);
  var preView = params.get('view');
  if (preView) { switchView(preView); }
  var preKb = params.get('kbq');
  if (preKb) {
    setTimeout(function () {
      switchView('kb');
      var el = document.getElementById('kb-q');
      if (el) { el.value = preKb; doKBSearch(); }
    }, 300);
  }
  if (params.get('selftest') === '1') {
    setTimeout(function () { switchView('test'); runSelftest(); }, 300);
  }
  var pre = params.get('q');
  if (pre) { setTimeout(function () { send(pre); }, 400); }

  window.__anxin = { send: send, switchView: switchView, state: state };
})();
