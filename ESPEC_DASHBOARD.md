# Especificação: reestruturação do dashboard e das correlações

Escrita em 13/11/2026 para ser implementada depois por outro modelo. Quem implementa não viu a conversa que gerou este
documento: tudo o que precisa está aqui e nos arquivos citados. Os números de "medido" saem de
`python estudo_correlacoes.py` (somente leitura, ~20 s) e podem ser refeitos.

## 1. Objetivo

O dono do projeto acompanha o mercado pelo dashboard, muitas vezes pelo celular. Hoje a página principal mostra muitas
séries lado a lado e uma única tabela de correlação, que tem defeitos de método. O objetivo é:

1. Corrigir a forma de medir e mostrar correlações (seção 4).
2. Acrescentar os cruzamentos que os dados já permitem e que têm sinal medido (seção 6).
3. Reorganizar a exibição em páginas por assunto, com um resumo curto na entrada (seção 5).

Três perguntas o dono faz quase todo dia e hoje são respondidas à mão, com consultas ao banco. Elas viram painéis e são
a régua de sucesso do trabalho:

| Pergunta | Painel que passa a responder |
|---|---|
| "Como está a coleta?" | Coleta (6.1) |
| "Saiu o fluxo oficial? Acertamos?" | Placar do estrangeiro (6.6) |
| "Como foi o fechamento?" | Fechamento do dia (6.2) |

## 2. Contexto obrigatório

Leia antes de mexer: `CONTEXTO_PARA_BOTS.md` (tabelas e armadilhas), o docstring de `dashboard.py`, `players.py`,
`features_fluxo.py`, `fluxo_estrangeiro.py` e `grafico_quant.py`.

**Convenções do projeto**

- `ts_ms` guarda a hora de Brasília codificada como epoch UTC. Use `datetime.utcfromtimestamp`, sem somar ou tirar 3 h.
  Dia = `ts_ms // 86_400_000` (11/11/2026 = 20768).
- Python: `.venv/Scripts/python.exe`. Só `numpy` e `pandas`; não há `scipy`. Não acrescente dependências.
- Front: HTML e JS puros, uPlot pelo CDN já usado, sem etapa de build. Temas claro e escuro pelas variáveis CSS que já
  existem em `dashboard.html`.
- O dashboard abre o banco em modo somente leitura (`file:...?mode=ro`). Quem grava é coletor ou job, nunca um `GET`.
- Código e docstrings em português sem acento, como no resto do repositório. Textos da interface com acento.
- Testes: `python -m unittest discover -p "test_*.py"` (121 testes passando em 13/11). Toda função de cálculo nova
  entra com teste de unidade em banco SQLite temporário ou com dados sintéticos, no estilo de `test_players.py`.
- Ao terminar cada fase: commit e push direto no `main`.

**Restrições**

- O servidor escuta só em `127.0.0.1` e no IP do Tailscale. Nunca `0.0.0.0`. A página não tem senha.
- Não apague dados do banco. Não faça downloads de fontes novas sem autorização do dono.
- Nenhum texto do dashboard pode recomendar operação. Painel que interpreta o mercado termina com a linha
  "Leitura dos dados, não recomendação."
- `GET /api/dados` é usado por bots (`CONTEXTO_PARA_BOTS.md`): as chaves atuais do JSON continuam existindo.
- `dashboard.html` e as outras páginas são lidas do disco a cada pedido: mudança de HTML vale na hora. Mudança em
  `dashboard.py` só vale depois de encerrar o processo `dashboard.py`; o `vigiar_dashboard.ps1` religa em até 60 s. Não
  encerre os coletores.

**Armadilhas dos dados que afetam este trabalho**

- `opcoes_resumo.atm_iv` e `rr25` são frações (0,174 = 17,4%).
- `regime_live.rv_dia_real` e `rv_dia_prev` são % do dia; para anualizar multiplique por √252.
- `barras_1m` só é atualizada na partida do coletor de regime (08:55). Para o pregão em curso use `snapshots` (WIN$,
  BOVA11) e `macro_cotacoes` (demais). Não há BOVA11 em `barras_1m`.
- `macro_cotacoes` começa em 25/09/2026 (35 pregões em 13/11); `barras_1m` de WIN$, WDO$ e DI1F27/29/33 começa em
  jan/2026 (208 pregões).
- `tryd_saldo` tem 9 pregões (desde 30/10). As colunas de agressão (`agrc`, `agrv`, `sagr`) estão vazias.
- O saldo do Tryd antes da abertura é o do pregão anterior. Use sempre os filtros de `players.py`
  (`abertura_ms`, `sem_tabela_de_ontem`, `desde_o_zeramento`, `saldo_por_grupo`), nunca a tabela crua.
