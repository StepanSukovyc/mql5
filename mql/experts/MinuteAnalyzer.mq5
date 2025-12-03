
//+------------------------------------------------------------------+
//|                                                   MinuteAnalyzer |
//|                              Copyright 2025, Mgr. Stepan Sukovyc |
//+------------------------------------------------------------------+
#include <Trade\Trade.mqh>
CTrade trade;

input string predictFile = "predict.json";
input string analyzeFile = "analyze.json";
input double defaultVolume = 0.01;
input int TimerInterval = 60; // každou minutu

//+------------------------------------------------------------------+
void OnInit()
  {
   EventSetTimer(TimerInterval);
   OnTimer(); // Spustí se ihned po nasazení
  }
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
  }
//+------------------------------------------------------------------+
void OnTimer()
  {
   double freeMargin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   double equity     = AccountInfoDouble(ACCOUNT_EQUITY);

   Print("=== TIMER START ===");
   Print("freeMargin:", freeMargin, "; equity:", equity);

   if(freeMargin > 0.2 * equity)
     {
      string predictPath = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Files\\" + predictFile;
      string analyzePath = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Files\\" + analyzeFile;

      if(!FileIsExist(predictPath))
        {
         Print("Soubor predict.json neexistuje, vytvářím analyze.json...");
         int handle = FileOpen(analyzePath, FILE_WRITE | FILE_TXT | FILE_ANSI);
         if(handle != INVALID_HANDLE)
           {
            FileWrite(handle, "{}");
            FileClose(handle);
           }
         return;
        }

      int handle = FileOpen(predictPath, FILE_READ | FILE_TXT | FILE_ANSI);
      if(handle == INVALID_HANDLE)
        {
         Print("Chyba: nelze otevřít predict.json. Error:", _LastError);
         return;
        }

      string jsonText = "";
      while(!FileIsEnding(handle))
        {
         jsonText += FileReadString(handle) + "\n";
        }
      FileClose(handle);

      string symbol    = CleanString(ExtractJsonValue(jsonText, "symbol"));
      string typ       = CleanString(ExtractJsonValue(jsonText, "typ"));
      string volumeStr = CleanString(ExtractJsonValue(jsonText, "volume"));

      double account_balance = AccountInfoDouble(ACCOUNT_BALANCE);
      double volume = ((int)(account_balance / 500) + 1) * 0.01;
      if(StringToDouble(volumeStr) > 0)
         volume = StringToDouble(volumeStr);

      Print("SYMBOL:", symbol, "; TYP:", typ, "; VOLUME:", volume);

      // ✅ Kontrola symbolu
      if(!SymbolSelect(symbol, true))
        {
         Print("Chyba: Symbol nelze aktivovat:", symbol);
         return;
        }

      bool result = false;
      ResetLastError(); // vyčistí předchozí chyby

      if(StringCompare(typ, "BUY") == 0)
        {
         result = trade.Buy(volume, symbol);
         Print("BUY result:", result, "; Error:", _LastError);
        }
      else if(StringCompare(typ, "SELL") == 0)
        {
         result = trade.Sell(volume, symbol);
         Print("SELL result:", result, "; Error:", _LastError);
        }
      else
        {
         Print("Neznámý typ obchodu:", typ);
         return;
        }

      // ✅ Pokud se obchod podařil, smažeme soubor
      if(result)
        {
         if(FileIsExist(predictPath))
           {
            FileDelete(predictPath);
            Print("Soubor predict.json byl úspěšně odstraněn po otevření obchodu.");
           }
      }
      else
        {
         Print("Obchod se nepodařil. Error:", _LastError);
        }
     }
   else
      Print("Margin pod limitem, obchod nebude proveden.");
   Print("=== TIMER END ===");
  }
//+------------------------------------------------------------------+
string CleanString(string s)
  {
   s = TrimString(s);
   StringReplace(s, "\"", "");
   StringReplace(s, "\r", "");
   StringReplace(s, "\n", "");
   return s;
  }
//+------------------------------------------------------------------+
string TrimString(string str)
  {
   while(StringLen(str) > 0 && StringGetCharacter(str, 0) == ' ')
      str = StringSubstr(str, 1);
   while(StringLen(str) > 0 && StringGetCharacter(str, StringLen(str) - 1) == ' ')
      str = StringSubstr(str, 0, StringLen(str) - 1);
   return str;
  }
//+------------------------------------------------------------------+
string ExtractJsonValue(string json, string key)
  {
   int keyPos = StringFind(json, "\"" + key + "\"");
   if(keyPos < 0) return "";
   int colonPos = StringFind(json, ":", keyPos);
   if(colonPos < 0) return "";
   int valueStart = colonPos + 1;

   if(StringGetCharacter(json, valueStart) == '\"')
     {
      int quoteStart = StringFind(json, "\"", valueStart);
      int quoteEnd = StringFind(json, "\"", quoteStart + 1);
      if(quoteStart < 0 || quoteEnd < 0) return "";
      return StringSubstr(json, quoteStart + 1, quoteEnd - quoteStart - 1);
     }
   else
     {
      int commaPos = StringFind(json, ",", valueStart);
      int bracePos = StringFind(json, "}", valueStart);
      int endPos = (commaPos > 0) ? commaPos : bracePos;
      if(endPos < 0) return "";
      return TrimString(StringSubstr(json, valueStart, endPos - valueStart));
     }
  }
//+------------------------------------------------------------------+
