Welcome to the official *konikvranik* [Kodi](https://kodi.tv) addon repository.

## 📦 Add-ons

| Add-on | Description |
|---|---|
| [`service.cec.server`](matrix/service.cec.server/) | Sends HDMI-CEC commands over a network to the CEC interface the Kodi instance is connected to. Based on the [pyCEC](/pyCEC) library. x86_64 only. |
| [`service.micesink`](matrix/service.micesink/) | Miracast over Infrastructure screen sink — the kiosk advertises itself as a network display (`_display._tcp` via avahi) and plays the incoming stream in Kodi. |
| [`service.nfo.sync`](matrix/service.nfo.sync/) | Event-driven `.nfo` sync: converts FULL Sonarr/Radarr `.nfo` files to identity form so Kodi scrapes fresh cs-CZ metadata online, repairs items without ratings and exports the library back to `.nfo`. |

The **master** branch hosts tested releases; the **alpha** branch is for experiments only.

## 🧭 How to Add This Repository to Kodi

1. **Open Kodi.**
2. Go to **Settings** (gear icon).
3. Select **File Manager**.
4. Click on **Add Source**.
5. Click on `<None>` and enter the following URL:

    ```
    https://konikvranik.github.io/kodi_repo/
    ```

6. Click **OK** and name the source (e.g., `konikvranik`).
7. Confirm with **OK** again.

## 🔧 Install Add-ons from the Repository

1. Return to the Kodi home screen.
2. Go to **Add-ons**.
3. Click the **box icon** in the top-left (Install from zip file).
4. Select the source you just added (`konikvranik`) and install repository from [repo/zips/repository.konikvranik/](repo/zips/repository.konikvranik/).
5. After installation, go to **Install from repository**.
6. Select **konikvranik Repository** and install any available addons.

## ⚠️ Troubleshooting

- Make sure **Unknown sources** are enabled:
    - Settings > System > Add-ons > **Unknown sources**
- Check your internet connection and verify the repository URL is entered correctly.

## 🤝 Contribute

This repository welcomes contributions! If you have ideas, bug reports, or your own add-on to share, feel free to [open an issue or pull request](https://github.com/konikvranik/kodi_repo).

---

## 🧪 Experimental Repository (Alpha)

For early access to work-in-progress or experimental add-ons, you can install the **alpha version** of the repository from the [`alpha` branch](https://github.com/konikvranik/kodi_repo/tree/alpha/repo/zips/repository.konikvranik-alpha):

1. Download and install the current `repository.konikvranik-alpha-<version>.zip` from the link above (**Install from zip file**).
2. After installation, you can access the experimental repository under **Install from repository**.

> ⚠️ The alpha repository may contain unstable or incomplete add-ons intended for testing purposes only.

---

🎬 Thank you for using Kodi and supporting the open-source community!

---

# 🇨🇿 Česká verze

Vítejte v oficiálním repozitáři Kodi doplňků *konikvranik*.

## 📦 Doplňky

| Doplňek | Popis |
|---|---|
| [`service.cec.server`](matrix/service.cec.server/) | Posílá HDMI-CEC příkazy po síti do CEC rozhraní, ke kterému je Kodi připojeno. Založeno na knihovně [pyCEC](/pyCEC). Pouze x86_64. |
| [`service.micesink`](matrix/service.micesink/) | Přijímání obrazovky přes Miracast over Infrastructure — kiosk se hlásí jako network display (`_display._tcp` přes avahi) a příchozí stream přehraje Kodim. |
| [`service.nfo.sync`](matrix/service.nfo.sync/) | Událostní synchronizace `.nfo`: FULL `.nfo` od Sonarru/Radarru převede na identitní formát, takže Kodi scrapuje čerstvá cs-CZ metadata online, opravuje položky bez hodnocení a exportuje knihovnu zpět do `.nfo`. |

Větev **master** obsahuje otestované verze, větev **alpha** je jen pro experimenty.

## 🧭 Přidání repozitáře do Kodi

1. **Otevřete Kodi.**
2. Přejděte do **Nastavení** (ikona ozubeného kola).
3. Zvolte **Správce souborů**.
4. Klikněte na **Přidat zdroj**.
5. Klikněte na `<None>` a zadejte adresu:

    ```
    https://konikvranik.github.io/kodi_repo/
    ```

6. Potvrďte **OK** a pojmenujte zdroj (např. `konikvranik`).
7. Potvrďte znovu **OK**.

## 🔧 Instalace doplňků z repozitáře

1. Vraťte se na hlavní obrazovku Kodio.
2. Přejděte do **Doplňky**.
3. Klikněte na **ikonu krabice** vlevo nahoře (Instalovat z zip souboru).
4. Zvolte právě přidaný zdroj (`konikvranik`) a nainstalujte repozitář z [repo/zips/repository.konikvranik/](repo/zips/repository.konikvranik/).
5. Po instalaci přejděte na **Instalovat z repozitáře**.
6. Zvolte **konikvranik Repository** a nainstalujte dostupné doplňky.

## ⚠️ Řešení problémů

- Ujistěte se, že jsou povoleny **Neznámé zdroje**:
    - Nastavení > Systém > Doplňky > **Neznámé zdroje**
- Zkontrolujte připojení k internetu a správnost adresy repozitáře.

## 🤝 Podílet se na vývoji

Repozitář rád přijme příspěvky! Nápady, hlášení chyb nebo vlastní doplňky můžete přidat přes [issue nebo pull request](https://github.com/konikvranik/kodi_repo).

---

## 🧪 Experimentální repozitář (Alpha)

Pro přístup k rozpracovaným a experimentálním doplňkům můžete nainstalovat **alfa verzi** repozitáře z [větve `alpha`](https://github.com/konikvranik/kodi_repo/tree/alpha/repo/zips/repository.konikvranik-alpha):

1. Stáhněte a nainstalujte aktuální `repository.konikvranik-alpha-<verze>.zip` z odkazu výše (**Instalovat z zip souboru**).
2. Po instalaci najdete experimentální repozitář pod **Instalovat z repozitáře**.

> ⚠️ Alfa repozitář může obsahovat nestabilní nebo nekompletní doplňky určené pouze k testování.
