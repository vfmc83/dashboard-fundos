#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Refresh de fundamentos do data.json a partir do Stock Guide do BTG (BTGSGF).

Motivo: o build diario (build_data.py) so atualiza COTACAO (Google Sheets) e
recompoe pvpa/valor_mercado. Campos "lentos" — Valor Patrimonial, DY, retornos,
P/VPA 2025, Part. IFIX, volume, ultima distribuicao — ficam parados no snapshot
original e defasam. Este script os re-sincroniza com a foto mais recente do BTG.

Fonte: scripts/fundamentos_btg.json (extraido da aba "Stock Guide" do BTGSGF,
gerado offline; regenerar quando chegar um BTGSGF novo).

Regras de seguranca (sync-safe, no espirito do OVERRIDES do analise_ia.py):
- NAO sobrescreve `cotacao`/`cotacao_em`: sao do build diario (mais frescos).
- Recompoe `cotas` a partir do BTG (valor_mercado_btg / fechamento_btg) e usa a
  cotacao ATUAL do data.json para recalcular valor_mercado e pvpa — junta VP
  fresco (BTG) com preco fresco (daily).
- Preserva 100%% dos enriquecimentos: analise_qual, pontos_atencao,
  fatos_relevantes, descricao, analise, risco, taxa_*, benchmark, etc.
- So escreve um campo quando o BTG traz um numero valido (None nao apaga dado).

Roda no GitHub Actions contra o data.json atual do repo (ver workflow
"Atualizar fundamentos"). Sem dependencias externas.
"""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data.json"
FUND = Path(__file__).resolve().parent / "fundamentos_btg.json"
METR = Path(__file__).resolve().parent / "metricas_infra_agro.json"  # TIR/spread/duration (FI-Infra) e estrategia/duration (FIAgro)

# Campos numericos copiados 1:1 quando o BTG traz numero valido.
CAMPOS_NUM = [
    "valor_patrimonial", "pvpa_2025", "dy_ltm", "dy_anualizado",
    "ult_dist_pct", "div_mes", "ret_mes", "ret_ano", "ret_ltm",
    "maximo_52s", "pct_do_maximo", "part_ifix", "vol_medio_3m",
]
# Campos de texto atualizados quando vierem preenchidos (ex.: troca de gestora).
CAMPOS_TXT = ["gestor", "administrador", "segmento_btg"]

# Reclassificacoes de cadastro (aplicadas DEPOIS do merge, entao sobrescrevem o BTGSGF).
# Ex.: FIIs de agro convertidos em Fiagro (aprovacao dos cotistas + CVM). Fonte: fatos relevantes/noticias.
CLASSE_FIX = {
    "BTRA11": {"classe": "Fiagro", "subclasse": "Imobiliário", "segmento_btg": "FIAgro - FII",
               "descricao": "Fiagro de terras agricolas (ex-FII BTG Pactual Terras Agricolas, convertido em Fiagro apos aprovacao dos cotistas em nov/2025). Investe em terras agricolas produtivas, inclusive em fase de transformacao, via sale & leaseback, gerando renda por arrendamento; distribui ao menos 95% do resultado de caixa. Gerido pela BTG Pactual."},
    "BTAL11": {"classe": "Fiagro", "subclasse": "Imobiliário", "segmento_btg": "FIAgro - FII",
               "descricao": "Fiagro de logistica do agronegocio (ex-FII BTG Pactual Agro Logistica, em conversao para Fiagro, mesmo movimento do BTRA11). Detem ativos logisticos ligados ao agro para geracao de renda por locacao. Gerido pela BTG Pactual."},
}


def _n(v):
    return v if isinstance(v, (int, float)) else None


def main():
    doc = json.loads(DATA.read_text(encoding="utf-8"))
    fundos = doc["fundos"]
    fund = json.loads(FUND.read_text(encoding="utf-8"))

    tocados = 0
    campos = 0
    for f in fundos:
        b = fund.get((f.get("ticker") or "").upper())
        if not b:
            continue
        tocados += 1

        for k in CAMPOS_NUM:
            v = _n(b.get(k))
            if v is not None:
                f[k] = v
                campos += 1
        for k in CAMPOS_TXT:
            v = b.get(k)
            if isinstance(v, str) and v.strip() and v.strip() != "-":
                f[k] = v.strip()

        # cotas a partir da foto BTG (mesma data p/ VM e fechamento -> consistente)
        vm_btg = _n(b.get("valor_mercado_btg"))
        fech_btg = _n(b.get("fechamento_btg"))
        if vm_btg and fech_btg:
            f["cotas"] = round(vm_btg / fech_btg)

        # recalcula valor_mercado/pvpa com o preco ATUAL (daily) + VP fresco (BTG)
        cot = _n(f.get("cotacao"))
        cotas = _n(f.get("cotas"))
        vp = _n(f.get("valor_patrimonial"))
        if cot and cotas:
            f["valor_mercado"] = round(cotas * cot, 2)
        if _n(f.get("valor_mercado")) and vp:
            f["pvpa"] = f["valor_mercado"] / vp

    # Metricas de carrego/TIR/duration para FI-Infra e FIAgro (relatorios mensais BTG)
    nmet = 0
    if METR.exists():
        metr = json.loads(METR.read_text(encoding="utf-8"))
        for f in fundos:
            m = metr.get((f.get("ticker") or "").upper())
            if m:
                f["metricas"] = m
                nmet += 1

    # Reclassificacoes de cadastro (sobrescrevem classe/subclasse/segmento/descricao)
    nclass = 0
    for f in fundos:
        cf = CLASSE_FIX.get((f.get("ticker") or "").upper())
        if cf:
            f.update(cf)
            nclass += 1
    print(f"reclassificacoes aplicadas: {nclass}")

    meta = doc.setdefault("meta", {})
    meta["fundamentos_fonte"] = "BTG Stock Guide (BTGSGF)"
    meta["fundamentos_em"] = "2026-07-20"
    print(f"metricas infra/agro aplicadas: {nmet}")

    DATA.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"OK. Fundos atualizados: {tocados}/{len(fundos)} | campos escritos: {campos}")


if __name__ == "__main__":
    main()
