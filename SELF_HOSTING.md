# Self-hosting Coffernest

Coffernest runs anywhere Docker does: a home server, a Raspberry Pi 5, a Jetson, or a small VPS.
It needs about 1.5 GB of RAM including Postgres and MinIO.

## 1. Start it

```bash
git clone https://github.com/vkfolio/coffernest && cd coffernest
cp .env.selfhost.example .env      # fill in the secrets (openssl rand -hex 32)
docker compose up -d --build
curl http://localhost:8080/health  # {"ok":true,"db":true,"s3":true}
```

In the Coffernest iPhone app, choose **Connect to a server**, enter `http://<this-machine>:8080`
and sign up: the first account on a server becomes its admin. To bring in family, add their
emails in Settings › Family; each person then signs up with that email and joins your household
as a member. Nobody else can sign up unless you set `ALLOW_REGISTRATION=true`.

The server is the API, Postgres and MinIO.

**Skip the build:** each release is also published as a ready-made image for arm64 and amd64.
In `docker-compose.yml`, replace `build: ./api` with
`image: ghcr.io/vkfolio/coffernest:latest` (or a version such as `:0.2.0`), then
`docker compose pull && docker compose up -d`.

### A demo account (optional)

Set `SEED_DEMO=true`, `DEMO_EMAIL` and `DEMO_PASSWORD` (10+ characters, your own choice) and the
server keeps a demo account with a year of sample books. It's created on start, brought back if
someone deletes it, and started afresh every `DEMO_REFRESH_DAYS` (30). It sits in its own
household and doesn't count as the first account, so the first real sign-up still becomes the
admin. Nobody can sign up with the demo email themselves.

## 2. Reach it from phones

At home, the app can use `http://<this-machine>:8080` directly. To reach it from anywhere, put a
reverse proxy in front of port 8080 (Caddy, Traefik, nginx, or a tunnel such as Cloudflare Tunnel
or Tailscale Funnel) and set `PUBLIC_URL` to that HTTPS address. MinIO (port 9000) must be reachable at `S3_PUBLIC_URL`
because the apps upload bills and statements to it directly.

If your proxy passes the visitor's IP in a header, set `TRUSTED_PROXY_HEADER` (for example
`CF-Connecting-IP` or `X-Real-IP`) so sign-in lockouts count the right address. Leave it
empty if the API is reached directly.

## 3. The assistant (optional)

With `AI_PROVIDER=none` everything works except the built-in assistant and reading PDF
statements. To turn it on:

| Provider | Settings |
|---|---|
| Claude | `AI_PROVIDER=claude_agent` and `ANTHROPIC_API_KEY` (or `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`) |
| OpenAI-compatible | `AI_PROVIDER=openai_compat`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`. Works with OpenAI, OpenRouter, Ollama and LM Studio |

The household owner then turns the assistant on in Settings. Questions and the figures needed to
answer them go to the provider you chose; tell your household before turning it on. The iOS app
can also use Apple Intelligence on the phone, which needs no server setup.

## 4. Backups and updates

- Settings → Backup downloads an encrypted `.cnz` file of the whole household.
- Back up the Postgres and MinIO volumes, and keep `SECRET_ENC_KEY` and `JWT_SECRET` safe.
- Update with `git pull && docker compose up -d --build` (or `docker compose pull && docker
  compose up -d` with the ready-made image). Database migrations run on start.

## 5. Privacy pages

The server serves a privacy policy at `/privacy` and terms at `/terms`. If you run Coffernest for
people outside your household, review them and point `PRIVACY_URL` and `TERMS_URL` at your own.
