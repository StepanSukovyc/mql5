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
//string pairs[] = {"NZDJPY", "GBPUSD", "GBPNZD", "EURJPY", "EURGBP", "EURNZD"};

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void OnInit()
  {
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
   string pairs[];
   GetValidSymbols(pairs);

   datetime now = TimeCurrent();
   datetime from = now - 30 * 24 * 60 * 60; // posledních 30 dní

   int groupSize = 6;
   int totalPairs = ArraySize(pairs);
   int fileIndex = 0;

   for(int i = 0; i < totalPairs; i += groupSize)
     {
      CJAVal json(jtOBJ, "");

      for(int k = 0; k < groupSize && (i + k) < totalPairs; k++)
        {
         string symbol = pairs[i + k] + suffix;
         MqlRates rates[];

         int copied = CopyRates(symbol, PERIOD_D1, from, now, rates);

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
              }

            CJAVal *target = json[symbol];
            if(target != NULL)
              {
               target.Set(pairData);
              }
           }
        }

      string jsonText = json.Serialize();
      string filename = StringFormat("tHistory_%d.json", fileIndex);
      SaveToFile(filename, jsonText);
      fileIndex++;
     }
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
     }
   else
     {
      Print("Nelze otevřít soubor: ", filename);
     }
  }
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
//| Vrací pole měnových párů s 6 písmeny po odstranění suffix        |
//+------------------------------------------------------------------+
void GetValidSymbols(string &pairs[])
  {
   int total = SymbolsTotal(false); // false = pouze symboly v Market Watch, jinak symboly dostupné v otevřených oknech

   for(int i = 0; i < total; i++)
     {
      string symbol = SymbolName(i, false);
      bool cleaned = StringReplace(symbol, suffix, "");

      if(StringLen(symbol) == 6 && IsAlpha(symbol))
        {
         int size = ArraySize(pairs);
         ArrayResize(pairs, size + 1);
         pairs[size] = symbol;
        }
     }
  }
//+------------------------------------------------------------------+
// Pomocná funkce pro kontrolu, zda je řetězec tvořen pouze písmeny
bool IsAlpha(string text)
  {
   for(int i = 0; i < StringLen(text); i++)
     {
      ushort ch = StringGetCharacter(text, i);
      if((ch < 'A' || ch > 'Z') && (ch < 'a' || ch > 'z'))
         return false;
     }
   return true;
  }
//+------------------------------------------------------------------+
