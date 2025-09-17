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
   double freeMargin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   double equity     = AccountInfoDouble(ACCOUNT_EQUITY);

   if(freeMargin > 0.2 * equity)
     {
      string predictPath = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Files\\" + predictFile;
      string analyzePath = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Files\\" + analyzeFile;

      if(!FileIsExist(predictFile))
        {
         int handle = FileOpen(analyzeFile, FILE_WRITE | FILE_TXT | FILE_ANSI);
         if(handle != INVALID_HANDLE)
           {
            FileWrite(handle, "{}");
            FileClose(handle);
           }
         return;
        }

      int handle = FileOpen(predictFile, FILE_READ | FILE_TXT | FILE_ANSI);
      if(handle == INVALID_HANDLE)
         return;

      string jsonText = "";
      while(!FileIsEnding(handle))
        {
         jsonText += FileReadString(handle) + "\n";
        }

      FileClose(handle);

      string symbol = ExtractJsonValue(jsonText, "symbol");
      string typ    = ExtractJsonValue(jsonText, "typ");
      string volumeStr = ExtractJsonValue(jsonText, "volume");

      double volume = defaultVolume;
      if(StringToDouble(volumeStr) > 0)
         volume = StringToDouble(volumeStr);

      bool result = false;

      if(typ == "BUY")
         result = trade.Buy(volume, symbol);
      else
         if(typ == "SELL")
            result = trade.Sell(volume, symbol);

      // Pokud se obchod podařil, smažeme soubor
      if(result)
        {
         if(FileIsExist(predictFile))
           {
            FileDelete(predictFile);
            Print("Soubor predict.json byl úspěšně odstraněn po otevření obchodu.");
           }
        }
     }
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
string TrimString(string str)
  {
// Odstranění mezer zleva
   while(StringLen(str) > 0 && StringGetCharacter(str, 0) == ' ')
      str = StringSubstr(str, 1);

// Odstranění mezer zprava
   while(StringLen(str) > 0 && StringGetCharacter(str, StringLen(str) - 1) == ' ')
      str = StringSubstr(str, 0, StringLen(str) - 1);

   return str;
  }

// Pomocná funkce pro extrakci hodnoty z JSON stringu
string ExtractJsonValue(string json, string key)
  {
   int keyPos = StringFind(json, "\"" + key + "\"");
   if(keyPos < 0)
      return "";

   int colonPos = StringFind(json, ":", keyPos);
   if(colonPos < 0)
      return "";

   int valueStart = colonPos + 1;

// Pokud hodnota je string (např. "BUY")
   if(StringGetCharacter(json, valueStart) == '\"')
     {
      int quoteStart = StringFind(json, "\"", valueStart);
      int quoteEnd = StringFind(json, "\"", quoteStart + 1);
      if(quoteStart < 0 || quoteEnd < 0)
         return "";
      return StringSubstr(json, quoteStart + 1, quoteEnd - quoteStart - 1);
     }
   else
     {
      // Pokud hodnota je číslo (např. volume)
      int commaPos = StringFind(json, ",", valueStart);
      int bracePos = StringFind(json, "}", valueStart);
      int endPos = (commaPos > 0) ? commaPos : bracePos;
      if(endPos < 0)
         return "";
      return TrimString(StringSubstr(json, valueStart, endPos - valueStart));
     }
  }

//+------------------------------------------------------------------+
