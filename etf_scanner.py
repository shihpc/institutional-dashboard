#!/usr/bin/env python3
"""
法人儀表板 - 投信外資ETF掃描器
產出4個Master表的JSON，供 index.html 顯示：
  etf    : 非債券型/債券型 ETF × 交易量/市值/三大法人買賣/匯總
  trust  : 全股投信 近1/5/10日 買賣超 + 持股 + 成交量佔比
  foreign: 全股外資 近1/5/10日 買賣超 + 持股 + 成交量佔比
  sync   : 投信+外資同步 近1/5日
"""
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

TOKEN = os.environ.get('FINMIND_TOKEN', '')
BASE  = 'https://api.finmindtrade.com/api/v4/data'
SLEEP = 0.1   # 付費版 6000次/hr


# ── API ──────────────────────────────────────────────────────────

def _get(dataset, params, timeout=120):
    try:
        r = requests.get(BASE, params={'dataset': dataset, 'token': TOKEN, **params},
                         timeout=timeout)
        r.raise_for_status()
        d = r.json()
        if d.get('status') != 200:
            log.warning(f'{dataset} 非200: {d.get("msg")}')
            return pd.DataFrame()
        time.sleep(SLEEP)
        return pd.DataFrame(d.get('data', []))
    except Exception as e:
        log.error(f'{dataset} 失敗: {e}')
        return pd.DataFrame()


# ── 交易日 ────────────────────────────────────────────────────────

def get_trading_dates(n=25):
    today = datetime.today()
    start = (today - timedelta(days=n * 2)).strftime('%Y-%m-%d')
    end   = today.strftime('%Y-%m-%d')
    df = _get('TaiwanStockTradingDate', {'start_date': start, 'end_date': end})
    if not df.empty and 'date' in df.columns:
        dates = sorted(d[:10] for d in df['date'].tolist() if d[:10] <= end)
        return dates[-n:]
    result, d = [], today
    while len(result) < n:
        if d.weekday() < 5:
            result.append(d.strftime('%Y-%m-%d'))
        d -= timedelta(days=1)
    return list(reversed(result))


# ── 資料抓取 ──────────────────────────────────────────────────────

def fetch_stock_list():
    log.info('股票清單...')
    df = _get('TaiwanStockInfo', {})
    if not df.empty:
        df['stock_id'] = df['stock_id'].astype(str).str.strip()
    return df


def fetch_inst_by_date(date):
    df = _get('TaiwanStockInstitutionalInvestorsBuySell',
              {'start_date': date, 'end_date': date})
    if df.empty:
        return df
    df['stock_id'] = df['stock_id'].astype(str).str.strip()
    for c in ['buy', 'sell']:
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)
    if 'date' in df.columns:
        df = df[df['date'].astype(str).str[:10] == date]
    return df


def fetch_inst_history(dates):
    log.info(f'法人資料 {dates[0]}~{dates[-1]} ({len(dates)}日)...')
    result = {}
    for d in dates:
        df = fetch_inst_by_date(d)
        if not df.empty:
            result[d] = df
    log.info(f'  取得 {len(result)} 個交易日法人資料')
    return result


def fetch_price_history(dates):
    log.info(f'股價 {dates[0]}~{dates[-1]} ({len(dates)}日)...')
    result = {}
    for d in dates:
        df = _get('TaiwanStockPrice', {'start_date': d, 'end_date': d})
        if df.empty:
            continue
        df['stock_id'] = df['stock_id'].astype(str).str.strip()
        for c in ['Trading_Volume', 'Trading_money', 'close', 'spread']:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)
        result[d] = df
    log.info(f'  取得 {len(result)} 個交易日股價')
    return result


def fetch_shareholding(date):
    log.info(f'外資持股比率 {date}...')
    df = _get('TaiwanStockShareholding', {'start_date': date, 'end_date': date})
    if df.empty:
        return df
    df['stock_id'] = df['stock_id'].astype(str).str.strip()
    if 'date' in df.columns:
        df = df[df['date'].astype(str).str[:10] == date]
    return df


# ── ETF 識別 ──────────────────────────────────────────────────────

BOND_KEYWORDS = ['債', 'Bond', 'bond', '公債', '投資級', '高收益', '新興債']


def identify_etfs(stock_df, all_stock_ids):
    """
    白名單過濾後，代號以 '0' 開頭的即為 ETF（台灣ETF代號規則）。
    後綴可為 B/L/R/A/U/K 等任意字母，統一以債券關鍵字判斷類型。
    """
    name_map = {}
    if not stock_df.empty and 'stock_name' in stock_df.columns:
        name_map = dict(zip(stock_df['stock_id'], stock_df['stock_name']))

    non_bond, bond = set(), set()
    for sid in all_stock_ids:
        if not sid.startswith('0'):
            continue
        name = name_map.get(sid, '')
        is_bond = sid.endswith('B') or any(k in name for k in BOND_KEYWORDS)
        if is_bond:
            bond.add(sid)
        else:
            non_bond.add(sid)

    log.info(f'ETF：非債券型 {len(non_bond)} 支，債券型 {len(bond)} 支')
    return non_bond, bond


