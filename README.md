# job-scout

Täglicher Scraper (GitHub Actions + Playwright) für Stellen im Bereich Realtime-/Game-Art, 3D, Illustration
in Österreich und remote (EU). Eine geplante Claude-Aufgabe liest `data/recent.json`, bewertet die Anzeigen
und schickt die Liste.

- Quellen & Suchbegriffe: `config.yaml`
- Ergebnisse: `data/latest.json` (dieser Lauf), `data/new.json` (neu), `data/recent.json` (letzte Tage), `data/status.json`
- Diagnose: `debug/` (Treffer pro URL, Fehler, Screenshots)
- Manuell starten: Actions → scout → Run workflow
