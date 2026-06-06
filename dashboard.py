import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timezone, timedelta, date as date_type
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
now_jst   = datetime.now(JST)
today_date = now_jst.date()
now_str    = now_jst.strftime('%Y年%m月%d日 %H:%M JST')
this_month = now_jst.strftime('%Y-%m')

# ── 担当者定義 ────────────────────────────────────────────
MEMBERS = ['takayuki', 'kayo.tatara', 'kataoka']
MEMBER_ROLES = {
    'takayuki':    'LP/メール経由の見積・出荷対応が主な業務です。',
    'kayo.tatara': 'Instagram経由顧客の出荷・発送確認が主な業務です。',
    'kataoka':     'Instagram経由の新規顧客への初回返信・フォローが主な業務です。',
}
rows_by_owner = {m: [r for r in rows if r.get('owner', '') == m] for m in MEMBERS}

# ── 日付パーサー ──────────────────────────────────────────
def parse_date(val):
    if not val:
        return None
    s = str(val).strip()[:10]
    for fmt in ('%Y-%m-%d', '%Y/%m/%d'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

# ── 優先スコア計算 ────────────────────────────────────────
HIGH_STAGES = {'取引継続', '発注確定', '出荷中'}

def compute_score(r):
    score = 0
    reasons = []
    stage    = r.get('stage', '')
    updated  = parse_date(r.get('last_updated', ''))
    deadline = parse_date(r.get('deadline', ''))
    facts    = str(r.get('confirmed_facts', '') or '')
    interest = str(r.get('interest', '') or '')

    # +100: 高優先ステージ
    if stage in HIGH_STAGES:
        score += 100
        label = {'出荷中': '出荷確認待ち', '発注確定': '発注確定・対応中', '取引継続': '取引継続顧客'}
        reasons.append(label.get(stage, '高優先ステージ'))

    # +80: 期限超過
    if deadline and deadline <= today_date:
        days = (today_date - deadline).days
        score += 80
        reasons.append(f'フォロー期限超過{days}日')

    # +60/+50/+40: 経過日数
    if updated:
        days = (today_date - updated).days
        if stage == '見積提示' and days >= 3:
            score += 60
            reasons.append(f'見積後{days}日経過')
        elif stage == 'サンプル送付' and days >= 3:
            score += 50
            reasons.append(f'サンプル送付後{days}日経過')
        elif days >= 7:
            score += 40
            reasons.append(f'最終更新から{days}日経過')
    elif not updated:
        score += 30
        reasons.append('更新日未設定')

    # +30: 大口見込み
    kg_vals = re.findall(r'(\d+)\s*kg', facts + ' ' + interest, re.IGNORECASE)
    if kg_vals and max(int(x) for x in kg_vals) >= 10:
        max_kg = max(int(x) for x in kg_vals)
        score += 30
        reasons.append(f'{max_kg}kg以上の見込み')

    # 優先ラベル
    if score >= 100:
        label = '最優先'
    elif score >= 60:
        label = '高優先'
    elif score >= 30:
        label = '中優先'
    else:
        label = ''

    top_reason = reasons[0] if reasons else ''
    return score, top_reason, label

# ── Anthropic API ─────────────────────────────────────────
def fetch_priorities(owner_rows, owner):
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key or not owner_rows:
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
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=2048,
            system='あなたは長峰製茶の海外営業アシスタントです。',
            messages=[{'role': 'user', 'content': prompt}]
        )
        text = msg.content[0].text.strip()
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

# ── 集計 ─────────────────────────────────────────────────
ACTION_STAGES = {'初回接触', 'サンプル送付', '見積提示', '交渉中'}
total         = len(rows)
cont_count    = sum(1 for r in rows if r.get('stage', '') == '取引継続')
monthly_count = sum(1 for r in rows if str(r.get('last_updated', '')).strip().startswith(this_month))

# スコア全行計算
score_map = {r.get('case_id', ''): compute_score(r) for r in rows}

# 今日対応 case_id セット（score>=40 or AI抽出）
today_cids = set()
for r in rows:
    cid = r.get('case_id', '')
    sc, _, _ = score_map.get(cid, (0, '', ''))
    if sc >= 40:
        today_cids.add(cid)
