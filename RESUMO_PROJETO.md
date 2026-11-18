# Jev-trader: resumo do projeto para revisão externa

Escrito em 18/11/2026 para ser lido por outra IA, que vai analisar o projeto e sugerir melhorias. O documento explica
o que o sistema coleta, como calcula, o que já foi medido, as suposições e os limites. Todos os números são da data
indicada em cada trecho. Quando o documento diz "medido", o número saiu de um script do repositório, e não de opinião.

**O que se pede ao revisor:**

1. Apontar erros de método: estatística, vazamento de informação do futuro (lookahead), suposições frágeis.
2. Sugerir cruzamentos, variáveis ou validações que os dados atuais já permitem.
3. Dizer o que deveria sair do projeto por não ter sinal.
4. Priorizar as melhorias por custo e benefício.
5. Separar o que exige dado novo do que dá para fazer com o que já existe.

**Restrição do dono:** o sistema descreve o mercado e não recomenda operações. Sugestões de "sinal de compra/venda" só
interessam como hipótese a testar fora da amostra, com custo de transação.

---

## 1. O que é o projeto

Pipeline pessoal de dados da B3 (bolsa brasileira), rodando num único PC com Windows 11 (Intel Core 7 240H). O dono é
um trader pessoa física e acompanha o painel pelo celular.

**Foco:**

- mini-índice (WIN);
- mini-dólar (WDO);
- curva de juros DI;
- BOVA11 e suas opções;
- as maiores ações;
- fluxo do investidor estrangeiro;
- contexto macro: juros, inflação implícita, leilões do Tesouro, liquidez do Banco Central e VIX.

**O sistema tem três partes:**

1. **Coletores:** 11 processos Python sob supervisores com reinício automático. Rodam das 08:55 às 18:30, de
   segunda a sexta, e gravam num SQLite em modo WAL (`dados/book.db`, 15,8 GB em 17/11/2026).
2. **Estudos offline:** scripts somente leitura que testam se uma relação existe, com validação fora da amostra.
3. **Dashboard local:** HTTP em Python puro com páginas HTML e uPlot, servido só em 127.0.0.1 e no IP privado do
   Tailscale (nunca 0.0.0.0).
   - Páginas atuais: `/`, `/grafico`, `/players` e `/leiloes`.
   - Páginas novas (`/v2`), de 16/11/2026: Resumo, Correlações e Fluxo.

**Dados técnicos:**

| Item | Valor |
|---|---|
| Código | 77 commits, de 24/09/2026 a 16/11/2026 |
| Tamanho | ~18 mil linhas de Python |
| Testes | 196 testes `unittest`, passando |
| Dependências | `numpy` e `pandas` (sem `scipy` nem `sklearn`; OLS e Spearman feitos à mão) |
| Repositório | Público, só código. A pasta `dados/` fica fora do git |

---

## 2. Convenções que afetam qualquer cálculo

- **Tempo:** `ts_ms` guarda a **hora de Brasília codificada como epoch UTC**. O índice do dia é `ts_ms // 86_400_000`
  (11/11/2026 = 20768) e o minuto do dia é `(ts_ms % 86_400_000) // 60_000`.
  - Os coletores do MT5 estimam a hora do servidor como relógio do PC + offset (o maior offset visto, porque um tick
    nunca chega antes da hora real).
- **Sessões:**
  - Futuros: 09:00 a 18:25. As correlações usam 09:05 a 17:55.
  - Ações e ETFs: pregão contínuo das 10:00 às 16:55. As correlações usam 10:05 a 16:55.
  - Leilões de abertura e fechamento não têm agressor e ficam fora da agressão.
- **Retornos:**
  - Preço: log-retorno.
  - Taxa (DI1, DAP): diferença em pontos-base.
  - Nunca entre dias.
  - Na grade de 1 minuto, um minuto sem cotação repete a última por até 5 minutos e depois fica sem dado.
- **Unidades:**
  - DI1 e DAP são taxas em % a.a., não preços.
  - `opcoes_resumo.atm_iv` está em fração.
  - `rv_dia_*` está em % do dia; para anualizar, multiplica-se por √252.
  - WIN vale R$ 0,20 por ponto e WDO R$ 10 por ponto.
- **Dias úteis:** `np.busday_count`, **sem calendário de feriados**. A exceção são os leilões, que usam os feriados da
  ANBIMA.
- **Séries contínuas:** WIN$ e WDO$ são as séries contínuas da corretora, com emenda entre contratos.
  - A rolagem cai entre dias, então não contamina retornos intradiários.
  - Existe uma tabela `barras_1m_antes_rolagem` com a versão anterior.

---

## 3. Fontes de dados

