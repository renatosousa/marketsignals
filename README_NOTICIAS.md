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
- Deduplica por URL e hash de título
- Classifica sentimento com FinBERT-PT-BR/EN (CPU, 2 threads)
- Agrega por minuto e 5 minutos
- Grava no `dados/book.db`

### Produção (via supervisor)

O `iniciar_coleta.ps1` já inclui o supervisor `NOTICIAS` que roda de segunda a sexta, das 06:00 às 23:30 (horários do `rodar_coleta.py`).

```powershell
.\iniciar_coleta.ps1
```

O log fica em `dados\supervisor_NOTICIAS.log`.

### Opções

```
python coletor_noticias.py [GRUPO] [SEGUNDOS] [--db dados/book.db] [--jev] [--sem-gdelt] 
                           [--sem-classificar] [--fontes infomoney,bcb_noticias,...]
```

- `--jev`: habilita TypeSafe Jev (requer `TYPESAFE_API_KEY`)
- `--sem-gdelt`: desabilita GDELT (lento, timeout de 45s por requisição)
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
- Deduplicação
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
- **Deduplicação**: por URL canônica (id = sha1(url)), hash de título (48h) e Jaccard >= 0.8 (2h)
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
- **GDELT:** atualiza a cada 15 min com 15 a 30 min de atraso; serve só para contexto lento
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

### GDELT timeout

O GDELT pode levar até 45s por requisição (handshake TLS lento). Se falhar consistentemente, use `--sem-gdelt`.

## Referências

- FinBERT-PT-BR: https://huggingface.co/lucas-leme/FinBERT-PT-BR
- ProsusAI/finbert: https://huggingface.co/ProsusAI/finbert
- TypeSafe Jev: https://docs.typesafe.ai/models
- Finnhub: https://finnhub.io/docs/api/market-news
- GDELT DOC API: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
