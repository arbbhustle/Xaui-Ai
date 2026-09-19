# APK installueshëm për Android

Ky projekt është gati për të prodhuar një **APK debug të nënshkruar automatikisht**, që mund të instalohet direkt në telefon Android.

## Mënyra më e lehtë: GitHub Actions
1. Ngarko projektin në një repository GitHub.
2. Hape skedën **Actions**.
3. Zgjidh **Build Android APK**.
4. Shtyp **Run workflow**.
5. Kur build-i të përfundojë, shkarko artifact-in **XAU-AI-Trader-APK**.
6. Brenda tij gjendet `app-debug.apk`.
7. Në Android hape APK-në dhe lejo **Install unknown apps** nëse telefoni të kërkon.

APK-ja debug është e nënshkruar nga Android build tools dhe mund të instalohet për testim.
