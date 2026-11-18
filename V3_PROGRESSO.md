# Jev-trader v3: progresso da implementação

Especificação: [ESPEC_V3.md](ESPEC_V3.md). Implementação delegada ao opencode (DeepSeek v4.1 flash, grátis e depois pelo plano Go) e
validada pelo Claude: código lido, suíte de testes rodada e números conferidos contra o banco. Cada tarefa validada vira
um commit no `main`.

## Decisões do dono (18/11/2026)

- **Retenção:** o código e os testes ficam prontos. No banco real, só exporta para Parquet e confere; o DELETE dos dados
  brutos espera confirmação.
- **Downloads e fontes:** bibliotecas autorizadas (duckdb, pyarrow, scipy, scikit-learn, httpx, feedparser) e já
  instaladas. FinBERT, Yahoo/Stooq/FRED e arquivos da B3 também estão autorizados.
- **Telegram:** código pronto e desligado; o alerta aparece só no painel.

## Tarefas

| # | Etapa da espec | Conteúdo | Situação |
|---|---|---|---|
| V3-1 | 1.1, 1.3 | `calendario_b3.py`, `fechamento_diario`, correção da abertura no `/api/fechamento` | feita (4f62059) |
| V3-2 | 1.2 | `manutencao_banco.py`: fita agregada por minuto, Parquet frio, backup diário (sem apagar) | feita (2ad6be4) |
| V3-3 | 1.4 | `monitor_saude.py` e `fallback_tryd_fita.py` | feita (29eaffe) |
| V3-4 | 1.5 | Notícias, Focus e leilões de volta | feita (f29bf79) |
| V3-5 | 2.1, 2.2 | `coletor_global.py`, `abertura_justa.py`, ε_BR, `calendario_macro.py` | feita (13a8891) |
| V3-6 | 2.3 | `nowcast_fluxo_v2.py`, `modelo_regime_v3.py` (CQR), `correlacoes_v3.py` | feita (9ad5ec6) |
| V3-7 | 2.4 | `motor_cenario.py` (pilares, arquétipo, briefing) | feita (2c75428) |
| V3-8 | 3.1, 3.2 | `conversor_paridade.py`, `motor_regioes.py`, `gatilhos_execucao.py` | feita (9b2d45a) |
| V3-9 | 3.3, 3.4 | Estruturas de opções, zonas de swing, `estudo_regioes.py` | feita (2004ad1) |
| V3-10 | 4 | `atualizador_cockpit.py`, `painel_v3.py`, `v3_cockpit.html` | feita |

## O que os números mostraram até agora

| Peça | Medido (fora da amostra) | Leitura |
|---|---|---|
| Abertura justa (espec completa, 11 variáveis, janela de 60) | R² WIN −0,24, WDO −0,03 | pior que prever gap zero |
| Abertura justa em uso (ES + cesta EM, janela de 120) | R² WIN +0,064 (sinal 78%), WDO +0,098 (83%) | ganho pequeno, mas real; o desvio do erro é robusto (MAD) |
| ε_BR (WDO contra DXY + cesta EM, 5 min) | R² médio 0,26 em 49 pregões | ¾ do movimento do dólar é local |
| Faixas conformalizadas (CQR) | cobertura 69,0%/90,8% (60 min), 73,4%/91,5% (fechamento) | calibradas; a perda pinball empata com a gaussiana |
| Nowcast do estrangeiro v2 | sinal 28/45; regra do dia anterior 33/46 | ainda não bate a regra simples |
| Tryd com correção EWMA do viés | 8/9 no sinal, viés +515 → −98 | promissor, mas com 9 dias |
| Saldo reconstruído pela fita | forma: correlação 0,8 a 0,9 em BTG, Estrangeiro, Institucional e Liquidez; nível muito errado (XP −71,7 mil × −2,6 mil) | serve como forma, não como número |
| Score do `motor_cenario` (pesos da espec, sem ajuste) | correlação de postos com o retorno: −0,18 (dia), −0,11 (seguinte), −0,18 (5 pregões), 120 pregões; acerto 32 a 40% com \|score\| ≥ 35 | sem sinal direcional; o cockpit mostra isso abaixo dos badges |
| Zonas do `motor_regioes` (`estudo_regioes`, 37 pregões, 24/09 a 17/11) | zonas de força ≥ 60 contra nível aleatório de mesma distância: −330 a −359 pts (15 a 60 min); só o balde ALVO/NEUTRA bateu o aleatório (+531 em 80+/30 min) | heurística não validada; amostra curta e só 9 gatilhos confirmados |
| Primeira manutenção (18/11) | 12 pregões agregados na fita de 1 min; 113 tabelas de dia exportadas (613 MB de Parquet contra ~9 GB no SQLite); backup do núcleo 118 MB | Parquet conferido por checksum |

