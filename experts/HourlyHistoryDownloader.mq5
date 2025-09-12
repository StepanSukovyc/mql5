//+------------------------------------------------------------------+
//|                                                      ProjectName |
//|                                      Copyright 2020, CompanyName |
//|                                       http://www.companyname.net |
//+------------------------------------------------------------------+

#include <Files\FileTxt.mqh>
#include <Trade\SymbolInfo.mqh>
#include <jason.mqh>

input int TimerInterval = 3600; // každou hodinu
string suffix = "_ecn";
string pairs[] = {"NZDJPY", "GBPUSD", "GBPNZD", "EURJPY", "EURGBP", "EURNZD"};

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void OnInit()
  {
   Print("EA inicializován");
   EventSetTimer(TimerInterval);
   OnTimer(); // Spustí se ihned po nasazení
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void OnTimer()
  {
   datetime now = TimeCurrent();
   datetime from = now - 30 * 24 * 60 * 60; // posledních 30 dní


   CJAVal json(jtOBJ, "");

   for(int i = 0; i < ArraySize(pairs); i++)
     {
      string symbol = pairs[i] + suffix;
      MqlRates rates[];

      Print("Načítám ", symbol);
      int copied = CopyRates(symbol, PERIOD_D1, from, now, rates);
      Print("Počet svíček pro ", symbol, ": ", copied);

      if(copied > 0)
        {
         CJAVal pairData(jtARRAY, "");

         for(int j = 0; j < ArraySize(rates); j++)
           {
            CJAVal candle(jtOBJ, "");
            candle["time"]   = (int)rates[j].time;
            candle["open"]   = rates[j].open;
            candle["high"]   = rates[j].high;
            candle["low"]    = rates[j].low;
            candle["close"]  = rates[j].close;
            candle["volume"] = rates[j].tick_volume;
            pairData.Add(candle);
            Print("Přidávám svíčku: ", candle.Serialize());
           }

         CJAVal *target = json[symbol];
         if(target != NULL)
           {
            target.Set(pairData);
           }
        }
     }

   string jsonText = json.Serialize();
   Print("JSON výstup: ", jsonText);
   SaveToFile("tHistory.json", jsonText);

  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void SaveToFile(string filename, string content)
  {
   int handle = FileOpen(filename, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(handle != INVALID_HANDLE)
     {
      FileWrite(handle, content);
      FileClose(handle);
      Print("Soubor uložen: ", filename);
     }
   else
     {
      Print("Nelze otevřít soubor: ", filename);
     }
  }
//+------------------------------------------------------------------+
