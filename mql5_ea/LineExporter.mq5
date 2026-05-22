//+------------------------------------------------------------------+
//|  LineExporter.mq5                                                 |
//|                                                                   |
//|  MT5-Python Trading Dashboard — Phase 2 SPEC §9                  |
//|                                                                   |
//|  Walks every open chart, enumerates supported drawing objects,    |
//|  and writes one lines_{SYMBOL}.json file per symbol to the        |
//|  terminal's Common\Files folder (FILE_COMMON), where Python's     |
//|  analyzer/line_reader.py picks them up via watchdog.              |
//|                                                                   |
//|  Triggers (SPEC §9.4: 1 s reflection target):                     |
//|    OnChartEvent  — immediate for the host chart                   |
//|    OnTimer  1 s  — polls every open chart for off-host updates    |
//|    OnTimer  5 s  — full integrity scan (no diff guard)            |
//|                                                                   |
//|  Supported object types (SPEC §9.1):                              |
//|    OBJ_TREND, OBJ_HLINE, OBJ_RECTANGLE,                           |
//|    OBJ_CHANNEL, OBJ_FIBO, OBJ_TEXT                                |
//+------------------------------------------------------------------+
#property copyright   "MT5-Python Trading Dashboard"
#property link        "local"
#property version     "1.00"
#property strict
#property description "Exports drawn TL/SR objects from every open chart as JSON for the dashboard."

input string  InpFilePrefix        = "lines_";    // Output filename prefix
input string  InpFileSuffix        = ".json";     // Output filename suffix
input int     InpFastPollSec       = 1;           // Poll-all-charts cadence
input int     InpIntegritySec      = 5;           // Full rescan cadence (SPEC §9.1)
input bool    InpVerbose           = false;       // Extra logging

//--- internal state ----------------------------------------------------------
// Per-symbol cache of the last JSON we wrote — we only rewrite when the
// content actually changed, so the watchdog on the Python side does not see
// pointless events.
struct SymbolPayload
  {
   string symbol;
   string payload;
  };
SymbolPayload g_cache[];

datetime g_lastIntegrity = 0;

//+------------------------------------------------------------------+
//|  Lifecycle                                                       |
//+------------------------------------------------------------------+
int OnInit()
  {
   EventSetTimer(InpFastPollSec);
   PrintFormat("LineExporter: started (fast=%ds, integrity=%ds)",
               InpFastPollSec, InpIntegritySec);
   ExportAllCharts(true);
   // Stamp the integrity clock so the very first OnTimer call does not
   // immediately fire a second full export (which would double-write every
   // file and pointlessly wake the Python watchdog twice).
   g_lastIntegrity = TimeCurrent();
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   PrintFormat("LineExporter: deinit reason=%d", reason);
  }

void OnTimer()
  {
   bool forceFull = false;
   datetime now = TimeCurrent();
   if(now - g_lastIntegrity >= InpIntegritySec)
     {
      forceFull = true;
      g_lastIntegrity = now;
     }
   ExportAllCharts(forceFull);
  }

void OnChartEvent(const int id,
                  const long &lparam,
                  const double &dparam,
                  const string &sparam)
  {
   // React to any object lifecycle event on the host chart.
   if(id == CHARTEVENT_OBJECT_CREATE
      || id == CHARTEVENT_OBJECT_CHANGE
      || id == CHARTEVENT_OBJECT_DELETE
      || id == CHARTEVENT_OBJECT_DRAG)
     {
      ExportChart(ChartID(), false);
     }
  }

//+------------------------------------------------------------------+
//|  Top-level export                                                |
//+------------------------------------------------------------------+
void ExportAllCharts(const bool forceWrite)
  {
   long chart_id = ChartFirst();
   int seen = 0;
   while(chart_id >= 0)
     {
      ExportChart(chart_id, forceWrite);
      chart_id = ChartNext(chart_id);
      seen++;
      if(seen > 256) break;  // hard guard against runaway
     }
  }

void ExportChart(const long chart_id, const bool forceWrite)
  {
   string symbol = ChartSymbol(chart_id);
   if(symbol == "") return;

   string payload = BuildJsonForChart(chart_id, symbol);

   // Skip the write if the content is identical to what we last wrote for
   // this symbol. Reduces watchdog churn dramatically when the user is
   // simply panning/zooming without editing objects.
   if(!forceWrite && CacheEquals(symbol, payload))
      return;

   string fname = InpFilePrefix + symbol + InpFileSuffix;
   if(!WriteCommonFile(fname, payload))
     {
      PrintFormat("LineExporter: WriteCommonFile failed for %s (err=%d)",
                  fname, GetLastError());
      return;
     }
   CachePut(symbol, payload);
   if(InpVerbose)
      PrintFormat("LineExporter: wrote %s (%d bytes)", fname, StringLen(payload));
  }