# ── 聚合工具 ──────────────────────────────────────────────────────

NAME_TO_GRP = {
    'Foreign_Investor':    'foreign',
    'Foreign_Dealer_Self': 'foreign',
    'Investment_Trust':    'trust',
    'Dealer_self':         'dealer',
    'Dealer_Hedging':      'dealer',
}


def aggregate_inst(inst_history, dates_subset):
    dfs = [inst_history[d] for d in dates_subset if d in inst_history]
    if not dfs:
        return pd.DataFrame()
    all_df = pd.concat(dfs, ignore_index=True)
    all_df['grp'] = all_df['name'].map(NAME_TO_GRP)
    all_df = all_df.dropna(subset=['grp'])

    buy_df  = all_df.groupby(['stock_id', 'grp'])['buy'].sum().unstack(fill_value=0)
    sell_df = all_df.groupby(['stock_id', 'grp'])['sell'].sum().unstack(fill_value=0)
    buy_df.columns  = [f'{c}_buy'  for c in buy_df.columns]
    sell_df.columns = [f'{c}_sell' for c in sell_df.columns]
    result = pd.concat([buy_df, sell_df], axis=1).fillna(0)
    for col in ['foreign_buy','foreign_sell','trust_buy','trust_sell','dealer_buy','dealer_sell']:
        if col not in result.columns:
            result[col] = 0.0
    return result


def compute_price_metrics(price_history, dates_all, dates_d5, dates_d10):
    date_latest = dates_all[-1]
    price_latest = price_history.get(date_latest, pd.DataFrame())
    metrics = {}

    if not price_latest.empty:
        for _, r in price_latest.iterrows():
            sid    = r['stock_id']
            close  = float(r.get('close', 0) or 0)
            spread = float(r.get('spread', 0) or 0)
            prev   = close - spread
            chg    = round(spread / prev * 100, 2) if prev > 0 else 0
            metrics[sid] = {
                'close':       close,
                'close_chg':   chg,
                'vol_d1':      float(r.get('Trading_Volume', 0) or 0),
                'money_d1':    float(r.get('Trading_money', 0) or 0),
                'ma20_bias':   0.0,
                'vol_d5_avg':  0.0,
                'vol_d10_avg': 0.0,
            }

    recent20 = [price_history[d] for d in dates_all[-20:] if d in price_history]
    if recent20:
        ma20_df  = pd.concat(recent20, ignore_index=True)
        ma20_avg = ma20_df.groupby('stock_id')['close'].mean()
        for sid, ma in ma20_avg.items():
            if sid in metrics and ma > 0:
                metrics[sid]['ma20_bias'] = round(
                    (metrics[sid]['close'] - float(ma)) / float(ma) * 100, 2)

    def set_vol_avg(dates, key):
        dfs = [price_history[d] for d in dates if d in price_history]
        if not dfs:
            return
        avg = pd.concat(dfs, ignore_index=True).groupby('stock_id')['Trading_Volume'].mean()
        for sid, v in avg.items():
            if sid in metrics:
                metrics[sid][key] = float(v or 0)

    set_vol_avg(dates_d5,  'vol_d5_avg')
    set_vol_avg(dates_d10, 'vol_d10_avg')
    return metrics


# ── 建立各Master表 ────────────────────────────────────────────────

def _f(v, digits=None):
    if v is None:
        return 0
    v = float(v)
    if v != v:   # NaN
        return 0
    return round(v, digits) if digits is not None else v


def _gi(agg, sid, col):
    if agg.empty or sid not in agg.index or col not in agg.columns:
        return 0.0
    return _f(agg.loc[sid, col])


