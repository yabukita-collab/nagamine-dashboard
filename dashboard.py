import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timezone, timedelta
from collections import Counter
import json
import html as html_mod
import os
import re
import anthropic

SCOPES = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = Credentials.from_service_account_file('key.json', scopes=SCOPES)
gc = gspread.authorize(creds)

sh = gc.open_by_key('13u7VGPuWBxNDy3nf4zqp29JxjO0k0DyxuUaBg_ILTes')
ws = sh.sheet1
rows = ws.get_all_records()

JST = timezone(timedelta(hours=9))
now_jst = datetime.now(JST)
now_str = now_jst.strftime('%Y年%m月%d日 %H:%M JST')
this_month = now_jst.strftime('%Y-%m')

# ── Anthropic API: 今日の優先案件を抽出 ─────────────────────
def fetch_priorities(rows):
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        print('ANTHROPIC_API_KEY が未設定のためAI分析をスキップします')
        return []

    cases = [
        {
            'case_id':          r.get('case_id', ''),
            'company_contact':  r.get('company_contact', ''),
            'country':          r.get('country', ''),
            'stage':            r.get('stage', ''),
            'last_updated':     r.get('last_updated', ''),
            'next_action':      r.get('next_action', ''),
            'confirmed_facts':  str(r.get('confirmed_facts', '') or '')[:120],
        }
        for r in rows
    ]

    prompt = f"""以下の案件リストから「今日対応すべき案件」を抽出してください。
判断基準:
- 出荷中・発注確定: 最優先
- 見積提示から3日以上経過: 高優先
- サンプル送付から3日以上経過: 高優先
- 初回接触から7日以上経過: 中優先
- last_updatedが空の案件: 確認必要

各案件について以下のJSON形式で返してください:
[{{"case_id": "C001", "priority": "最優先/高優先/中優先", "reason": "理由を20字以内で", "action": "具体的なアクションを30字以内で"}}]

JSON配列のみを返してください。余分なテキストや```は不要です。

案件データ:
{json.dumps(cases, ensure_ascii=False)}"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=2048,
            system='あなたは長峰製茶の海外営業アシスタントです。',
            messages=[{'role': 'user', 'content': prompt}]
        )
        text = message.content[0].text.strip()
        # マークダウンコードブロックを除去
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        # JSON配列部分をregexで抽出（前後の余分なテキストを除去）
        m = re.search(r'\[[\s\S]*\]', text)
        if m:
            text = m.group()
        result = json.loads(text)
        print(f'AI分析完了: {len(result)}件の優先案件を抽出')
        return result
    except Exception as ex:
        print(f'AI分析エラー: {ex}')
        return []

priorities = fetch_priorities(rows)

# 優先度別カウント
prio_total  = len(priorities)
prio_high   = sum(1 for p in priorities if p.get('priority') in ('最優先', '高優先'))

# 既存集計
ACTION_STAGES = {'初回接触', 'サンプル送付', '見積提示', '交渉中'}
total         = len(rows)
cont_count    = sum(1 for r in rows if r.get('stage', '') == '取引継続')
monthly_count = sum(1 for r in rows if str(r.get('last_updated', '')).strip().startswith(this_month))

# ── グラフ用集計 ──────────────────────────────────────────
stage_counter  = Counter(r.get('stage', '不明') for r in rows)
stage_sorted   = stage_counter.most_common()
stage_labels   = json.dumps([s for s, _ in stage_sorted], ensure_ascii=False)
stage_data     = json.dumps([c for _, c in stage_sorted])

country_counter = Counter(r.get('country', '') for r in rows if str(r.get('country', '')).strip() not in ('', '-'))
top10 = country_counter.most_common(10)
country_labels = json.dumps([c for c, _ in top10], ensure_ascii=False)
country_data   = json.dumps([n for _, n in top10])

# ── ステージバッジ ────────────────────────────────────────
STAGE_COLORS = {
    '初回接触':    ('#90a4ae', '#1a2744'),
    'サンプル送付': ('#4fc3f7', '#1a2744'),
    '見積提示':    ('#f9c74f', '#1a2744'),
    '交渉中':      ('#f8961e', '#1a2744'),
    '取引継続':    ('#66bb6a', '#1a2744'),
    '発注確定':    ('#ce93d8', '#1a2744'),
    '出荷中':      ('#26c6da', '#1a2744'),
    '終了':        ('#546e7a', '#e8eaf6'),
    '保留':        ('#8d6e63', '#e8eaf6'),
}

ROW_BG = {
    '初回接触':    'rgba(144,164,174,0.06)',
    'サンプル送付': 'rgba(79,195,247,0.08)',
    '見積提示':    'rgba(249,199,79,0.08)',
    '交渉中':      'rgba(248,150,30,0.08)',
    '取引継続':    'rgba(102,187,106,0.07)',
    '発注確定':    'rgba(206,147,216,0.08)',
    '出荷中':      'rgba(38,198,218,0.07)',
}

PRIO_STYLE = {
    '最優先': ('bg:#ef5350;color:#fff',  '#ef5350'),
    '高優先': ('bg:#f8961e;color:#1a2744', '#f8961e'),
    '中優先': ('bg:#f9c74f;color:#1a2744', '#f9c74f'),
}

def e(val):
    return html_mod.escape(str(val) if val is not None else '', quote=True)

def stage_badge(stage):
    bg, fg = STAGE_COLORS.get(stage, ('#78909c', '#e8eaf6'))
    return (f'<span style="background:{bg};color:{fg};padding:2px 9px;'
            f'border-radius:4px;font-size:0.78em;font-weight:bold;white-space:nowrap;">'
            f'{e(stage)}</span>')

def search_attr(r, stage, country):
    parts = [r.get('case_id',''), r.get('company_contact',''), country, stage, r.get('next_action','')]
    return e(' '.join(str(p) for p in parts))

# ── 優先案件セクション HTML ───────────────────────────────
case_map    = {r.get('case_id', ''): r.get('company_contact', '') for r in rows}
country_map = {r.get('case_id', ''): str(r.get('country', '') or '') for r in rows}

def prio_badge(prio):
    styles = {'最優先': 'background:#ef5350;color:#fff',
              '高優先': 'background:#f8961e;color:#1a2744',
              '中優先': 'background:#f9c74f;color:#1a2744'}
    s = styles.get(prio, 'background:#546e7a;color:#e8eaf6')
    return f'<span style="{s};padding:3px 10px;border-radius:4px;font-size:0.78em;font-weight:bold;white-space:nowrap;">{e(prio)}</span>'

def prio_border(prio):
    return {'最優先': '#ef5350', '高優先': '#f8961e', '中優先': '#f9c74f'}.get(prio, '#546e7a')

if priorities:
    prio_order = {'最優先': 0, '高優先': 1, '中優先': 2}
    sorted_prios = sorted(priorities, key=lambda p: prio_order.get(p.get('priority',''), 9))
    cards = []
    for p in sorted_prios:
        cid     = p.get('case_id', '')
        prio    = p.get('priority', '')
        reason  = p.get('reason', '')
        action  = p.get('action', '')
        contact = case_map.get(cid, '')
        country = country_map.get(cid, '')
        border  = prio_border(prio)
        country_badge = (
            f'<span class="prio-country-badge">{e(country)}</span>' if country and country != '-' else ''
        )
        cards.append(
            f'<div class="prio-card" style="border-left:4px solid {border}">'
            f'<div class="prio-top">'
            f'  <div class="prio-left">'
            f'    <div class="prio-company">{e(contact)}</div>'
            f'    {country_badge}'
            f'  </div>'
            f'  <span class="prio-id">{e(cid)}</span>'
            f'</div>'
            f'<div class="prio-middle">'
            f'  {prio_badge(prio)}'
            f'  <span class="prio-reason">{e(reason)}</span>'
            f'</div>'
            f'<div class="prio-action">→ {e(action)}</div>'
            f'</div>'
        )
    priority_section_html = '<div class="prio-grid">' + '\n'.join(cards) + '</div>'
else:
    priority_section_html = '<div class="prio-empty">本日の優先案件はありません ✓</div>'

# ── 全案件テーブル HTML ───────────────────────────────────
table_rows_html_parts = []
for r in rows:
    stage   = r.get('stage', '')
    country = str(r.get('country', '') or '')
    cid     = r.get('case_id', '')
    contact = r.get('company_contact', '')
    action  = r.get('next_action', '')
    updated = r.get('last_updated', '')
    facts   = r.get('confirmed_facts', '') or ''
    notes   = r.get('notes', '') or ''
    interest = r.get('interest', '') or ''

    row_bg   = ROW_BG.get(stage, '')
    search_v = search_attr(r, stage, country)

    data_row = (
        f'<tr class="data-row" style="background:{row_bg}" '
        f'data-search="{search_v.lower()}" '
        f'data-stage="{e(stage)}" '
        f'data-country="{e(country)}" '
        f'onclick="toggleDetail(this)">'
        f'<td>{e(cid)}</td>'
        f'<td>{e(contact)}</td>'
        f'<td>{e(country)}</td>'
        f'<td>{stage_badge(stage)}</td>'
        f'<td>{e(action)}</td>'
        f'<td>{e(updated)}</td>'
        f'</tr>'
    )

    detail_inner = ''
    if facts:
        detail_inner += f'<div><span class="dl">Confirmed Facts</span>{e(facts)}</div>'
    if notes:
        detail_inner += f'<div><span class="dl">Notes</span>{e(notes)}</div>'
    if interest:
        detail_inner += f'<div><span class="dl">Interest</span>{e(interest)}</div>'
    if not detail_inner:
        detail_inner = '<div style="color:#546e7a">（詳細なし）</div>'

    detail_row = (
        f'<tr class="detail-row hidden">'
        f'<td colspan="6"><div class="detail-box">{detail_inner}</div></td>'
        f'</tr>'
    )
    table_rows_html_parts.append(data_row + '\n' + detail_row)

table_rows_html = '\n'.join(table_rows_html_parts)

# ── HTML出力 ──────────────────────────────────────────────
CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#1a2744;color:#e8eaf6;font-family:system-ui,-apple-system,sans-serif;min-height:100vh}
header{background:#111d3a;padding:20px 32px;border-bottom:2px solid #4fc3f7;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
header h1{font-size:1.5rem;color:#4fc3f7;letter-spacing:1px}
.date{color:#90a4ae;font-size:0.88rem}
main{max-width:1200px;margin:0 auto;padding:28px 20px}
section{margin-bottom:36px}
h2{color:#4fc3f7;font-size:0.85rem;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:16px;padding-left:10px;border-left:3px solid #4fc3f7}
.kpi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}
.kpi-card{background:#1e2f52;border-radius:12px;padding:22px 18px;box-shadow:0 4px 18px rgba(0,0,0,.45);border-top:3px solid #4fc3f7}
.kpi-card .label{font-size:0.76rem;color:#90a4ae;margin-bottom:8px;letter-spacing:.5px}
.kpi-card .value{font-size:2.5rem;font-weight:700;color:#fff;line-height:1}
.kpi-card .sub{font-size:0.73rem;color:#4fc3f7;margin-top:6px}
.chart-card{background:#1e2f52;border-radius:12px;padding:24px;box-shadow:0 4px 18px rgba(0,0,0,.45)}
.filter-bar{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:12px;align-items:center}
.filter-bar input,.filter-bar select{
  padding:9px 12px;background:#1e2f52;border:1px solid #4fc3f7;border-radius:8px;
  color:#e8eaf6;font-size:0.86rem;outline:none;min-width:160px}
.filter-bar input::placeholder{color:#546e7a}
.filter-bar select option{background:#1e2f52}
.count-display{font-size:0.82rem;color:#4fc3f7;margin-bottom:10px}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:0.86rem}
thead tr{background:#111d3a}
thead th{padding:11px 14px;text-align:left;color:#4fc3f7;font-weight:600;white-space:nowrap}
thead th.sortable{cursor:pointer;user-select:none}
thead th.sortable:hover{color:#81d4fa}
thead th[data-sort="asc"]::after{content:' ▲';color:#f9c74f}
thead th[data-sort="desc"]::after{content:' ▼';color:#f9c74f}
tr.data-row{border-bottom:1px solid #263659;cursor:pointer;transition:background .15s}
tr.data-row:hover{background:rgba(79,195,247,.1)!important}
tr.data-row.expanded{background:rgba(79,195,247,.13)!important}
tr.data-row td{padding:10px 14px;vertical-align:middle}
tr.detail-row td{padding:0}
.detail-box{padding:14px 20px;background:#162240;border-left:3px solid #4fc3f7;font-size:0.84rem}
.detail-box div{margin-bottom:6px;color:#b0bec5;line-height:1.6}
.dl{display:inline-block;color:#4fc3f7;font-weight:600;min-width:130px;margin-right:8px}
.hidden{display:none!important}
.prio-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:14px}
.prio-card{background:#1e2f52;border-radius:10px;padding:16px 18px;box-shadow:0 4px 14px rgba(0,0,0,.4);display:flex;flex-direction:column;gap:8px}
.prio-top{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.prio-left{display:flex;flex-direction:column;gap:5px;min-width:0}
.prio-company{font-size:0.95rem;font-weight:600;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.prio-country-badge{display:inline-block;background:#263659;color:#90a4ae;padding:1px 7px;border-radius:4px;font-size:0.72em;font-weight:600;width:fit-content}
.prio-id{font-size:0.75rem;color:#546e7a;white-space:nowrap;flex-shrink:0;padding-top:2px}
.prio-middle{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.prio-reason{font-size:0.82rem;color:#cfd8dc}
.prio-action{font-size:0.82rem;color:#4fc3f7}
.prio-empty{background:#1e2f52;border-radius:10px;padding:20px 24px;color:#66bb6a;font-size:0.95rem}
@media(max-width:700px){.kpi-grid{grid-template-columns:repeat(2,1fr)}}
"""

