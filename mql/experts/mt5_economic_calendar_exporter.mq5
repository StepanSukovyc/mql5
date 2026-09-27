// Read-only MT5 Economic Calendar bridge. It never creates, changes, or closes orders.
#property strict
#property version "1.00"

input int RefreshSeconds = 300;
input int LookaheadHours = 24;
input string OutputFile = "economic_calendar.json"; // Terminal Common\Files

string EscapeJson(string value)
{
   StringReplace(value, "\\", "\\\\");
   StringReplace(value, "\"", "\\\"");
   StringReplace(value, "\r", "");
   StringReplace(value, "\n", " ");
   return value;
}

string IsoTime(datetime value)
{
   MqlDateTime parts;
   TimeToStruct(value, parts);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02d", parts.year, parts.mon, parts.day, parts.hour, parts.min, parts.sec);
}

string NumberOrNull(double value)
{
   if(value == EMPTY_VALUE)
      return "null";
   return DoubleToString(value, 8);
}

void ExportCalendar()
{
   datetime server_now = TimeTradeServer();
   datetime utc_now = TimeGMT();
   bool offset_trusted = (server_now > 0 && utc_now > 0);
   int server_offset_seconds = offset_trusted ? (int)(server_now - utc_now) : 0;
   MqlCalendarValue values[];
   datetime until = server_now + LookaheadHours * 3600;
   int total = CalendarValueHistory(values, server_now - 3600, until, NULL, NULL);
   int handle = FileOpen(OutputFile + ".tmp", FILE_WRITE|FILE_TXT|FILE_COMMON|FILE_ANSI);
   if(handle == INVALID_HANDLE)
   {
      Print("Calendar export open failed: ", GetLastError());
      return;
   }
   FileWriteString(handle, StringFormat("{\"schema_version\":1,\"generated_at_utc\":\"%sZ\",\"source\":\"MT5_ECONOMIC_CALENDAR\",\"server_utc_offset_seconds\":%d,\"events\":[", IsoTime(utc_now), server_offset_seconds));
   bool first = true;
   for(int index = 0; index < total; index++)
   {
      MqlCalendarEvent event;
      MqlCalendarCountry country;
      if(!CalendarEventById(values[index].event_id, event) || !CalendarCountryById(event.country_id, country))
         continue;
      datetime utc_time = values[index].time - server_offset_seconds;
      if(!first) FileWriteString(handle, ",");
      first = false;
      FileWriteString(handle, StringFormat("{\"event_id\":\"%I64u\",\"value_id\":\"%I64u\",\"country_id\":\"%I64d\",\"country_code\":\"%s\",\"currency\":\"%s\",\"event_name\":\"%s\",\"importance\":\"%s\",\"event_time_server\":\"%s\",\"event_time_utc\":%s,\"time_is_trusted\":%s,\"actual\":%s,\"forecast\":%s,\"previous\":%s,\"revised\":%s,\"status\":\"%s\",\"source\":\"MT5_ECONOMIC_CALENDAR\"}",
         values[index].event_id, values[index].id, event.country_id, EscapeJson(country.code), EscapeJson(country.currency), EscapeJson(event.name), EnumToString(event.importance), IsoTime(values[index].time),
         offset_trusted ? "\"" + IsoTime(utc_time) + "Z\"" : "null", offset_trusted ? "true" : "false", NumberOrNull(values[index].GetActualValue()), NumberOrNull(values[index].GetForecastValue()), NumberOrNull(values[index].GetPreviousValue()), NumberOrNull(values[index].GetRevisedValue()), values[index].GetActualValue() == EMPTY_VALUE ? "SCHEDULED" : "RELEASED"));
   }
   FileWriteString(handle, "]}");
   FileClose(handle);
   FileMove(OutputFile + ".tmp", FILE_COMMON, OutputFile, FILE_COMMON|FILE_REWRITE);
}

int OnInit()
{
   EventSetTimer(MathMax(1, RefreshSeconds));
   ExportCalendar();
   return(INIT_SUCCEEDED);
}
void OnTimer() { ExportCalendar(); }
void OnDeinit(const int reason) { EventKillTimer(); }