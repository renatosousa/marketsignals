# Especificação de Arquitetura e Implementação — Jev-Trader v3 (Cockpit Holístico de Decisão)

**Repositório Alvo:** `https://github.com/renatosousa/marketsignals.git`  
**Documento Base de Referência:** `RESUMO_PROJETO.md` (estado em 18/11/2026)  
**Público-Alvo deste Documento:** Agente de IA Engenheiro Quantitativo / Desenvolvedor Sênior responsável pela implementação da v3.  
**Perfil do Usuário Final:** Trader experiente (opera Daytrade, Opções e Swing Trade na B3 com gestão de risco própria). **Diretriz expressa:** Não incluir avisos de compliance de varejo ("não é recomendação"), bloqueios artificiais de exposição direcional ou textos genéricos. O sistema deve entregar diagnósticos diretos de viés, probabilidades calibradas, assimetria e regiões operacionais objetivas.

---

## 1. Visão Geral e Mudança de Paradigma (v2 para v3)

### 1.1 Diagnóstico do Estado Atual
Os estudos da v1/v2 comprovaram rigorosamente que modelos lineares de antecipação (*lead-lag*) em barras de 1 minuto ou segundos ($r \approx 0$ em defasagem $+1$) não batem o custo transacional no `WIN$` e `WDO$`. Em contrapartida, o sistema construiu uma base valiosa de dados intradiários, volatilidade realizada/implícita ($R^2 \approx 0,75$ fora da amostra para vol horária), superfície de opções, curva DI/DAP e fluxo de corretoras.

Hoje, porém, o projeto sofre de **fragmentação cognitiva e lacunas pré-mercado**:
1. Os dados mostram métricas isoladas, mas não sintetizam a **narrativa macro-estatística dominante** (ex.: saída estrutural de estrangeiro + compra cara de proteção em puts + abertura da curva DI + alta do dólar vs. alívio ou estresse vindo da madrugada na Ásia/Europa).
2. Não há cobertura dos acontecimentos da madrugada (Ásia, Europa, commodities e futuros americanos antes das 08:55 BRT).
3. Falta o elo tático final: traduzir o viés estrutural e intradiário em **Regiões Operacionais Objetivas (Zonas de Confluência)** para **Daytrade**, **Opções** e **Swing Trade**, diferenciando claramente operações **a favor da tendência** e **contra a tendência** (exaustão/reversão).
4. Existem dívidas técnicas críticas documentadas no `RESUMO_PROJETO.md` (SQLite monolítico de 15,8 GB sem retenção, fragilidade manual do DDE do Tryd, coletores de Notícias/Focus/Leilões parados, ausência de posição de futuros D-1 da B3 e bug no `/api/fechamento`).

### 1.2 Arquitetura Alvo da v3
A v3 transforma o pipeline em um **Sistema Institucional de Suporte à Decisão (Cockpit `/v3`)** estruturado em 4 camadas causais:

1. **Camada de Ingestão Resiliente & Pré-Mercado (24x5 / Madrugada + Pregão):** Coleta global (Ásia, Europa, EUA, Commodities, Cesta EM FX), reativação de Notícias/Calendário Econômico, Posicionamento D-1 da B3 em Derivativos (`WIN/IND`, `WDO/DOL`, `DDI`) e *fallback* automático da fita do Tryd.
2. **Motor de Cenário e Viés Multi-Horizonte (`motor_cenario.py`):** Consolida 5 pilares quantitativos em Scores normalizados em $[-100, +100]$ para dois horizontes — **Estrutural (Swing: 5 a 20 dias)** e **Tático (Intraday: Madrugada + Sessão Atual)** — isolando o Risco Brasil idiossincrático ($\epsilon_{\text{BR}}$).
3. **Motor Espacial de Regiões e Playbooks (`motor_regioes.py`):** Agrupa níveis de preço por densidade de confluência (*Volume Profile* multi-período, VWAPs ancoradas, *Gamma/Put/Call Walls*, Bandas Quantílicas Assimétricas) e monitora gatilhos de microestrutura em tempo real quando o preço testa cada região.
4. **Cockpit Visual `/v3` (`painel_v3.py` + `v3_cockpit.html`):** Interface rápida (HTML + CSS Grid + `uPlot`, responsiva para celular via Tailscale e monitor desktop) exibindo o Briefing do Dia, a Régua Tática de Preços (Price Ladder) e as matrizes de execução para Daytrade, Opções e Swing.

---

## 2. Regras Inegociáveis de Engenharia para a IA Implementadora

1. **Zero Vazamento de Futuro (*Strict Causality*):** Todo cálculo histórico, backtest ou calibração (`walk-forward`) em $t$ deve usar exclusivamente dados com `ts_ms` $\le t$ (para dados de mercado) e `ts_disponivel_ms` $\le t$ (para notícias, boletins B3 D+2, posições D-1 e Focus).
2. **Convenção Temporal (`ts_ms`) e Janela Pré-Mercado:**
   - Manter a convenção existente no repositório: `ts_ms` representa **hora de Brasília (BRT) codificada como epoch UTC**, onde `dia_idx = ts_ms // 86_400_000` e `min_dia = (ts_ms % 86_400_000) // 60_000`.
   - **Atenção:** Novos coletores de madrugada/pré-mercado rodarão antes das 09:00 BRT (`min_dia < 540`). Nunca misturar retornos pré-mercado dentro das tabelas/funções de correlação de pregão contínuo da B3 (`545 <= min_dia <= 1075` para futuros; `605 <= min_dia <= 1015` para ações).
3. **Calendário Oficial de Feriados (`calendario_b3.py`):** Substituir `np.busday_count` puro em **todos** os módulos (opções, curvas DI/DAP, janelas móveis) pelo calendário oficial de feriados da ANBIMA já presente no módulo de leilões.
4. **Evolução Controlada de Dependências:** É autorizado e recomendado adicionar ao ambiente Python: `duckdb`, `pyarrow`, `scipy`, `scikit-learn` (para Regressão Quantílica, Ridge e KDE) e `feedparser`/`httpx`. Manter o servidor web leve sem frameworks pesados que quebrem a simplicidade atual, servindo exclusivamente em `127.0.0.1` e no IP privado do Tailscale.
5. **Preservação e Testes:** Não quebrar os 196 testes `unittest` existentes. Cada novo módulo deve ter cobertura de testes unitários verificando unidades, casos de dado faltante e ausência de *lookahead*.

