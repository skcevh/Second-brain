# AMIROS Second Brain

Windows-archief voor AI-gesprekken, e-mail en projectkennis. De collector leest geselecteerde bronnen onder het eigen Windows-account. Een lokale SQLite-wachtrij levert genormaliseerde records via een SMB 3-inbox aan. AMIROS-SERVER indexeert berichten en kennisnotities met SQLite FTS5. Import en zoeken werken zonder AI-model.

## Installeren

Vereist Windows, Python 3.11 of nieuwer met SQLite FTS5 en een NTFS-volume. Gebruik CPython 3.12 x64 voor de optionele PST-ondersteuning. Voer de installer uit als de gebruiker van wie de bronbestanden worden gelezen.

~~~powershell
git clone https://github.com/skcevh/Second-brain.git
Set-Location .\Second-brain
# Op AMIROS-SERVER, als dezelfde gebruiker daar ook bronnen heeft:
.\Install-SecondBrain.ps1 -Role Both -OllamaModel 'qwen3-coder:30b' -EnablePst
# Op een andere computer, nadat de serverinbox is ingericht:
.\Install-SecondBrain.ps1 -Role Client -Inbox '\\AMIROS-SERVER\AMIROS-SecondBrain$'
~~~

Gebruik `-Role Server` als de server uitsluitend moet ontvangen. Standaard staat data in `%USERPROFILE%\AMIROS\SecondBrain`. `-State` kiest een andere, toegewijde locatie buiten Git. Een herinstallatie ververst de app en laat bestaande configuratiebestanden staan. Gebruik `-NoSchedule` voor een testinstallatie. Configureer één geplande installatie per Windows-account.

De installer voert direct een eerste cyclus uit en registreert een taak bij aanmelding en vervolgens elke vijf minuten. Het account mag vergrendeld zijn. Bij volledig afmelden wachten beide onderdelen op een volgende aanmelding. Bekijk in Taakplanner **AMIROS Second Brain** als volgende uitvoertijd, account of resultaat afwijkt.

## Versleutelde inbox voor andere computers

Op AMIROS-SERVER voert een beheerder dit eenmalig uit in PowerShell, binnen de bestaande LAN/VPN-configuratie. Geef uitsluitend de concrete, vertrouwde Windows-accounts van de archiefbeheerder en de clients op. De ontvangende Windows-accountnaam kan met `whoami` worden gecontroleerd.

~~~powershell
.\Enable-SecondBrainInbox.ps1 -ArchiveAccount 'DOMEIN\archiefgebruiker' -ClientAccount 'DOMEIN\clientgebruiker'
~~~

Het script toont de echte lokale inboxlocatie. De standaardlocatie is `%ProgramData%\AMIROS.SecondBrain.Inbox`. Zet `inbox` in **server.json** op het getoonde lokale pad. Een client op dezelfde server mag zijn eigen bestaande lokale inbox via diezelfde configuratie gebruiken of zijn **client.json** naar het nieuwe lokale pad laten wijzen. Server en client moeten voor lokale aanlevering hetzelfde pad gebruiken. Op andere computers is de bestemming `\\AMIROS-SERVER\AMIROS-SecondBrain$`. Gebruik geïntegreerde Windows-aanmelding of Windows Referentiebeheer voor de server. Er staan geen SMB-wachtwoorden in config.

De installer wijzigt firewall of globaal SMB-beleid niet. De share vereist SMB-versleuteling en de client verifieert die vóór het schrijven van bestanden. Zonder geauthenticeerde, versleutelde verbinding blijven records in de lokale wachtrij. Controleer de verbinding onder het geplande account met `Get-Item -LiteralPath '\\AMIROS-SERVER\AMIROS-SecondBrain$'` en `Get-SmbConnection -ServerName AMIROS-SERVER`. Houd SMB beperkt tot het bestaande vertrouwde netwerk. Geef deelnemers aan één inbox alleen toegang als zij ook de tijdelijke wachtende bestanden van elkaar mogen zien. De database is privé en staat buiten die share.

