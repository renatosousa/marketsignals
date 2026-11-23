//+------------------------------------------------------------------+
//|                                           PressureDeltaEA.mq5 |
//|                                      Copyright 2026, Renato Sousa |
//|                       https://github.com/renatosousa/marketsignals |
//+------------------------------------------------------------------+
#property copyright "Copyright 2026, Renato Sousa"
#property link      "https://github.com/renatosousa/marketsignals"
#property version   "1.00"
#property description "Expert Advisor: Delta de Pressão - Trading automatizado baseado em agressão B3"
#property strict

#include <Trade\Trade.mqh>
#include <PressureDelta.mqh>

//--- Input parameters - Pressão
input group "=== Parâmetros de Pressão ==="
input int      InpWindowSeconds = 5;           // Janela de tempo (segundos)
input double   InpTriggerDeltaPct = 10.0;      // Delta mínimo para sinal (%)

//--- Input parameters - Trading
input group "=== Parâmetros de Trading ==="
input bool     InpEnableLiveTrading = false;   // Habilitar trading ao vivo (SEGURANÇA)
input double   InpLotSize = 1.0;               // Volume (lotes)
input int      InpTakeProfit = 300;            // Take Profit (pontos)
input int      InpStopLoss = 150;              // Stop Loss (pontos)
input bool     InpReverseOnOpposite = true;    // Reverter em sinal oposto
input int      InpMagicNumber = 202610;        // Magic Number
input string   InpSymbolPrefix = "WIN";        // Prefixo do ativo (ex: WIN para todos os contratos)

//--- Input parameters - Controles de Risco
input group "=== Controles de Risco ==="
input int      InpMaxEntriesPerDay = 5;        // Máximo de entradas por dia
input string   InpTradingStartTime = "09:20";  // Horário de início
input string   InpTradingEndTime = "17:30";    // Horário de encerramento

//--- Global variables
CPressureDelta *g_pressure = NULL;
CTrade g_trade;
datetime g_last_bar_time = 0;
int g_entries_today = 0;
datetime g_current_day = 0;
bool g_has_position = false;
ENUM_POSITION_TYPE g_position_type = POSITION_TYPE_BUY;

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
   if(!InpEnableLiveTrading)
   {
      Print("╔═══════════════════════════════════════════════════════════════╗");
      Print("║          MODO SOMENTE SINAIS - TRADING DESABILITADO          ║");
      Print("║  Defina 'InpEnableLiveTrading = true' para operar ao vivo   ║");
      Print("╚═══════════════════════════════════════════════════════════════╝");
      Comment("⚠️ MODO SOMENTE SINAIS - Trading desabilitado");
   }
   else
   {
      Print("╔═══════════════════════════════════════════════════════════════╗");
      Print("║               ⚠️  TRADING AO VIVO HABILITADO ⚠️              ║");
      Print("║        Este EA executará ordens reais no mercado!           ║");
      Print("╚═══════════════════════════════════════════════════════════════╝");
      Comment("✅ TRADING AO VIVO HABILITADO");
   }
   
   g_pressure = new CPressureDelta(_Symbol, InpWindowSeconds);
   
   if(!g_pressure.Initialize())
   {
      Print("Erro ao inicializar CPressureDelta");
      return INIT_FAILED;
   }
   
   if(!g_pressure.HasAggressorFlags())
   {
      Print("╔═══════════════════════════════════════════════════════════════╗");
      Print("║                    ⚠️  AVISO CRÍTICO ⚠️                      ║");
      Print("║  O símbolo ", _Symbol, " não possui dados de agressor!          ║");
      Print("║  (TICK_FLAG_BUY/SELL não detectados)                        ║");
      Print("║  O EA NÃO VAI OPERAR para sua segurança.                    ║");
      Print("║  Verifique se está usando dados corretos da B3.             ║");
      Print("╚═══════════════════════════════════════════════════════════════╝");
      Alert("ERRO: Dados de agressor não disponíveis. EA não vai operar.");
      return INIT_FAILED;
   }
   
   g_trade.SetExpertMagicNumber(InpMagicNumber);
   g_trade.SetDeviationInPoints(50);
   g_trade.SetTypeFilling(ORDER_FILLING_RETURN);
   
   g_current_day = GetCurrentDayStart();
   g_entries_today = CountTodayEntries();
   
   Print("EA inicializado com sucesso");
   Print("Janela: ", InpWindowSeconds, "s | Delta gatilho: ", InpTriggerDeltaPct, "%");
   Print("TP: ", InpTakeProfit, " pts | SL: ", InpStopLoss, " pts");
   Print("Max entradas/dia: ", InpMaxEntriesPerDay, " | Entradas hoje: ", g_entries_today);
   
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   if(g_pressure != NULL)
   {
      delete g_pressure;
      g_pressure = NULL;
   }
   
   Comment("");
}