---

## 3. Fase 0: Saneamento de Infraestrutura, Dados e Dívida Técnica

Antes de construir os novos modelos, a IA deve resolver os gargalos operacionais apontados na Seção 9 do `RESUMO_PROJETO.md`.

### 3.1 Arquitetura Hot/Cold Storage e Backup (`manutencao_banco.py`)
O arquivo `dados/book.db` atingiu 15,8 GB devido às tabelas brutas de alta frequência (`tryd_fita`, `opcoes_cotacoes`, `niveis`, `snapshots`).
- **Banco Quente (`dados/book.db` - SQLite WAL):** Mantém todas as tabelas agregadas (`barras_1m`, `agressao_1min`, `opcoes_resumo`, `correl_diaria`, tabelas macro e novas tabelas da v3) por tempo indeterminado, mas **retém apenas os últimos 10 pregões** das tabelas brutas (`tryd_fita`, `opcoes_cotacoes`, `niveis`, `eventos`, `snapshots`, `tryd_saldo`).
- **Arquivo Frio (`dados/historico_parquet/` - Parquet particionado por `ano_mes/tabela`):** Script diário executado às 18:45 BRT exporta os pregões com idade $> 10$ dias úteis para Parquet via `duckdb`/`pyarrow` (compressão `zstd`), valida a contagem de linhas + *checksum*, faz `DELETE` no SQLite e roda `PRAGMA incremental_vacuum`.
- **Agregação Permanente da Fita Antes do Expurgo:** Antes de mover `tryd_fita` para o Parquet frio, consolidar em uma nova tabela leve `tryd_fita_1min` (por ativo e grupo de corretora: volume agressor compra/venda, volume passivo, contagem de lotes grandes $\ge p_{95}$, eventos de absorção e % de captura medida pelos saltos no ID `num` de passo 10).
- **Backup Diário:** Cópia consistente via API `sqlite3.Connection.backup()` das tabelas leves/diárias para `dados/backups/book_core_YYYYMMDD.db.zst` (mantendo últimos 15 backups).