for m in MEMBERS:
    for p in priorities_by_owner[m]:
        today_cids.add(p.get('case_id', ''))

def kpi_for(owner_rows, priorities):
    return {
        'total':      len(owner_rows),
        'prio_total': sum(1 for r in owner_rows if r.get('case_id', '') in today_cids),
        'prio_high':  sum(1 for p in priorities if p.get('priority') in ('最優先', '高優先')),
        'action':     sum(1 for r in owner_rows if r.get('stage', '') in ACTION_STAGES),
    }
kpi = {m: kpi_for(rows_by_owner[m], priorities_by_owner[m]) for m in MEMBERS}

# ── グラフ ────────────────────────────────────────────────
stage_counter  = Counter(r.get('stage', '不明') for r in rows)
stage_sorted   = stage_counter.most_common()
stage_labels   = json.dumps([s for s, _ in stage_sorted], ensure_ascii=False)
stage_data     = json.dumps([c for _, c in stage_sorted])
country_counter = Counter(r.get('country', '') for r in rows if str(r.get('country', '')).strip() not in ('', '-'))
top10 = country_counter.most_common(10)
country_labels = json.dumps([c for c, _ in top10], ensure_ascii=False)
country_data   = json.dumps([n for _, n in top10])

# ── 色定義（統一体系）─────────────────────────────────────
# 赤#e57373: 期限超過・今すぐ / 黄#ffd54f: 注意 / 青#4fc3f7: 通常 / 緑#81c784: 良好 / グレー#90a4ae: 休眠
STAGE_COLORS = {
    '初回接触':    ('#90a4ae', '#1a2744'),
    'サンプル送付': ('#4fc3f7', '#1a2744'),
    '見積提示':    ('#ffd54f', '#1a2744'),
    '交渉中':      ('#ffd54f', '#1a2744'),
    '取引継続':    ('#81c784', '#1a2744'),
    '発注確定':    ('#81c784', '#1a2744'),
    '出荷中':      ('#e57373', '#fff'),
    '終了':        ('#546e7a', '#e8eaf6'),
    '保留':        ('#90a4ae', '#e8eaf6'),
}
PRIO_COLORS = {
    '最優先': ('#e57373', '#fff'),
    '高優先': ('#ffd54f', '#1a2744'),
    '中優先': ('#4fc3f7', '#1a2744'),
}

def e(val):
    return html_mod.escape(str(val) if val is not None else '', quote=True)

def stage_badge(stage):
    bg, fg = STAGE_COLORS.get(stage, ('#78909c', '#e8eaf6'))
    return (f'<span style="background:{bg};color:{fg};padding:2px 8px;'
            f'border-radius:4px;font-size:0.76em;font-weight:bold;white-space:nowrap;">'
            f'{e(stage)}</span>')

def prio_badge(prio):
    bg, fg = PRIO_COLORS.get(prio, ('#546e7a', '#e8eaf6'))
    return (f'<span style="background:{bg};color:{fg};padding:2px 8px;'
            f'border-radius:4px;font-size:0.76em;font-weight:bold;white-space:nowrap;">'
            f'{e(prio)}</span>')

def prio_border_color(prio):
    return {'最優先': '#e57373', '高優先': '#ffd54f', '中優先': '#4fc3f7'}.get(prio, '#546e7a')

case_map    = {r.get('case_id', ''): r.get('company_contact', '') for r in rows}
country_map = {r.get('case_id', ''): str(r.get('country', '') or '') for r in rows}
row_map     = {r.get('case_id', ''): r for r in rows}