def build_etf_items(agg_d1, price_metrics, non_bond_ids, bond_ids, name_map):
    all_etf = non_bond_ids | bond_ids
    items = []
    for sid in all_etf:
        pm = price_metrics.get(sid, {})
        fb = _gi(agg_d1, sid, 'foreign_buy');  fs = _gi(agg_d1, sid, 'foreign_sell')
        tb = _gi(agg_d1, sid, 'trust_buy');    ts = _gi(agg_d1, sid, 'trust_sell')
        db = _gi(agg_d1, sid, 'dealer_buy');   ds = _gi(agg_d1, sid, 'dealer_sell')
        items.append({
            'stock_id':    sid,
            'stock_name':  name_map.get(sid, ''),
            'bond':        sid in bond_ids,
            'close':       _f(pm.get('close'), 2),
            'volume':      int(_f(pm.get('vol_d1'))),
            'money_b':     round(_f(pm.get('money_d1')) / 1e8, 2),
            'foreign_buy': int(fb), 'foreign_sell': int(fs), 'foreign_net': int(fb - fs),
            'trust_buy':   int(tb), 'trust_sell':   int(ts), 'trust_net':   int(tb - ts),
            'dealer_buy':  int(db), 'dealer_sell':  int(ds), 'dealer_net':  int(db - ds),
        })
    items.sort(key=lambda x: x['volume'], reverse=True)
    for i, it in enumerate(items, 1):
        it['rank'] = i
    return items


def build_etf_summary(agg_d1, price_metrics, non_bond_ids, bond_ids):
    def calc(ids, label):
        vol = sum(int(_f(price_metrics.get(s, {}).get('vol_d1'))) for s in ids)
        fb = sum(_gi(agg_d1, s, 'foreign_buy') for s in ids)
        tb = sum(_gi(agg_d1, s, 'trust_buy')   for s in ids)
        db = sum(_gi(agg_d1, s, 'dealer_buy')  for s in ids)
        fn = sum(_gi(agg_d1, s, 'foreign_buy') - _gi(agg_d1, s, 'foreign_sell') for s in ids)
        tn = sum(_gi(agg_d1, s, 'trust_buy')   - _gi(agg_d1, s, 'trust_sell')   for s in ids)
        dn = sum(_gi(agg_d1, s, 'dealer_buy')  - _gi(agg_d1, s, 'dealer_sell')  for s in ids)
        return {'label': label, 'volume': vol,
                'foreign_buy': int(fb), 'foreign_net': int(fn),
                'trust_buy':   int(tb), 'trust_net':   int(tn),
                'dealer_buy':  int(db), 'dealer_net':  int(dn)}

    return [
        calc(non_bond_ids | bond_ids, '整體ETF'),
        calc(non_bond_ids,            '非債券型'),
        calc(bond_ids,                '債券型'),
    ]


def build_entity_items(agg_d1, agg_d5, agg_d10,
                       price_metrics, foreign_ratio,
                       all_etf_ids, name_map, entity='trust'):
    if agg_d1.empty:
        return []
    bk = f'{entity}_buy'
    sk = f'{entity}_sell'
    candidate_ids = set(agg_d1.index) - all_etf_ids

    items = []
    for sid in candidate_ids:
        b1 = _gi(agg_d1, sid, bk);  s1 = _gi(agg_d1, sid, sk)
        b5 = _gi(agg_d5, sid, bk);  s5 = _gi(agg_d5, sid, sk)
        b10= _gi(agg_d10,sid, bk);  s10= _gi(agg_d10,sid, sk)
        pm = price_metrics.get(sid, {})
        v1 = _f(pm.get('vol_d1'));  v5 = _f(pm.get('vol_d5_avg'));  v10 = _f(pm.get('vol_d10_avg'))

        def vr(diff, vol, days=1):
            total = vol * days
            return round(abs(diff) / total * 100, 2) if total > 0 else 0

        items.append({
            'stock_id':     sid,
            'stock_name':   name_map.get(sid, ''),
            'close':        _f(pm.get('close'), 2),
            'close_chg':    _f(pm.get('close_chg'), 2),
            'ma20_bias':    _f(pm.get('ma20_bias'), 2),
            'foreign_ratio': _f(foreign_ratio.get(sid), 2) if entity == 'foreign' else None,
            'd1': {'buy': int(b1), 'sell': int(s1), 'diff': int(b1 - s1),
                   'vol_ratio': vr(b1 - s1, v1, 1)},
            'd5': {'buy': int(b5), 'sell': int(s5), 'diff': int(b5 - s5),
                   'vol_ratio': vr(b5 - s5, v5, 5)},
            'd10':{'buy': int(b10),'sell': int(s10),'diff': int(b10 - s10),
                   'vol_ratio': vr(b10 - s10, v10, 10)},
        })

    items.sort(key=lambda x: x['d1']['diff'], reverse=True)
    for i, it in enumerate(items, 1):
        it['rank'] = i
    return items


