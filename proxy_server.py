from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import requests, json, os, threading, hmac, hashlib, time
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

# ─────────────────────────────────────────────
# VERSİYON
# major → büyük mimari değişiklik
# minor → yeni endpoint / özellik
# patch → hata düzeltme
# ─────────────────────────────────────────────
VERSION = "3.24.1"

# ─────────────────────────────────────────────
# KALICI LOG SİSTEMİ — günlük dosyaya yazar
# ─────────────────────────────────────────────
import datetime as _dt

def _log_file_path():
    """Günlük log dosyası: btc_5m_bot_YYYY-MM-DD.log"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    date_str = _dt.datetime.now().strftime('%Y-%m-%d')
    return os.path.join(script_dir, f'btc_5m_bot_{date_str}.log')

def write_bot_log(level, message, source='bLog'):
    """Log satırını günlük dosyaya yaz."""
    try:
        now = _dt.datetime.now().strftime('%H:%M:%S')
        line = f"{now} [{level:5}] [{source}] {message}\n"
        with open(_log_file_path(), 'a', encoding='utf-8') as lf:
            lf.write(line)
    except Exception as e:
        print(f"[LOG] Yazma hatası: {e}")

def _log_file_path():
    """Günlük log dosyası yolu — btc_5m_bot_YYYY-MM-DD.log"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    date_str = _dt.datetime.now().strftime('%Y-%m-%d')
    return os.path.join(script_dir, f'btc_5m_bot_{date_str}.log')

def write_bot_log(level, message, source='bLog'):
    """Log satırını günlük dosyaya yaz."""
    try:
        now = _dt.datetime.now().strftime('%H:%M:%S')
        line = f"{now} [{level:5}] [{source}] {message}\n"
        with open(_log_file_path(), 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception as e:
        print(f"[LOG] Yazma hatası: {e}")

app = Flask(__name__)
CORS(app)

GAMMA   = "https://gamma-api.polymarket.com"
CLOB    = "https://clob.polymarket.com"
RELAYER = "https://relayer-v2.polymarket.com"
BINANCE = "https://api.binance.com"
RPC     = "https://polygon-mainnet.g.alchemy.com/v2/ydtTpkyWKbEBtYO6R1wSl"

PRIVATE_KEY    = os.getenv("POLYMARKET_PRIVATE_KEY")
API_KEY        = os.getenv("POLYMARKET_API_KEY") or os.getenv("POLY_BUILDER_API_KEY","")
API_SECRET     = os.getenv("POLYMARKET_API_SECRET") or os.getenv("POLY_BUILDER_SECRET","")
API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE") or os.getenv("POLY_BUILDER_PASSPHRASE","")
METAMASK_KEY   = os.getenv("METAMASK_PRIVATE_KEY")

BUILDER_KEY        = os.getenv("POLY_BUILDER_API_KEY","")
BUILDER_SECRET     = os.getenv("POLY_BUILDER_SECRET","")
BUILDER_PASSPHRASE = os.getenv("POLY_BUILDER_PASSPHRASE","")
RELAYER_KEY        = os.getenv("RELAYER_API_KEY","")
RELAYER_KEY_ADDR   = os.getenv("RELAYER_API_KEY_ADDRESS", "")  # proxy wallet adresi

USDC_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
CTF_ADDRESS  = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
EOA_ADDR     = "0x93ae477c0eb9F3006aD832874c8186C153BFD9E1"

USDC_ABI = [{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf",
             "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]

_lock = threading.Lock()
_order_lock = threading.Lock()  # Eş zamanlı emir gönderimini engelle
_last_order_time = 0            # Son emir zamanı

# _redeemed dosyadan yükle — restart sonrası tekrar bildirim gitmesin
_REDEEMED_FILE = os.path.join(os.path.dirname(__file__), '.redeemed_cids')
def _load_redeemed():
    try:
        with open(_REDEEMED_FILE, 'r') as f:
            return set(line.strip() for line in f if line.strip())
    except: return set()
def _save_redeemed(s):
    try:
        with open(_REDEEMED_FILE, 'w') as f:
            f.write('\n'.join(s))
    except: pass
_redeemed = _load_redeemed()

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN", "8756734857:AAFHudOly6fr3i_JY01KtVj8eZXpSPSTifA")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1638471757")
_tg_offset = 0

def tg_send(text):
    """Telegram'a mesaj gönder."""
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=8
        )
    except Exception as e:
        print(f"[TG] Mesaj gönderilemedi: {e}")

def _tr_time():
    """Türkiye saati (UTC+3) formatla."""
    from datetime import datetime, timezone, timedelta
    tr = datetime.now(timezone(timedelta(hours=3)))
    return tr.strftime('%H:%M')

_notified_redeems = set()  # Tekrar bildirim gitmesin

def tg_notify_trade(direction, price, bet, status, pnl, market, bot='BTC5'):
    """Sadece kesinleşmiş işlem bildirimi — open status görmezden gel."""
    if status == 'open':
        return  # Match bildirimi gitmiyor
    bot_tag = f"[{bot}] "
    saat = _tr_time()
    # Son bakiyeyi çek
    bal_str = ""
    try:
        from web3 import Web3 as _W3
        _w3 = _W3(_W3.HTTPProvider(RPC))
        _usdc = _w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)
        if PRIVATE_KEY:
            _addr = _w3.eth.account.from_key(PRIVATE_KEY).address
            _bal = _usdc.functions.balanceOf(_addr).call() / 1e6
            bal_str = f"\n💵 Bakiye: <b>${_bal:.2f}</b>"
    except: pass

    if status == 'win':
        emoji = "✅"
        msg = f"{emoji} <b>{bot_tag}KAZANDI!</b>\n"
        msg += f"Yön: <b>{direction}</b> @ {price:.1%} | Maliyet: ${bet:.2f}\n"
        msg += f"Kâr: <b>+${pnl:.2f}</b> 🎉\n"
        msg += f"Piyasa: {market[:40]}{bal_str}\n"
        msg += f"🕐 {saat}"
    else:
        emoji = "❌"
        msg = f"{emoji} <b>{bot_tag}KAYBETTİ</b>\n"
        msg += f"Yön: <b>{direction}</b> @ {price:.1%} | Maliyet: ${bet:.2f}\n"
        msg += f"Kayıp: <b>-${abs(pnl):.2f}</b>\n"
        msg += f"Piyasa: {market[:40]}{bal_str}\n"
        msg += f"🕐 {saat}"
    threading.Thread(target=tg_send, args=(msg,), daemon=True).start()

def tg_notify_redeem(amount, addr, cid=''):
    """Redeem bildirimi — aynı conditionId için bir daha gitmesin."""
    global _notified_redeems
    # conditionId varsa onu kullan (kalıcı), yoksa tutar+adres (geçici)
    key = cid if cid else f"{addr[:12]}_{amount:.2f}"
    if key in _notified_redeems:
        return
    _notified_redeems.add(key)
    # cid yoksa 10dk sonra temizle, cid varsa kalıcı tut
    if not cid:
        def _clear():
            import time as _t; _t.sleep(600); _notified_redeems.discard(key)
        threading.Thread(target=_clear, daemon=True).start()
    saat = _tr_time()
    msg = f"💰 <b>REDEEM YAPILDI</b>\n"
    msg += f"Tutar: <b>${amount:.2f}</b>\n"
    msg += f"Cüzdan: {addr[:12]}...\n"
    msg += f"🕐 {saat}"
    threading.Thread(target=tg_send, args=(msg,), daemon=True).start()

def _tg_bot_loop():
    """Telegram'dan gelen komutları dinle."""
    global _tg_offset
    time.sleep(10)
    tg_send("🤖 <b>PolyBot başlatıldı!</b>\nKomutlar:\n/durum — Bot durumu\n/bakiye — USDC bakiyesi\n/son5 — Son 5 işlem\n/yardim — Komut listesi")
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                params={"offset": _tg_offset, "timeout": 30},
                timeout=35
            )
            if not r.ok:
                time.sleep(5)
                continue
            updates = r.json().get("result", [])
            for upd in updates:
                _tg_offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip().lower()
                if chat_id != TG_CHAT_ID:
                    continue
                if text in ("/durum", "durum"):
                    _tg_cmd_durum()
                elif text in ("/bakiye", "bakiye"):
                    _tg_cmd_bakiye()
                elif text in ("/son5", "son5"):
                    tg_send("📊 Son işlemler için dashboard'a bak:\nhttp://127.0.0.1:5000/trades")
                elif text in ("/yardim", "/help", "yardim"):
                    tg_send("📋 <b>Komutlar:</b>\n/durum — Bot durumu\n/bakiye — USDC bakiyesi\n/son5 — Son işlemler\n/redeem — Manuel redeem\n/yardim — Bu liste")
                elif text in ("/redeem", "redeem"):
                    tg_send("⚡ Redeem başlatılıyor...")
                    threading.Thread(target=_tg_do_redeem, daemon=True).start()
        except Exception as e:
            print(f"[TG] Loop hata: {e}")
            time.sleep(10)

