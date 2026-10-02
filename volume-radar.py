#!/usr/bin/env python3
"""Serial multi-chain volume radar using GeckoTerminal and DEX Screener."""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / "volume.env"
STATE_FILE = Path(os.environ.get("VOLUME_STATE_FILE", str(ROOT / "runtime-state.json")))


def load_env_file(path=ENV_FILE):
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env_file()


def env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


def env_float(name, default):
    try:
        return float(os.environ.get(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


def parse_csv(value):
    return tuple(item.strip().lower() for item in value.split(",") if item.strip())


def parse_mapping(value, defaults):
    mapping = dict(defaults)
    for item in value.split(","):
        if ":" not in item:
            continue
        key, mapped = item.split(":", 1)
        if key.strip() and mapped.strip():
            mapping[key.strip().lower()] = mapped.strip().lower()
    return mapping


CHAINS = parse_csv(os.environ.get("VOLUME_CHAINS", "bsc,base,eth,arbitrum"))
TOP_N = max(1, env_int("VOLUME_TOP_N", 25))
MAX_TREND_PAGES = max(1, min(10, env_int("MAX_TREND_PAGES", 10)))
TREND_DURATION = os.environ.get("VOLUME_TREND_DURATION", "1h").strip().lower()
MIN_MARKET_CAP = env_float("VOLUME_MIN_MARKET_CAP", 500_000)
MAX_MARKET_CAP = env_float("VOLUME_MAX_MARKET_CAP", 50_000_000)
MIN_VOLUME_15M = env_float("VOLUME_SPIKE_MIN_15M", 500_000)
MIN_VOLUME_1H = env_float("VOLUME_SPIKE_MIN_1H", 1_000_000)
ALERT_COOLDOWN_SECONDS = max(0, env_int("VOLUME_ALERT_COOLDOWN_SECONDS", 900))
CYCLE_COOLDOWN_SECONDS = max(0, env_int("CYCLE_COOLDOWN_SECONDS", 180))
# GeckoTerminal documents approximately 10 calls/minute for its public API.
# Eight/minute leaves margin for jitter and other tools sharing the VPS IP.
GECKO_MIN_INTERVAL = max(6.0, env_float("GECKO_MIN_INTERVAL_SECONDS", 7.5))
DEX_MIN_INTERVAL = max(0.2, env_float("DEX_MIN_INTERVAL_SECONDS", 1.0))
HTTP_TIMEOUT = max(5, env_int("HTTP_TIMEOUT_SECONDS", 25))
INCLUDE_UNKNOWN_MARKET_CAP = os.environ.get(
    "INCLUDE_UNKNOWN_MARKET_CAP", "0"
).strip().lower() in {"1", "true", "yes", "on"}

GECKO_NETWORKS = parse_mapping(
    os.environ.get("GECKO_NETWORK_MAP", ""),
    {
        "bsc": "bsc",
        "base": "base",
        "eth": "eth",
        "ethereum": "eth",
        "arbitrum": "arbitrum",
        "optimism": "optimism",
        "polygon": "polygon_pos",
        "sol": "solana",
        "solana": "solana",
        "avalanche": "avax",
        "avax": "avax",
        "linea": "linea",
        "blast": "blast",
        "mantle": "mantle",
        "scroll": "scroll",
        "zksync": "zksync",
        "sonic": "sonic",
    },
)
DEX_CHAINS = parse_mapping(
    os.environ.get("DEX_CHAIN_MAP", ""),
    {
        "bsc": "bsc",
        "base": "base",
        "eth": "ethereum",
        "ethereum": "ethereum",
        "arbitrum": "arbitrum",
        "optimism": "optimism",
        "polygon": "polygon",
        "sol": "solana",
        "solana": "solana",
        "avalanche": "avalanche",
        "avax": "avalanche",
        "linea": "linea",
        "blast": "blast",
        "mantle": "mantle",
        "scroll": "scroll",
        "zksync": "zksync",
        "sonic": "sonic",
    },
)

STOCK_SYMBOLS = {
    item.strip().upper()
    for item in os.environ.get(
        "STOCK_TOKEN_SYMBOLS",
        "AAPL,ABT,ADBE,AMD,AMZN,AVGO,BA,BAC,BRK.B,COIN,COST,CRM,CSCO,DIS,GOOG,GOOGL,HD,INTC,JNJ,JPM,KO,LLY,MA,MCD,META,MRK,MSFT,MSTR,NFLX,NKE,NVDA,ORCL,PFE,PLTR,PYPL,QQQ,SPY,TMO,TSLA,TSM,UNH,V,VTI,WMT,XOM",
    ).split(",")
    if item.strip()
}
STOCK_NAME_RE = re.compile(
    r"\b(?:stock|stocks|equity|equities|share|shares|tokenized|tokenised|xstock)\b",
    re.IGNORECASE,
)

TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_RADAR_GROUP_CHAT_ID", "").strip()
TG_THREAD_ID = os.environ.get("TG_VOLUME_THREAD_ID", "").strip()
TIMEZONE = os.environ.get("RADAR_TIMEZONE", "Asia/Jakarta").strip()
LOCATION = os.environ.get("RADAR_LOCATION", "Pemangkat").strip()

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
DEX_BASE = "https://api.dexscreener.com"
GECKO_ACCEPT = "application/json;version=20230302"


class RateGate:
    """Minimum spacing between calls to one provider; all calls stay serial."""

    def __init__(self, interval, label):
        self.interval = interval
        self.label = label
        self.last_call = 0.0

    def wait(self):
        delay = self.interval - (time.monotonic() - self.last_call)
        if delay > 0:
            print(f"[{self.label}] rate pacing: wait {delay:.1f}s")
            time.sleep(delay)
        self.last_call = time.monotonic()


GECKO_GATE = RateGate(GECKO_MIN_INTERVAL, "GeckoTerminal")
DEX_GATE = RateGate(DEX_MIN_INTERVAL, "DEX Screener")


def request_json(url, provider):
    gate = GECKO_GATE if provider == "GeckoTerminal" else DEX_GATE
    headers = {"Accept": GECKO_ACCEPT if provider == "GeckoTerminal" else "application/json"}
    for attempt in range(2):
        gate.wait()
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:400]
            if exc.code == 429 and attempt == 0:
                retry_after = exc.headers.get("Retry-After", "")
                try:
                    delay = max(1, min(300, int(float(retry_after))))
                except (TypeError, ValueError):
                    delay = 60
                print(f"[{provider}] HTTP 429; pause {delay}s sebelum retry satu kali")
                time.sleep(delay)
                continue
            raise RuntimeError(f"{provider} HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == 0:
                delay = 5
                print(f"[{provider}] request gagal ({exc}); retry satu kali dalam {delay}s")
                time.sleep(delay)
                continue
            raise RuntimeError(f"{provider} request gagal: {exc}") from exc
    raise RuntimeError(f"{provider} request gagal setelah retry")


def included_tokens(data):
    output = {}
    for item in data.get("included", []) or []:
        if item.get("type") == "token":
            output[str(item.get("id") or "").lower()] = item.get("attributes") or {}
    return output


def token_from_relationship(pool, included, relation_name, network):
    relation = (pool.get("relationships") or {}).get(relation_name) or {}
    token_ref = relation.get("data") or {}
    token_id = str(token_ref.get("id") or "")
    token_data = included.get(token_id.lower(), {})
    address = token_data.get("address")
    if not address and "_" in token_id:
        address = token_id.split("_", 1)[1]
    return {
        "address": str(address or "").strip(),
        "name": str(token_data.get("name") or "").strip(),
        "symbol": str(token_data.get("symbol") or "").strip(),
        "network": network,
    }


def as_positive_float(value):
    try:
        result = float(value)
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None


def is_stock_token(token):
    symbol = str(token.get("symbol") or "").strip().upper()
    normalized = re.sub(r"[^A-Z0-9.]", "", symbol)
    name = str(token.get("name") or "")
    return (
        normalized in STOCK_SYMBOLS
        or normalized.endswith("STOCK")
        or bool(STOCK_NAME_RE.search(name))
    )


def fetch_trending_candidates(chain):
    network = GECKO_NETWORKS.get(chain)
    if not network:
        print(f"[{chain}] skip: GECKO_NETWORK_MAP belum punya mapping")
        return []

    candidates = []
    seen_tokens = set()
    pages = 0
    for page in range(1, MAX_TREND_PAGES + 1):
        query = urllib.parse.urlencode({
            "include": "base_token,quote_token",
            "page": page,
            "duration": TREND_DURATION,
        })
        url = f"{GECKO_BASE}/networks/{urllib.parse.quote(network, safe='')}/trending_pools?{query}"
        try:
            payload = request_json(url, "GeckoTerminal")
        except RuntimeError as exc:
            print(f"[{chain}] trending gagal: {exc}")
            break

        pools = payload.get("data", []) if isinstance(payload, dict) else []
        included = included_tokens(payload) if isinstance(payload, dict) else {}
        pages += 1
        if not pools:
            break

        for pool in pools:
            attrs = pool.get("attributes") or {}
            token = token_from_relationship(pool, included, "base_token", network)
            address = token["address"]
            if not address or address.lower() in seen_tokens:
                continue
            seen_tokens.add(address.lower())
            if is_stock_token(token):
                continue

            volume = attrs.get("volume_usd") or {}
            transactions = attrs.get("transactions") or {}
            candidates.append({
                "chain": chain,
                "network": network,
                "address": address,
                "symbol": token["symbol"] or "?",
                "name": token["name"] or "",
                "pool_address": str(attrs.get("address") or "").strip(),
                "pool_name": str(attrs.get("name") or "").strip(),
                "market_cap": as_positive_float(attrs.get("market_cap_usd")),
                "market_cap_source": "GeckoTerminal",
                "liquidity": as_positive_float(attrs.get("reserve_in_usd")) or 0,
                "volume_15m": as_positive_float(volume.get("m15")) or 0,
                "volume_1h": as_positive_float(volume.get("h1")) or 0,
                "tx_buys_15m": int((transactions.get("m15") or {}).get("buys") or 0),
                "tx_sells_15m": int((transactions.get("m15") or {}).get("sells") or 0),
                "trend_rank": len(candidates) + 1,
                "sampled_at": int(time.time()),
            })
            if len(candidates) >= TOP_N:
                break
        if len(candidates) >= TOP_N or len(pools) < 20:
            break

    print(f"[{chain}] trending pages={pages}, unique eligible before MC={len(candidates)}")
    return candidates[:TOP_N]


def dexscreener_market_caps(chain, candidates):
    """Fetch market-cap fallback in one batched lookup for unknown GT caps."""
    unknown = [candidate for candidate in candidates if candidate["market_cap"] is None]
    dex_chain = DEX_CHAINS.get(chain)
    if not unknown or not dex_chain:
        if unknown and not dex_chain:
            print(f"[{chain}] DEX fallback dilewati: DEX_CHAIN_MAP belum punya mapping")
        return

    # API permits up to 30 addresses per batch; TOP_N defaults to 25.
    for start in range(0, len(unknown), 30):
        batch = unknown[start:start + 30]
        addresses = ",".join(candidate["address"] for candidate in batch)
        url = f"{DEX_BASE}/tokens/v1/{urllib.parse.quote(dex_chain, safe='')}/{urllib.parse.quote(addresses, safe=',')}"
        try:
            pairs = request_json(url, "DEX Screener")
        except RuntimeError as exc:
            print(f"[{chain}] DEX market-cap fallback gagal: {exc}")
            continue
        if not isinstance(pairs, list):
            continue

        by_address = {candidate["address"].lower(): candidate for candidate in batch}
        matches = {}
        for pair in pairs:
            base = pair.get("baseToken") or {}
            address = str(base.get("address") or "").lower()
            if address not in by_address:
                continue
            market_cap = as_positive_float(pair.get("marketCap"))
            if market_cap is None:
                continue
            liquidity = as_positive_float((pair.get("liquidity") or {}).get("usd")) or 0
            current = matches.get(address)
            if current is None or liquidity > current[0]:
                matches[address] = (liquidity, market_cap, base)

        for address, (_liquidity, market_cap, base) in matches.items():
            candidate = by_address[address]
            candidate["market_cap"] = market_cap
            candidate["market_cap_source"] = "DEX Screener"
            candidate["symbol"] = candidate["symbol"] or base.get("symbol") or "?"
            candidate["name"] = candidate["name"] or base.get("name") or ""


def market_cap_eligible(candidate):
    market_cap = candidate.get("market_cap")
    if market_cap is None:
        return INCLUDE_UNKNOWN_MARKET_CAP
    return MIN_MARKET_CAP <= market_cap <= MAX_MARKET_CAP


def money(value):
    value = float(value or 0)
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}k"
    return f"${value:.0f}"


def threshold_status(candidate):
    pass_15m = candidate["volume_15m"] >= MIN_VOLUME_15M
    pass_1h = candidate["volume_1h"] >= MIN_VOLUME_1H
    if not pass_15m and not pass_1h:
        return None
    if pass_15m and pass_1h:
        label = "🔥 BOTH PASS"
        status = "both"
    elif pass_15m:
        label = "⚡ 15M PASS"
        status = "15m"
    else:
        label = "🕐 1H PASS"
        status = "1h"
    return {
        "pass_15m": pass_15m,
        "pass_1h": pass_1h,
        "label": label,
        "status": status,
    }


def load_state():
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temp.replace(STATE_FILE)


def filter_repeat_alerts(matches, state):
    now = int(time.time())
    last_alerts = state.get("last_alerts", {})
    if not isinstance(last_alerts, dict):
        last_alerts = {}
    active = {}
    filtered = []
    for match in matches:
        key = f"{match['chain']}:{match['address'].lower()}"
        previous = last_alerts.get(key) or {}
        if isinstance(previous, dict):
            sent_at = int(previous.get("sent_at") or 0)
            old_status = previous.get("status")
        else:
            sent_at = int(previous or 0)
            old_status = None
        info = threshold_status(match)
        if info is None:
            continue
        if sent_at and now - sent_at < ALERT_COOLDOWN_SECONDS and old_status == info["status"]:
            active[key] = previous
            continue
        match.update(info)
        filtered.append(match)
        active[key] = {"sent_at": now, "status": info["status"]}

    # Keep unexpired historical entries so restarts do not resend the same alert.
    for key, value in last_alerts.items():
        if key in active:
            continue
        sent_at = value.get("sent_at", 0) if isinstance(value, dict) else value
        try:
            if now - int(sent_at) < ALERT_COOLDOWN_SECONDS:
                active[key] = value
        except (TypeError, ValueError):
            continue
    state["last_alerts"] = active
    return filtered


def build_report(matches, cycle_time):
    try:
        local_tz = ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError:
        local_tz = timezone.utc
    stamp = datetime.fromtimestamp(cycle_time, local_tz).strftime("%H:%M")
    lines = [f"<b>👀 VOLUME WATCH · {stamp} {html.escape(LOCATION)}</b>", ""]
    for token in matches:
        v15 = "✅" if token["pass_15m"] else "⚠️"
        v1h = "✅" if token["pass_1h"] else "⚠️"
        average_15m = token["volume_1h"] / 4
        spike = token["volume_15m"] / average_15m if average_15m > 0 else 0
        symbol = html.escape(str(token.get("symbol") or "?")[:24])
        ca = html.escape(str(token["address"]))
        chain = html.escape(token["chain"].upper())
        lines.extend([
            f"<b>{symbol}</b> · {token['label']} · {chain}",
            f"15M: {v15} {money(token['volume_15m'])} / min {money(MIN_VOLUME_15M)}",
            f"1H: {v1h} {money(token['volume_1h'])} / min {money(MIN_VOLUME_1H)}",
            f"SPIKE: {spike:.2f}x",
            f"MC: {money(token['market_cap']) if token.get('market_cap') else 'unknown'}",
            f"CA: <code>{ca}</code>",
            "",
        ])
    lines.extend([
        "<b>RULE</b>",
        "Alert jika volume 15M ATAU 1H memenuhi minimum.",
        "Volume berdasarkan pool trending GeckoTerminal; MC diverifikasi melalui DEX Screener bila data GeckoTerminal kosong.",
        "",
        "Volume besar bukan jaminan aman untuk LP.",
    ])
    return "\n".join(lines)


def split_message(text, limit=3800):
    chunks = []
    current = []
    current_size = 0
    for line in text.splitlines():
        size = len(line) + 1
        if current and current_size + size > limit:
            chunks.append("\n".join(current))
            current, current_size = [], 0
        current.append(line)
        current_size += size
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def send_telegram(report):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("telegram=disabled (TG_BOT_TOKEN/TG_RADAR_GROUP_CHAT_ID belum diisi)")
        print(report)
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for part in split_message(report):
        values = {
            "chat_id": TG_CHAT_ID,
            "text": part,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if TG_THREAD_ID:
            values["message_thread_id"] = TG_THREAD_ID
        request = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(values).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API returned: {result}")
    print(f"telegram=sent({len(split_message(report))} msg)")
    return True


def run_cycle(state):
    cycle_started = int(time.time())
    print_credit()
    print(f"cycle=start {datetime.now().astimezone().isoformat(timespec='seconds')}")
    if not CHAINS:
        raise RuntimeError("VOLUME_CHAINS kosong; isi minimal satu chain di volume.env")
    if TREND_DURATION not in {"5m", "1h", "6h", "24h"}:
        raise RuntimeError("VOLUME_TREND_DURATION harus salah satu: 5m,1h,6h,24h")

    all_matches = []
    totals = {"candidates": 0, "eligible": 0, "unknown_mc": 0, "over_cap": 0, "under_min": 0}
    for chain in CHAINS:
        print(f"\n[{chain}] ambil top {TOP_N} trending pools, serial")
        candidates = fetch_trending_candidates(chain)
        totals["candidates"] += len(candidates)
        dexscreener_market_caps(chain, candidates)

        chain_matches = []
        for candidate in candidates:
            if candidate["market_cap"] is None:
                totals["unknown_mc"] += 1
                if not INCLUDE_UNKNOWN_MARKET_CAP:
                    continue
            elif candidate["market_cap"] > MAX_MARKET_CAP:
                totals["over_cap"] += 1
                continue
            elif candidate["market_cap"] < MIN_MARKET_CAP:
                totals["under_min"] += 1
                continue

            totals["eligible"] += 1
            status = threshold_status(candidate)
            if status:
                candidate.update(status)
                chain_matches.append(candidate)
            print(
                f"[{chain}] #{candidate['trend_rank']:02d} {candidate['symbol'][:14]:<14} "
                f"MC={money(candidate['market_cap']) if candidate.get('market_cap') else 'unknown':>9} "
                f"15m={money(candidate['volume_15m']):>8} 1h={money(candidate['volume_1h']):>8} "
                f"{'MATCH' if status else 'skip-volume'}"
            )
        all_matches.extend(chain_matches)
        print(f"[{chain}] selesai eligible={sum(1 for t in candidates if market_cap_eligible(t))} matches={len(chain_matches)}")

    send_matches = filter_repeat_alerts(all_matches, state)
    if send_matches:
        report = build_report(send_matches, int(time.time()))
        try:
            delivered = send_telegram(report)
            if not delivered:
                raise RuntimeError("Telegram tidak dikirim; periksa konfigurasi bot/chat/topic")
        except Exception as exc:
            print(f"telegram=FAIL {exc}")
            # Do not consume cooldown when delivery failed; allow a retry next cycle.
            last_alerts = state.get("last_alerts", {})
            for token in send_matches:
                last_alerts.pop(f"{token['chain']}:{token['address'].lower()}", None)
            state["last_alerts"] = last_alerts
    else:
        print("volume=matches(0)")

    finished = int(time.time())
    state["last_cycle_finished"] = finished
    state["cycle_count"] = int(state.get("cycle_count", 0)) + 1
    save_state(state)
    print(
        "cycle=done "
        f"candidates({totals['candidates']}) eligible({totals['eligible']}) "
        f"unknown_mc({totals['unknown_mc']}) over_max({totals['over_cap']}) "
        f"under_min({totals['under_min']}) matches({len(all_matches)}) "
        f"alerts({len(send_matches)}) duration({finished - cycle_started}s)"
    )
    return finished


def print_credit():
    print("  *==========================================*")
    print("    > Built by: Noya-xen (Github)")
    print("    > Follow me on X : @xinomixo")
    print("  *==========================================*\n")


def main():
    parser = argparse.ArgumentParser(description="Serial GeckoTerminal/DEX Screener volume radar")
    parser.add_argument("--once", action="store_true", help="jalankan satu siklus lalu keluar")
    args = parser.parse_args()
    state = load_state()

    while True:
        prior_finish = int(state.get("last_cycle_finished", 0) or 0)
        remaining = CYCLE_COOLDOWN_SECONDS - (int(time.time()) - prior_finish)
        if prior_finish and remaining > 0 and not args.once:
            print(f"cycle=waiting-after-previous({remaining}s)")
            time.sleep(remaining)
        try:
            completed_at = run_cycle(state)
            if args.once:
                return 0
            print(f"next_cycle=after({CYCLE_COOLDOWN_SECONDS}s from completion)")
            time.sleep(CYCLE_COOLDOWN_SECONDS)
            state["last_cycle_finished"] = completed_at
        except KeyboardInterrupt:
            print("stopped=keyboard-interrupt")
            return 0
        except Exception as exc:
            print(f"cycle=FAIL {exc}", file=sys.stderr)
            state["last_cycle_finished"] = int(time.time())
            save_state(state)
            if args.once:
                return 1
            print(f"retry_cycle=after({CYCLE_COOLDOWN_SECONDS}s)")
            time.sleep(CYCLE_COOLDOWN_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
