#property copyright "ForexAI"
#property version   "1.00"
#property strict
#property service

input int RefreshSeconds = 300;

string ExportDirectory = "ForexAI";
string ExportFile = "ForexAI\\symbol_sessions.csv";

void OnStart()
  {
   while(!IsStopped())
     {
      ExportSymbolSessions();
      int wait_seconds=MathMax(RefreshSeconds,10);
      for(int elapsed=0; elapsed<wait_seconds && !IsStopped(); elapsed++)
         Sleep(1000);
     }
  }

bool ExportSymbolSessions()
  {
   long generated_server_epoch=(long)TimeTradeServer();
   if(generated_server_epoch<=0)
     {
      Print("ForexAI session export skipped: trade server time unavailable");
      return false;
     }

   FolderCreate(ExportDirectory,FILE_COMMON);
   int handle=FileOpen(
      ExportFile,
      FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON,
      ';'
   );
   if(handle==INVALID_HANDLE)
     {
      PrintFormat("ForexAI session export failed: error=%d",GetLastError());
      return false;
     }

   FileWrite(handle,"FOREXAI_SESSION_EXPORT_V1");
   FileWrite(handle,"generated_server_epoch",generated_server_epoch);

   int symbol_count=SymbolsTotal(true);
   for(int symbol_index=0; symbol_index<symbol_count; symbol_index++)
     {
      string symbol=SymbolName(symbol_index,true);
      if(symbol=="")
         continue;
      FileWrite(handle,"symbol",symbol);
      for(int day=0; day<=6; day++)
        {
         for(uint session_index=0; ; session_index++)
           {
            datetime session_from;
            datetime session_to;
            ResetLastError();
            if(!SymbolInfoSessionTrade(
               symbol,
               (ENUM_DAY_OF_WEEK)day,
               session_index,
               session_from,
               session_to
            ))
               break;
            long start_seconds=((long)session_from)%86400;
            long end_seconds=((long)session_to)%86400;
            FileWrite(
               handle,
               "session",
               symbol,
               day,
               session_index,
               start_seconds,
               end_seconds
            );
           }
        }
     }

   FileWrite(handle,"END");
   FileFlush(handle);
   FileClose(handle);
   return true;
  }
