# Jev-trader: contexto para bots e assistentes (briefing)

Atualizado em 07/10/2026. Este arquivo não contém credenciais. O repositório de código é público
(github.com/renatosousa/marketsignals); **os dados não estão nele** (a pasta `dados/` é ignorada pelo git).

## 1. Prompt pronto para colar no bot (Grok ou outro)

```text
Você é um assistente de análise de mercado para um trader pessoa física na B3 (Brasil).
Foco: mini-índice (WIN), mini-dólar (WDO), BOVA11 e suas opções, curva de DI e contexto macro.
Você NÃO dá recomendação de investimento; descreve o que os dados mostram, separa fato observado de
inferência e diz quando algo não dá para concluir.

Fontes: um pipeline próprio que coleta dados do MT5 (Genial) e da IBKR e grava num SQLite. Você NÃO
tem acesso direto ao banco: trabalhe só com os CSVs que eu anexar (descritos abaixo) e com o que eu colar.
Se faltar um dado, peça a fatia exata (tabela, ativo, período) em vez de supor.

Convenções:
- Todo horário é de Brasília (a coluna `hora`, formato AAAA-MM-DD HH:MM:SS). Pregão regular: 09:00 a 18:25.
- WIN$ e WDO$ são séries contínuas do contrato mais líquido (há emenda entre contratos).
- Colunas DI1F27, DI1F29, DI1F33 são TAXAS (% a.a.), não preços; variação em pontos-base (bps).
- IB:* são dados da IBKR; só existem até 28/09 (VIX até 30/09). Cotações da IBKR hoje chegam atrasadas.
- O símbolo B3 do ativo-objeto das opções do BOVA11 aparece como "BOVA" nos arquivos da B3.
- Opções: sufixo W1/W2/W4 = série semanal; sem sufixo = mensal. "Coberto/Travado/Descoberto" = posição em
  aberto; descoberto = lançamento a descoberto (naked); travado = posições travadas/estruturadas.
- Volatilidade implícita (nosso coletor) difere 2 a 3 pontos da exibida pela corretora; compare formato, não nível.

O que já foi validado (não refaça do zero, mas questione se os dados novos contradisserem):
1. Nenhum ativo externo antecipa o WIN em barras de 1 min (ES, NQ, VIX, DX, ZN, CL, GC, EWZ, WDO).
   Correlação no mesmo minuto é forte (EWZ +0,56; WDO -0,50; ES +0,37); no minuto seguinte ~0.
2. Macro serve como CONTEXTO de volatilidade, não de direção: a vol do WIN é prevista sobretudo pelo
   próprio histórico (R2 fora da amostra ~0,47 para o dia, ~0,75 para a próxima hora); o VIX acrescenta pouco.
   Direção e tendência não foram previsíveis.
3. Imbalance do livro e fluxo de agressão têm poder preditivo pequeno e não pagam o custo (spread ~5 pts).
4. Fator preço-volume (Alpha101) não funciona no WIN; entre ações da B3 tem IC pequeno e estável (h=5),
   mas o giro (~91%) não sobrevive a custos.
5. 1º turno em 04/10/2026. Em 05/10 o WIN abriu com gap de +9,2% e fechou +7,6%; WDO -4,4%;
   DI1F29 -104 bps; DI1F33 -123 bps; vol implícita ATM do vencimento de 16/10 foi de ~36% (02/10) para ~20%.

Lacunas conhecidas (cite-as quando afetarem a resposta):
- 05/10 e 06/10: o PC entrou em suspensão; livro, agressão, opções e macro desses dois dias estão quase
  vazios. Para esses dias só existem as barras de 1 min de WIN, WDO e DI (recuperadas do MT5).
- Agressão do BOVA11 é estimada pelo tick ao vivo e pode subestimar o volume. O MT5 não informa nº de ordens.
- Fluxo de opções é um proxy (detecta mudança de último preço); não há volume real por série no MT5.
- Posição em aberto (coberto/travado/descoberto) só existe nas grades que eu colo manualmente.

Fluxo estrangeiro (B3): a série oficial é do mercado de AÇÕES (à vista, termo, opções, blocos), não de futuros;
sai com 2 pregões de atraso e só ~21 publicações ficam disponíveis, por isso acumulamos todo dia. O MT5 não
identifica corretora nem tipo de investidor: o fluxo do dia é ESTIMADO por proxy (agressão das maiores ações e do
WDO, corrigida pelo volume real do minuto) e só vale depois de calibrado com o rótulo oficial (meta: >= 60 dias).
Até lá, trate a estimativa como hipótese, não como dado. O coletor de agressão captura ~53% do volume e ~61% dos
negócios (varreduras grandes se perdem), então os níveis absolutos não são confiáveis; vale a proporção compra/venda.

Estilo: português do Brasil, direto, tabelas quando ajudarem, números com a unidade, e sempre diga o período
e o arquivo de onde veio cada afirmação.
```

## 2. Como alimentar o bot