//+------------------------------------------------------------------+
//| Expert tick function                                             |
//+------------------------------------------------------------------+
void OnTick()
{
   g_pressure.ProcessTick();
   g_pressure.Calculate();
   
   CheckNewDay();
   CheckTradingHours();
   UpdatePositionStatus();
   
   if(!IsTradeAllowed())
      return;
   
   CheckSignals();
   UpdateComment();
}

//+------------------------------------------------------------------+
//| Verifica se é um novo dia                                        |
//+------------------------------------------------------------------+
void CheckNewDay()
{
   datetime current_day = GetCurrentDayStart();
   
   if(current_day != g_current_day)
   {
      Print("═══ NOVO DIA DE TRADING ═══");
      Print("Data: ", TimeToString(current_day, TIME_DATE));
      Print("Entradas ontem: ", g_entries_today);
      
      g_current_day = current_day;
      g_entries_today = CountTodayEntries();
      
      Print("Contador resetado. Entradas hoje: ", g_entries_today);
   }
}

//+------------------------------------------------------------------+
//| Verifica horário de negociação                                   |
//+------------------------------------------------------------------+
void CheckTradingHours()
{
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   
   int current_minutes = dt.hour * 60 + dt.min;
   
   string start_parts[];
   string end_parts[];
   StringSplit(InpTradingStartTime, ':', start_parts);
   StringSplit(InpTradingEndTime, ':', end_parts);
   
   int start_minutes = (int)StringToInteger(start_parts[0]) * 60 + (int)StringToInteger(start_parts[1]);
   int end_minutes = (int)StringToInteger(end_parts[0]) * 60 + (int)StringToInteger(end_parts[1]);
   
   if(current_minutes >= end_minutes && g_has_position)
   {
      Print("Horário de encerramento atingido. Fechando posição.");
      CloseAllPositions("Encerramento por horário");
   }
}

//+------------------------------------------------------------------+
//| Atualiza status da posição                                       |
//+------------------------------------------------------------------+
void UpdatePositionStatus()
{
   g_has_position = false;
   
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket <= 0)
         continue;
      
      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;
      
      if(PositionGetInteger(POSITION_MAGIC) != InpMagicNumber)
         continue;
      
      if(!MatchesSymbolFamily(PositionGetString(POSITION_SYMBOL)))
         continue;
      
      g_has_position = true;
      g_position_type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      break;
   }
}

//+------------------------------------------------------------------+
//| Verifica se o trading é permitido                                |
//+------------------------------------------------------------------+
bool IsTradeAllowed()
{
   if(!InpEnableLiveTrading)
      return false;
   
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   
   int current_minutes = dt.hour * 60 + dt.min;
   
   string start_parts[];
   string end_parts[];
   StringSplit(InpTradingStartTime, ':', start_parts);
   StringSplit(InpTradingEndTime, ':', end_parts);
   
   int start_minutes = (int)StringToInteger(start_parts[0]) * 60 + (int)StringToInteger(start_parts[1]);
   int end_minutes = (int)StringToInteger(end_parts[0]) * 60 + (int)StringToInteger(end_parts[1]);
   
   if(current_minutes < start_minutes || current_minutes >= end_minutes)
      return false;
   
   if(g_entries_today >= InpMaxEntriesPerDay)
   {
      static bool warned = false;
      if(!warned)
      {
         Print("Máximo de entradas diárias atingido: ", g_entries_today);
         warned = true;
      }
      return false;
   }
   
   return true;
}

