# Coletor de Notícias com Análise de Sentimento

Coletor de notícias em tempo real de fontes gratuitas (RSS Brasil, BCB, Federal Reserve, Finnhub, GDELT) com classificação de sentimento local via FinBERT-PT-BR e ProsusAI/finbert.

## Instalação

### 1. Dependências Python

Atualize o ambiente virtual com as novas dependências:

```powershell
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install feedparser transformers huggingface_hub
```

### 2. PyTorch (CPU)

Para Windows, instale o PyTorch otimizado para CPU:

```powershell
.venv\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cpu
```

**Nota:** O PyTorch com CPU (~200 MB) é suficiente. Os modelos FinBERT-PT-BR e FinBERT-EN consomem ~2 × 440 MB de RAM em fp32.

### 3. Primeira execução

Na primeira execução, o coletor baixará os modelos FinBERT para o cache do Hugging Face (`%USERPROFILE%\.cache\huggingface`, ~900 MB total). Isso pode levar alguns minutos.

Para rodar offline após o download inicial:

```powershell
$env:HF_HUB_OFFLINE = "1"
.venv\Scripts\python.exe coletor_noticias.py NOTICIAS 60
```

## Variáveis de Ambiente (Opcionais)

### FINNHUB_API_KEY

Chave gratuita do [Finnhub](https://finnhub.io/):

- 60 chamadas/min no plano gratuito
- Notícias globais em inglês (categorias `general` e `forex`)

Sem a variável, o Finnhub é desabilitado automaticamente com um aviso.

```powershell
$env:FINNHUB_API_KEY = "sua_chave_aqui"
```

### TYPESAFE_API_KEY (Jev)

[TypeSafe Jev](https://docs.typesafe.ai/) é opcional e **desligado por padrão**. Custo estimado: ~US$ 0,05/dia (1.000 notícias/dia × 1.250 tokens/notícia × US$ 0,042/1M tokens).

Requer Python >= 3.10 e `pip install typesafe-sdk`.

```powershell
.venv\Scripts\python.exe -m pip install typesafe-sdk
$env:TYPESAFE_API_KEY = "sua_chave_aqui"
.venv\Scripts\python.exe coletor_noticias.py NOTICIAS 60 --jev
```

## Uso

### Teste rápido (10 minutos)

```powershell
.venv\Scripts\python.exe coletor_noticias.py NOTICIAS 600 --db dados/book.db
```

Isso:
- Coleta notícias de RSS BR, BCB, Fed (e Finnhub/GDELT se configurados)
- Deduplica por URL, hash de título e similaridade de Jaccard
- Classifica sentimento com FinBERT-PT-BR/EN (CPU, 2 threads)
- Agrega por minuto e 5 minutos
- Grava no `dados/book.db`

### Produção (via supervisor)

O `iniciar_coleta.ps1` tem uma entrada `NOTICIAS` pronta (segunda a sexta, 06:00 às 23:30, horários do `rodar_coleta.py`), mas **ela está comentada** (linha 43): o supervisor só a assume depois de instalar feedparser/transformers/torch e descomentar. Enquanto isso, o coletor roda em primeiro plano:

```powershell
.venv\Scripts\python.exe coletor_noticias.py NOTICIAS 60 --db dados/book.db
```

Com a entrada ativa, o log fica em `dados\supervisor_NOTICIAS.log`.

### Estado das fontes em 18/11/2026

Verificado com `coletor_noticias.py NOTICIAS 180 --db %TEMP%\teste_noticias3.db` (banco temporário, 15 fontes ativas): **370 linhas gravadas, 370 URLs distintas, 369 originais**. A única duplicata é o `bcb_notastecnicas`: NT 53/2021 e NT 54/2021 têm o mesmo título ("BC - Nota Técnica de agosto 2021") e o hash de título marca a segunda como cópia. Antes da correção da dedup eram 359 originais: o `bcb_focus` perdia 9 dos 10 relatórios semanais.

| Fonte | Tipo | Estado | Itens no feed | Originais | Dup |
|---|---|---|---|---|---|
| infomoney | RSS pt | OK | 10 | 10 | 0 |
| moneytimes | RSS pt | OK | 10 | 10 | 0 |
| valor | RSS pt | OK | 100 | 100 | 0 |
| estadao_economia | RSS pt | OK | 20 | 20 | 0 |
| exame | RSS pt | OK | 25 | 25 | 0 |
| g1_economia | RSS pt | OK | 100 | 100 | 0 |
| poder360 | RSS pt | OK | 10 | 10 | 0 |
| braziljournal | RSS pt | OK | 15 | 15 | 0 |
| agenciabrasil_economia | RSS pt | OK | 10 | 10 | 0 |
| bcb_noticias | Atom pt | OK | 10 | 10 | 0 |
| bcb_copom_comunicados | Atom pt | OK | 10 | 10 | 0 |
| bcb_copom_atas | Atom pt | OK | 10 | 10 | 0 |
| bcb_focus | Atom pt | OK | 10 | 10 | 0 |
| bcb_notastecnicas | Atom pt | OK | 10 | 9 | 1 |
| fed_press | RSS en | OK | 20 | 20 | 0 |
| **total** | | | **370** | **369** | **1** |

- `g1_economia` também era afetado: "Mega-Sena 3086" e "Mega-Sena 3087" caíam como duplicata; agora são duas notícias.
- `investing_br` continua `ativo: False` no `FONTES` (o RSS respondeu 200 em 18/11, mas não foi ligado nesta tarefa).
- Finnhub: desabilitado por falta de `FINNHUB_API_KEY` (aviso na partida).
- GDELT: **0 linhas** em `gdelt_timeline`; as 3 consultas foram tentadas e a API devolveu HTTP 429 (ver Troubleshooting). Sem GDELT as features de notícia seguem funcionando.

Classificação e latência nesse teste (CPU, cache de modelos já quente):

- 369 notícias classificadas (349 pt com FinBERT-PT-BR, 20 en com ProsusAI/finbert), em 24 lotes.
- Carga dos modelos: 11,5 s; inferência total: 43,2 s.
- Latência coleta → score: média **29,3 s**, máxima 53,1 s (fila zerada no fim).
- 243 notícias com `relevancia >= 0,5` (65,9%); temas: oficial 69, fiscal_br 62, politica_br 32, cambio 27, fed_eua 22, inflacao_br 11, copom_selic 11, atividade_br 5, commodities 4.
- Agregados gravados em `noticias_1min`/`noticias_5min` nos três escopos (br 300, oficial 69, todas 369), sem evento macro na janela.

### Opções

```
python coletor_noticias.py [GRUPO] [SEGUNDOS] [--db dados/book.db] [--jev] [--sem-gdelt] 
                           [--sem-classificar] [--fontes infomoney,bcb_noticias,...]
```

- `--jev`: habilita TypeSafe Jev (requer `TYPESAFE_API_KEY`)
- `--sem-gdelt`: desabilita GDELT (a API responde em ~12 s e, desde 18/11/2026, devolve HTTP 429 para este IP)
- `--sem-classificar`: desabilita classificação de sentimento (só coleta e dedup)
- `--fontes`: filtra fontes separadas por vírgula (ex: `infomoney,valor,bcb_noticias`)

## Validação

Execute o estudo de validação fora da amostra:

```powershell
.venv\Scripts\python.exe estudo_noticias.py --parte todos --db dados/book.db --csv-dir dados
```

Partes:
- `cobertura`: latência, duplicatas, dias cobertos
- `intraday`: R² incremental (noticias → vol da próxima hora)
- `eod`: noticias da noite → vol e retorno do dia seguinte
- `rotulos`: concordância com conjunto rotulado (quando existir)

**Critério de aprovação:** R² incremental >= +0,01 no alvo `log_win_rv_fut` (volatilidade da próxima hora), com sinal estável no walk-forward.

Antes de 40 pregões coletados, o estudo imprime "amostra insuficiente".

## Testes

```powershell
.venv\Scripts\python.exe testar_noticias.py
```

Testa:
- Normalização de URL e título
- Deduplicação (inclusive título igual com data/edição diferente, que não é duplicata)
- Similaridade de Jaccard
- Conversão de timestamps
- Regra anti-lookahead (ts_pub_ms > ts_coleta_ms + 5min → NULL)
- Agregação de 1 min

## Tabelas no `dados/book.db`

| Tabela | Descrição |
|---|---|
| `noticias` | Manchetes brutas (1 por notícia única, pós-dedup): id, ts_coleta_ms, ts_pub_ms, fonte, idioma, titulo, resumo, url, hash_titulo, dup_de, evento |
| `noticias_score` | Classificação (1+ por notícia original): modelo (finbert, jev-1.13.0), sentimento (p_pos - p_neg), p_pos/neg/neu, conf, relevancia, tema, surpresa |
| `noticias_1min` | Agregados por minuto: n, n_relevantes, sent_medio, sent_min, frac_neg, surpresa_max, evento |
| `noticias_5min` | Agregados por 5 min: + n_60, sent_60 (meia-vida 30 min), z_volume, pico (z >= 3) |
| `calendario_macro` | Eventos agendados (Copom, FOMC, IPCA): ts_ms, tipo, pais, descricao, origem (manual via CSV ou feed detectado) |
| `noticias_estado` | Estado de consulta por fonte: etag, last_modified, ultimo_ok_ms, falhas_seguidas, ultimo_id (Finnhub) |
| `gdelt_timeline` | Volume e tom GDELT (br_juros, br_fiscal, us_fed): ts_ms (passo de 15 min), consulta, volume, tom |

## Exportação para Bots

As notícias são incluídas automaticamente no `exportar_para_bot.py`:

```powershell
.venv\Scripts\python.exe exportar_para_bot.py --desde 2026-09-24
```

CSVs gerados em `dados/export_bot/`:
- `noticias.csv`: últimas 500 manchetes com sentimento
- `noticias_5min.csv`: agregados de 5 min
- `calendario_macro.csv`: eventos agendados

## Convenções do Projeto

- **`ts_coleta_ms`** é a hora de disponibilidade (quando o coletor VIU a notícia), nunca `ts_pub_ms` (para evitar lookahead)
- **`ts_pub_ms`** pode ser `NULL` se o feed não declarar ou se for inválido (> ts_coleta_ms + 5 min)
- **Deduplicação**: por URL canônica (id = sha1(url)), hash de título (48h) e Jaccard >= 0.8 (2h) **com os mesmos números** (data/edição/percentual); sem essa trava, "Focus - 13/11/2026" e "Focus - 06/11/2026" davam Jaccard 1,0 (os números de 1-2 dígitos ficam fora do Jaccard)
- **Classificação**: roda em thread separada; mediana de atraso < 60s em CPU
- **Agregação**: idempotente (`INSERT OR REPLACE`); recalcula últimos 70 min a cada minuto fechado
- **ETag/If-Modified-Since**: 304 → nada novo, sem parse
- **Disjuntor**: 3 falhas seguidas → pausa exponencial (min 30s, max 30 min)

## Integração com `coletor_regime.py` (Futuro)

Após validação em `estudo_noticias.py` (R² incremental >= +0,01), adicionar features em `coletor_regime.py::estado`:

```python
# em coletor_regime.py
cursor = db.execute(
    """SELECT sent_medio, n_relevantes, pico FROM noticias_5min
       WHERE escopo IN ('br','oficial') AND modelo='finbert' AND ts_ms < ?
       ORDER BY ts_ms DESC LIMIT 12""",
    (agora_ms,)
)
# calcular log1p_n60, sent60, pico_ult_30min, etc.
```

E em `modelo_regime.py`, incluir as features aprovadas no modelo `hora`.

## Lacunas Conhecidas

- **Fins de semana:** não coletados no momento; na segunda às 06:00, a passada de partida grava os itens ainda presentes nos feeds com `ts_coleta_ms` = segunda 06:00 (correto pela regra anti-lookahead)
- **Latência:** esperada de 1 a 5 minutos entre publicação e coleta (RSS + intervalo de consulta)
- **GDELT:** desde 18/11/2026 a API devolve HTTP 429 para este IP (limite de 1 requisição/5 s); quando responde, atualiza a cada 15 min com 15 a 30 min de atraso e serve só para contexto lento
- **FinBERT-PT-BR:** ajustado com apenas 500 exemplos (model card); validar com conjunto rotulado antes de confiar
- **Jev:** modelo recente (setembro 2026), mais forte em inglês, sem avaliação independente em finanças

## Troubleshooting

### Erro "huggingface_hub.utils._errors.LocalEntryNotFoundError"

O modelo não está no cache. Rode sem `HF_HUB_OFFLINE=1` para baixar:

```powershell
Remove-Item Env:\HF_HUB_OFFLINE
.venv\Scripts\python.exe coletor_noticias.py NOTICIAS 60
```

### "banco ocupado" nos logs

Normal em picos de gravação. O coletor usa transações curtas e tenta de novo no ciclo seguinte. Se persistir por mais de 3 ciclos, verifique se há outro processo travando o banco.

### Classificação lenta (fila crescendo)

- Reduza o número de fontes com `--fontes`
- Ou desabilite GDELT com `--sem-gdelt`
- Ou use `--sem-classificar` e classifique offline depois

### GDELT timeout / HTTP 429

O handshake do GDELT levou 11-19 s em 18/11/2026 (timeout configurado: conexão 20 s, leitura 60 s). Além disso, a API devolveu **HTTP 429** ("Please limit requests to one every 5 seconds") em **todas** as tentativas nesse dia, mesmo com 8 s entre chamadas e sem o cabeçalho `Retry-After`; o coletor repete uma vez com pausa de 6 s e desiste daquela consulta. Se persistir, use `--sem-gdelt`: as notícias e o sentimento não dependem dele.

## Referências

- FinBERT-PT-BR: https://huggingface.co/lucas-leme/FinBERT-PT-BR
- ProsusAI/finbert: https://huggingface.co/ProsusAI/finbert
- TypeSafe Jev: https://docs.typesafe.ai/models
- Finnhub: https://finnhub.io/docs/api/market-news
- GDELT DOC API: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
