# ForexAI

ForexAI یک سیستم Local برای تحلیل داده‌های MetaTrader 5 Demo و تولید Signal قابل‌توضیح است. مسیر عادی Runtime همچنان Signal-only است و تولید Signal به‌تنهایی هیچ معامله‌ای ایجاد نمی‌کند. قابلیت Semi-Automatic Execution فقط پس از کلیک صریح کاربر مجاز روی دکمه Telegram و فقط روی حساب Demo وارد مسیر اجرای محافظت‌شده می‌شود. Full Auto پشت Feature Flag خاموش باقی مانده است.

## وضعیت فعلی

- هفت Instrument اولیه: EURUSD، GBPUSD، USDJPY، USDCHF، USDCAD، AUDUSD و XAUUSD
- دریافت فقط Candleهای بسته‌شده از MT5
- تحلیل D1/H4/H1/M15 قابل تنظیم
- Swing، HH/HL/LH/LL، BOS/CHOCH، Liquidity Sweep و Session/DST
- ATR، EMA و RSI به‌عنوان تأییدهای محدود، نه Trigger مستقل
- Currency-relative strength
- Myfxbook RSS Economic Calendar بدون API Key، با Cache و Fallback قابل تنظیم
- Confidence Score شفاف، حداقل 75 و سقف پنج Alert در روز UTC
- ذخیرهٔ Barها در Parquet و تصمیم‌ها در SQLite
- Telegram outbound با جلوگیری از ارسال تکراری
- نرمال‌سازی runtime زمان Broker/Server به UTC و نمایش Telegram در Asia/Tehran
- Entry شفاف بر اساس Ask برای BUY و Bid برای SELL، همراه با حجم پیشنهادی MT5 بر حسب Lot
- Auto Execution اختیاری فقط برای MARKET Order روی Demo، با SL/TP اجباری و جلوگیری پایدار از اجرای تکراری
- Inline Keyboard برای اجرای صریح یا Recheck کاملاً read-only هر Signal ذخیره‌شده

## راه‌اندازی Windows

PowerShell را در `E:\ForexAI` باز کنید:

```powershell
uv sync --dev
Copy-Item .env.example .env
```

فایل `.python-version` پروژه روی Python 3.12.7 تنظیم شده است. اگر `uv sync` اعلام کرد این نسخه در سیستم موجود نیست:

```powershell
uv python install 3.12.7
uv sync --dev
```

## کارهایی که باید در MetaTrader 5 انجام دهید

1. MetaTrader 5 را باز نگه دارید و وارد همان Demo Account شوید.
2. در Market Watch هر هفت Symbol را نمایش دهید.
3. اگر بروکر پسوند یا پیشوند دارد، مقدار `broker_symbol` را در `config/instruments.yaml` اصلاح کنید؛ مثلاً `EURUSD.a` یا `XAUUSDm`.
4. از مسیر Tools → Options → Charts مقدار Max bars in chart را به‌اندازهٔ کافی بالا ببرید.
5. اتصال را بررسی کنید:

```powershell
uv run forexai check
```

برای مشاهدهٔ کامل زمان Python، timestamp خام MT5، offset تشخیص‌داده‌شده و
زمان نرمال‌شدهٔ tick و کندل‌های M15/H1 اجرا کنید:

```powershell
uv run forexai time-check
```

Offset بروکر در runtime تشخیص داده می‌شود و هیچ offset ثابتی در کد وجود ندارد.
timezone نمایشی Telegram با `telegram.display_timezone` و ریسک پیشنهادی با
`risk.risk_per_trade_percent` در `config/app.yaml` قابل تنظیم است.

## محافظ Session و Position باز

پکیج Python متاتریدر Sessionهای معاملاتی Symbol را مستقیماً ارائه نمی‌کند.
سرویس read-only پروژه با `SymbolInfoSessionTrade()` برنامهٔ واقعی تمام Symbolهای
Market Watch را هر پنج دقیقه به Common Files متاتریدر صادر می‌کند.

ابتدا exporter کامپایل‌شده را نصب کنید:

```powershell
.\scripts\install_session_exporter.ps1
```

سپس در MT5 از Navigator → Services گزینهٔ `ForexAISessionExporter` را Refresh و
Start کنید. وضعیت هر Symbol و Positionهای باز را بررسی کنید:

```powershell
uv run forexai market-check
```

اگر export موجود یا تازه نباشد، سیستم به‌صورت محافظه‌کارانه Signal را با
`SESSION_STATUS_UNAVAILABLE` suppress می‌کند. Positionهای باز فقط با
`positions_get()` خوانده می‌شوند. Auto Execution هیچ Scale-in، Hedging، Reversal،
Pending Order یا مدیریت/بستن خودکار Position ندارد.

برنامه در صورت فعال‌بودن حساب غیر Demo متوقف می‌شود.

## وضعیت فعلی: Signal-only + اجرای نیمه‌خودکار با تأیید کاربر

تنظیمات runtime در `config/app.yaml` عمداً خاموش‌اند:

```yaml
auto_execution:
  enabled: false
  demo_only: true
  market_orders_only: true
  magic: 26081101
  comment: ForexAI
  maximum_price_deviation_points: 20
```

Semi Execution مستقل از Full Auto فعال است:

```yaml
semi_execution:
  enabled: true
  demo_only: true
  market_orders_only: true
  signal_max_age_minutes: 15
  magic: 26081101
  comment: ForexAI-Semi
```

قبل از هر تصمیم برای فعال‌سازی، فقط diagnostic بدون معامله را اجرا کنید:

```powershell
uv run forexai execution-check
```