## Bronnen toelaten

Kopieer de structuur in [client.example.json](config/client.example.json) naar de taakconfiguratie als referentie. Bewerk **client.json** in de runtime-map. Nieuwe installaties starten met bronnen uitgeschakeld.

- Zet alleen gewenste sleutels in `sources` op `true`. Koppel projectnamen aan lokale bronmappen met `projects`. De specifiekste map wint. `deny_paths` heeft voorrang. Zet `allow_all_workspaces` alleen aan wanneer alle lokale werkruimten zijn toegelaten. Onbekende projecten heten dan **Overige werkruimten**.
- Voor expliciet geselecteerde projectinstructies zet je `instructions` aan en gebruik je `instruction_files`, bijvoorbeeld `{"Amiros":["C:/src/voorbeeld/AGENTS.md"]}`. De bestanden moeten binnen een overeenkomstig toegelaten project liggen.
- Geef exportbestanden toegang per conversatie-ID via `approved_exports`, bijvoorbeeld `{"chatgpt":{"voorbeeld-id":"Amiros"}}`. Leg deze exports direct in `imports/<bron>`. Gebruik `list-export chatgpt <volledig-bestandspad>` of `list-export copilot-vscode <volledig-bestandspad>` om beschikbare ID's lokaal te bekijken.
- Voor een export die integraal mag worden opgenomen, maak je `imports/<bron>/<project>` aan en voeg je de projectnaam toe aan `reviewed_drop_projects`. Ieder bestand dat je daarin plaatst, wijs je toe aan dat project. Verplaats een gemengde export naar de bronmap zonder projectmap en laat alleen de bedoelde conversatie-ID’s toe.
- Gebruik de allowlist volgens de rechten van elk bedrijf waarvoor je werkt. Een ingeschakelde bron geeft toegang tot de lokale gegevens waarvoor dat account al rechten heeft. Uitgezette bronnen of projectregels blokkeren nieuwe overdracht. Reeds opgeslagen materiaal blijft in het archief. Bewaar exports in de privé-importmap en kopieer ze daar pas heen zodra de download klaar is.

## Import per bron

| Bron | Werkwijze |
| --- | --- |
| ChatGPT | Vraag de export aan via **Instellingen → Gegevensbeheer → Gegevens exporteren**. Kopieer de ZIP of de conversatie-JSON naar `imports/chatgpt` volgens de toelatingsregels hierboven. Volgende exports kunnen naast de eerste worden geplaatst. Tekst, zichtbare berichten uit vertakkingen, ID's, datums en beschikbare citatie-URL’s worden geïmporteerd. De import loopt automatisch nadat het bestand is geplaatst. |
| Codex | De collector leest `rollout-*.jsonl` in `%CODEX_HOME%/sessions` en `archived_sessions`, standaard onder `%USERPROFILE%/.codex`. Hij streamt grote logbestanden en neemt zichtbare gebruiker- en assistenttekst mee. Systeeminstructies, toolpayloads, beelden en authenticatiebestanden worden overgeslagen. Een sessie die tijdens een turn van project wisselt, wordt niet automatisch geïmporteerd. |
| Copilot in VS Code | Kies **Chat: Export Chat…** voor een goedgekeurde sessie en sla de JSON op in `imports/copilot-vscode`. Sessies uit `User/workspaceStorage` met één lokale werkmap worden automatisch gelezen wanneer het project is toegelaten. JSON en JSONL-operatielogs worden ondersteund. Exporteer sessies zonder herleidbaar lokaal project of uit een multi-rootworkspace handmatig. |
| Copilot CLI | De collector leest `events.jsonl` onder `%COPILOT_HOME%/session-state`, standaard onder `%USERPROFILE%/.copilot`. Herkende kopieën met dezelfde Copilot-sessie-ID worden samengevoegd. De broninstellingen en bestaande GitHub-sync blijven actief volgens eigen configuratie. |
| Outlook-mail | Voor bulkimport in nieuwe Outlook gebruik **Instellingen → Bestanden → Exporteren** per toegelaten mailbox of map. Plaats de afgeronde **PST** in `imports/outlook/<toegelaten project>`. Bestaande PST-archieven kunnen daar als kopie bij. Losse geëxporteerde **EML**-berichten kunnen in dezelfde map. Voor PST is `-EnablePst` nodig op de computer die importeert. |
| Klassieke Outlook | Open een geconfigureerd klassiek Outlook-profiel. Roep `outlook-stores` aan en voeg de bedoelde stores toe aan `outlook_stores` als `{"<store-id>":"E-mail"}`. Terwijl klassieke Outlook draait, leest de collector toegelaten e-mailmappen via het Outlook Object Model, inclusief geopende PST-archieven. De eerste cyclus leest alle beschikbare items; latere cycli lezen wijzigingen met overlap. |

