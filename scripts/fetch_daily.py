"""抓取上市、上櫃的每日行情與三大法人買賣超，存成 data/YYYY-MM-DD.json，並更新 data/index.json。

用法：
    python scripts/fetch_daily.py                       # 台北時間今天（順便每週更新一次產業分類）
    python scripts/fetch_daily.py 2026-10-01 ...        # 指定日期（可多個）
    python scripts/fetch_daily.py --since 2025-10-01    # 補抓：從昨天往回抓到該日，已有的日期跳過
        [--until 2026-09-30] [--max-minutes 40] [--delay 4]
    python scripts/fetch_daily.py --industry            # 只更新產業分類 data/industry.json

只用標準函式庫。非交易日（證交所回傳無資料）時不寫檔；三大法人抓不到時只警告，該欄存 null。
一般模式下行情抓不到會以錯誤結束，讓 GitHub Actions 標示失敗。

補抓模式會放慢請求、遇到阻擋時逐步拉長等待，連續失敗就停下保留進度。結束碼：
    0 = 範圍內已全部抓完   3 = 到達時間上限，還有沒抓的   4 = 疑似被交易所擋下，先停止
"""
import argparse
import datetime
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / 'data'
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; twstock-list-sorter)'}
CODE = re.compile(r'^[1-9]\d{3}$')        # 四位數普通股，與 index.html 的篩選相同
TAIPEI = datetime.timezone(datetime.timedelta(hours=8))

QUOTE_FIELDS = ['code', 'name', 'amount', 'shares', 'close', 'high', 'low', 'change']
INST_FIELDS = ['code', 'net']

DELAY = 3.0                                # 同一主機兩次請求的最短間隔（秒）；證交所要求 5 秒內不超過 3 次
_last_hit = {}


def throttle(url):
    host = urllib.parse.urlsplit(url).netloc
    wait = _last_hit.get(host, 0) + DELAY - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.monotonic()