### 3.2 Watchdog Ativo de Saúde (`monitor_saude.py`)
Criar um processo supervisor leve que avalia a cada 30 segundos durante o pregão:
- Idade do último registro (`staleness`) por coletor ativo.
- Qualidade do DDE do Tryd: % de células vazias em `tryd_saldo` e taxa de captura da `tryd_fita` ($\text{captura} = \text{linhas lidas} / (\Delta \text{num} / 10)$).
- Conexão do terminal MT5 e congelamento de preço (`WIN$` sem variação em pregão aberto por $> 90\text{ s}$).
- **Alerta Ativo:** Exibir barra de alerta vermelha no topo do painel `/v3` e disparar notificação via Webhook/Telegram configurável em `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) na primeira falha, com *cooldown* de 15 min por fonte.

### 3.3 Fallback Automático do Tryd via Fita (`fallback_tryd_fita.py`)
Como `tryd_saldo` para quando as janelas perdem exportação DDE, mas `tryd_fita` segue ativa com corretora compradora, vendedora, agressor e `Agente Agressor`:
- Sempre que `tryd_saldo` de `WIN` ou `WDO` estiver atrasado $> 60\text{ s}$, reconstruir automaticamente a série intradiária de saldo líquido e saldo de agressão por grupo de corretora integrando `tryd_fita`, ajustado pelo fator de captura do intervalo e marcando a flag `fonte_saldo = 'FITA_FALLBACK'`.

### 3.4 Correção do Fechamento Oficial (`/api/fechamento` e `consolidar_fechamento.py`)
Corrigir o defeito reportado em 17/11 onde `anterior` coincidia com a abertura e seções vinham vazias:
- Criar a tabela `fechamento_diario (dia_idx, ativo, abertura, maxima, minima, ultimo_liquido, ajuste_b3, vwap_dia, poc_dia, vah_dia, val_dia, vol_realizada_dia, fonte)`.
- Rodar `consolidar_fechamento.py` às 18:35 BRT capturando o último preço do pregão contínuo (ex.: 17:55 para futuros / 16:55 para ações, evitando distorção de leilão ilíquido) **e** o preço de ajuste oficial da B3 (via BDI/MT5). Todos os cálculos de variação diária do dia $T$ devem referenciar explicitamente `fechamento_diario` de $T-1$.

### 3.5 Reativação de Fontes Paradas + Derivativos B3 D-1
1. **`coletor_noticias.py`:** Isolar dependências em ambiente limpo; usar RSS públicos resilientes (Valor, InfoMoney, BCB, Fed, Investing, Reuters/GDELT) + classificador leve (FinBERT-PT-BR local em CPU fora dos núcleos críticos ou API LLM assíncrona opcional com *cache* SQLite).
2. **`coletor_focus.py` e `coletor_leiloes.py`:** Atualizar endpoints OData do BCB para o Focus; para leilões (SISWEB instável), adicionar *scraper/parser* alternativo via comunicados de resultado da ANBIMA / Tesouro Transparente CSV.
3. **NOVO — `posicoes_b3_futuros.py`:** Coletar diariamente (D-1, após 19:30 BRT) nos arquivos/endpoints públicos da B3 ("Posições em Aberto dos Investidores") o estoque líquido e a variação diária de **Investidor Não Residente (Estrangeiro)**, **Institucional Nacional** e **Bancos** em:
   - Futuro de Índice (`IND` + `WIN` convertidos em equivalente IBOV/R\$);
   - Câmbio Sintético Total (`DOL` + `WDO` + `DDI` Cupom Cambial em US\$ milhões);
   - Futuro de DI (`DI1` em DV01 estimado por vértices curtos vs. longos).
   *Justificativa:* O fluxo B3 D+2 atual enxerga apenas ações à vista. O estrangeiro frequentemente vende ou faz *hedge* macro pesado via `WDO/DOL + DDI` e `DI1` dias antes de aparecer no D+2 de ações.

---

## 4. Fase 1: Motor Global Overnight, Madrugada e Pré-Mercado

### 4.1 Coletor Global (`coletor_global.py`)
Executa a partir das **07:00 BRT até as 18:30 BRT** (intervalo de 60 s no pré-mercado, 180 s durante o pregão), gravando na tabela `global_cotacoes`:

| Bloco | Ativos / Séries | Papel no Modelo Holístico |
|---|---|---|
| **Ásia (Madrugada)** | Nikkei 225, Hang Seng, CSI 300, Minério de Ferro (SGX/Dalian proxy), Petróleo Brent | Direção de `VALE3`, `PETR4` e apetite a risco asiático |
| **Europa (Manhã Cedo)** | Euro Stoxx 50, DAX, FTSE 100 | Tom de risco das 05:00 às 09:00 BRT |
| **EUA & Vol Global** | Futuros `ES` (S&P 500), `NQ` (Nasdaq), `ZN`/`US10Y` (Treasury 10Y bps), `DXY`, Estrutura a termo do VIX (`VIX9D`, `VIX`, `VIX3M`), Ouro | Condições financeiras globais e estrutura de medo (`VIX9D > VIX` = estresse agudo) |
| **Cesta Emergentes (EM FX)** | `USD/MXN`, `USD/CLP`, `USD/ZAR`, `EWZ` (pré/pós-mercado EUA) | Separar movimento global do dólar de **Risco Idiossincrático Brasil** |

*(Fontes sugeridas sem custo/baixa fricção: endpoints JSON Yahoo Finance / Stooq / Cboe / FRED / Investing com fallback cruzado e validação de timestamp real de negociação).*

### 4.2 Modelo de Abertura Justa (*Fair Value Pre-Market* — `abertura_justa.py`)
Calculado entre 08:00 e 08:58 BRT para projetar onde `WIN$` e `WDO$` deveriam abrir dada a variação externa acumulada desde as 18:00 BRT de $T-1$:

$$\hat{r}_{\text{WIN, gap}} = \alpha + \beta_{\text{ES}} r_{\text{ES, overnight}} + \beta_{\text{Minerio}} r_{\text{Minerio}} + \beta_{\text{Brent}} r_{\text{Brent}} + \beta_{\text{EM}} r_{\text{EM\_FX}} + \beta_{\text{US10Y}} \Delta y_{\text{US10Y}}$$

$$\hat{r}_{\text{WDO, gap}} = \alpha_w + \gamma_{\text{DXY}} r_{\text{DXY, overnight}} + \gamma_{\text{EM}} r_{\text{EM\_FX}} + \gamma_{\text{Commod}} r_{\text{Commod}}$$

- Os coeficientes são estimados em janela móvel de 60 pregões com regularização Ridge ($\lambda = 1,0$) usando retornos *close-to-open*.
- **Sinal Tático de Abertura (09:00–09:15):** Se a abertura real da B3 desviar mais de $1,5 \sigma_{\text{erro}}$ do *Fair Value* sem notícia doméstica (ex.: gap emocional ou distorção de leilão), o painel destaca **"Distorção de Abertura vs. Pares Globais"** (oportunidade tática de fechamento parcial de gap ou confirmação de choque local).

### 4.3 Isolamento do Risco Brasil Idiossincrático ($\epsilon_{\text{BR}}$)
Durante o pregão, decompor continuamente a variação do `WDO$` e do `DI1F29`:

$$\epsilon_{\text{BR}, t} = r_{\text{WDO}, t} - \left( \hat{\gamma}_{\text{DXY}} r_{\text{DXY}, t} + \hat{\gamma}_{\text{EM}} r_{\text{EM\_FX}, t} \right)$$

- Se `WDO$` sobe $+0,8\%$ e $\epsilon_{\text{BR}} \approx 0$: movimento global de dólar forte (menor contágio estrutural na curva longa de DI).
- Se `WDO$` sobe $+0,8\%$ e $\epsilon_{\text{BR}} > +0,5\%$ acompanhado de abertura em `DI1F33 - DI1F27`: **Estresse Idiossincrático Doméstico (Fiscal/Político)** — alta probabilidade de tendência direcional persistente (baixa em bolsa / alta em dólar e juros).

### 4.4 Calendário Econômico e Sentimento de Notícias (`calendario_macro.py`)
- Manter agenda diária de eventos de Impacto Alto/Médio (Copom, FOMC, IPCA, CPI, Payroll, PIB, Caged, Leilão Tradicional do Tesouro às 11:00–11:30, Ptax de fim de mês, Vencimento de Opções/Futuros).
- **Janela de Bloqueio/Cautela (`alerta_evento`):** 15 min antes até 10 min após divulgação de indicador Tier-1, sinalizando alargamento provável de *spread* e risco de *whipsaw* (violino) nas regiões de daytrade.
- **Score de Notícias (`S_news`):** Média ponderada exponencialmente no tempo ($\tau = 4\text{ horas}$ para intraday, $\tau = 3\text{ dias}$ para estrutural) das manchetes classificadas em `[-1 (Pessimista/Hawkish/Risco Fiscal), 0, +1 (Otimista/Dovish/Fluxo Positivo)]`.

---

## 5. Fase 2: Aperfeiçoamento Estatístico e Motor Holístico de Cenário

### 5.1 Correções Metodológicas (Respostas às Questões da Seção 10 do `RESUMO_PROJETO.md`)

1. **Nowcast de Fluxo Estrangeiro v2 (`nowcast_fluxo_v2.py`):**
   - *Problema atual:* Amostra curta, viés sistemático de $+\text{R\$ } 515\text{ mi}$ no Tryd e persistência forte do sinal oficial D-2.
   - *Solução:* Implementar um **Ensemble Regularizado com Correção Dinâmica de Viés**:
     - Corrigir o estimador do Tryd subtraindo a mediana móvel exponencial do erro observado contra o oficial divulgado até $T-2$: $\tilde{F}_{\text{Tryd}, t} = F_{\text{Tryd}, t} - \text{EWMA}_{10}(F_{\text{Tryd}, t-k} - F_{\text{Oficial}, t-k})$.
     - Combinar via Ridge Bayesiano (com *priors* fixos informativos para evitar *overfitting* enquanto $N < 60$, migrando gradualmente para pesos empíricos conforme $N$ cresce):
       $$\hat{F}_{\text{Est}, t} = w_1 F_{\text{Oficial}, t-2} + w_2 \tilde{F}_{\text{Tryd}, t} + w_3 F_{\text{CestaAgressao}, t} + w_4 \Delta \text{PosFuturos}_{\text{B3}, t-1}$$
       com pesos iniciais *prior* $(0.35, 0.35, 0.15, 0.15)$ validados em *walk-forward* expansivo.

2. **Bandas de Volatilidade Assimétricas Calibradas (`modelo_regime_v3.py`):**
   - *Problema atual:* Assume normalidade simétrica ($\pm 1\sigma, \pm 2\sigma$). Em cenário de pessimismo com `RR25` esticado para puts e `GEX` negativo, a cauda inferior é significativamente mais longa que a superior.
   - *Solução:* Substituir o intervalo gaussiano por **Regressão Quantílica Conformalizada (CQR)** (`sklearn.linear_model.QuantileRegressor` + calibração conformal nos últimos 60 pregões fora da amostra) estimando diretamente os quantis condicionais $q_{0.05}, q_{0.15}, q_{0.50}, q_{0.85}, q_{0.95}$ para os horizontes de **60 minutos** e **até o fechamento do dia**, usando como covariáveis: `vol_prevista_HAR`, `RR25` (skew de opções), `sinal_GEX` e `er` (eficiência direcional).

3. **Correlações Condicionadas ao Regime e Controle de Falsas Descobertas (`correlacoes_v3.py`):**
   - Substituir Pearson puro em 1 min por **Correlação sobre Retornos Winsorizados via Huber** (truncamento suave em $\pm 3,5 \times \text{MAD}$), exibindo Spearman em paralelo.
   - Condicionar a referência histórica ao regime de volatilidade: separar os últimos 120 pregões em 2 baldes (`Vol Baixa/Normal` vs. `Vol Alta`, corte na mediana de `rv_dia`) para calcular média e desvio de referência compatíveis com o dia atual.
   - Aplicar o procedimento de **Benjamini-Hochberg (FDR $\alpha = 0,05$)** sobre os $p$-valores bilaterais de todos os pares monitorados antes de acionar alertas visuais de "Descolamento Anômalo".

### 5.2 Motor de Scores dos 5 Pilares (`motor_cenario.py`)
Todos os indicadores de entrada são padronizados em z-scores causais truncados em $[-3, +3]$ contra janela histórica (60d para estrutural, 20d para intradiário) e mapeados pela função tangente hiperbólica $\text{Score} = 100 \cdot \tanh(z_{\text{composto}} / 1,5) \in [-100, +100]$, onde **valores positivos indicam cenário construtivo para Bolsa Brasil (`WIN$` / `IBOV` alta, `WDO$` / `DI` queda)** e **valores negativos indicam cenário pessimista / aversão a risco (`WIN$` queda, `WDO$` / `DI` alta)**:

1. **Pilar 1 — Macro Doméstico & Curva de Juros (Peso: 25%):**
   - Nível e variação de `DI1F29` e `DI1F33` (sinal invertido: abertura de juros penaliza o score);
   - Inclinação da curva (`DI1F33 - DI1F27`: *bear steepening* = forte alerta fiscal negativo);
   - Inflação implícita (`DI / DAP - 1`) e deriva do Focus IPCA;
   - Concessão/prêmio do último leilão do Tesouro (`leiloes.py`).
2. **Pilar 2 — Fluxo Institucional & Câmbio (Peso: 25%):**
   - Acumulado de 5d e 20d do Fluxo Estrangeiro B3 D+2 + Nowcast Intraday v2;
   - Posição líquida e variação D-1 do Estrangeiro em Futuros (`IND+WIN` a favor; `DOL+WDO+DDI` invertido);
   - Tendência estrutural do `WDO$` (distância para VWAP de 5d/20d) e Risco Idiossincrático $\epsilon_{\text{BR}}$ (invertido).
3. **Pilar 3 — Superfície de Volatilidade & Opções (Peso: 20%):**
   - *Skew* `RR25` de `BOVA11` e Dólar (demanda cara por puts reduz o score direcional e sinaliza medo institucional);
   - Prêmio de Risco de Variância ($VRP = IV_{\text{ATM}} - RV_{20\text{d}}$) e percentil de `IV ATM`;
   - Exposição líquida de Gamma dos Dealers (`GEX`: `GEX < 0` atua como **amplificador de regime**, aumentando a magnitude do viés vigente e a instabilidade intradiária).
4. **Pilar 4 — Pulso Global & Commodities (Peso: 20%):**
   - Ásia + Europa + `ES`/`NQ` + Cesta Commodities IBOV ($0,5 \cdot r_{\text{Minerio}} + 0,5 \cdot r_{\text{Brent}}$) $-$ `DXY` $-$ inclinação `VIX9D / VIX`.
5. **Pilar 5 — Sentimento de Notícias & Eventos (Peso: 10%):**
   - Score `S_news` ponderado no tempo + surpresa de indicadores macro divulgados no dia.

### 5.3 Classificador de Arquétipo do Dia (Síntese Executiva)
Com base no cruzamento entre o **Score Estrutural ($S_{\text{macro}}$)**, o **Score Pré-Mercado/Intraday ($S_{\text{intra}}$)** e o **Regime de Gamma/Vol (`GEX`, `VRP`)**, o sistema classifica automaticamente o pregão em um de **5 Arquétipos Táticos**:

| Arquétipo | Condição Lógica | Orientação Operacional Primária |
|---|---|---|
| **1. Tendência Direcional Alinhada** | $|S_{\text{macro}}| \ge 35$, $\text{sinal}(S_{\text{macro}}) = \text{sinal}(S_{\text{intra}})$, ou `GEX < 0` | Priorizar operações **a favor da tendência** em pullbacks curtos (VWAP / VAH / VAL) e rompimentos confirmados. Evitar contra-tendência precoce. |
| **2. Repique / Alívio Contra-Tendência (*Fade*)** | $|S_{\text{macro}}| \ge 40$ e $\text{sinal}(S_{\text{intra}}) \neq \text{sinal}(S_{\text{macro}})$ (ex.: macro pessimista, mas madrugada positiva) | Aguardar exaustão do movimento de abertura nas **Zonas de Resistência/Suporte Institucional** para entrar **a favor da tendência estrutural** com ótimo risco/retorno. |
| **3. Compressão / Pinning de Gamma** | $|S_{\text{macro}}| < 25$, `GEX > 0`, sem eventos Tier-1 | Mercado travado por *hedging* de dealers. Priorizar **contra-tendência nas extremidades** ($\pm 1,5\sigma$, VAH/VAL) e venda de volatilidade em opções. |
| **4. Exaustão / Risco de *Short Squeeze* ou Capitulação** | $|S_{\text{macro}}| \ge 65$, preço além do quantil $q_{0.05}$ ou $q_{0.95}$ de 5 dias, `IV > p85` e divergência de fluxo | Alerta máximo: tendência madura/esticada. Reduzir tamanho em rompimentos; habilitar setups **contra a tendência** mediante confirmação de absorção/Spring/UTAD. |
| **5. Dia de Evento Binário Macro** | Evento Tier-1 pendente (Copom, FOMC, Payroll, IPCA) | Operar leve antes do dado; aguardar varredura de liquidez pós-evento e reancoragem na VWAP do evento. |

---

## 6. Fase 3: Motor de Regiões Operacionais e Matriz de Playbooks (`motor_regioes.py`)

Este módulo responde diretamente à necessidade central do usuário: **mostrar de forma limpa e objetiva onde operar a favor ou contra a tendência no Daytrade, em Opções e no Swing Trade.**

### 6.1 Conversão Precisa de Escalas entre Ativos Relacionados (`conversor_paridade.py`)
Para projetar paredes de opções (`Call Wall`, `Put Wall`, `Gamma Flip`, `Max Pain`) de `BOVA11` sobre o gráfico/régua do `WIN$` sem erro de juros/carrego:
- Calcular a razão dinâmica intradiária $k_{\text{WIN/BOVA}, t} = \text{mediana}_{5\text{min}}\left(\frac{\text{mid}_{\text{WIN\$}}}{\text{mid}_{\text{BOVA11}}}\right)$ (fallback na abertura: razão do fechamento de $T-1$ ajustada por 1 dia de carrego DI). Assim, um strike $K$ de `BOVA11` equivale exatamente a $K_{\text{WIN}} = K \times k_{\text{WIN/BOVA}, t}$ (arredondado ao lote de 5 pontos).
- Aplicar lógica análoga entre o futuro `DOL` (base das opções de dólar da B3) e o `WDO$` verificando equivalência de vencimento ($W$ vs. $F$).

### 6.2 Algoritmo de Clusterização de Confluência Espacial
Em vez de poluir a tela com 30 linhas soltas, o `motor_regioes.py` coleta todos os níveis candidatos relevantes para o ativo (`WIN$`, `WDO$`, `BOVA11`) e agrupa níveis próximos usando **Kernel Density / Agrupamento Hierárquico** com largura de banda calibrada pela volatilidade ($\Delta_{\text{cluster}} = 0,12 \times \text{ATR}_{\text{diário}}$, tipicamente $\sim 180\text{ a }250\text{ pts}$ no `WIN$` e $\sim 8\text{ a }12\text{ pts}$ no `WDO$`):

**Fontes de Níveis e Pesos Individuais de Confluência ($w_i$):**
1. **Estrutura de Volume e Valor:**
   - `POC`, `VAH`, `VAL` e `Ajuste` de $D-1$ ($w = 20$);
   - `POC`, `VAH`, `VAL` e Nós de Alto Volume (`HVN`) do Perfil Composto de 5 dias e 20 dias ($w = 25$);
   - Máxima e Mínima de $D-1$ e da Semana Anterior ($w = 15$);
   - `VWAP` do Dia, `VWAP` Semanal/Mensal e `VWAP` Ancorada em Eventos Macro ($w = 20$).
2. **Posicionamento de Opções (Dealers):**
   - `Put Wall` e `Call Wall` do vencimento semanal e mensal corrente ($w = 25$);
   - `Gamma Flip` ($w = 30$ — divisor crítico de comportamento de mercado).
3. **Envelope Estatístico Calibrado (CQR):**
   - Quantis do Dia $q_{0.15}$ / $q_{0.85}$ ($w = 15$) e extremos $q_{0.05}$ / $q_{0.95}$ ($w = 25$).
4. **Custo Médio Institucional (Players):**
   - Preço médio (`premed` / VWAP estimada) da posição acumulada do grupo **Estrangeiro** e **Institucional** em $D-1$ e no dia corrente ($w = 20$).

**Score da Região ($0\text{ a }100$):** Soma dos pesos distintos dentro do cluster limitada a $100$:
$$\text{Força da Zona} = \min\left(100, \sum_{i \in \text{cluster}} w_i\right)$$
Apenas as **Top 3 Regiões Acima do Preço Atual (Resistências)** e **Top 3 Regiões Abaixo do Preço Atual (Suportes)** com $\text{Força} \ge 35$ são destacadas na Régua Tática principal (evitando ruído visual).

### 6.3 Classificação Tática e Gatilhos de Microestrutura (`gatilhos_execucao.py`)
Dado o viés do `motor_cenario.py` (ex.: **Tendência de Baixa**, $S_{\text{macro}} = -65$):

#### A) Playbook 1: DAYTRADE (`WIN$` e `WDO$`)
- **Regiões A FAVOR DA TENDÊNCIA (Ex.: Venda em Tendência de Baixa):**
  - *Tipo 1 — Zona de Repique Institucional (Pullback):* Clusters de resistência acima do preço atual contendo `[VWAP Dia / 5d, VAH D-1, Preço Médio Vendedor do Estrangeiro, q_0.85 horário]`.
  - *Tipo 2 — Zona de Aceleração / Rompimento:* Perda de suporte crítico (`VAL D-1` ou `Gamma Flip`) entrando em Vazio de Volume (`LVN`), habilitada **somente** quando `GEX < 0`, eficiência `er > 0.35` e `WDO$` + `DI` confirmam o movimento.
- **Regiões CONTRA A TENDÊNCIA (Ex.: Compra Tática de Exaustão em Tendência de Baixa):**
  - Restrita exclusivamente a clusters de **Exaustão Extrema** ($\text{Força} \ge 60$) contendo obrigatoriamente quantil extremo ($q_{0.05}$ do dia) **mais** `Put Wall` (com gamma local positivo) ou `HVN` de 20 dias.
- **Validador de Gatilho ao Vivo (Semáforo na Região):**
  Quando o preço entra na faixa $[P_{\text{min\_zona}}, P_{\text{max\_zona}}]$, o módulo `gatilhos_execucao.py` lê `snapshots`, `niveis`, `tryd_fita` e `wyckoff.py` e muda o status da zona de `AGUARDANDO TOQUE` para:
  - `EM TESTE (SEM DEFESA AINDA)`: Agressão segue rasgando o nível sem absorção — **não executar contra o fluxo imediato**;
  - `GATILHO CONFIRMADO`: Detectada **Absorção Institucional** (alto volume agredido sem deslocamento de preço + renovação de lote / *iceberg* no `Agente Agressor`), divergência de `OFI` no topo do livro ou evento `Spring` / `UTAD` / `LPS` do `wyckoff.py`.
  - Calcula automaticamente: **Faixa de Entrada**, **Stop Técnico de Invalidação** ($0,25 \times \text{ATR}_{15\text{m}}$ além da borda externa do cluster) e **Alvo Parcial / Final** (próximo HVN / POC / VWAP), exibindo a razão **Risco:Retorno ($R:R$)** líquida de custos.

#### B) Playbook 2: OPÇÕES (`BOVA11`, Ações Líquidas e Dólar)
Cruza **Direção ($S_{\text{macro}}$)** $\times$ **Nível de Volatilidade (`IV Percentile` e $VRP = IV - RV$)** $\times$ **Skew (`RR25`)** $\times$ **Prazo (Semanal vs. Mensal)** para indicar as estruturas mais eficientes e os **Strikes de Referência**:

| Cenário Detectado pelo Sistema | Diagnóstico da Superfície | Estrutura A Favor da Tendência | Estrutura Contra a Tendência (em Suporte/Resistência Extrema) |
|---|---|---|---|
| **Queda + IV Cara ($VRP > 0, \text{IV} > p_{70}$) + Puts Caras (`RR25` p90)** *(Exemplo exato citado pelo usuário)* | Comprar put a seco tem expectativa negativa pelo risco de *vol crush* em qualquer repique e prêmio inflado. | **1. Trava de Baixa com Puts (Débito):** Compra Put ATM/ levemente OTM + Vende Put exatamente no `Put Wall` / $q_{0.05}$ (vende o skew caro).<br>**2. Trava de Baixa com Calls (Crédito):** Vende Call na Zona de Resistência (`Call Wall` / `VAH 20d`) + Compra Call proteção acima. | **Trava de Alta com Puts (Crédito)** fora do dinheiro abaixo do suporte de 20d (apenas quando preço tocar $q_{0.05}$ semanal com absorção), colhendo o prêmio inflado das puts. |
| **Queda + IV Barata ($VRP \le 0, \text{IV} < p_{35}$)** | Proteção subprecificada; mercado complacente antes de aceleração. | **Compra de Put Seca** (delta $-0,35$ a $-0,45$, vencimento mensal) ou **Put Backspread / Ratio** para capturar explosão de convexidade + alta de IV. | Evitar venda de prêmio; se operar repique em suporte forte, usar **Call Seca curta** ou **Trava de Alta com Calls** barata. |
| **Lateral / Gamma Positivo + IV Alta** | `GEX > 0` trava o preço entre `Put Wall` e `Call Wall`; variância realizada abaixo da implícita. | — | **Iron Condor / Borboleta / Calendário** ancorando as pontas vendidas fora do `Put Wall` ($q_{0.10}$) e `Call Wall` ($q_{0.90}$), alvo no `Max Pain` / `POC`. |
| **Tendência de Alta + IV Moderada/Baixa** | Fluxo comprador com puts baratas e calls ganhando tração. | **Trava de Alta com Calls** (alvo no `Call Wall`) ou **Financiamento / Collar** em posição à vista. | **Trava de Baixa com Puts** tática apenas em esticada $> q_{0.95}$ + divergência macro. |

*(O painel calcula para cada estrutura sugerida: Strikes exatos disponíveis na tabela `opcoes_cotacoes`, custo/crédito no mid atual, breakeven, payoff máximo e razão retorno/risco).*

#### C) Playbook 3: SWING TRADE (3 a 15 Pregões — `WIN/IBOV`, `WDO`, `DI1`, `BOVA11`)
- Constrói o mapa em barras diárias e de 60 minutos combinando:
  - Áreas de Valor de 20 e 60 pregões (`POC_20d`, `VAH_20d`, `VAL_20d`);
  - `VWAP` ancorada na rolagem do contrato futuro, no último Copom e no início do mês;
  - Movimento Esperado Semanal ($\pm \text{IV}_{\text{ATM}} \cdot P \cdot \sqrt{5/252}$) e quantis de 5 dias;
  - Gatilhos de invalidação macro (ex.: *"Tese de venda swing em IBOV permanece válida enquanto `DI1F29` fechar acima de $X\%$ a.a., estrangeiro seguir vendedor em 5d e `BOVA11` permanecer abaixo da `VWAP_20d` em R\$ $Y$"*).

---

## 7. Fase 4: Especificação do Cockpit Visual `/v3` (`painel_v3.py` + `v3_cockpit.html`)

### 7.1 Princípios Visuais e Performance
- **Latência Zero na Leitura:** Um processo em background (`atualizador_cockpit.py`) pré-calcula todo o estado consolidado a cada **5 segundos** no pregão (e a cada **60 segundos** no pré-mercado) gravando um JSON enxuto em memória compartilhada / tabela `estado_cockpit_v3` de linha única. O endpoint `/api/v3/cockpit` responde em $< 15\text{ ms}$.
- **Mobile-First via Tailscale & Desktop Widescreen:**
  - No **Desktop ($\ge 1280\text{ px}$):** Grid de 3 colunas em tela única sem necessidade de rolagem excessiva (*Single-Screen Trading Desk*).
  - No **Celular ($< 768\text{ px}$):** Cabeçalho fixo com Viés + Alerta de Zona Próxima, seguido de abas rápidas deslizáveis (`[1. Cenário & Macro]`, `[2. Régua de Regiões]`, `[3. Opções & Swing]`, `[4. Gráfico Quant]`).

### 7.2 Estrutura da Tela `/v3`

1. **Barra Superior Fixa (Status & Síntese Rápida):**
   - Semáforo de Saúde dos Coletores (`MT5 OK | Tryd Fita 98% | Global OK | Últ. atualiz: 2s`).
   - Cronômetro de Próximo Evento Macro (ex.: `Leilão NTN-B em 18m` ou `CPI EUA às 09:30`).
   - Badges de Score: `VIÉS ESTRUTURAL: -68 (BAIXA FORTE)` | `CLIMA INTRADAY: +18 (REPIQUE LEVE)` | `ARQUÉTIPO: REPIQUE PARA VENDA` | `GEX: NEGATIVO (AMPLIFICADOR)`.

2. **Coluna Esquerda — Panorama Holístico (De Onde Viemos & Mundo Agora):**
   - **Card Briefing do Dia (Texto Direto Gerado por Regra/Template):** Resumo de 4 linhas conectando fechamento D-1, madrugada (Ásia/Europa/Commodities), fluxo estrangeiro, curva de juros e volatilidade implícita.
   - **Matriz dos 5 Pilares:** Barras horizontais bipolares ($[-100, +100]$) mostrando contribuição de Macro/Juros, Fluxo/Câmbio (com destaque para $\epsilon_{\text{BR}}$), Vol/Skew, Global/Commodities e Notícias.
   - **Pré-Mercado & Abertura Justa:** Gap projetado vs. abertura real para `WIN$` e `WDO$`.

3. **Coluna Central — Régua Tática de Regiões (*Interactive Price Ladder*):**
   - Seletor rápido de ativo: `[WIN$] [WDO$] [BOVA11]`.
   - Representação vertical ordenada por preço mostrando:
     - As 3 Zonas Superiores de Confluência (com etiqueta `VENDA A FAVOR DA TENDÊNCIA [Força 85]` ou `VENDA CONTRA TENDÊNCIA`, lista das confluências que formam o cluster, distância em pontos/% e status do gatilho ao vivo);
     - Linha central destacada com **Preço Atual**, `VWAP`, `Gamma Flip` e `Eficiência (ER)`;
     - As 3 Zonas Inferiores de Confluência (ex.: `ALVO / COMPRA EXAUSTÃO CONTRA TENDÊNCIA [Força 75] — Put Wall + q0.05 + HVN 20d`).
   - Ao tocar/clicar em qualquer zona, abre o detalhe tático: Entrada sugerida, Stop técnico (ATR), Alvos 1 e 2, $R:R$ e leitura instantânea da microestrutura (`OFI`, absorção, players agredindo).

4. **Coluna Direita — Playbooks de Opções, Swing & Termômetros Internos:**
   - **Aba Opções:** Exibe `IV ATM`, `VRP`, `RR25 (Skew)`, `Gamma Flip`, `Walls` e os **Cards de Estruturas Ótimas** (A Favor e Contra a Tendência) já preenchidos com os strikes líquidos reais de `BOVA11` / Dólar.
   - **Aba Swing Trade:** Zonas estruturais de 5 a 20 dias, tendência da curva DI (`DI1F27/F29/F33` + implícita), estoque estrangeiro em Ações (D+2) + Futuros Índices/Dólar/Cupom (D-1) e condições objetivas de invalidação da tese.
   - **Aba Correlações & Players:** Matriz de correlações condicionadas ao regime (com alerta FDR) e placar em tempo real dos grupos (`Estrangeiro`, `Institucional`, `Bancos`, `XP/BTG`, `Varejo/RLP`).

---

## 8. Contratos de Dados (Novas Tabelas SQLite e Endpoints)

### 8.1 Novas Tabelas em `dados/book.db`

```sql
-- 1. Consolidação limpa do pregão anterior (corrige /api/fechamento)
CREATE TABLE IF NOT EXISTS fechamento_diario (
    dia_idx INTEGER NOT NULL,
    ativo TEXT NOT NULL,
    abertura REAL,
    maxima REAL,
    minima REAL,
    ultimo_liquido REAL NOT NULL,
    ajuste_b3 REAL,
    vwap_dia REAL,
    poc_dia REAL,
    vah_dia REAL,
    val_dia REAL,
    vol_realizada_dia REAL,
    ts_consolidacao_ms INTEGER NOT NULL,
    PRIMARY KEY (dia_idx, ativo)
);

