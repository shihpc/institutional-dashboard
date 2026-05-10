#!/usr/bin/env python3
"""
法人儀表板 v2 - 38 個子表單 JSON
  ETF    6: 非債/債 × 成交量/市值排行 + 2 張匯總
  投信  12: 近1/5/20日 × 買超/賣超 × 金額/量比
  外資  12: 同上 + 外資持股欄位
  同步   8: 近1/5日 × 買/賣 + 投信/外資量比 × 近1/5日
"""
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

TOKEN = os.environ.get('FINMIND_TOKEN', '')
BASE  = 'https://api.finmindtrade.com/api/v4/data'
SLEEP = 0.1
TOP_N = 20

# ── API ──────────────────────────────────────────────────────────────────────

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


# ── 交易日 ────────────────────────────────────────────────────────────────────

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


# ── 資料抓取 ─────────────────────────────────────────────────────────────────

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
    log.info(f'外資持股 {date}...')
    df = _get('TaiwanStockShareholding', {'start_date': date, 'end_date': date})
    if df.empty:
        return df
    df['stock_id'] = df['stock_id'].astype(str).str.strip()
    if 'date' in df.columns:
        df = df[df['date'].astype(str).str[:10] == date]
    for c in ['ForeignInvestmentShares', 'ForeignInvestmentSharesRatio', 'NumberOfSharesIssued']:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)
    return df


def check_data_available(date):
    df = _get('TaiwanStockInstitutionalInvestorsBuySell',
              {'start_date': date, 'end_date': date, 'data_id': '2330'})
    if df.empty:
        return False
    if 'date' in df.columns:
        return not df[df['date'].astype(str).str[:10] == date].empty
    return True


# ── ETF 識別 ─────────────────────────────────────────────────────────────────

BOND_KEYWORDS = ['債', 'Bond', 'bond', '公債', '投資級', '高收益', '新興債']


def identify_etfs(stock_df, all_stock_ids):
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


# ── 聚合 ─────────────────────────────────────────────────────────────────────

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
    for col in ['foreign_buy', 'foreign_sell', 'trust_buy', 'trust_sell', 'dealer_buy', 'dealer_sell']:
        if col not in result.columns:
            result[col] = 0.0
    return result


# ── 價格指標 ─────────────────────────────────────────────────────────────────

def compute_price_metrics(price_history, dates_all, dates_d5, dates_d20):
    date_latest  = dates_all[-1]
    price_latest = price_history.get(date_latest, pd.DataFrame())
    metrics = {}

    if not price_latest.empty:
        for _, r in price_latest.iterrows():
            sid    = r['stock_id']
            close  = float(r.get('close', 0) or 0)
            spread = float(r.get('spread', 0) or 0)
            prev   = close - spread
            chg    = round(spread / prev * 100, 2) if prev > 0 else 0.0
            metrics[sid] = {
                'close':        close,
                'close_chg':    chg,
                'vol_d1':       float(r.get('Trading_Volume', 0) or 0),  # shares
                'money_d1':     float(r.get('Trading_money', 0) or 0),   # 元
                'ma20_bias':    0.0,
                'vol_d5_avg':   0.0,   # avg daily shares over 5d
                'vol_d20_avg':  0.0,
            }

    recent20 = [price_history[d] for d in dates_all[-20:] if d in price_history]
    if recent20:
        ma20_avg = pd.concat(recent20, ignore_index=True).groupby('stock_id')['close'].mean()
        for sid, ma in ma20_avg.items():
            if sid in metrics and float(ma) > 0:
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
    set_vol_avg(dates_d20, 'vol_d20_avg')
    return metrics


# ── 持股地圖 ─────────────────────────────────────────────────────────────────

def build_shareholding_map(share_latest, share_prev):
    """回傳 {stock_id: {ratio, shares, issued, chg}} 外資持股"""
    result = {}
    if share_latest.empty:
        return result
    ratio_col  = next((c for c in share_latest.columns if 'SharesRatio' in c), None)
    shares_col = 'ForeignInvestmentShares'
    issued_col = 'NumberOfSharesIssued'

    prev_shares = {}
    if not share_prev.empty and shares_col in share_prev.columns:
        prev_shares = dict(zip(
            share_prev['stock_id'],
            pd.to_numeric(share_prev[shares_col], errors='coerce').fillna(0)))

    for _, r in share_latest.iterrows():
        sid    = r['stock_id']
        ratio  = float(r.get(ratio_col, 0) or 0) if ratio_col else 0.0
        shares = float(r.get(shares_col, 0) or 0)
        issued = float(r.get(issued_col, 0) or 0)
        prev_s = prev_shares.get(sid, 0.0)
        chg    = round((shares - prev_s) / prev_s * 100, 2) if prev_s > 0 else 0.0
        result[sid] = {'ratio': round(ratio, 2), 'shares': shares,
                       'issued': issued, 'chg': chg}
    return result