JS = f"""
const chartOpts = {{
  indexAxis:'y', responsive:true, maintainAspectRatio:false,
  plugins:{{legend:{{display:false}}}},
  scales:{{
    x:{{ticks:{{color:'#90a4ae'}},grid:{{color:'rgba(255,255,255,0.05)'}}}},
    y:{{ticks:{{color:'#e8eaf6',font:{{size:12}}}},grid:{{display:false}}}}
  }}
}};
new Chart(document.getElementById('stageChart'),{{
  type:'bar',
  data:{{labels:{stage_labels},datasets:[{{data:{stage_data},backgroundColor:'#4fc3f7',borderRadius:4}}]}},
  options:chartOpts
}});
new Chart(document.getElementById('countryChart'),{{
  type:'bar',
  data:{{labels:{country_labels},datasets:[{{data:{country_data},backgroundColor:'#7986cb',borderRadius:4}}]}},
  options:chartOpts
}});

function buildDropdowns() {{
  const stageSet = new Set(), countrySet = new Set();
  document.querySelectorAll('#allTable tbody tr.data-row').forEach(tr => {{
    if (tr.dataset.stage)   stageSet.add(tr.dataset.stage);
    if (tr.dataset.country) countrySet.add(tr.dataset.country);
  }});
  const stageEl = document.getElementById('stageFilter');
  [...stageSet].sort().forEach(s => {{
    const o = document.createElement('option'); o.value = s; o.textContent = s; stageEl.appendChild(o);
  }});
  const countryEl = document.getElementById('countryFilter');
  [...countrySet].sort().forEach(c => {{
    const o = document.createElement('option'); o.value = c; o.textContent = c; countryEl.appendChild(o);
  }});
}}

function filterTable() {{
  const q       = document.getElementById('searchBox').value.toLowerCase();
  const stage   = document.getElementById('stageFilter').value;
  const country = document.getElementById('countryFilter').value;
  let visible = 0;
  const dataRows = document.querySelectorAll('#allTable tbody tr.data-row');
  dataRows.forEach(tr => {{
    const detail = tr.nextElementSibling;
    const ok = (!q       || tr.dataset.search.includes(q))
            && (!stage   || tr.dataset.stage   === stage)
            && (!country || tr.dataset.country === country);
    tr.classList.toggle('hidden', !ok);
    if (detail && detail.classList.contains('detail-row')) {{
      if (!ok) detail.classList.add('hidden');
    }}
    if (ok) visible++;
  }});
  document.getElementById('countDisplay').textContent = visible + '件 / ' + dataRows.length + '件中';
}}

function toggleDetail(tr) {{
  const detail = tr.nextElementSibling;
  if (detail && detail.classList.contains('detail-row')) {{
    detail.classList.toggle('hidden');
    tr.classList.toggle('expanded');
  }}
}}

let _sortCol = -1, _sortAsc = true;
function sortTable(col) {{
  if (_sortCol === col) {{ _sortAsc = !_sortAsc; }} else {{ _sortCol = col; _sortAsc = true; }}
  document.querySelectorAll('#allTable thead th.sortable').forEach((th, i) => {{
    th.dataset.sort = (i === col) ? (_sortAsc ? 'asc' : 'desc') : '';
  }});
  const tbody = document.querySelector('#allTable tbody');
  const pairs = [...document.querySelectorAll('#allTable tbody tr.data-row')]
    .map(tr => [tr, tr.nextElementSibling]);
  pairs.sort((a, b) => {{
    const av = a[0].querySelectorAll('td')[col]?.textContent.trim().toLowerCase() || '';
    const bv = b[0].querySelectorAll('td')[col]?.textContent.trim().toLowerCase() || '';
    return _sortAsc ? av.localeCompare(bv, 'ja') : bv.localeCompare(av, 'ja');
  }});
  pairs.forEach(([dr, dtr]) => {{
    tbody.appendChild(dr);
    if (dtr?.classList.contains('detail-row')) tbody.appendChild(dtr);
  }});
}}

window.addEventListener('DOMContentLoaded', () => {{
  buildDropdowns();
  filterTable();
}});
"""