//+------------------------------------------------------------------+
//|  JSON building                                                   |
//+------------------------------------------------------------------+
string BuildJsonForChart(const long chart_id, const string symbol)
  {
   string horizontal = "";
   string trendlines = "";
   string rectangles = "";
   string channels   = "";
   string fibos      = "";
   string texts      = "";

   int total = ObjectsTotal(chart_id, -1, -1);
   for(int i = 0; i < total; i++)
     {
      string name = ObjectName(chart_id, i, -1, -1);
      if(name == "") continue;
      int type = (int)ObjectGetInteger(chart_id, name, OBJPROP_TYPE);
      switch(type)
        {
         case OBJ_HLINE:
            AppendObject(horizontal, BuildHLineJson(chart_id, name));
            break;
         case OBJ_TREND:
            AppendObject(trendlines, BuildTrendlineJson(chart_id, name));
            break;
         case OBJ_RECTANGLE:
            AppendObject(rectangles, BuildRectangleJson(chart_id, name));
            break;
         case OBJ_CHANNEL:
            AppendObject(channels, BuildChannelJson(chart_id, name));
            break;
         case OBJ_FIBO:
            AppendObject(fibos, BuildFibonacciJson(chart_id, name));
            break;
         case OBJ_TEXT:
            AppendObject(texts, BuildTextJson(chart_id, name));
            break;
         default:
            break;
        }
     }

   string lines =
      "    \"horizontal\":["  + horizontal + "],\n"
    + "    \"trendlines\":["  + trendlines + "],\n"
    + "    \"rectangles\":["  + rectangles + "],\n"
    + "    \"channels\":["    + channels   + "],\n"
    + "    \"fibonacci\":["   + fibos      + "],\n"
    + "    \"texts\":["       + texts      + "]";

   string out = "{\n"
              + "  \"symbol\":" + JsonString(symbol) + ",\n"
              + "  \"updated_at\":" + JsonString(IsoTime(TimeCurrent())) + ",\n"
              + "  \"lines\":{\n" + lines + "\n  }\n"
              + "}\n";
   return out;
  }

string BuildHLineJson(const long chart_id, const string name)
  {
   double price = ObjectGetDouble(chart_id, name, OBJPROP_PRICE, 0);
   string color = ColorToHex((color)ObjectGetInteger(chart_id, name, OBJPROP_COLOR));
   string s = "{"
            + "\"name\":" + JsonString(name)
            + ",\"price\":" + JsonNumber(price)
            + ",\"color\":" + JsonString(color)
            + "}";
   return s;
  }

string BuildTrendlineJson(const long chart_id, const string name)
  {
   datetime t1 = (datetime)ObjectGetInteger(chart_id, name, OBJPROP_TIME, 0);
   datetime t2 = (datetime)ObjectGetInteger(chart_id, name, OBJPROP_TIME, 1);
   double p1 = ObjectGetDouble(chart_id, name, OBJPROP_PRICE, 0);
   double p2 = ObjectGetDouble(chart_id, name, OBJPROP_PRICE, 1);
   bool ray = (bool)ObjectGetInteger(chart_id, name, OBJPROP_RAY_RIGHT);
   string color = ColorToHex((color)ObjectGetInteger(chart_id, name, OBJPROP_COLOR));

   // Linear interpolation at "now" so Python can render the current value
   // without knowing the symbol's bar arithmetic.
   datetime now = TimeCurrent();
   double current = ExtrapolatedPriceAt(t1, p1, t2, p2, now);
   double slopePerDay = SlopePerDay(t1, p1, t2, p2);

   string s = "{"
            + "\"name\":" + JsonString(name)
            + ",\"point1\":[" + JsonString(IsoTime(t1)) + "," + JsonNumber(p1) + "]"
            + ",\"point2\":[" + JsonString(IsoTime(t2)) + "," + JsonNumber(p2) + "]"
            + ",\"ray_right\":" + (ray ? "true" : "false")
            + ",\"current_value\":" + JsonNumber(current)
            + ",\"slope_per_day\":" + JsonNumber(slopePerDay)
            + ",\"color\":" + JsonString(color)
            + "}";
   return s;
  }