//+------------------------------------------------------------------+
//| Verifica sinais de trading                                       |
//+------------------------------------------------------------------+
void CheckSignals()
{
   double delta_buy = g_pressure.GetDeltaBuyPct();
   double delta_sell = g_pressure.GetDeltaSellPct();
   
   bool buy_signal = delta_buy >= InpTriggerDeltaPct;
   bool sell_signal = delta_sell >= InpTriggerDeltaPct;
   
   if(!InpEnableLiveTrading)
   {
      if(buy_signal)
         Alert("🟢 SINAL DE COMPRA | Delta: +", DoubleToString(delta_buy, 1), "% | ", 
               _Symbol, " | ", g_pressure.GetBiasLabel());
      
      if(sell_signal)
         Alert("🔴 SINAL DE VENDA | Delta: +", DoubleToString(delta_sell, 1), "% | ", 
               _Symbol, " | ", g_pressure.GetBiasLabel());
      
      return;
   }
   
   if(buy_signal)
   {
      if(g_has_position && g_position_type == POSITION_TYPE_SELL && InpReverseOnOpposite)
      {
         Print("═══ SINAL DE REVERSÃO COMPRA ═══");
         Print("Delta Compra: +", DoubleToString(delta_buy, 2), "%");
         CloseAllPositions("Reversão para compra");
         OpenBuy();
      }
      else if(!g_has_position)
      {
         Print("═══ SINAL DE COMPRA ═══");
         Print("Delta Compra: +", DoubleToString(delta_buy, 2), "%");
         OpenBuy();
      }
   }
   else if(sell_signal)
   {
      if(g_has_position && g_position_type == POSITION_TYPE_BUY && InpReverseOnOpposite)
      {
         Print("═══ SINAL DE REVERSÃO VENDA ═══");
         Print("Delta Venda: +", DoubleToString(delta_sell, 2), "%");
         CloseAllPositions("Reversão para venda");
         OpenSell();
      }
      else if(!g_has_position)
      {
         Print("═══ SINAL DE VENDA ═══");
         Print("Delta Venda: +", DoubleToString(delta_sell, 2), "%");
         OpenSell();
      }
   }
}

//+------------------------------------------------------------------+
//| Abre posição de compra                                           |
//+------------------------------------------------------------------+
void OpenBuy()
{
   double price = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double sl = 0;
   double tp = 0;
   
   if(InpStopLoss > 0)
      sl = price - InpStopLoss * _Point;
   
   if(InpTakeProfit > 0)
      tp = price + InpTakeProfit * _Point;
   
   if(g_trade.Buy(InpLotSize, _Symbol, price, sl, tp, 
                  "PressureDelta|Buy|" + g_pressure.GetBiasLabel()))
   {
      Print("✅ COMPRA EXECUTADA");
      Print("Preço: ", price, " | SL: ", sl, " | TP: ", tp);
      Print("Viés: ", g_pressure.GetBiasLabel());
      g_entries_today++;
      g_has_position = true;
      g_position_type = POSITION_TYPE_BUY;
   }
   else
   {
      Print("❌ ERRO AO ABRIR COMPRA: ", GetLastError());
      Print("RetCode: ", g_trade.ResultRetcode(), " | ", g_trade.ResultRetcodeDescription());
   }
}

//+------------------------------------------------------------------+
//| Abre posição de venda                                            |
//+------------------------------------------------------------------+
void OpenSell()
{
   double price = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double sl = 0;
   double tp = 0;
   
   if(InpStopLoss > 0)
      sl = price + InpStopLoss * _Point;
   
   if(InpTakeProfit > 0)
      tp = price - InpTakeProfit * _Point;
   
   if(g_trade.Sell(InpLotSize, _Symbol, price, sl, tp, 
                   "PressureDelta|Sell|" + g_pressure.GetBiasLabel()))
   {
      Print("✅ VENDA EXECUTADA");
      Print("Preço: ", price, " | SL: ", sl, " | TP: ", tp);
      Print("Viés: ", g_pressure.GetBiasLabel());
      g_entries_today++;
      g_has_position = true;
      g_position_type = POSITION_TYPE_SELL;
   }
   else
   {
      Print("❌ ERRO AO ABRIR VENDA: ", GetLastError());
      Print("RetCode: ", g_trade.ResultRetcode(), " | ", g_trade.ResultRetcodeDescription());
   }
}

