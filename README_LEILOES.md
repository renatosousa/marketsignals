# Leilões do Tesouro Nacional

Monitor e histórico dos resultados dos leilões da dívida pública (LTN, NTN-F, NTN-B, LFT e, no histórico antigo, NTN-C e NTN-D), com métricas por linha, z-scores contra o histórico, alertas e a página `/leiloes` do dashboard.

Origem: especificação do GrokBot para o repositório `marketsignals`, adaptada a este projeto. Tudo o que está abaixo como "conferido" foi baixado e inspecionado em 04/11/2026.

## Fonte

Dataset ds013 do Tesouro Transparente ("Resultado dos leilões da Dívida Pública Mobiliária Federal Interna"). Não existe CSV; são planilhas do SISWEB:

| Arquivo | URL |
|---|---|
| Histórico completo (xls antigo, ~5 MB, 27 abas "Ano 2026" a "Ano 2000") | `https://sisweb.tesouro.gov.br/apex/cosis/rleiloes/arquivos/todos/desc/historico-leiloes-todos.xls` |
| Um ano (xlsx; 2011 a 2018 só existem em xls; o do ano novo dá 404 em janeiro) | `https://sisweb.tesouro.gov.br/apex/cosis/rleiloes/arquivos/unico/desc/historico-leiloes-{ANO}.xlsx` |

Conferido: 19.951 linhas de 04/01/2000 a 03/11/2026; Venda 17.161, Compra 1.505, Troca 598, Extra Compra 358, Extra Venda 329; 12.686 linhas de 1ª volta e 7.265 de 2ª. O leilão de 03/11 já estava no arquivo de 2026 às 11h de 04/11. A linha da LFT 01/03/2032 de 06/01/2026 (750.000 ofertados e vendidos, R$ 13.506.269.547,68, 500.000 ao Bacen) é o teste de aceitação.

O cabeçalho não fica numa linha fixa: o parser procura "Data do leilão" e mapeia as 13 colunas pelo nome; se faltar alguma, falha em vez de gravar errado. O `.xlsx` do Tesouro não traz a tag `<dimension>`, por isso é lido sem `read_only`.

## O que o arquivo diz (e o que a spec original errou)

- **Não há volume de propostas.** Não existe bid-to-cover. Usa-se a absorção = venda ÷ oferta.
- **"Venda para Bacen" é quantidade adicional** à venda ao mercado, ao mesmo PU (em 793 das 1.035 linhas com Bacen a quantidade supera a venda). A participação é Bacen ÷ (venda + Bacen), não Bacen ÷ venda.
- **A chave natural da spec colide** em 16 linhas (2004 e 2009). A chave aqui inclui a data de liquidação e um `seq`: cópias exatas viram uma linha só (2) e linhas com a mesma chave e valores diferentes ficam as duas (4 pares de 2009).
- **`Oferta` vem como o texto `null`** em 243 linhas (2003 a 2007): vira NULL, nunca zero. `Venda para Bacen` vazia também.
- **2026 tem 839 linhas**, não 846 (846 é a contagem de linhas da planilha, com o cabeçalho). 477 têm venda menor que a oferta e 216 têm taxa média diferente da taxa de corte (corte ≥ média em toda venda).
- **Compra e Troca não ocorreram em 2026.** Houve Extra Compra e Extra Venda em 16, 17 e 18/03 e Extra Compra de NTN-B e NTN-F em 26 e 27/10.
- **2ª volta com venda zero é comum** (148 das 377 linhas de 2026): não é fracasso.

## Uso

```bash
python coletor_leiloes.py backfill        # baixa o historico completo e carrega tudo (~8 s); --arquivo CAMINHO usa uma planilha local
python coletor_leiloes.py update          # baixa o ano corrente, grava o que mudou, recalcula metricas/alertas das linhas novas
python coletor_leiloes.py status
python leiloes.py recalcular [--tudo]     # recalcula metricas; alertas dos ultimos 10 dias (--tudo: de todo o historico)
python leiloes.py relatorio [--dia AAAA-MM-DD]   # markdown e CSV em dados/relatorios/
python -m unittest test_leiloes -v
```

Agendamento: `iniciar_coleta.ps1` sobe o supervisor `LEILOES` (08:00 a 18:05, seg a sex). Ao subir faz uma busca (pega os Extras do dia anterior). Em terça e quinta (dias esperados, estimativa) espera até 12:00 e busca a cada 15 min até a data de hoje aparecer; nos demais dias úteis busca uma vez por hora. O horário real de publicação é medido: `leilao_resultados.visto_em` guarda quando esta cópia viu cada linha pela primeira vez.

## Tabelas (`dados/book.db`)