- O grupo de corretoras é uma hipótese (`players.MAPA`), não o tipo de investidor.
- XINA11 tem ~500 cotações por dia e BIT$ não se relaciona com o WIN (correlação +0,07): saem das tabelas.

## 3. Diagnóstico do dashboard atual

Medido em 13/11/2026.

**Exibição**

- A página principal tem 9 seções, 62 cartões e 25 gráficos numa rolagem única: 14.700 px de altura numa tela de
  574 px de largura.
- Não há hierarquia: o regime do WIN e a taxa da compromissada do BC têm o mesmo peso visual.
- `/api/dados` recalcula tudo a cada 10 s (220 KB), inclusive o que está fora da tela.
- A navegação e ~100 linhas de CSS e JS estão copiadas em 4 arquivos HTML.
- O gráfico de saldo por grupo em `/players` não mostra o preço do ativo, que é a referência para ler o saldo.

**Tabela "Correlação com o WIN"** (`dashboard.py`, função `dados`, lista `CORREL`)

| Problema | Evidência medida |
|---|---|
| A coluna "Antecipa" (1 min de defasagem) é ruído | Em grades de 5, 10, 30 e 60 s, 8 pregões, defasagens de ±1 e ±2: módulo ≤ 0,06 para todos os ativos; a única exceção é ITUB4 a 5 s (+0,10), e ali é o WIN que vem antes. Em 1 min, 40 pregões: WDO +1 min = −0,025 |
| A janela padrão (1 h) tem 60 pontos | Erro padrão ≈ 0,10 para uma correlação de −0,47. Com 15 min o erro chega a 0,20. A correlação móvel de 30 min varia 0,17 dentro do dia só por amostragem, mais que os 0,11 de diferença entre dias |
| Não há referência do que é normal | WIN×WDO: média −0,47, desvio entre pregões 0,11 (208 pregões). Em 11/11 foi −0,38, WIN×DI29 −0,14 (normal −0,26) e WDO×DI29 +0,13 (normal +0,31): um descolamento que a tabela não deixa ver |
| IVVB11 e NASD11 entram em reais | Em reais o câmbio embutido cancela a relação: −0,18 a +0,38 nos últimos 6 pregões. Em dólar (÷ WDO$): +0,10 a +0,56 |
| Só mostra correlação, sem tamanho | Falta o beta (quanto o WIN anda por unidade do outro ativo) e quanto do movimento do WIN o conjunto explica (R² de WIN ~ WDO + DI29: 0,25 em média) |
| Só o WIN é alvo | Não há WDO×DI, nem visão de conjunto |
| Ações paradas contam como retorno zero | Na janela "Pregão", ações e ETFs antes das 10:00 e depois das 16:55 achatam a correlação |

**Painéis com leitura enganosa**

- "Pressão do livro", com a legenda "−1 vendedor a +1 comprador": a correlação com o retorno do mesmo intervalo é
  **−0,50** (10 a 60 s) e com o intervalo seguinte é +0,01 a +0,03. O indicador anda contra o preço e não antecipa nada.
- "Saldo de agressão acumulado" do WIN: correlação de 0,81 a 0,91 com o retorno do mesmo intervalo. O gráfico repete o
  preço.

**Dados que existem e não são cruzados**

- Saldo por grupo de corretoras × preço.
- Cesta de agressão das ações × índice.
- Vol implícita × vol realizada × retorno.
- Estimativas do fluxo estrangeiro × dado oficial da B3.

## 4. Regras para toda correlação exibida

- **R1. Retorno.** Preço: log-retorno. Taxa (símbolos `DI1*` e `DAP*`): diferença em pontos-base. Nunca entre dias.
- **R2. Grade.** 1 minuto, último `mid` do minuto. Minuto sem cotação repete a última por até 5 minutos; depois disso
  fica sem dado. Retorno só entre dois minutos com dado.
- **R3. Sessão.** Futuros (WIN$, WDO$, IND$, DI1*): 09:05 a 17:55. Ações e ETFs: 10:05 a 16:55. Um par usa a
  interseção das duas sessões.
- **R4. Amostra mínima.** Menos de 60 retornos válidos em comum: mostra "–" e o motivo ("amostra curta, n = 37"). As
  janelas das correlações são 1 h, 3 h e Pregão. Os botões de 15 e 30 min continuam só nos gráficos de preço.
- **R5. Incerteza.** Toda correlação vai com `n` e intervalo de 95% por Fisher:
  `tanh(atanh(r) ± 1,96 / √(n − 3))`.
- **R6. Referência.** Toda correlação vai com o normal: média, desvio, p10 e p90 da correlação do pregão inteiro nos
  últimos 60 pregões anteriores (mínimo 15), e `z = (hoje − média) / desvio`. Sem 15 pregões: sem referência e sem z.
