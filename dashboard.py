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

# ── 担当者定義 ────────────────────────────────────────────
MEMBERS = ['takayuki', 'kayo.tatara', 'kataoka']
MEMBER_ROLES = {
    'takayuki':   'LP/メール経由の見積・出荷対応が主な業務です。',
    'kayo.tatara':'Instagram経由顧客の出荷・発送確認が主な業務です。',
    'kataoka':    'Instagram経由の新規顧客への初回返信・フォローが主な業務です。',
}

def member_html_id(name):
    return 'member-' + name.replace('.', '-')

# owner列でグループ分け
rows_by_owner = {m: [r for r in rows if r.get('owner', '') == m] for m in MEMBERS}

# ── Anthropic API: 担当者ごとの優先案件を抽出 ────────────────
def fetch_priorities(owner_rows, owner):
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return []
    if not owner_rows:
        return []

    role_desc = MEMBER_ROLES.get(owner, '')
    cases = [
        {
            'case_id':         r.get('case_id', ''),
            'company_contact': r.get('company_contact', ''),
            'country':         r.get('country', ''),
            'stage':           r.get('stage', ''),
            'last_updated':    r.get('last_updated', ''),
            'next_action':     r.get('next_action', ''),
            'confirmed_facts': str(r.get('confirmed_facts', '') or '')[:120],
        }
        for r in owner_rows
    ]

    prompt = f"""担当者の役割: {role_desc}

以下の案件リストから「今日対応すべき案件」を抽出してください。
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
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        m = re.search(r'\[[\s\S]*\]', text)
        if m:
            text = m.group()
        result = json.loads(text)
        print(f'  [{owner}] AI分析完了: {len(result)}件抽出')
        return result
    except Exception as ex:
        print(f'  [{owner}] AI分析エラー: {ex}')
        return []

print('AI分析開始...')
priorities_by_owner = {m: fetch_priorities(rows_by_owner[m], m) for m in MEMBERS}

# ── 集計（全体・担当者別） ────────────────────────────────
ACTION_STAGES = {'初回接触', 'サンプル送付', '見積提示', '交渉中'}
total         = len(rows)
cont_count    = sum(1 for r in rows if r.get('stage', '') == '取引継続')
monthly_count = sum(1 for r in rows if str(r.get('last_updated', '')).strip().startswith(this_month))

def kpi_for(owner_rows, priorities):
    return {
        'total':      len(owner_rows),
        'prio_total': len(priorities),
        'prio_high':  sum(1 for p in priorities if p.get('priority') in ('最優先', '高優先')),
        'action':     sum(1 for r in owner_rows if r.get('stage', '') in ACTION_STAGES),
    }

kpi = {m: kpi_for(rows_by_owner[m], priorities_by_owner[m]) for m in MEMBERS}

# ── グラフ用集計（全体） ──────────────────────────────────
stage_counter  = Counter(r.get('stage', '不明') for r in rows)
stage_sorted   = stage_counter.most_common()
stage_labels   = json.dumps([s for s, _ in stage_sorted], ensure_ascii=False)
stage_data     = json.dumps([c for _, c in stage_sorted])

country_counter = Counter(r.get('country', '') for r in rows if str(r.get('country', '')).strip() not in ('', '-'))
top10 = country_counter.most_common(10)
country_labels = json.dumps([c for c, _ in top10], ensure_ascii=False)
country_data   = json.dumps([n for _, n in top10])

# ── ヘルパー関数 ──────────────────────────────────────────
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

def e(val):
    return html_mod.escape(str(val) if val is not None else '', quote=True)

def stage_badge(stage):
    bg, fg = STAGE_COLORS.get(stage, ('#78909c', '#e8eaf6'))
    return (f'<span style="background:{bg};color:{fg};padding:2px 9px;'
            f'border-radius:4px;font-size:0.78em;font-weight:bold;white-space:nowrap;">'
            f'{e(stage)}</span>')

def prio_badge(prio):
    styles = {'最優先': 'background:#ef5350;color:#fff',
              '高優先': 'background:#f8961e;color:#1a2744',
              '中優先': 'background:#f9c74f;color:#1a2744'}
    s = styles.get(prio, 'background:#546e7a;color:#e8eaf6')
    return f'<span style="{s};padding:3px 10px;border-radius:4px;font-size:0.78em;font-weight:bold;white-space:nowrap;">{e(prio)}</span>'

def prio_border(prio):
    return {'最優先': '#ef5350', '高優先': '#f8961e', '中優先': '#f9c74f'}.get(prio, '#546e7a')

case_map    = {r.get('case_id', ''): r.get('company_contact', '') for r in rows}
country_map = {r.get('case_id', ''): str(r.get('country', '') or '') for r in rows}

# ── 優先案件カード HTML（担当者別） ──────────────────────────
def build_priority_html(priorities):
    if not priorities:
        return '<div class="prio-empty">本日の優先案件はありません ✓</div>'
    prio_order = {'最優先': 0, '高優先': 1, '中優先': 2}
    sorted_prios = sorted(priorities, key=lambda p: prio_order.get(p.get('priority', ''), 9))
    cards = []
    for p in sorted_prios:
        cid     = p.get('case_id', '')
        prio    = p.get('priority', '')
        reason  = p.get('reason', '')
        action  = p.get('action', '')
        contact = case_map.get(cid, '')
        country = country_map.get(cid, '')
        border  = prio_border(prio)
        ctry_badge = (f'<span class="prio-country-badge">{e(country)}</span>'
                      if country and country != '-' else '')
        cards.append(
            f'<div class="prio-card" style="border-left:4px solid {border}">'
            f'<div class="prio-top">'
            f'  <div class="prio-left">'
            f'    <div class="prio-company">{e(contact)}</div>'
            f'    {ctry_badge}'
            f'  </div>'
            f'  <span class="prio-id">{e(cid)}</span>'
            f'</div>'
            f'<div class="prio-middle">{prio_badge(prio)}'
            f'  <span class="prio-reason">{e(reason)}</span>'
            f'</div>'
            f'<div class="prio-action">→ {e(action)}</div>'
            f'</div>'
        )
    return '<div class="prio-grid">' + '\n'.join(cards) + '</div>'

# ── 担当者別セクション HTML ───────────────────────────────
def build_member_section(owner):
    mid   = member_html_id(owner)
    k     = kpi[owner]
    prios = priorities_by_owner[owner]
    role  = MEMBER_ROLES[owner]
    return f"""
