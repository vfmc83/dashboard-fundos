#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Atualização diária do data.json — Engage Fundos Listados.

Cotação via Yahoo Finance (TICKER.SA, endpoint v8/chart). O Yahoo limita por
taxa o IP do GitHub Actions, então o coletor foi desenhado para NUNCA travar
(antes podia ficar ~2h):

- Busca CONCORRENTE (ThreadPool) com retries curtos e limitados.
- TETO DE TEMPO global (--max-seconds, default 720s): ao estourar, para de
  buscar e grava o que já tem. Fundos sem preço novo mantêm o último valor bom.
  Garante término em poucos minutos mesmo sob throttling pesado.
- 2ª passada de recuperação, também concorrente (concorrência menor), para
  stragglers de throttling pontual — só roda se ainda houver tempo.

Recalcula valor de mercado e P/VP a partir do nº de cotas (derivado do snapshot
BTG na 1ª execução e mantido fixo). Atualiza CDI (BCB) no bloco meta.

Roda no GitHub Actions (ver .github/workflows/daily.yml).
Local: python scripts/build_data.py [--workers N] [--max-seconds S] [--apenas TICKERS]
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

try:
    sys.stdout.reconfigure(line_buffering=True)   # logs ao vivo no Actions
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}.SA"
BCB = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{}/dados/ultimos/1?formato=json"
HOJE = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")
DEADLINE = None   # epoch; definido no main (teto de tempo global)

# Sessão por thread (requests.Session não é garantidamente thread-safe).
_tl = threading.local()
def _sess():
    s = getattr(_tl, "s", None)
    if s is None:
        s = requests.Session(); s.headers.update(HEADERS); _tl.s = s
    return s


def yahoo_preco(ticker, retries=1, timeout=8):
    """(preco, prev_close) ou (None, None). Respeita o teto de tempo global."""
    url = YAHOO.format(ticker)
    for k in range(retries + 1):
        if DEADLINE and time.time() > DEADLINE:
            return None, None
        try:
            r = _sess().get(url, params={"range": "5d", "interval": "1d"}, timeout=timeout)
            if r.status_code == 429:
                time.sleep(1.5 * (k + 1) + random.uniform(0, 0.6)); continue
            r.raise_for_status()
            meta = r.json()["chart"]["result"][0]["meta"]
            return meta.get("regularMarketPrice"), (meta.get("chartPreviousClose") or meta.get("previousClose"))
        except Exception:
            time.sleep(1.0 * (k + 1) + random.uniform(0, 0.4))
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
    f["cotacao"] = round(preco, 2); f["cotacao_em"] = HOJE
    if prev:
        f["var_dia"] = preco / prev - 1
    if f.get("cotas") and f.get("valor_patrimonial"):
        f["valor_mercado"] = round(f["cotas"] * preco, 2)
        f["pvpa"] = f["valor_mercado"] / f["valor_patrimonial"]
    return True


def _busca_lote(targets, workers, retries, timeout):
    """Busca concorrente; retorna a lista de fundos que ficaram sem preço."""
    falhas = []
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        fut = {ex.submit(yahoo_preco, f["ticker"], retries, timeout): f for f in targets}
        done = 0
        for fu in cf.as_completed(fut):
            f = fut[fu]
            try:
                preco, prev = fu.result()
            except Exception:
                preco, prev = None, None
            if not _aplica(f, preco, prev):
                falhas.append(f)
            done += 1
            if done % 50 == 0:
                print(f"  ...{done}/{len(targets)}", flush=True)
    return falhas


def main():
    global DEADLINE
    ap = argparse.ArgumentParser()
    ap.add_argument("--apenas", help="Tickers csv (debug)")
    ap.add_argument("--workers", type=int, default=int(os.getenv("YAHOO_WORKERS", "6")),
                    help="Requisições concorrentes ao Yahoo (default 6)")
    ap.add_argument("--max-seconds", type=int, default=int(os.getenv("YAHOO_MAX_SECONDS", "720")),
                    help="Teto de tempo da busca de cotações (default 720s)")
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
    DEADLINE = t0 + max(30, args.max_seconds)

    # passada 1: rápida e concorrente
    falhas = _busca_lote(targets, args.workers, retries=1, timeout=8)

    # passada 2: recupera stragglers (concorrência menor), se houver tempo
    recuperados = 0
    if falhas and time.time() < DEADLINE:
        antes = len(falhas)
        print(f"  recuperando {antes} falhas (concorrência menor)...", flush=True)
        falhas2 = _busca_lote(falhas, max(2, args.workers // 2), retries=2, timeout=10)
        recuperados = antes - len(falhas2)

    ok = sum(1 for f in targets if f.get("cotacao_em") == HOJE)

    meta = doc.setdefault("meta", {})
    meta["cotacoes_em"] = HOJE
    cdi_aa = bcb_valor(4389)  # CDI anualizado (base 252)
    if cdi_aa is not None:
        meta["cdi_aa"] = cdi_aa; meta["cdi_em"] = HOJE
    meta["total"] = len(fundos)

    DATA.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    dt = time.time() - t0
    estourou = " (teto de tempo atingido)" if time.time() > DEADLINE else ""
    print(f"\nOK em {dt:.0f}s{estourou}. Cotações: {ok}/{len(targets)} "
          f"(recuperados: {recuperados}) | sem preço novo: {len(targets) - ok} | CDI a.a.: {cdi_aa}", flush=True)
    print(f"data.json salvo em {DATA}", flush=True)


if __name__ == "__main__":
    main()