def _tg_cmd_durum():
    try:
        w3 = Web3(Web3.HTTPProvider(RPC))
        proxy_addr = w3.eth.account.from_key(PRIVATE_KEY).address if PRIVATE_KEY else "?"
        usdc = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)
        bal = usdc.functions.balanceOf(proxy_addr).call() / 1e6
        msg = f"🤖 <b>PolyBot Durumu</b>\n"
        msg += f"Durum: <b>Çalışıyor ✅</b>\n"
        msg += f"Bakiye: <b>${bal:.2f} USDC</b>\n"
        msg += f"Versiyon: v{VERSION}"
        tg_send(msg)
    except Exception as e:
        tg_send(f"⚠️ Durum alınamadı: {str(e)[:60]}")

def _tg_cmd_bakiye():
    try:
        w3 = Web3(Web3.HTTPProvider(RPC))
        proxy_addr = w3.eth.account.from_key(PRIVATE_KEY).address if PRIVATE_KEY else "?"
        usdc = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)
        proxy_bal = usdc.functions.balanceOf(proxy_addr).call() / 1e6
        eoa_addr = w3.eth.account.from_key(METAMASK_KEY).address if METAMASK_KEY else "?"
        eoa_bal = usdc.functions.balanceOf(eoa_addr).call() / 1e6
        proxy_pol = w3.eth.get_balance(proxy_addr) / 1e18 if PRIVATE_KEY else 0
        pol_warn = " ⚠️ AZ!" if proxy_pol < 0.3 else ""
        msg = f"💵 <b>USDC Bakiyeleri</b>\n"
        msg += f"Bot cüzdanı: <b>${proxy_bal:.2f}</b>\n"
        msg += f"Ana cüzdan: <b>${eoa_bal:.2f}</b>\n"
        msg += f"⛽ Gas (POL): <b>{proxy_pol:.4f}</b>{pol_warn}"
        tg_send(msg)
    except Exception as e:
        tg_send(f"⚠️ Bakiye alınamadı: {str(e)[:60]}")

def _tg_do_redeem():
    try:
        w3 = Web3(Web3.HTTPProvider(RPC))
        CTF_ABI = [{"inputs":[{"name":"collateralToken","type":"address"},
                               {"name":"parentCollectionId","type":"bytes32"},
                               {"name":"conditionId","type":"bytes32"},
                               {"name":"indexSets","type":"uint256[]"}],
                    "name":"redeemPositions","outputs":[],
                    "stateMutability":"nonpayable","type":"function"}]
        ctf = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
        addrs = []
        if PRIVATE_KEY:
            addrs.append((w3.eth.account.from_key(PRIVATE_KEY).address, PRIVATE_KEY))
        if METAMASK_KEY:
            addrs.append((w3.eth.account.from_key(METAMASK_KEY).address, METAMASK_KEY))
        total = 0
        for addr, key in addrs:
            resp = requests.get('https://data-api.polymarket.com/positions',
                params={'user': addr, 'limit': 100}, timeout=10)
            if not resp.ok: continue
            for pos in resp.json():
                if not pos.get('redeemable'): continue
                cid = pos.get('conditionId', '')
                if not cid or cid in _redeemed: continue
                try:
                    nonce = w3.eth.get_transaction_count(addr, 'latest')
                    tx = ctf.functions.redeemPositions(
                        USDC_ADDRESS, b'\x00'*32,
                        bytes.fromhex(cid.replace('0x','')), [1, 2]
                    ).build_transaction({'from': addr, 'nonce': nonce,
                        'gas': 200000, 'gasPrice': w3.to_wei('300','gwei'), 'chainId': 137})
                    signed = w3.eth.account.sign_transaction(tx, key)
                    w3.eth.send_raw_transaction(signed.raw_transaction)
                    _redeemed.add(cid); _save_redeemed(_redeemed)
                    total += 1
                except: pass
        tg_send(f"✅ Redeem tamamlandı: {total} pozisyon işlendi.")
    except Exception as e:
        tg_send(f"❌ Redeem hata: {str(e)[:80]}")

