# PSN Messenger

Send messages to a PSN group chat from your Redragon SS-552 stream deck or any HTTP client.

## Architecture

- Python FastAPI service using PSNAWP library
- Authenticates with PSN via NPSSO token
- Exposes HTTP endpoints for sending messages
- StreamDock plugin calls the API on button press

## Endpoints

- `GET /health` — status check
- `POST /send` — send a message (`{"message": "text"}`)
- `GET /messages?limit=5` — get recent messages

## Coolify Deployment

1. Create new resource → Docker build pack
2. Connect repo: `Qureshi-Inc/psn-messenger`
3. Environment variables:
   ```
   NPSSO_TOKEN=your-64-char-npsso-token
   GROUP_ID=9b1ba8e02ad17050e3fa4351685b48c40688e8ba-351
   ```
4. Health check: `/health`
5. Port: 3000

## Getting NPSSO Token

1. Log into https://my.playstation.com/
2. Visit https://ca.account.sony.com/api/v1/ssocookie
3. Copy the `npsso` value (64 characters)

Note: Token lasts ~2 months. Generating a new one invalidates the old one.