-- 2. Cotações globais overnight, Ásia, Europa, Commodities e EM FX
CREATE TABLE IF NOT EXISTS global_cotacoes (
    ts_ms INTEGER NOT NULL,
    ativo TEXT NOT NULL,
    ultimo REAL NOT NULL,
    variacao_pct_ref_b3 REAL, -- variação desde as 18:00 BRT de D-1
    sessao TEXT,              -- 'ASIA', 'EUROPA', 'US_PRE', 'PREGAO_B3'
    fonte TEXT,
    PRIMARY KEY (ts_ms, ativo)
);

-- 3. Posições em aberto por tipo de investidor nos derivativos B3 (D-1)
CREATE TABLE IF NOT EXISTS b3_derivativos_investidor (
    dia_idx INTEGER NOT NULL,
    ts_disponivel_ms INTEGER NOT NULL,
    familia_contrato TEXT NOT NULL, -- 'IND_TOTAL', 'CAMBIO_SINTETICO', 'DI1_CURTO', 'DI1_LONGO'
    grupo_investidor TEXT NOT NULL, -- 'ESTRANGEIRO', 'INSTITUCIONAL', 'BANCOS', 'PF'
    estoque_liquido REAL NOT NULL,  -- em R$ mi, US$ mi ou DV01 R$ mil
    variacao_dia REAL NOT NULL,
    PRIMARY KEY (dia_idx, familia_contrato, grupo_investidor)
);

