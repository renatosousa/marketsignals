//+------------------------------------------------------------------+
//|                                              DeltaDePressao.mq5 |
//|                                      Copyright 2026, Renato Sousa |
//|                       https://github.com/renatosousa/marketsignals |
//+------------------------------------------------------------------+
#property copyright "Copyright 2026, Renato Sousa"
#property link      "https://github.com/renatosousa/marketsignals"
#property version   "1.00"
#property description "Indicador de Delta de Pressão - Análise tick-by-tick de agressão B3"
#property indicator_chart_window
#property strict

#include <PressureDelta.mqh>

//--- Input parameters
input int      InpWindowSeconds = 5;           // Janela de tempo (segundos)
input double   InpPanelScale = 1.0;            // Escala do painel (1.0=Full HD, 1.5=4K)
input int      InpPanelX = 20;                 // Posição X do painel
input int      InpPanelY = 50;                 // Posição Y do painel
input color    InpColorBuy = clrLimeGreen;     // Cor de compra
input color    InpColorSell = clrRed;          // Cor de venda
input color    InpColorNeutral = clrYellow;    // Cor neutra
input color    InpColorBackground = C'25,25,25'; // Cor de fundo
input color    InpColorText = clrWhite;        // Cor do texto

//--- Global variables
CPressureDelta *g_pressure = NULL;
string g_prefix = "PressPanel_";
int g_base_width = 300;
int g_base_height = 200;
int g_scaled_width;
int g_scaled_height;

//+------------------------------------------------------------------+
//| Custom indicator initialization function                         |
//+------------------------------------------------------------------+
int OnInit()
{
   g_scaled_width = (int)(g_base_width * InpPanelScale);
   g_scaled_height = (int)(g_base_height * InpPanelScale);
   
   g_pressure = new CPressureDelta(_Symbol, InpWindowSeconds);
   
   if(!g_pressure.Initialize())
   {
      Print("Erro ao inicializar CPressureDelta");
      return INIT_FAILED;
   }
   
   if(!g_pressure.HasAggressorFlags())
   {
      Alert("AVISO: O símbolo ", _Symbol, " não possui dados de agressor (TICK_FLAG_BUY/SELL).");
      Alert("O cálculo pode não ser confiável. Verifique se está usando dados da B3.");
   }
   
   CreatePanel();
   
   EventSetTimer(1);
   
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Custom indicator deinitialization function                       |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   DeletePanel();
   
   if(g_pressure != NULL)
   {
      delete g_pressure;
      g_pressure = NULL;
   }
}

//+------------------------------------------------------------------+
//| Custom indicator iteration function                              |
//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const int begin,
                const double &price[])
{
   g_pressure.ProcessTick();
   g_pressure.Calculate();
   UpdatePanel();
   
   return rates_total;
}

//+------------------------------------------------------------------+
//| Timer function                                                    |
//+------------------------------------------------------------------+
void OnTimer()
{
   g_pressure.Calculate();
   UpdatePanel();
}