# ── 計算工具 ─────────────────────────────────────────────────────────────────

def _f(v, digits=None):
    if v is None:
        return 0
    v = float(v)
    if v != v:
        return 0
    return round(v, digits) if digits is not None else v


def _gi(agg, sid, col):
    if agg.empty or sid not in agg.index or col not in agg.columns:
        return 0.0
    return _f(agg.loc[sid, col])


def net_lots(net_k, close):
    """淨買超張數：淨金額(千元) / 收盤(元) = 張"""
    if close <= 0:
        return 0.0
    return round(net_k / close, 1)


def vol_ratio_pct(net_k, close, avg_daily_vol_shares, days=1):
    """量比%：|淨買超張數| / (均日量張 × 天數) × 100"""
    avg_lots   = avg_daily_vol_shares / 1000
    total_lots = avg_lots * days
    if total_lots <= 0 or close <= 0:
        return 0.0
    return round(abs(net_k / close) / total_lots * 100, 2)


# ── ETF 建表 ─────────────────────────────────────────────────────────────────

def _etf_record(sid, agg_d1, price_metrics, share_map, name_map, bond_set):
    pm    = price_metrics.get(sid, {})
    close = _f(pm.get('close'), 2)
    fb = _gi(agg_d1, sid, 'foreign_buy');  fs = _gi(agg_d1, sid, 'foreign_sell')
    tb = _gi(agg_d1, sid, 'trust_buy');    ts = _gi(agg_d1, sid, 'trust_sell')
    db = _gi(agg_d1, sid, 'dealer_buy');   ds = _gi(agg_d1, sid, 'dealer_sell')
    vol_shares = _f(pm.get('vol_d1'))
    money_e    = _f(pm.get('money_d1'))    # 元
    sh     = share_map.get(sid, {})
    issued = sh.get('issued', 0.0)
    mktval_b = round(issued * close / 1e8, 2) if issued > 0 and close > 0 else 0.0
    fn = fb - fs;  tn = tb - ts;  dn = db - ds
    return {
        'stock_id':       sid,
        'stock_name':     name_map.get(sid, ''),
        'bond':           sid in bond_set,
        'close':          close,
        'vol_k':          int(vol_shares / 1000),
        'money_b':        round(money_e / 1e8, 2),
        'mktval_b':       mktval_b,
        'foreign_net_k':  int(fn),
        'foreign_net_lots': net_lots(fn, close),
        'trust_net_k':    int(tn),
        'trust_net_lots': net_lots(tn, close),
        'dealer_net_k':   int(dn),
        'dealer_net_lots':net_lots(dn, close),
    }


def build_etf_volume_rank(agg_d1, price_metrics, etf_ids, share_map, name_map, bond_set):
    items = [_etf_record(s, agg_d1, price_metrics, share_map, name_map, bond_set)
             for s in etf_ids]
    items.sort(key=lambda x: x['vol_k'], reverse=True)
    for i, it in enumerate(items, 1):
        it['rank'] = i
    return items


def build_etf_mktval_rank(agg_d1, price_metrics, etf_ids, share_map, name_map, bond_set):
    items = [_etf_record(s, agg_d1, price_metrics, share_map, name_map, bond_set)
             for s in etf_ids]
    items.sort(key=lambda x: x['mktval_b'], reverse=True)
    for i, it in enumerate(items, 1):
        it['rank'] = i
    return items


def build_etf_summary(agg_d1, price_metrics, share_map, non_bond_ids, bond_ids):
    all_etf = non_bond_ids | bond_ids

    def calc(ids, label):
        vol_k   = sum(int(_f(price_metrics.get(s, {}).get('vol_d1')) / 1000) for s in ids)
        money_b = round(sum(_f(price_metrics.get(s, {}).get('money_d1')) for s in ids) / 1e8, 2)
        mktval_b = 0.0
        for s in ids:
            pm    = price_metrics.get(s, {})
            close = _f(pm.get('close'))
            issued = share_map.get(s, {}).get('issued', 0.0)
            if issued > 0 and close > 0:
                mktval_b += issued * close / 1e8
        fn = sum(_gi(agg_d1, s, 'foreign_buy') - _gi(agg_d1, s, 'foreign_sell') for s in ids)
        tn = sum(_gi(agg_d1, s, 'trust_buy')   - _gi(agg_d1, s, 'trust_sell')   for s in ids)
        dn = sum(_gi(agg_d1, s, 'dealer_buy')  - _gi(agg_d1, s, 'dealer_sell')  for s in ids)
        return {
            'label':    label,
            'vol_k':    vol_k,
            'money_b':  money_b,
            'mktval_b': round(mktval_b, 2),
            'foreign_net_k': int(fn),
            'trust_net_k':   int(tn),
            'dealer_net_k':  int(dn),
        }

    return [
        calc(all_etf,      '整體ETF'),
        calc(non_bond_ids, '非債券型'),
        calc(bond_ids,     '債券型'),
    ]