| Fonte | Como chega | O que traz | Situação em 17/11/2026 |
|---|---|---|---|
| **MetaTrader 5 (Genial)** | Biblioteca `MetaTrader5`, caminho do terminal fixo | Livro do WIN$ e BOVA11 (5 níveis), ticks de negócio (agressão), cotações de ~25 ativos macro, opções mensais do BOVA11, barras de 1 min (histórico) | Em dia |
| **Tryd** (plataforma de corretora) | DDE (`profitchart|TT`) por ctypes, sem Excel | "Saldo por corretora" de WIN, WDO, PETR4, VALE3, ITUB4, BBDC4 e BOVA11; fita de negócios do WIN e WDO com corretora compradora, vendedora, agressor e, desde 16/11, o "Agente Agressor" | **Fita em dia; saldo parado desde 13/11, 18:29** (janelas com exportação DDE desligada, ver 9.1) |
| **Profit (Nelogica)** | Servidor RTD via COM (comtypes), sem Excel | Opções **semanais** do BOVA11 (o MT5 da Genial deixou de trazê-las em nov/2026) | Em dia. O saldo de agressão do Profit foi coletado de 28 a 30/10 e desativado |
| **B3 BDI** | Endpoints públicos não documentados | Fluxo por tipo de investidor (ações, D+2); posição em aberto das opções de BOVA11 (coberto/travado/descoberto) e de dólar (D-1) | Em dia |
| **Cboe** | JSON público atrasado ~15 min | VIX | Em dia |
| **ANBIMA** | Arquivos públicos | Taxas indicativas de títulos públicos, ETTJ (pré, IPCA, inflação implícita, Svensson) | Em dia |
| **Tesouro Transparente / SISWEB** | CSV e planilhas | Taxas do Tesouro Direto; resultados dos leilões do Tesouro desde 2000 | **Leilões parados desde 05/11** (SISWEB fora do ar) |
| **BCB** | OData e SGS | Compromissadas (tomador, 1047), Selic meta e over, Focus (IPCA) | **Focus parado desde 23/10** |
| Notícias (RSS, Fed, BCB, Finnhub, GDELT + FinBERT) | — | Sentimento por minuto | **Desativado desde 20/10** (faltam dependências) |
| IBKR | IB Gateway | Futuros externos (ES, NQ, ZN, CL, GC, DX, EWZ), VIX | **Desativado em 19/10** (não havia antecipação, ver 6.1) |

### 3.1 Tabelas principais (contagem em 17/11/2026)

| Tabela | Linhas | Período | Conteúdo |
|---|---|---|---|
| `snapshots` | 2,27 mi | 24/09 a hoje | 1 por segundo: WIN$ e BOVA11 (bid, ask, mid, spread, imbalance, microprice, agressão do intervalo) |
| `niveis` | 22,7 mi | 24/09 a hoje | 5 níveis de cada lado do livro por snapshot |
| `eventos` | 4,3 mi | 24/09 a hoje | Paredes de volume que surgem ou somem (nível ≥ 2× a mediana) |
| `macro_cotacoes` | 5,6 mi | 25/09 a hoje | Cotações por mudança: WDO$, IND$, DI1F27..F35, DAP, 8 ações, IVVB11, NASD11, GOLD11, BEWZ39, VIX... |
| `barras_1m` | 2,5 mi | 09/01 a hoje | WIN$, WDO$, DI1F27/28/29/33 e outros (MT5; atualizadas na partida das 08:55) |
| `opcoes_cotacoes` | 29 mi | 25/09 a hoje | Séries do BOVA11 por mudança: bid, ask, last, IV, delta, spot implícito |
| `opcoes_resumo` | 592 mil | 25/09 a hoje | A cada 5 s, por vencimento: IV ATM, IV de call e put 25-delta, RR25, BF25, forward, proxy de fluxo |
| `agressao_1min` | 145 mil | 08/10 a hoje | Agressão por minuto de 12 ações + WDO (volume e R$ de compra e venda, maior lote) |
| `features_fluxo_dia` | 26 | 08/10 a 16/11 | Variáveis diárias para estimar o fluxo estrangeiro |
| `fluxo_diario` | 235 | 08/09 a 13/11 | Fluxo oficial da B3 por tipo de investidor (D+2) |
| `tryd_saldo` | 2,4 mi | 30/10 a 13/11 | Foto do saldo por corretora a cada 10 s (futuros) ou 60 s (ações) |
| `tryd_fita` | 54,5 mi | 30/10 a hoje | Negócios do WIN e WDO com corretoras e agressor |
| `tryd_estrangeiro_dia` | 41 | 03/11 a 13/11 | Estimativa diária do estrangeiro pelo Tryd, por ação |
| `regime_live` | 17 mil | 28/09 a hoje | 1 por minuto: regime do WIN, vol prevista, faixa da próxima hora, alertas |
| `correl_diaria` / `fatores_dia` | 10,9 mil / 431 | jan/26 a hoje | Correlação diária de cada par e betas do WIN |
| `b3_posicoes_*`, `b3_dolar_series` | 26 mil / 6,5 mil | 28/10 a hoje | Posição em aberto das opções (D-1) |
| `anbima_*`, `bcb_*`, `focus_ipca`, `tesouro_taxas`, `leilao_*` | — | variado | Macro diário e leilões |
| `lacunas_dados` | 3 | — | Janelas com preço congelado (queda de internet), excluídas dos estudos |

