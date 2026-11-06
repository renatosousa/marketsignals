# Delta de Pressão - MQL5

Sistema completo de análise e trading baseado em pressão compradora/vendedora tick-by-tick para ativos da B3 (WIN, WDO, BOVA11).

## 📋 Visão Geral

Este projeto implementa:

1. **Indicador "Delta de Pressão"** (`DeltaDePressao.mq5`) - Painel gráfico em tempo real
2. **Expert Advisor** (`PressureDeltaEA.mq5`) - Sistema de trading automatizado
3. **Biblioteca compartilhada** (`PressureDelta.mqh`) - Lógica de cálculo reutilizável

### Como funciona

O sistema classifica cada negócio (tick) usando as flags `TICK_FLAG_BUY` e `TICK_FLAG_SELL` fornecidas pela B3 no MetaTrader 5, que identificam o lado agressor da operação. 

**O Delta de Pressão calcula:**
- Porcentagem de volume comprador vs vendedor numa janela deslizante (ex: 5 segundos)
- **Delta percentual**: quanto a pressão compradora (ou vendedora) *subiu* em pontos percentuais nessa janela
- Exemplo: se a compra estava em 48% e agora está em 62%, o delta é +14 pontos percentuais

Ao invés de apenas mostrar o saldo acumulado, o delta captura **acelerações** de pressão - útil para detectar ondas agressivas em mercados de futuros líquidos.

## 📦 Estrutura de Arquivos

```
mql5/
├── Include/
│   └── PressureDelta.mqh          # Classe CPressureDelta (lógica central)
├── Indicators/
│   └── DeltaDePressao.mq5         # Indicador com painel gráfico
├── Experts/
│   └── PressureDeltaEA.mq5        # Expert Advisor de trading
└── README.md                       # Este arquivo
```

## 🚀 Instalação

### 1. Copiar os arquivos

**No Windows:**

```powershell
# Copie os arquivos para as pastas do MT5
Copy-Item "PressureDelta.mqh" "$env:APPDATA\MetaQuotes\Terminal\<ID_DA_INSTALAÇÃO>\MQL5\Include\"
Copy-Item "DeltaDePressao.mq5" "$env:APPDATA\MetaQuotes\Terminal\<ID_DA_INSTALAÇÃO>\MQL5\Indicators\"
Copy-Item "PressureDeltaEA.mq5" "$env:APPDATA\MetaQuotes\Terminal\<ID_DA_INSTALAÇÃO>\MQL5\Experts\"
```

**Ou manualmente:**

1. Abra o MetaEditor (F4 no MT5)
2. Clique em "Arquivo" → "Abrir Pasta de Dados"
3. Navegue até as pastas `MQL5\Include`, `MQL5\Indicators` e `MQL5\Experts`
4. Copie os arquivos correspondentes

### 2. Compilar no MetaEditor

1. Abra o MetaEditor (F4 no MT5)
2. Abra cada arquivo `.mq5` (indicador e EA)
3. Pressione **F7** ou clique em "Compilar"
4. Verifique se não há erros na aba "Erros"

> ⚠️ **Nota:** Este código foi gerado por IA e **não foi compilado no MetaEditor**. Por favor, verifique erros de sintaxe e teste em conta demo antes de usar em produção.

### 3. Adicionar ao gráfico

**Indicador:**
1. No MT5, navegue: "Inserir" → "Indicadores" → "Personalizado" → "DeltaDePressao"
2. Configure os parâmetros e clique OK

**Expert Advisor:**
1. Navegue: "Inserir" → "Expert Advisors" → "PressureDeltaEA"
2. **IMPORTANTE:** O EA inicia em **modo somente sinais** por segurança
3. Configure todos os parâmetros antes de habilitar trading ao vivo

## ⚙️ Parâmetros

### Indicador: DeltaDePressao.mq5