# ── 投信/外資建表 ─────────────────────────────────────────────────────────────

def build_entity_records(agg_d1, agg_d5, agg_d20, price_metrics,
                          share_map, all_etf_ids, name_map, entity='trust'):
    bk = f'{entity}_buy'
    sk = f'{entity}_sell'
    candidate_ids = set()
    for agg in (agg_d1, agg_d5, agg_d20):
        if not agg.empty:
            candidate_ids |= set(agg.index)
    candidate_ids -= all_etf_ids

    rows = []
    for sid in candidate_ids:
        pm    = price_metrics.get(sid, {})
        close = _f(pm.get('close'))
        b1 = _gi(agg_d1,  sid, bk);  s1 = _gi(agg_d1,  sid, sk)
        b5 = _gi(agg_d5,  sid, bk);  s5 = _gi(agg_d5,  sid, sk)
        b20= _gi(agg_d20, sid, bk);  s20= _gi(agg_d20, sid, sk)
        n1 = b1 - s1;  n5 = b5 - s5;  n20 = b20 - s20
        v1  = _f(pm.get('vol_d1'))
        v5  = _f(pm.get('vol_d5_avg'))
        v20 = _f(pm.get('vol_d20_avg'))

        sh = share_map.get(sid, {}) if entity == 'foreign' else {}
        hold_shares  = sh.get('shares', 0.0)
        hold_ratio   = sh.get('ratio',  0.0)
        hold_lots    = int(hold_shares / 1000) if hold_shares > 0 else 0
        hold_mktval  = round(hold_shares * close / 1e6, 1) if hold_shares > 0 and close > 0 else 0.0
        hold_chg     = sh.get('chg', 0.0)

        rows.append({
            'stock_id':    sid,
            'stock_name':  name_map.get(sid, ''),
            'close':       _f(close, 2),
            'close_chg':   _f(pm.get('close_chg'), 2),
            'ma20_bias':   _f(pm.get('ma20_bias'), 2),
            'hold_ratio':  hold_ratio,
            'hold_lots':   hold_lots,
            'hold_mktval': hold_mktval,
            'hold_chg':    hold_chg,
            'd1_net_k':    int(n1),  'd1_lots': net_lots(n1, close),  'd1_vr': vol_ratio_pct(n1, close, v1, 1),
            'd5_net_k':    int(n5),  'd5_lots': net_lots(n5, close),  'd5_vr': vol_ratio_pct(n5, close, v5, 5),
            'd20_net_k':   int(n20), 'd20_lots':net_lots(n20,close),  'd20_vr':vol_ratio_pct(n20,close,v20,20),
        })
    return rows


def _topn(rows, key, reverse=True, n=TOP_N, pre_filter=None):
    data = [r for r in rows if pre_filter(r)] if pre_filter else rows
    sorted_rows = sorted(data, key=lambda x: x.get(key, 0), reverse=reverse)
    result = []
    for i, r in enumerate(sorted_rows[:n], 1):
        out = dict(r)
        out['rank'] = i
        result.append(out)
    return result


def build_entity_tables(rows):
    is_buy  = lambda r: r.get('d1_net_k', 0) > 0
    is_sell = lambda r: r.get('d1_net_k', 0) < 0
    is5b = lambda r: r.get('d5_net_k', 0) > 0
    is5s = lambda r: r.get('d5_net_k', 0) < 0
    is20b= lambda r: r.get('d20_net_k',0) > 0
    is20s= lambda r: r.get('d20_net_k',0) < 0

    return {
        'd1': {
            'buy':     _topn(rows, 'd1_net_k',  reverse=True,  pre_filter=is_buy),
            'sell':    _topn(rows, 'd1_net_k',  reverse=False, pre_filter=is_sell),
            'buy_vr':  _topn(rows, 'd1_vr',     reverse=True,  pre_filter=is_buy),
            'sell_vr': _topn(rows, 'd1_vr',     reverse=True,  pre_filter=is_sell),
        },
        'd5': {
            'buy':     _topn(rows, 'd5_net_k',  reverse=True,  pre_filter=is5b),
            'sell':    _topn(rows, 'd5_net_k',  reverse=False, pre_filter=is5s),
            'buy_vr':  _topn(rows, 'd5_vr',     reverse=True,  pre_filter=is5b),
            'sell_vr': _topn(rows, 'd5_vr',     reverse=True,  pre_filter=is5s),
        },
        'd20': {
            'buy':     _topn(rows, 'd20_net_k', reverse=True,  pre_filter=is20b),
            'sell':    _topn(rows, 'd20_net_k', reverse=False, pre_filter=is20s),
            'buy_vr':  _topn(rows, 'd20_vr',    reverse=True,  pre_filter=is20b),
            'sell_vr': _topn(rows, 'd20_vr',    reverse=True,  pre_filter=is20s),
        },
    }