---

## 4. Coleta: métodos e correções importantes

### 4.1 Agressão (quem bateu no preço)

- **Fonte:** ticks de negócio do MT5 (`COPY_TICKS_TRADE`), com o lado tirado das flags `TICK_FLAG_BUY` e
  `TICK_FLAG_SELL`.
- **Correções de 27 e 28/10/2026** (`ticks_trade.py`; o saldo do WIN no painel era ~17× o do Profit):
  1. Tick com as duas flags (sem agressor definido) entrava como compra. Agora fica neutro.
  2. Ticks no mesmo milissegundo eram descartados, menos o primeiro: ~45% do volume do WIN. Agora cada tick é contado
     uma vez.
  3. Ações eram lidas só pelo último tick (~60% do volume, saldo com sinal trocado). Agora se lê o histórico de ticks.
  4. A impressão dos leilões vinha marcada como compra. Agora, uma rajada depois de 1 min sem negócio é tratada como
     leilão, sem agressor.
- O histórico até 13/10 foi recalculado (`recalcular_agressao.py`). Antes disso a agressão do WIN foi anulada.
- **Limite:** o MT5 agrupa várias execuções num tick e marca o tick inteiro com um lado. O Profit deixa ~27% do volume
  do WIN sem agressor (RLP, negócio direto); o MT5 deixa ~2%. Os níveis absolutos de saldo de agressão são diferentes
  entre plataformas.

### 4.2 Guarda de conexão e lacunas

- Em 20/10/2026, ~3 h sem internet: o MT5 seguiu devolvendo o último tick e os coletores gravaram preço congelado.
- **Guarda** (`conexao.py`): um dado só é gravado se o terminal estiver conectado e, no pregão, o tick tiver no máximo N
  segundos.
- Para o passado, `lacunas.py` detecta o WIN parado por ≥ 3 min no pregão e registra a janela em `lacunas_dados`.
- **Outra lacuna conhecida:** 05 e 06/10, quando o PC entrou em suspensão. Só sobraram as barras de 1 min.

### 4.3 Tryd (corretoras)

- **"Saldo por corretora"** (`<ativo>_SAL`): acumulado do dia, completo. Colunas:
  - `qtd = qtdc − qtdv` e `vol = volc − volv` (contratos e R$);
  - `sagr` (saldo de agressão) e `premed`.
  - O fluxo de um intervalo sai da diferença entre fotos.
  - Validação: a linha só vale se `|qtd − (qtdc − qtdv)| < 0,5`. O DDE às vezes devolve células vazias, e a corretora
    "zerava" por uma foto.
- **"Negócios"** (`<ativo>_NEGS`): a janela guarda os N últimos negócios e o coletor lê a cada 125 ms.
  - N foi 31 até 03/11, 100 até 13/11 e é 200 desde 16/11.
  - Com 31 linhas a captura era de 73% em trecho normal e 45% em pico (WIN > 120 negócios/s).
  - A numeração tem passo de 10, então buracos no `num` medem o que faltou.
- **RLP:** na fita, todos os negócios RLP têm a mesma corretora nas duas pontas (varejo cruzado com o provedor de
  liquidez da própria casa). O saldo líquido por grupo seria zero; mede-se só o volume como sinal de varejo.
- **Fragilidade operacional:** a exportação DDE e as colunas de agressão se perdem quando as janelas são reabertas
  (aconteceu em 03, 05, 06 e 13/11). Exige ação manual.

### 4.4 Opções

- **Vol implícita:**
  - Black-Scholes sobre o mid do livro;
  - prazo em dias úteis/252 sem feriados;
  - taxa = Selic meta;
  - subjacente de cada vencimento = **spot implícito pela paridade put-call**, para o skew não herdar erro de carrego.
- **Resumo** por vencimento a cada 5 s: IV ATM, IV 25-delta de call e put, RR25, BF25.
- **Fluxo de opções é proxy:** o MT5 não informa volume nem negócios por série. "Negócio" é a mudança de último preço
  entre duas leituras, e o agressor é inferido por bid/ask.