html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Nagamine Sales Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>Nagamine Sales Dashboard</h1>
  <span class="date">{now_str}</span>
</header>
<main>

<section>
  <h2>今日の優先案件（AI分析）</h2>
  {priority_section_html}
</section>

<section>
  <h2>Summary</h2>
  <div class="kpi-grid">
    <div class="kpi-card" style="border-top-color:#ef5350">
      <div class="label">今日の優先案件</div>
      <div class="value" style="color:#ef5350">{prio_total}</div>
      <div class="sub">AI抽出</div>
    </div>
    <div class="kpi-card" style="border-top-color:#f8961e">
      <div class="label">高優先度以上</div>
      <div class="value" style="color:#f8961e">{prio_high}</div>
      <div class="sub">最優先＋高優先</div>
    </div>
    <div class="kpi-card">
      <div class="label">取引継続</div>
      <div class="value">{cont_count}</div>
      <div class="sub">アクティブ顧客</div>
    </div>
    <div class="kpi-card">
      <div class="label">今月更新</div>
      <div class="value">{monthly_count}</div>
      <div class="sub">{now_jst.strftime('%Y年%m月')}</div>
    </div>
  </div>
</section>

<section>
  <h2>ステージ別件数</h2>
  <div class="chart-card">
    <div style="position:relative;height:320px"><canvas id="stageChart"></canvas></div>
  </div>
