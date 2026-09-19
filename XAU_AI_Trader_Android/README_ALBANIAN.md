# XAU AI Trader — Android

Ky është versioni i parë i app-it Android për AI trading të XAUUSD.

## Çka bën tani
- Shfaq BUY / SELL / NO TRADE
- Confidence %
- BUY score / SELL score
- Entry, SL, TP1, TP2
- DXY, session, mode, action
- Ka Demo Signal për testim
- Mund të lidhet me një AI API endpoint përmes HTTP GET

## Si ta hapësh
1. Instalo Android Studio në PC/Mac.
2. Open -> zgjidh folderin `XAU_AI_Trader_Android`.
3. Lëre Gradle Sync të përfundojë.
4. Lidhe telefonin Android me USB debugging ose përdor emulator.
5. Run.

## API JSON që app-i pret
Shembull:

```json
{
  "direction": "BUY",
  "confidence": 0.81,
  "buy_score": 81,
  "sell_score": 18,
  "entry": 4317.20,
  "sl": 4310.40,
  "tp1": 4325.90,
  "tp2": 4330.80,
  "dxy": "BEARISH",
  "session": "NEW YORK",
  "mode": "PULLBACK",
  "action": "BUY CONFIRMED"
}
```

## Hapi tjetër
Backend-i Python që kemi për XAU AI duhet të ekspozojë endpoint `/signal` me këtë JSON. Pastaj app-i lidhet direkt me të.

Ky version nuk bën autotrading automatik. Për autotrading duhet një execution bridge/API e brokerit dhe risk controls të ndara.