string BuildRectangleJson(const long chart_id, const string name)
  {
   datetime t1 = (datetime)ObjectGetInteger(chart_id, name, OBJPROP_TIME, 0);
   datetime t2 = (datetime)ObjectGetInteger(chart_id, name, OBJPROP_TIME, 1);
   double p1 = ObjectGetDouble(chart_id, name, OBJPROP_PRICE, 0);
   double p2 = ObjectGetDouble(chart_id, name, OBJPROP_PRICE, 1);
   string color = ColorToHex((color)ObjectGetInteger(chart_id, name, OBJPROP_COLOR));

   double price_low  = MathMin(p1, p2);
   double price_high = MathMax(p1, p2);
   string s = "{"
            + "\"name\":" + JsonString(name)
            + ",\"time1\":" + JsonString(IsoTime(t1))
            + ",\"time2\":" + JsonString(IsoTime(t2))
            + ",\"price_low\":" + JsonNumber(price_low)
            + ",\"price_high\":" + JsonNumber(price_high)
            + ",\"color\":" + JsonString(color)
            + "}";
   return s;
  }

string BuildChannelJson(const long chart_id, const string name)
  {
   datetime t1 = (datetime)ObjectGetInteger(chart_id, name, OBJPROP_TIME, 0);
   datetime t2 = (datetime)ObjectGetInteger(chart_id, name, OBJPROP_TIME, 1);
   datetime t3 = (datetime)ObjectGetInteger(chart_id, name, OBJPROP_TIME, 2);
   double p1 = ObjectGetDouble(chart_id, name, OBJPROP_PRICE, 0);
   double p2 = ObjectGetDouble(chart_id, name, OBJPROP_PRICE, 1);
   double p3 = ObjectGetDouble(chart_id, name, OBJPROP_PRICE, 2);
   bool ray = (bool)ObjectGetInteger(chart_id, name, OBJPROP_RAY_RIGHT);
   string color = ColorToHex((color)ObjectGetInteger(chart_id, name, OBJPROP_COLOR));

   datetime now = TimeCurrent();
   double mainNow     = ExtrapolatedPriceAt(t1, p1, t2, p2, now);
   double parallelNow = ExtrapolatedPriceAt(t3, p3, t3, p3, now);  // p3 anchor
   // Real parallel value: same slope as main, offset = p3 - main_at(t3)
   double mainAtT3 = ExtrapolatedPriceAt(t1, p1, t2, p2, t3);
   double offset = p3 - mainAtT3;
   parallelNow = mainNow + offset;

   string s = "{"
            + "\"name\":" + JsonString(name)
            + ",\"main_point1\":[" + JsonString(IsoTime(t1)) + "," + JsonNumber(p1) + "]"
            + ",\"main_point2\":[" + JsonString(IsoTime(t2)) + "," + JsonNumber(p2) + "]"
            + ",\"parallel_anchor\":[" + JsonString(IsoTime(t3)) + "," + JsonNumber(p3) + "]"
            + ",\"ray_right\":" + (ray ? "true" : "false")
            + ",\"main_value\":" + JsonNumber(mainNow)
            + ",\"parallel_value\":" + JsonNumber(parallelNow)
            + ",\"color\":" + JsonString(color)
            + "}";
   return s;
  }

string BuildFibonacciJson(const long chart_id, const string name)
  {
   datetime t1 = (datetime)ObjectGetInteger(chart_id, name, OBJPROP_TIME, 0);
   datetime t2 = (datetime)ObjectGetInteger(chart_id, name, OBJPROP_TIME, 1);
   double p1 = ObjectGetDouble(chart_id, name, OBJPROP_PRICE, 0);
   double p2 = ObjectGetDouble(chart_id, name, OBJPROP_PRICE, 1);
   int levels = (int)ObjectGetInteger(chart_id, name, OBJPROP_LEVELS);
   string levels_json = "";
   for(int i = 0; i < levels; i++)
     {
      double lv = ObjectGetDouble(chart_id, name, OBJPROP_LEVELVALUE, i);
      string desc = ObjectGetString(chart_id, name, OBJPROP_LEVELTEXT, i);
      double absPrice = p1 + (p2 - p1) * lv;
      string l = "{"
               + "\"ratio\":" + JsonNumber(lv)
               + ",\"label\":" + JsonString(desc)
               + ",\"price\":" + JsonNumber(absPrice)
               + "}";
      AppendObject(levels_json, l);
     }
   string color = ColorToHex((color)ObjectGetInteger(chart_id, name, OBJPROP_COLOR));
   string s = "{"
            + "\"name\":" + JsonString(name)
            + ",\"point1\":[" + JsonString(IsoTime(t1)) + "," + JsonNumber(p1) + "]"
            + ",\"point2\":[" + JsonString(IsoTime(t2)) + "," + JsonNumber(p2) + "]"
            + ",\"levels\":[" + levels_json + "]"
            + ",\"color\":" + JsonString(color)
            + "}";
   return s;
  }

