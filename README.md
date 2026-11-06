# Market Signals - Sistema Quantitativo B3

Sistema de análise quantitativa e coleta de dados para mercado brasileiro (B3), focado em:
- **WIN** (Mini-índice Bovespa)
- **WDO** (Mini-dólar)
- **BOVA11** (ETF Ibovespa e suas opções)

## 🗂️ Estrutura do Projeto

### Python (Coleta e Análise)

Sistema principal em Python para coleta de dados tick-by-tick, análise de microestrutura, regime de volatilidade, e estudos de fatores.

**Principais módulos:**
- `coletor_book.py`, `coletor_agressao.py` - Coleta de livro de ofertas e agressão
- `coletor_regime.py` - Modelo de regime de volatilidade
- `coletor_opcoes.py` - Volatilidade implícita e gregas
- `coletor_noticias.py` - Análise de sentimento de notícias (FinBERT)
- `dashboard.py` - Dashboard web em tempo real (Dash/Plotly)
- `estudo_*.py` - Estudos de lead-lag, fatores, nowcasting, etc.

**Banco de dados:** SQLite (`dados/book.db`) com ~2GB de dados históricos.

📖 Veja [CONTEXTO_PARA_BOTS.md](CONTEXTO_PARA_BOTS.md) para documentação completa.

### MQL5 (Trading Automatizado)

Sistema de trading baseado em **Delta de Pressão** tick-by-tick para MetaTrader 5.

**Componentes:**
- 📊 **Indicador** (`DeltaDePressao.mq5`) - Painel gráfico em tempo real
- 🤖 **Expert Advisor** (`PressureDeltaEA.mq5`) - Trading automatizado
- 📚 **Biblioteca** (`PressureDelta.mqh`) - Lógica compartilhada
- 🐍 **Referência Python** - Implementação e testes para validação

📖 Veja [mql5/README.md](mql5/README.md) para instalação e documentação completa.

### Lógica do Delta de Pressão

Classifica cada negócio usando flags `TICK_FLAG_BUY/SELL` da B3 e calcula:
- **Porcentagem de pressão** compradora vs vendedora em janela deslizante
- **Delta percentual**: aceleração da pressão (quantos pontos % subiu/caiu)
- **Sinais de trading**: quando o delta ultrapassa limiar configurável

Diferente de volume delta tradicional (saldo acumulado), foca em **mudanças de regime** de agressão.

## 🚀 Quick Start

### Python

```bash
# Instalar dependências
pip install -r requirements.txt

# Rodar dashboard
python dashboard.py

# Executar coleta (Windows, com MT5 conectado)
powershell -File iniciar_coleta.ps1
```

### MQL5

```bash
# Copiar arquivos para o MT5
# Ver instruções completas em mql5/README.md

# Testar implementação Python de referência
cd mql5/Tests
pip install -r requirements.txt
pytest test_pressure_delta.py -v
python pressure_delta_reference.py
```

## 📊 Dados Coletados

- **Snapshots de livro** (1 por segundo): bid/ask, volumes, imbalance
- **Agressão tick-by-tick**: volume comprador vs vendedor com flags da B3
- **Barras de 1 min**: WIN, WDO, DI, BOVA11, ativos externos (histórico)
- **Opções**: volatilidade implícita ATM, risk reversal, butterfly
- **Regime de volatilidade**: modelo próprio com previsão de faixa intradiária
- **Notícias**: RSS BR/BCB/Fed/Finnhub com sentimento (FinBERT-PT-BR)
- **Fluxo estrangeiro**: estimativa por proxy calibrada com dados oficiais B3

## ⚠️ Avisos

- **Repositório público** - Não commitar credenciais ou dados sensíveis
- **Trading automatizado envolve risco** - Sempre teste em conta demo primeiro
- **Dados da B3** - Requer feed de corretora compatível (ex: Genial, Clear)
- **MQL5 não compilado** - O código MQL5 foi gerado mas não testado no MetaEditor

## 📄 Licença

Projeto open source para fins educacionais e de pesquisa.

Copyright 2026, Renato Sousa

---

🔗 [GitHub](https://github.com/renatosousa/marketsignals)