- A IV calculada difere 2 a 3 pontos da exibida pela corretora. Compara-se a forma, não o nível.
- **Opções de dólar (B3, D-1):** Black-76 sobre o ajuste do futuro DOL do mesmo vencimento.
- **GEX, DEX, gamma flip e walls:** gama e delta de Black-Scholes com a IV ao vivo. Suposição usual de **dealer
  comprado em call e vendido em put**, porque a B3 não informa o lado.

### 4.5 Fluxo oficial do estrangeiro (rótulo)

- A B3 publica compras e vendas por tipo de investidor **acumuladas no mês, com 2 pregões de atraso**. O fluxo de um dia
  é a diferença entre publicações consecutivas.
- Conferido contra a imprensa: 05/10 = +R$ 10,06 bi.
- **Escopo: só o mercado de ações** (à vista, termo, opções, blocos). Não inclui futuros.
- O portal só guarda ~21 publicações, por isso o banco acumula todo dia.

---

## 5. Modelos e cálculos em produção

### 5.1 Regime do WIN (`modelo_regime.py`, `coletor_regime.py`)

Retreinado todo dia às 08:55 com todo o histórico (212 pregões em 17/11).

- **Vol da próxima hora:** OLS de `log(vol realizada dos próximos 60 min)` sobre:
  - horário;
  - vol da última hora e da anterior;
  - vol diária de véspera, 5 e 20 dias (estrutura HAR);
  - EXTRAS: médias móveis exponenciais da vol em blocos de 5 min, vol da última meia hora e vol da mesma janela nos
    5 dias anteriores.

  Avaliado a cada 15 min, de 10:05 a 17:15.

| Modelo | Uso | R² dentro da amostra (17/11) | R² fora da amostra (walk-forward por dia, 182 dias) |
|---|---|---|---|
| `modelo_hora` (com EXTRAS) | Vol da próxima hora | 0,733 | ~0,746 a 0,758 |
| `modelo_hora_base` (sem EXTRAS) | Comparação | 0,705 | ~0,721 |
| `modelo_dia` | Vol do dia, prevista na abertura | 0,40 | ~0,47 |

- **Faixa da próxima hora** em pontos = vol prevista × preço (1 desvio). Calibração medida: ±1σ cobre ~72% e ±2σ ~96%.
- **Rótulo do regime:**
  - vol relativa = vol da última hora / normal do horário;
  - eficiência `er` = |deslocamento| / soma dos |movimentos| em barras de 5 min (0 = lateral, 1 = tendência limpa);
  - exemplos: "vol normal, lateral", "vol alta, tendência".
- **Alertas:**
  - VIX > 20;
  - variação do DI1F29 no dia acima do percentil 95 histórico. Percentis de |variação|: p50 9 bps, p80 18 bps,
    p95 33 bps.
- **O VIX saiu do modelo em 19/10:** com os EXTRAS, piorava o R² fora da amostra (0,758 sem, 0,750 com).
- **Não há modelo de direção.** Os estudos não encontraram previsibilidade (ver 6).

### 5.2 Correlações com referência (`correlacoes.py`, painel `/v2/correlacoes`)

Regras R1 a R10 da `ESPEC_DASHBOARD.md`:

- grade de 1 min com o último preço;
- mínimo de 60 retornos em comum, senão mostra "–" com o motivo;
- IC de 95% por Fisher: `tanh(atanh(r) ± 1,96/√(n−3))`;
- **referência** = média, desvio, p10 e p90 da correlação do pregão inteiro nos 60 pregões anteriores (mínimo 15),
  com `z = (hoje − média)/desvio`;
- destaque só com |z| ≥ 2 e n ≥ 120;
- S&P e Nasdaq **em dólar**: `SPX_USD = IVVB11 / WDO$`. Em reais, o câmbio embutido cancelava a relação.
- **Sem colunas de defasagem:** nenhum ativo antecipa o WIN (6.1).
- **Decomposição do WIN:** OLS de `r_WIN` sobre `[r_WDO, Δ DI1F29 (bps), r_SPX_USD]`, betas estimados nos 20 pregões
  anteriores. O movimento do dia vira contribuição de cada fator mais um resíduo ("próprio"), e o painel mostra o R² do
  dia.
  - R² médio de WIN ~ WDO + DI: ~0,25.
  - R² de treino em 17/11: 0,24. R² do dia: 0,32.
- **Normais medidos (60 pregões, 17/11):**

| Par | Normal |
|---|---|
| WIN × WDO | −0,42 |
| WIN × DI29 | −0,23 |
| WDO × DI29 | +0,30 |
| WIN × SPX_USD | +0,26 |

  Desvio entre pregões ~0,09 a 0,10. Com 60 pontos (1 h) o erro-padrão de uma correlação de −0,47 é ~0,10.