این Command نوع حساب، مجوزهای Terminal/Account، Positionهای باز، Session،
trade mode و محدودیت حجم هر Symbol را نمایش می‌دهد و `order_check` یا
`order_send` را فراخوانی نمی‌کند.

در وضعیت فعلی `uv run forexai run` و `scripts/run_local.ps1` در یک process هم
scan loop و هم Telegram callback listener را اجرا می‌کنند. هر Signal Telegram دو
دکمه دارد: `✅ اجرای معامله` و `🔄 بررسی ورود الآن`. دکمه Recheck هیچ Order API را
فراخوانی نمی‌کند. دکمه Execute فقط پس از بازیابی Signal از SQLite، کنترل expiration،
Demo account، permission، Session، Tick، Position باز، Entry deviation، حجم، SL/TP،
Margin و `order_check` اجازه یک `order_send` بدون retry را می‌دهد.

تولید و ارسال Signal به Algo Trading نیاز ندارد؛ اما کلیک Execute فقط وقتی مجاز است
که permission معامله در MT5 فعال باشد. `--console` همیشه dry-run و بدون callback
execution است. Full Auto را فعال نکنید.

Callback فقط از Chat تنظیم‌شده و User مجاز پذیرفته می‌شود. در Private Chat، Chat ID
مثبت به‌طور خودکار همان User ID در نظر گرفته می‌شود. برای Group/Channel مقدار زیر را
در `.env` نیز مشخص کنید:

```dotenv
TELEGRAM_ALLOWED_USER_ID=YOUR_TELEGRAM_USER_ID
```

Diagnostic بدون معامله:

```powershell
uv run forexai semi-execution-check
```

هر `signal_id` پیش از دسترسی به API سفارش به‌صورت اتمیک در جدول `executions`
SQLite رزرو می‌شود. نتیجه مبهم، failure یا restart باعث ارسال کورکورانهٔ سفارش
دوم نمی‌شود. در لحظه اجرا Tick و Session تازه خوانده می‌شوند، Position همان Symbol
دو بار (قبل از check و بلافاصله قبل از send) کنترل می‌شود، حجم فعلی ForexAI دوباره
محاسبه می‌شود، SL/TP از خود Signal در request قرار می‌گیرند و بعد از retcode موفق،
وجود Position با `positions_get(symbol=...)` تأیید می‌شود.

## ساخت Telegram Bot

1. در Telegram وارد گفت‌وگو با `@BotFather` شوید.
2. دستور `/newbot` را اجرا و Name و Username بات را تعیین کنید.
3. Token دریافتی را فقط در فایل محلی `.env` مقابل `TELEGRAM_BOT_TOKEN` قرار دهید؛ Token را در Chat یا Git وارد نکنید.
4. گفت‌وگوی بات ساخته‌شده را باز کنید و `/start` بفرستید.
5. برای به‌دست‌آوردن Chat ID، در PowerShell موقتاً Token را در Environment قرار دهید و Updates را بخوانید:

```powershell
$env:TELEGRAM_BOT_TOKEN="YOUR_LOCAL_TOKEN"
Invoke-RestMethod "https://api.telegram.org/bot$env:TELEGRAM_BOT_TOKEN/getUpdates"
```

مقدار `result.message.chat.id` را در `.env` مقابل `TELEGRAM_CHAT_ID` قرار دهید. سپس متغیر موقت را پاک کنید:

```powershell
Remove-Item Env:TELEGRAM_BOT_TOKEN
```

## Economic Calendar

Provider پیش‌فرض `myfxbook_rss` است و برای استفادهٔ شخصی/Demo به API Key نیاز ندارد. Feed در `config/events.yaml` تنظیم شده و پاسخ آن در `data/state` Cache می‌شود.

برای مشاهدهٔ خروجی استانداردشدهٔ واقعی:

```powershell
uv run forexai calendar --hours 48 --importance High --limit 20
```

رفتار قطع Provider با `unavailable_behavior` قابل تنظیم است:

- `warn_and_continue`: پیش‌فرض Demo؛ خطا واضح Log می‌شود و تحلیل با هشدار ادامه پیدا می‌کند.
- `block`: حالت محافظه‌کارانه؛ Candidate تا بازگشت تقویم Block می‌شود.

FMP فقط به‌عنوان Provider اختیاری حفظ شده است. اگر بعداً `provider: fmp` انتخاب شود، آن زمان `FMP_API_KEY` و احتمالاً اشتراک مناسب لازم خواهد بود.

## اجرای امن اولیه

برای ارسال فقط یک پیام تست مستقل به Telegram configured در `.env`:

```powershell
uv run forexai telegram-test
```

این Command به MT5، Signal Engine، Signal Lifecycle، SQLite performance state یا Order execution متصل نیست.

برای تست کامل بدون ارسال Telegram:

```powershell
uv run forexai scan-once --console
uv run forexai run --console
```

برای ارسال Signalهای واجد شرایط به Telegram:

```powershell
uv run forexai scan-once
uv run forexai run
```

یا از Launcherها استفاده کنید:

```powershell
.\scripts\check_mt5_connection.ps1
.\scripts\run_console.ps1
.\scripts\run_local.ps1
```

برای توقف پایش مداوم `Ctrl+C` بزنید.

## تست‌ها

```powershell
uv run pytest
```

Confidence Score احتمال برد نیست. پارامترهای فعلی قبل از هر استفادهٔ واقعی باید با Historical Replay، هزینهٔ Spread/Slippage و Walk-forward Validation کالیبره شوند. تا آن زمان سیستم فقط ابزار تحلیل و Demo/Shadow testing است.
