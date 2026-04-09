---
name: icloud-caldav
description: Pełna integracja z Apple iCloud Calendar via CalDAV (tylko Linux). Bezpieczna, z env vars i UID-based delete/update.
version: 1.0.0
metadata:
  requires:
    env:
      - ICLOUD_APPLE_ID
      - ICLOUD_APP_PASSWORD
    python:
      - caldav>=3.0
      - icalendar
      - pytz
      - tzlocal
    bins: []
  install:
    command: "uv pip install --system caldav icalendar pytz tzlocal"
---

# icloud-caldav

Bezpieczny, produkcyjny skill OpenClaw do zarządzania kalendarzem **Apple iCloud** przez protokół **CalDAV**. Używa wyłącznie oficjalnej biblioteki `caldav` + `icalendar` — brak `pyicloud`, brak regexów, brak prymitywnego parsowania tekstu iCal, brak `curl`.

## Zakres

Skill obsługuje **wyłącznie wydarzenia kalendarza Apple iCloud**.

**NIE obsługuje:**

- iCloud Reminders (przypomnień)
- iCloud Notes, Contacts, Photos, Drive
- Kalendarzy Google, Outlook, Exchange (użyj dedykowanego skilla)
- Importu/eksportu plików `.ics` z dysku

Platforma: **Linux**, Python 3.10+.

---

## Security

### 1. Zero sekretów w plaintext

Apple ID i hasło czytane są **wyłącznie** ze zmiennych środowiskowych:

| Zmienna | Wartość |
|---|---|
| `ICLOUD_APPLE_ID` | Twój Apple ID (e-mail) |
| `ICLOUD_APP_PASSWORD` | **App-specific password** z https://appleid.apple.com |

Skill **nigdy** nie czyta tych wartości z plików `config.json`, `.yaml`, argumentów CLI ani nie wypisuje ich do logów. Zwykłe hasło Apple ID **nie zadziała** — iCloud CalDAV wymaga hasła app-specific.

### 2. Destrukcyjne operacje wymagają UID

- `delete-event <UID> --calendar NAME --force` — wymaga UID (RFC 5545), nazwy kalendarza **oraz** jawnej flagi `--force`. Bez `--force` skill odmawia i zwraca exit code 3.
- `update-event <UID> --calendar NAME ...` — operuje wyłącznie po UID. Brak "pierwszy pasujący po tytule" ani żadnej innej heurystyki.

**Agent wywołujący ten skill powinien ZAWSZE zapytać użytkownika o potwierdzenie przed `delete-event`.** Sam `--force` chroni przed pomyłkami CLI, nie zastępuje intencji użytkownika.

### 3. Zero parsowania tekstu

Wszystkie operacje wykorzystują obiekty `caldav.Calendar`, `caldav.Event` i komponenty `icalendar.Event`. Kod **nigdy** nie wykonuje `str.split`, `str.replace`, `re.match` ani żadnych operacji na surowym iCalu.

### 4. Izolowany klient TLS

Każda komenda otwiera i zamyka `caldav.DAVClient` w bloku `with`, co gwarantuje zamknięcie sesji HTTPS nawet przy wyjątku.

### 5. Strukturyzowane błędy

Wszystkie ścieżki błędów zwracają:

```json
{ "success": false, "error": "human readable", "detail": "optional class name" }
```

Exit code ≠ 0 (patrz tabela `Exit Codes` niżej). **Nigdy** nie drukują surowych stack-trace'ów na stdout — traceback pojawia się wyłącznie na stderr, i tylko gdy podany jest `--verbose`.

---

## Jak skonfigurować

### Krok 1 — Wygeneruj App-Specific Password

1. Zaloguj się na https://appleid.apple.com
2. Sekcja **Sign-In and Security** → **App-Specific Passwords**
3. Kliknij **Generate an app-specific password**, nazwij je np. `OpenClaw-CalDAV`
4. Skopiuj wygenerowane hasło (format `xxxx-xxxx-xxxx-xxxx`). Po zamknięciu dialogu Apple nie pokaże go drugi raz.

### Krok 2 — Ustaw zmienne środowiskowe

Zalecana metoda — plik `~/.openclaw/.env`:

```bash
ICLOUD_APPLE_ID=twoj.apple.id@icloud.com
ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
```

Ustaw restrykcyjne uprawnienia:

```bash
chmod 600 ~/.openclaw/.env
```

OpenClaw załaduje te zmienne przed uruchomieniem skilla. Alternatywnie wyeksportuj je w bieżącej sesji:

```bash
export ICLOUD_APPLE_ID="twoj.apple.id@icloud.com"
export ICLOUD_APP_PASSWORD="xxxx-xxxx-xxxx-xxxx"
```

### Krok 3 — Zainstaluj zależności

```bash
uv pip install --system caldav icalendar pytz tzlocal
```

(`pytz` jest fallbackiem dla `tzlocal` na starszych wersjach; `zoneinfo` z stdlib jest używane jako główne źródło stref.)

---

## Komendy

Wszystkie komendy zwracają JSON na stdout.

- **Sukces:** `{"success": true, "data": ...}` — exit 0
- **Błąd:**  `{"success": false, "error": "...", "detail": "..."}` — exit ≠ 0

### `list-calendars`

Lista wszystkich kalendarzy w Twoim iCloud.

```bash
python icloud_caldav_cli.py list-calendars
```

Przykładowy output:

```json
{
  "success": true,
  "data": [
    {
      "name": "home",
      "displayname": "Home",
      "url": "https://p01-caldav.icloud.com/12345/calendars/home/"
    },
    {
      "name": "work",
      "displayname": "Work",
      "url": "https://p01-caldav.icloud.com/12345/calendars/work/"
    }
  ]
}
```

### `list-events`

```bash
python icloud_caldav_cli.py list-events \
  --calendar "work" \
  --start 2026-04-01 \
  --end 2026-04-30 \
  --limit 100 \
  --timezone Europe/Warsaw
```

| Flaga | Wymagana | Opis |
|---|---|---|
| `--calendar` | tak | Nazwa kalendarza (sprawdź `list-calendars`) |
| `--start` | tak | ISO 8601: `YYYY-MM-DD` lub `YYYY-MM-DDTHH:MM[:SS]` |
| `--end` | tak | Jak wyżej, musi być ≥ `--start` |
| `--limit` | nie | Domyślnie 50 |
| `--timezone` | nie | Domyślnie lokalna strefa (via `tzlocal`) |

### `get-event`

Pobiera pojedyncze wydarzenie po UID. Bez `--calendar` przeszukuje wszystkie kalendarze.

```bash
# Znane miejsce
python icloud_caldav_cli.py get-event "ABCD-1234-EFGH-5678" --calendar "work"

# Nieznane miejsce — skanuje wszystkie kalendarze
python icloud_caldav_cli.py get-event "ABCD-1234-EFGH-5678"
```

### `create-event`

```bash
python icloud_caldav_cli.py create-event \
  --calendar "work" \
  --title "Sprint Planning" \
  --start "2026-04-10T10:00" \
  --end "2026-04-10T11:30" \
  --description "Q2 roadmap review" \
  --location "Room 4B" \
  --timezone "Europe/Warsaw"
```

| Flaga | Wymagana | Opis |
|---|---|---|
| `--calendar` | tak | Docelowy kalendarz |
| `--title` | tak | Niepusty string |
| `--start` | tak | ISO 8601 |
| `--end` | tak | Musi być > `--start` |
| `--description` | nie | Dowolny tekst |
| `--location` | nie | Dowolny tekst |
| `--timezone` | nie | Domyślnie lokalna |

Skill generuje nowy UUID v4 jako UID wydarzenia i zwraca go w `data.uid` — **zachowaj go**, jeśli planujesz późniejszy update lub delete.

### `update-event`

Wymaga UID oraz `--calendar`. Przynajmniej jedno z `--title`, `--start`, `--end`, `--description`, `--location` musi być podane.

```bash
python icloud_caldav_cli.py update-event "ABCD-1234-EFGH-5678" \
  --calendar "work" \
  --title "Sprint Planning (moved)" \
  --start "2026-04-11T10:00" \
  --end "2026-04-11T11:30"
```

Update jest atomowy per wywołanie — wydarzenie jest wczytywane, modyfikowane na poziomie komponentów `icalendar`, a następnie zapisywane jako całość. `LAST-MODIFIED` jest automatycznie aktualizowane.

### `delete-event`

```bash
python icloud_caldav_cli.py delete-event "ABCD-1234-EFGH-5678" \
  --calendar "work" \
  --force
```

**Bez `--force`** → błąd i exit code 3. Flaga `--force` to zabezpieczenie przed pomyłką na CLI. Agent wywołujący skill **zawsze** powinien najpierw zapytać użytkownika o potwierdzenie.

### `search-events`

Pełnotekstowe, case-insensitive wyszukiwanie po polach `summary`, `description`, `location` (client-side filter — działa identycznie na każdym serwerze CalDAV).

```bash
python icloud_caldav_cli.py search-events "sprint" --calendar "work" --limit 20
```