<div id="{mid}" class="member-section hidden">
  <div class="member-role-desc">{e(role)}</div>
  <div class="kpi-grid" style="margin-bottom:24px">
    <div class="kpi-card">
      <div class="label">担当案件数</div>
      <div class="value">{k['total']}</div>
      <div class="sub">全ステージ</div>
    </div>
    <div class="kpi-card" style="border-top-color:#f8961e">
      <div class="label">対応待ち</div>
      <div class="value" style="color:#f8961e">{k['action']}</div>
      <div class="sub">初回〜交渉中</div>
    </div>
    <div class="kpi-card" style="border-top-color:#ef5350">
      <div class="label">今日の優先案件</div>
      <div class="value" style="color:#ef5350">{k['prio_total']}</div>
      <div class="sub">AI抽出</div>
    </div>
    <div class="kpi-card" style="border-top-color:#f9c74f">
      <div class="label">高優先度以上</div>
      <div class="value" style="color:#f9c74f">{k['prio_high']}</div>
      <div class="sub">最優先＋高優先</div>
    </div>
  </div>
  <h2>今日の優先案件（AI分析）</h2>
  {build_priority_html(prios)}
</div>"""

member_sections_html = ''.join(build_member_section(m) for m in MEMBERS)

# ── 全案件テーブル HTML ───────────────────────────────────
table_rows_html_parts = []
for r in rows:
    stage   = r.get('stage', '')
    country = str(r.get('country', '') or '')
    owner   = str(r.get('owner', '') or '')
    cid     = r.get('case_id', '')
    contact = r.get('company_contact', '')
    action  = r.get('next_action', '')
    updated = r.get('last_updated', '')
    facts   = r.get('confirmed_facts', '') or ''
    notes   = r.get('notes', '') or ''
    interest = r.get('interest', '') or ''

    row_bg  = ROW_BG.get(stage, '')
    search_v = e(' '.join([cid, contact, country, owner, stage, action]))

    data_row = (
        f'<tr class="data-row" style="background:{row_bg}" '
        f'data-search="{search_v.lower()}" '
        f'data-stage="{e(stage)}" '
        f'data-country="{e(country)}" '
        f'data-owner="{e(owner)}" '
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

# ── CSS / JS / HTML ───────────────────────────────────────
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
.member-bar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.member-btn{padding:10px 24px;border:2px solid #4fc3f7;border-radius:8px;background:transparent;
  color:#4fc3f7;font-size:0.9rem;font-weight:600;cursor:pointer;transition:all .18s;letter-spacing:.5px}
.member-btn:hover{background:rgba(79,195,247,.12)}
.member-btn.active{background:#4fc3f7;color:#1a2744}
.member-placeholder{color:#546e7a;font-size:0.92rem;padding:8px 0}
.member-section{margin-top:24px}
.member-role-desc{font-size:0.82rem;color:#90a4ae;margin-bottom:20px;padding:10px 14px;
  background:#162240;border-left:3px solid #546e7a;border-radius:0 6px 6px 0}
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

let _activeMember = null;

function selectMember(name) {{
  if (_activeMember === name) {{
    // 同じボタン再クリックで選択解除
    _activeMember = null;
    document.querySelectorAll('.member-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.member-section').forEach(s => s.classList.add('hidden'));
    document.getElementById('memberPlaceholder').classList.remove('hidden');
    return;
  }}
  _activeMember = name;
  document.querySelectorAll('.member-btn').forEach(b => {{
    b.classList.toggle('active', b.dataset.member === name);
  }});
  document.querySelectorAll('.member-section').forEach(s => s.classList.add('hidden'));
  const mid = 'member-' + name.replace(/\\./g, '-');
  const sec = document.getElementById(mid);
  if (sec) sec.classList.remove('hidden');
  document.getElementById('memberPlaceholder').classList.add('hidden');
}}

function buildDropdowns() {{
  const stageSet = new Set(), countrySet = new Set(), ownerSet = new Set();
  document.querySelectorAll('#allTable tbody tr.data-row').forEach(tr => {{
    if (tr.dataset.stage)   stageSet.add(tr.dataset.stage);
    if (tr.dataset.country) countrySet.add(tr.dataset.country);
    if (tr.dataset.owner)   ownerSet.add(tr.dataset.owner);
  }});
  const stageEl = document.getElementById('stageFilter');
  [...stageSet].sort().forEach(s => {{
    const o = document.createElement('option'); o.value = s; o.textContent = s; stageEl.appendChild(o);
  }});
  const countryEl = document.getElementById('countryFilter');
  [...countrySet].sort().forEach(c => {{
    const o = document.createElement('option'); o.value = c; o.textContent = c; countryEl.appendChild(o);
  }});
  const ownerEl = document.getElementById('ownerFilter');
  [...ownerSet].sort().forEach(ow => {{
    const o = document.createElement('option'); o.value = ow; o.textContent = ow; ownerEl.appendChild(o);
  }});
}}

function filterTable() {{
  const q       = document.getElementById('searchBox').value.toLowerCase();
  const stage   = document.getElementById('stageFilter').value;
  const country = document.getElementById('countryFilter').value;
  const owner   = document.getElementById('ownerFilter').value;
  let visible = 0;
  const dataRows = document.querySelectorAll('#allTable tbody tr.data-row');
  dataRows.forEach(tr => {{
    const detail = tr.nextElementSibling;
    const ok = (!q       || tr.dataset.search.includes(q))
            && (!stage   || tr.dataset.stage   === stage)
            && (!country || tr.dataset.country === country)
            && (!owner   || tr.dataset.owner   === owner);
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
  <h2>担当者</h2>
  <div class="member-bar">
    <button class="member-btn" data-member="takayuki"   onclick="selectMember('takayuki')">takayuki</button>
    <button class="member-btn" data-member="kayo.tatara" onclick="selectMember('kayo.tatara')">kayo.tatara</button>
    <button class="member-btn" data-member="kataoka"    onclick="selectMember('kataoka')">kataoka</button>
  </div>
  <div id="memberPlaceholder" class="member-placeholder">担当者を選択してください</div>
  {member_sections_html}
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
    <select id="stageFilter"   onchange="filterTable()"><option value="">ステージ：すべて</option></select>
    <select id="countryFilter" onchange="filterTable()"><option value="">国：すべて</option></select>
    <select id="ownerFilter"   onchange="filterTable()"><option value="">担当者：すべて</option></select>
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

print(f"\n出力完了: {out_path}")
for m in MEMBERS:
    k = kpi[m]
    print(f"  [{m}] 担当:{k['total']}件 / 優先:{k['prio_total']}件（高優先以上:{k['prio_high']}件）")