def build_sync_items(agg_d1, agg_d5, price_metrics, all_etf_ids, name_map):
    if agg_d1.empty:
        return []
    sids = set(agg_d1.index) - all_etf_ids
    items = []
    for sid in sids:
        td1 = _gi(agg_d1, sid, 'trust_buy')   - _gi(agg_d1, sid, 'trust_sell')
        fd1 = _gi(agg_d1, sid, 'foreign_buy') - _gi(agg_d1, sid, 'foreign_sell')
        td5 = _gi(agg_d5, sid, 'trust_buy')   - _gi(agg_d5, sid, 'trust_sell')
        fd5 = _gi(agg_d5, sid, 'foreign_buy') - _gi(agg_d5, sid, 'foreign_sell')
        pm  = price_metrics.get(sid, {})
        items.append({
            'stock_id':   sid,
            'stock_name': name_map.get(sid, ''),
            'close':      _f(pm.get('close'), 2),
            'close_chg':  _f(pm.get('close_chg'), 2),
            'd1': {'trust': int(td1), 'foreign': int(fd1), 'total': int(td1 + fd1)},
            'd5': {'trust': int(td5), 'foreign': int(fd5), 'total': int(td5 + fd5)},
        })
    items.sort(key=lambda x: x['d1']['total'], reverse=True)
    for i, it in enumerate(items, 1):
        it['rank'] = i
    return items


# ── 主程式 ────────────────────────────────────────────────────────

def main():
    log.info('=== 法人儀表板掃描開始 ===')

    dates_all   = get_trading_dates(25)
    dates_d10   = dates_all[-10:]
    dates_d5    = dates_all[-5:]
    date_latest = dates_all[-1]
    log.info(f'目標日期：{date_latest}，範圍：{dates_all[0]} ~ {date_latest}')

    stock_df     = fetch_stock_list()
    inst_history = fetch_inst_history(dates_d10)
    price_history= fetch_price_history(dates_all)
    share_df     = fetch_shareholding(date_latest)

    if not inst_history:
        log.error('法人資料空白，終止')
        sys.exit(1)

    name_map      = (dict(zip(stock_df['stock_id'], stock_df['stock_name']))
                     if not stock_df.empty and 'stock_name' in stock_df.columns else {})
    foreign_ratio = {}
    if not share_df.empty:
        ratio_col = next((c for c in share_df.columns if 'Ratio' in c or 'ratio' in c), None)
        if ratio_col:
            foreign_ratio = dict(zip(
                share_df['stock_id'],
                pd.to_numeric(share_df[ratio_col], errors='coerce').fillna(0)))

    # 以 TaiwanStockInfo 為白名單，過濾掉權證/期貨等非正式掛牌商品
    listed_ids = set(stock_df['stock_id'].tolist()) if not stock_df.empty else set()
    all_stock_ids = set()
    for df in inst_history.values():
        all_stock_ids |= set(df['stock_id'].tolist())
    if listed_ids:
        before = len(all_stock_ids)
        all_stock_ids &= listed_ids
        log.info(f'白名單過濾：{before} → {len(all_stock_ids)} 支（移除 {before - len(all_stock_ids)} 支權證/期貨）')

    non_bond_ids, bond_ids = identify_etfs(stock_df, all_stock_ids)
    all_etf_ids = non_bond_ids | bond_ids

    agg_d1  = aggregate_inst(inst_history, dates_d10[-1:])
    agg_d5  = aggregate_inst(inst_history, dates_d5)
    agg_d10 = aggregate_inst(inst_history, dates_d10)

    # 將 agg 結果也套用白名單，去除權證/期貨
    for _agg in (agg_d1, agg_d5, agg_d10):
        if not _agg.empty:
            _agg.drop(index=_agg.index.difference(all_stock_ids), inplace=True)

    price_metrics = compute_price_metrics(price_history, dates_all, dates_d5, dates_d10)

    log.info('建立輸出JSON...')
    output = {
        'date':       date_latest,
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'etf': {
            'summary': build_etf_summary(agg_d1, price_metrics, non_bond_ids, bond_ids),
            'items':   build_etf_items(agg_d1, price_metrics, non_bond_ids, bond_ids, name_map),
        },
        'trust':   build_entity_items(agg_d1, agg_d5, agg_d10,
                                       price_metrics, foreign_ratio,
                                       all_etf_ids, name_map, 'trust'),
        'foreign': build_entity_items(agg_d1, agg_d5, agg_d10,
                                       price_metrics, foreign_ratio,
                                       all_etf_ids, name_map, 'foreign'),
        'sync':    build_sync_items(agg_d1, agg_d5, price_metrics, all_etf_ids, name_map),
    }

    os.makedirs('data', exist_ok=True)
    with open('data/etf.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    log.info('=== 完成 ===')
    log.info(f'  ETF        : {len(output["etf"]["items"])} 支（非債券型{len(non_bond_ids)}/債券型{len(bond_ids)}）')
    log.info(f'  投信       : {len(output["trust"])} 支')
    log.info(f'  外資       : {len(output["foreign"])} 支')
    log.info(f'  同步買賣超 : {len(output["sync"])} 支')


if __name__ == '__main__':
    main()
