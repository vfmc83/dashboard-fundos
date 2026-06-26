#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Análise dos relatórios SEM custo — Engage Fundos Listados.

Para cada fundo, busca na API do Fundos.NET (fnet) o relatório mais recente
(prioridade: Relatório Gerencial > Informe Trimestral > Informe Mensal), baixa o
PDF, extrai o TEXTO com pymupdf (e OCR Tesseract só se a página não tiver texto)
e captura campos reais por regex — distribuição, DY, carrego (CDI/IPCA+), alocação,
vacância/ocupação, inadimplência, alavancagem/LTV, vendas (SSS). Monta analise_qual
e pontos_atencao. NÃO usa API paga. Roda no GitHub Actions (analise-ia.yml).
É incremental: só reprocessa quando o id do relatório mudou (analise_qual_id).

Instalar: pip install requests pymupdf pytesseract pillow
OCR (fallback): apt-get install -y tesseract-ocr tesseract-ocr-por

Uso:
    python scripts/analise_ia.py                 # todos (incremental)
    python scripts/analise_ia.py --apenas KNCR11,HGLG11
    python scripts/analise_ia.py --limite 50
"""
import os, sys, json, re, time, base64, argparse, io
from pathlib import Path

try:
    import requests, fitz  # pymupdf
except ImportError as e:
    sys.exit(f"Falta dependência ({e}). Rode: pip install requests pymupdf pytesseract pillow")

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data.json"
LIST = "https://fnet.bmfbovespa.com.br/fnet/publico/pesquisarGerenciadorDocumentosDados"
DOWN = "https://fnet.bmfbovespa.com.br/fnet/publico/exibirDocumento"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# Prioridade de tipos de documento que servem como "relatório do mês/período".
PRIOR = ["relatório gerencial", "relatorio gerencial", "informe trimestral",
         "relatório do gestor", "relatorio do gestor", "carta do gestor", "informe mensal"]

# Campos descritivos (para analise_qual).
F_DIST = re.compile(r"(distribu[íi]\w*|rendimento)[^R\n]{0,25}?R\$ ?\d+[,.]\d{2,}", re.I)
F_DY = re.compile(r"(dividend yield|\bDY\b|yield)[^%\n]{0,22}?\d{1,2}[,.]?\d?\s?%", re.I)
F_CARR = re.compile(r"(CDI|IPCA)\s*\+\s*\d{1,2}[,.]?\d?\s?%", re.I)
F_ALOC = re.compile(r"(aloca[çc]\w*|ativos[- ]alvo)[^%\n]{0,26}?\d{1,3}[,.]?\d?\s?%", re.I)
# Campos de risco (para pontos_atencao).
R_VAC = re.compile(r"(vac[âa]nc\w*|ocupa[çc][ãa]o f[íi]sica|taxa de ocupa[çc][ãa]o)[^%\n]{0,26}?\d{1,3}[,.]?\d?\s?%", re.I)
R_INAD = re.compile(r"inadimpl\w*[^%\n]{0,26}?\d{1,2}[,.]?\d?\s?%", re.I)
R_LTV = re.compile(r"(LTV|alavancagem)[^%\n]{0,36}?\d{1,2}[,.]?\d?\s?%", re.I)
R_SSS = re.compile(r"(SSS|same store|vendas mesmas lojas)[^%\n]{0,24}?-?\d{1,2}[,.]?\d?\s?%", re.I)


def _txt(m):
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else None


def dkey(s):
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{2}):(\d{2}))?", s or "")
    return (m.group(3)+m.group(2)+m.group(1)+(m.group(4) or "00")+(m.group(5) or "00")) if m else "0"


def docs_fundo(cnpj):
    todos, s = [], 0
    while s < 2400:
        try:
            r = requests.get(LIST, params={"d": 1, "s": s, "l": 200, "cnpjFundo": cnpj}, headers=HEADERS, timeout=60)
            j = r.json()
            arr = j.get("data") or j.get("dados") or []
        except Exception:
            break
        if not arr:
            break
        todos += arr
        if len(arr) < 200:
            break
        s += 200
        time.sleep(0.4)
    return todos


def melhor_relatorio(docs):
    cand = []
    for d in docs:
        if d.get("situacaoDocumento") != "A":
            continue
        tipo = ((d.get("tipoDocumento") or "") + " " + (d.get("categoriaDocumento") or "") + " " + (d.get("especieDocumento") or "")).lower()
        pr = next((i for i, p in enumerate(PRIOR) if p in tipo), None)
        if pr is None:
            continue
        cand.append((pr, dkey(d.get("dataEntrega") or d.get("dataReferencia")), d))
    if not cand:
        return None
    # menor índice de prioridade vence; em empate, data de entrega mais recente
    cand.sort(key=lambda x: (x[0], "" ), )
    melhor_pr = cand[0][0]
    mesmo = [c for c in cand if c[0] == melhor_pr]
    mesmo.sort(key=lambda x: x[1], reverse=True)
    return mesmo[0][2]


def baixar_pdf(doc_id):
    for k in range(3):
        try:
            r = requests.get(DOWN, params={"id": doc_id, "cvm": "true"}, headers=HEADERS, timeout=90)
            data = r.content
            if not data:
                return None
            if data[:1] == b'"':  # base64 dentro de string JSON
                return base64.b64decode(json.loads(data.decode("utf-8", "ignore")))
            if data[:4] == b"%PDF":
                return data
            try:
                dec = base64.b64decode(data)
                if dec[:4] == b"%PDF":
                    return dec
            except Exception:
                pass
            return None
        except Exception:
            time.sleep(2 * (k + 1))
    return None


def texto_pdf(pdf, maxpg=12):
    out = []
    doc = fitz.open(stream=pdf, filetype="pdf")
    for i in range(min(maxpg, doc.page_count)):
        t = doc[i].get_text("text")
        if len(t.strip()) < 40:  # página sem camada de texto -> OCR
            try:
                import pytesseract
                from PIL import Image
                pix = doc[i].get_pixmap(dpi=200)
                t = pytesseract.image_to_string(Image.open(io.BytesIO(pix.tobytes("png"))), lang="por")
            except Exception:
                pass
        out.append(t)
    doc.close()
    return re.sub(r"[ \t]+", " ", "\n".join(out))


def analisa(f, t, ref):
    desc0 = (f.get("descricao") or "").split(".")[0].strip()
    campos = [c for c in (_txt(F_DIST.search(t)), _txt(F_DY.search(t)), _txt(F_CARR.search(t)), _txt(F_ALOC.search(t))) if c]
    riscos = [c for c in (_txt(R_VAC.search(t)), _txt(R_INAD.search(t)), _txt(R_LTV.search(t)), _txt(R_SSS.search(t))) if c]
    aq = f"Relatório de {ref}"
    if desc0:
        aq += f" — {desc0}"
    if campos:
        aq += ". Indicadores extraídos: " + "; ".join(campos)
    aq += f". Fonte: relatório no Fundos.NET, ref. {ref}. (Resumo automático.)"
    estrut = (f.get("risco") or "").replace("Riscos típicos: ", "").strip()
    if riscos:
        pa = "Indicadores de atenção no relatório: " + "; ".join(riscos) + "."
        if estrut:
            pa += " Riscos estruturais: " + estrut
    else:
        pa = estrut or "Sem indicadores numéricos de atenção destacados no relatório."
    return aq, pa


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apenas")
    ap.add_argument("--limite", type=int)
    ap.add_argument("--sleep", type=float, default=0.6)
    a = ap.parse_args()

    doc = json.loads(DATA.read_text(encoding="utf-8"))
    alvo = {t.strip().upper() for t in a.apenas.split(",")} if a.apenas else None
    feitos = pulados = falhas = 0
    for f in doc["fundos"]:
        if alvo and f["ticker"] not in alvo:
            continue
        if a.limite and feitos >= a.limite:
            break
        cnpj = (f.get("cnpj") or "").replace(".", "").replace("/", "").replace("-", "")
        if not cnpj:
            continue
        docs = docs_fundo(cnpj)
        rel = melhor_relatorio(docs)
        if not rel:
            continue
        rid = str(rel["id"])
        if f.get("analise_qual_id") == rid:
            pulados += 1
            continue
        pdf = baixar_pdf(rel["id"])
        if not pdf:
            falhas += 1
            continue
        try:
            t = texto_pdf(pdf)
            if len(t) < 80:
                falhas += 1
                continue
            ref = (rel.get("dataReferencia") or rel.get("dataEntrega") or "")[:10]
            aq, pa = analisa(f, t, ref)
            f["analise_qual"] = aq
            f["pontos_atencao"] = pa
            f["analise_qual_id"] = rid
            feitos += 1
            print(f"[{feitos}] {f['ticker']} ok")
            DATA.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
            time.sleep(a.sleep)
        except Exception as e:
            falhas += 1
            print(f"  ! {f['ticker']}: {e}")

    print(f"\nOK. feitos:{feitos} | já atualizados:{pulados} | falhas:{falhas}")


if __name__ == "__main__":
    main()
