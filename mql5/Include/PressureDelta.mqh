//+------------------------------------------------------------------+
//|                                                PressureDelta.mqh |
//|                                      Copyright 2026, Renato Sousa |
//|                       https://github.com/renatosousa/marketsignals |
//+------------------------------------------------------------------+
#property copyright "Copyright 2026, Renato Sousa"
#property link      "https://github.com/renatosousa/marketsignals"
#property version   "1.00"
#property strict

//+------------------------------------------------------------------+
//| Estrutura para armazenar um tick de negociação                   |
//+------------------------------------------------------------------+
struct TickData
{
   datetime time;
   double   volume_buy;
   double   volume_sell;
};

//+------------------------------------------------------------------+
//| Classe para calcular o delta de pressão de agressão              |
//+------------------------------------------------------------------+
class CPressureDelta
{
private:
   string            m_symbol;
   int               m_window_seconds;
   TickData          m_ticks[];
   int               m_tick_count;
   int               m_max_ticks;
   
   double            m_current_buy_volume;
   double            m_current_sell_volume;
   double            m_current_buy_pct;
   double            m_current_sell_pct;
   
   double            m_prev_buy_pct;
   double            m_prev_sell_pct;
   
   double            m_delta_buy_pct;
   double            m_delta_sell_pct;
   
   bool              m_has_aggressor_flags;
   datetime          m_last_check_time;
   
public:
                     CPressureDelta(string symbol, int window_seconds);
                    ~CPressureDelta();
   
   bool              Initialize();
   void              ProcessTick();
   void              Calculate();
   
   double            GetDeltaBuyPct() { return m_delta_buy_pct; }
   double            GetDeltaSellPct() { return m_delta_sell_pct; }
   double            GetCurrentBuyPct() { return m_current_buy_pct; }
   double            GetCurrentSellPct() { return m_current_sell_pct; }
   double            GetBuyVolume() { return m_current_buy_volume; }
   double            GetSellVolume() { return m_current_sell_volume; }
   bool              HasAggressorFlags() { return m_has_aggressor_flags; }
   string            GetBiasLabel();
   
private:
   void              AddTick(datetime time, double vol_buy, double vol_sell);
   void              RemoveOldTicks();
   void              UpdatePercentages();
   bool              CheckAggressorFlags();
};

//+------------------------------------------------------------------+
//| Constructor                                                       |
//+------------------------------------------------------------------+
CPressureDelta::CPressureDelta(string symbol, int window_seconds)
{
   m_symbol = symbol;
   m_window_seconds = window_seconds;
   m_tick_count = 0;
   m_max_ticks = 100000;
   
   m_current_buy_volume = 0.0;
   m_current_sell_volume = 0.0;
   m_current_buy_pct = 0.0;
   m_current_sell_pct = 0.0;
   m_prev_buy_pct = 0.0;
   m_prev_sell_pct = 0.0;
   m_delta_buy_pct = 0.0;
   m_delta_sell_pct = 0.0;
   m_has_aggressor_flags = false;
   m_last_check_time = 0;
   
   ArrayResize(m_ticks, m_max_ticks);
}

//+------------------------------------------------------------------+
//| Destructor                                                        |
//+------------------------------------------------------------------+
CPressureDelta::~CPressureDelta()
{
   ArrayFree(m_ticks);
}

//+------------------------------------------------------------------+
//| Inicializa e verifica se o símbolo tem flags de agressor        |
//+------------------------------------------------------------------+
bool CPressureDelta::Initialize()
{
   m_has_aggressor_flags = CheckAggressorFlags();
   
   if(!m_has_aggressor_flags)
   {
      Print("AVISO: O símbolo ", m_symbol, " não possui flags de agressor (TICK_FLAG_BUY/SELL).");
      Print("O cálculo de pressão delta pode não ser confiável.");
   }
   
   return true;
}

//+------------------------------------------------------------------+
//| Verifica se os ticks do símbolo contêm flags de agressor        |
//+------------------------------------------------------------------+
bool CPressureDelta::CheckAggressorFlags()
{
   if(TimeCurrent() - m_last_check_time < 60)
      return m_has_aggressor_flags;
   
   m_last_check_time = TimeCurrent();
   
   MqlTick ticks[];
   int copied = CopyTicks(m_symbol, ticks, COPY_TICKS_TRADE, 0, 100);
   
   if(copied <= 0)
      return false;
   
   int buy_count = 0;
   int sell_count = 0;
   
   for(int i = 0; i < copied; i++)
   {
      if((ticks[i].flags & TICK_FLAG_BUY) != 0)
         buy_count++;
      if((ticks[i].flags & TICK_FLAG_SELL) != 0)
         sell_count++;
   }
   
   return (buy_count > 0 || sell_count > 0);
}