-- 4. Histórico por minuto dos Scores Holísticos e Arquétipo do Dia
CREATE TABLE IF NOT EXISTS cenario_scores (
    ts_ms INTEGER NOT NULL PRIMARY KEY,
    score_estrutural REAL NOT NULL,
    score_intraday REAL NOT NULL,
    pilar_macro_juros REAL,
    pilar_fluxo_cambio REAL,
    pilar_vol_opcoes REAL,
    pilar_global_commod REAL,
    pilar_noticias REAL,
    risco_brasil_bps REAL,
    arquetipo_dia TEXT NOT NULL,
    resumo_narrativo TEXT
);

-- 5. Regiões operacionais calculadas (para exibição e auditoria/estudo walk-forward)
CREATE TABLE IF NOT EXISTS regioes_operacionais (
    ts_ms INTEGER NOT NULL,
    ativo TEXT NOT NULL,
    horizonte TEXT NOT NULL,        -- 'DAYTRADE', 'SWING'
    id_zona TEXT NOT NULL,
    preco_min REAL NOT NULL,
    preco_centro REAL NOT NULL,
    preco_max REAL NOT NULL,
    forca_score REAL NOT NULL,      -- 0 a 100
    alinhamento TEXT NOT NULL,      -- 'A_FAVOR_TENDENCIA', 'CONTRA_TENDENCIA'
    direcao_operacao TEXT NOT NULL, -- 'COMPRA', 'VENDA'
    componentes_json TEXT NOT NULL, -- ex: ["Put Wall Semanal", "q0.05 Dia", "HVN 20d"]
    stop_sugerido REAL,
    alvo_1 REAL,
    alvo_2 REAL,
    status_gatilho TEXT,            -- 'DISTANTE', 'PROXIMA', 'EM_TESTE', 'GATILHO_CONFIRMADO'
    PRIMARY KEY (ts_ms, ativo, horizonte, id_zona)
);