Outlook bewaart voor zoekdoeleinden de tekst, afzender, geadresseerden, onderwerp, datum en beschikbare Message-ID. PST en klassieke Outlook leveren waar mogelijk ook het mappad. Herkenbaar geclassificeerde privé-, vertrouwelijke en beschermde berichten worden overgeslagen. Bijlagen, contacten en agenda-items worden niet geïndexeerd. De collector wijzigt geen mailboxen, bronbestanden of mailstatus.

Voor consumentenchats publiceert OpenAI een exportworkflow. Sign in with ChatGPT verleent geen toegang tot gespreksgeschiedenis. De Compliance API is voor geautoriseerde beheerders van daarvoor in aanmerking komende werkruimten. Deze installatie gebruikt lokale exports. Microsoft 365 Copilot en ChatGPT-toegang tot de server hebben geen geautoriseerde koppeling in deze installatie. De Outlook-route heeft betrekking op e-mail en staat los van Microsoft 365 Copilot.

Primaire documentatie: [ChatGPT-export](https://help.openai.com/en/articles/7260999-exporting-your-chatgpt-history-and-data), [Sign in with ChatGPT](https://developers.openai.com/siwc/quickstart), [Compliance Platform](https://help.openai.com/en/articles/9261474-openai-compliance-platform-for-enterprise-and-edu-customers), [Codex-rollouts](https://github.com/openai/codex/tree/main/codex-rs/rollout), [VS Code-chatgeschiedenis](https://code.visualstudio.com/docs/agents/run/sessions/session-history), [Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/chronicle), [Outlook-export](https://support.microsoft.com/en-us/outlook/export-emails-contacts-and-calendar-items-to-outlook-using-a-pst-file), [Outlook Object Model](https://learn.microsoft.com/en-us/office/vba/api/outlook.namespace.stores).

## Rechtstreekse mailimport instellen

Maak per mailbox een sleutel aan in `mail_accounts` in de lokale **client.json**. Gebruik een korte alias met kleine letters, cijfers en koppeltekens. Stel `sources.outlook` in op `true`. Kies een `initial_since` in UTC vóór de meest recente PST-export. Mail die daarna is ontvangen valt zo binnen de eerste scan. Mail die al in de index staat wordt op Message-ID ontdubbeld wanneer de bron die ID meelevert.

```json
"mail_accounts": {
  "voorbeeld": {
    "provider": "graph",
    "email": "mail@example.invalid",
    "project": "E-mail",
    "initial_since": "2026-11-18T00:00:00Z"
  }
}
```

### Amiros en Hotmail

1. Ga naar **Microsoft Entra → App registrations → New registration**. Kies het accounttype dat zowel accounts in elke organisatie als persoonlijke Microsoft-accounts ondersteunt.
2. Voeg onder **Authentication** het platform **Mobile and desktop applications** toe met redirect-URI `http://localhost`. Gebruik een public client.
3. Voeg onder **API permissions** de gedelegeerde Graph-machtigingen **Mail.Read** en **User.Read** toe. **User.Read** dient om het aangemelde account te controleren. In de browser vraagt de connector ook `offline_access` voor het vernieuwen van de sessie.
4. Kopieer de **Application (client) ID**. Voor beide mailboxen kun je dezelfde appregistratie gebruiken. Voer `mail-connect` per mailbox uit en kies telkens het bijbehorende account. Als de Amiros-tenant toestemming door gebruikers blokkeert, laat de beheerder de app beoordelen.

Zet voor een persoonlijk Hotmail-account `"authority": "consumers"` in de mailboxconfiguratie als de algemene Microsoft-aanmelding een andere accountidentiteit teruggeeft. De desktop-appregistratie moet persoonlijke Microsoft-accounts ondersteunen.

[Entra-desktopapp registreren](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-registration) · [Graph-mailtoegang](https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0)

### Gmail

1. Maak of selecteer een project in **Google Cloud Console** en schakel de **Gmail API** in. Configureer **Google Auth Platform** voor de bedoelde gebruiker. Gebruik voor een persoonlijk Gmail-account het publiek **External**.
2. Maak onder **Clients** een OAuth-client van het type **Desktop app**. Download het client-JSON-bestand naar een privémap buiten Git. De connector vraagt `gmail.readonly`.
3. Voer `mail-connect` uit, kies het juiste Gmail-account en verleen leestoegang in de browser. Bij een project met publicatiestatus **Testing** vervalt de Gmail-refresh-token normaal na zeven dagen. Volg voor doorlopende toegang Googles publicatie- en eventuele verificatievereisten voor dit project.

[Gmail API inschakelen](https://console.cloud.google.com/apis/library/gmail.googleapis.com) · [OAuth voor desktopapps](https://developers.google.com/identity/protocols/oauth2/native-app) · [Google OAuth-publiek en publicatiestatus](https://support.google.com/cloud/answer/15549945)

### Aanmelden op AMIROS-SERVER

Voer de opdrachten uit in PowerShell op de server, onder hetzelfde Windows-account als de vijfminutentaak. De aanmelding gaat via je browser, waar je de machtigingen beoordeelt. Tokens worden versleuteld met Windows DPAPI onder dat account.

```powershell
$brain = Join-Path $env:USERPROFILE 'AMIROS\SecondBrain\brain.cmd'
$graphId = Read-Host 'Entra Application (client) ID'
& $brain mail-connect amiros --client-id $graphId
& $brain mail-connect hotmail --client-id $graphId
$googleFile = Read-Host 'Volledig pad naar het Google OAuth Desktop-clientbestand'
& $brain mail-connect gmail --credentials-file $googleFile
& $brain mail-status
```

De geplande taak werkt ook als Outlook gesloten is. Het Windows-account blijft aangemeld. Graph leest veranderingen in de primaire mailbox sinds de laatst verwerkte periode met een kwartier overlap. Gmail bewaart zijn positie in de wijzigingenhistoriek en scant opnieuw vanaf `initial_since` wanneer die historiek is vervallen. `mail-status` toont per account of er een autorisatie is opgeslagen, wanneer ophalen voor het laatst is geslaagd en welke fout de provider eventueel terugstuurt. Losse PST-archieven blijven als aparte importbron beschikbaar. Graph kan een afzonderlijk Exchange Online Archive niet lezen. [Graph-archiefbeperking](https://learn.microsoft.com/en-us/graph/api/resources/mail-api-overview?view=graph-rest-1.0) · [Gmail-synchronisatie](https://developers.google.com/workspace/gmail/api/guides/sync)

## Zoeken en kennis opbouwen

Voer deze commando’s op AMIROS-SERVER uit onder de archiefgebruiker. Dezelfde CLI is beschikbaar vanuit een lokale Codex- of Copilot-terminal. Draai de opdracht zelf voor gevoelige treffers die je buiten de context van een AI-assistent wilt houden.

~~~powershell
$brain = Join-Path $env:USERPROFILE 'AMIROS\SecondBrain\brain.cmd'
& $brain status
& $brain detect
& $brain run
& $brain search 'Wat was de oplossing voor de Indigo timeouts?'
& $brain search 'SAP OData' --project Amiros --json
& $brain show codex '<bron-ID>'
& $brain derive codex '<bron-ID>'
& $brain annotate codex '<bron-ID>' beslissing 'Titel' 'Bevestigde beslissing en motivatie' --status bevestigd
~~~

`show` geeft de genormaliseerde bron en beschikbare berichtverwijzingen. `derive` analyseert op verzoek een begrensde selectie van berichten via het gekozen, lokaal geïnstalleerde Ollama-model op loopback. Elke opgeslagen suggestie vereist een letterlijk fragment uit een genummerd bericht en krijgt status **voorstel**. Bewerk of bevestig kennis expliciet via `annotate`. Kennisdocumenten hebben eigen datum, onderwerp, type en status en verwijzen naar de bron. Projectindeling en index werken zonder Ollama.

De eerste scan van grote bronnen kan langer duren. `status` toont wachtrij, tellingen, laatste uitvoering en fouten. `sync` verzamelt en verstuurt opnieuw, `receive` verwerkt de centrale inbox en `run` voert beide geconfigureerde rollen uit. `rescan` leest toegelaten bronnen opnieuw en stuurt beschikbare records opnieuw, bijvoorbeeld na herstel van een oudere back-up. De server controleert hashes, schrijft vóór het verwijderen van transportkopieën en verwerkt herhaalde leveringen veilig.

## Opslag, back-up en herstel

Het oorspronkelijke export- of sessiebestand blijft op zijn bronlocatie. De server bewaart genormaliseerde berichten in een privé SQLite-archief. Archiveer originele bestanden apart als je ook media, bijlagen of ruwe toolrecords nodig hebt. Alleen de eigenaar, SYSTEM en lokale beheerders krijgen toegang tot de runtime-map. Herkenbare tokens, wachtwoordvelden en privésleutels worden bij de import gemaskeerd. De dekking hangt af van het bronformaat en herkenbare patronen. Houd exports, wachtrij, inbox, index en back-ups buiten Git en buiten ongeschikte synchronisatiemappen.

~~~powershell
$state = Join-Path $env:USERPROFILE 'AMIROS\SecondBrain'
New-Item -Type Directory -Force (Join-Path $state 'backups') | Out-Null
$backup = Join-Path $state ('backups\archive-' + (Get-Date -Format yyyyMMdd-HHmmss) + '.sqlite')
& $brain backup $backup
& $brain restore $backup (Join-Path $state 'restored.sqlite')
~~~

`backup` gebruikt de SQLite-back-up-API, inclusief gecommitteerde WAL-transacties, en controleert integriteit. Kopieer back-ups naar bestaande afgeschermde, versleutelde back-upopslag om een schijfdefect te kunnen overleven. Voor herstel pauzeer je de ontvangertaak, laat je `restore` een **nieuw** bestand maken, stel je `archive` in **server.json** in op dat bestand en controleer je `status` en `search`. Hervat de taak en voer indien nodig `rescan` uit op clients die sinds de back-up records hebben verzonden.

## Tests en publicatie

~~~powershell
python -m unittest discover -s tests -v
git diff --cached --check
gitleaks git --staged --redact .
~~~

De tests genereren hun eigen fictieve bronbestanden buiten de checkout. Stage uitsluitend expliciet beoordeelde broncode, configuratiesjablonen, documentatie en tests. De GitHub-repository is openbaar. Alleen de repository-inhoud hoort op GitHub.