### 5.3 Fluxo do estrangeiro: duas estimativas e um placar

Objetivo: estimar no próprio dia o que a B3 só publica em D+2.

- **Estimador do Tryd** (`estimador_tryd.py`):
  - para cada ação com janela de saldo, pega a última foto válida de cada corretora a partir das 10:00;
  - soma `volc − volv` (R$) das corretoras cujo grupo é "Estrangeiro" (Goldman, Morgan, JP Morgan, UBS, Merrill,
    Citigroup, Scotiabank);
  - a estimativa do dia é a soma das 3 a 5 ações cobertas (PETR4, VALE3, ITUB4, BBDC4, BOVA11).
- **Cesta de agressão** (`features_fluxo.py`): compra − venda agressora em R$ de 12 ações líquidas, das 10:00 às 16:55.
  - Fica fora do placar se a captura de volume for < 90%.
  - Até 27/10 a captura era ~80% e o sinal saía trocado.
- **Placar** (9 pregões com oficial, de 03/11 a 13/11, em R$ mi):

| Data | Oficial | Tryd | Cesta |
|---|---|---|---|
| 13/11 | −85,1 | −118,3 | +47,8 |
| 12/11 | −91,2 | +263,2 | +221,3 (captura 89,9%, fora do resumo) |
| 11/11 | −370,0 | +8,1 | −425,5 |
| 10/11 | −696,8 | −167,0 | +210,8 |
| 09/11 | −85,9 | +220,7 | −192,4 |
| 06/11 | −459,6 | −306,0 | +49,3 |
| 05/11 | −1.734,4 | −835,1 | −555,4 |
| 04/11 | −1.667,4 | −506,2 (3 ações) | −593,2 |
| 03/11 | −390,8 | +490,9 (3 ações) | +438,8 |

| Estimativa | n | Sinal certo | Correlação de postos | Erro absoluto médio | Viés (estimativa − oficial) |
|---|---|---|---|---|---|
| Tryd | 9 | 5/9 | 0,70 | 522 | **+515** (acima do oficial em 8 de 9) |
| Cesta | 9 | 5/9 | 0,52 | 743 | +707 |
| Regra "sinal do oficial do dia anterior" | 25 | 21/25 | – | – | – |

**Leitura:**

- O Tryd ordena bem os dias (postos 0,70 a 0,86), mas tem viés positivo sistemático. A correção do viés não foi feita
  por ser amostra curta.
- O estrangeiro foi vendedor em todos os pregões desde 22/10, o que torna a regra do dia anterior difícil de bater.
- **Hipóteses para o viés do Tryd:**
  - só 5 ações contra o mercado todo;
  - estrangeiros que operam por corretoras fora do grupo "Estrangeiro" (XP, BTG, Itaú);
  - o oficial inclui termo, opções e blocos.
- `estudo_nowcast.py` exige ≥ 60 observações, acerto de sinal > 60% e Spearman > 0,4 estável fora da amostra para uma
  variável virar modelo. Nenhuma chegou lá.

### 5.4 Players por grupo de corretora (`players.py`, `/players`, `/v2/fluxo`)

- **Grupos** (hipótese, não dado): Estrangeiro, Institucional, Varejo, XP, BTG, Genial, Misto, Liquidez, Outros.
  - XP, BTG e Genial têm grupo próprio porque misturam pessoa física e institucional e são grandes demais para sumir
    dentro de outro.
- **Séries do dia por grupo:** saldo acumulado (soma zero entre grupos), saldo de agressão, volume RLP e giro
  (volume / |saldo|, sinal de quem opera dos dois lados).
- **"Quem acompanha o preço":** correlação entre a variação do saldo do grupo e o retorno em baldes de 5 min (mínimo 24
  baldes).
  - Rótulos: "a favor do preço" (≥ +0,25), "contra o preço" (≤ −0,25) e "sem relação". Os limiares foram escolhidos,
    não estimados.
  - Medido em 9 pregões no WIN:

| Grupo | corr 5 min | Saldo final com o sinal do dia |
|---|---|---|
| Estrangeiro | +0,44 | 7 de 9 |
| BTG | −0,48 | 4 de 9 |
| XP | −0,33 | 4 de 9 |

  Nenhum grupo antecipa o preço (defasagem +1: |r| ≤ 0,09).

### 5.5 Gráfico do WIN e WDO (`grafico_quant.py`, `/grafico`)

Tudo é causal: cada barra usa só dados até ela.

- **Preço e volume:** VWAP do dia ±1 e ±2 desvios; perfil de volume (POC, área de valor de 70%); perfil composto de
  5 pregões; níveis do pregão anterior.
