# API Reference

Base URL: `http://localhost:8000`

Ogni richiesta (tranne `/health`) richiede header:

```
Authorization: Bearer <token>
```

Il token deve corrispondere a uno di quelli configurati in `API_TOKENS` (dashboard `/ui/config` o variabile d'ambiente, comma-separated). Se `API_TOKENS` è vuoto, gli endpoint restano aperti — va impostato prima di esporre il servizio oltre `localhost`.

---

## Account e credenziali

Le credenziali IMAP/SMTP non si passano nella request. Ogni endpoint accetta `"account": "<nome>"` e il server risolve le credenziali dalle variabili d'ambiente:

| Variabile | Descrizione |
|-----------|-------------|
| `IMAP_<ACCOUNT>_HOST` | Hostname IMAP |
| `IMAP_<ACCOUNT>_PORT` | Porta (default `993`) |
| `IMAP_<ACCOUNT>_USERNAME` | Utente |
| `IMAP_<ACCOUNT>_PASSWORD` | Password |
| `IMAP_<ACCOUNT>_SSL` | SSL/TLS (default `true`) |
| `SMTP_<ACCOUNT>_HOST` | Hostname SMTP |
| `SMTP_<ACCOUNT>_PORT` | Porta (default `587`) |
| `SMTP_<ACCOUNT>_USERNAME` | Utente |
| `SMTP_<ACCOUNT>_PASSWORD` | Password |
| `SMTP_<ACCOUNT>_STARTTLS` | STARTTLS (default `true`) |
| `SMTP_<ACCOUNT>_SSL` | SSL diretto (default `false`) |

Il nome account viene convertito in maiuscolo: `"danilo"` → `IMAP_DANILO_HOST`. Account non configurato → `400`.

---

## GET /health

Liveness check. Nessun header richiesto.

**Response**
```json
{ "status": "ok" }
```

---

## POST /folders

Lista le cartelle IMAP dell'account.

**Body**
```json
{
  "account": "danilo"
}
```

**Response**
```json
{
  "folders": ["INBOX", "Sent", "Trash", "Spam"]
}
```

---

## POST /messages/list

Recupera gli header di tutti i messaggi a partire da un UID. Utile per sincronizzazione incrementale.

**Body**
```json
{
  "account": "danilo",
  "folder": "INBOX",
  "since_uid": "1040",
  "limit": 100
}
```

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `folder` | string | `"INBOX"` | Cartella da listare |
| `since_uid` | string | `null` (tutti) | Ritorna UID >= questo valore |
| `limit` | int | `100` | Max messaggi; `null` o assente = 100, negativo = tutti |

**Response**
```json
{
  "folder": "INBOX",
  "since_uid": "1040",
  "count": 3,
  "messages": [
    {
      "uid": "1040",
      "message_id": "<abc@mail.com>",
      "from": "Mittente <m@example.com>",
      "to": "tuo@email.com",
      "cc": "",
      "subject": "Oggetto",
      "date": "Mon, 1 Jan 2025 10:00:00 +0100",
      "flags": ["\\Seen"],
      "size": 3210
    }
  ]
}
```

---

## POST /messages/get

Recupera il contenuto completo di un singolo messaggio: header, corpo testo/HTML e lista allegati.

**Body**
```json
{
  "account": "danilo",
  "folder": "INBOX",
  "uid": "1042"
}
```

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `folder` | string | `"INBOX"` | Cartella contenente il messaggio |
| `uid` | string | — | UID del messaggio |
| `include_attachments` | bool | `false` | Se `true`, include il contenuto degli allegati in base64 |

**Response**
```json
{
  "uid": "1042",
  "message_id": "<abc123@mail.com>",
  "from": "Mittente <m@example.com>",
  "to": "tuo@email.com",
  "cc": "",
  "subject": "Fattura marzo",
  "date": "Mon, 1 Jan 2025 10:00:00 +0100",
  "flags": ["\\Seen"],
  "body_text": "Testo in chiaro...",
  "body_html": "<html>...</html>",
  "attachments": [
    {
      "filename": "fattura.pdf",
      "content_type": "application/pdf",
      "size": 48210,
      "data": "JVBERi0xLjQK..."
    }
  ]
}
```

`body_text` e `body_html` sono `null` se la parte non è presente. Il campo `data` (base64) è presente negli allegati solo se `include_attachments: true`. Ritorna `404` se l'UID non esiste.

---

## POST /messages/search

Cerca messaggi in una cartella.

**Body**
```json
{
  "account": "danilo",
  "folder": "INBOX",
  "limit": 50,
  "criteria": {
    "unseen": true,
    "from": "mittente@example.com",
    "subject": "fattura",
    "since": "01-Jan-2025",
    "before": "01-Jun-2025"
  }
}
```

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `folder` | string | `"INBOX"` | Cartella da cercare |
| `limit` | int | `50` | Max messaggi restituiti (i più recenti) |
| `criteria` | object | `{}` (ALL) | Criteri di ricerca (vedi sotto) |

**Criteri di ricerca disponibili:**

| Chiave | Tipo | Descrizione |
|--------|------|-------------|
| `unseen` | bool | Solo messaggi non letti |
| `seen` | bool | Solo messaggi letti |
| `from` | string | Filtro mittente |
| `to` | string | Filtro destinatario |
| `subject` | string | Filtro oggetto |
| `since` | string | Dopo data (`DD-Mon-YYYY`, es. `01-Jan-2025`) |
| `before` | string | Prima di data (`DD-Mon-YYYY`) |
| `body` | string | Testo nel corpo |
| `raw` | string | Stringa IMAP grezza (es. `"UID 100:200"`) |

**Response**
```json
{
  "folder": "INBOX",
  "criteria": "UNSEEN FROM \"mittente@example.com\"",
  "count": 2,
  "messages": [
    {
      "uid": "1042",
      "message_id": "<abc123@mail.com>",
      "from": "Mittente <mittente@example.com>",
      "to": "tuo@email.com",
      "cc": "",
      "subject": "Fattura marzo",
      "date": "Mon, 1 Jan 2025 10:00:00 +0100",
      "flags": [],
      "size": 4821
    }
  ]
}
```

---

## POST /messages/delete

Cancella messaggi per UID ed esegue expunge.

**Body**
```json
{
  "account": "danilo",
  "folder": "INBOX",
  "uids": ["1042", "1043"]
}
```

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `folder` | string | `"INBOX"` | Cartella contenente i messaggi |
| `uids` | string[] | — | Lista UID da cancellare |

**Response**
```json
{
  "deleted": ["1042", "1043"]
}
```

---

## POST /messages/move

Sposta messaggi in un'altra cartella. Usa il comando IMAP `MOVE` (RFC 6851) se supportato dal server, altrimenti `COPY` + `DELETE` + expunge.

**Body**
```json
{
  "account": "danilo",
  "folder": "INBOX",
  "uids": ["1042"],
  "destination": "Archivio"
}
```

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `folder` | string | `"INBOX"` | Cartella sorgente |
| `uids` | string[] | — | Lista UID da spostare |
| `destination` | string | — | Cartella destinazione |

**Response**
```json
{
  "moved": ["1042"],
  "destination": "Archivio"
}
```

---

## POST /messages/flag

Marca messaggi come letti o non letti.

**Body**
```json
{
  "account": "danilo",
  "folder": "INBOX",
  "uids": ["1042", "1043"],
  "action": "read"
}
```

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `folder` | string | `"INBOX"` | Cartella contenente i messaggi |
| `uids` | string[] | — | Lista UID |
| `action` | string | — | `"read"` o `"unread"` |

**Response**
```json
{
  "uids": ["1042", "1043"],
  "action": "read"
}
```

---

## POST /messages/send

Invia un messaggio via SMTP.

**Body**
```json
{
  "account": "danilo",
  "from_addr": "tuo@email.com",
  "to": ["destinatario@example.com"],
  "cc": ["copia@example.com"],
  "bcc": ["nascosto@example.com"],
  "subject": "Oggetto",
  "body": "Testo del messaggio",
  "html": false
}
```

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `from_addr` | string | — | Indirizzo mittente |
| `to` | string[] | — | Destinatari |
| `cc` | string[] | `[]` | CC |
| `bcc` | string[] | `[]` | BCC |
| `subject` | string | — | Oggetto |
| `body` | string | — | Corpo del messaggio |
| `html` | bool | `false` | `true` per corpo HTML |

**Response**
```json
{
  "sent": true,
  "to": ["destinatario@example.com"],
  "cc": ["copia@example.com"],
  "bcc": ["nascosto@example.com"]
}
```

---

## Errori

| Codice | Causa |
|--------|-------|
| `401` | Token Bearer assente o non valido (solo se `API_TOKENS` è configurato) |
| `429` | Rate limit superato (`RATE_LIMIT`, configurabile da dashboard) |
| `400` | Account non configurato, o `action` non valida in `/messages/flag` |
| `404` | UID non trovato (`/messages/get`) |
| `500` | Errore IMAP/SMTP (credenziali errate, server non raggiungibile, cartella inesistente) |

---

## Dashboard

Una dashboard web (NiceGUI, protetta da login separato dall'API) è disponibile su `/ui` — mostra richieste totali/ok/errori, durata media e lo storico delle ultime chiamate (endpoint, account, status, durata, errore). Configurazione runtime (rate limit, token API, auto-refresh) in `/ui/config`. Vedi `CLAUDE.md` per dettagli su autenticazione e setup.

```json
{ "detail": "messaggio di errore" }
```
