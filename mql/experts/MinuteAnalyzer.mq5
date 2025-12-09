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
   double equity     = AccountInfoDouble(ACCOUNT_BALANCE);

   Print("=== TIMER START ===");
   Print("freeMargin:", freeMargin, "; equity:", equity);

   if(freeMargin > 0.2 * equity)
     {
      string predictPath = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Files\\" + predictFile;
      string fileToUse = predictPath;

      // Nejprve zkusíme absolutní cestu
      if(!FileIsExist(predictPath))
        {
         Print("Soubor na absolutní cestě nenalezen, zkouším relativní cestu...");
         if(FileIsExist(predictFile))
           {
            fileToUse = predictFile;
            Print("Soubor nalezen v MQL5\\Files pomocí relativní cesty.");
           }
         else
           {
            Print("Soubor predict.json neexistuje ani na absolutní, ani na relativní cestě.");
            // Vytvoříme analyze.json
            string analyzePath = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Files\\" + analyzeFile;
            string analyzeToUse = analyzePath;

            // Nejprve zkusíme absolutní cestu
            bool ok = WriteAnalyzeJsonToFile(analyzeToUse);
            if(!ok)
            {
              Print("Absolutní cesta selhala, zkouším relativní...");
              ok = WriteAnalyzeJsonToFile(analyzeFile);
            }

            if(ok)
              Print("Soubor analyze.json byl vytvořen a vyplněn aktuálními daty.");
            else
              Print("Chyba: nelze vytvořit analyze.json. Error:", _LastError);
           }
        }

      // ✅ Otevření souboru
      int handle = FileOpen(fileToUse, FILE_READ | FILE_TXT | FILE_ANSI);

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
      else
         if(StringCompare(typ, "SELL") == 0)
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
         if(FileIsExist(fileToUse))
           {
            FileDelete(fileToUse);
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
   if(keyPos < 0)
      return "";
   int colonPos = StringFind(json, ":", keyPos);
   if(colonPos < 0)
      return "";
   int valueStart = colonPos + 1;

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
      int commaPos = StringFind(json, ",", valueStart);
      int bracePos = StringFind(json, "}", valueStart);
      int endPos = (commaPos > 0) ? commaPos : bracePos;
      if(endPos < 0)
         return "";
      return TrimString(StringSubstr(json, valueStart, endPos - valueStart));
     }
  }
//+------------------------------------------------------------------+

// Sestaví JSON s informacemi o účtu a otevřených pozicích
string BuildAnalyzeJson()
{
   // --- Účet ---
   double balance    = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity     = AccountInfoDouble(ACCOUNT_EQUITY);
   double margin     = AccountInfoDouble(ACCOUNT_MARGIN);
   double marginFree = AccountInfoDouble(ACCOUNT_MARGIN_FREE);

   string json = 
      "{\n"
      "  \"account\": {\n"
      "    \"balance\": "     + DoubleToString(balance, 2)    + ",\n"
      "    \"equity\": "      + DoubleToString(equity, 2)     + ",\n"
      "    \"margin\": "      + DoubleToString(margin, 2)     + ",\n"
      "    \"margin_free\": " + DoubleToString(marginFree, 2) + "\n"
      "  },\n"
      "  \"positions\": [";

   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket))
      {
         string  symbol       = PositionGetString(POSITION_SYMBOL);
         long    type         = PositionGetInteger(POSITION_TYPE); // BUY/SELL
         double  volume       = PositionGetDouble(POSITION_VOLUME);
         datetime tOpen       = (datetime)PositionGetInteger(POSITION_TIME);
         double  priceOpen    = PositionGetDouble(POSITION_PRICE_OPEN);
         double  priceCurrent = PositionGetDouble(POSITION_PRICE_CURRENT); // aktuální cena
         double  profit       = PositionGetDouble(POSITION_PROFIT);
         double  commission   = PositionGetDouble(POSITION_COMMISSION);
         double  swap         = PositionGetDouble(POSITION_SWAP);

         // Formátování
         int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
         string typeStr  = (type == POSITION_TYPE_BUY ? "BUY" : "SELL");
         string timeStr  = TimeToString(tOpen, TIME_DATE | TIME_SECONDS);

         json += 
            "\n    {"
            "\"symbol\":\""        + symbol                           + "\","
            "\"ticket\":"          + IntegerToString((long)ticket)    + ","
            "\"type\":\""          + typeStr                          + "\","
            "\"volume\":"          + DoubleToString(volume, 2)        + ","
            "\"time_open\":\""     + timeStr                          + "\","
            "\"price_open\":"      + DoubleToString(priceOpen, digits)+ ","
            "\"price_current\":"   + DoubleToString(priceCurrent, digits) + ","
            "\"profit\":"          + DoubleToString(profit, 2)        + ","
            "\"commission\":"      + DoubleToString(commission, 2)    + ","
            "\"swap\":"            + DoubleToString(swap, 2)          +
            "}";

         if(i < total - 1) json += ",";
      }
   }

   json += "\n  ]\n}";
   return json;
}

// Zapíše analyze.json do dané cesty (UTF-8)
bool WriteAnalyzeJsonToFile(const string analyzePath)
{
   string payload = BuildAnalyzeJson();

   // UTF-8 kvůli spolehlivému textovému zápisu
   int handle = FileOpen(analyzePath, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE)
      return false;

   // Zápis celého JSONu bez automatické separace
   FileWriteString(handle, payload);
   FileClose(handle);
   return true;
}