//+------------------------------------------------------------------+
//| Fecha todas as posições                                          |
//+------------------------------------------------------------------+
void CloseAllPositions(string reason)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket <= 0)
         continue;
      
      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;
      
      if(PositionGetInteger(POSITION_MAGIC) != InpMagicNumber)
         continue;
      
      if(!MatchesSymbolFamily(PositionGetString(POSITION_SYMBOL)))
         continue;
      
      if(g_trade.PositionClose(ticket))
      {
         Print("✅ POSIÇÃO FECHADA: ", reason);
         Print("Ticket: ", ticket);
      }
      else
      {
         Print("❌ ERRO AO FECHAR POSIÇÃO: ", GetLastError());
      }
   }
   
   g_has_position = false;
}

//+------------------------------------------------------------------+
//| Conta entradas do dia                                            |
//+------------------------------------------------------------------+
int CountTodayEntries()
{
   int count = 0;
   datetime day_start = GetCurrentDayStart();
   datetime day_end = day_start + 86400;
   
   HistorySelect(day_start, day_end);
   
   for(int i = 0; i < HistoryDealsTotal(); i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket <= 0)
         continue;
      
      if(HistoryDealGetInteger(ticket, DEAL_MAGIC) != InpMagicNumber)
         continue;
      
      if(!MatchesSymbolFamily(HistoryDealGetString(ticket, DEAL_SYMBOL)))
         continue;
      
      ENUM_DEAL_ENTRY entry_type = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket, DEAL_ENTRY);
      if(entry_type == DEAL_ENTRY_IN)
         count++;
   }
   
   return count;
}

//+------------------------------------------------------------------+
//| Retorna o início do dia atual                                    |
//+------------------------------------------------------------------+
datetime GetCurrentDayStart()
{
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   dt.hour = 0;
   dt.min = 0;
   dt.sec = 0;
   return StructToTime(dt);
}

//+------------------------------------------------------------------+
//| Verifica se o símbolo pertence à família configurada             |
//+------------------------------------------------------------------+
bool MatchesSymbolFamily(string symbol)
{
   if(InpSymbolPrefix == "")
      return (symbol == _Symbol);
   
   return (StringFind(symbol, InpSymbolPrefix) == 0);
}

//+------------------------------------------------------------------+
//| Atualiza comentário do gráfico                                   |
//+------------------------------------------------------------------+
void UpdateComment()
{
   string status = InpEnableLiveTrading ? "✅ TRADING ATIVO" : "⚠️ SOMENTE SINAIS";
   
   string comment = "╔═══════════════════════════════════════╗\n";
   comment += "║     PRESSURE DELTA EA v1.00          ║\n";
   comment += "╠═══════════════════════════════════════╣\n";
   comment += StringFormat("║ Status: %-28s║\n", status);
   comment += "╠═══════════════════════════════════════╣\n";
   comment += StringFormat("║ Viés: %-30s║\n", g_pressure.GetBiasLabel());
   comment += StringFormat("║ Compra: %5.1f%% (Δ%+5.1f%%)        ║\n", 
                          g_pressure.GetCurrentBuyPct(), g_pressure.GetDeltaBuyPct());
   comment += StringFormat("║ Venda:  %5.1f%% (Δ%+5.1f%%)        ║\n", 
                          g_pressure.GetCurrentSellPct(), g_pressure.GetDeltaSellPct());
   comment += "╠═══════════════════════════════════════╣\n";
   comment += StringFormat("║ Entradas hoje: %2d / %2d            ║\n", 
                          g_entries_today, InpMaxEntriesPerDay);
   comment += StringFormat("║ Posição: %-27s║\n", 
                          g_has_position ? (g_position_type == POSITION_TYPE_BUY ? "COMPRADO" : "VENDIDO") : "SEM POSIÇÃO");
   comment += "╚═══════════════════════════════════════╝";
   
   Comment(comment);
}
//+------------------------------------------------------------------+