- **Projeção do regime:** abertura ±1σ do dia e último ±1σ da próxima hora.
- **Indicadores portados de um repositório NTSL público:** stop ATR dinâmico, velas de tempo maior, OFI de Cont,
  Kukanov e Stoikov no topo do livro, e reversão à média AR(1).
  - A reversão está marcada **experimental, sem validação**.
  - O "Flow VWAP PDE" foi rejeitado.
- **Opções:** movimento implícito pela IV ATM do BOVA11; posição em aberto por strike convertida em pontos do WIN;
  GEX/DEX/gamma flip.
- **Wyckoff objetivo** (`wyckoff.py`): máquina de estados com fases A a E, eventos SC, BC, AR, ST, Spring, UTAD,
  SOS/SOW e LPS/LPSY, pivôs com atraso de confirmação explícito e limiares em `ConfigWyckoff`. Não foi validado como
  previsor.

### 5.6 Juros, inflação e Tesouro

- **Inflação implícita intradiária:** `((1+DI)/(1+DAP) − 1)`, com DI interpolado em flat-forward no vencimento do DAP
  (base 252, sem feriados; o erro quase se cancela na razão).
- **Leilões do Tesouro** (`leiloes.py`), por resultado:
  - absorção = venda / oferta (não é bid-to-cover: o Tesouro não publica as propostas);
  - spread entre taxa de corte e taxa média;
  - concessão contra a taxa ANBIMA da véspera;
  - prêmio contra a ETTJ interpolada;
  - duration e DV01.
  - z-scores só contra observações anteriores (janelas de 1 e 3 anos, mínimo 8), e alertas.
  - Armadilha: "Venda para Bacen" é quantidade **adicional**.
- **Liquidez do BC:** volume do tomador (excesso de liquidez absorvido) e taxa das compromissadas 1047.

---

## 6. Resultados dos estudos (o que foi testado e descartado)

### 6.1 Antecipação (lead-lag)

- **Barras de 1 min, ~40 pregões, sem antecipação:** ES, NQ, VIX, DX, ZN, CL, GC, EWZ e WDO.
  - Correlação no mesmo minuto é forte: EWZ +0,56, WDO −0,50, ES +0,37.
  - No minuto seguinte, ~0. Por exemplo, WDO +1 min = −0,025.
- **Grades de 5, 10, 30 e 60 s, 8 pregões, defasagens ±1 e ±2:** |r| ≤ 0,06 para todos. A exceção é ITUB4 a 5 s
  (+0,10), e ali é o WIN que vem antes.
- **Regra adotada:** só reintroduzir coluna de antecipação com |r| ≥ 0,10 estável em 20+ pregões.

### 6.2 Livro e agressão

- **Imbalance do livro:** correlação **−0,50** com o retorno do mesmo intervalo (anda contra o preço) e +0,01 a +0,03
  com o seguinte.
- **Saldo de agressão acumulado do WIN:** correlação 0,81 a 0,91 com o retorno do mesmo intervalo. Repete o preço.
- Poder preditivo pequeno, que **não paga o spread** (~5 pontos).
- **Cesta de agressão × índice:** +0,40 em 5 min, sem antecipação.

### 6.3 Fatores e modelos alternativos

- **Alpha101 preço-volume:**
  - Não funciona no WIN.
  - Entre ações da B3 tem IC pequeno e estável (h = 5), mas o giro de ~91% não sobrevive a custos.
  - O estudo tem viés de sobrevivência (lista de ações atual) e barras não ajustadas por proventos.
- **Timestral** (série temporal zero-shot da Mistral) como previsor extra de vol: +0,008 a +0,0095 de R², abaixo do
  critério de +0,01. Não entrou.
- **Vol implícita × preço do BOVA11:** −0,27 a −0,41 em baldes de 5 min (normal: vol sobe quando o mercado cai).

### 6.4 Notícias

O estudo está pronto, com critério de R² incremental ≥ +0,01 para a vol da próxima hora e estabilidade em 2 de 3
terços. O coletor está desligado desde 20/10, então a amostra continua insuficiente (< 40 pregões).

### 6.5 Fluxo estrangeiro × mercado

- O fluxo do dia T só é conhecido em T+2.
- A relação no mesmo dia é descritiva. Para previsão, só vale a defasagem 2.
- O outlier de 05/10 (+R$ 10 bi, WIN +7,6%, dia seguinte ao 1º turno) domina qualquer correlação.

---

## 7. Suposições explícitas

1. **Corretora como proxy do tipo de investidor.** Bancos globais = estrangeiro. Esta é a suposição mais frágil do
   módulo de fluxo.