</section>

<section>
  <h2>国別分布（上位10）</h2>
  <div class="chart-card">
    <div style="position:relative;height:280px"><canvas id="countryChart"></canvas></div>
  </div>
</section>

<section>
  <h2>全案件テーブル</h2>
  <div class="filter-bar">
    <input type="text" id="searchBox" placeholder="キーワードで絞り込み…" oninput="filterTable()">
    <select id="stageFilter" onchange="filterTable()"><option value="">ステージ：すべて</option></select>
    <select id="countryFilter" onchange="filterTable()"><option value="">国：すべて</option></select>
  </div>
  <div class="count-display" id="countDisplay"></div>
  <div class="table-wrap">
    <table id="allTable">
      <thead>
        <tr>
          <th class="sortable" data-sort="" onclick="sortTable(0)">Case ID</th>
          <th class="sortable" data-sort="" onclick="sortTable(1)">会社 / 担当者</th>
          <th class="sortable" data-sort="" onclick="sortTable(2)">国</th>
          <th class="sortable" data-sort="" onclick="sortTable(3)">ステージ</th>
          <th class="sortable" data-sort="" onclick="sortTable(4)">Next Action</th>
          <th class="sortable" data-sort="" onclick="sortTable(5)">最終更新</th>
        </tr>
      </thead>
      <tbody>
{table_rows_html}
      </tbody>
    </table>
  </div>
</section>

</main>
<script>{JS}</script>
</body>
</html>"""

out_path = 'index.html'
with open(out_path, 'w', encoding='utf-8') as f:
    f.write(html)

print(f"出力完了: {out_path}")
print(f"優先案件:{prio_total}件（高優先以上:{prio_high}件）/ 取引継続:{cont_count} / 今月更新:{monthly_count}")
