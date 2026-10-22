# Device-Identity-Spoofing pro Container: Optionen (Research 2026-10-14)

## Das Problem, das offen ist
Wir haben pro Phone: Crane (Container), Shadowrocket (Proxy pro Container), Shadow
(Jailbreak verstecken), LocSim (GPS pro Container). **Was fehlt:** die Geräte-Identität.
IDFV, IDFA, Seriennummer, MAC und UDID sind in **allen** Crane-Containern eines Phones
gleich. Instagram kann daran erkennen, dass 25 "Geräte" in Wahrheit ein Handy sind.
DeviceCheck / App Attest (Secure-Enclave-Signatur) ist aus dem Userland **nicht**
fälschbar, das ist bei allen Lösungen so und keine kann es.

## Die drei Optionen im Vergleich

### InstaVault (micdudd, GitHub, kostenlos)
- Spooft UDID, IDFV, IDFA, Serial, MAC **pro Container**. Dazu GPS und JB-Stealth.
- **Aber:** es ist ein **eigenes Container-System** in einer umgepackten Instagram-IPA
  (Installation per Sideloadly), **nicht** ein Tweak, der auf Crane aufsetzt. Es würde
  Crane ersetzen, nicht ergänzen. Unser ganzer cranectl-Öffnungspfad müsste weichen.
- Sehr jung: v1 bis Build 4 alle am 1./2. Oktober 2026, 6 Releases in zwei Tagen. v2
  hat erst einen SIGABRT gefixt (IDFV/IDFA gaben den falschen Typ zurück).
- Keine Angabe zu iOS-Version, Jailbreak oder ob Signup/Login läuft. DeviceCheck/Attest
  nicht abgedeckt.

### DeviceSpoofer (GitHub, kostenlos)
- Hookt IDFV, IDFA, MobileGestalt (UDID/Serial/IMEI), DeviceCheck -> nil, App Attest
  -> nil, dazu einen X-spezifischen Header.
- **Aber:** nur **ein** Profil pro Gerät, abgelegt in
  `/var/mobile/Library/Preferences/com.devicespoofer.profile.plist`. Nicht pro Container.
  Auf einem Phone mit 25 Containern sähen alle 25 dieselbe gefälschte Identität ->
  so wie es ist, nutzlos für uns (derselbe Fehler wie systemweites Spoofing).
- deb über Sileo. Gebaut/getestet für Grok/X, nicht Instagram. Kleines Projekt.

### Ghost / DORITOS (bezahlt)
- "Ghost" wird auf BlackHatWorld über Telegram verkauft, Vertrauen unklar, keine Belege.
- DORITOS ist die dokumentierte Kauf-Variante: 200 $ Lifetime / 5 Geräte, zufälliges
  Profil pro App/Container, Proxy pro App, LocSim, JB-Bypass, CLI + Daemon. Passt
  technisch genau auf unseren Bedarf, ist aber geschlossen und kostet.

## Bewertung für UNSER Setup
- **InstaVault:** per Container korrekt, aber bedeutet Crane aufgeben. Große Umstellung,
  kollidiert mit unserem cranectl-Flow und dem Rest der Pipeline. Zu jung/instabil.
- **DeviceSpoofer:** falsche Granularität (pro Gerät), passt nicht zu 25 Containern.
- **DORITOS/Ghost:** passt, aber Geld + Vertrauen.

## Empfehlung: DeviceSpoofer-Hooks, aber von uns pro Container umgeschaltet
Der Trick: DeviceSpoofer liest **ein** Profil-Plist. Wir öffnen pro Phone **immer nur
einen Container gleichzeitig**, und wir steuern das Öffnen zentral in
`open_container_cli()` (genau dort, wo schon Proxy, IP-Gate und LocSim sitzen).
-> Direkt vor dem Instagram-Start schreiben wir in das Profil-Plist die **feste**
Identität dieses Containers (IDFV/Serial/UDID deterministisch aus der Container-ID
abgeleitet, damit G3 immer dieselbe behält). Damit wird das per-Gerät-Tool für unseren
Ablauf effektiv per-Container, ohne Crane anzufassen.

Vorteile: kostenlos, bleibt auf Crane + Shadowrocket + Shadow + LocSim, ein zusätzlicher
Schreibschritt im schon bestehenden Opener. Grenze: DeviceCheck/Attest bleibt echt
(unvermeidbar). Erst auf F/G testen, dann messen (zwei Phones, eins mit, eins ohne,
zwei Wochen Reach/Flags) bevor es auf die Flotte geht.

## Nächster Schritt, wenn Rene zustimmt
1. DeviceSpoofer-deb auf F installieren, Hooks nur für Instagram.
2. In `open_container_cli()` nach dem LocSim-Schritt das Profil-Plist mit der aus der
   Container-ID abgeleiteten Identität überschreiben, dann cfprefsd droppen, dann IG.
3. Mit der Jailbreak-/Device-Test-App (schon auf G) IDFV vor/nach vergleichen.

## Quellen
- InstaVault: https://github.com/micdudd/InstaVault , Release v2 (IDFV/IDFA-Fix):
  https://github.com/micdudd/InstaVault/releases/tag/v2
- DeviceSpoofer: https://github.com/cuchingoc68-creator/DeviceSpoofer
- DORITOS (BHW): https://www.blackhatworld.com/seo/doritos-your-jb-suite-for-ios-multi-accounting-containers-proxies-locsim-spoofing.1804744/
- Cloning-Übersicht: https://www.blackhatworld.com/seo/all-ways-of-cloning-instagram-on-iphones.1840905/
