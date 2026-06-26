#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Atualização diária do data.json — Engage Fundos Listados.

- Cotação de cada fundo via Yahoo Finance (TICKER.SA), endpoint v8/chart.
- Busca CONCORRENTE (ThreadPool) com retries limitados. Antes era 1-a-1 com
  backoff longo (5/10/15s) e timeout 30s — sob throttling do Yahoo podia
  travar ~2h. Agora roda em poucos minutos.
- Recalcula valor de mercado e P/VP a partir do nº de cotas (derivado do
  snapshot BTG na 1ª execução e mantido fixo até novo snapshot).
- Atualiza CDI (BCB) no bloco meta.
- Mantém o último valor bom quando a fonte falha (não zera nada).

Roda no GitHub Actions (ver .github/workflows/daily.yml).
Local: python scripts/build_data.py [--workers N] [--apenas TICKERS]
Requisito: pip install requests
"""
import json, sys, time, argparse, os, random, threading
import concurrent.futures as cf
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Falta 'requests'. Rode: pip install requests")

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}.SA"
BCB = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{}/dados/ultimos/1?formato=json"
HOJE = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")

# Sessão por thread (requests.Session não é garantidamente thread-safe).
_tl = threading.local()
def _sess():
    s = getattr(_tl, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _tl.s = s
    return s


def yahoo_preco(ticker, retries=2, timeout=10):
    """Retorna (preco, prev_close) ou (None, None). Backoff curto e limitado."""
    url = YAHOO.format(ticker)
    for k in range(retries + 1):
        try:
            r = _sess().get(url, params={"range": "5d", "interval": "1d"}, timeout=timeout)
            if r.status_code == 429:
                time.sleep(2 * (k + 1) + random.uniform(0, 0.8)); continue
            r.raise_for_status()
            meta = r.json()["chart"]["result"][0]["meta"]
            return meta.get("regularMarketPrice"), (meta.get("chartPreviousClose") or meta.get("previousClose"))
        except Exception:
            time.sleep(1.5 * (k + 1) + random.uniform(0, 0.5))
    return None, None


def bcb_valor(serie):
    try:
        r = requests.get(BCB.format(serie), headers=HEADERS, timeout=30)
        r.raise_for_status()
        return float(r.json()[-1]["valor"])
    except Exception:
        return None


def _aplica(f, preco, prev):
    """Atualiza o fundo só quando há preço novo (preserva o último valor bom)."""
    if not preco:
        return False
    f["cotacao"] = round(preco, 2)
    f["cotacao_em"] = HOJE
    if prev:
        f["var_dia"] = preco / prev - 1
    if f.get("cotas") and f.get("valor_patrimonial"):
        f["valor_mercado"] = round(f["cotas"] * preco, 2)
        f["pvpa"] = f["valor_mercado"] / f["valor_patrimonial"]
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apenas", help="Tickers csv (debug)")
    ap.add_argument("--workers", type=int, default=int(os.getenv("YAHOO_WORKERS", "8")),
                    help="Requisições concorrentes ao Yahoo (default 8)")
    ap.add_argument("--sleep", type=float, default=0.0, help="(legado, ignorado)")
    args = ap.parse_args()

    doc = json.loads(DATA.read_text(encoding="utf-8"))
    fundos = doc["fundos"]
    alvo = {t.strip().upper() for t in args.apenas.split(",")} if args.apenas else None
    targets = [f for f in fundos if f.get("ticker") and (not alvo or f["ticker"] in alvo)]

    # nº de cotas fixo, derivado uma vez do snapshot (sem rede)
    for f in targets:
        if f.get("cotas") is None and f.get("valor_mercado") and f.get("cotacao"):
            try:
                f["cotas"] = f["valor_mercado"] / f["cotacao"]
            except Exception:
                pass

    t0 = time.time()
    ok = 0
    falhas = []
    with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        fut = {ex.submit(yahoo_preco, f["ticker"]): f for f in targets}
        done = 0
        for fu in cf.as_completed(fut):
            f = fut[fu]
            try:
                preco, prev = fu.result()
            except Exception:
                preco, prev = None, None
            if _aplica(f, preco, prev):
                ok += 1
            else:
                falhas.append(f)
            done += 1
            if done % 50 == 0:
                print(f"  ...{done}/{len(targets)} (ok={ok})")

    # 2ª passada sequencial para stragglers (throttling pontual)
    recuperados = 0
    if falhas:
        print(f"  retry sequencial de {len(falhas)} falhas...")
        for f in falhas:
            preco, prev = yahoo_preco(f["ticker"], retries=2, timeout=12)
            if _aplica(f, preco, prev):
                ok += 1; recuperados += 1
            time.sleep(0.4)

    meta = doc.setdefault("meta", {})
    meta["cotacoes_em"] = HOJE
    cdi_aa = bcb_valor(4389)  # CDI anualizado (base 252)
    if cdi_aa is not None:
        meta["cdi_aa"] = cdi_aa
        meta["cdi_em"] = HOJE
    meta["total"] = len(fundos)

    DATA.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    dt = time.time() - t0
    print(f"\nOK em {dt:.0f}s. Cotações: {ok}/{len(targets)} "
          f"(recuperados no retry: {recuperados}) | falhas finais: {len(targets) - ok} | CDI a.a.: {cdi_aa}")
    print(f"data.json salvo em {DATA}")


if __name__ == "__main__":
    main()