- **R7. Sem antecipação.** Nenhuma coluna de defasagem. Reintroduzir só depois de `estudo_correlacoes.py --parte leadlag`
  mostrar módulo ≥ 0,10 estável em 20+ pregões.
- **R8. Nível não é retorno.** Correlação entre séries acumuladas (preço × saldo do dia) aparece com o rótulo
  "acompanha no dia" e nunca gera alerta: duas séries com tendência se correlacionam por acaso.
- **R9. Exterior em dólar.** `SPX_USD = IVVB11 / WDO$` e `NDX_USD = NASD11 / WDO$`, calculados minuto a minuto.
- **R10. Sinal.** Cor azul (`--pos`) para positivo e vermelha (`--neg`) para negativo, como hoje. Destaque só quando
  `|z| ≥ 2` e `n ≥ 120`.

## 5. Nova estrutura

### 5.1 Páginas

| Rota | Conteúdo | Origem |
|---|---|---|
| `/` | Resumo: coleta, regime, o que explica o WIN, pares principais, alertas, fechamento do dia | novo |
| `/correlacoes` | Decomposição, tabela com referência, correlação móvel, matriz | novo |
| `/fluxo` | Placar do estrangeiro, estrangeiro hoje, mapa grupo × ativo, quem acompanha o preço, cesta × índice | novo |
| `/mercado` | WIN e BOVA11 (preço, saldo, livro), variações macro, Wyckoff | seções atuais 2, 5 (3 gráficos de variação) e 6 |
| `/opcoes` | Vol ATM, RR25, smile, fluxo, posição em aberto, vol implícita × realizada | seções atuais 3 e 4, mais 6.9 |
| `/juros` | Curva DI, inclinação, inflação implícita, Tesouro Direto, liquidez do BC | seções atuais 5 (2 gráficos), 7, 8 e 9 |
| `/grafico`, `/players`, `/leiloes` | Sem mudança, fora o preço em `/players` (6.8) | atuais |

Navegação, na ordem: Resumo · Mercado · Correlações · Fluxo · Opções · Juros · Gráfico WIN · Gráfico WDO · Players ·
Leilões.

**Limites por página** (largura de 390 px): Resumo com no máximo 3.000 px de altura, 12 cartões e 3 gráficos; as outras
com no máximo 8 gráficos. Tabelas largas rolam na horizontal dentro do cartão, nunca a página.

### 5.2 Arquivos comuns

- `estatico/comum.css`: variáveis de tema, `.card`, `.tiles`, `.grade`, `.paginas`, tabelas.
- `estatico/comum.js`: navegação (gera o `<nav>` e marca a página atual), formatos (`fmt`, `sinal`, `hora`), o
  `grafico()` genérico com a tabela gêmea, a barra de correlação e o laço de atualização com pausa quando a aba está
  oculta (`document.hidden`).
- Rota `GET /estatico/<nome>`: serve só nomes de uma lista fixa no código (sem caminho vindo do pedido).
- `grafico.html`, `players.html` e `leiloes.html` passam a usar a navegação comum; o resto delas não muda.

### 5.3 Endpoints

| Endpoint | Cache | Conteúdo |
|---|---|---|
| `GET /api/resumo` | 5 s | coleta, regime, decomposição, 4 pares, alertas |
| `GET /api/fechamento?dia=AAAA-MM-DD` | 60 s | tabela do dia (6.2); sem `dia` = último pregão |
| `GET /api/correlacoes?janela=3600\|10800\|pregao&alvo=WIN\|WDO` | 30 s | decomposição, tabela, móvel, matriz |
| `GET /api/fluxo?dia=AAAA-MM-DD` | 30 s | placar, estrangeiro do dia, mapa, alinhamento, cesta |
| `GET /api/mercado?janela=` | 3 s | chaves `win`, `bova`, `macro.graficos` de hoje |
| `GET /api/opcoes?janela=` | 3 s | chave `opcoes` de hoje, mais `vol` (6.9) |
| `GET /api/juros?janela=` | 3 s | `macro.di`, `macro.inclinacao`, `macro.breakeven` |
| `GET /api/dados?janela=` | 3 s | **inalterado**: união das três anteriores, mais `status`, `regime`, `macro.correl` |

Para isso, quebre `dashboard.dados()` em `secao_livro`, `secao_opcoes` e `secao_macro`; `dados()` vira a união delas.
Validação de parâmetros e respostas de erro seguem o padrão atual (400 com `{"erro": ...}`, 500 com a exceção).

