# Colin Furze → Spotify (dagelijkse sync)

[Open de bestaande Spotify-playlist](https://open.spotify.com/playlist/59DazYEmOTJlmDb1fLAkCd)

Een **openbare GitHub-repository** met één Python-script (uitsluitend standaardbibliotheek)
en een automatische GitHub Actions-job. De playlist bevatte al 273 tracks
bij de eerste migratie; deze repo werkt hem voortaan **incrementeel** bij.

## Werking

Elke ochtend om **08:17 Europe/Amsterdam**:

1. Check de 50 nieuwste uploads van **beide Colin Furze-kanalen** met de YouTube Data API.
2. Controleer de daadwerkelijke videopublicatiedatum; selecteer alleen video's van de **laatste 48 uur**.
3. Parse de muziekcredits uit de YouTube-beschrijvingen met de bestaande, bewezen regex-regels.
4. Zoek artiest/titel in Spotify; accepteer een track pas als artist/title-match voldoende betrouwbaar is.
5. Lees de actuele Spotify-playlist en voeg **alleen ontbrekende tracks bovenaan** toe. Geen dubbele nummers, geen playlistbeschrijving en geen vervanging van bestaande nummers.
6. Schrijf gecontroleerde video's naar [data/videos.json](data/videos.json) en een samenvatting van elke run naar [data/runs.jsonl](data/runs.jsonl).

Resultaten worden iedere run, ook bij fouten, door de workflow naar de
repository gecommit. Geen database, externe opslag of Spotify-zoekcache nodig.

## Eenmalige configuratie: GitHub Actions secrets

Open **[Settings → Secrets and variables → Actions](https://github.com/lhaagsma/colinfurze-spotify-sync/settings/secrets/actions)**
in deze repo en klik **New repository secret**. Maak exact deze vier aan:

| Secret name | Waarde |
|---|---|
| `YOUTUBE_API_KEY` | Je bestaande YouTube Data API-sleutel |
| `SPOTIFY_CLIENT_ID` | Spotify app Client ID |
| `SPOTIFY_CLIENT_SECRET` | Spotify app Client Secret |
| `SPOTIFY_REFRESH_TOKEN` | Spotify user OAuth Refresh Token voor deze playlist |

Je hebt ze eerder lokaal met `export` ingesteld. Je kunt de waarde uit je
lokale shell rechtstreeks kopiëren naar GitHub, **niet naar een commit,
issue of chat**. Er is geen `SPOTIFY_ACCESS_TOKEN` nodig: het script
vraagt per run zelf een nieuw access token aan. Playlist-ID
`59DazYEmOTJlmDb1fLAkCd` is hardcoded (geen secret).

**Extra belangrijk:** omdat dit een publieke repository is, moeten de
API-sleutels *alleen* in GitHub Actions-secrets staan. Een publieke fork of
clone krijgt ze niet.

## Start de eerste run

Open **[Actions → Daily Colin Furze to Spotify](https://github.com/lhaagsma/colinfurze-spotify-sync/actions/workflows/daily.yml)**
en kies **Run workflow**. Bekijk daarna het runlog. Als er geen nieuwe,
goed herkenbare muziek was, is `spotify_added: 0` normaal.

De workflow heeft toestemming `contents: write` nodig om de geschiedenis
te committen. Er wordt **dagelijks** een nieuwe regel toegevoegd aan
`data/runs.jsonl`; dit levert ook repositoryactiviteit op, al kan
GitHub in publieke repositories de cron toch uitschakelen bij
60 dagen zonder door GitHub erkende activiteit. Controleer af en toe
of de geplande runs nog actief zijn.

De job is bewust **geen** Cloudflare Worker. Met deze Python-implementatie
is een publieke GitHub Actions-runner eenvoudiger en zijn de standaard
Linux-runner-minuten gratis. De workflow is onze dagelijkse *worker*.

## Historische gegevens

De initiële `data/videos.json` bevat **454 video's** uit de eerdere
volledige YouTube-export. Bij **65** daarvan zijn samen **237 handmatig
gecontroleerde referentiecredits** uit de historische testbestanden
opgenomen. Dit is alleen de bestaande referentieset, **niet** alle
circa 661 geëxtraheerde credits of de volledige lijst van 273 Spotify-tracks.
Daarom hebben historische records `status: historical_unreviewed` of
`historical_reference_subset` en zijn hun Spotify-ID's nog onbekend.
Een nieuwe dagelijkse controle vervangt het betreffende record met
daadwerkelijke matchresultaten.

De bestaande Spotify-playlist is de betrouwbare bron voor welke nummers
al aanwezig zijn. Het JSON-overzicht is alleen een publiek auditarchief,
**geen checkpoint**.

## Beperkingen

- 48 uur is een bewuste versimpeling: later gewijzigde beschrijvingen of
  uitzonderlijk laat verschenen video's kunnen ontbreken.
- Bij een volledig stille dag doet het script geen Spotify-aanroepen.
- GitHub-hosted acties worden niet gegarandeerd op de exacte cronminuut
  gestart; als een run helemaal vervalt, kunnen video's gemist worden.
- Spotify OAuth refresh tokens verlopen volgens Spotify na **6 maanden**.
  Daarna moet je opnieuw autoriseren en alleen de repository-secret
  `SPOTIFY_REFRESH_TOKEN` vervangen.
- De openbare `data/videos.json` bevat uitsluitend openbare videotitels,
  artiesten en nummers, **geen volledige beschrijvingen of credentials**.

## Lokaal uitvoeren

In een terminal waarin de vier genoemde environment variables al zijn
gezet:

```bash
python3 sync.py
```

Dit is meteen een **echte sync**. Testen zonder Spotify-schrijfacties kan
door uitsluitend de workflow-syntaxcheck te gebruiken:
`python3 -m py_compile sync.py`.