1. **Cole o prompt acima** como instrução inicial do bot.
2. **Gere os CSVs** (o bot não enxerga o seu PC, e o `book.db` tem ~2 GB, grande demais para anexar):

   ```powershell
   cd C:\desenv\Jev-trader
   .venv\Scripts\python.exe exportar_para_bot.py --desde 2026-09-24
   ```

   Saem em `C:\desenv\Jev-trader\dados\export_bot\` (~6,5 MB no total). Anexe os arquivos ao bot e rode de novo
   sempre que quiser dados frescos.
3. **Grades de opções novas:** continue colando no dashboard (`http://localhost:8050`, seção "Posição em aberto")
   e cole o mesmo texto para o bot.
4. **Nunca envie** login/senha/conta da IBKR, nem o conteúdo do Gateway. O banco e os CSVs não têm credenciais.

Se o bot rodar na mesma máquina, ele pode ler o dashboard em JSON: `http://127.0.0.1:8050/api/dados?janela=3600`
(janela em segundos ou `pregao`) e `http://127.0.0.1:8050/api/opcoes-manual`. Só funciona localmente.

### Dicionário dos CSVs (`dados/export_bot/`)

| Arquivo | Conteúdo | Colunas principais |
|---|---|---|
| `barras_1m.csv` | Barras de 1 min (hora de abertura da barra) | `hora, symbol, open, high, low, close, volume` |
| `livro_agressao_1min.csv` | WIN$ e BOVA11 por minuto | `mid, spread, imbalance_pond/near, vol_compra_agr, vol_venda_agr, delta, saldo_acum` |
| `macro_1min.csv` | Último `mid` por minuto de dólar, DI, ações, ETFs | `hora, symbol, mid` |
| `opcoes_resumo_1min.csv` | Opções do BOVA11 por vencimento | `atm_iv, iv_call25, iv_put25, rr25, bf25, spot, forward, neg_*` |
| `regime_1min.csv` | Regime do WIN (modelo próprio) | `rv_60, rv_rel, er_60, rotulo, rv_prev_1h, faixa_1h_pts, rv_dia_prev/real, vix, di29_var_bps, alertas` |
| `fluxo_investidores_dia.csv` | Fluxo oficial da B3 por tipo de investidor, por dia de referência (D+2) | `data_ref, tipo, compras_mil, vendas_mil, saldo_mil` (R$ mil; `Investidor Estrangeiro` é o foco) |
| `agressao_1min.csv` | Agressão por minuto de 12 ações líquidas + WDO | `symbol, n, vol_compra, vol_venda, vol_neutro, fin_compra, fin_venda, maior_lote, preco` |
| `features_fluxo_dia.csv` | Variáveis diárias para estimar o fluxo estrangeiro | `cesta_fin_liq_esc_mi, cesta_liq_esc_pct, captura_vol, wdo_liq_pct, win_ret_pct, ...` |
| `posicao_aberta_series.csv` | Grades coladas (por série) | `coberto, travado, descoberto, titulares, lancadores, vol_impl_pct, delta, gamma...` |
| `posicao_aberta_resumo.csv` | Resumo por vencimento | `atm_iv, rr25, strike_ima, concentracao_ima, suporte, resistencia` |

Observações: `saldo_acum` zera no início de cada sessão do coletor; `delta` = agressão compradora menos vendedora;
`imbalance_*` vai de -1 (vendedor) a +1 (comprador); `rv_*` é volatilidade realizada em %.

## 3. Onde está cada coisa