| Parâmetro | Padrão | Descrição |
|-----------|--------|-----------|
| `InpWindowSeconds` | 5 | Janela de tempo para cálculo (segundos). Valores menores = mais sensível |
| `InpPanelScale` | 1.0 | Escala do painel (1.0 = Full HD, 1.5 = 4K) |
| `InpPanelX` | 20 | Posição horizontal do painel (pixels) |
| `InpPanelY` | 50 | Posição vertical do painel (pixels) |
| `InpColorBuy` | Verde Lima | Cor para pressão compradora |
| `InpColorSell` | Vermelho | Cor para pressão vendedora |
| `InpColorNeutral` | Amarelo | Cor para zona de equilíbrio |
| `InpColorBackground` | Cinza Escuro | Cor de fundo do painel |
| `InpColorText` | Branco | Cor do texto |

**O painel exibe:**
- **Barras horizontais** proporcionais à % de compra e venda
- **Linha de equilíbrio** central (50%)
- **Rótulo de viés**: Compra Forte / Moderada, Venda Forte / Moderada, ou Neutro
- **Delta**: variação percentual em relação ao cálculo anterior

### Expert Advisor: PressureDeltaEA.mq5

#### Pressão

| Parâmetro | Padrão | Descrição |
|-----------|--------|-----------|
| `InpWindowSeconds` | 5 | Janela de tempo (segundos) |
| `InpTriggerDeltaPct` | 10.0 | Delta mínimo para gerar sinal (%). Ex: 10 = a compra subiu ≥10 pontos percentuais |

#### Trading

| Parâmetro | Padrão | Descrição |
|-----------|--------|-----------|
| `InpEnableLiveTrading` | **false** | **SEGURANÇA:** Habilitar ordens reais. Padrão = somente sinais/alertas |
| `InpLotSize` | 1.0 | Volume em lotes |
| `InpTakeProfit` | 300 | Take Profit em pontos (0 = desabilitado) |
| `InpStopLoss` | 150 | Stop Loss em pontos (0 = desabilitado) |
| `InpReverseOnOpposite` | true | Reverter posição ao receber sinal oposto |
| `InpMagicNumber` | 202610 | Magic Number (identificador único) |
| `InpSymbolPrefix` | "WIN" | Prefixo do ativo para consolidar família (ex: WIN = WINZ26, WING27...) |

#### Controles de Risco

| Parâmetro | Padrão | Descrição |
|-----------|--------|-----------|
| `InpMaxEntriesPerDay` | 5 | Máximo de entradas por dia (limite diário) |
| `InpTradingStartTime` | "09:20" | Horário de início das operações (formato HH:MM) |
| `InpTradingEndTime` | "17:30" | Horário de encerramento (fecha posições abertas) |

## 🛡️ Segurança e Validações

### Verificação de Flags de Agressor

O código verifica automaticamente se o símbolo fornece dados de agressor (`TICK_FLAG_BUY` / `TICK_FLAG_SELL`):

- **Indicador:** Emite alertas se as flags não estiverem presentes, mas continua operando
- **Expert Advisor:** **Recusa operar** e retorna erro na inicialização se não detectar flags

**Por quê?** Sem flags de agressor, não há como distinguir compra de venda de forma confiável. O EA prioriza sua segurança.

### Modo Somente Sinais

Por padrão, o EA inicia com `InpEnableLiveTrading = false`. Neste modo:

- ✅ Processa dados de pressão normalmente
- ✅ Gera **alertas visuais** quando sinais são detectados
- ❌ **Não abre posições reais**

Isso permite validar a lógica e os parâmetros em ambiente real sem risco.

### Consolidação por Família de Ativos

O parâmetro `InpSymbolPrefix` permite consolidar estatísticas entre contratos do mesmo ativo:

- Exemplo: `InpSymbolPrefix = "WIN"` rastreia entradas e posições em `WINZ26`, `WING27`, etc.
- O contador `InpMaxEntriesPerDay` e a verificação de posição aberta consideram **todos os contratos da família**
- Útil para evitar sobreoperação quando há rolagem de vencimento

## 📊 Exemplos de Uso

### Configuração Agressiva (Scalping)

```
InpWindowSeconds = 3
InpTriggerDeltaPct = 8.0
InpTakeProfit = 150
InpStopLoss = 100
InpMaxEntriesPerDay = 10
```

Janela curta e gatilho menor = mais sinais, adequado para scalping em WIN.

### Configuração Conservadora (Swing Intraday)