# ── 優先案件カード HTML（担当者別）──────────────────────────
def build_priority_html(owner, priorities):
    # AI lookup
    ai_data = {p['case_id']: p for p in priorities}
    # Score all rows for this owner
    owner_rows = rows_by_owner.get(owner, [])
    scored = []
    for r in owner_rows:
        cid = r.get('case_id', '')
        sc, reason, score_prio = score_map.get(cid, (0, '', ''))
        if sc <= 0 and cid not in ai_data:
            continue
        ai = ai_data.get(cid, {})
        prio   = ai.get('priority') or score_prio or '中優先'
        action = ai.get('action') or r.get('next_action', '')
        scored.append((sc, cid, prio, reason, action, r))

    scored.sort(key=lambda x: -x[0])
    if not scored:
        return '<div class="prio-empty">本日の優先案件はありません ✓</div>'

    mid = owner.replace('.', '-')
    cards_html = ''
    extra_html = ''
    for idx, (sc, cid, prio, reason, action, r) in enumerate(scored):
        contact = r.get('company_contact', '')
        country = str(r.get('country', '') or '')
        stage   = r.get('stage', '')
        facts   = r.get('confirmed_facts', '') or ''
        notes   = r.get('notes', '') or ''
        interest_v = r.get('interest', '') or ''
        border  = prio_border_color(prio)
        ctry_badge = (f'<span class="prio-country-badge">{e(country)}</span>'
                      if country and country not in ('-', '') else '')
        action_short = (action[:40] + '…') if len(action) > 40 else action
        reason_badge = (f'<span class="prio-reason-badge">{e(reason)}</span>' if reason else '')
        detail_inner = ''
        if facts:   detail_inner += f'<div><span class="dl">Confirmed Facts</span>{e(facts)}</div>'
        if notes:   detail_inner += f'<div><span class="dl">Notes</span>{e(notes)}</div>'
        if interest_v: detail_inner += f'<div><span class="dl">Interest</span>{e(interest_v)}</div>'
        if not detail_inner: detail_inner = '<div style="color:#546e7a">（詳細なし）</div>'

        card = (
            f'<div class="prio-card" style="border-left:4px solid {border}" '
            f'onclick="toggleCardDetail(this)">'
            f'<div class="prio-top">'
            f'  <div class="prio-left">'
            f'    <div class="prio-company">{e(contact)}</div>'
            f'    <div class="prio-badges">{ctry_badge}{stage_badge(stage)}</div>'
            f'  </div>'
            f'  <span class="prio-id">{e(cid)}</span>'
            f'</div>'
            f'<div class="prio-middle">{prio_badge(prio)}{reason_badge}</div>'
            f'<div class="prio-action">→ {e(action_short)}</div>'
            f'<div class="prio-detail hidden"><div class="detail-box">{detail_inner}</div></div>'
            f'</div>'
        )
        if idx < 8:
            cards_html += card + '\n'
        else:
            extra_html += card + '\n'

    remaining = len(scored) - 8
    show_more = ''
    if remaining > 0:
        show_more = (f'<div class="prio-extra hidden" id="extra-{mid}">'
                     f'{extra_html}</div>'
                     f'<button class="show-more-btn" '
                     f'onclick="showMoreCards(this,\'extra-{mid}\')">'
                     f'さらに表示（残り{remaining}件）</button>')

    return f'<div class="prio-grid">{cards_html}</div>{show_more}'