Raiz do projeto: `C:\desenv\Jev-trader\` (Python em `.venv\Scripts\python.exe`).

| O quê | Caminho |
|---|---|
| **Banco principal (SQLite, WAL)** | `C:\desenv\Jev-trader\dados\book.db` (~2 GB; `-wal`/`-shm` ao lado) |
| Modelo de regime treinado | `dados\modelo_regime.json` (retreinado todo dia na partida do coletor de regime) |
| Estudos exportados | `dados\leadlag_1m.csv`, `regime_eod.csv`, `regime_intraday.csv`, `b3_d1.csv`, `fator_b3_quintis.csv` |
| Logs | `dados\supervisor_*.log` (e `.err`), `dados\dashboard.log`, `dados\vigia_dashboard.log` |
| Dashboard | `dashboard.py` + `dashboard.html` → `http://localhost:8050` |
| Exportação para bots | `exportar_para_bot.py` → `dados\export_bot\` |
| Código no GitHub | https://github.com/renatosousa/marketsignals (público; só código) |

Coleta (supervisores com reinício automático, 08:55 a 18:30, seg a sex):
`iniciar_coleta.ps1` sobe `rodar_coleta.py` para WIN$, BOVA11 (livro + agressão pelo tick), opções do BOVA11,
macro, IBKR e regime; `vigiar_dashboard.ps1` mantém o dashboard no ar. A tarefa agendada do Windows
`JevTrader-Coleta` roda no login e todo dia às 08:50.

Scripts de análise: `estudo_leadlag.py`, `estudo_regime.py`, `estudo_fator_pv.py`, `estudo_fator_b3.py`,
`analisar_sinais.py`. Utilitários: `verificar_coleta.py`, `tamanho_banco.py`, `backfill_barras.py`,
`opcoes_manual.py` (parser da grade colada), `posicoes_b3.py` (cliente da API da B3, **não usado em produção**).

## 4. Tabelas do `book.db`

Todas as colunas `ts_ms` são em milissegundos e guardam o **relógio de Brasília codificado como epoch UTC**:
`datetime.utcfromtimestamp(ts_ms/1000)` já dá o horário local. Em `barras_1m`, `ts_ms` é a abertura da barra.

| Tabela | Linhas | Cobertura | O que tem |
|---|---|---|---|
| `snapshots` | 437 mil | 24/09 a 07/10 | 1 por segundo por ativo (WIN$, BOVA11): bid/ask/mid/spread, volumes totais e próximos, `imbalance_total/near/pond`, `microprice`, agressão (`n_trades, vol_compra_agr, vol_venda_agr, delta, saldo_acum`) |
| `niveis` | 4,4 mi | 24/09 a 07/10 | 5 níveis de cada lado do livro por snapshot (`lado, dist, preco, volume`) |
| `eventos` | 982 mil | 24/09 a 07/10 | `PAREDE_NOVA` / `PAREDE_REMOVIDA` (nível com volume ≥ 2× a mediana do livro) |
| `macro_cotacoes` | 935 mil | 25/09 a 07/10 | Cotações bid/ask/last/mid: WDO$, IND$, DI1F27/28/29/31/33/35, ITUB4, BBAS3, VALE3, PETR4, PRIO3, SUZB3, SMAL11, IVVB11, NASD11, XINA11, BEWZ39, GOLD11, BIT$, IB:VIX |
| `opcoes_cotacoes` | 7,4 mi | 25/09 a 07/10 | Por mudança de cotação, séries do BOVA11 (3 vencimentos, ±8% do spot): bid, ask, last, `iv`, `delta`, `spot` implícito por paridade |
| `opcoes_resumo` | 120 mil | 25/09 a 07/10 | A cada 5 s por vencimento: `atm_iv, iv_call25, iv_put25, rr25, bf25`, forward, taxa (Selic), proxies de fluxo |
| `regime_live` | 3 mil | 28/09 a 07/10 | 1 por minuto: regime, faixa esperada da próxima hora em pontos, vol do dia prevista x realizada, VIX, DI, alertas (JSON) |
| `barras_1m` | 2,3 mi | 09/01 a 07/10 | WIN$, WDO$, DI1F27/29/33 (MT5, atualizadas); IB:ES/NQ/ZN/CL/GC/DX/EWZ (até 28/09) e IB:VIX (até 30/09), futuros emendados por vencimento |
| `fluxo_acumulado` / `fluxo_diario` | ~22 / ~100 | 08/09 em diante | Publicações cruas da B3 (acumulado do mês, por tipo de investidor) e o fluxo diário derivado pela diferença |
| `agressao_1min` | cresce ~5 mil por pregão | 08/10 em diante | Agressão por minuto de 12 ações + WDO (variáveis para estimar o fluxo estrangeiro) |
| `features_fluxo_dia` | 1 por pregão | 08/10 em diante | Variáveis diárias derivadas da agressão e das barras |
| `opcoes_manual_series` / `_resumo` | 148 / 7 | grades de 28/09, 29/09 e 06/10 | Grades coladas à mão, com posição em aberto por série |

Para consultar sem atrapalhar a coleta, abra sempre em modo leitura:

```python
import sqlite3, pandas as pd
con = sqlite3.connect(r"file:C:\desenv\Jev-trader\dados\book.db?mode=ro", uri=True)
df = pd.read_sql("SELECT ts_ms, mid, delta, saldo_acum FROM snapshots WHERE symbol='WIN$' AND ts_ms >= ?",
                 con, params=(int(pd.Timestamp('2026-10-07', tz='UTC').timestamp() * 1000),))
df.index = pd.to_datetime(df.ts_ms, unit='ms')   # já é horário de Brasília
```

## 5. Limitações e armadilhas

- **Lacuna de 05 e 06/10** (PC em suspensão), detalhada no prompt. A recomendação de energia é desligar a suspensão
  durante o pregão (`powercfg /change standby-timeout-ac 0`, executado por você).
- **IBKR:** sem assinatura de dados, as cotações chegam com 10 a 15 minutos de atraso e o coletor descarta tudo, exceto o VIX.
  O estudo de antecipação mostrou que ativos externos não antecipam o WIN em 1 minuto, então não pagamos a assinatura.
- **Times & trades de ações:** a Genial não entrega histórico de ticks de ações (a chamada trava o terminal), por isso o BOVA11
  usa o tick ao vivo, que pode perder negócios em rajada.
- **WIN$ ↔ BOVA11:** para converter níveis, 1 R$ de BOVA11 ≈ 1.019 pontos do WIN (fator de 06/10; a base varia).
- **Posição em aberto da B3:** a API pública (`arquivos.b3.com.br/bdi`) devolve dados incompletos para clientes HTTP simples;
  por isso a coleta é manual (colar a grade no dashboard).
- **Privacidade:** o repositório é público e os commits trazem o e-mail do git do autor.