**Orçamento de tempo** (servidor, cache frio, pregão em curso): `/api/resumo` ≤ 0,5 s; `/api/correlacoes` ≤ 3 s;
`/api/fluxo` ≤ 3 s. A referência histórica é lida de tabela pronta (5.4), nunca recalculada no pedido.

### 5.4 Módulos e tabelas novos

**`correlacoes.py`** (cálculo puro, com testes):

```python
SESSAO_FUTUROS, SESSAO_ACOES = (9 * 60 + 5, 17 * 60 + 55), (10 * 60 + 5, 16 * 60 + 55)   # minutos do dia
SINTETICOS = {"SPX_USD": ("IVVB11", "WDO$"), "NDX_USD": ("NASD11", "WDO$")}
PARES_WIN = ["WDO$", "DI1F27", "DI1F29", "DI1F33", "SPX_USD", "NDX_USD", "BEWZ39",
             "VALE3", "PETR4", "PRIO3", "ITUB4", "BBAS3", "SMAL11", "GOLD11"]
PARES_WDO = ["DI1F29", "SPX_USD", "GOLD11", "VALE3", "PETR4"]
MATRIZ = ["WIN$", "WDO$", "DI1F29", "SPX_USD", "VALE3", "PETR4", "ITUB4", "BBAS3", "SMAL11", "GOLD11"]
FATORES_WIN = ["WDO$", "DI1F29", "SPX_USD"]

def serie_1m(con, simbolo, dia, ao_vivo) -> pd.Series      # indice = minuto do dia; R2 e R9
def retornos(serie, simbolo) -> pd.Series                   # R1 e R3
def par(ry, rx) -> dict                                     # corr, n, ic_lo, ic_hi, beta; None se n < 60 (R4, R5)
def referencia(con, y, x, antes_de, n_dias=60) -> dict      # media, dp, p10, p90, dias (R6); le correl_diaria
def movel(ry, rx, janela=60) -> pd.Series                   # correlacao movel, minimo 50 pares
def betas(con, antes_de, alvo="WIN$", n_dias=20) -> dict    # le fatores_dia
def decompor(retornos_por_simbolo, betas) -> dict           # 6.3
def atualizar(db, dias=90)                                  # preenche correl_diaria e fatores_dia; idempotente
```

- `serie_1m`: com `ao_vivo=True`, WIN$ vem de `snapshots.mid` (`spread > 0`) e os demais de `macro_cotacoes.mid`. Com
  `ao_vivo=False` (histórico), usa `barras_1m.close` quando o símbolo existe lá e `macro_cotacoes` caso contrário.
- `beta` de `par` = `cov(ry, rx) / var(rx)`, em unidades de `ry` por unidade de `rx`.
- `atualizar` roda na partida de `coletor_regime.py`, depois de `mr.atualizar_barras` e de `features_fluxo`, dentro de
  `try/except` que só avisa no log. Também tem CLI: `python correlacoes.py atualizar [--dias 90]`.

```sql
CREATE TABLE IF NOT EXISTS correl_diaria (        -- correlacao do pregao inteiro, retornos de 1 min
    data TEXT NOT NULL, y TEXT NOT NULL, x TEXT NOT NULL,
    corr REAL, beta REAL, n INTEGER, calculado_em TEXT,
    PRIMARY KEY (data, y, x));
CREATE TABLE IF NOT EXISTS fatores_dia (          -- WIN ~ fatores, ajustado nos 20 pregoes ANTERIORES a `data`
    data TEXT NOT NULL, alvo TEXT NOT NULL, fator TEXT NOT NULL,
    beta REAL, r2 REAL, n INTEGER,
    PRIMARY KEY (data, alvo, fator));
CREATE TABLE IF NOT EXISTS tryd_estrangeiro_dia ( -- estimador do fluxo estrangeiro pelo saldo do Tryd (6.6)
    data TEXT NOT NULL, ativo TEXT NOT NULL,
    saldo_qtd REAL, fin_liq REAL, vol_fin_grupo REAL, vol_fin_total REAL, foto_ms INTEGER,
    PRIMARY KEY (data, ativo));
```

**`estimador_tryd.py`**: `calcular(con, dia) -> list[dict]`, `atualizar(db)` e CLI. Regra em 6.6. Também chamado na
partida de `coletor_regime.py`.

## 6. Painéis

Cada painel traz a pergunta que responde, o cálculo, a exibição e o critério de aceite.

### 6.1 Coleta (Resumo)

**Pergunta:** está tudo sendo coletado?

**Cálculo:** uma linha por fonte, com o último dado e o estado.