# ── 担当者別セクション HTML ───────────────────────────────
def build_member_section(owner):
    mid  = owner.replace('.', '-')
    k    = kpi[owner]
    role = MEMBER_ROLES[owner]
    return f"""
<div id="member-{mid}" class="member-section hidden">
  <div class="member-role-desc">{e(role)}</div>
  <div class="kpi-grid" style="margin-bottom:24px">
    <div class="kpi-card">
      <div class="label">担当案件数</div><div class="value">{k['total']}</div>
      <div class="sub">全ステージ</div>
    </div>
    <div class="kpi-card" style="border-top-color:#ffd54f">
      <div class="label">対応待ち</div>
      <div class="value" style="color:#ffd54f">{k['action']}</div>
      <div class="sub">初回〜交渉中</div>
    </div>
    <div class="kpi-card" style="border-top-color:#e57373">
      <div class="label">今日の優先案件</div>
      <div class="value" style="color:#e57373">{k['prio_total']}</div>
      <div class="sub">スコア抽出+AI</div>
    </div>
    <div class="kpi-card" style="border-top-color:#81c784">
      <div class="label">高優先度以上</div>
      <div class="value" style="color:#81c784">{k['prio_high']}</div>
      <div class="sub">最優先＋高優先</div>
    </div>
  </div>
  <h2>今日の優先案件</h2>
  {build_priority_html(owner, priorities_by_owner[owner])}
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
    interest_v = r.get('interest', '') or ''
    deadline_v = str(r.get('deadline', '') or '').strip()

    sc, _, prio_lbl = score_map.get(cid, (0, '', ''))
    is_today   = '1' if cid in today_cids else ''
    is_overdue = '1' if (parse_date(deadline_v) and parse_date(deadline_v) <= today_date) else ''
    is_highprio = '1' if (stage in HIGH_STAGES or sc >= 100) else ''

    # 行ボーダー色
    if is_overdue:
        row_border = 'border-left:4px solid #e57373'
    elif is_highprio:
        row_border = 'border-left:4px solid #81c784'
    else:
        row_border = 'border-left:4px solid transparent'

    action_short = (action[:35] + '…') if len(action) > 35 else action
    search_v = e(' '.join([cid, contact, country, owner, stage, action]))

    data_row = (
        f'<tr class="data-row" style="{row_border}" '
        f'data-search="{search_v.lower()}" '
        f'data-stage="{e(stage)}" '
        f'data-country="{e(country)}" '
        f'data-owner="{e(owner)}" '
        f'data-today="{is_today}" '
        f'data-overdue="{is_overdue}" '
        f'data-highprio="{is_highprio}" '
        f'onclick="toggleDetail(this)">'
        f'<td>{e(cid)}</td>'
        f'<td>{e(contact)}</td>'
        f'<td>{e(country)}</td>'
        f'<td>{stage_badge(stage)}</td>'
        f'<td class="action-cell" title="{e(action)}">{e(action_short)}</td>'
        f'<td>{e(updated)}</td>'
        f'</tr>'
    )
    detail_inner = ''
    if facts:      detail_inner += f'<div><span class="dl">Confirmed Facts</span>{e(facts)}</div>'
    if notes:      detail_inner += f'<div><span class="dl">Notes</span>{e(notes)}</div>'
    if interest_v: detail_inner += f'<div><span class="dl">Interest</span>{e(interest_v)}</div>'
    if not detail_inner:
        detail_inner = '<div style="color:#546e7a">（詳細なし）</div>'

    detail_row = (
        f'<tr class="detail-row hidden">'
        f'<td colspan="6"><div class="detail-box">{detail_inner}</div></td>'
        f'</tr>'
    )
    table_rows_html_parts.append(data_row + '\n' + detail_row)

table_rows_html = '\n'.join(table_rows_html_parts)

# ── CSS ──────────────────────────────────────────────────
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
/* member */
.member-bar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.member-btn{padding:10px 24px;border:2px solid #4fc3f7;border-radius:8px;background:transparent;
  color:#4fc3f7;font-size:0.9rem;font-weight:600;cursor:pointer;transition:all .18s;letter-spacing:.5px}
.member-btn:hover{background:rgba(79,195,247,.12)}
.member-btn.active{background:#4fc3f7;color:#1a2744}
.member-placeholder{color:#546e7a;font-size:0.92rem;padding:8px 0}
.member-section{margin-top:24px}
.member-role-desc{font-size:0.82rem;color:#90a4ae;margin-bottom:20px;padding:10px 14px;
  background:#162240;border-left:3px solid #546e7a;border-radius:0 6px 6px 0}
/* priority cards */
.prio-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-bottom:10px}
.prio-extra{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-bottom:10px}
.prio-card{background:#1e2f52;border-radius:10px;padding:14px 16px;box-shadow:0 4px 14px rgba(0,0,0,.4);
  display:flex;flex-direction:column;gap:7px;cursor:pointer;transition:background .15s}
.prio-card:hover{background:#243a60}
.prio-top{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.prio-left{display:flex;flex-direction:column;gap:4px;min-width:0}
.prio-company{font-size:0.93rem;font-weight:600;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.prio-badges{display:flex;gap:5px;flex-wrap:wrap;margin-top:2px}
.prio-country-badge{display:inline-block;background:#263659;color:#90a4ae;padding:1px 7px;border-radius:4px;font-size:0.72em;font-weight:600}
.prio-id{font-size:0.74rem;color:#546e7a;white-space:nowrap;flex-shrink:0;padding-top:2px}
.prio-middle{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.prio-reason-badge{background:#1a2f50;color:#90a4ae;padding:2px 8px;border-radius:4px;font-size:0.72em;border:1px solid #2a3f60}
.prio-action{font-size:0.80rem;color:#4fc3f7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.prio-detail{margin-top:4px}
.prio-empty{background:#1e2f52;border-radius:10px;padding:20px 24px;color:#81c784;font-size:0.95rem}
.show-more-btn{background:transparent;border:1px solid #4fc3f7;color:#4fc3f7;padding:7px 16px;
  border-radius:6px;cursor:pointer;font-size:0.82rem;margin-top:6px}
.show-more-btn:hover{background:rgba(79,195,247,.1)}
/* table */
.tab-bar{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.tab-btn{padding:6px 14px;border:1px solid #2a3f60;border-radius:20px;background:transparent;
  color:#90a4ae;font-size:0.80rem;cursor:pointer;transition:all .15s;white-space:nowrap}
.tab-btn:hover{border-color:#4fc3f7;color:#4fc3f7}
.tab-btn.active{background:#4fc3f7;border-color:#4fc3f7;color:#1a2744;font-weight:600}
.filter-bar{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:10px;align-items:center}
.filter-bar input,.filter-bar select{
  padding:8px 12px;background:#1e2f52;border:1px solid #4fc3f7;border-radius:8px;
  color:#e8eaf6;font-size:0.85rem;outline:none;min-width:150px}
.filter-bar input::placeholder{color:#546e7a}
.filter-bar select option{background:#1e2f52}
.count-display{font-size:0.82rem;color:#4fc3f7;margin-bottom:8px}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:0.85rem}
thead tr{background:#111d3a}
thead th{padding:10px 13px;text-align:left;color:#4fc3f7;font-weight:600;white-space:nowrap}
thead th.sortable{cursor:pointer;user-select:none}
thead th.sortable:hover{color:#81d4fa}
thead th[data-sort="asc"]::after{content:' ▲';color:#ffd54f}
thead th[data-sort="desc"]::after{content:' ▼';color:#ffd54f}
tr.data-row{border-bottom:1px solid #263659;cursor:pointer;transition:background .15s}
tr.data-row:hover{background:rgba(79,195,247,.08)!important}
tr.data-row.expanded{background:rgba(79,195,247,.12)!important}
tr.data-row td{padding:9px 13px;vertical-align:middle}
.action-cell{max-width:180px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
tr.detail-row td{padding:0}
.detail-box{padding:13px 18px;background:#162240;border-left:3px solid #4fc3f7;font-size:0.83rem}
.detail-box div{margin-bottom:6px;color:#b0bec5;line-height:1.6}
.dl{display:inline-block;color:#4fc3f7;font-weight:600;min-width:130px;margin-right:8px}
/* pagination */
.pagination{display:flex;gap:6px;flex-wrap:wrap;margin-top:12px;align-items:center}
.page-btn{padding:5px 11px;border:1px solid #2a3f60;border-radius:5px;background:transparent;
  color:#90a4ae;font-size:0.80rem;cursor:pointer;transition:all .15s;min-width:32px;text-align:center}
.page-btn:hover{border-color:#4fc3f7;color:#4fc3f7}
.page-btn.active{background:#4fc3f7;border-color:#4fc3f7;color:#1a2744;font-weight:700}
.page-btn:disabled{opacity:.4;cursor:default}
.hidden{display:none!important}
@media(max-width:700px){.kpi-grid{grid-template-columns:repeat(2,1fr)}}
"""