# ─────────────────────────────────────────────
# YARDIMCI
# ─────────────────────────────────────────────
def _builder_headers():
    """Builder attribution HMAC header'ları."""
    if not BUILDER_KEY or not BUILDER_SECRET:
        return {}
    ts  = str(int(time.time() * 1000))
    msg = ts + BUILDER_KEY
    sig = hmac.new(BUILDER_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {
        "POLY_BUILDER_API_KEY":    BUILDER_KEY,
        "POLY_BUILDER_TIMESTAMP":  ts,
        "POLY_BUILDER_SIGNATURE":  sig,
        "POLY_BUILDER_PASSPHRASE": BUILDER_PASSPHRASE,
    }

def _get_best_ask(token_id):
    """CLOB order book'tan best ask fiyatını çek."""
    if not token_id:
        return None
    try:
        r = requests.get(f"{CLOB}/book", params={'token_id': token_id}, timeout=5)
        if not r.ok:
            return None
        book = r.json()
        asks = book.get('asks', [])
        if asks:
            return float(asks[0].get('price', 0))
    except:
        pass
    return None

def _get_market(symbol, slot):
    slug = f"{symbol}-updown-5m-{slot}"
    try:
        resp = requests.get(f"{GAMMA}/events", params={'slug': slug}, timeout=10)
        data = resp.json()
        if not data:
            return jsonify({'market': None})
        event = data[0]
        raw = event.get('markets', [])
        if not raw:
            return jsonify({'market': None})
        m = raw[0]
        try:
            outcomes  = json.loads(m.get('outcomes',  '["Up","Down"]'))
            prices    = json.loads(m.get('outcomePrices', '[0.5,0.5]'))
            token_ids = json.loads(m.get('clobTokenIds', '[]'))
        except:
            outcomes=['Up','Down']; prices=[0.5,0.5]; token_ids=['','']
        up_idx   = next((i for i,o in enumerate(outcomes) if o.lower()=='up'),  0)
        down_idx = next((i for i,o in enumerate(outcomes) if o.lower()=='down'), 1)
        up_tok   = token_ids[up_idx]   if len(token_ids)>up_idx   else ''
        down_tok = token_ids[down_idx] if len(token_ids)>down_idx else ''

        # CLOB'dan anlık midpoint çek
        try:
            if up_tok:
                mp_r = requests.get(f"{CLOB}/midpoint", params={'token_id': up_tok}, timeout=3)
                if mp_r.ok:
                    up_mid = float(mp_r.json().get('mid', 0))
                    if 0.01 < up_mid < 0.99:
                        up_price   = up_mid
                        down_price = round(1.0 - up_mid, 4)
        except: pass

        # CLOB best ask
        up_ask = up_price; down_ask = down_price
        try:
            if up_tok:
                bk = requests.get(f"{CLOB}/book", params={'token_id': up_tok}, timeout=3).json()
                if bk.get('asks'): up_ask = float(bk['asks'][0]['price'])
            if down_tok:
                bk2 = requests.get(f"{CLOB}/book", params={'token_id': down_tok}, timeout=3).json()
                if bk2.get('asks'): down_ask = float(bk2['asks'][0]['price'])
        except: pass

        return jsonify({'market': {
            'id':               m.get('id') or m.get('conditionId',''),
            'title':            event.get('title', slug),
            'endDate':          m.get('endDate') or m.get('endDateIso',''),
            'up_price':         up_price,
            'down_price':       down_price,
            'up_token':         up_tok,
            'down_token':       down_tok,
            'accepting_orders': m.get('acceptingOrders', False),
            'up_best_ask':      up_ask,
            'down_best_ask':    down_ask,
        }})
    except Exception as e:
        return jsonify({'market': None, 'error': str(e)})

def _get_result(market_id):
    try:
        resp = requests.get(f"{GAMMA}/markets/{market_id}", timeout=10)
        m = resp.json()
        resolved = m.get('resolved', False) or m.get('closed', False)
        winner = None

        prices_raw   = m.get('outcomePrices', '[0.5,0.5]')
        outcomes_raw = m.get('outcomes', '["Up","Down"]')
        try:
            prices   = json.loads(prices_raw)
            outcomes = json.loads(outcomes_raw)
        except:
            prices, outcomes = [0.5, 0.5], ['Up', 'Down']

        if resolved:
            try:
                winner = outcomes[prices.index(max(prices))]
            except:
                pass
        else:
            # Resolve beklemeden fiyat bazlı erken tespit
            # Polymarket fiyatı 0.97+ olduğunda market fiilen kesinleşmiştir
            try:
                max_price = max(float(p) for p in prices)
                max_idx   = [float(p) for p in prices].index(max_price)
                if max_price >= 0.97:
                    winner   = outcomes[max_idx]
                    resolved = True  # fiyat bazlı resolve
            except:
                pass

        return jsonify({
            'resolved': resolved,
            'winner':   winner,
            'prices':   prices,
            'outcomes': outcomes
        })
    except Exception as e:
        return jsonify({'resolved': False, 'winner': None, 'error': str(e)})

# ─────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────

@app.route('/version')
def version():
    return jsonify({'version': VERSION})

@app.route('/test_auth')
def test_auth():
    """CLOB auth'u ham HTTP ile test et — hangi key/adres kombinasyonu çalışıyor?"""
    results = {}
    # Test 1: mevcut API_KEY ile direkt GET
    try:
        r = requests.get(f"{CLOB}/auth/api-key",
            headers={'POLY_API_KEY': API_KEY, 'POLY_API_SECRET': API_SECRET,
                     'POLY_API_PASSPHRASE': API_PASSPHRASE}, timeout=5)
        results['current_key'] = {'status': r.status_code, 'body': r.json()}
    except Exception as e:
        results['current_key'] = {'error': str(e)}
    # Test 2: EOA adresi ile positions
    try:
        r2 = requests.get('https://data-api.polymarket.com/positions',
            params={'user': EOA_ADDR, 'limit': 1}, timeout=5)
        results['eoa_positions'] = {'status': r2.status_code, 'count': len(r2.json()) if r2.ok else 0}
    except Exception as e:
        results['eoa_positions'] = {'error': str(e)}
    # Test 3: proxy wallet adresi ile positions
    try:
        w3t = Web3(Web3.HTTPProvider(RPC))
        proxy_addr = w3t.eth.account.from_key(PRIVATE_KEY).address if PRIVATE_KEY else None
        r3 = requests.get('https://data-api.polymarket.com/positions',
            params={'user': proxy_addr, 'limit': 1}, timeout=5)
        results['proxy_positions'] = {'status': r3.status_code, 'count': len(r3.json()) if r3.ok else 0, 'addr': proxy_addr}
    except Exception as e:
        results['proxy_positions'] = {'error': str(e)}
    return jsonify(results)

@app.route('/reset_api_key')
def reset_api_key():
    """PRIVATE_KEY (proxy wallet 0xf32F) ile creds türet — bu key çalışıyor."""
    global API_KEY, API_SECRET, API_PASSPHRASE
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
    results = {}

    # 92e4e6c4 key'i PRIVATE_KEY ile türetiliyor — bunu kaydet
    try:
        cl = ClobClient(host=CLOB, chain_id=POLYGON, key=PRIVATE_KEY, signature_type=0)
        creds = cl.create_or_derive_api_creds()
        results['proxy_key_creds'] = {'api_key': creds.api_key, 'success': True}

        # Hemen test et — AssetType enum kullan
        from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams
        test_client = ClobClient(host=CLOB, chain_id=POLYGON, key=PRIVATE_KEY,
                                 creds=creds, signature_type=0)

        # Her durumda kaydet (92e4e6c4 türetilebildi)
        env_path = os.path.join(os.path.dirname(__file__), '.env')
        lines = []
        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                lines = f.readlines()
        keys_to_set = {
            'POLYMARKET_API_KEY':        creds.api_key,
            'POLYMARKET_API_SECRET':     creds.api_secret,
            'POLYMARKET_API_PASSPHRASE': creds.api_passphrase,
        }
        updated = set()
        new_lines = []
        for line in lines:
            written = False
            for k, v in keys_to_set.items():
                if line.startswith(k + '='):
                    new_lines.append(f'{k}={v}\n')
                    updated.add(k)
                    written = True
                    break
            if not written:
                new_lines.append(line)
        for k, v in keys_to_set.items():
            if k not in updated:
                new_lines.append(f'{k}={v}\n')
        with open(env_path, 'w') as f:
            f.writelines(new_lines)
        API_KEY        = creds.api_key
        API_SECRET     = creds.api_secret
        API_PASSPHRASE = creds.api_passphrase
        results['saved'] = True

        try:
            # AssetType enum'u bul
            from py_clob_client.clob_types import AssetType as AT
            # Olası isimler: COLLATERAL, USDC, collateral
            asset_val = getattr(AT, 'COLLATERAL', None) or getattr(AT, 'USDC', None) or getattr(AT, 'collateral', None)
            if asset_val:
                ba = test_client.get_balance_allowance(BalanceAllowanceParams(asset_type=asset_val))
            else:
                ba = test_client.get_balance_allowance(BalanceAllowanceParams())
            results['allowance_test'] = {'success': True, 'data': str(ba)}
        except Exception as te:
            results['allowance_test'] = {'success': False, 'enum_error': str(te)[:100]}
    except Exception as e:
        results['proxy_key_creds'] = {'success': False, 'error': str(e)}

    return jsonify({'results': results, 'current_key': API_KEY})


@app.route('/markets')
def markets():
    resp = requests.get(f"{GAMMA}/markets",
        params={'limit':'200','active':'true','closed':'false',
                'order':'volume24hr','ascending':'false'}, timeout=15)
    return jsonify(resp.json())

# ── Binance mumlar ────────────────────────────
def _klines(symbol, interval, limit):
    r = requests.get(f"{BINANCE}/api/v3/klines",
        params={'symbol': symbol, 'interval': interval, 'limit': str(limit)}, timeout=8)
    return jsonify(r.json())

@app.route('/btc_candles')
def btc_candles():     return _klines('BTCUSDT','5m',12)
@app.route('/btc_candles_1m')
def btc_candles_1m():  return _klines('BTCUSDT','1m',30)
@app.route('/eth_candles')
def eth_candles():     return _klines('ETHUSDT','5m',12)
@app.route('/eth_candles_1m')
def eth_candles_1m():  return _klines('ETHUSDT','1m',30)
@app.route('/xrp_candles')
def xrp_candles():     return _klines('XRPUSDT','5m',12)
@app.route('/xrp_candles_1m')
def xrp_candles_1m():  return _klines('XRPUSDT','1m',30)
@app.route('/sol_candles')
def sol_candles():     return _klines('SOLUSDT','5m',12)
@app.route('/sol_candles_1m')
def sol_candles_1m():  return _klines('SOLUSDT','1m',30)

# ── Polymarket market lookup ──────────────────
@app.route('/btc_market')
def btc_market():   return _get_market('btc', request.args.get('slot',''))

@app.route('/live_price')
def live_price():
    """Token ID ile anlık CLOB midpoint — cache yok, çok hızlı."""
    up_tok = request.args.get('up','')
    dn_tok = request.args.get('dn','')
    if not up_tok and not dn_tok:
        return jsonify({'error': 'token_id gerekli'}), 400
    try:
        results = {}
        if up_tok:
            r = requests.get(f"{CLOB}/midpoint", params={'token_id': up_tok}, timeout=2)
            if r.ok:
                results['up'] = float(r.json().get('mid', 0))
        if dn_tok:
            r2 = requests.get(f"{CLOB}/midpoint", params={'token_id': dn_tok}, timeout=2)
            if r2.ok:
                results['dn'] = float(r2.json().get('mid', 0))
        # up+dn toplamı ~1 olmalı
        if 'up' in results and 'dn' not in results:
            results['dn'] = round(1.0 - results['up'], 4)
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/btc15_market')
def btc15_market():
    slot = request.args.get('slot','')
    # 15dk slug formatı: btc-updown-15m-{slot}
    slug = f"btc-updown-15m-{slot}"
    try:
        resp = requests.get(f"{GAMMA}/events", params={'slug': slug}, timeout=10)
        data = resp.json()
        if not data:
            # fallback: 5m slug ile dene
            return _get_market('btc', slot)
        event = data[0]
        raw = event.get('markets', [])
        if not raw:
            return jsonify({'market': None})
        m = raw[0]
        try:
            outcomes  = json.loads(m.get('outcomes',  '["Up","Down"]'))
            prices    = json.loads(m.get('outcomePrices', '[0.5,0.5]'))
            token_ids = json.loads(m.get('clobTokenIds', '[]'))
        except:
            outcomes=['Up','Down']; prices=[0.5,0.5]; token_ids=['','']
        up_idx   = next((i for i,o in enumerate(outcomes) if o.lower()=='up'),  0)
        down_idx = next((i for i,o in enumerate(outcomes) if o.lower()=='down'), 1)
        up_tok   = token_ids[up_idx]   if len(token_ids)>up_idx   else ''
        down_tok = token_ids[down_idx] if len(token_ids)>down_idx else ''

        # CLOB'dan anlık midpoint çek
        try:
            if up_tok:
                mp_r = requests.get(f"{CLOB}/midpoint", params={'token_id': up_tok}, timeout=3)
                if mp_r.ok:
                    up_mid = float(mp_r.json().get('mid', 0))
                    if 0.01 < up_mid < 0.99:
                        up_price   = up_mid
                        down_price = round(1.0 - up_mid, 4)
        except: pass

        # CLOB best ask
        up_ask = up_price; down_ask = down_price
        try:
            if up_tok:
                bk = requests.get(f"{CLOB}/book", params={'token_id': up_tok}, timeout=3).json()
                if bk.get('asks'): up_ask = float(bk['asks'][0]['price'])
            if down_tok:
                bk2 = requests.get(f"{CLOB}/book", params={'token_id': down_tok}, timeout=3).json()
                if bk2.get('asks'): down_ask = float(bk2['asks'][0]['price'])
        except: pass

        return jsonify({'market': {
            'id':               m.get('id') or m.get('conditionId',''),
            'title':            event.get('title', slug),
            'endDate':          m.get('endDate') or m.get('endDateIso',''),
            'up_price':         up_price,
            'down_price':       down_price,
            'up_token':         up_tok,
            'down_token':       down_tok,
            'accepting_orders': m.get('acceptingOrders', False),
            'up_best_ask':      up_ask,
            'down_best_ask':    down_ask,
        }})
    except Exception as e:
        return jsonify({'market': None, 'error': str(e)})
@app.route('/btc_result')
def btc_result():   return _get_result(request.args.get('market_id',''))

@app.route('/eth_market')
def eth_market():   return _get_market('eth', request.args.get('slot',''))
@app.route('/eth_result')
def eth_result():   return _get_result(request.args.get('market_id',''))

@app.route('/xrp_market')
def xrp_market():   return _get_market('xrp', request.args.get('slot',''))
@app.route('/xrp_result')
def xrp_result():   return _get_result(request.args.get('market_id',''))

@app.route('/sol_market')
def sol_market():   return _get_market('sol', request.args.get('slot',''))
@app.route('/sol_result')
def sol_result():   return _get_result(request.args.get('market_id',''))

# ── Bakiye ────────────────────────────────────
@app.route('/')
@app.route('/dashboard')
def serve_dashboard():
    """Dashboard HTML dosyasını serve et — cache'siz."""
    import os
    from flask import make_response
    script_dir = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(script_dir, 'polymarket_dashboard.html')
    if os.path.exists(html_path):
        resp = make_response(send_file(html_path))
        # Tarayıcı cache'ini tamamen devre dışı bırak
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
        return resp
    return "Dashboard bulunamadı: polymarket_dashboard.html proxy ile aynı klasörde olmalı.", 404

@app.route('/positions')
def get_positions():
    """Proxy cüzdanının açık pozisyonlarını döndür."""
    try:
        w3 = Web3(Web3.HTTPProvider(RPC))
        addr = w3.eth.account.from_key(PRIVATE_KEY).address if PRIVATE_KEY else EOA_ADDR
        resp = requests.get('https://data-api.polymarket.com/positions',
            params={'user': addr, 'limit': 100}, timeout=10)
        if not resp.ok:
            return jsonify([])
        return jsonify(resp.json())
    except Exception as e:
        return jsonify([])


@app.route('/balance')
def balance():
    try:
        w3   = Web3(Web3.HTTPProvider(RPC))
        usdc = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)

        proxy_usdc = 0.0
        if PRIVATE_KEY:
            proxy_addr = w3.eth.account.from_key(PRIVATE_KEY).address
            proxy_usdc = usdc.functions.balanceOf(proxy_addr).call() / 1e6

        eoa_usdc = usdc.functions.balanceOf(EOA_ADDR).call() / 1e6

        clob_bal = 0.0
        if API_KEY:
            try:
                cr = requests.get(f"{CLOB}/balance-allowance",
                    params={'asset_type':'USDC'},
                    headers={'POLY_API_KEY': API_KEY, 'POLY_API_SECRET': API_SECRET,
                             'POLY_API_PASSPHRASE': API_PASSPHRASE}, timeout=5)
                if cr.ok:
                    clob_bal = float(cr.json().get('balance', 0))
            except:
                pass

        proxy_pol = 0.0
        if PRIVATE_KEY:
            proxy_pol = w3.eth.get_balance(proxy_addr) / 1e18

        total = proxy_usdc + eoa_usdc + clob_bal
        return jsonify({'balance': round(total,2), 'proxy': round(proxy_usdc,2),
                        'eoa': round(eoa_usdc,2), 'clob': round(clob_bal,2),
                        'proxy_pol': round(proxy_pol,4)})
    except Exception as e:
        return jsonify({'balance': 0, 'error': str(e)})


