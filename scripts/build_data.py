#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Atualização diária do data.json — Engage Fundos Listados.

Fonte de cotações: planilha Google (função GOOGLEFINANCE) compartilhada por link.
Motivo: o Yahoo Finance bloqueia o IP do GitHub Actions (cobertura ~0). O
GOOGLEFINANCE roda no IP do Google e cobre TODAS as classes da B3
(FII / FI-Infra / FIP / Fiagro). Este script baixa o CSV da planilha em UMA
requisição e mapeia ticker -> preço — rápido e sem throttling.

Robustez:
- Mantém o último valor bom de cada fundo se a planilha falhar (não zera nada).
- Preserva todos os demais campos do data.json (analise_qual, PL, DY, etc.).

Planilha (aba gid=0): coluna A = ticker (via IMPORTDATA), coluna B = preço
(=GOOGLEFINANCE("BVMF:"&ticker)). Precisa estar como "qualquer pessoa com o
link pode ver". A URL pode ser sobrescrita pela env COTACOES_CSV_URL.

Roda no GitHub Actions (ver .github/workflows/daily.yml). Requisito: pip install requests
"""
import os, json, sys, csv, io
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Falta 'requests'. Rode: pip install requests")

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data.json"
SHEET_ID = "1vD3iz4Ap_X2g5s5KXOuIEt3HCG7m-0Aw7dN5NDGRAlY"
CSV_URL = os.getenv("COTACOES_CSV_URL",
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=0")
BCB = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{}/dados/ultimos/1?formato=json"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
HOJE = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")


def _num(s):
    """Texto de preço (pt-BR ou en) -> float positivo, ou None."""
    if s is None:
        return None
    s = str(s).strip().replace("R$", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:        # '.' = milhar, ',' = decimal (pt-BR)
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:                    # só ',' = decimal
        s = s.replace(",", ".")
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def precos_da_planilha(url):
    """Baixa o CSV publicado e retorna {ticker: preço}."""
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    txt = r.text
    if "<html" in txt[:300].lower() or txt.lstrip().lower().startswith("<!doctype"):
        raise RuntimeError("planilha não acessível — compartilhe como 'qualquer pessoa com o link pode ver'")
    out = {}
    for row in csv.reader(io.StringIO(txt)):
        if not row:
            continue
        tk = (row[0] or "").strip().upper()
        if not tk or tk == "TICKER":
            continue
        preco = _num(row[1]) if len(row) > 1 else None
        if preco:
            out[tk] = preco
    return out


def bcb_valor(serie):
    try:
        r = requests.get(BCB.format(serie), headers=HEADERS, timeout=30)
        r.raise_for_status()
        return float(r.json()[-1]["valor"])
    except Exception:
        return None


def main():
    doc = json.loads(DATA.read_text(encoding="utf-8"))
    fundos = doc["fundos"]

    # nº de cotas fixo, derivado uma vez do snapshot (para recompor PL/P-VP)
    for f in fundos:
        if f.get("cotas") is None and f.get("valor_mercado") and f.get("cotacao"):
            try:
                f["cotas"] = f["valor_mercado"] / f["cotacao"]
            except Exception:
                pass

    try:
        precos = precos_da_planilha(CSV_URL)
        print(f"planilha: {len(precos)} cotações lidas")
    except Exception as e:
        precos = {}
        print(f"AVISO: falha ao ler a planilha: {e} — mantendo cotações anteriores.")

    ok = 0
    for f in fundos:
        p = precos.get((f.get("ticker") or "").upper())
        if p:
            f["cotacao"] = round(p, 2)
            f["cotacao_em"] = HOJE
            if f.get("cotas") and f.get("valor_patrimonial"):
                f["valor_mercado"] = round(f["cotas"] * p, 2)
                f["pvpa"] = f["valor_mercado"] / f["valor_patrimonial"]
            ok += 1

    meta = doc.setdefault("meta", {})
    meta["cotacoes_em"] = HOJE
    meta["cotacoes_fonte"] = "Google Sheets (GOOGLEFINANCE)"
    cdi = bcb_valor(4389)  # CDI anualizado (base 252)
    if cdi is not None:
        meta["cdi_aa"] = cdi
        meta["cdi_em"] = HOJE
    meta["total"] = len(fundos)

    DATA.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"OK. Cotações atualizadas: {ok}/{len(fundos)} | CDI a.a.: {cdi}")


if __name__ == "__main__":
    main()