-- 6. Saúde operacional dos coletores
CREATE TABLE IF NOT EXISTS saude_coletores (
    coletor TEXT PRIMARY KEY,
    ts_ultimo_dado_ms INTEGER,
    status TEXT NOT NULL,           -- 'OK', 'DEGRADADO', 'PARADO'
    metrica_qualidade REAL,         -- ex: % captura fita ou % células válidas DDE
    detalhe TEXT,
    ts_verificacao_ms INTEGER NOT NULL
);
```

### 8.2 Endpoints HTTP Novos (`dashboard.py` / `painel_v3.py`)
- `GET /v3` -> Renderiza `v3_cockpit.html`.
- `GET /api/v3/cockpit` -> Payload completo consolidado (Scores, Briefing, Global Overnight, Saúde, Termômetros DI/Fluxo/Opções e Regiões Ativas de `WIN$`, `WDO$` e `BOVA11`).
- `GET /api/v3/regioes?ativo=WIN$&horizonte=DAYTRADE` -> Lista detalhada das zonas, histórico de toques no dia e métricas do gatilho de microestrutura em tempo real.
- `GET /api/v3/opcoes_estruturas?ativo=BOVA11` -> Estruturas calculadas com strikes reais do livro atual (`opcoes_cotacoes`), gregas agregadas e relação risco/retorno.
- `GET /api/v3/saude` -> Diagnóstico completo de todos os processos e integridade do banco.

---

## 9. Validação Científica e Estudo de Eficácia das Regiões (`estudo_regioes.py`)

Mantendo a cultura rigorosa de **disciplina contra autoengano** do repositório (Seção 8 do `RESUMO_PROJETO.md`), nenhuma calibração de pesos das zonas ou quantis entra cega:

1. **Validação da Cobertura das Bandas Quantílicas (CQR):**
   - Em *walk-forward* nos 210+ pregões de `barras_1m` (e nos ~40 pregões com `opcoes_resumo`), verificar se o intervalo $[q_{0.15}, q_{0.85}]$ cobre empiricamente $70\% \pm 3\%$ dos retornos e $[q_{0.05}, q_{0.95}]$ cobre $90\% \pm 3\%$, separando pregões de alta e baixa volatilidade.
2. **Estudo de Reação nas Zonas de Confluência (`estudo_regioes.py`):**
   - Medir em histórico *out-of-sample* a **Excursão Favorável Máxima (MFE)** versus **Excursão Adversa Máxima (MAE)** nos 15, 30 e 60 minutos seguintes ao toque em um cluster com $\text{Força} \ge 60$ comparado a níveis aleatórios de mesma distância do preço (benchmark Monte Carlo intradiário), descontando *slippage* e custo operacional ($5\text{ pts}$ no `WIN$`, $0,5\text{ pt}$ no `WDO$`).
   - Medir o ganho incremental de expectativa matemática quando o filtro de confirmação (`gatilhos_execucao.py`: absorção / `OFI` / `Wyckoff`) é exigido antes da entrada contra a tendência.

---

## 10. Roteiro Sequencial de Execução (Ordem de Implementação)

Para garantir entregas incrementais testáveis sem interromper a coleta diária das 08:55 às 18:30 BRT, a IA implementadora deve executar exatamente nesta ordem:

### Etapa 1 — Fundação, Retenção e Confiabilidade (Dias 1–2)
1. Implementar `calendario_b3.py` unificado com feriados ANBIMA e atualizar módulos de opções e curvas.
2. Implementar `manutencao_banco.py` (agregação `tryd_fita_1min`, arquivamento Parquet frio $> 10\text{ d}$ e backup comprimido) + rodar testes em cópia do banco.
3. Implementar `consolidar_fechamento.py` e corrigir `/api/fechamento`.
4. Implementar `fallback_tryd_fita.py` e `monitor_saude.py` (com alerta Telegram/UI).
5. Reativar `coletor_noticias.py`, `coletor_focus.py`, `coletor_leiloes.py` (com fallback ANBIMA) e criar `posicoes_b3_futuros.py` (D-1 de `IND/WIN`, `DOL/WDO/DDI`, `DI1`).

### Etapa 2 — Mundo Overnight e Motores Estatísticos v3 (Dias 3–4)
1. Criar `coletor_global.py` (Ásia, Europa, EUA, Commodities, EM FX) rodando a partir das 07:00 BRT e atualizar `iniciar_coleta.ps1` / `rodar_coleta.py`.
2. Implementar `abertura_justa.py` e o cálculo intradiário do Risco Brasil Idiossincrático ($\epsilon_{\text{BR}}$).
3. Implementar `nowcast_fluxo_v2.py` (correção EWMA de viés do Tryd + ensemble Ridge com futuros D-1), `modelo_regime_v3.py` (CQR assimétrico com skew `RR25` e `GEX`) e `correlacoes_v3.py` (Huber + regime de vol + FDR Benjamini-Hochberg).
4. Criar `motor_cenario.py` gerando os 5 Pilares, o Arquétipo do Dia e o Briefing Narrativo.

### Etapa 3 — Motor de Regiões, Gatilhos e Playbooks (Dias 5–6)
1. Implementar `conversor_paridade.py` (`BOVA11` <-> `WIN$` e `DOL` <-> `WDO$` ao vivo).
2. Implementar `motor_regioes.py` (clusterização espacial de confluências multi-período) e `gatilhos_execucao.py` (integração de absorção na fita/livro, `OFI` e `wyckoff.py`).
3. Implementar o seletor quantitativo de estruturas de Opções (com strikes reais do livro) e zonas de Swing Trade.
4. Rodar `estudo_regioes.py` para calibrar limiares de `Força` e validar $MFE / MAE$ fora da amostra.

### Etapa 4 — Cockpit `/v3` e Integração Final (Dia 7)
1. Implementar `atualizador_cockpit.py`, `painel_v3.py` e `v3_cockpit.html` (Desktop 3-colunas + Mobile Tabs via Tailscale).
2. Validar performance ($< 15\text{ ms}$ na API `/api/v3/cockpit`), responsividade mobile e suíte completa de testes (`unittest`).