#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Atualização diária do data.json — Engage Fundos Listados.

- Cotação de cada fundo via Yahoo Finance (TICKER.SA).
- Recalcula valor de mercado e P/VP a partir do nº de cotas (derivado do
  snapshot BTG na 1ª execução e mantido fixo até novo snapshot).
- Atualiza CDI (BCB) no bloco meta.
- Mantém os fundamentais do snapshot BTG (patrimônio, DY, retornos LTM).

Roda no GitHub Actions (ver .github/workflows/daily.yml). Local: python scripts/build_data.py
Requisito: pip install requests
"""
import json, sys, time, argparse
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


def yahoo_preco(ticker, session):
    for k in range(3):
        try:
            r = session.get(YAHOO.format(ticker), params={"range": "5d", "interval": "1d"},
                            headers=HEADERS, timeout=30)
            if r.status_code == 429:
                time.sleep(5 * (k + 1)); continue
            r.raise_for_status()
            meta = r.json()["chart"]["result"][0]["meta"]
            return meta.get("regularMarketPrice"), (meta.get("chartPreviousClose") or meta.get("previousClose"))
        except Exception:
            time.sleep(3 * (k + 1))
    return None, None


def bcb_valor(serie):
    try:
        r = requests.get(BCB.format(serie), headers=HEADERS, timeout=30)
        r.raise_for_status()
        return float(r.json()[-1]["valor"])
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apenas", help="Tickers csv (debug)")
    ap.add_argument("--sleep", type=float, default=0.3)
    args = ap.parse_args()

    doc = json.loads(DATA.read_text(encoding="utf-8"))
    fundos = doc["fundos"]
    alvo = {t.strip().upper() for t in args.apenas.split(",")} if args.apenas else None

    session = requests.Session()
    ok = falha = 0
    for i, f in enumerate(fundos, 1):
        t = f.get("ticker")
        if alvo and t not in alvo:
            continue
        # nº de cotas fixo, derivado uma vez do snapshot
        if f.get("cotas") is None and f.get("valor_mercado") and f.get("cotacao"):
            try:
                f["cotas"] = f["valor_mercado"] / f["cotacao"]
            except Exception:
                pass
        preco, prev = yahoo_preco(t, session)
        if preco:
            f["cotacao"] = round(preco, 2)
            f["cotacao_em"] = HOJE
            if prev:
                f["var_dia"] = preco / prev - 1
            if f.get("cotas") and f.get("valor_patrimonial"):
                f["valor_mercado"] = round(f["cotas"] * preco, 2)
                f["pvpa"] = f["valor_mercado"] / f["valor_patrimonial"]
            ok += 1
        else:
            falha += 1
        if i % 25 == 0:
            print(f"  ...{i}/{len(fundos)} (ok={ok} falha={falha})")
        time.sleep(args.sleep)

    meta = doc.setdefault("meta", {})
    meta["cotacoes_em"] = HOJE
    cdi_aa = bcb_valor(4389)  # CDI anualizado (base 252)
    if cdi_aa is not None:
        meta["cdi_aa"] = cdi_aa
        meta["cdi_em"] = HOJE
    meta["total"] = len(fundos)

    DATA.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nOK. Cotações atualizadas: {ok} | falhas: {falha} | CDI a.a.: {cdi_aa}")
    print(f"data.json salvo em {DATA}")


if __name__ == "__main__":
    main()