def fetch(url, form=None, retries=3):
    body = urllib.parse.urlencode(form).encode() if form else None
    for attempt in range(retries):
        throttle(url)
        try:
            req = urllib.request.Request(url, data=body, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:                  # 連線逾時、被暫時擋下等，稍等重試
            if attempt == retries - 1:
                raise
            print(f'  重試（{e}）', file=sys.stderr)
            time.sleep(15 * (attempt + 1))


def fetch_json(url, form=None):
    raw = fetch(url, form)
    try:
        return json.loads(raw.decode('utf-8'))
    except ValueError:                          # 被擋時常回傳 HTML 警告頁
        raise RuntimeError('回應不是 JSON，可能被交易所暫時擋下') from None


def num(s):
    """'1,234'、'+53.00'、'-0.04 '、'--'、'<p style=...>+</p>' → float 或 None"""
    s = re.sub(r'<[^>]+>', '', str(s)).replace(',', '').strip()
    try:
        return float(s)
    except ValueError:
        return None


def whole(v):
    return None if v is None else int(v)


def columns(fields, names):
    missing = [n for n in names if n not in fields]
    if missing:
        raise RuntimeError(f'欄位不見了：{missing}，格式可能改版')
    return [fields.index(n) for n in names]


def find_table(tables, *names):
    return [t for t in tables if all(n in (t.get('fields') or []) for n in names)]


# ── 證交所 ──

def twse_quote(d):
    j = fetch_json(f'https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d:%Y%m%d}&type=ALLBUT0999&response=json')
    if j.get('stat') != 'OK':
        return None                              # 非交易日
    tables = find_table(j.get('tables', []), '證券代號', '成交金額')
    if not tables:
        raise RuntimeError('MI_INDEX 找不到個股行情表，格式可能改版')
    t = tables[0]
    ic, iname, ish, iamt, icl, ihi, ilo, isg, ich = columns(
        t['fields'], ['證券代號', '證券名稱', '成交股數', '成交金額', '收盤價', '最高價', '最低價', '漲跌(+/-)', '漲跌價差'])
    rows = []
    for r in t['data']:
        code = r[ic].strip()
        if not CODE.match(code):
            continue
        sign = re.sub(r'<[^>]+>', '', r[isg]).strip()
        change = num(r[ich])
        if change is not None:
            change = None if sign == 'X' else -change if sign == '-' else change   # X 為不比價
        rows.append([code, r[iname].strip(), whole(num(r[iamt])), whole(num(r[ish])),
                     num(r[icl]), num(r[ihi]), num(r[ilo]), change])
    return rows


def twse_inst(d):
    j = fetch_json(f'https://www.twse.com.tw/rwd/zh/fund/T86?date={d:%Y%m%d}&selectType=ALLBUT0999&response=json')
    if j.get('stat') != 'OK':
        return None
    ic, iv = columns(j['fields'], ['證券代號', '三大法人買賣超股數'])
    return [[r[ic].strip(), whole(num(r[iv]))] for r in j['data'] if CODE.match(r[ic].strip())]


# ── 櫃買中心 ──

def tpex_post(path, form, d):
    j = fetch_json('https://www.tpex.org.tw' + path, {**form, 'date': f'{d:%Y/%m/%d}', 'id': '', 'response': 'json'})
    if str(j.get('stat', '')).lower() != 'ok' or j.get('date') != f'{d:%Y%m%d}':
        return None
    return j.get('tables', [])


def tpex_quote(d):
    tables = tpex_post('/www/zh-tw/afterTrading/dailyQuotes', {}, d)
    if tables is None:
        return None
    tables = find_table(tables, '代號', '成交金額(元)')       # 一般股票與管理股票兩張表
    if not tables:
        raise RuntimeError('上櫃行情找不到個股表，格式可能改版')
    rows = []
    for t in tables:
        ic, iname, icl, ich, ihi, ilo, ish, iamt = columns(
            t['fields'], ['代號', '名稱', '收盤', '漲跌', '最高', '最低', '成交股數', '成交金額(元)'])
        for r in t.get('data') or []:
            code = r[ic].strip()
            if CODE.match(code):
                rows.append([code, r[iname].strip(), whole(num(r[iamt])), whole(num(r[ish])),
                             num(r[icl]), num(r[ihi]), num(r[ilo]), num(r[ich])])
    return rows


def tpex_inst(d):
    tables = tpex_post('/www/zh-tw/insti/dailyTrade', {'type': 'Daily', 'sect': 'EW'}, d)
    if tables is None:
        return None
    tables = find_table(tables, '代號', '三大法人買賣超股數合計')
    if not tables:
        return None
    ic, iv = columns(tables[0]['fields'], ['代號', '三大法人買賣超股數合計'])
    return [[r[ic].strip(), whole(num(r[iv]))] for r in tables[0]['data'] if CODE.match(r[ic].strip())]


# ── 產業分類：證交所 ISIN 證券代碼表（上市 strMode=2、上櫃 strMode=4），表內第 5 欄為產業別 ──

def fetch_industry():
    mapping = {}
    for mode in ('2', '4'):
        html = fetch(f'https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}').decode('cp950', errors='replace')
        for tr in re.findall(r'<tr>(.*?)</tr>', html, re.S):
            tds = [re.sub(r'<[^>]+>', '', td).strip() for td in re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)]
            if len(tds) < 5:
                continue
            m = re.match(r'^([1-9]\d{3})\s', tds[0].replace('　', ' '))
            if m:
                mapping[m.group(1)] = tds[4] or '未分類'           # DR 等沒有產業別
    if len(mapping) < 1500:
        raise RuntimeError(f'產業分類只解析出 {len(mapping)} 檔，格式可能改版')
    DATA.mkdir(exist_ok=True)
    today = datetime.datetime.now(TAIPEI).date()
    (DATA / 'industry.json').write_text(json.dumps(
        {'updated': f'{today:%Y-%m-%d}', 'map': dict(sorted(mapping.items()))},
        ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(f'產業分類：{len(mapping)} 檔、{len(set(mapping.values()))} 類')


def industry_stale(days=7):
    p = DATA / 'industry.json'
    if not p.exists():
        return True
    try:
        updated = datetime.date.fromisoformat(json.loads(p.read_text(encoding='utf-8'))['updated'])
    except (ValueError, KeyError):
        return True
    return (datetime.datetime.now(TAIPEI).date() - updated).days >= days


# ── 主流程 ──

def optional(label, fn, d):
    try:
        rows = fn(d)
    except Exception as e:
        print(f'  警告：{label}抓取失敗（{e}），先存成 null', file=sys.stderr)
        return None
    if rows is None:
        print(f'  警告：{label}尚未公布，先存成 null', file=sys.stderr)
    return rows


def day_path(d):
    return DATA / f'{d:%Y-%m-%d}.json'


def complete(d):
    """這天的檔案存在，且兩邊的三大法人都有資料"""
    p = day_path(d)
    if not p.exists():
        return False
    j = json.loads(p.read_text(encoding='utf-8'))
    return all(j.get(m, {}).get('inst') for m in ('TWSE', 'TPEX'))


def fetch_day(d):
    """抓一天。回傳 True = 已寫檔，False = 非交易日。行情抓不到時丟出例外"""
    print(f'{d:%Y-%m-%d}')
    tq = twse_quote(d)
    if tq is None:
        print('  證交所沒有這天的資料（非交易日），略過')
        return False
    ti = optional('上市三大法人', twse_inst, d)
    tp = tpex_quote(d)
    if tp is None:
        raise RuntimeError('證交所有資料，但櫃買沒有這天的行情')
    pi = optional('上櫃三大法人', tpex_inst, d)

    day = {
        'date': f'{d:%Y-%m-%d}',
        'fields': {'quote': QUOTE_FIELDS, 'inst': INST_FIELDS},
        'TWSE': {'quote': tq, 'inst': ti},
        'TPEX': {'quote': tp, 'inst': pi},
    }
    DATA.mkdir(exist_ok=True)
    day_path(d).write_text(json.dumps(day, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(f'  上市 {len(tq)} 檔、法人 {len(ti) if ti else "無"}；上櫃 {len(tp)} 檔、法人 {len(pi) if pi else "無"}')
    return True


# ── 歷史指標：由前幾個交易日的資料算出，存成 data/hist/YYYY-MM-DD.json ──
# 只用 data/ 裡已有的日期；前面的資料不夠時該欄為 null。價格未還原除權息，跨除權息日的漲幅會偏低

HIST = DATA / 'hist'
HIST_FIELDS = ['code', 'volRatio', 'ret5', 'ret20', 'streak', 'inst5',
               'gap20', 'gap60', 'gap240', 'gapAll', 'maxClose']
# gapN：收盤價距「前 N 個交易日最高收盤」幾 %，正數 = 創新高且高出幾 %，負數 = 離前高還差幾 %。
# gapAll 用資料期間內的最高收盤（目前能判斷的歷史新高）；maxClose 為含當天的最高收盤，供下次接續計算
GAP_WINDOWS = (20, 60, 240)
_day_cache = {}


def trading_dates():
    return sorted(p.stem for p in DATA.glob('*.json') if re.fullmatch(r'\d{4}-\d{2}-\d{2}', p.stem))


def load_day(ds):
    """回傳 {code: (amount, close, net)}；net 為 None 表示該市場當天沒有法人資料"""
    if ds not in _day_cache:
        j = json.loads((DATA / f'{ds}.json').read_text(encoding='utf-8'))
        out = {}
        for m in ('TWSE', 'TPEX'):
            inst = j[m].get('inst')
            nets = dict(inst) if inst else None
            for code, _name, amount, _sh, close, *_ in j[m]['quote']:
                out[code] = (amount, close, None if nets is None else nets.get(code, 0))
        _day_cache[ds] = out
    return _day_cache[ds]


def build_hist(dates, i, gaps):
    """gaps：{code: [gap20, gap60, gap240, gapAll, maxClose]}，由 ensure_hist 依序累積算出"""
    today = load_day(dates[i])
    prev = [load_day(ds) for ds in dates[max(0, i - 20):i]]       # 前 20 個交易日，舊 → 新
    rows = []
    for code, (amount, close, net) in today.items():
        # 量比：今天成交金額 ÷ 前 20 日平均（至少要有 10 天）
        past = [d[code][0] for d in prev if code in d and d[code][0]]
        vol_ratio = round(amount / (sum(past) / len(past)), 2) if len(past) >= 10 and amount else None

        def ret(n):
            if i < n or close is None:
                return None
            old = load_day(dates[i - n]).get(code)
            return round((close / old[1] - 1) * 100, 2) if old and old[1] else None

        # 法人連續買超（正）或賣超（負）天數，往前數到方向改變或沒有資料為止（最多看 60 天）
        streak = 0
        if net:
            sign = 1 if net > 0 else -1
            for k in range(i, max(-1, i - 60), -1):
                n = load_day(dates[k]).get(code, (None, None, None))[2]
                if n is None or n * sign <= 0:
                    break
                streak += sign

        # 法人 5 日買超金額：最近 5 天（含今天）買賣超股數 × 當天收盤，至少要有 3 天
        days5 = [today] + prev[::-1][:4]
        # 收盤先換成「分」的整數再相乘加總，避免浮點誤差讓不同機器算出差 1 元的結果
        vals = [d[code][2] * round(d[code][1] * 100) for d in days5 if code in d and d[code][2] is not None and d[code][1]]
        inst5 = round(sum(vals) / 100) if len(vals) >= 3 else None

        rows.append([code, vol_ratio, ret(5), ret(20), streak if net is not None else None, inst5]
                    + gaps.get(code, [None] * 5))
    HIST.mkdir(parents=True, exist_ok=True)
    (HIST / f'{dates[i]}.json').write_text(json.dumps(
        {'date': dates[i], 'fields': HIST_FIELDS, 'rows': rows}, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')


def _seed_max_close(ds):
    """讀某天 hist 檔的 maxClose；舊格式或沒有檔案回傳 None"""
    p = HIST / f'{ds}.json'
    if not p.exists():
        return None
    j = json.loads(p.read_text(encoding='utf-8'))
    if 'maxClose' not in j['fields']:
        return None
    k = j['fields'].index('maxClose')
    return {r[0]: r[k] for r in j['rows'] if r[k] is not None}


def ensure_hist(rebuild=False, recent=25):
    """補上缺少的歷史指標檔。新抓到的日期會影響之後的指標，所以最近 recent 天一律重算。
    由舊到新走過每個交易日，用單調佇列維護各期間的最高收盤，資料再多也只需走一遍"""
    from collections import deque
    dates = trading_dates()
    if rebuild:
        targets = set(range(len(dates)))
    else:
        targets = {i for i, ds in enumerate(dates)
                   if i >= len(dates) - recent or not (HIST / f'{ds}.json').exists()}
    if not targets:
        return
    start = 0 if rebuild else max(0, min(targets) - max(GAP_WINDOWS))
    ath = {}
    if start > 0:
        seed = _seed_max_close(dates[start - 1])
        if seed is None:
            start = 0                            # 沒有可接續的紀錄，從頭算
        else:
            ath = seed
    windows = {n: {} for n in GAP_WINDOWS}       # n → {code: deque[(日期索引, 收盤)]，收盤遞減}

    for i in range(start, len(dates)):
        day = load_day(dates[i])
        if i in targets:
            gaps = {}
            for code, (_amt, close, _net) in day.items():
                g = []
                for n in GAP_WINDOWS:
                    dq = windows[n].get(code)
                    while dq and dq[0][0] < i - n:
                        dq.popleft()
                    g.append(round((close / dq[0][1] - 1) * 100, 2) if i >= n and dq and close else None)
                prev_ath = ath.get(code)
                g.append(round((close / prev_ath - 1) * 100, 2) if prev_ath and close else None)
                g.append(max(prev_ath or 0, close or 0) or None)
                gaps[code] = g
            build_hist(dates, i, gaps)
        for code, (_amt, close, _net) in day.items():
            if not close:
                continue
            ath[code] = max(ath.get(code, close), close)
            for n in GAP_WINDOWS:
                dq = windows[n].setdefault(code, deque())
                while dq and dq[-1][1] <= close:
                    dq.pop()
                dq.append((i, close))
        if i >= 70:
            _day_cache.pop(dates[i - 70], None)  # 只需保留最近 60 多天，避免多年資料吃光記憶體
    print(f'歷史指標：更新 {len(targets)} 天')


def write_index():
    dates = sorted((p.stem for p in DATA.glob('*.json') if re.fullmatch(r'\d{4}-\d{2}-\d{2}', p.stem)), reverse=True)
    (DATA / 'index.json').write_text(json.dumps({'dates': dates}, separators=(',', ':')), encoding='utf-8')


def backfill(since, until, max_minutes):
    """由新到舊逐日補抓。失敗時等 1、3、10 分鐘再試，連續 3 天失敗就停"""
    deadline = time.monotonic() + max_minutes * 60
    d, fails, done = until, 0, 0
    while d >= since:
        if d.weekday() < 5 and not complete(d):
            if time.monotonic() > deadline:
                print(f'到達 {max_minutes} 分鐘上限，本次補了 {done} 天，下次從 {d} 繼續')
                return 3
            try:
                if fetch_day(d):
                    done += 1
                fails = 0
            except Exception as e:
                fails += 1
                print(f'  失敗（{e}）', file=sys.stderr)
                if fails >= 3:
                    print(f'連續 {fails} 天失敗，可能被交易所擋下，先停止。本次補了 {done} 天', file=sys.stderr)
                    return 4
                time.sleep([60, 180, 600][fails - 1])
                continue                         # 同一天再試
        d -= datetime.timedelta(days=1)
    print(f'補抓完成，本次補了 {done} 天')
    return 0


def main():
    global DELAY
    ap = argparse.ArgumentParser()
    ap.add_argument('dates', nargs='*', help='YYYY-MM-DD')
    ap.add_argument('--since', type=datetime.date.fromisoformat, help='補抓的最早日期')
    ap.add_argument('--until', type=datetime.date.fromisoformat, help='補抓的最晚日期（預設昨天）')
    ap.add_argument('--max-minutes', type=float, default=40)
    ap.add_argument('--delay', type=float, default=None, help='同一主機請求間隔秒數（補抓預設 4）')
    ap.add_argument('--industry', action='store_true', help='只更新產業分類')
    ap.add_argument('--hist', action='store_true', help='只重算全部的歷史指標')
    a = ap.parse_args()

    if a.industry:
        fetch_industry()
        return 0

    if a.hist:
        ensure_hist(rebuild=True)
        return 0

    if a.since:
        DELAY = a.delay or 4.0
        today = datetime.datetime.now(TAIPEI).date()
        rc = backfill(a.since, a.until or today - datetime.timedelta(days=1), a.max_minutes)
        if DATA.exists():
            write_index()
            ensure_hist(rebuild=True)            # 補進舊日期會影響之後每一天的指標
        return rc

    if a.delay:
        DELAY = a.delay
    days = [datetime.date.fromisoformat(s) for s in a.dates] or [datetime.datetime.now(TAIPEI).date()]
    for d in days:
        fetch_day(d)
    if DATA.exists():
        write_index()
        ensure_hist(rebuild=bool(a.dates))      # 指定日期重抓時可能是舊日期，全部重算
    if not a.dates and industry_stale():
        try:
            fetch_industry()
        except Exception as e:                   # 產業分類失敗不影響當天行情
            print(f'警告：產業分類更新失敗（{e}）', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
