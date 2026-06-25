#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Coletor de documentos do Fundos.NET (fnet / B3) — Engage, uso interno.

Baixa, para TODOS os fundos do data.json, os documentos das categorias-alvo
(por padrão: Relatório Gerencial e Fato Relevante), organizados por fundo,
e gera um índice (documentos/indice.json) para o dashboard consumir.

NÃO roda no sandbox do assistente — execute na sua máquina (ou no GitHub Actions).

Requisitos:
    pip install requests
Uso:
    python scripts/baixar_fnet.py                 # todos os fundos, categorias padrão
    python scripts/baixar_fnet.py --apenas ALZR11,HGLG11
    python scripts/baixar_fnet.py --desde 2024    # só documentos a partir do ano
    python scripts/baixar_fnet.py --so-indice     # lista e indexa, sem baixar PDFs
    python scripts/baixar_fnet.py --sleep 0.7     # pausa entre requisições (gentil com o fnet)

Observação de volume: "de todos" gera muitos arquivos (vários GB). É incremental
(pula o que já baixou), então pode rodar em partes e reexecutar sem duplicar.
"""
import argparse, json, os, re, sys, time, unicodedata
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Falta a biblioteca 'requests'. Rode: pip install requests")

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data.json"
OUT = BASE / "documentos"
LISTA_URL = "https://fnet.bmfbovespa.com.br/fnet/publico/pesquisarGerenciadorDocumentosDados"
DOWN_URL = "https://fnet.bmfbovespa.com.br/fnet/publico/exibirDocumento"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://fnet.bmfbovespa.com.br/fnet/publico/abrirGerenciadorDocumentosCVM",
}
CATEGORIAS_PADRAO = ["relatorio gerencial", "fato relevante"]


def norm(s):
    s = (s or "").lower().strip()
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def saneia(s, max_len=120):
    s = re.sub(r"[^\w\-.]+", "_", (s or "").strip())
    return s[:max_len].strip("_") or "doc"


def parse_data(doc):
    """Extrai datetime de dataReferencia/dataEntrega (formatos dd/mm/aaaa[ hh:mm])."""
    for campo in ("dataReferencia", "dataEntrega"):
        v = doc.get(campo)
        if not v:
            continue
        for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
            try:
                return datetime.strptime(v.strip(), fmt)
            except ValueError:
                pass
    return None


def listar_documentos(session, cnpj, page_len=200):
    """Pagina a API do fnet e devolve a lista completa de documentos do fundo."""
    docs, start = [], 0
    while True:
        params = {"d": 1, "s": start, "l": page_len, "cnpjFundo": cnpj}
        for tentativa in range(3):
            try:
                r = session.get(LISTA_URL, params=params, headers=HEADERS, timeout=40)
                r.raise_for_status()
                payload = r.json()
                break
            except Exception as e:
                if tentativa == 2:
                    print(f"      ! falha ao listar (start={start}): {e}")
                    return docs
                time.sleep(2 * (tentativa + 1))
        lote = payload.get("data", []) or []
        docs.extend(lote)
        total = payload.get("recordsTotal", len(docs))
        start += page_len
        if start >= total or not lote:
            break
        time.sleep(0.2)
    return docs


def e_alvo(doc, alvos_norm):
    campos = norm(" ".join([
        doc.get("categoriaDocumento") or "",
        doc.get("tipoDocumento") or "",
        doc.get("especieDocumento") or "",
    ]))
    return any(a in campos for a in alvos_norm)


def baixar(session, doc_id, destino, sleep):
    for tentativa in range(3):
        try:
            r = session.get(DOWN_URL, params={"id": doc_id, "cvm": "true"},
                            headers=HEADERS, timeout=60)
            r.raise_for_status()
            ct = (r.headers.get("Content-Type") or "").lower()
            ext = ".pdf" if "pdf" in ct else (".zip" if "zip" in ct else ".bin")
            destino = destino.with_suffix(ext)
            destino.write_bytes(r.content)
            time.sleep(sleep)
            return destino
        except Exception as e:
            if tentativa == 2:
                print(f"      ! falha download id={doc_id}: {e}")
                return None
            time.sleep(2 * (tentativa + 1))


def main():
    ap = argparse.ArgumentParser(description="Coletor de documentos do fnet (Engage).")
    ap.add_argument("--apenas", help="Tickers separados por vírgula (ex.: ALZR11,HGLG11)")
    ap.add_argument("--desde", type=int, help="Ano mínimo do documento (ex.: 2024)")
    ap.add_argument("--categorias", help="Sobrescreve categorias-alvo (csv, sem acento)")
    ap.add_argument("--sleep", type=float, default=0.5, help="Pausa entre downloads (s)")
    ap.add_argument("--max-por-fundo", type=int, help="Limite de documentos por fundo (teste)")
    ap.add_argument("--so-indice", action="store_true", help="Lista e indexa sem baixar PDFs")
    args = ap.parse_args()

    if not DATA.exists():
        sys.exit(f"data.json não encontrado em {DATA}")
    fundos = json.loads(DATA.read_text(encoding="utf-8")).get("fundos", [])
    if args.apenas:
        alvo_tk = {t.strip().upper() for t in args.apenas.split(",")}
        fundos = [f for f in fundos if f["ticker"] in alvo_tk]
    alvos = [norm(x) for x in (args.categorias.split(",") if args.categorias else CATEGORIAS_PADRAO)]

    OUT.mkdir(exist_ok=True)
    indice_path = OUT / "indice.json"
    indice = json.loads(indice_path.read_text(encoding="utf-8")) if indice_path.exists() else []
    ja_baixados = {str(e["id"]) for e in indice}

    session = requests.Session()
    cats_vistas = {}
    tot_baixados = tot_pulados = tot_falhas = 0
    sem_cnpj = []

    for i, f in enumerate(fundos, 1):
        cnpj = (f.get("cnpj") or "").strip()
        tk = f["ticker"]
        if not cnpj:
            sem_cnpj.append(tk)
            continue
        print(f"[{i}/{len(fundos)}] {tk} ({f.get('classe')}/{f.get('subclasse')}) — listando…")
        docs = listar_documentos(session, cnpj)
        for d in docs:
            cats_vistas[d.get("categoriaDocumento") or "?"] = cats_vistas.get(d.get("categoriaDocumento") or "?", 0) + 1
        alvo_docs = [d for d in docs if e_alvo(d, alvos)]
        if args.desde:
            alvo_docs = [d for d in alvo_docs if (parse_data(d) or datetime(1900, 1, 1)).year >= args.desde]
        if args.max_por_fundo:
            alvo_docs = sorted(alvo_docs, key=lambda d: parse_data(d) or datetime(1900, 1, 1), reverse=True)[:args.max_por_fundo]
        print(f"      {len(docs)} documentos no total · {len(alvo_docs)} nas categorias-alvo")

        pasta = OUT / saneia(tk)
        if not args.so_indice:
            pasta.mkdir(exist_ok=True)
        for d in alvo_docs:
            did = str(d.get("id"))
            if did in ja_baixados:
                tot_pulados += 1
                continue
            dt = parse_data(d)
            data_str = dt.strftime("%Y-%m-%d") if dt else "sem-data"
            cat = saneia(norm(d.get("categoriaDocumento") or "doc"))
            nome = f"{data_str}__{cat}__{did}"
            registro = {
                "ticker": tk, "cnpj": cnpj, "id": did,
                "categoria": d.get("categoriaDocumento"), "tipo": d.get("tipoDocumento"),
                "especie": d.get("especieDocumento"), "dataReferencia": d.get("dataReferencia"),
                "dataEntrega": d.get("dataEntrega"),
                "url": f"{DOWN_URL}?id={did}&cvm=true", "arquivo": None,
            }
            if not args.so_indice:
                salvo = baixar(session, did, pasta / nome, args.sleep)
                if salvo:
                    registro["arquivo"] = str(salvo.relative_to(BASE)).replace("\\", "/")
                    tot_baixados += 1
                else:
                    tot_falhas += 1
                    continue
            indice.append(registro)
            ja_baixados.add(did)
        indice_path.write_text(json.dumps(indice, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n===== RESUMO =====")
    print(f"Baixados: {tot_baixados} | Pulados (já existiam): {tot_pulados} | Falhas: {tot_falhas}")
    print(f"Índice: {indice_path} ({len(indice)} registros)")
    if sem_cnpj:
        print(f"Sem CNPJ (pulados, ex. FIPs): {', '.join(sem_cnpj)}")
    print("\nCategorias encontradas (para ajustar o filtro --categorias se quiser):")
    for c, n in sorted(cats_vistas.items(), key=lambda x: -x[1]):
        print(f"  {n:6d}  {c}")


if __name__ == "__main__":
    main()