## Bloqueios encontrados

- **Posição em futuros por tipo de investidor (3.5.3):** a B3 descontinuou a consulta gratuita "Contratos em aberto por
  tipo de participante" em 07/04/2025. No Boletim Diário só há o total de contratos por produto. Hoje o dado só existe
  nos produtos pagos (UP2DATA e DataWise+). Sem ele, o nowcast v2 roda com três componentes em vez de quatro.

## Decisões que tomei sozinho

- **Plano Go (18/11, 02:10):** a cota gratuita do opencode acabou depois de cinco tarefas rodando em paralelo. As sessões
  seguem com o mesmo modelo, `opencode-go/deepseek-v4.1-flash`, pelo plano Go, que não tem custo extra mas tem limite de
  5 h de uso. Agora rodam no máximo duas tarefas ao mesmo tempo.
- **Fechamento de futuros:** o último preço do pregão contínuo vai até 18:30, porque WIN, WDO e DI não têm leilão de
  fechamento. A espec sugeria 17:55.
- **Abertura justa:** a regressão completa da espec (11 variáveis, 60 pregões) teve R² negativo fora da amostra. Ficou
  ES + cesta EM com janela de 120 e desvio robusto (MAD), porque o gap de 05/10 inflava o desvio-padrão.
- **Regime v3:** o modelo B (com mais features) supercobre as faixas com 35 pregões; ficou o modelo A, calibrado em 60.
- **Zonas de confluência:** a ligação simples encadeava 15 níveis numa zona de 2.200 pts no WIN; troquei por
  agrupamento guloso por densidade com largura máxima de 2 deltas.
- **Perfil de volume com passo fracionário:** `grafico_quant.perfil` montava a grade em float e perdia ~98% do volume
  no BOVA11 (passo 0,01) e no DI (0,005). Agora agrupa por faixas inteiras.
- **Backup do núcleo:** copia só as tabelas do núcleo (ATTACH só leitura + INSERT SELECT), em vez dos 15,8 GB.
- **Leilões:** a fonte alternativa ao SISWEB (que dá erro 500 desde 05/11) é a API pública do Tesouro
  `apiapex.tesouro.gov.br/aria/v1/api-leiloes-pub/custom/resultados?dataleilao=DD/MM/AAAA`.

## Para o dono decidir

1. **Expurgo dos dados brutos:** a primeira exportação para Parquet roda na madrugada de 18/11. Quando ela estiver
   conferida, rode `python manutencao_banco.py expurgar` (modo seco) para ver o que sairia. O DELETE de verdade usa
   `--apagar`. Depois, um `VACUUM` com os coletores parados devolve o espaço. Atenção: o /players de dias antigos, o
   estimador do Tryd e o perfil composto leem as tabelas brutas (há `ler_historico` para ler do Parquet, mas eles ainda
   não usam).
2. **Reiniciar o dashboard:** as mudanças em `dashboard.py`, `painel_v2.py` e `players.py` (duas fontes do Tryd,
   `/api/saude`, fallback pela fita) e o `/v3` só valem depois de encerrar o processo `dashboard.py`. O
   `vigiar_dashboard.ps1` religa em até 60 s.
3. **Janelas de saldo do Tryd:** continuam sem exportar desde 13/11. Reabra as 7 com DDE e as colunas Agr.
4. **Copom de 08-09/12/2026** em `dados/calendario_manual.csv`: confira a data (marcada "confirmar").
5. **Score do cenário sem sinal:** com os pesos da espec, a correlação com o retorno é levemente negativa (−0,11 a
   −0,18). Opções: manter como contexto (atual), ajustar os pesos por walk-forward (risco de sobreajuste com 120
   pregões) ou inverter o pilar que puxa contra. Minha sugestão: manter e reavaliar com 250 pregões.
6. **Nowcast do estrangeiro v2** não bate a regra do dia anterior (28/45 × 33/46). O painel continua mostrando os dois.
7. **Saldo do Tryd pela fita:** a forma acompanha, o nível não. Sem as janelas de saldo, o /players usa a fita e avisa.
8. **Posição em futuros por investidor:** só nos produtos pagos da B3 (UP2DATA, DataWise+). Assinar ou seguir sem.