# ── JS ───────────────────────────────────────────────────
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

// ── member switch ──
let _activeMember = null;
function selectMember(name) {{
  if (_activeMember === name) {{
    _activeMember = null;
    document.querySelectorAll('.member-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.member-section').forEach(s => s.classList.add('hidden'));
    document.getElementById('memberPlaceholder').classList.remove('hidden');
    return;
  }}
  _activeMember = name;
  document.querySelectorAll('.member-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.member === name));
  document.querySelectorAll('.member-section').forEach(s => s.classList.add('hidden'));
  const sec = document.getElementById('member-' + name.replace(/\\./g,'-'));
  if (sec) sec.classList.remove('hidden');
  document.getElementById('memberPlaceholder').classList.add('hidden');
}}

// ── card expand ──
function toggleCardDetail(card) {{
  const det = card.querySelector('.prio-detail');
  if (det) det.classList.toggle('hidden');
}}
function showMoreCards(btn, extraId) {{
  document.getElementById(extraId).classList.remove('hidden');
  btn.style.display = 'none';
}}

// ── table: dropdowns ──
function buildDropdowns() {{
  const stageSet = new Set(), countrySet = new Set(), ownerSet = new Set();
  document.querySelectorAll('#allTable tbody tr.data-row').forEach(tr => {{
    if (tr.dataset.stage)   stageSet.add(tr.dataset.stage);
    if (tr.dataset.country) countrySet.add(tr.dataset.country);
    if (tr.dataset.owner)   ownerSet.add(tr.dataset.owner);
  }});
  const stageEl = document.getElementById('stageFilter');
  [...stageSet].sort().forEach(s => {{
    const o = document.createElement('option'); o.value=s; o.textContent=s; stageEl.appendChild(o);
  }});
  const countryEl = document.getElementById('countryFilter');
  [...countrySet].sort().forEach(c => {{
    const o = document.createElement('option'); o.value=c; o.textContent=c; countryEl.appendChild(o);
  }});
  const ownerEl = document.getElementById('ownerFilter');
  [...ownerSet].sort().forEach(ow => {{
    const o = document.createElement('option'); o.value=ow; o.textContent=ow; ownerEl.appendChild(o);
  }});
}}

// ── table: filter + pagination ──
const PAGE_SIZE = 20;
let _currentPage = 1;
let _activeTab   = 'all';

function setTab(filter) {{
  _activeTab = filter;
  _currentPage = 1;
  document.querySelectorAll('.tab-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.filter === filter));
  applyFilter();
}}

function filterTable() {{
  _currentPage = 1;
  applyFilter();
}}

function rowMatchesTab(tr, tab) {{
  switch(tab) {{
    case 'today':    return tr.dataset.today === '1';
    case 'highprio': return tr.dataset.highprio === '1';
    case 'overdue':  return tr.dataset.overdue === '1';
    case 'sample':   return tr.dataset.stage === 'サンプル送付';
    case 'estimate': return tr.dataset.stage === '見積提示';
    case 'shipping': return tr.dataset.stage === '出荷中';
    default:         return true;
  }}
}}

function applyFilter() {{
  const q       = document.getElementById('searchBox').value.toLowerCase();
  const stage   = document.getElementById('stageFilter').value;
  const country = document.getElementById('countryFilter').value;
  const owner   = document.getElementById('ownerFilter').value;

  const allRows = [...document.querySelectorAll('#allTable tbody tr.data-row')];
  const matching = allRows.filter(tr => {{
    const okTab     = rowMatchesTab(tr, _activeTab);
    const okSearch  = !q       || tr.dataset.search.includes(q);
    const okStage   = !stage   || tr.dataset.stage   === stage;
    const okCountry = !country || tr.dataset.country === country;
    const okOwner   = !owner   || tr.dataset.owner   === owner;
    return okTab && okSearch && okStage && okCountry && okOwner;
  }});

  // hide all
  allRows.forEach(tr => {{
    tr.classList.add('hidden');
    const det = tr.nextElementSibling;
    if (det?.classList.contains('detail-row')) det.classList.add('hidden');
  }});

  // show current page
  const totalPages = Math.max(1, Math.ceil(matching.length / PAGE_SIZE));
  if (_currentPage > totalPages) _currentPage = totalPages;
  const start = (_currentPage - 1) * PAGE_SIZE;
  matching.slice(start, start + PAGE_SIZE).forEach(tr => tr.classList.remove('hidden'));

  document.getElementById('countDisplay').textContent =
    matching.length + '件 / ' + allRows.length + '件中';
  renderPagination(matching.length);
}}

function renderPagination(total) {{
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const el = document.getElementById('pagination');
  el.innerHTML = '';

  const prev = document.createElement('button');
  prev.className = 'page-btn'; prev.textContent = '← 前へ';
  prev.disabled = _currentPage <= 1;
  prev.onclick = () => {{ _currentPage--; applyFilter(); }};
  el.appendChild(prev);

  const maxBtns = 7;
  let startP = Math.max(1, _currentPage - Math.floor(maxBtns/2));
  let endP   = Math.min(totalPages, startP + maxBtns - 1);
  if (endP - startP < maxBtns - 1) startP = Math.max(1, endP - maxBtns + 1);
  for (let p = startP; p <= endP; p++) {{
    const btn = document.createElement('button');
    btn.className = 'page-btn' + (p === _currentPage ? ' active' : '');
    btn.textContent = p;
    const pg = p;
    btn.onclick = () => {{ _currentPage = pg; applyFilter(); }};
    el.appendChild(btn);
  }}

  const next = document.createElement('button');
  next.className = 'page-btn'; next.textContent = '次へ →';
  next.disabled = _currentPage >= totalPages;
  next.onclick = () => {{ _currentPage++; applyFilter(); }};
  el.appendChild(next);
}}

// ── table: row detail ──
function toggleDetail(tr) {{
  const detail = tr.nextElementSibling;
  if (detail && detail.classList.contains('detail-row')) {{
    detail.classList.toggle('hidden');
    tr.classList.toggle('expanded');
  }}
}}

// ── table: sort ──
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
    return _sortAsc ? av.localeCompare(bv,'ja') : bv.localeCompare(av,'ja');
  }});
  pairs.forEach(([dr,dtr]) => {{
    tbody.appendChild(dr);
    if (dtr?.classList.contains('detail-row')) tbody.appendChild(dtr);
  }});
  _currentPage = 1;
  applyFilter();
}}