2. **Dealer comprado em call e vendido em put** no GEX.
3. **Selic meta como taxa livre de risco** e dias úteis sem feriados nas opções.
4. **A cesta de 12 ações representa o fluxo estrangeiro** nas ações.
5. **O fechamento da barra (histórico) e o `mid` (dia corrente) são intercambiáveis** nas correlações. A diferença é
   pequena, mas existe.
6. **Betas dos últimos 20 pregões valem para hoje** na decomposição.
7. **A normal de 60 pregões é estável.** Não há ajuste por regime: em dia de vol alta a correlação muda.
8. **O VIX atrasado ~15 min serve como contexto lento.**
9. **Limiares escolhidos, não estimados:** ±0,25 (players), |z| ≥ 2 e n ≥ 120 (alertas), captura ≥ 90% (cesta), 3 min
   parado (lacuna).

---

## 8. Pontos fortes

- **Disciplina contra autoengano:**
  - validação fora da amostra (walk-forward por dia inteiro) com critério de aprovação fixado antes;
  - comparação contra uma base ingênua (persistência da vol, regra do dia anterior);
  - resultados negativos documentados e aplicados (colunas removidas, VIX tirado do modelo).
- **Causalidade:** todo indicador usa só o passado. Notícias usam a hora de disponibilidade, não a de publicação.
  Z-scores de leilões ignoram observações do mesmo dia.
- **Rótulo oficial acumulado** (o portal apaga após ~21 dias) e um **placar** que mede as estimativas contra ele.
- **Fontes cruzadas:** MT5, Tryd, Profit e B3 para a mesma grandeza (agressão, fluxo). Foi assim que se descobriram os
  erros de agressão.
- **Robustez operacional:**
  - supervisores com reinício;
  - guarda de conexão;
  - detecção de lacunas;
  - processos fora do modo de eficiência do Windows (sem isso, ficavam ~5× mais lentos);
  - caminho fixo do terminal MT5 (um MT5 genérico chegou a abrir conta demo);
  - coletores idempotentes.
- **Documentação no código:** cada módulo explica a fonte, as unidades e as armadilhas. Há 196 testes.

## 9. Pontos fracos e limitações

### 9.1 Dados

- **Dependência de interação manual com plataformas gráficas:**
  - o saldo do Tryd depende de janelas abertas com DDE ligado (parado de 13/11 a pelo menos 17/11);
  - o RTD do Profit depende do Profit aberto e logado.
- **Amostras curtas:**

| Base | Pregões |
|---|---|
| Saldo por corretora | 9 |
| Placar | 9 |
| Livro de ofertas e opções | ~37 |
| `macro_cotacoes` (referência de SPX_USD e ações) | ~37 |
| `barras_1m` | ~210 |

  Quase toda conclusão sobre fluxo ou corretoras é preliminar.
- **Captura incompleta:**
  - a fita do Tryd perdia 25 a 55% dos negócios (a janela de 200 linhas desde 16/11 ainda não foi medida);
  - o fluxo de opções é proxy;
  - os ticks do MT5 agregam execuções.
- **Período dominado por um regime:** eleição em outubro e estrangeiro vendedor contínuo desde 22/10. Os resultados
  podem não generalizar.
- **Fontes paradas:** Focus (23/10), leilões (05/11), notícias (20/10).
- **Endpoints da B3 não documentados**, que podem mudar sem aviso.
- **Barras do MT5 não ajustadas por proventos** e lista de ações com viés de sobrevivência nos estudos de ações.
- **Fechamento no `/api/fechamento` em 17/11:**
  - as seções de IND, faixas, DIs e players vieram vazias;
  - a "anterior" do WIN e do WDO coincidiu com a abertura;
  - possível defeito na referência de fechamento anterior, a investigar.

### 9.2 Método

- **Sem modelo de direção, por escolha** (os estudos não acharam sinal). O valor prático do sistema é contexto,
  volatilidade e fluxo, não "sinal".
- **Estimador do Tryd com viés grande e sem correção**, e mapa de corretoras não calibrado. O ideal seria estimar pesos
  por corretora contra o oficial, mas 9 observações não bastam.
- **OLS simples sem regularização**, betas de janela fixa (20 pregões) e referência de janela fixa (60), sem teste de
  sensibilidade a essas escolhas.
- **A faixa de vol assume normalidade do retorno de 60 min.** A cobertura empírica (72% e 96%) é próxima, mas não foi
  verificada por regime.
- **Correlação de Pearson em minutos com caudas grossas:** um único minuto extremo pesa muito. Não há versão robusta
  (Spearman, winsorização) no painel.
- **Testes múltiplos:** muitas correlações e pares olhados todo dia. Um |z| ≥ 2 aparece por acaso em ~5% dos pares
  por dia.

