# LP Quote Server

Always-on Python server that turns call notes into ready-to-send quotes in Luke's voice, saves leads to Airtable, and books jobs into Google Calendar via one-tap links.

## Endpoints

All endpoints require header `X-API-Key: <LP_SHARED_SECRET>`.

- `POST /api/quote`  body: `{"notes": "..."}` → `{message, parsed, record_id, airtable_url}`
- `POST /api/update` body: `{"name"|"record_id", "edit": "..."}` → updated message
- `POST /api/book`   body: `{"name"|"record_id", "date":"YYYY-MM-DD", "time"?:"HH:MM"}` → `{calendar_url, ...}`
- `GET  /api/find`   optional `?name=Sarah` → recent or matching leads

UI lives at `/` (mobile-first, prompts once for the shared secret).

## Deploy to Vercel

1. `cd lp-quote-server && git init && git add . && git commit -m "init"`
2. Push to a new GitHub repo.
3. In Vercel: New Project → Import the repo → framework = Other → deploy.
4. Project Settings → Environment Variables: paste everything from `.env.example` with real values.
5. Redeploy. Done.

## iOS Shortcut

- Action: **Get Contents of URL**
- URL: `https://<your-vercel>.vercel.app/api/quote`
- Method: `POST`
- Headers: `X-API-Key: <LP_SHARED_SECRET>`, `Content-Type: application/json`
- Request Body (JSON): `{"notes": <Dictated Text>}`
- Then **Get Dictionary Value** → `message` → **Show Result** (or Copy to Clipboard).

Trigger via Siri: "Hey Siri, new quote".

## Why one-tap calendar instead of full OAuth

`/api/book` returns a Google Calendar `render?action=TEMPLATE&...` URL. Tap it on your phone, hit save, done. No OAuth, no service account, no token refresh nightmare. v2 can swap to the real Calendar API if you want auto-create.
