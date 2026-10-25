# Inflação Implícita do Tesouro Direto

Seção do dashboard que mostra a inflação implícita entre Tesouro Prefixado e Tesouro IPCA+ (breakeven), comparando com as expectativas do Boletim Focus do Banco Central.

## Conceito

**Breakeven** (ponto de equilíbrio) é a inflação implícita entre um título prefixado e um título indexado à inflação:

```
breakeven = (1 + taxa_prefixada) / (1 + taxa_real_ipca) - 1
```

Exemplo:
- Tesouro Prefixado 2029: 12,64% a.a.
- Tesouro IPCA+ 2029: IPCA + 6,77% a.a. (juro real)
- **Breakeven**: ~5,50% a.a.

**Interpretação:**
- Se o IPCA médio até o vencimento ficar **abaixo** do breakeven → Prefixado vence
- Se o IPCA médio ficar **acima** do breakeven → IPCA+ vence

O breakeven inclui prêmio de risco de inflação (e de liquidez), então tende a ficar **acima** das expectativas do Focus.

## Fontes de Dados

### 1. Tesouro Direto (CSV público)

**Arquivo:** `coletor_tesouro.py`

**URL:** https://www.tesourotransparente.gov.br/ckan/dataset/df56aa42-484a-4a59-8184-7676580c81e3/resource/796d2059-14e9-44e3-80c9-2d9e30b405c1/download/PrecoTaxaTesouroDireto.csv

- Histórico completo desde 2002
- Atualizado diariamente por volta de 18h-19h
- Formato: CSV separado por `;`, codificação `latin-1`, vírgula decimal
- Campos: Data Base, Tipo Titulo, Data Vencimento, Taxa Compra, Taxa Venda, PU Compra, PU Venda

**Coleta:**
```bash
python coletor_tesouro.py                       # últimos 10 dias
python coletor_tesouro.py 2026-10-20            # um dia específico
python coletor_tesouro.py 2026-10-01 2026-10-20 # intervalo
```

### 2. Boletim Focus (API pública do BCB)

**Arquivo:** `coletor_focus.py`

**URL:** https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/ExpectativasMercadoAnuais

- API OData do Banco Central, sem autenticação
- Expectativas de mercado do IPCA por ano (mediana, média, desvio-padrão, mínimo, máximo, Top5)
- Publicado às sextas-feiras (Relatório Focus semanal)

**Coleta:**
```bash
python coletor_focus.py                # última sexta-feira
python coletor_focus.py 2026-10-16     # data específica
```

## Limitações e Avisos

### Vencimentos Não Coincidem

Os títulos têm vencimentos diferentes:
- **Tesouro Prefixado**: vence em **01/01** do ano
- **Tesouro IPCA+**: vence em **15/05** ou **15/08** do ano

A diferença é de meses (ex: 2029 Prefixado vence em 01/01/2029, IPCA+ vence em 15/05/2029 = 134 dias de diferença).

**Impacto:** A comparação é **aproximada**, pois os prazos não são exatamente iguais. Para análise rigorosa, seria necessário interpolar as curvas.

### Prêmio de Risco

O breakeven **não** é uma previsão pura de inflação. Ele inclui:
1. **Prêmio de risco de inflação**: compensação pelo risco de a inflação subir além do esperado
2. **Prêmio de liquidez**: diferença de liquidez entre Prefixado e IPCA+

Por isso, o breakeven costuma ficar **acima** das expectativas do Focus.

### Pareamento por Safra

Os pares são formados por **ano** (safra), não por vencimento exato. Exemplos:
- Prefixado 2029 (01/01/2029) ↔ IPCA+ 2029 (15/05/2029)
- Prefixado 2032 (01/01/2032) ↔ IPCA+ 2032 (15/08/2032)

Se não houver ambos os títulos para um ano, aquele ano não aparece na comparação.

### Metas de Inflação

A meta do IPCA no Brasil é **3,0% a.a.** (centro), com intervalo de tolerância de ±1,5 p.p.:
- **Piso:** 1,5% a.a.
- **Teto:** 4,5% a.a.

