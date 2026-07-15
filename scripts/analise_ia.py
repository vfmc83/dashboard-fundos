#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Análise dos relatórios SEM custo — Engage Fundos Listados.

Para cada fundo, usa documentos/indice.json (gerado por baixar_fnet.py / workflow
"Indice de documentos") para achar o Relatório Gerencial MAIS RECENTE, baixa o PDF
no fnet, extrai o TEXTO com pymupdf (OCR Tesseract só se a página não tiver texto)
e captura campos reais por regex — distribuição, DY, carrego (CDI/IPCA+), alocação,
vacância/ocupação, inadimplência, alavancagem/LTV, vendas (SSS). Monta analise_qual
e pontos_atencao. NÃO usa API paga. É incremental (analise_qual_id).

Cobre FII e Fiagro (que publicam Relatório Gerencial no fnet). FI-Infra e FIP, que
não publicam esse tipo de documento, são ignorados (ficam com descrição + análise
quantitativa do data.json).

Pré-requisito: documentos/indice.json atualizado (rode antes o workflow indice-docs).
Instalar: pip install requests pymupdf pytesseract pillow
OCR (fallback): apt-get install -y tesseract-ocr tesseract-ocr-por
"""
import os, sys, json, re, time, base64, argparse, io
from pathlib import Path

try:
    import requests, fitz  # pymupdf
except ImportError as e:
    sys.exit(f"Falta dependência ({e}). Rode: pip install requests pymupdf pytesseract pillow")

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data.json"
IDX = BASE / "documentos" / "indice.json"
DOWN = "https://fnet.bmfbovespa.com.br/fnet/publico/exibirDocumento"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

F_DIST = re.compile(r"(distribu[íi]\w*|rendimento)[^R\n]{0,25}?R\$ ?\d+[,.]\d{2,}", re.I)
F_DY = re.compile(r"(dividend yield|\bDY\b|yield)[^%\n]{0,22}?\d{1,2}[,.]?\d?\s?%", re.I)
F_CARR = re.compile(r"(CDI|IPCA)\s*\+\s*\d{1,2}[,.]?\d?\s?%", re.I)
F_ALOC = re.compile(r"(aloca[çc]\w*|ativos[- ]alvo)[^%\n]{0,26}?\d{1,3}[,.]?\d?\s?%", re.I)
R_VAC = re.compile(r"(vac[âa]nc\w*|ocupa[çc][ãa]o f[íi]sica|taxa de ocupa[çc][ãa]o)[^%\n]{0,26}?\d{1,3}[,.]?\d?\s?%", re.I)
R_INAD = re.compile(r"inadimpl\w*[^%\n]{0,26}?\d{1,2}[,.]?\d?\s?%", re.I)
R_LTV = re.compile(r"(LTV|alavancagem)[^%\n]{0,36}?\d{1,2}[,.]?\d?\s?%", re.I)
R_SSS = re.compile(r"(SSS|same store|vendas mesmas lojas)[^%\n]{0,24}?-?\d{1,2}[,.]?\d?\s?%", re.I)
R_PDD = re.compile(r"(PDD|provis[ãa]o para devedores|provis[ãa]o para perdas|devedores duvidosos)[^.\n]{0,45}", re.I)
R_RJ = re.compile(r"(recupera[çc][ãa]o judicial|reperfilament\w+|reestrutura[çc][ãa]o de d[íi]vida|renegocia[çc][ãa]o de d[íi]vida|repactua[çc][ãa]o de[^.\n]{0,20}d[íi]vida)[^.\n]{0,40}", re.I)
R_RATING = re.compile(r"(rebaixament\w+|downgrade|high[- ]?yield|abaixo de investment grade|carteira[^.\n]{0,20}menor rating)[^.\n]{0,30}", re.I)
R_DEF = re.compile(r"(default|vencimento antecipado|inadimplement\w+|evento de inadimpl\w+|calote|atraso[^.\n]{0,20}pagament\w+)[^.\n]{0,30}", re.I)


def _t(m):
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else None


def dkey(s):
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{2}):(\d{2}))?", s or "")
    return (m.group(3)+m.group(2)+m.group(1)+(m.group(4) or "00")+(m.group(5) or "00")) if m else "0"


def rel_recente(ticker, idx):
    rg = [d for d in idx if d.get("ticker") == ticker and
          "gerencial" in ((d.get("tipo") or "") + (d.get("categoria") or "") +
                          (d.get("especie") or "") + (d.get("tipoDocumento") or "")).lower()]
    if not rg:
        return None
    rg.sort(key=lambda d: dkey(d.get("dataEntrega") or d.get("dataReferencia")), reverse=True)
    return rg[0]


FR_RISK = re.compile(r"(inadimpl\w*|recupera[çc][ãa]o judicial|vencimento antecipado|execu[çc][ãa]o (de |da )?garantia|\bdefault\b|reperfilament\w+|renegocia[çc][ãa]o de d[íi]vida|\bwaiver\b|car[êe]ncia de juros|substitui[çc][ãa]o d[oa] (gestor|administrador)|desenquadramento|reestrutura[çc][ãa]o)", re.I)


def frs_recentes(ticker, idx, n=2):
    fr = [d for d in idx if d.get("ticker") == ticker and
          "fato relevante" in ((d.get("categoria") or "") + (d.get("tipo") or "") + (d.get("especie") or "")).lower()]
    fr.sort(key=lambda d: dkey(d.get("dataEntrega") or d.get("dataReferencia")), reverse=True)
    return fr[:n]


def analisa_frs(frs):
    """Le os FRs recentes e devolve itens estruturados {data, risco, resumo}."""
    itens = []
    for fr in frs:
        pdf = baixar_pdf(fr["id"])
        if not pdf:
            continue
        try:
            t = texto_pdf(pdf, maxpg=6)
        except Exception:
            continue
        if len(t.strip()) < 30:
            continue
        ref = (fr.get("dataEntrega") or fr.get("dataReferencia") or "")[:10]
        m = FR_RISK.search(t)
        if m:
            snip = re.sub(r"\s+", " ", t[max(0, m.start() - 30):m.start() + 140]).strip()
            itens.append({"data": ref, "risco": True, "resumo": "..." + snip + "..."})
        else:
            ini = re.sub(r"\s+", " ", t[:190]).strip()
            itens.append({"data": ref, "risco": False, "resumo": ini})
        time.sleep(0.4)
    return itens


def baixar_pdf(doc_id):
    for k in range(3):
        try:
            r = requests.get(DOWN, params={"id": doc_id, "cvm": "true"}, headers=HEADERS, timeout=90)
            data = r.content
            if not data:
                return None
            if data[:1] == b'"':
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
        if len(t.strip()) < 40:
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
    campos = [c for c in (_t(F_DIST.search(t)), _t(F_DY.search(t)), _t(F_CARR.search(t)), _t(F_ALOC.search(t))) if c]
    riscos = [c for c in (_t(R_VAC.search(t)), _t(R_INAD.search(t)), _t(R_LTV.search(t)), _t(R_SSS.search(t)),
                          _t(R_PDD.search(t)), _t(R_RJ.search(t)), _t(R_RATING.search(t)), _t(R_DEF.search(t))) if c]
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


# Overrides manuais a partir de noticias/fatos relevantes (pesquisa web). Protegidos com "manual-".
OVERRIDES = {
    "HCTR11": "Exposicao relevante ao Grupo Gramado Parks (recuperacao judicial desde abr/2023): ~11% da carteira inadimplente e ~74% em carencia de juros; assembleias aprovaram waivers de CRIs (GPK/Brasil Parques ate jul/2026, Resort do Lago ate set/2026). Tambem citado em inadimplencia de CRI do Shopping Feira da Madrugada, com execucao de garantias. Distribuicao e cota fortemente pressionadas. (Noticias e fatos relevantes, 2025-2026.)",
    "DEVA11": "Inadimplencia da carteira de CRIs em ~11-12% (jan/2026), concentrada no Grupo Gramado Parks (maior devedor, em recuperacao judicial); ~64% da carteira em carencia de juros. Distribuicao cortada para R$0,30/cota e cota em forte desconto. Execucao de garantias aprovada (Forte Securitizadora). Fundo em reestruturacao. (Noticias e fatos relevantes, 2025-2026.)",
    "VSLH11": "CRIs inadimplentes do Grupo Gramado Parks (recuperacao judicial) e do Shopping Feira da Madrugada; vencimento antecipado e execucao de garantias aprovados. Distribuicao colapsou (~R$0,03-0,04/cota em 2026) por inadimplencia recorrente e diferimentos sucessivos de juros a devedores relevantes. (Noticias e fatos relevantes, 2025-2026.)",
    "URPR11": "Inadimplencia de CRIs residenciais agravada pela Selic a 15%; gestao renegociou e diferiu juros de varios devedores, reduzindo a distribuicao. Cortes sucessivos de dividendo (R$0,40 em ago/2025, menor patamar desde 2020, ante ~R$2,00 no passado); cota caiu de ~R$38 para ~R$20 em 2026. (Noticias e fatos relevantes, 2025-2026.)",
}


def aplica_overrides(doc):
    idx = {f.get("ticker"): f for f in doc["fundos"]}
    n = 0
    for tk, pa in OVERRIDES.items():
        f = idx.get(tk)
        if not f:
            continue
        f["pontos_atencao"] = pa
        f["analise_qual"] = "Leitura de noticias e fatos relevantes publicos (midia especializada, 2025-2026)."
        f["analise_qual_id"] = "manual-noticias-202607"
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apenas")
    ap.add_argument("--limite", type=int)
    ap.add_argument("--sleep", type=float, default=0.6)
    a = ap.parse_args()

    if not IDX.exists():
        sys.exit("documentos/indice.json não encontrado — rode antes o workflow 'Indice de documentos'.")
    doc = json.loads(DATA.read_text(encoding="utf-8"))
    idx = json.loads(IDX.read_text(encoding="utf-8"))
    if not isinstance(idx, list):
        idx = idx.get("docs") or idx.get("documentos") or []
    novos_ov = aplica_overrides(doc)
    if novos_ov:
        DATA.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"overrides manuais aplicados: {novos_ov}")
    alvo = {x.strip().upper() for x in a.apenas.split(",")} if a.apenas else None
    feitos = pulados = falhas = 0
    for f in doc["fundos"]:
        if alvo and f["ticker"] not in alvo:
            continue
        if a.limite and feitos >= a.limite:
            break
        rel = rel_recente(f["ticker"], idx)
        if not rel:
            continue
        rid = str(rel["id"])
        frs = frs_recentes(f["ticker"], idx, 2)
        combo = rid + ("|fr:" + "+".join(str(x["id"]) for x in frs) if frs else "")
        # Não sobrescrever análises manuais ricas (marcadas com "manual-...")
        if str(f.get("analise_qual_id", "")).startswith("manual"):
            fr_itens = analisa_frs(frs)
            if fr_itens:
                f["fatos_relevantes"] = fr_itens
                DATA.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
            pulados += 1
            continue
        if f.get("analise_qual_id") == combo:
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
            fr_itens = analisa_frs(frs)
            if fr_itens:
                f["fatos_relevantes"] = fr_itens
                risco = [i for i in fr_itens if i["risco"]]
                if risco:
                    pa += " " + " ".join(f"Fato relevante ({i['data']}): {i['resumo']}" for i in risco)
            f["analise_qual"] = aq
            f["pontos_atencao"] = pa
            f["analise_qual_id"] = combo
            feitos += 1
            print(f"[{feitos}] {f['ticker']} ok ({ref}, {len(frs)}FR)")
            DATA.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
            time.sleep(a.sleep)
        except Exception as e:
            falhas += 1
            print(f"  ! {f['ticker']}: {e}")

    print(f"\nOK. feitos:{feitos} | já atualizados:{pulados} | falhas:{falhas}")


if __name__ == "__main__":
    main()
