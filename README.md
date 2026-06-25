# Engage · Dashboard de Fundos Listados B3

Dashboard interno (Engage / BTG Pactual) para selecionar, comparar e analisar fundos listados na B3 — FII, Fiagro, FI-Infra e FIP. **Uso interno.**

## Como funciona

1. `index.html` é estático (React via CDN, sem build). Ao abrir, faz `fetch('./data.json')` e renderiza o dashboard.
2. `data.json` é a base de dados (281 fundos). Vem do **BTG Stock Guide de Fundos** (FII/Fiagro/FI-Infra) + **Investidor10** (FIP). Atualizado pelo pipeline.
3. **Atualização diária**: GitHub Actions roda `scripts/build_data.py`, que atualiza cotações (Yahoo Finance) e CDI (BCB), recalcula valor de mercado e P/VP, e comita o `data.json`. A Vercel republica em seguida.
4. `documentos/indice.json` (opcional) lista os documentos coletados do Fundos.NET; quando presente, a ficha de cada fundo mostra os documentos com link.

## Estrutura

```
.
├── index.html                 # Dashboard React (CDN, sem build)
├── data.json                  # Base de dados — atualizada pelo Action (não editar à mão)
├── vercel.json                # Config Vercel (cache do data.json)
├── scripts/
│   ├── build_data.py          # Atualização diária: cotações Yahoo + CDI BCB
│   └── baixar_fnet.py         # Coletor de documentos do Fundos.NET (rodar local)
├── documentos/                # PDFs por fundo + indice.json (NÃO versionado, exceto o índice)
└── .github/workflows/
    └── daily.yml              # Agenda 09:07 e 18:07 BRT (seg-sex)
```

## Deploy (uma vez)

1. **GitHub**: crie um repositório (ex. `fundos-listados`) e suba estes arquivos.
2. **Vercel**: importe o repositório (Framework Preset: *Other* — é estático, sem build). O deploy publica `index.html` + `data.json`.
3. **Senha** (uso interno): no projeto da Vercel → Settings → Deployment Protection → ative *Password Protection* (ou Vercel Authentication). Define a senha de acesso.
4. **Build diário** (opcional, como no Isentos): em cron-job.org, agende um `repository_dispatch` (event type `build`) 2×/dia úteis, ou confie no cron nativo do Actions já configurado.

## Atualizar os fundamentais (BTG)

Cotação e P/VP atualizam sozinhos (diário). Os **fundamentais** (patrimônio, DY, segmento) vêm do snapshot do BTG Stock Guide. Para atualizá-los, gere um novo `data.json` a partir do xlsx mais recente do BTG (o assistente faz isso) e comite.

## Coletor de documentos (Fundos.NET)

`scripts/baixar_fnet.py` baixa Relatório Gerencial e Fato Relevante de todos os fundos:

```
pip install requests
python scripts/baixar_fnet.py --apenas ALZR11 --desde 2024   # teste
python scripts/baixar_fnet.py --desde 2024                   # todos, recente
```

Salva em `documentos/<TICKER>/` e gera `documentos/indice.json` (consumido pela ficha). É incremental.

## Pendências

- CNPJ dos 11 FIPs (para o link Fundos.NET deles).
- Site / Taxa de adm. / benchmark por fundo (da planilha de cadastro).
- Indicadores específicos por classe (vacância, LTV, duration) — dos relatórios gerenciais.

## Fontes e aviso

Dados: BTG Stock Guide de Fundos e Investidor10. Material de uso interno; não constitui recomendação de investimento. Segue o Guideline de Marketing do BTG Pactual e a Resolução CVM 178.