Esses valores são mostrados como referência no dashboard.

## Cálculo da Média do Focus

Para cada safra (ex: 2029), o dashboard calcula a **média das medianas** das expectativas do Focus para os anos entre hoje e o vencimento.

Exemplo (hoje = 2026):
- Safra 2029 → média de Focus(2026), Focus(2027), Focus(2028), Focus(2029)
- Safra 2032 → média de Focus(2026) até Focus(2032)

Se não houver projeções para todos os anos, usa os anos disponíveis.

## Testes

```bash
python3 test_tesouro.py
```

Testa:
- Cálculo de breakeven
- Pareamento de títulos por safra
- Tratamento de dados faltantes
- Casos limites

## Estrutura de Arquivos

```
coletor_tesouro.py      # Coleta taxas do Tesouro Direto (CSV público)
coletor_focus.py        # Coleta expectativas do Focus (API BCB)
curvas.py               # Funções de cálculo de breakeven e pareamento
dashboard.py            # Endpoint /api/tesouro-breakeven
dashboard.html          # UI da seção no dashboard
test_tesouro.py         # Testes unitários
README_TESOURO.md       # Esta documentação
```

## Integração com o PR #2

Este PR (#X) é **complementar** ao PR #2 (Inflação Implícita DI x DAP):

**PR #2 (DI x DAP):**
- Usa **futuros** negociados na B3 (DI1 e DAP via MT5)
- Inflação implícita **intradiária** (coleta tick-by-tick)
- Breakeven atualizado a cada minuto durante o pregão
- Baseado em derivativos líquidos

**Este PR (Tesouro Direto):**
- Usa **títulos públicos** (Tesouro Prefixado e IPCA+)
- Taxas de **fechamento diário** (publicadas às ~18h-19h)
- Comparação com expectativas do **Focus semanal**
- Baseado em títulos do Tesouro Direto (investimento acessível)

**Diferenças de Nível:**
Os níveis de inflação implícita diferem alguns centésimos a ~0,2 p.p. nos prazos longos, por causa de:
1. Diferenças de liquidez entre futuros e títulos
2. Diferenças de vencimentos (DI1/DAP são mensais; Tesouro tem vencimentos anuais)
3. Diferenças de convenção de prazo

**Recomendação:** Compare **variações**, não níveis absolutos.

## Referências

- [Tesouro Transparente - Preços e Taxas](https://www.tesourotransparente.gov.br/temas/divida-publica-federal/precos-e-taxas-dos-titulos)
- [API de Expectativas de Mercado do BCB](https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/aplicacao#!/recursos)
- [Relatório Focus - BCB](https://www.bcb.gov.br/publicacoes/focus)
- [Meta de Inflação - BCB](https://www.bcb.gov.br/controleinflacao/metainflacao)

## Exemplos de Leitura

### Cenário 1: Breakeven > Focus
- Prefixado 2029: 12,64% vs IPCA+ 6,77% → Breakeven = 5,50%
- Focus (média 2026-2029): 4,32%
- **Leitura:** IPCA+ favorecido (Focus abaixo do breakeven). Se o IPCA médio ficar nos 4,32% do Focus, IPCA+ rende mais.

### Cenário 2: Breakeven < Focus (hipotético)
- Prefixado 2032: 11,50% vs IPCA+ 6,00% → Breakeven = 5,19%
- Focus (média 2026-2032): 5,50%
- **Leitura:** Prefixado favorecido (Focus acima do breakeven). Se o IPCA médio ficar nos 5,50% do Focus, Prefixado rende mais.

### Cenário 3: Breakeven próximo do teto
- Prefixado 2035: 13,00% vs IPCA+ 6,50% → Breakeven = 6,10%
- Meta = 3,0%, Teto = 4,5%
- **Leitura:** Breakeven **muito** acima do teto (6,10% vs 4,5%). Mercado precifica risco elevado de inflação desancorada.