| Fonte | Consulta do último dado | Atrasado se |
|---|---|---|
| WIN (livro) | `max(ts_ms)` de `snapshots`, `WIN$` | > 30 s no pregão; > 180 s = parado |
| BOVA11 (livro) | idem, `BOVA11` | idem, das 10:00 às 17:00 |
| Macro | `macro_cotacoes`, `WDO$` | idem |
| Opções mensais e semanais | `opcoes_resumo`, por vencimento | > 180 s, das 10:00 às 17:00 |
| Agressão da cesta | `agressao_1min` | > 180 s, das 10:00 às 16:55 |
| Tryd saldo e fita | `tryd_saldo`, `tryd_fita` | > 180 s no pregão |
| Regime | `regime_live` | > 180 s no pregão |
| VIX | `macro_cotacoes`, `IB:VIX` | > 30 min |
| Fluxo oficial B3 | `max(data_ref)` de `fluxo_diario` | mais velho que D−2 úteis depois das 10:00 |
| Posição em aberto B3 | `b3_posicoes_resumo` | mais velha que D−1 útil depois das 10:00 |
| ANBIMA | `anbima_ettj` | mais velha que D−1 útil |
| Leilões do Tesouro | `max(data)` de `leilao_resultados` | mais de 7 dias corridos |
| Focus | `focus_ipca` | mais de 10 dias corridos |
| Barras de 1 min | `max(ts_ms)` de `barras_1m` por símbolo de `backfill_barras.MT5_ATIVOS` | não chega ao fim do pregão anterior |

"Pregão" = dia útil das 09:00 às 18:25 (feriados: `leiloes.feriados`). Fora do horário de cada fonte o estado é
"fora do horário", nunca erro. Mantenha a checagem de preço congelado (`parado_ha`).

**Exibição:** uma faixa no topo, "14 de 16 fontes em dia", verde, amarela ou vermelha; ao tocar, abre a lista. As fontes
com problema aparecem sem precisar abrir.

**Aceite:** em 13/11 a lista mostra Leilões do Tesouro (último 05/11) e Focus (23/10) como atrasados.

### 6.2 Fechamento do dia (Resumo)

**Pergunta:** como foi o pregão?

**Cálculo** (`/api/fechamento`), para o dia pedido:

- WIN$, WDO$, IND$: abertura, mínima, máxima, último e variação contra o fechamento anterior (`barras_1m`; para o dia
  em curso, `snapshots` e `macro_cotacoes`). O último do WIN é a barra das 18:24, não a das 18:00.
- Variação por faixa de horário do WIN: 09–10, 10–13, 13–16, 16–fim.
- DI1F27, DI1F29, DI1F33: taxa e variação em pontos-base.
- Ações e ETFs de `MACRO_GRAFICOS`: variação %, ordenada.
- VIX, vol ATM por vencimento (último e variação no dia), regime (`rotulo`, `rv_rel`).
- Saldo final por grupo no WIN e no WDO (`players.montar`), 3 maiores de cada lado.
- Estimador do estrangeiro do dia (6.6).

**Exibição:** cartão com tabelas compactas e seletor de dia. Sem gráfico.

**Aceite:** para 2026-11-11, pelas barras de 1 min: WIN$ abertura 173.770, mínima 173.300, máxima 174.850, último
173.375 (+0,04% sobre os 173.305 de 10/11).

### 6.3 O que explica o WIN (Resumo e Correlações)

**Pergunta:** quanto do movimento do WIN na janela vem do dólar, dos juros e do exterior, e quanto é próprio?

**Cálculo:**

1. Betas: regressão por mínimos quadrados de `r_WIN` em `[r_WDO, d_DI29, r_SPX_USD]`, retornos de 1 min dos 20 pregões
   anteriores empilhados, nunca o dia corrente. Guardados em `fatores_dia`. Fator sem histórico suficiente fica de fora.