```
InpWindowSeconds = 10
InpTriggerDeltaPct = 15.0
InpTakeProfit = 500
InpStopLoss = 250
InpMaxEntriesPerDay = 3
```

Janela maior e gatilho mais alto = sinais mais raros e fortes.

### Somente Monitoramento

```
InpEnableLiveTrading = false
(qualquer outra configuração)
```

O EA gera alertas de compra/venda sem executar ordens. Útil para validação paralela ao trading manual.

## 🧪 Testes e Validação

### Teste em Conta Demo

1. Configure o EA com `InpEnableLiveTrading = false`
2. Observe os alertas durante uma sessão de pregão
3. Valide se os sinais fazem sentido com a ação do preço
4. Ajuste `InpTriggerDeltaPct` e `InpWindowSeconds` conforme necessário
5. Quando confiante, teste com `InpEnableLiveTrading = true` em **conta demo**
6. Só vá para conta real após verificar consistência

### Implementação Python de Referência

Para validar a lógica do cálculo, veja a implementação Python em `Tests/pressure_delta_reference.py` (se disponível). Ela pode ser executada com dados históricos e comparada ao output do MQL5.

## ⚠️ Avisos Importantes

1. **Este código não foi compilado nem testado no MetaEditor.** Verifique erros de sintaxe e teste cuidadosamente.

2. **Opere primeiro em conta demo.** Trading automatizado envolve risco. Valide a estratégia antes de usar dinheiro real.

3. **A B3 fornece flags de agressor confiáveis no MT5 (Genial, Clear, etc.), mas confirme com sua corretora.**

4. **Ativos diferentes têm características diferentes:** O que funciona no WIN pode não funcionar no WDO ou BOVA11. Ajuste os parâmetros.

5. **Monitoramento:** Mesmo com EA automatizado, monitore as operações regularmente e ajuste stops/limites diários conforme necessário.

6. **Slippage e latência:** Em momentos de alta volatilidade, ordens a mercado podem sofrer slippage. O EA usa `SetDeviationInPoints(50)` como tolerância.

## 🔧 Solução de Problemas

### "Símbolo não possui dados de agressor"

**Causa:** O feed da corretora não está enviando `TICK_FLAG_BUY/SELL`.

**Solução:**
- Verifique se está conectado a um servidor B3 real (não demo sintético)
- Confirme com a corretora se o feed suporta aggressor flags
- Teste com outro símbolo (ex: WIN vs WDO)

### Indicador não aparece no gráfico

**Causa:** Erro de compilação ou arquivo não copiado.

**Solução:**
1. Abra o MetaEditor e compile novamente
2. Verifique a aba "Erros" por mensagens de compilação
3. Confirme que `PressureDelta.mqh` está na pasta `Include`

### EA não abre operações (mesmo com `InpEnableLiveTrading = true`)

**Verificações:**
1. Horário de trading configurado corretamente? (`InpTradingStartTime` / `InpTradingEndTime`)
2. Limite diário atingido? (`InpMaxEntriesPerDay` vs `g_entries_today`)
3. Sinais sendo gerados? (verifique o log no terminal)
4. AutoTrading habilitado? (botão verde "AutoTrading" no MT5)
5. EA passou pela verificação de aggressor flags na inicialização?

### Painel do indicador muito pequeno/grande

Ajuste o parâmetro `InpPanelScale`:
- **Full HD (1920×1080):** `1.0`
- **4K (3840×2160):** `1.5` a `2.0`
- **HD (1366×768):** `0.8` a `0.9`

## 📚 Referências

- [Documentação MQL5](https://www.mql5.com/pt/docs)
- [Estrutura MqlTick](https://www.mql5.com/pt/docs/constants/structures/mqltick)
- [CopyTicks](https://www.mql5.com/pt/docs/series/copyticks)
- [Projeto marketsignals no GitHub](https://github.com/renatosousa/marketsignals)

## 📄 Licença

Copyright 2026, Renato Sousa  
Projeto open source - veja o repositório para detalhes.

---

**Desenvolvido para o projeto marketsignals** - Sistema quantitativo Python para B3 (WIN, WDO, BOVA11)