---

## Flagi globalne

| Flaga | Opis |
|---|---|
| `--verbose` | Włącza `DEBUG` na stderr. Nigdy nie loguje haseł ani tokenów. |
| `--json` | Kompatybilność wsteczna; JSON jest zawsze domyślnym outputem. |

---

## Exit Codes

| Code | Znaczenie |
|---:|---|
| 0 | Sukces |
| 1 | Generyczny błąd logiki |
| 2 | Brak zależności lub nieznana komenda |
| 3 | Odmowa `delete-event` bez `--force` |
| 4 | Brak zmiennych `ICLOUD_APPLE_ID` / `ICLOUD_APP_PASSWORD` |
| 5 | Błąd autoryzacji (złe hasło, wygasłe app-specific password) |
| 6 | Kalendarz lub wydarzenie nie istnieje |
| 7 | Inny błąd CalDAV / HTTP |
| 8 | Problem z połączeniem TCP do `caldav.icloud.com` |
| 9 | Timeout |
| 10 | HTTP error |
| 11 | Inny błąd sieciowy |
| 12 | `LookupError` (np. kalendarz nieznaleziony, UID poza kalendarzem) |
| 13 | `ValueError` (zła data, pusty tytuł, `end <= start`) |
| 14 | `EnvironmentError` |
| 99 | Nieoczekiwany wyjątek (uruchom z `--verbose`, żeby zobaczyć traceback na stderr) |

---

## Troubleshooting

| Problem | Przyczyna | Rozwiązanie |
|---|---|---|
| `Authorization failed` (exit 5) | Używasz zwykłego hasła | Wygeneruj app-specific password |
| `Authorization failed` po migracji konta | Stare hasło app-specific zostało unieważnione | Wygeneruj nowe i zaktualizuj `ICLOUD_APP_PASSWORD` |
| `Calendar not found` | Zła nazwa, case-sensitive | Użyj `list-calendars`, skopiuj `name` 1:1 |
| `Cannot reach iCloud CalDAV server` | Firewall / DNS / VPN | Sprawdź `curl -I https://caldav.icloud.com` |
| Puste `list-events` | Zły timezone lub zakres | Dodaj `--timezone Europe/Warsaw`, rozszerz zakres |
| `Event not found` w update/delete | UID z innego kalendarza | Użyj `get-event <UID>` bez `--calendar`, żeby znaleźć właściwy kalendarz |

---

## Jak agent powinien korzystać z tego skilla

1. **Nigdy** nie pytaj użytkownika o hasło w chacie. Sprawdź zmienne środowiskowe i poproś o ich ustawienie, jeśli brakuje.
2. Przed operacjami na konkretnym kalendarzu wywołaj `list-calendars`, żeby ustalić dokładną nazwę.
3. Dla `delete-event`:
   - **Najpierw** wywołaj `get-event <UID>`, pokaż użytkownikowi pełne dane.
   - Zapytaj: "Czy na pewno chcesz usunąć to wydarzenie?".
   - **Dopiero po potwierdzeniu** wywołaj `delete-event <UID> --calendar NAME --force`.
4. Dla `update-event`:
   - Pobierz aktualną wersję przez `get-event`.
   - Pokaż użytkownikowi proponowaną zmianę (diff-style).
   - Po akceptacji wywołaj `update-event` z odpowiednimi flagami.
5. Przy tworzeniu wydarzeń zawsze zwróć użytkownikowi `uid` z odpowiedzi — bez niego nie da się ich później zmienić ani usunąć.

---

## Przykładowy flow

```bash
# 1. Zobacz dostępne kalendarze
python icloud_caldav_cli.py list-calendars

# 2. Znajdź konflikty w przyszłym tygodniu
python icloud_caldav_cli.py list-events \
  --calendar "work" \
  --start 2026-04-13 \
  --end 2026-04-19

# 3. Utwórz nowe spotkanie
python icloud_caldav_cli.py create-event \
  --calendar "work" \
  --title "1:1 z Anną" \
  --start "2026-04-15T14:00" \
  --end   "2026-04-15T14:30" \
  --location "Meet"

# 4. Zapisz UID z odpowiedzi (np. 9f2e...)

# 5. Przesuń spotkanie
python icloud_caldav_cli.py update-event 9f2e... \
  --calendar "work" \
  --start "2026-04-15T15:00" \
  --end   "2026-04-15T15:30"

# 6. Usuń (po potwierdzeniu użytkownika!)
python icloud_caldav_cli.py delete-event 9f2e... --calendar "work" --force
```