# ── 同步建表 ─────────────────────────────────────────────────────────────────

def build_sync_records(agg_d1, agg_d5, price_metrics, all_etf_ids, name_map):
    if agg_d1.empty:
        return []
    candidate_ids = set(agg_d1.index)
    if not agg_d5.empty:
        candidate_ids |= set(agg_d5.index)
    candidate_ids -= all_etf_ids

    rows = []
    for sid in candidate_ids:
        pm    = price_metrics.get(sid, {})
        close = _f(pm.get('close'))
        td1 = _gi(agg_d1, sid, 'trust_buy')   - _gi(agg_d1, sid, 'trust_sell')
        fd1 = _gi(agg_d1, sid, 'foreign_buy') - _gi(agg_d1, sid, 'foreign_sell')
        td5 = _gi(agg_d5, sid, 'trust_buy')   - _gi(agg_d5, sid, 'trust_sell')
        fd5 = _gi(agg_d5, sid, 'foreign_buy') - _gi(agg_d5, sid, 'foreign_sell')
        v1  = _f(pm.get('vol_d1'))
        v5  = _f(pm.get('vol_d5_avg'))
        rows.append({
            'stock_id':      sid,
            'stock_name':    name_map.get(sid, ''),
            'close':         _f(close, 2),
            'close_chg':     _f(pm.get('close_chg'), 2),
            'd1_trust_k':    int(td1), 'd1_foreign_k': int(fd1), 'd1_total_k': int(td1 + fd1),
            'd1_total_lots': net_lots(td1 + fd1, close),
            'd5_trust_k':    int(td5), 'd5_foreign_k': int(fd5), 'd5_total_k': int(td5 + fd5),
            'd5_total_lots': net_lots(td5 + fd5, close),
            'd1_trust_vr':   vol_ratio_pct(td1, close, v1, 1),
            'd1_foreign_vr': vol_ratio_pct(fd1, close, v1, 1),
            'd5_trust_vr':   vol_ratio_pct(td5, close, v5, 5),
            'd5_foreign_vr': vol_ratio_pct(fd5, close, v5, 5),
        })
    return rows


def build_sync_tables(rows):
    return {
        'd1': {
            'buy':  _topn(rows, 'd1_total_k', reverse=True,  pre_filter=lambda r: r.get('d1_total_k',0)>0),
            'sell': _topn(rows, 'd1_total_k', reverse=False, pre_filter=lambda r: r.get('d1_total_k',0)<0),
        },
        'd5': {
            'buy':  _topn(rows, 'd5_total_k', reverse=True,  pre_filter=lambda r: r.get('d5_total_k',0)>0),
            'sell': _topn(rows, 'd5_total_k', reverse=False, pre_filter=lambda r: r.get('d5_total_k',0)<0),
        },
        'trust_vol': {
            'd1': _topn(rows, 'd1_trust_vr', reverse=True, pre_filter=lambda r: r.get('d1_trust_k',0)>0),
            'd5': _topn(rows, 'd5_trust_vr', reverse=True, pre_filter=lambda r: r.get('d5_trust_k',0)>0),
        },
        'foreign_vol': {
            'd1': _topn(rows, 'd1_foreign_vr', reverse=True, pre_filter=lambda r: r.get('d1_foreign_k',0)>0),
            'd5': _topn(rows, 'd5_foreign_vr', reverse=True, pre_filter=lambda r: r.get('d5_foreign_k',0)>0),
        },
    }


# ── 主程式 ────────────────────────────────────────────────────────────────────