//+------------------------------------------------------------------+
//| Cria o painel gráfico                                            |
//+------------------------------------------------------------------+
void CreatePanel()
{
   int x = InpPanelX;
   int y = InpPanelY;
   int w = g_scaled_width;
   int h = g_scaled_height;
   int fs = (int)(10 * InpPanelScale);
   int fs_title = (int)(12 * InpPanelScale);
   int fs_bias = (int)(14 * InpPanelScale);
   
   ObjectCreate(0, g_prefix + "Background", OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(0, g_prefix + "Background", OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, g_prefix + "Background", OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, g_prefix + "Background", OBJPROP_XSIZE, w);
   ObjectSetInteger(0, g_prefix + "Background", OBJPROP_YSIZE, h);
   ObjectSetInteger(0, g_prefix + "Background", OBJPROP_BGCOLOR, InpColorBackground);
   ObjectSetInteger(0, g_prefix + "Background", OBJPROP_BORDER_TYPE, BORDER_FLAT);
   ObjectSetInteger(0, g_prefix + "Background", OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, g_prefix + "Background", OBJPROP_BACK, false);
   ObjectSetInteger(0, g_prefix + "Background", OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, g_prefix + "Background", OBJPROP_HIDDEN, true);
   
   ObjectCreate(0, g_prefix + "Title", OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, g_prefix + "Title", OBJPROP_XDISTANCE, x + (int)(10 * InpPanelScale));
   ObjectSetInteger(0, g_prefix + "Title", OBJPROP_YDISTANCE, y + (int)(10 * InpPanelScale));
   ObjectSetInteger(0, g_prefix + "Title", OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, g_prefix + "Title", OBJPROP_COLOR, InpColorText);
   ObjectSetInteger(0, g_prefix + "Title", OBJPROP_FONTSIZE, fs_title);
   ObjectSetString(0, g_prefix + "Title", OBJPROP_FONT, "Arial Bold");
   ObjectSetString(0, g_prefix + "Title", OBJPROP_TEXT, "Delta de Pressão");
   ObjectSetInteger(0, g_prefix + "Title", OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, g_prefix + "Title", OBJPROP_HIDDEN, true);
   
   ObjectCreate(0, g_prefix + "Window", OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, g_prefix + "Window", OBJPROP_XDISTANCE, x + (int)(10 * InpPanelScale));
   ObjectSetInteger(0, g_prefix + "Window", OBJPROP_YDISTANCE, y + (int)(30 * InpPanelScale));
   ObjectSetInteger(0, g_prefix + "Window", OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, g_prefix + "Window", OBJPROP_COLOR, clrGray);
   ObjectSetInteger(0, g_prefix + "Window", OBJPROP_FONTSIZE, fs);
   ObjectSetString(0, g_prefix + "Window", OBJPROP_FONT, "Arial");
   ObjectSetString(0, g_prefix + "Window", OBJPROP_TEXT, "Janela: " + IntegerToString(InpWindowSeconds) + "s");
   ObjectSetInteger(0, g_prefix + "Window", OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, g_prefix + "Window", OBJPROP_HIDDEN, true);
   
   ObjectCreate(0, g_prefix + "BuyBar", OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(0, g_prefix + "BuyBar", OBJPROP_XDISTANCE, x + (int)(10 * InpPanelScale));
   ObjectSetInteger(0, g_prefix + "BuyBar", OBJPROP_YDISTANCE, y + (int)(55 * InpPanelScale));
   ObjectSetInteger(0, g_prefix + "BuyBar", OBJPROP_XSIZE, 1);
   ObjectSetInteger(0, g_prefix + "BuyBar", OBJPROP_YSIZE, (int)(20 * InpPanelScale));
   ObjectSetInteger(0, g_prefix + "BuyBar", OBJPROP_BGCOLOR, InpColorBuy);
   ObjectSetInteger(0, g_prefix + "BuyBar", OBJPROP_BORDER_TYPE, BORDER_FLAT);
   ObjectSetInteger(0, g_prefix + "BuyBar", OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, g_prefix + "BuyBar", OBJPROP_BACK, false);
   ObjectSetInteger(0, g_prefix + "BuyBar", OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, g_prefix + "BuyBar", OBJPROP_HIDDEN, true);
   
   ObjectCreate(0, g_prefix + "BuyLabel", OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, g_prefix + "BuyLabel", OBJPROP_XDISTANCE, x + (int)(15 * InpPanelScale));
   ObjectSetInteger(0, g_prefix + "BuyLabel", OBJPROP_YDISTANCE, y + (int)(80 * InpPanelScale));
   ObjectSetInteger(0, g_prefix + "BuyLabel", OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, g_prefix + "BuyLabel", OBJPROP_COLOR, InpColorText);
   ObjectSetInteger(0, g_prefix + "BuyLabel", OBJPROP_FONTSIZE, fs);
   ObjectSetString(0, g_prefix + "BuyLabel", OBJPROP_FONT, "Arial");
   ObjectSetString(0, g_prefix + "BuyLabel", OBJPROP_TEXT, "Compra: 0.0%");
   ObjectSetInteger(0, g_prefix + "BuyLabel", OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, g_prefix + "BuyLabel", OBJPROP_HIDDEN, true);
   
   ObjectCreate(0, g_prefix + "SellBar", OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(0, g_prefix + "SellBar", OBJPROP_XDISTANCE, x + (int)(10 * InpPanelScale));
   ObjectSetInteger(0, g_prefix + "SellBar", OBJPROP_YDISTANCE, y + (int)(105 * InpPanelScale));
   ObjectSetInteger(0, g_prefix + "SellBar", OBJPROP_XSIZE, 1);
   ObjectSetInteger(0, g_prefix + "SellBar", OBJPROP_YSIZE, (int)(20 * InpPanelScale));
   ObjectSetInteger(0, g_prefix + "SellBar", OBJPROP_BGCOLOR, InpColorSell);
   ObjectSetInteger(0, g_prefix + "SellBar", OBJPROP_BORDER_TYPE, BORDER_FLAT);
   ObjectSetInteger(0, g_prefix + "SellBar", OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, g_prefix + "SellBar", OBJPROP_BACK, false);
   ObjectSetInteger(0, g_prefix + "SellBar", OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, g_prefix + "SellBar", OBJPROP_HIDDEN, true);
   
   ObjectCreate(0, g_prefix + "SellLabel", OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, g_prefix + "SellLabel", OBJPROP_XDISTANCE, x + (int)(15 * InpPanelScale));
   ObjectSetInteger(0, g_prefix + "SellLabel", OBJPROP_YDISTANCE, y + (int)(130 * InpPanelScale));
   ObjectSetInteger(0, g_prefix + "SellLabel", OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, g_prefix + "SellLabel", OBJPROP_COLOR, InpColorText);
   ObjectSetInteger(0, g_prefix + "SellLabel", OBJPROP_FONTSIZE, fs);
   ObjectSetString(0, g_prefix + "SellLabel", OBJPROP_FONT, "Arial");
   ObjectSetString(0, g_prefix + "SellLabel", OBJPROP_TEXT, "Venda: 0.0%");
   ObjectSetInteger(0, g_prefix + "SellLabel", OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, g_prefix + "SellLabel", OBJPROP_HIDDEN, true);
   
   ObjectCreate(0, g_prefix + "EquilibriumLine", OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(0, g_prefix + "EquilibriumLine", OBJPROP_XDISTANCE, x + (int)(w/2));
   ObjectSetInteger(0, g_prefix + "EquilibriumLine", OBJPROP_YDISTANCE, y + (int)(55 * InpPanelScale));
   ObjectSetInteger(0, g_prefix + "EquilibriumLine", OBJPROP_XSIZE, 2);
   ObjectSetInteger(0, g_prefix + "EquilibriumLine", OBJPROP_YSIZE, (int)(70 * InpPanelScale));
   ObjectSetInteger(0, g_prefix + "EquilibriumLine", OBJPROP_BGCOLOR, InpColorNeutral);
   ObjectSetInteger(0, g_prefix + "EquilibriumLine", OBJPROP_BORDER_TYPE, BORDER_FLAT);
   ObjectSetInteger(0, g_prefix + "EquilibriumLine", OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, g_prefix + "EquilibriumLine", OBJPROP_BACK, false);
   ObjectSetInteger(0, g_prefix + "EquilibriumLine", OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, g_prefix + "EquilibriumLine", OBJPROP_HIDDEN, true);
   
   ObjectCreate(0, g_prefix + "BiasLabel", OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, g_prefix + "BiasLabel", OBJPROP_XDISTANCE, x + (int)(10 * InpPanelScale));
   ObjectSetInteger(0, g_prefix + "BiasLabel", OBJPROP_YDISTANCE, y + (int)(160 * InpPanelScale));
   ObjectSetInteger(0, g_prefix + "BiasLabel", OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, g_prefix + "BiasLabel", OBJPROP_COLOR, InpColorNeutral);
   ObjectSetInteger(0, g_prefix + "BiasLabel", OBJPROP_FONTSIZE, fs_bias);
   ObjectSetString(0, g_prefix + "BiasLabel", OBJPROP_FONT, "Arial Bold");
   ObjectSetString(0, g_prefix + "BiasLabel", OBJPROP_TEXT, "Neutro");
   ObjectSetInteger(0, g_prefix + "BiasLabel", OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, g_prefix + "BiasLabel", OBJPROP_HIDDEN, true);
   
   ChartRedraw();
}

//+------------------------------------------------------------------+
//| Atualiza o painel gráfico                                        |
//+------------------------------------------------------------------+
void UpdatePanel()
{
   double buy_pct = g_pressure.GetCurrentBuyPct();
   double sell_pct = g_pressure.GetCurrentSellPct();
   string bias = g_pressure.GetBiasLabel();
   
   int max_bar_width = (int)((g_scaled_width - 20 * InpPanelScale) / 2);
   int buy_bar_width = (int)(max_bar_width * buy_pct / 100.0);
   int sell_bar_width = (int)(max_bar_width * sell_pct / 100.0);
   
   ObjectSetInteger(0, g_prefix + "BuyBar", OBJPROP_XSIZE, buy_bar_width);
   ObjectSetInteger(0, g_prefix + "SellBar", OBJPROP_XSIZE, sell_bar_width);
   
   ObjectSetString(0, g_prefix + "BuyLabel", OBJPROP_TEXT, 
                   StringFormat("Compra: %.1f%% (Δ%.1f%%)", buy_pct, g_pressure.GetDeltaBuyPct()));
   ObjectSetString(0, g_prefix + "SellLabel", OBJPROP_TEXT, 
                   StringFormat("Venda: %.1f%% (Δ%.1f%%)", sell_pct, g_pressure.GetDeltaSellPct()));
   
   ObjectSetString(0, g_prefix + "BiasLabel", OBJPROP_TEXT, "Viés: " + bias);
   
   color bias_color = InpColorNeutral;
   if(StringFind(bias, "Compra") >= 0)
      bias_color = InpColorBuy;
   else if(StringFind(bias, "Venda") >= 0)
      bias_color = InpColorSell;
   
   ObjectSetInteger(0, g_prefix + "BiasLabel", OBJPROP_COLOR, bias_color);
   
   ChartRedraw();
}

//+------------------------------------------------------------------+
//| Deleta o painel gráfico                                          |
//+------------------------------------------------------------------+
void DeletePanel()
{
   ObjectDelete(0, g_prefix + "Background");
   ObjectDelete(0, g_prefix + "Title");
   ObjectDelete(0, g_prefix + "Window");
   ObjectDelete(0, g_prefix + "BuyBar");
   ObjectDelete(0, g_prefix + "BuyLabel");
   ObjectDelete(0, g_prefix + "SellBar");
   ObjectDelete(0, g_prefix + "SellLabel");
   ObjectDelete(0, g_prefix + "EquilibriumLine");
   ObjectDelete(0, g_prefix + "BiasLabel");
   
   ChartRedraw();
}
//+------------------------------------------------------------------+
