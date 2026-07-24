#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Snapshot diario do Score de risco Engage -> historico_scores.json.

Objetivo: permitir que o dashboard mostre MIGRACAO DE FAIXA (quem subiu/desceu
de risco) no Radar. A comparacao no cliente e feita entre dois snapshots deste
mesmo script (Python vs Python), entao e auto-consistente e nao depende do score
recomputado no navegador.

IMPORTANTE: a logica abaixo deve ESPELHAR `computeScores` + `_sinalRel` do
index.html. Se mudar o score la, atualizar aqui (e vice-versa). Teste de paridade:
apos deploy, o 1o diff de faixa deve ser ~vazio.

Roda no daily.yml apos build_data.py. Sem dependencias externas.
"""
import re
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data.json"
HIST = BASE / "historico_scores.json"
HOJE = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")
MAX_DIAS = 120

PERFIL_RISCO = {"Desenvolvimento": 88, "Crédito (FIDC)": 70, "Papel (CRI)": 60,
                "Infraestrutura (FIP-IE)": 60, "Imobiliário": 56, "Gestão ativa": 54,
                "Outros": 54, "Híbrido": 50, "Infraestrutura": 42, "Tijolo": 40, "FoF": 30}
PERFIL_SEG = {"Galpão Logístico": 34, "Renda Urbana": 38, "Agronegócio": 40,
              "Agência Bancária": 42, "Shopping Center": 44, "Hospital": 44,
              "Residencial": 46, "Educacional": 46, "Laje Corporativa": 48, "Hotel": 50}
REAL_ASSET_FIAGRO = {"BTRA11", "BTAL11", "AAZQ11", "SNFZ11"}


def _num(m):
    if not m:
        return None
    g = m.groups()
    return float(g[0] + ("." + g[1] if len(g) > 1 and g[1] else ""))


def sinal_rel(txt):
    if not txt:
        return None
    t0 = txt.lower()
    # re.A (ASCII) para espelhar EXATAMENTE o \w/\b/\s do JavaScript do index.html
    # (o \w do Python e Unicode e casa acentos, divergindo em textos com OCR grudado).
    neg = re.compile(r"(n[ãa]o\b|sem\b)([^.\n]{0,45}?)(inadimpl\w*|eventos?\s+negativos?|evento\s+de\s+cr[ée]dito|default|calote)", re.I | re.A)
    t = neg.sub(r"\1\2 semrisco ", t0)
    se = lambda rx: re.search(rx, t, re.A)
    s = 30
    add = lambda rx, p: p if se(rx) else 0

    if se(r"inadimpl|\bnpl\b|\bcalote\b"):
        iv = _num(se(r"inadimpl\w*\s*(?:d[aeo]\s+carteira\s*)?(?:de|em|:|é de)?\s*(\d{1,2})(?:[,.](\d))?\s?%"))
        if iv is None:
            iv = _num(se(r"(\d{1,2})(?:[,.](\d))?\s?%\s*(?:d[aeo]\s+carteira\s+)?(?:em\s+|de\s+)?inadimpl"))
        if iv is not None:
            s += 30 if iv > 10 else 22 if iv > 6 else 12 if iv > 3 else 0
        elif se(r"inadimpl\w*\s*(elevad|alta|relevant|crescent|significativ|forte|agravad)"):
            s += 18
        else:
            s += 4
    s += add(r"recupera[çc][ãa]o judicial|\brj\b|reperfilament|reestrutura[çc][ãa]o de d[íi]vida|renegocia[çc][ãa]o de d[íi]vida", 26)
    s += add(r"\bdefault\b|vencimento antecipado|inadimplement|execu[çc][ãa]o (de |da )?garantia", 26)
    s += add(r"pdd[^.\n]{0,25}(elevad|em alta|crescent|aument|subiu|dispar)", 12)
    s += add(r"estressad|menor rating|rating baixo|rebaixament|downgrade|\bccc\b|rating\s+d\b", 14)
    s += add(r"alavancagem elevada|acima de 100% do pl", 12)
    vv = _num(se(r"vac[âa]nc\w*[^%\n]{0,25}?(\d{1,2})(?:[,.](\d))?\s?%"))
    if vv is not None and vv > 7:
        s += 14 if vv > 12 else 8
    lv = _num(se(r"\bltv\b[^%\n]{0,30}?(\d{1,3})(?:[,.](\d))?\s?%"))
    if lv is not None:
        s += 20 if lv > 80 else 14 if lv > 70 else 8 if lv > 60 else 4 if lv > 50 else 0
    km = se(r"high[\s-]?yield|abaixo d[eo] grau de investimento|abaixo de investment grade")
    if km:
        i = km.start()
        w = t[max(0, i - 45):i + 45]
        anchor = i - max(0, i - 45)
        best, bd = None, 999
        for pm in re.finditer(r"(\d{1,3})(?:[,.](\d))?\s?%", w, re.A):
            d = abs(pm.start() - anchor)
            if d < bd:
                bd, best = d, pm
        if best:
            hv = float(best.group(1) + ("." + best.group(2) if best.group(2) else ""))
            s += 16 if hv > 40 else 10 if hv > 25 else 5 if hv > 10 else 0
    s += add(r"fraude|irregularidade grave|desvio de recursos|apropria[çc][ãa]o indevida", 30)
    s += add(r"ren[úu]nci\w*[^.\n]{0,14}(administra|gest)|(substitui|destitui)[çc][ãa]o d[oa] (administrador|gestor|administradora|gestora)", 26)
    s += add(r"interven[çc][ãa]o d[oa] fundo|liquida[çc][ãa]o (extrajudicial|do fundo)", 26)
    s += add(r"(investiga[çc][ãa]o|inqu[ée]rito|processo administrativo|apura[çc][ãa]o)[^.\n]{0,25}(cvm|b3|regulador|minist[ée]rio p[úu]blico)|\bcvm\b[^.\n]{0,25}(investiga|questiona|apura|processo administrativo|of[íi]cio)", 24)
    s += add(r"demonstra[çc][õo]es (financeiras )?(reprovad|rejeitad|n[ãa]o aprovad)|contas (reprovad|rejeitad)|parecer[^.\n]{0,25}(com ressalva|adverso|absten[çc]|negativa de opini)|reaudito", 22)
    s += add(r"volume at[íi]pico de fatos relevantes|sucess[ãa]o de fatos relevantes", 12)
    return max(0, min(100, s))


def score_fundo(f):
    rperf = PERFIL_RISCO.get(f.get("subclasse"), 54)
    if f.get("subclasse") == "Tijolo" and PERFIL_SEG.get(f.get("segmento_btg")) is not None:
        rperf = PERFIL_SEG[f["segmento_btg"]]
    if f.get("classe") == "Fiagro" and f.get("subclasse") == "Imobiliário":
        rperf = 44 if f.get("ticker") in REAL_ASSET_FIAGRO else 60
    rs = sinal_rel(f.get("pontos_atencao"))
    sc = 0.40 * rperf + 0.60 * rs if rs is not None else rperf
    return round(sc)


def main():
    doc = json.loads(DATA.read_text(encoding="utf-8"))
    scores = {f["ticker"]: score_fundo(f) for f in doc["fundos"] if f.get("ticker")}

    hist = []
    if HIST.exists():
        try:
            hist = json.loads(HIST.read_text(encoding="utf-8"))
        except Exception:
            hist = []
    hist = [h for h in hist if h.get("data") != HOJE]   # substitui o de hoje se re-rodar
    hist.append({"data": HOJE, "sc": scores})
    hist = hist[-MAX_DIAS:]
    HIST.write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"snapshot {HOJE}: {len(scores)} fundos | historico com {len(hist)} dias")


if __name__ == "__main__":
    main()