2. Na janela: `contribuição_i = beta_i × soma(x_i)`; `própria = retorno do WIN − Σ contribuições`.
3. Converta para pontos: `% × preço do WIN no início da janela`.
4. `r2_hoje`: R² fora da amostra dos retornos de 1 min da janela com esses betas (pode ser negativo; mostre "o normal
   não explica hoje" quando for menor que 0,05).

**Exibição:** barras horizontais em pontos (Dólar, Juros, Exterior, Próprio), o total ao lado e a frase "o normal
explica X% do minuto a minuto de hoje". Com menos de 60 minutos: "amostra curta".

**Aceite:** teste com séries sintéticas `r_win = −0,5·r_wdo + ruído` recupera beta −0,5 ± 0,05 e a soma das
contribuições com a parte própria é igual ao retorno do WIN.

### 6.4 Tabela de correlações com referência (Correlações)

**Pergunta:** a relação do WIN (ou do WDO) com cada ativo hoje está dentro do normal?

**Colunas:** Ativo · Hoje (número e barra) · IC 95% · Normal (média, com a faixa p10–p90 desenhada) · z · Beta · n.

- Alvo WIN: `PARES_WIN`. Alvo WDO: `PARES_WDO`.
- Beta em unidades legíveis: pontos de WIN por 1% do ativo, ou por 1 ponto-base de taxa.
- Ordenação padrão: `|z|` decrescente; sem z, vão para o fim.
- Nomes legíveis: "S&P 500 em dólar", "Nasdaq em dólar", "DI jan/29".

**Resumo:** os 4 pares WIN×WDO, WIN×DI29, WDO×DI29 e WIN×S&P em dólar, em linha única cada, com hoje, normal e z.

**Aceite:** `correl_diaria` reproduz, com tolerância de 0,03:

| Data | WIN×WDO | WIN×DI29 | WDO×DI29 |
|---|---|---|---|
| 2026-11-10 | −0,50 | −0,29 | +0,40 |
| 2026-11-11 | −0,38 | −0,14 | +0,13 |

E a referência de WIN×WDO com 208 pregões dá média −0,47 e desvio 0,11.

### 6.5 Correlação móvel e matriz (Correlações)

- **Móvel:** correlação de 60 min (mínimo 50 pares) ao longo do pregão para um par escolhido entre os 4 principais,
  com a faixa p10–p90 do normal como banda horizontal. Um gráfico, com seletor de par.
- **Matriz:** `MATRIZ` × `MATRIZ` em mapa de calor (tabela HTML com fundo colorido e o número dentro), com alternância
  "hoje" e "diferença contra o normal". Célula com `n < 60` fica vazia.

**Aceite:** a matriz é simétrica e a diagonal é vazia; no celular rola na horizontal dentro do cartão.

### 6.6 Placar do estrangeiro (Fluxo)

**Pergunta:** saiu o fluxo oficial? As estimativas acertaram?

**Estimador do Tryd** (hoje calculado à mão; passa a ser gravado em `tryd_estrangeiro_dia`):

1. Ativos: os de `tryd_saldo` no dia que não são futuros (`players._FUTURO`).
2. Para cada ativo, só fotos a partir das 10:00. Para cada corretora, a última foto válida: `qtd`, `qtdc` e `qtdv`
   numéricos e `|qtd − (qtdc − qtdv)| < 0,5`.
3. Grupo "Estrangeiro" por `players.grupo`. `fin_liq = Σ (volc − volv)` das corretoras do grupo, em R$.
   `vol_fin_grupo = Σ (volc + volv)`; `vol_fin_total = Σ (volc + volv) / 2` de todas as corretoras.
4. Estimador do dia = soma de `fin_liq` dos ativos.

**Placar** (uma linha por pregão com dado oficial, do mais novo para o mais velho):

- Oficial: `fluxo_diario`, `tipo = 'Investidor Estrangeiro'`, `saldo_mil / 1000`.
- Tryd: soma de `tryd_estrangeiro_dia.fin_liq / 1e6`. Cesta: `features_fluxo_dia.cesta_fin_liq_mi`.
- Por estimativa: acertos de sinal, correlação de postos (`rank().corr()`), erro absoluto médio e viés
  (média de estimativa − oficial).
- Linha de comparação: acertos de sinal da regra "igual ao sinal do dia anterior".
- Pregões já fechados sem dado oficial aparecem como "oficial sai em DD/MM" (D+2 úteis).

**Estrangeiro hoje:** o estimador do Tryd parcial, por ação, e a cesta parcial (soma de `fin_compra − fin_venda` de
`agressao_1min` sem o WDO$, das 10:00 às 16:55).

**Exibição:** tabela do placar, um gráfico de dispersão oficial × estimativa (duas séries) e a nota, com N calculado:
"Com N pregões a amostra é pequena. Leia o viés antes de ler o tamanho de uma estimativa."

**Aceite:** valores em R$ mi, tolerância de 1:

| Data | Oficial | Tryd | Cesta |
|---|---|---|---|
| 2026-11-10 | −696,8 | −167,0 | +210,8 |
| 2026-11-11 | −370,0 | +8,1 | −425,5 |
| 2026-11-12 | (sai em 16/11) | +263,2 | (calculada às 08:55 de 13/11) |

Com os 7 pregões de 03/11 a 11/11 (oficial −390,8; −1.667,4; −1.734,4; −459,6; −85,9; −696,8; −370,0):

| Estimativa | Sinal certo | Postos | Erro absoluto médio | Viés |
|---|---|---|---|---|
| Tryd | 4 de 7 | 0,86 | 616 | +616 (acima do oficial nos 7 pregões) |
| Cesta | 4 de 7 | 0,39 | 666 | +620 (acima do oficial em 5 de 7) |

Em 03/11 e 04/11 o Tryd só tinha 3 ações com janela de saldo; mostre o número de ações de cada dia.

### 6.7 Mapa grupo × ativo e quem acompanha o preço (Fluxo)

**Pergunta:** quem está comprando e vendendo o quê, e quem está do lado do preço?

**Mapa:** linhas = grupos (`players.GRUPOS`), colunas = ativos do dia. Célula = saldo do grupo em R$ mi.

- Ações: `Σ (volc − volv)` do grupo.
- WIN: `saldo em contratos × preço × 0,20`. WDO: `saldo em contratos × preço × 10`.
- Fundo colorido pelo sinal, intensidade proporcional ao módulo dentro da coluna.

**Quem acompanha o preço** (por ativo, com seletor):

- `corr_5m`: correlação entre a variação do saldo do grupo e o retorno do ativo em baldes de 5 min do dia, mínimo de 24
  baldes. Use 5 min: em 1 min o saldo aparece até 1 minuto depois do movimento (medido: +0,31 com 1 min de atraso
  contra +0,15 no mesmo minuto, grupo Estrangeiro no WIN).
- `nivel`: correlação entre o saldo acumulado e o preço no dia (R8).
- Rótulo: "a favor do preço" se `corr_5m ≥ +0,25`; "contra o preço" se `≤ −0,25`; senão "sem relação".

**Medido em 9 pregões** (referência para conferir a implementação, não para exibir como regra):

| Grupo no WIN | corr 5 min | nível | Saldo final com o sinal do dia |
|---|---|---|---|
| Estrangeiro | +0,44 | +0,57 | 7 de 9 |
| BTG | −0,48 | −0,61 | 4 de 9 |
| XP | −0,33 | −0,69 | 4 de 9 |

Nenhum grupo antecipa o preço (defasagem de +1: módulo ≤ 0,09).

**Exibição:** o mapa, a tabela por grupo e a nota: "Grupo é uma hipótese sobre quem opera por cada corretora. O saldo
acompanha o preço; não o antecipa."

### 6.8 Preço em `/players` e cesta × índice (Fluxo)

- **`/players`:** no gráfico "Posição líquida por grupo", desenhe o preço do ativo em eixo à direita, linha cinza
  (`--muted`). O `/api/players` ganha a chave `preco` com a série na mesma grade de `t`.
- **Cesta × índice:** um gráfico com o saldo acumulado da cesta de agressão (R$ mi, eixo esquerdo) e a variação % do
  IND$ desde as 10:00 (eixo direito). Ao lado, a correlação de 5 min do dia e o normal. Medido: +0,40 em 5 min, +0,35
  em 1 min, sem antecipação.

### 6.9 Vol implícita × realizada (Opções)

**Pergunta:** a vol das opções está cara ou barata contra o que o mercado oscila?

**Cálculo:**

- Vol implícita: `atm_iv × 100` do vencimento mais próximo com 5 ou mais dias úteis (mesma regra de
  `grafico_quant.implicito`).
- Realizada do dia: `rv_dia_real × √252`. Prevista pelo modelo: `rv_dia_prev × √252`.
- Diferença em pontos de vol: implícita − realizada.
- Série diária dos últimos 30 pregões (último valor de cada dia).
- Vol × preço: correlação entre a variação da vol ATM e o retorno do BOVA11 em baldes de 5 min do dia (mínimo 40
  baldes). Medido: −0,27 a −0,41 conforme o vencimento. Mostre o valor de hoje e a nota "positivo é incomum: vol
  subindo com o mercado subindo".

**Exibição:** 3 números (implícita, realizada, diferença), o gráfico diário com as 3 linhas e o número de vol × preço.

**Aceite:** em 2026-11-11 a realizada anualizada é 13,0% e a prevista 15,8%.

### 6.10 Ajustes nos painéis que mudam de página

- "Pressão do livro" passa a se chamar "Desequilíbrio do livro" e a legenda vira: "volume de compra − venda nas
  ofertas, ponderado pela distância. Anda contra o preço do mesmo intervalo (correlação −0,5) e não antecipa o
  seguinte."
- "Saldo de agressão acumulado" do WIN ganha a nota: "acompanha o preço de perto (correlação 0,9); o que informa é
  quando os dois se separam."
- Em `MACRO_GRAFICOS["brasil_exterior"]`, troque IVVB11 por `SPX_USD`.
- A tabela antiga de correlação sai da página; `macro.correl` continua no JSON de `/api/dados`.

### 6.11 Alertas (Resumo)

Junte numa lista só, do mais grave para o menos:

1. Coleta: fontes paradas (6.1).
2. Regime: os alertas que `regime_live.alertas` já traz.
3. Descolamento: par principal com `|z| ≥ 2` e `n ≥ 120`. Texto: "WIN × dólar em −0,15; o normal é −0,47 (z +2,9)".

Sem alerta: uma linha "Sem alertas".

## 7. Fases

Cada fase termina com os testes passando, a página conferida no navegador em 390 px e em 1.280 px, nos dois temas, e
commit com push.

| Fase | Entrega | Aceite além dos testes |
|---|---|---|
| 0 | `estatico/`, navegação comum, quebra de `dados()` em seções, endpoints `/api/mercado`, `/api/opcoes`, `/api/juros` | `/api/dados` devolve as mesmas chaves e valores de antes; as 4 páginas atuais sem mudança visível |
| 1 | `correlacoes.py`, tabelas `correl_diaria` e `fatores_dia`, `/api/correlacoes`, página `/correlacoes` | valores de 6.3 e 6.4 |
| 2 | `estimador_tryd.py`, `/api/fluxo`, página `/fluxo`, preço em `/players` | valores de 6.6 |
| 3 | `/api/resumo`, `/api/fechamento`, novo `/` | valores de 6.1 e 6.2; Resumo dentro dos limites de 5.1 |
| 4 | Páginas `/mercado`, `/opcoes`, `/juros` com as seções movidas, 6.9 e 6.10 | todo painel da página única antiga existe em alguma página nova, fora a tabela de correlação antiga |
| 5 | Atualizar `CONTEXTO_PARA_BOTS.md` e o docstring de `dashboard.py` | as rotas novas e as 3 tabelas estão documentadas |

## 8. Fora do escopo

- Qualquer previsão de direção. As medições mostram relações no mesmo instante, sem antecipação.
- Contribuição de cada ação para o Ibovespa: precisa dos pesos da carteira, que não estão no banco (download novo).
- Divergência entre preço e saldo de agressão como sinal: sem validação.
- Corrigir o viés do estimador do Tryd: 7 pregões não bastam.
- Trocar o servidor HTTP, o uPlot ou a estrutura do banco existente.
- Mexer nos coletores, além das duas chamadas novas na partida de `coletor_regime.py`.

## 9. O que é frágil nesta especificação

- As amostras são curtas: 9 pregões de saldo por corretora, 7 de placar, 8 de livro e opções. Os limiares de 6.7
  (±0,25) e de 6.11 (z ≥ 2) são escolhas iniciais, não resultado de estudo.
- A referência dos pares com `SPX_USD`, ações e ETFs só tem os pregões de `macro_cotacoes` (35 em 13/11).
- Para o dia em curso a série vem do `mid`; a referência histórica vem do fechamento da barra. A diferença é pequena,
  mas existe.
- O orçamento de tempo de 5.3 não foi medido: é a meta, a conferir na fase 1.

## 10. Adendo de implementação (13/11/2026)

Decisão do dono: implementar como **painel novo**, mantendo o atual para comparação. Isso muda as seções 5 e 7 assim:

- **Páginas novas com prefixo `/v2`:** `/v2` (Resumo), `/v2/correlacoes` e `/v2/fluxo`. A rota `/` e as páginas
  `dashboard.html`, `grafico.html`, `players.html` e `leiloes.html` **não mudam**. A única exceção é um link "Painel novo"
  na navegação delas, que fica para o fim.
- **Navegação do painel novo:** Resumo · Correlações · Fluxo · Painel atual (`/`) · Gráfico WIN · Gráfico WDO · Players ·
  Leilões.
- **Fase 0 reduzida:** `dashboard.dados()` não é dividida. `estatico/comum.css` e `estatico/comum.js` são usados só
  pelas páginas `/v2`. Os endpoints `/api/mercado`, `/api/opcoes` e `/api/juros` não são criados.
- **Adiados:** fase 4 (páginas Mercado, Opções e Juros), o preço em `/players` (6.8, primeiro item) e as mudanças de
  6.10. A vol implícita × realizada (6.9) entra como cartão no Resumo.
- **Banco real:** quem implementa não grava em `dados/book.db`. As funções que gravam (`atualizar`) são testadas em
  banco temporário; a primeira carga no banco real é feita por quem valida.
- **Processos:** quem implementa não encerra nem reinicia o dashboard nem os coletores. A validação no navegador usa
  uma segunda instância na porta 8051.
- **Commits:** feitos por quem valida, ao fim de cada etapa.

Ordem das etapas: A (`correlacoes.py`), B (`estimador_tryd.py`), C (endpoints `/api/correlacoes`, `/api/fluxo`,
`/api/resumo`, `/api/fechamento`), D (arquivos `estatico/` e as três páginas `/v2`).