@app.route('/trade_history')
def trade_history():
    """Polymarket Data API'den gerçek işlem geçmişini çek — TRADE ve REDEEM."""
    try:
        w3 = Web3(Web3.HTTPProvider(RPC))
        proxy_addr = w3.eth.account.from_key(PRIVATE_KEY).address if PRIVATE_KEY else EOA_ADDR

        limit = int(request.args.get('limit', 500))
        offset = int(request.args.get('offset', 0))
        btc_filter = request.args.get('btc_only', 'true').lower() == 'true'
        debug = request.args.get('debug', 'false').lower() == 'true'

        resp = requests.get(
            'https://data-api.polymarket.com/activity',
            params={'user': proxy_addr, 'limit': limit, 'offset': offset},
            timeout=15
        )
        if not resp.ok:
            return jsonify({'success': False, 'error': f'API {resp.status_code}'})

        activities = resp.json()

        # Debug: ham veriyi döndür
        if debug:
            return jsonify({'success': True, 'raw': activities[:5]})

        # Deposit toplamını da hesapla — proxy wallet'a gelen USDC transferleri
        total_deposit = 0.0
        _deposit_debug = {}
        try:
            pg_key = os.getenv('POLYGONSCAN_API_KEY', '')
            pg_resp = requests.get(
                'https://api.etherscan.io/v2/api',
                params={
                    'chainid': '137',  # Polygon PoS
                    'module': 'account',
                    'action': 'tokentx',
                    'contractaddress': '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174',  # USDC.e
                    'address': proxy_addr,
                    'sort': 'asc',
                    'apikey': pg_key if pg_key else 'YourApiKeyToken'
                },
                timeout=10
            )
            _deposit_debug['status'] = pg_resp.status_code
            _deposit_debug['has_key'] = bool(pg_key)
            if pg_resp.ok:
                pg_data = pg_resp.json()
                _deposit_debug['message'] = pg_data.get('message','')
                txs = pg_data.get('result', [])
                # Rate limit veya hata: result string dönebilir
                if isinstance(txs, str):
                    _deposit_debug['error'] = txs[:120]
                    print(f"[DEPOSIT] Polygonscan hata: {txs[:80]}")
                    txs = []
                if isinstance(txs, list):
                    _deposit_debug['tx_count'] = len(txs)
                    ctf = '0x4d97dcd97ec945f40cf65f87097ace5ea0476045'
                    for tx in txs:
                        if tx.get('to','').lower() == proxy_addr.lower():
                            val = float(tx.get('value', 0)) / 1e6
                            from_addr = tx.get('from','').lower()
                            if from_addr != ctf.lower() and val >= 1.0:
                                total_deposit += val
                    _deposit_debug['total'] = round(total_deposit, 2)
                    print(f"[DEPOSIT] {len(txs)} tx tarandı → deposit=${total_deposit:.2f}")
            else:
                _deposit_debug['error'] = f'HTTP {pg_resp.status_code}'
                print(f"[DEPOSIT] Polygonscan HTTP hata: {pg_resp.status_code}")
        except Exception as dep_e:
            _deposit_debug['exception'] = str(dep_e)[:100]
            print(f"[DEPOSIT] Exception: {dep_e}")

        # conditionId bazında grupla
        # Her conditionId = bir market = bir işlem çifti (TRADE in + REDEEM out)
        cond_trades = {}   # conditionId → {title, buy_cash, buy_size, buy_price, timestamp, outcome}
        cond_redeems = {}  # conditionId → redeem_cash

        for a in activities:
            title = (a.get('title') or a.get('market') or '')
            if btc_filter and 'bitcoin' not in title.lower():
                continue

            atype = (a.get('type') or '').upper()
            cid = (a.get('conditionId') or a.get('condition_id') or '')
            if not cid:
                continue

            # Gerçek alan adı: usdcSize
            cash = float(a.get('usdcSize') or a.get('cash') or a.get('usdcAmount') or 0)
            ts   = int(a.get('timestamp') or 0)

            if atype == 'TRADE':
                side = (a.get('side') or '').upper()
                if side != 'BUY': continue
                # outcome: "Up"/"Down" string veya outcomeIndex: 0/1
                outcome_str = str(a.get('outcome') or '')
                outcome_idx = a.get('outcomeIndex', -1)
                if outcome_str.lower() == 'up' or outcome_idx == 0:
                    outcome = 'UP'
                elif outcome_str.lower() == 'down' or outcome_idx == 1:
                    outcome = 'DOWN'
                else:
                    outcome = 'UP'
                size  = float(a.get('size') or 0)
                price = float(a.get('price') or 0)
                if cid not in cond_trades:
                    cond_trades[cid] = {
                        'title':     title[:50],
                        'buy_cash':  cash,
                        'buy_size':  size,
                        'buy_price': price,
                        'timestamp': ts,
                        'outcome':   outcome,
                        'conditionId': cid,
                    }
                else:
                    # Aynı market'e birden fazla emir → cash topla
                    cond_trades[cid]['buy_cash'] += cash

            elif atype == 'REDEEM':
                if cid not in cond_redeems:
                    cond_redeems[cid] = 0
                cond_redeems[cid] += cash

        # Trade listesi oluştur
        result = []
        for cid, t in cond_trades.items():
            redeem = cond_redeems.get(cid, 0)
            bet = t['buy_cash']
            if redeem > 0:
                status = 'win'
                pnl    = round(redeem - bet, 2)
                payout = round(redeem, 2)
            else:
                age_min = (int(__import__('time').time()) - t['timestamp']) / 60
                if age_min > 10:
                    status = 'loss'
                    pnl    = round(-bet, 2)
                    payout = 0
                else:
                    status = 'open'
                    pnl    = 0
                    payout = 0

            # Outcome zaten 'UP' veya 'DOWN' olarak set edildi
            dir_ = t['outcome']  # 'UP' veya 'DOWN'

            ts_ms  = t['timestamp'] * 1000
            result.append({
                'id':      ts_ms,
                'time':    __import__('datetime').datetime.fromtimestamp(
                               t['timestamp'], tz=__import__('datetime').timezone.utc
                           ).astimezone(
                               __import__('datetime').timezone(__import__('datetime').timedelta(hours=3))
                           ).strftime('%H:%M:%S'),
                'date':    __import__('datetime').datetime.fromtimestamp(
                               t['timestamp'], tz=__import__('datetime').timezone.utc
                           ).astimezone(
                               __import__('datetime').timezone(__import__('datetime').timedelta(hours=3))
                           ).strftime('%d.%m.%Y'),
                'dateISO': __import__('datetime').datetime.fromtimestamp(
                               t['timestamp'], tz=__import__('datetime').timezone.utc
                           ).isoformat(),
                'market':  t['title'],
                'dir':     dir_,
                'price':   t['buy_price'],
                'bet':     round(bet, 2),
                'size':    t['buy_size'],
                'pnl':     pnl,
                'payout':  payout,
                'status':  status,
                'dry':     False,
                'mid':     cid,
            })

        result.sort(key=lambda x: x['id'], reverse=True)

        wins   = sum(1 for t in result if t['status'] == 'win')
        losses = sum(1 for t in result if t['status'] == 'loss')
        total_pnl  = round(sum(t['pnl'] for t in result), 2)
        total_cost = round(sum(t['bet'] for t in result), 2)
        total_redeem = round(sum(t['payout'] for t in result), 2)

        return jsonify({
            'success':      True,
            'address':      proxy_addr,
            'count':        len(result),
            'wins':         wins,
            'losses':       losses,
            'total_cost':   total_cost,
            'total_redeem': total_redeem,
            'total_pnl':    total_pnl,
            'total_deposit': round(total_deposit, 2),
            'deposit_debug': _deposit_debug,
            'trades':       result
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ── Emir ver ──────────────────────────────────
@app.route('/place_order', methods=['POST'])
def place_order():
    global _last_order_time
    data     = request.json
    token_id = data.get('token_id', '')
    price    = float(data.get('price', 0.5))
    size     = float(data.get('size',  1.0))
    side     = data.get('side', 'BUY')

    if not token_id:
        return jsonify({'success': False, 'error': 'token_id eksik'})
    if not PRIVATE_KEY:
        return jsonify({'success': False, 'error': 'POLYMARKET_PRIVATE_KEY eksik'})

    print(f"[ORDER] Gönderiliyor: side={side} price={price} size={size} token={token_id[:20]}...")

    # Eş zamanlı emir gönderimini engelle — 2sn bekleme
    with _order_lock:
        now = time.time()
        elapsed = now - _last_order_time
        if elapsed < 2.0:
            time.sleep(2.0 - elapsed)
        _last_order_time = time.time()

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
        from py_clob_client.constants import POLYGON

        # PRIVATE_KEY (0xf32F) proxy trading wallet
        use_key = PRIVATE_KEY or METAMASK_KEY
        use_sig = 0

        # --- CLOB modu (Relayer entegrasyonu test aşamasında) ---
        # Creds: builder key varsa onu kullan, yoksa türet
        if API_KEY and API_SECRET and API_PASSPHRASE:
            creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
        else:
            tmp = ClobClient(host=CLOB, chain_id=POLYGON, key=use_key, signature_type=use_sig)
            creds = tmp.create_or_derive_api_creds()

        client = ClobClient(
            host=CLOB,
            chain_id=POLYGON,
            key=use_key,
            creds=creds,
            signature_type=use_sig,
        )

        order_args = OrderArgs(
            token_id=token_id,
            price=round(price, 2),
            size=round(size, 0),
            side=side,
        )

        # İmzalı order oluştur
        signed = client.create_order(order_args)

        # Builder header enjeksiyonu
        bh = _builder_headers()
        if bh:
            try:
                client._headers.update(bh)
            except AttributeError:
                pass

        # Gönder — GTC: market kapanana kadar bekle
        resp = client.post_order(signed, OrderType.GTC)
        print(f"[ORDER] CLOB yanıtı: {str(resp)[:200]}")
        return jsonify({'success': True, 'order': str(resp), 'mode': 'clob'})

    except Exception as e:
        # 401 gelirse creds yanlış demek — otomatik yeniden türet ve tekrar dene
        if '401' in str(e) or 'Unauthorized' in str(e):
            try:
                from py_clob_client.client import ClobClient
                from py_clob_client.clob_types import OrderArgs, OrderType
                from py_clob_client.constants import POLYGON
                use_key = METAMASK_KEY or PRIVATE_KEY
                tmp    = ClobClient(host=CLOB, chain_id=POLYGON, key=use_key, signature_type=0)
                creds2 = tmp.create_or_derive_api_creds()
                client2 = ClobClient(host=CLOB, chain_id=POLYGON, key=use_key,
                                     creds=creds2, signature_type=0)
                signed2 = client2.create_order(OrderArgs(
                    token_id=token_id, price=round(price,2), size=round(size,0), side=side))
                resp2 = client2.post_order(signed2, OrderType.GTC)
                return jsonify({'success': True, 'order': str(resp2), 'note': 'creds yenilendi'})
            except Exception as e2:
                return jsonify({'success': False, 'error': f'401 sonrası retry: {str(e2)}'})
        return jsonify({'success': False, 'error': str(e)})

# ── Telegram bildirim endpoint'leri ──────────
@app.route('/notify_trade', methods=['POST'])
def notify_trade():
    """Dashboard'dan işlem bildirimi al ve Telegram'a gönder."""
    try:
        d = request.json or {}
        tg_notify_trade(
            direction=d.get('direction','?'),
            price=float(d.get('price',0)),
            bet=float(d.get('bet',0)),
            status=d.get('status','open'),
            pnl=float(d.get('pnl',0)),
            market=d.get('market',''),
            bot=d.get('bot','BTC5')
        )
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ── Tüm emirleri iptal et ─────────────────────
@app.route('/cancel_all_orders', methods=['POST'])
def cancel_all_orders():
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        from py_clob_client.constants import POLYGON
        creds  = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
        client = ClobClient(host=CLOB, chain_id=POLYGON, key=PRIVATE_KEY,
                            creds=creds, signature_type=0)
        result = client.cancel_all()
        return jsonify({'success': True, 'result': str(result)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ── Proxy → EOA geri çekme ────────────────────
@app.route('/withdraw', methods=['POST'])
def withdraw():
    """Proxy wallet (0xf32F) → EOA (0x93ae) belirli miktar veya tüm USDC gönder."""
    try:
        if not PRIVATE_KEY or not METAMASK_KEY:
            return jsonify({'success': False, 'error': 'Key eksik'})
        data = request.json or {}
        amount_requested = float(data.get('amount', 0))  # 0 = tümü
        w3   = Web3(Web3.HTTPProvider(RPC))
        prx  = w3.eth.account.from_key(PRIVATE_KEY)
        eoa  = w3.eth.account.from_key(METAMASK_KEY)
        ERC20_ABI = [
            {"inputs":[{"name":"account","type":"address"}],
             "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],
             "stateMutability":"view","type":"function"},
            {"inputs":[{"name":"to","type":"address"},{"name":"amount","type":"uint256"}],
             "name":"transfer","outputs":[{"name":"","type":"bool"}],
             "stateMutability":"nonpayable","type":"function"},
        ]
        usdc = w3.eth.contract(address=USDC_ADDRESS, abi=ERC20_ABI)
        bal  = usdc.functions.balanceOf(prx.address).call()
        if bal == 0:
            return jsonify({'success': False, 'error': 'Proxy wallet USDC bakiyesi 0'})
        # Miktar belirtilmişse o kadar, yoksa tümü
        if amount_requested > 0:
            send_amount = min(int(amount_requested * 1e6), bal)
        else:
            send_amount = bal
        nonce = w3.eth.get_transaction_count(prx.address, 'latest')
        tx = usdc.functions.transfer(eoa.address, send_amount).build_transaction({
            'from': prx.address, 'nonce': nonce,
            'gas': 100000, 'gasPrice': w3.to_wei('150', 'gwei'), 'chainId': 137
        })
        signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        txh = w3.eth.send_raw_transaction(signed.raw_transaction)
        return jsonify({'success': True, 'tx': txh.hex(),
                        'amount_usdc': round(send_amount / 1e6, 2),
                        'to': eoa.address})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ── Bot log ───────────────────────────────────
@app.route('/bot_log')
def bot_log():
    # Önce günlük dosyayı dene, yoksa eski dosyaya bak
    log_file = _log_file_path()
    if not os.path.exists(log_file):
        log_file = os.path.join(os.path.dirname(__file__), 'btc_5m_bot.log')
    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        return jsonify({'lines': [l.strip() for l in lines[-200:] if l.strip()]})
    except FileNotFoundError:
        return jsonify({'lines': []})
    except Exception as e:
        return jsonify({'lines': [], 'error': str(e)})

@app.route('/log_write', methods=['POST'])
def log_write():
    """Dashboard'dan gelen log satırını dosyaya yaz."""
    try:
        d = request.json or {}
        level   = d.get('level', 'INFO').upper()
        message = d.get('message', '')
        source  = d.get('source', 'bLog')
        if message:
            write_bot_log(level, message, source)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/log_list')
def log_list():
    """Mevcut log dosyalarını listele."""
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        files = sorted([
            f for f in os.listdir(script_dir)
            if f.startswith('btc_5m_bot_') and f.endswith('.log')
        ], reverse=True)
        return jsonify({'files': files})
    except Exception as e:
        return jsonify({'files': [], 'error': str(e)})

@app.route('/log_download')
def log_download():
    """Belirli log dosyasını indir."""
    try:
        date = request.args.get('date', _dt.datetime.now().strftime('%Y-%m-%d'))
        script_dir = os.path.dirname(os.path.abspath(__file__))
        log_file = os.path.join(script_dir, f'btc_5m_bot_{date}.log')
        if os.path.exists(log_file):
            return send_file(log_file, as_attachment=True,
                           download_name=f'polybot_log_BTC5_{date}.txt')
        return jsonify({'error': 'Dosya bulunamadı'})
    except Exception as e:
        return jsonify({'error': str(e)})

# ── Pozisyon debug ────────────────────────────
@app.route('/check_positions')
def check_positions():
    """Her iki cüzdandaki pozisyonları listele — market_id verilirse sadece o markete bak."""
    try:
        market_id = request.args.get('market_id', '')
        w3 = Web3(Web3.HTTPProvider(RPC))
        result = {}
        wallets = {}
        if PRIVATE_KEY:
            wallets['proxy'] = w3.eth.account.from_key(PRIVATE_KEY).address
        if METAMASK_KEY:
            wallets['eoa'] = w3.eth.account.from_key(METAMASK_KEY).address

        for label, addr in wallets.items():
            resp = requests.get('https://data-api.polymarket.com/positions',
                params={'user': addr, 'limit': 100}, timeout=10)
            if not resp.ok:
                result[label] = {'error': f'HTTP {resp.status_code}', 'addr': addr}
                continue
            all_pos = resp.json()
            # market_id verilmişse sadece o markete ait pozisyonları filtrele
            if market_id:
                all_pos = [p for p in all_pos if market_id in (
                    p.get('conditionId',''), p.get('marketId',''),
                    p.get('market',''), p.get('asset_id','')
                )]
            positions = []
            for p in all_pos:
                positions.append({
                    'market':       p.get('title', p.get('market', ''))[:50],
                    'conditionId':  p.get('conditionId', '')[:20]+'...',
                    'redeemable':   p.get('redeemable', False),
                    'currentValue': float(p.get('currentValue') or 0),
                    'size':         float(p.get('size') or 0),
                    'outcome':      p.get('outcome', ''),
                })
            redeemable = [p for p in positions if p['redeemable']]
            result[label] = {
                'addr':        addr,
                'total':       len(positions),
                'redeemable':  len(redeemable),
                'positions':   positions[:20],
                'to_redeem':   redeemable,
            }
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)})

# ── Redeem ────────────────────────────────────
@app.route('/redeem', methods=['POST'])
def redeem():
    with _lock:
        try:
            force = (request.json or {}).get('force', False)
            w3 = Web3(Web3.HTTPProvider(RPC))
            addrs = []
            if PRIVATE_KEY:
                addrs.append((w3.eth.account.from_key(PRIVATE_KEY).address, PRIVATE_KEY))
            if METAMASK_KEY:
                addrs.append((w3.eth.account.from_key(METAMASK_KEY).address, METAMASK_KEY))
            if not addrs:
                return jsonify({'success': False, 'error': 'Key eksik'})

            CTF_ABI = [{"inputs":[{"name":"collateralToken","type":"address"},
                                   {"name":"parentCollectionId","type":"bytes32"},
                                   {"name":"conditionId","type":"bytes32"},
                                   {"name":"indexSets","type":"uint256[]"}],
                        "name":"redeemPositions","outputs":[],
                        "stateMutability":"nonpayable","type":"function"}]
            ctf = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
            redeemed_count = 0
            errors = []

            for addr, key in addrs:
                try:
                    resp = requests.get('https://data-api.polymarket.com/positions',
                        params={'user': addr, 'limit': 500}, timeout=10)
                    all_positions = resp.json() if resp.ok else []
                    positions = [p for p in all_positions if p.get('redeemable')]
                    if not positions:
                        continue
                    nonce = w3.eth.get_transaction_count(addr, 'latest')
                    for pos in positions:
                        cid = pos.get('conditionId', '')
                        if not cid:
                            continue
                        # force=True ise _redeemed'ı atla
                        if not force and cid in _redeemed:
                            continue
                        try:
                            tx = ctf.functions.redeemPositions(
                                USDC_ADDRESS, b'\x00'*32,
                                bytes.fromhex(cid.replace('0x','')), [1, 2]
                            ).build_transaction({
                                'from': addr, 'nonce': nonce,
                                'gas': 200000, 'gasPrice': w3.to_wei('300','gwei'), 'chainId': 137
                            })
                            signed_tx = w3.eth.account.sign_transaction(tx, key)
                            w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                            _redeemed.add(cid); _save_redeemed(_redeemed)
                            redeemed_count += 1
                            nonce += 1
                        except Exception as e:
                            err = str(e)
                            # already known = zaten TX gönderildi, saymaya devam
                            if 'already known' in err or 'nonce too low' in err:
                                redeemed_count += 1
                            else:
                                errors.append(err[:60])
                except Exception as e:
                    errors.append(f'{addr[:10]}: {str(e)[:60]}')

            return jsonify({'success': True, 'redeemed': redeemed_count, 'errors': errors})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})