| Tabela | Conteúdo |
|---|---|
| `leilao_resultados` | uma linha por (data, título, tipo, volta, vencimento, liquidação, seq); `hash` detecta revisão do Tesouro |
| `leilao_revisoes` | antes e depois (JSON) de toda linha que mudou |
| `leilao_metricas` | métricas e z-scores por resultado |
| `leilao_alertas` | alertas, um por (resultado, tipo) |

Taxas em fração (0,078280 = 7,8280% a.a.; na LFT é ágio/deságio, e pode ser negativa), financeiro em R$. A ANBIMA do mercado secundário já é coletada por `coletor_anbima.py` (`anbima_titulos`, `anbima_ettj`); não há tabela duplicada.

## Métricas (leiloes.py)

- **absorcao**, **spread_bps** = (corte − média) × 10.000, **pu_implicito** = financeiro ÷ venda, **bacen_share**.
- **concessao_bps** = taxa de corte − taxa indicativa da ANBIMA da véspera (mesmo vencimento; última data anterior ao leilão, no máximo 7 dias corridos antes). A ANBIMA só tem histórico desde 14/09/2026, então só existe para os leilões desde então. LFT fica sem concessão.
- **curva_bps** = taxa de corte contra a ETTJ da ANBIMA da véspera, interpolada no prazo (curva pré para LTN e NTN-F, IPCA para NTN-B). Substitui o "contra o DI1 interpolado" da spec: o projeto só coleta 6 vértices de DI1 (F27 a F35) em `macro_cotacoes`, o que não dá curva.
- **duracao_mod** e **dv01_brl** = duration × financeiro × 0,0001, base 252, valor na liquidação, taxa média. LTN: PU = 1000/(1+y)^(du/252). NTN-F: cupom de 10% a.a. semestral. NTN-B: cupom de 6% a.a. semestral sobre o VNA (fluxos em fração do VNA). Compra e Extra Compra têm sinal negativo; LFT e Troca ficam NULL. Feriados da ANBIMA calculados em `leiloes.feriados` (Páscoa, Carnaval, Corpus Christi e datas fixas; 20/11 só desde 2024), sem baixar nada.
- **Verificação do fluxo:** o PU recalculado pela taxa média bate com financeiro ÷ venda das LTN e NTN-F de 2026 (erro máximo 0,0000%); antes de 2020 há linhas com desvio maior (taxas fora do padrão do arquivo). O VNA que a NTN-B implica é constante dentro do dia (R$ 4.772,79 em 03/11/2026).
- **z_*_1a / z_*_3a**: z de absorção, spread, tamanho (financeiro) e concessão contra as observações **anteriores** (nunca a própria nem as do mesmo dia), janelas de 1 e 3 anos, mínimo de 8; por título+tipo+volta+vencimento e, se faltar amostra, pela faixa de prazo (até 2, 2 a 5, 5 a 10 e acima de 10 anos). `z_nivel` diz qual nível foi usado na absorção. Como a absorção quase sempre é 1,00, um leilão apenas parcial já dá z muito negativo.

## Alertas

Limites em `leiloes.LIMITES`. Gravados em `leilao_alertas` e mostrados na página; não há canal externo (o projeto não tem notificação), por escolha do usuário.

| Tipo | Severidade | Regra |
|---|---|---|
| `LEILAO_FRACO` | alta | venda, 1ª volta: absorção < 0,5, ou z de absorção < −2, ou z de concessão > +2 |
| `VENDA_ZERO` | aviso (1ª volta) / baixa (2ª) | nada aceito com oferta |
| `ACEITACAO_PARCIAL` | info | 0 < venda < oferta |
| `LEILAO_FORTE` | info | 1ª e 2ª voltas integrais e concessão ≤ 0 (só com ANBIMA) |
| `LOTE_GRANDE` | info | z de tamanho > 2 |
| `LOTE_GRANDE_DV01` | aviso | DV01 do dia acima do percentil 95 do histórico anterior (mínimo 40 dias) |
| `EXTRA_VENDA`, `EXTRA_COMPRA` | aviso | sempre; um por dia e título |
| `BACEN` | info | Bacen levou quantidade |

Nos 12 meses até 04/11/2026 (101 datas de leilão) o `LEILAO_FRACO` disparou 48 vezes (medido sem a regra de concessão, que ainda não tem histórico), incluindo 18/12/2025, com absorção zero em três títulos.

## Limites

- O calendário oficial de leilões não existe em formato legível: "próximos leilões" é estimativa pelo padrão de 2026 (terça: LFT e NTN-B; quinta: LTN e NTN-F). O horário de publicação do resultado e o edital não foram confirmados.
- Não se sabe por quanto tempo a ANBIMA mantém os arquivos antigos; o coletor da ANBIMA guarda tudo desde 14/09/2026.
- Sem tabela de calendário manual nem relatório em PNG: a página `/leiloes` cobre os gráficos.
