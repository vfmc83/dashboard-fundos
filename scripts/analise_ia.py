#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Análise dos relatórios via IA multimodal (visão) — Engage Fundos Listados.

Para cada fundo, pega o Relatório Gerencial mais recente (de documentos/indice.json),
baixa o PDF no fnet, renderiza as primeiras páginas como imagem e envia para a API da
Anthropic (Claude com visão), gerando uma análise factual + risco. Salva em data.json
no campo "analise_qual" (exibido na ficha em "Dos relatórios"). É incremental: só
reprocessa quando há um relatório mais novo que o já analisado.

Roda no GitHub Actions (.github/workflows/analise-ia.yml). NÃO roda no sandbox.
Requer o secret ANTHROPIC_API_KEY. Instalar: pip install requests pymupdf anthropic

Uso:
    python scripts/analise_ia.py                 # todos (incremental)
    python scripts/analise_ia.py --apenas KNCR11,HGLG11
    python scripts/analise_ia.py --limite 50     # no máx. 50 fundos nesta execução
    MODELO=claude-sonnet-4-6 python scripts/analise_ia.py   # mais qualidade (custo maior)
"""
import os, sys, json, time, base64, argparse
from pathlib import Path

try:
    import requests, fitz  # pymupdf
    import anthropic
except ImportError as e:
    sys.exit(f"Dependência faltando ({e}). Rode: pip install requests pymupdf anthropic")

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data.json"
IDX = BASE / "documentos" / "indice.json"
DOWN = "https://fnet.bmfbovespa.com.br/fnet/publico/exibirDocumento"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
MODELO = os.environ.get("MODELO", "claude-haiku-4-5")
PAGINAS = int(os.environ.get("PAGINAS", "3"))

PROMPT = (
    "As imagens são páginas do Relatório Gerencial mais recente de um fundo listado na B3. "
    "Responda APENAS com um JSON válido: {\"analise\": \"...\", \"atencao\": \"...\"}. "
    "Em \"analise\" (4 a 6 frases, português, factual): composição/alocação da carteira, "
    "indexadores e prazo médio, distribuição/rendimento do período e dados-chave. "
    "Em \"atencao\": liste de forma ESPECÍFICA e PRÁTICA os problemas e pontos de atenção REAIS "
    "observados NESTE relatório que possam prejudicar o fundo — por exemplo: devedores ou CRIs "
    "inadimplentes/em atraso/reestruturação, NPL, aumento de PDD/provisões, vacância elevada ou "
    "em alta, saída de inquilinos, queda de receita ou de distribuição, alavancagem alta, "
    "deságio/ágio relevante, ativos problemáticos — citando nomes, números e percentuais quando "
    "aparecerem. NÃO escreva riscos genéricos ou teóricos. Se o relatório não apontar problemas "
    "relevantes, escreva exatamente 'Sem pontos de atenção relevantes no período.'. "
    "Não recomende compra/venda e não invente dados fora das imagens."
)


def dkey(s):
    import re
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{2}):(\d{2}))?", s or "")
    return (m.group(3)+m.group(2)+m.group(1)+(m.group(4) or "00")+(m.group(5) or "00")) if m else "0"


def rel_recente(ticker, idx):
    docs = [d for d in idx if d.get("ticker") == ticker and
            "gerencial" in ((d.get("tipo") or "") + (d.get("categoria") or "") + (d.get("especie") or "")).lower()]
    if not docs:
        return None
    docs.sort(key=lambda d: dkey(d.get("dataEntrega")), reverse=True)
    return docs[0]


def baixar_pdf(doc_id):
    for k in range(3):
        try:
            r = requests.get(DOWN, params={"id": doc_id, "cvm": "true"}, headers=HEADERS, timeout=60)
            r.raise_for_status()
            if "pdf" in (r.headers.get("Content-Type") or "").lower():
                return r.content
            return None
        except Exception:
            time.sleep(2 * (k + 1))
    return None


def paginas_png(pdf_bytes, n):
    out = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for i in range(min(n, doc.page_count)):
        pix = doc[i].get_pixmap(dpi=110)
        out.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()
    return out


def analisa(client, imgs):
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": im}} for im in imgs]
    content.append({"type": "text", "text": PROMPT})
    r = client.messages.create(model=MODELO, max_tokens=600, messages=[{"role": "user", "content": content}])
    return "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apenas", help="Tickers csv")
    ap.add_argument("--limite", type=int, help="Máx. de fundos a processar nesta execução")
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    if not IDX.exists():
        sys.exit("documentos/indice.json não encontrado — rode antes o workflow de índice (indice-docs.yml).")
    doc = json.loads(DATA.read_text(encoding="utf-8"))
    idx = json.loads(IDX.read_text(encoding="utf-8"))
    fundos = doc["fundos"]
    alvo = {t.strip().upper() for t in args.apenas.split(",")} if args.apenas else None

    client = anthropic.Anthropic()  # usa ANTHROPIC_API_KEY do ambiente
    feitos = pulados = falhas = 0
    for f in fundos:
        if alvo and f["ticker"] not in alvo:
            continue
        if args.limite and feitos >= args.limite:
            break
        rel = rel_recente(f["ticker"], idx)
        if not rel:
            continue
        if f.get("analise_qual_id") == str(rel["id"]):  # já analisado este relatório
            pulados += 1
            continue
        pdf = baixar_pdf(rel["id"])
        if not pdf:
            falhas += 1
            continue
        try:
            imgs = paginas_png(pdf, PAGINAS)
            if not imgs:
                falhas += 1
                continue
            res = analisa(client, imgs)
            data_ref = (rel.get("dataReferencia") or rel.get("dataEntrega") or "")[:10]
            import re as _re
            try:
                o = json.loads(_re.search(r"\{.*\}", res, _re.S).group(0))
                ana = (o.get("analise") or "").strip(); ate = (o.get("atencao") or "").strip()
            except Exception:
                ana = res.strip(); ate = ""
            f["analise_qual"] = ana + f" Fonte: Relatório Gerencial ({f.get('gestor') or 'gestor'}), via Fundos.NET, ref. {data_ref}."
            f["pontos_atencao"] = ate
            f["analise_qual_id"] = str(rel["id"])
            feitos += 1
            print(f"[{feitos}] {f['ticker']} ok")
            DATA.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")  # salva incremental
            time.sleep(args.sleep)
        except Exception as e:
            falhas += 1
            print(f"  ! {f['ticker']}: {e}")
            time.sleep(args.sleep)

    print(f"\nOK. Analisados: {feitos} | já atualizados: {pulados} | falhas: {falhas} | modelo: {MODELO}")


if __name__ == "__main__":
    main()