string BuildTextJson(const long chart_id, const string name)
  {
   datetime t = (datetime)ObjectGetInteger(chart_id, name, OBJPROP_TIME, 0);
   double p = ObjectGetDouble(chart_id, name, OBJPROP_PRICE, 0);
   string text = ObjectGetString(chart_id, name, OBJPROP_TEXT);
   string color = ColorToHex((color)ObjectGetInteger(chart_id, name, OBJPROP_COLOR));
   string s = "{"
            + "\"name\":" + JsonString(name)
            + ",\"time\":" + JsonString(IsoTime(t))
            + ",\"price\":" + JsonNumber(p)
            + ",\"text\":" + JsonString(text)
            + ",\"color\":" + JsonString(color)
            + "}";
   return s;
  }

//+------------------------------------------------------------------+
//|  File output                                                     |
//+------------------------------------------------------------------+
bool WriteCommonFile(const string fname, const string payload)
  {
   int h = FileOpen(fname,
                    FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(h == INVALID_HANDLE) return false;
   FileWriteString(h, payload);
   FileClose(h);
   return true;
  }

//+------------------------------------------------------------------+
//|  Cache (only rewrite when content changed)                        |
//+------------------------------------------------------------------+
int CacheIndex(const string symbol)
  {
   for(int i = 0; i < ArraySize(g_cache); i++)
      if(g_cache[i].symbol == symbol) return i;
   return -1;
  }

bool CacheEquals(const string symbol, const string payload)
  {
   int idx = CacheIndex(symbol);
   if(idx < 0) return false;
   return g_cache[idx].payload == payload;
  }

void CachePut(const string symbol, const string payload)
  {
   int idx = CacheIndex(symbol);
   if(idx < 0)
     {
      int n = ArraySize(g_cache);
      ArrayResize(g_cache, n + 1);
      g_cache[n].symbol = symbol;
      g_cache[n].payload = payload;
     }
   else
     {
      g_cache[idx].payload = payload;
     }
  }

//+------------------------------------------------------------------+
//|  Small JSON / number helpers                                     |
//+------------------------------------------------------------------+
string JsonString(const string s)
  {
   string r = "\"";
   int n = StringLen(s);
   for(int i = 0; i < n; i++)
     {
      ushort c = StringGetCharacter(s, i);
      switch(c)
        {
         case '\\': r += "\\\\"; break;
         case '"':  r += "\\\""; break;
         case '\n': r += "\\n";  break;
         case '\r': r += "\\r";  break;
         case '\t': r += "\\t";  break;
         default:   r += ShortToString((ushort)c); break;
        }
     }
   r += "\"";
   return r;
  }

string JsonNumber(const double v)
  {
   return DoubleToString(v, 8);
  }

string IsoTime(const datetime t)
  {
   // SPEC alignment: MetaTrader5 Python returns bar times as UTC-tagged
   // epochs (see analyzer/mt5_connector.py copy_rates), and so do we here
   // by converting the broker server time to GMT. The output is suffixed
   // with "Z" so Python can parse it as a tz-aware UTC ISO 8601 string
   // regardless of which timezone the broker server runs in.
   datetime gmt;
   if(t == TimeCurrent())
      gmt = TimeGMT();
   else
      gmt = t - (TimeCurrent() - TimeGMT());   // shift server→GMT
   MqlDateTime mdt;
   TimeToStruct(gmt, mdt);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02dZ",
                       mdt.year, mdt.mon, mdt.day,
                       mdt.hour, mdt.min, mdt.sec);
  }

string ColorToHex(const color c)
  {
   int v = (int)c;
   int r = (v) & 0xFF;
   int g = (v >> 8) & 0xFF;
   int b = (v >> 16) & 0xFF;
   return StringFormat("#%02X%02X%02X", r, g, b);
  }

void AppendObject(string &accum, const string item)
  {
   if(StringLen(accum) > 0) accum += ",";
   accum += "\n      " + item;
  }

double ExtrapolatedPriceAt(const datetime t1, const double p1,
                           const datetime t2, const double p2,
                           const datetime at)
  {
   if(t1 == t2) return p1;
   double dt = (double)(t2 - t1);
   double slope = (p2 - p1) / dt;
   return p1 + slope * (double)(at - t1);
  }

double SlopePerDay(const datetime t1, const double p1,
                   const datetime t2, const double p2)
  {
   if(t1 == t2) return 0.0;
   double dt_days = ((double)(t2 - t1)) / 86400.0;
   if(dt_days == 0.0) return 0.0;
   return (p2 - p1) / dt_days;
  }
//+------------------------------------------------------------------+