@app.route('/redeem_clear', methods=['POST'])
def redeem_clear():
    """_redeemed cache'i temizle — sadece gerçekten gerektiğinde kullan.
    Dashboard forceRedeem() butonundan çağrılır.
    NOT: Bu endpoint artık sadece dashboard tarafından kontrollü çağrılmalı.
    """
    global _redeemed
    # Kaç adet temizlendi logla
    count = len(_redeemed)
    _redeemed = set()
    _save_redeemed(_redeemed)
    print(f"[REDEEM-CLEAR] {count} conditionId cache temizlendi.")
    return jsonify({'success': True, 'message': f'{count} conditionId temizlendi'})


# ── Belirli market redeem ──────────────────────
@app.route('/redeem_market', methods=['POST'])
def redeem_market():
    """Belirli bir market_id için redeem dene."""
    with _lock:
        try:
            market_id = request.args.get('market_id') or (request.json or {}).get('market_id', '')
            w3 = Web3(Web3.HTTPProvider(RPC))
            addrs = []
            if PRIVATE_KEY:
                addrs.append((w3.eth.account.from_key(PRIVATE_KEY).address, PRIVATE_KEY))
            if METAMASK_KEY:
                addrs.append((w3.eth.account.from_key(METAMASK_KEY).address, METAMASK_KEY))
            if not addrs:
                return jsonify({'success': False, 'error': 'Key eksik'})

            USDC_ABI = [{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]
            usdc = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)

            CTF_ABI = [{"inputs":[{"name":"collateralToken","type":"address"},
                                   {"name":"parentCollectionId","type":"bytes32"},
                                   {"name":"conditionId","type":"bytes32"},
                                   {"name":"indexSets","type":"uint256[]"}],
                        "name":"redeemPositions","outputs":[],
                        "stateMutability":"nonpayable","type":"function"}]
            ctf = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
            redeemed_count = 0
            errors = []
            txhash = ''
            payout = 0.0

            # Redeem öncesi bakiyeyi al
            bal_before = 0.0
            try:
                for addr, _ in addrs:
                    bal_before += usdc.functions.balanceOf(addr).call() / 1e6
            except: pass

            # Payout'u positions API'den de dene (fallback)
            payout_from_pos = 0.0
            try:
                for addr, _ in addrs:
                    resp2 = requests.get('https://data-api.polymarket.com/positions',
                        params={'user': addr, 'limit': 100}, timeout=10)
                    all_pos2 = resp2.json() if resp2.ok else []
                    for p in all_pos2:
                        if not p.get('redeemable'): continue
                        if market_id and market_id not in (p.get('conditionId',''), p.get('marketId',''), p.get('market','')):
                            continue
                        val = float(p.get('currentValue') or p.get('value') or 0)
                        payout_from_pos += val
                payout_from_pos = round(payout_from_pos, 2)
            except: pass

            for addr, key in addrs:
                try:
                    resp = requests.get('https://data-api.polymarket.com/positions',
                        params={'user': addr, 'limit': 100}, timeout=10)
                    all_pos = resp.json() if resp.ok else []

                    positions = []
                    for p in all_pos:
                        is_redeemable = p.get('redeemable')
                        if not is_redeemable:
                            continue
                        if market_id:
                            if market_id in (p.get('conditionId',''), p.get('marketId',''), p.get('market','')):
                                positions.append(p)
                        else:
                            positions.append(p)

                    if market_id and not positions:
                        positions = [p for p in all_pos if p.get('redeemable')]

                    if not positions:
                        continue

                    nonce = w3.eth.get_transaction_count(addr, 'latest')
                    for pos in positions:
                        cid = pos.get('conditionId', '')
                        if not cid or cid in _redeemed:
                            continue
                        try:
                            tx = ctf.functions.redeemPositions(
                                USDC_ADDRESS, b'\x00'*32,
                                bytes.fromhex(cid.replace('0x','')), [1, 2]
                            ).build_transaction({
                                'from': addr, 'nonce': nonce,
                                'gas': 200000, 'gasPrice': w3.to_wei('300','gwei'), 'chainId': 137
                            })
                            signed_tx = w3.eth.account.sign_transaction(tx, key)
                            raw = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                            txhash = raw.hex()
                            _redeemed.add(cid); _save_redeemed(_redeemed)
                            redeemed_count += 1
                            nonce += 1
                        except Exception as e:
                            errors.append(str(e)[:80])
                except Exception as e:
                    errors.append(f'{addr[:10]}: {str(e)[:60]}')

            # Payout = bakiye farkı (en güvenilir yöntem)
            # TX onaylanması birkaç sn sürer, kısa bekle
            if redeemed_count > 0:
                import time as _time
                _time.sleep(3)
                try:
                    bal_after = 0.0
                    for addr, _ in addrs:
                        bal_after += usdc.functions.balanceOf(addr).call() / 1e6
                    bal_diff = round(bal_after - bal_before, 2)
                    if bal_diff > 0:
                        payout = bal_diff
                    elif payout_from_pos > 0:
                        payout = payout_from_pos
                    print(f"[REDEEM] bal_before={bal_before:.2f} bal_after={bal_after:.2f} diff={bal_diff:.2f} pos_payout={payout_from_pos:.2f} → payout={payout:.2f}")
                except Exception as e:
                    if payout_from_pos > 0:
                        payout = payout_from_pos
                    print(f"[REDEEM] Bakiye farkı hesaplanamadı: {e}, pos_payout kullanılıyor: {payout_from_pos}")
            elif payout_from_pos > 0:
                payout = payout_from_pos

            return jsonify({'success': True, 'redeemed': redeemed_count,
                            'txhash': txhash, 'payout': payout, 'errors': errors})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})