window.addEventListener('DOMContentLoaded', () => {{
  buildDropdowns();
  applyFilter();
}});
"""

# ── HTML ─────────────────────────────────────────────────
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
    <button class="member-btn" data-member="takayuki"    onclick="selectMember('takayuki')">takayuki</button>
    <button class="member-btn" data-member="kayo.tatara" onclick="selectMember('kayo.tatara')">kayo.tatara</button>
    <button class="member-btn" data-member="kataoka"     onclick="selectMember('kataoka')">kataoka</button>
  </div>
  <div id="memberPlaceholder" class="member-placeholder">担当者を選択してください</div>
  {member_sections_html}
</section>

<section>
  <h2>ステージ別件数</h2>
  <div class="chart-card">
    <div style="position:relative;height:300px"><canvas id="stageChart"></canvas></div>
  </div>
</section>

<section>
  <h2>国別分布（上位10）</h2>
  <div class="chart-card">
    <div style="position:relative;height:260px"><canvas id="countryChart"></canvas></div>
  </div>
</section>

<section>
  <h2>全案件テーブル</h2>
  <div class="tab-bar">
    <button class="tab-btn active" data-filter="all"      onclick="setTab('all')">全件</button>
    <button class="tab-btn"        data-filter="today"    onclick="setTab('today')">📌 今日対応</button>
    <button class="tab-btn"        data-filter="highprio" onclick="setTab('highprio')">🟢 高優先度</button>
    <button class="tab-btn"        data-filter="overdue"  onclick="setTab('overdue')">🔴 期限超過</button>
    <button class="tab-btn"        data-filter="sample"   onclick="setTab('sample')">🔵 サンプル後フォロー</button>
    <button class="tab-btn"        data-filter="estimate" onclick="setTab('estimate')">🟡 見積後フォロー</button>
    <button class="tab-btn"        data-filter="shipping" onclick="setTab('shipping')">📦 出荷確認</button>
  </div>
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
  <div class="pagination" id="pagination"></div>
</section>

</main>
<script>{JS}</script>
</body>
</html>"""

out_path = 'index.html'
with open(out_path, 'w', encoding='utf-8') as f:
    f.write(html)

print(f"\n出力完了: {out_path}")
print(f"今日対応case_id数: {len(today_cids)}件")
for m in MEMBERS:
    k = kpi[m]
    print(f"  [{m}] 担当:{k['total']}件 / 今日優先:{k['prio_total']}件 / 高優先:{k['prio_high']}件")