### 9.3 Infraestrutura

- **Um único PC e um único SQLite de 15,8 GB, sem rotina de backup no repositório.**
  - Tabelas que mais crescem: `tryd_fita` (54 mi linhas), `opcoes_cotacoes` (29 mi), `niveis` (23 mi).
  - Não há política de retenção nem compactação.
- **Suspensão, queda de internet ou atualização do Windows apagam pregões.** Só as barras de 1 min se recuperam por
  backfill.
- **Sem alerta ativo:** o dono descobre problemas de coleta perguntando ou olhando o painel.
- **Python sem `scipy`/`sklearn`:** estatística feita à mão, com mais chance de erro sutil. Há testes, mas não contra
  uma referência independente.

---

## 10. Perguntas abertas para o revisor

1. Como estimar o fluxo estrangeiro do dia com poucos rótulos? Possíveis caminhos: modelo bayesiano com prior no mapa de
   corretoras, ponderação por corretora, combinação Tryd + cesta + regra do dia anterior. Como evitar overfitting com
   < 60 observações?
2. Vale corrigir o viés do Tryd (média móvel do erro) já, ou é cedo?
3. A referência de 60 pregões deveria ser condicionada ao regime de vol? Que método é robusto com ~200 pregões?
4. Que tratamento de testes múltiplos usar nos alertas de descolamento?
5. O OFI e a reversão à média do gráfico merecem um estudo formal? Com que desenho (horizonte, custo, walk-forward)?
6. Que validação faria a "faixa da próxima hora" virar um intervalo calibrado de verdade (quantis condicionais,
   regressão quantílica)?
7. A fita com corretora + agressor + "Agente Agressor" (desde 16/11) permite que análises que hoje não são feitas? Por
   exemplo: tamanho de lote por grupo, iceberg, quem agride em rompimentos.
8. Retenção e armazenamento: o que agregar e descartar (`niveis`, `opcoes_cotacoes`, `tryd_fita`) sem perder o que os
   estudos usam?
9. Que métricas de qualidade da coleta deveriam ser gravadas todo dia para detectar regressões? Exemplos: captura da
   fita, % de células DDE vazias, idade dos dados.

---

## 11. Mapa dos arquivos

| Grupo | Arquivos |
|---|---|
| Supervisão | `iniciar_coleta.ps1`, `rodar_coleta.py`, `executar.py`, `desempenho.py`, `vigiar_dashboard.ps1` |
| Coletores ao vivo | `coletor_book.py`, `coletor_macro.py`, `coletor_opcoes.py`, `coletor_opcoes_rtd.py`, `coletor_agressao.py`, `coletor_tryd.py` (+ `dde_tryd.py`), `coletor_vix.py`, `coletor_regime.py`, `coletor_bcb.py`, `coletor_leiloes.py` |
| Coletores diários e batch | `coletor_anbima.py`, `coletor_tesouro.py`, `coletor_focus.py`, `fluxo_estrangeiro.py`, `posicoes_b3_csv.py`, `posicoes_b3_dolar.py`, `backfill_barras.py`, `features_fluxo.py`, `recalcular_agressao.py`, `lacunas.py` |
| Desativados | `coletor_ibkr.py`, `coletor_profit_rtd.py`, `coletor_noticias.py`, `posicoes_b3.py` |
| Infra comum | `banco.py`, `relogio.py`, `conexao.py`, `terminal_mt5.py`, `ticks_trade.py`, `curvas.py`, `regimes.py` |
| Modelos e cálculo | `modelo_regime.py`, `correlacoes.py`, `estimador_tryd.py`, `players.py`, `grafico_quant.py`, `wyckoff.py`, `leiloes.py`, `opcoes_manual.py`, `painel_v2.py` |
| Estudos | `estudo_leadlag.py`, `estudo_regime.py`, `estudo_timestral.py`, `estudo_correlacoes.py`, `estudo_fluxo.py`, `estudo_nowcast.py`, `estudo_fator_pv.py`, `estudo_fator_b3.py`, `estudo_noticias.py`, `analisar_sinais.py` |
| Painel | `dashboard.py`, `dashboard.html`, `grafico.html`, `players.html`, `leiloes.html`, `v2_*.html`, `estatico/` |
| Documentos | `CONTEXTO_PARA_BOTS.md` (tabelas e CSVs para bots, de 07/10, parcialmente desatualizado), `ESPEC_DASHBOARD.md` (especificação do painel novo, com medições de 13/11), `README_LEILOES.md`, `README_TESOURO.md`, `README_NOTICIAS.md` |

Os dados podem ser exportados em CSV compacto com `exportar_para_bot.py` (agregados de 1 min e tabelas pequenas), caso
o revisor precise de amostras.