//+------------------------------------------------------------------+
//| Processa o tick atual                                            |
//+------------------------------------------------------------------+
void CPressureDelta::ProcessTick()
{
   MqlTick last_tick;
   if(!SymbolInfoTick(m_symbol, last_tick))
      return;
   
   double vol_buy = 0.0;
   double vol_sell = 0.0;
   
   if((last_tick.flags & TICK_FLAG_BUY) != 0)
      vol_buy = last_tick.volume;
   else if((last_tick.flags & TICK_FLAG_SELL) != 0)
      vol_sell = last_tick.volume;
   
   if(vol_buy > 0.0 || vol_sell > 0.0)
      AddTick(last_tick.time, vol_buy, vol_sell);
}

//+------------------------------------------------------------------+
//| Adiciona um tick ao buffer                                       |
//+------------------------------------------------------------------+
void CPressureDelta::AddTick(datetime time, double vol_buy, double vol_sell)
{
   if(m_tick_count >= m_max_ticks)
   {
      for(int i = 0; i < m_tick_count - 1; i++)
         m_ticks[i] = m_ticks[i + 1];
      m_tick_count--;
   }
   
   m_ticks[m_tick_count].time = time;
   m_ticks[m_tick_count].volume_buy = vol_buy;
   m_ticks[m_tick_count].volume_sell = vol_sell;
   m_tick_count++;
}

//+------------------------------------------------------------------+
//| Remove ticks fora da janela de tempo                             |
//+------------------------------------------------------------------+
void CPressureDelta::RemoveOldTicks()
{
   datetime cutoff = TimeCurrent() - m_window_seconds;
   int new_count = 0;
   
   for(int i = 0; i < m_tick_count; i++)
   {
      if(m_ticks[i].time >= cutoff)
      {
         if(new_count != i)
            m_ticks[new_count] = m_ticks[i];
         new_count++;
      }
   }
   
   m_tick_count = new_count;
}

//+------------------------------------------------------------------+
//| Atualiza as porcentagens atuais                                 |
//+------------------------------------------------------------------+
void CPressureDelta::UpdatePercentages()
{
   m_current_buy_volume = 0.0;
   m_current_sell_volume = 0.0;
   
   for(int i = 0; i < m_tick_count; i++)
   {
      m_current_buy_volume += m_ticks[i].volume_buy;
      m_current_sell_volume += m_ticks[i].volume_sell;
   }
   
   double total = m_current_buy_volume + m_current_sell_volume;
   
   if(total > 0.0)
   {
      m_current_buy_pct = (m_current_buy_volume / total) * 100.0;
      m_current_sell_pct = (m_current_sell_volume / total) * 100.0;
   }
   else
   {
      m_current_buy_pct = 0.0;
      m_current_sell_pct = 0.0;
   }
}

//+------------------------------------------------------------------+
//| Calcula o delta de pressão                                       |
//+------------------------------------------------------------------+
void CPressureDelta::Calculate()
{
   RemoveOldTicks();
   
   m_prev_buy_pct = m_current_buy_pct;
   m_prev_sell_pct = m_current_sell_pct;
   
   UpdatePercentages();
   
   m_delta_buy_pct = m_current_buy_pct - m_prev_buy_pct;
   m_delta_sell_pct = m_current_sell_pct - m_prev_sell_pct;
}

//+------------------------------------------------------------------+
//| Retorna o rótulo de viés (bias)                                 |
//+------------------------------------------------------------------+
string CPressureDelta::GetBiasLabel()
{
   if(m_current_buy_pct >= 60.0)
      return "Compra Forte";
   else if(m_current_buy_pct >= 52.0)
      return "Compra Moderada";
   else if(m_current_sell_pct >= 60.0)
      return "Venda Forte";
   else if(m_current_sell_pct >= 52.0)
      return "Venda Moderada";
   else
      return "Neutro";
}
//+------------------------------------------------------------------+