@app.route('/derive_creds')
def derive_creds():
    """Trading wallet (0xf32F) için CLOB API creds türet ve .env'e yaz."""
    global API_KEY, API_SECRET, API_PASSPHRASE
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON
        # Emir gönderen wallet = PRIVATE_KEY (0xf32F)
        use_key = PRIVATE_KEY or METAMASK_KEY
        if not use_key:
            return jsonify({'success': False, 'error': 'POLYMARKET_PRIVATE_KEY eksik'})

        # Trading wallet — type=0
        tmp = ClobClient(host=CLOB, chain_id=POLYGON, key=use_key, signature_type=0)
        creds = tmp.create_or_derive_api_creds()

        # .env'e yaz
        env_path = os.path.join(os.path.dirname(__file__), '.env')
        lines = []
        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                lines = f.readlines()
        keys_to_set = {
            'POLYMARKET_API_KEY':        creds.api_key,
            'POLYMARKET_API_SECRET':     creds.api_secret,
            'POLYMARKET_API_PASSPHRASE': creds.api_passphrase,
        }
        updated = set()
        new_lines = []
        for line in lines:
            written = False
            for k, v in keys_to_set.items():
                if line.startswith(k + '='):
                    new_lines.append(f'{k}={v}\n')
                    updated.add(k)
                    written = True
                    break
            if not written:
                new_lines.append(line)
        for k, v in keys_to_set.items():
            if k not in updated:
                new_lines.append(f'{k}={v}\n')
        with open(env_path, 'w') as f:
            f.writelines(new_lines)
        API_KEY        = creds.api_key
        API_SECRET     = creds.api_secret
        API_PASSPHRASE = creds.api_passphrase
        print(f"[CREDS] Trading wallet creds türetildi: {creds.api_key[:16]}...")
        return jsonify({'success': True, 'api_key': creds.api_key,
                        'wallet': use_key[:20]+'...',
                        'message': '.env güncellendi, bellek güncellendi'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ── EOA'dan proxy wallet'a USDC transfer ─────
@app.route('/transfer_usdc', methods=['GET','POST'])
def transfer_usdc():
    """EOA (0x93ae) → proxy wallet (0xf32F) USDC transfer + proxy wallet CTF approve."""
    try:
        if not METAMASK_KEY or not PRIVATE_KEY:
            return jsonify({'success': False, 'error': 'Her iki key de gerekli'})
        w3   = Web3(Web3.HTTPProvider(RPC))
        eoa  = w3.eth.account.from_key(METAMASK_KEY)
        prx  = w3.eth.account.from_key(PRIVATE_KEY)

        ERC20_ABI = [
            {"inputs":[{"name":"account","type":"address"}],
             "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],
             "stateMutability":"view","type":"function"},
            {"inputs":[{"name":"to","type":"address"},{"name":"amount","type":"uint256"}],
             "name":"transfer","outputs":[{"name":"","type":"bool"}],
             "stateMutability":"nonpayable","type":"function"},
            {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
             "name":"approve","outputs":[{"name":"","type":"bool"}],
             "stateMutability":"nonpayable","type":"function"},
        ]
        usdc  = w3.eth.contract(address=USDC_ADDRESS, abi=ERC20_ABI)
        bal   = usdc.functions.balanceOf(eoa.address).call()
        txhashes = []

        if bal == 0:
            return jsonify({'success': False, 'error': f'EOA ({eoa.address}) USDC bakiyesi 0'})

        # 1) EOA → proxy wallet transfer (tüm bakiye)
        nonce = w3.eth.get_transaction_count(eoa.address, 'latest')
        tx = usdc.functions.transfer(prx.address, bal).build_transaction({
            'from': eoa.address, 'nonce': nonce,
            'gas': 100000, 'gasPrice': w3.to_wei('150','gwei'), 'chainId': 137
        })
        signed = w3.eth.account.sign_transaction(tx, METAMASK_KEY)
        txh = w3.eth.send_raw_transaction(signed.raw_transaction)
        txhashes.append({'step': 'transfer_eoa_to_proxy', 'tx': txh.hex(), 'amount_usdc': bal / 1e6})

        # 2) Proxy wallet → 3 kontrata max approve (CTF dahil)
        MAX = 2**256 - 1
        spenders = [
            Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"),
            Web3.to_checksum_address("0xC5d563A3D9370c4E3e5cFC4A7bEeae1A50C7Db47"),
            Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
        ]
        nonce2 = w3.eth.get_transaction_count(prx.address, 'latest')
        for sp in spenders:
            tx2 = usdc.functions.approve(sp, MAX).build_transaction({
                'from': prx.address, 'nonce': nonce2,
                'gas': 100000, 'gasPrice': w3.to_wei('150','gwei'), 'chainId': 137
            })
            s2 = w3.eth.account.sign_transaction(tx2, PRIVATE_KEY)
            th2 = w3.eth.send_raw_transaction(s2.raw_transaction)
            txhashes.append({'step': f'approve_{sp[:10]}', 'tx': th2.hex()})
            nonce2 += 1

        return jsonify({'success': True, 'txhashes': txhashes,
                        'note': f'{bal/1e6:.2f} USDC transfer + 3 approve gönderildi'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ── EOA approve (3 kontrата max allowance) ────
@app.route('/deposit_eoa', methods=['GET','POST'])
def deposit_eoa():
    """EOA wallet'tan 3 Polymarket exchange kontratına max USDC approve ver."""
    try:
        use_key = METAMASK_KEY or PRIVATE_KEY
        if not use_key:
            return jsonify({'success': False, 'error': 'METAMASK_PRIVATE_KEY eksik'})
        w3   = Web3(Web3.HTTPProvider(RPC))
        acct = w3.eth.account.from_key(use_key)
        APPROVE_ABI = [{"inputs":[{"name":"spender","type":"address"},
                                   {"name":"amount","type":"uint256"}],
                        "name":"approve","outputs":[{"name":"","type":"bool"}],
                        "stateMutability":"nonpayable","type":"function"}]
        usdc = w3.eth.contract(address=USDC_ADDRESS, abi=APPROVE_ABI)
        MAX  = 2**256 - 1
        spenders = [
            Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"),  # CTF Exchange
            Web3.to_checksum_address("0xC5d563A3D9370c4E3e5cFC4A7bEeae1A50C7Db47"),  # Neg risk
            Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),  # Neg risk adapter
        ]
        nonce  = w3.eth.get_transaction_count(acct.address, 'latest')
        txhashes = []
        for sp in spenders:
            tx = usdc.functions.approve(sp, MAX).build_transaction({
                'from': acct.address, 'nonce': nonce,
                'gas': 100000, 'gasPrice': w3.to_wei('150','gwei'), 'chainId': 137
            })
            signed_tx = w3.eth.account.sign_transaction(tx, use_key)
            txh = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            txhashes.append(txh.hex())
            nonce += 1
        return jsonify({'success': True, 'txhashes': txhashes,
                        'message': '3 kontrата max approve gönderildi'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ── Deposit debug ────────────────────────────
@app.route('/deposit_debug')
def deposit_debug():
    """Polygonscan deposit hesabını test et — tarayıcıdan açılabilir."""
    try:
        w3 = Web3(Web3.HTTPProvider(RPC))
        proxy_addr = w3.eth.account.from_key(PRIVATE_KEY).address if PRIVATE_KEY else EOA_ADDR
        pg_key = os.getenv('POLYGONSCAN_API_KEY', '')
        pg_resp = requests.get(
            'https://api.etherscan.io/v2/api',
            params={
                'chainid': '137',  # Polygon PoS
                'module': 'account', 'action': 'tokentx',
                'contractaddress': '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174',
                'address': proxy_addr, 'sort': 'asc',
                'apikey': pg_key if pg_key else 'YourApiKeyToken'
            }, timeout=10
        )
        pg_data = pg_resp.json()
        txs = pg_data.get('result', [])
        total = 0.0
        deposits = []
        ctf = '0x4d97dcd97ec945f40cf65f87097ace5ea0476045'
        if isinstance(txs, list):
            for tx in txs:
                if tx.get('to','').lower() == proxy_addr.lower():
                    val = float(tx.get('value', 0)) / 1e6
                    from_addr = tx.get('from','').lower()
                    if from_addr != ctf.lower() and val >= 1.0:
                        total += val
                        deposits.append({'from': tx.get('from','')[:20], 'val': val, 'hash': tx.get('hash','')[:20]})
        return jsonify({
            'proxy_addr': proxy_addr,
            'has_api_key': bool(pg_key),
            'api_key_preview': pg_key[:8]+'...' if pg_key else 'YOK',
            'polygonscan_message': pg_data.get('message',''),
            'polygonscan_status': pg_data.get('status',''),
            'tx_total': len(txs) if isinstance(txs, list) else txs,
            'deposit_total': round(total, 2),
            'deposit_txs': deposits
        })
    except Exception as e:
        return jsonify({'error': str(e)})

# ── CLOB allowance kontrol ────────────────────
@app.route('/check_allowance')
def check_allowance():
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams
        from py_clob_client.constants import POLYGON
        use_key = PRIVATE_KEY or METAMASK_KEY
        creds   = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
        client  = ClobClient(host=CLOB, chain_id=POLYGON, key=use_key,
                             creds=creds, signature_type=0)
        from py_clob_client.clob_types import AssetType as AT
        asset_val = getattr(AT, 'COLLATERAL', None) or getattr(AT, 'USDC', None) or getattr(AT, 'collateral', None)
        params  = BalanceAllowanceParams(asset_type=asset_val) if asset_val else BalanceAllowanceParams()
        result  = client.get_balance_allowance(params)
        return jsonify({'success': True, 'data': str(result)})
    except Exception as e:
        try:
            r = requests.get(f"{CLOB}/balance-allowance",
                params={'asset_type': 'USDC'},
                headers={'POLY_API_KEY': API_KEY, 'POLY_API_SECRET': API_SECRET,
                         'POLY_API_PASSPHRASE': API_PASSPHRASE}, timeout=5)
            return jsonify({'success': True, 'data': r.json()})
        except Exception as e2:
            return jsonify({'success': False, 'error': str(e2)})

# ─────────────────────────────────────────────
# NOT: _auto_redeem_loop kaldırıldı (v3.18.0)
# Redeem işlemi artık sadece dashboard OTO-REDEEM tarafından yönetiliyor.
# Python loop + dashboard aynı anda TX gönderince çakışma ve patlama oluyordu.
# Redeem tetikleyicileri: /redeem, /redeem_market, /redeem (force) endpoint'leri.


# ── Otomatik güncelleme ───────────────────────
GITHUB_RAW = "https://raw.githubusercontent.com/zihnitas/polybot/main"

@app.route('/update', methods=['POST'])
def update():
    """GitHub'dan son versiyonu çek, dosyaları güncelle, proxy'yi yeniden başlat."""
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        updated = []
        errors = []

        # proxy_server.py güncelle
        try:
            r = requests.get(f"{GITHUB_RAW}/proxy_server.py", timeout=15)
            if r.ok and len(r.text) > 1000:
                proxy_path = os.path.join(script_dir, 'proxy_server.py')
                with open(proxy_path, 'w', encoding='utf-8') as f:
                    f.write(r.text)
                updated.append('proxy_server.py')
            else:
                errors.append(f'proxy_server.py: HTTP {r.status_code}')
        except Exception as e:
            errors.append(f'proxy_server.py: {str(e)[:60]}')

        # polymarket_dashboard.html güncelle
        try:
            r2 = requests.get(f"{GITHUB_RAW}/polymarket_dashboard.html", timeout=15)
            if r2.ok and len(r2.text) > 1000:
                html_path = os.path.join(script_dir, 'polymarket_dashboard.html')
                with open(html_path, 'w', encoding='utf-8') as f:
                    f.write(r2.text)
                updated.append('polymarket_dashboard.html')
            else:
                errors.append(f'polymarket_dashboard.html: HTTP {r2.status_code}')
        except Exception as e:
            errors.append(f'polymarket_dashboard.html: {str(e)[:60]}')

        if errors:
            return jsonify({'success': False, 'updated': updated, 'errors': errors})

        return jsonify({
            'success': True,
            'updated': updated,
            'message': 'Güncelleme tamamlandı! Proxy yeniden başlatmanız gerekiyor (BASLAT.bat).'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/check_update')
def check_update():
    """GitHub'daki son versiyonu kontrol et."""
    try:
        r = requests.get(f"{GITHUB_RAW}/proxy_server.py", timeout=10)
        if not r.ok:
            return jsonify({'success': False, 'error': f'HTTP {r.status_code}'})
        gh_proxy_version = None
        for line in r.text.split('\n'):
            if line.strip().startswith('VERSION'):
                try: gh_proxy_version = line.split('"')[1]
                except: 
                    try: gh_proxy_version = line.split("'")[1]
                    except: pass
                break
        # Dashboard versiyonunu da çek
        r2 = requests.get(f"{GITHUB_RAW}/polymarket_dashboard.html", timeout=10)
        gh_dash_version = None
        if r2.ok:
            for line in r2.text.split('\n'):
                if 'DASHBOARD_VERSION' in line:
                    try: gh_dash_version = line.split("'")[1]
                    except: pass
                    break
        proxy_up_to_date = VERSION == gh_proxy_version
        return jsonify({
            'success': True,
            'current': VERSION,
            'latest': gh_proxy_version,
            'dash_latest': gh_dash_version,
            'up_to_date': proxy_up_to_date
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

if __name__ == '__main__':
    tg = threading.Thread(target=_tg_bot_loop, daemon=True)
    tg.start()
    print(f"Proxy sunucu v{VERSION} baslatiliyor: http://127.0.0.1:5000")
    print(f"Telegram bot aktif: Chat ID={TG_CHAT_ID}")
    app.run(port=5000, debug=False)