def main():
    log.info('=== 法人儀表板掃描開始 v2 ===')

    dates_all   = get_trading_dates(25)
    dates_d20   = dates_all[-20:]
    dates_d5    = dates_all[-5:]
    date_latest = dates_all[-1]
    log.info(f'目標日期：{date_latest}，範圍：{dates_all[0]} ~ {date_latest}')

    today_str = datetime.today().strftime('%Y-%m-%d')
    if date_latest >= today_str:
        log.info(f'檢查 FinMind 是否已發布 {date_latest} 資料...')
        if not check_data_available(date_latest):
            log.warning(f'FinMind 尚未發布 {date_latest} 法人資料，本次跳過')
            sys.exit(0)
        log.info('資料已就緒，開始完整掃描')

    stock_df      = fetch_stock_list()
    inst_history  = fetch_inst_history(dates_all)
    price_history = fetch_price_history(dates_all)
    share_latest  = fetch_shareholding(date_latest)
    share_prev    = fetch_shareholding(dates_all[0])  # ~25 個交易日前

    if not inst_history:
        log.error('法人資料空白，終止')
        sys.exit(1)

    name_map  = (dict(zip(stock_df['stock_id'], stock_df['stock_name']))
                 if not stock_df.empty and 'stock_name' in stock_df.columns else {})
    share_map = build_shareholding_map(share_latest, share_prev)

    listed_ids = set(stock_df['stock_id'].tolist()) if not stock_df.empty else set()
    all_stock_ids = set()
    for df in inst_history.values():
        all_stock_ids |= set(df['stock_id'].tolist())
    if listed_ids:
        before = len(all_stock_ids)
        all_stock_ids &= listed_ids
        log.info(f'白名單過濾：{before} → {len(all_stock_ids)} 支（移除 {before - len(all_stock_ids)} 支）')

    non_bond_ids, bond_ids = identify_etfs(stock_df, all_stock_ids)
    all_etf_ids = non_bond_ids | bond_ids

    agg_d1  = aggregate_inst(inst_history, dates_d20[-1:])
    agg_d5  = aggregate_inst(inst_history, dates_d5)
    agg_d20 = aggregate_inst(inst_history, dates_d20)

    for _agg in (agg_d1, agg_d5, agg_d20):
        if not _agg.empty:
            _agg.drop(index=_agg.index.difference(all_stock_ids), inplace=True)

    price_metrics = compute_price_metrics(price_history, dates_all, dates_d5, dates_d20)

    log.info('建立輸出 JSON...')

    # ETF
    etf_vol_nb  = build_etf_volume_rank(agg_d1, price_metrics, non_bond_ids, share_map, name_map, bond_ids)
    etf_vol_b   = build_etf_volume_rank(agg_d1, price_metrics, bond_ids,     share_map, name_map, bond_ids)
    etf_mv_nb   = build_etf_mktval_rank(agg_d1, price_metrics, non_bond_ids, share_map, name_map, bond_ids)
    etf_mv_b    = build_etf_mktval_rank(agg_d1, price_metrics, bond_ids,     share_map, name_map, bond_ids)
    etf_summary = build_etf_summary(agg_d1, price_metrics, share_map, non_bond_ids, bond_ids)

    # 投信 / 外資
    trust_rows   = build_entity_records(agg_d1, agg_d5, agg_d20, price_metrics, share_map, all_etf_ids, name_map, 'trust')
    foreign_rows = build_entity_records(agg_d1, agg_d5, agg_d20, price_metrics, share_map, all_etf_ids, name_map, 'foreign')
    trust_tables   = build_entity_tables(trust_rows)
    foreign_tables = build_entity_tables(foreign_rows)

    # 同步
    sync_rows   = build_sync_records(agg_d1, agg_d5, price_metrics, all_etf_ids, name_map)
    sync_tables = build_sync_tables(sync_rows)

    output = {
        'date':       date_latest,
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'etf': {
            'volume':  {'non_bond': etf_vol_nb, 'bond': etf_vol_b},
            'mktval':  {'non_bond': etf_mv_nb,  'bond': etf_mv_b},
            'summary': etf_summary,
        },
        'trust':   trust_tables,
        'foreign': foreign_tables,
        'sync':    sync_tables,
    }

    os.makedirs('data', exist_ok=True)
    with open('data/etf.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    log.info('=== 完成 ===')
    log.info(f'  ETF 非債 {len(etf_vol_nb)} 支 / 債 {len(etf_vol_b)} 支')
    log.info(f'  投信 d1買 {len(trust_tables["d1"]["buy"])} / d1賣 {len(trust_tables["d1"]["sell"])}')
    log.info(f'  外資 d1買 {len(foreign_tables["d1"]["buy"])} / d1賣 {len(foreign_tables["d1"]["sell"])}')
    log.info(f'  同步 d1買 {len(sync_tables["d1"]["buy"])} / d1賣 {len(sync_tables["d1"]["sell"])}')


if __name__ == '__main__':
    main()
