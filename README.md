# Wave Archive

Private audio intelligence workspace and Python 3 server-side backend for durable audio ingestion, speech-to-text,
speaker diarization, transcript merging, and optional analysis. The HTTP API is
built with FastAPI; long-running AI work is performed by an independent worker
and never blocks an upload request.

The Raspberry Pi recorder/uploader is a **separate Git repository**. This
repository does not record from microphones, control GPIO, chunk audio on a
client, maintain the recorder's local upload queue, or install recorder
systemd services.

## Architecture

```text
Raspberry Pi / iPhone / future client
                   │
                   │ authenticated multipart upload
                   ▼
              FastAPI API ◀──────── Nginx web gateway
          validation + metadata       React management UI
             │           │            same-origin cookie auth
             │           └──────── PostgreSQL
             │                    recordings + jobs + web auth
             ▼                              │
 Local StorageBackend                       │ lease/claim
 original audio, unchanged                  ▼
                                      Worker process
                                ffmpeg normalize to 16 kHz mono PCM
                                             │
                                      faster-whisper
                                             │
                                      pyannote.audio
                                             │
                                  timestamp/speaker merge
                                             │
                                  optional LM Studio analysis
                                  (transcript only; never audio)
                                             │
                                             ▼
                                      PostgreSQL
                                  segments + job state
```

Upload and processing have deliberately different lifecycles:

1. The API authenticates the caller and streams the upload while enforcing the
   configured byte limit and calculating SHA-256.
2. It validates metadata, checksum, timestamps, and the media with `ffprobe`.
3. It durably stores the unchanged original, inserts a `Recording` and a
   `ProcessingJob`, then returns the recording ID.
4. A worker claims the PostgreSQL-backed job and runs preprocessing,
   transcription, diarization, merging, and optional analysis.
5. Clients poll status and retrieve the segment-level transcript.

PostgreSQL is both the application database and the MVP job queue. This avoids
a database/Redis dual-write and is sufficient for a small number of expensive,
long-running AI jobs. It uses leases and `FOR UPDATE SKIP LOCKED` so workers do
not hold a transaction during model inference. The queue provides at-least-once
processing, not exactly-once processing.

## Requirements

The recommended development path is Docker Compose. It requires:

- Docker Engine or Docker Desktop with Compose v2
- Enough disk for PostgreSQL data, recordings, and model caches
- Enough RAM for the selected Whisper and diarization models
- Access to Hugging Face when speaker diarization is enabled

A native installation requires:

- Python 3.11
- Node.js 22 for frontend development and production SPA builds
- PostgreSQL 16 (older supported PostgreSQL versions may work but are not the
  tested Compose target)
- `ffmpeg` and `ffprobe` on `PATH`
- The AI dependency extra for a worker: `faster-whisper`, `pyannote.audio`, and
  their runtime dependencies

AI models are not baked into either Docker image. They download into the model
cache volume on first use.

## Docker quick start

Create a private configuration file and generate three local secrets:

```bash
install -m 600 .env.example .env
python3 -c 'import secrets; print(secrets.token_hex(32))'
python3 -c 'import secrets; print(secrets.token_hex(24))'
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

Put the first value in `API_TOKEN`, the second in `POSTGRES_PASSWORD`, and the
third in `WEB_SETUP_TOKEN` in `.env`. The setup token authorizes only creation
of the first web administrator. Do not commit `.env`. If diarization is
enabled, also complete the [pyannote setup](#speaker-diarization) and set
`HUGGINGFACE_TOKEN`.

Start PostgreSQL, run Alembic, and start the API and one CPU worker:

```bash
docker compose up --build
```

Open `http://127.0.0.1:3000/setup`, create the administrator using the
`WEB_SETUP_TOKEN` value, and then sign in at `/login`. Setup does not log in
automatically. Afterwards, remove `WEB_SETUP_TOKEN` from `.env` and recreate
the API container to disable the setup credential:

```bash
docker compose up -d --force-recreate api
```

Compose exposes the web gateway at `http://127.0.0.1:3000`, the machine API at
`http://127.0.0.1:8000`, and PostgreSQL at `127.0.0.1:5432` by default. Keep
all published interfaces on loopback for local use. Production deployments
should publish only the web gateway to a trusted HTTPS ingress and should not
publish the API or PostgreSQL. The `migrate` service must finish successfully
before the API, web gateway, or worker starts. Runtime state is kept in three
named volumes:

- `postgres-data`: database data
- `recordings-data`: original and derived audio
- `model-cache`: Hugging Face and PyTorch model caches

Compose reads `.env` for variable interpolation but does not inject the whole
file into every container. The migration service receives only its database
URL, the API alone receives `API_TOKEN` and web-auth settings, and the worker
alone receives Hugging Face and LLM credentials. The web container receives no
runtime environment variables or secrets. Named volumes are likewise scoped to
the services that need them; migration and web cannot access recordings.

Useful operations:

```bash
docker compose ps
docker compose logs -f web api worker
docker compose run --rm migrate
docker compose stop
```

Stopping containers keeps all named volumes. Do not run `docker compose down -v`
unless permanent deletion of database, audio, and cached models is
intentional and separately approved.

## Native development

Create a Python 3.11 environment and install the API plus development tools:

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

To run an AI worker locally, install the AI extra using the PyTorch build that
matches the machine first, then install the project extra. For example, on
Linux CPU:

```bash
.venv/bin/pip install \
  --index-url https://download.pytorch.org/whl/cpu \
  'torch==2.11.0' 'torchaudio==2.11.0' 'torchcodec==0.13.0'
.venv/bin/pip install -e '.[dev,ai]'
```

macOS and CUDA hosts require the matching PyTorch installation instructions
for that platform instead of the Linux CPU command. TorchCodec is ABI-coupled
to PyTorch; verify both versions against its official compatibility table
before changing either pin.

Use the same `install -m 600 .env.example .env` command, set `DATABASE_URL`,
`API_TOKEN`, and the relevant model settings, then migrate and run the two
processes in separate terminals:

```bash
.venv/bin/alembic upgrade head
.venv/bin/uvicorn audio_server.main:app --host 127.0.0.1 --port 8000 --reload
```

```bash
.venv/bin/python -m audio_server.jobs.worker
```

The equivalent installed commands are `audio-server` and `audio-worker`.
Application and worker processes must point to the same PostgreSQL database and
storage root.

For native frontend development, use Node.js 22 and run the API on port 8000:

```bash
cd web
npm ci
npm run dev
```

Vite serves `http://127.0.0.1:5173` and proxies `/api` and `/health` to the API.
Set `WEB_ALLOWED_ORIGIN=http://127.0.0.1:5173` while using it because setup,
login, and logout require an exact Origin match.

## Configuration

Settings are read from environment variables and, for local development, a
gitignored `.env` file. Empty or placeholder production secrets are invalid.

| Variable | Default/example | Purpose |
| --- | --- | --- |
| `APP_ENV` | `development` | Runtime environment; use a production value for deployment safety checks. |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | Application log level and `json` or `plain` output. |
| `DOCS_ENABLED` | `false` | Enable OpenAPI schema and interactive documentation. Always forced off when `APP_ENV=production`, because those routes are unauthenticated. |
| `API_TOKEN` | empty | Machine Bearer credential required for upload/retry and accepted for recording reads. |
| `WEB_SETUP_TOKEN` | empty | One-time secret of at least 32 characters; leave empty after the first administrator exists. |
| `WEB_SESSION_HOURS` | `12` | Lifetime of an opaque browser session. |
| `WEB_COOKIE_SECURE` | `false` | Set `true` whenever the browser uses HTTPS. |
| `WEB_ALLOWED_ORIGIN` | `http://127.0.0.1:3000` | Exact browser origin accepted for state-changing auth requests; no wildcard or path. |
| `WEB_AUTH_MAX_REQUEST_BYTES` | `4096` | Total request-body cap for setup and login JSON. |
| `WEB_LOGIN_MAX_ATTEMPTS` / `WEB_LOGIN_WINDOW_SECONDS` | `5` / `300` | Process-local failed-login limit and window. |
| `WEB_LOGIN_RATE_LIMIT_ENTRIES` | `2048` | Bound on process-local login limiter keys. |
| `TRUSTED_PROXY_IPS` | empty | Comma-separated addresses or CIDR ranges whose `X-Forwarded-For` header is honoured when identifying a login client. Empty trusts no proxy and uses the direct peer address. |
| `DATABASE_URL` | local PostgreSQL URL | SQLAlchemy PostgreSQL URL used outside Compose. |
| `STORAGE_PATH` | `./data` | Root for originals, work files, staging, and local model caches. |
| `MAX_UPLOAD_BYTES` | `536870912` | Exact maximum number of bytes in the `audio` part, 512 MiB by default. |
| `MAX_METADATA_BYTES` | `16384` | Maximum JSON metadata part, 16 KiB by default. |
| `MAX_AUDIO_DURATION_SECONDS` | `21600` | Maximum probed audio duration, six hours by default. |
| `MAX_MUTATION_REQUEST_BYTES` | `33554432` | Request-body cap for every non-upload `/api/v1` request, 32 MiB by default. Raise it alongside `MAX_AUDIO_DURATION_SECONDS`. |
| `FFMPEG_BINARY` / `FFPROBE_BINARY` | executable names | Explicit binary locations when not on `PATH`. |
| `FFMPEG_TIMEOUT_SECONDS` | `3600` | Upper bound for preprocessing. |
| `PROCESSING_WORKERS` | `1` | Number of worker processes; each loads its own model copies. |
| `WORKER_JOB_KINDS` | empty | Job kinds this worker may claim: `full`, `analysis`, or both. Empty means every kind. |
| `PROCESSING_MAX_ATTEMPTS` | `3` | Automatic attempt limit for retryable failures. |
| `JOB_POLL_SECONDS` | `1` | Delay when no job is available. |
| `JOB_HEARTBEAT_SECONDS` | `30` | Active-job heartbeat interval. |
| `JOB_LEASE_SECONDS` | `300` | Time before an unresponsive claim can be recovered. |
| `JOB_RECOVERY_SECONDS` | `30` | Interval for recovering expired claims. |
| `RETRY_BASE_SECONDS` / `RETRY_MAX_SECONDS` | `30` / `900` | Exponential retry backoff bounds. |
| `WHISPER_MODEL` | `small` | faster-whisper model name or path. |
| `WHISPER_DEVICE` | `cpu` | `cpu` or an explicitly configured CUDA device. |
| `WHISPER_COMPUTE_TYPE` | `int8` | CTranslate2 compute type; `float16` is typical for CUDA. |
| `WHISPER_LANGUAGE` | blank/unset | Leaving it blank enables language detection; set `ja` only when input is known to be Japanese. |
| `WHISPER_CPU_THREADS` | `4` | CPU threads allocated to a Whisper model instance. |
| `WHISPER_CACHE_DIR` | model cache path | faster-whisper download/cache root. |
| `DIARIZATION_ENABLED` | `true` | Enables pyannote speaker diarization. |
| `DIARIZATION_MODEL` | Community-1 | Gated Hugging Face model ID. |
| `DIARIZATION_DEVICE` | `cpu` | Explicit pyannote execution device. |
| `HUGGINGFACE_TOKEN` | empty | Read token for the gated diarization model; never logged. |
| `PYANNOTE_METRICS_ENABLED` | `0` | Disables pyannote telemetry in the supplied runtime. |
| `HF_HOME` / `TORCH_HOME` | cache paths | Persistent model-cache roots. |
| `LLM_ENABLED` | `false` | Optional analysis switch; transcription does not depend on it. |
| `LLM_PROVIDER` | `disabled` | Set `lmstudio` together with `LLM_ENABLED=true`. |
| `LM_STUDIO_HOST` | `127.0.0.1:1234` | LM Studio SDK server as `host:port`; from Docker use the 5090 host's trusted-LAN address. |
| `LM_STUDIO_API_KEY` | empty | Optional LM Studio API token injected only into the worker. |
| `LM_STUDIO_TIMEOUT_SECONDS` | `600` | Inference inactivity timeout. |
| `LM_STUDIO_CHUNK_CHARS` | `12000` | Maximum transcript characters per map-analysis chunk. |
| `LM_STUDIO_MAX_TOKENS` | `4096` | Structured output token cap for every map/reduce layer. |
| `AUDIO_RETENTION_DAYS` / `TRANSCRIPT_RETENTION_DAYS` | blank/unset | Optional positive policy values; the MVP has no automatic cleanup worker. |

The Compose-only variables `POSTGRES_DB`, `POSTGRES_USER`,
`POSTGRES_PASSWORD`, `POSTGRES_BIND_HOST`, `POSTGRES_PORT`, `API_BIND_HOST`,
`API_PORT`, `WEB_BIND_HOST`, and `WEB_PORT` configure its local services and
published interfaces. All bind hosts default to `127.0.0.1`.
`PYTORCH_VERSION` and `TORCHCODEC_VERSION` are paired build arguments for the
CPU worker. Use URL-safe PostgreSQL credentials because Compose constructs the
internal `DATABASE_URL` from these values.

Before multipart parsing, the API enforces a total request-body guard derived
as `MAX_UPLOAD_BYTES + MAX_METADATA_BYTES + 1 MiB` of multipart overhead. The
streaming storage limit still independently enforces the exact number of audio
bytes. Every other `/api/v1` request is capped at `MAX_MUTATION_REQUEST_BYTES`
so transcript and analysis edits cannot be buffered without bound before
validation.

A transcript edit is deliberately not limited by segment count. The merge stage
emits one segment per contiguous same-speaker run, which is per word when
attribution alternates, so a dense multi-hour conversation legitimately reaches
tens of thousands of segments; and because an edit must submit the complete
current segment list, any count ceiling would leave those recordings
permanently uneditable. The byte cap is the only bound, and it must therefore
stay above the largest complete transcript the duration ceiling allows. At the
six-hour default that payload is roughly 8 MiB against a 32 MiB cap, so raise
`MAX_MUTATION_REQUEST_BYTES` whenever `MAX_AUDIO_DURATION_SECONDS` grows.

A production reverse proxy must also impose a total request-body limit;
set it to the derived application cap or slightly higher so the API, rather
than an unbounded ingress buffer, remains the final validator.

### Client identification behind a reverse proxy

The failed-login limiter buckets attempts by client address. Behind a reverse
proxy every request arrives from the proxy, so without `TRUSTED_PROXY_IPS` all
browser clients share a single bucket and any visitor can exhaust the window
for the administrator. Set `TRUSTED_PROXY_IPS` to the address or CIDR range of
the proxy itself; the API then reads `X-Forwarded-For` from right to left,
skipping further trusted hops, so a client-supplied prefix cannot forge an
identity.

Only list peers that always pass through the proxy. In the bundled Compose
topology the API port is also published directly, and traffic arriving on it
appears to originate from the same Docker bridge range as the web container, so
a broad range such as `172.16.0.0/12` would also let a direct client forge its
address. Either publish the API exclusively through the proxy before enabling
this, or pin the proxy to a known address and trust only that.

## Authentication

The browser UI uses an opaque database-backed session. Login sets an HttpOnly
`audio_server_session` cookie and a readable SameSite Strict
`audio_server_csrf` cookie. The SPA reads the CSRF cookie only for logout,
browser upload, editing, reprocessing, and deletion and echoes it in
`X-CSRF-Token`; it never stores
credentials in `localStorage` or `sessionStorage`. Setup requires the exact
`WEB_ALLOWED_ORIGIN` plus the
one-time `WEB_SETUP_TOKEN` in `X-Setup-Token`. Passwords and raw session tokens
are not stored in plaintext.

An authenticated browser session may read the recording list, metadata,
status, transcript, analysis, and activity endpoints. Browser upload, editing,
reprocessing, and deletion additionally require the exact configured Origin and CSRF token.
Bearer-authenticated machine clients may use the same mutation endpoints
without browser CSRF headers. Upload and the legacy failed-job retry endpoint
remain machine-Bearer-only. The detail UI may stream the private original through
the authenticated same-origin audio endpoint for timestamped sentence playback;
the endpoint supports byte ranges and never exposes a storage path.

Raspberry Pi and other machine clients send:

```http
Authorization: Bearer <API_TOKEN>
```

The liveness and readiness endpoints are intentionally unauthenticated and
must not expose configuration. Put the service behind HTTPS at deployment
ingress; the application does not terminate TLS itself. Never pass the token in
a query string.

## Raspberry Pi upload contract

The current Pi uploader sends `multipart/form-data` to:

```http
POST /api/v1/recordings
```

Required headers:

| Header | Value |
| --- | --- |
| `Authorization` | `Bearer <API_TOKEN>` |
| `Idempotency-Key` | Client-generated recording UUID; must equal `metadata.id`. |
| `X-Device-ID` | Stable recorder ID; must equal `metadata.device_id`. |
| `X-Content-SHA256` | Lowercase SHA-256 of the exact uploaded bytes. |

Required multipart parts:

| Part | Content |
| --- | --- |
| `audio` | WAV, FLAC, M4A/AAC, or Ogg/WebM Opus bytes. |
| `metadata` | JSON object, sent as `application/json`. |
| `checksum` | The same lowercase SHA-256 value as the header and metadata. |

The compatibility metadata object accepts:

```json
{
  "id": "d7fd10c1-e9c8-4ec0-a1ea-1917fa95832a",
  "recording_start_time": "2026-08-10T09:00:00+09:00",
  "recording_end_time": "2026-08-10T09:10:00+09:00",
  "duration_seconds": 600.0,
  "filename": "capture-20260810.flac",
  "file_size": 12345678,
  "checksum_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "created_at": "2026-08-10T09:10:02+09:00",
  "device_id": "pi-recorder-01",
  "audio_format": "flac",
  "sample_rate": 48000,
  "channels": 1,
  "upload_status": "pending",
  "retry_count": 0,
  "extra": {
    "client_version": "example"
  }
}
```

`created_at`, `upload_status`, and `retry_count` are accepted for client
compatibility but never control server timestamps or processing state.
`duration_seconds`, `audio_format`, `sample_rate`, and `channels` are also
non-authoritative client claims: they are retained as client metadata, while
the values measured by `ffprobe` become the recording's authoritative media
properties. The server does not use `filename` to construct a path. Timestamps
must include a UTC offset and the end must be later than the start.

Example upload, after replacing the metadata checksum with the calculated
value:

```bash
AUDIO_FILE=/path/to/capture-20260810.flac
TOKEN='replace-with-api-token'
DEVICE_ID='pi-recorder-01'
RECORDING_ID='d7fd10c1-e9c8-4ec0-a1ea-1917fa95832a'
CHECKSUM=$(shasum -a 256 "$AUDIO_FILE" | awk '{print $1}')

curl --fail-with-body \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: $RECORDING_ID" \
  -H "X-Device-ID: $DEVICE_ID" \
  -H "X-Content-SHA256: $CHECKSUM" \
  -F "audio=@$AUDIO_FILE" \
  -F "metadata=<metadata.json;type=application/json" \
  -F "checksum=$CHECKSUM" \
  http://127.0.0.1:8000/api/v1/recordings
```

For Linux, `sha256sum "$AUDIO_FILE" | awk '{print $1}'` is the common
equivalent. The actual byte count must equal `file_size`; all checksum copies
and the client UUID/device identity must agree. Client media claims do not need
to match the probe and cannot override the server-probed format, duration,
sample rate, or channel count.

A newly accepted upload returns `201 Created`, a `Location` header, and:

```json
{
  "recording_id": "d7fd10c1-e9c8-4ec0-a1ea-1917fa95832a",
  "status": "queued",
  "duplicate": false
}
```

Uploads are idempotent:

- Repeating the same client UUID and recording identity returns the existing
  recording with `200 OK` and `duplicate: true`.
- A different UUID with the same `device_id + sha256` also returns the existing
  recording and does not store a second copy.
- Reusing a UUID for different device, hash, or size data returns
  `409 recording_identity_conflict`.

The API returns a 4xx domain error for invalid authentication, metadata,
checksum, size, type, or duration. A safe error has this shape:

```json
{
  "error": {
    "code": "checksum_mismatch",
    "message": "Uploaded audio checksum does not match."
  }
}
```

Important current client limitation: the Pi uploader retries **every non-2xx
response indefinitely**, including permanent 4xx validation failures. The
server reports correct status codes rather than acknowledging invalid data.
Until the separate client repository classifies permanent failures, operators
must inspect and quarantine repeatedly rejected client items.

## API endpoints

| Method | Path | Behavior |
| --- | --- | --- |
| `GET` | `/health/live` | Process liveness; no authentication. |
| `GET` | `/health/ready` | Database/application readiness; no authentication. |
| `GET` | `/api/v1/auth/setup-status` | Report whether first-admin setup is required and enabled. |
| `POST` | `/api/v1/auth/setup` | Create the web administrator using `X-Setup-Token`. |
| `POST` | `/api/v1/auth/login` | Validate credentials and create session/CSRF cookies. |
| `GET` | `/api/v1/auth/me` | Return the current web administrator and session expiry. |
| `POST` | `/api/v1/auth/logout` | Revoke the current session with CSRF validation. |
| `POST` | `/api/v1/recordings` | Validate, durably store, enqueue, and return immediately. |
| `POST` | `/api/v1/web/recordings` | Session/Origin/CSRF-protected MP3/WAV/M4A browser upload. |
| `GET` | `/api/v1/recordings` | Paginated newest-first list; optional device/status filters. |
| `GET` | `/api/v1/recordings/{id}` | Recording metadata without an absolute filesystem path. |
| `GET` | `/api/v1/recordings/{id}/audio` | Session-protected original audio stream with HTTP byte-range support. |
| `GET` | `/api/v1/recordings/{id}/status` | Current recording and latest job stage/error state. |
| `GET` | `/api/v1/recordings/{id}/activity` | Paginated, privacy-safe processing activity. |
| `GET` | `/api/v1/recordings/{id}/transcript` | Ordered timestamped segments and formatted transcript. |
| `PUT` | `/api/v1/recordings/{id}/transcript` | Replace all segments using an expected revision. |
| `POST` | `/api/v1/recordings/{id}/retry` | Create a new job for a terminal failed recording. |
| `POST` | `/api/v1/recordings/{id}/reprocess` | Re-run a completed/failed recording; browser calls require Origin and CSRF. |
| `DELETE` | `/api/v1/recordings/{id}` | Permanently delete a terminal recording and its private data. |
| `GET` | `/api/v1/recordings/{id}/analysis` | Completed, skipped, or failed optional analysis. |
| `POST` | `/api/v1/recordings/{id}/analysis/reprocess` | Queue analysis only; never reads audio or runs Whisper/pyannote. |
| `PUT` | `/api/v1/recordings/{id}/analysis` | Replace structured analysis using an expected revision. |
| `GET` | `/api/v1/bookmarks` | Saved expressions/highlights for the signed-in administrator; optional `kind` filter. |
| `POST` | `/api/v1/bookmarks` | Save an analysis quote as a snapshot; Origin and CSRF required. |
| `DELETE` | `/api/v1/bookmarks/{id}` | Remove one saved quote; Origin and CSRF required. |

List requests accept `limit` (default 50, maximum 100), `offset`, `device_id`,
and `status`. Transcript or analysis requests made before the result is ready
return a conflict response rather than partial data. A retry returns `202` only
for a terminal failed recording; active or completed recordings cannot acquire
a second active job. Reprocessing accepts completed or failed recordings and
preserves the last successful transcript until the replacement job commits.
Deletion rejects queued or processing recordings with `409`; success returns
`204` and permanently removes the original audio, transcript, analyses, jobs,
and processing activity.

Bookmarks belong to an administrator account rather than to a device, so they
are the one resource that accepts only browser session authentication; the
machine Bearer credential is rejected. Saving a quote stores an independent
snapshot of its Japanese text, Cantonese translation, usage or reason note,
speaker, and timestamp. Analysis items live inside the analysis JSON and have
no stable identity — reprocessing regenerates them, transcript edits renumber
their segments, and deleting a recording removes them — so a snapshot is what
keeps a personal study list intact. Deleting a recording therefore detaches its
bookmarks rather than removing them: `recording_id` becomes null,
`source_deleted_at` is set, and `source_label` preserves the original filename.
Saving the same quote twice is idempotent, keyed on kind plus exact Japanese
text within a recording.

### Japanese reading aids

Transcript, analysis, and bookmark responses carry a `furigana` map alongside
their existing fields: a dictionary keyed by the exact Japanese string, whose
value is the list of runs that make it up, each with a hiragana `reading` when
it covers kanji. The UI sets those readings as `<ruby>` over the kanji. Runs
always reconstruct the original string exactly, and the reading stops at the
okurigana, so `持ち帰り` is read `もちかえ` over `持ち帰` with `り` left plain.

Readings come from a dictionary-based morphological analyser (`janome`), not
from the language model: an LLM invents plausible-looking readings, and a wrong
reading teaches the wrong word. They are computed when a response is built
rather than stored, so existing recordings and saved quotes gain readings with
no reprocessing and no migration. The analyser is pure Python with no model
download and no credentials, so it does not bring AI runtime into the API.

A numeral immediately followed by a counter is deliberately left unannotated.
The dictionary scores the two pieces separately, which loses every irregular
and euphonic reading — `一人` becomes いちにん rather than ひとり, `二十日`
にじゅうにち rather than はつか, `四時` よんじ rather than よじ — and some
pairs cannot be resolved without context at all, since `一日` is いちにち or
ついたち depending on the sentence. UniDic was measured against the same set and
was wrong on 11 of 22 cases where IPADIC was wrong on 14, so a heavier
dictionary does not fix this class either. Showing those compounds bare is the
deliberate trade: no help there, rather than confident misinformation.

Its dictionary costs roughly 100 MB of resident memory and 120 ms once it is
first built. Loading is deferred until the first Japanese string is annotated,
so an API process that never serves one never pays it; after that each string
costs about 0.05 ms and repeats are memoised.

Interactive OpenAPI documentation is available at `/docs` in environments
where it is enabled.

## Web management UI

The React/TypeScript SPA is served by an unprivileged Nginx container. It has
five browser routes:

- `/setup`: one-time administrator creation
- `/login`: administrator sign-in
- `/recordings`: paginated/filterable list and sequential multi-file MP3/WAV/M4A upload
- `/recordings/{id}`: metadata, current stage, safe activity, editable transcript,
  bilingual analysis, separate retranscription/reanalysis, and permanent deletion
- `/bookmarks`: saved natural expressions and highlights, filterable by kind, each
  linking back to its recording while that recording still exists

The interface defaults to Japanese and can be switched to Hong Kong
Traditional Chinese from the login, setup, desktop sidebar, or mobile header.
Only this non-sensitive locale preference is stored in browser local storage;
credentials, sessions, CSRF values, and API tokens are not stored there.

Nginx serves the SPA and same-origin proxies `/api` and `/health` to FastAPI.
The browser therefore needs no API hostname or secret at build or runtime.
As in the companion llm-ocr app, the authenticated layout polls backend health
immediately, then every three minutes while ready or every five seconds after a
failure. It uses `/health/ready`, so the indicator covers both FastAPI and its
PostgreSQL dependency. Set `VITE_SHOW_HEALTH=false` before building the web
image to hide the indicator and disable browser health polling.
Active recordings and analysis-only jobs poll every three seconds. Transcript
and LLM output are rendered as plain text, never injected as HTML. Transcript
and analysis saves use optimistic revisions so two tabs cannot silently
overwrite one another.

Build and test it independently with:

```bash
cd web
npm ci
npm run build
npm test
```

The gateway adds a restrictive Content Security Policy, `Cache-Control:
no-store`, frame denial, referrer suppression, MIME-sniffing protection, and no
microphone/camera/geolocation permissions. It has a read-only root filesystem,
drops Linux capabilities, and receives neither model/audio volumes nor
application secrets.

## Storage layout

The local backend generates every path from validated server state, never from
the client filename:

```text
data/
  recordings/
    YYYY/MM/DD/<recording-uuid>/original.<detected-extension>
  staging/
    deletions/                  # short-lived atomic deletion quarantine
  work/<job-uuid>/<claim-token>/processing.wav
  model-cache/
    huggingface/
    torch/
```

The date is derived from the recording start time. Original bytes are flushed,
atomically published without overwrite into their final directory, and never
overwritten by ffmpeg.

Terminal recording deletion first atomically moves the original into a private
quarantine on the same filesystem. A database failure restores it; a successful
database commit unlinks the quarantine and removes known derived job
workspaces. This local-filesystem transaction boundary must be implemented with
equivalent semantics by any future object-storage backend.
Temporary normalized audio is mono, 16 kHz, signed 16-bit PCM and is only used
for processing. Database rows store relative storage keys; APIs do not expose
host absolute paths.

On POSIX systems, the local backend keeps the storage root and its recording,
staging, and work directories private with mode `0700`; staged, original, and
processing files use mode `0600`. The process account must own the mounted
storage root. Preserve equivalent restrictions on host mounts and backups, and
keep `.env` at `0600`.

`StorageBackend` isolates filesystem behavior so a future deployment can add
S3, MinIO, NAS, or another shared backend. The MVP implements local storage
only. A multi-host API/worker deployment therefore requires the same shared
filesystem mount or a new object-storage backend.

## Processing and recovery

Jobs advance through these stages:

```text
queued → preprocessing → transcribing → diarizing → merging → analyzing
       → completed
       ↘ failed / scheduled retry
```

Workers claim a job in a short database transaction, attach a unique claim
token, and periodically renew a lease. They do not hold a database lock during
ffmpeg or model execution. An expired claim is recovered on worker startup and
during normal polling. A stale worker cannot heartbeat, advance, or commit a
result after losing its claim.

Retryable failures use bounded exponential backoff. Invalid audio and
deterministic decode failures are permanent. Model/token/CUDA configuration
errors prevent a misconfigured worker from claiming jobs. An interrupted retry
starts again from the unchanged original; the MVP does not checkpoint inside an
AI stage.

Handled failures clean up their staging and per-claim work files. A hard stop
such as `SIGKILL`, host loss, or storage failure can still leave stale `.part`
files or work directories because the MVP has no temporary-file janitor. Do not
delete anything merely because it looks old: first prove that no queued,
processing, or retryable job references it, and never remove a preserved
original as temporary data. Automated, job-aware cleanup remains roadmap work.

The default is one processing worker. Raising `PROCESSING_WORKERS` loads one
Whisper and diarization model copy per process and can exhaust RAM or VRAM.
Upload concurrency is independent of this setting.

### Separating analysis from transcription

One queue holds both job kinds, and a claim takes the oldest available job
regardless of kind. A `full` job occupies its worker for the whole
transcription, so an `analysis` job -- a single network call to LM Studio --
can otherwise wait hours behind CPU-bound work it does not depend on.

`WORKER_JOB_KINDS` restricts which kinds a worker claims. A worker that omits
`full` never loads Whisper or pyannote, so an analysis-only worker costs a
process rather than gigabytes of model memory. Compose runs `worker` with
`full` and `analysis-worker` with `analysis`, which keeps analysis responsive
while transcription saturates the CPU. Both services build the same
`wave-archive-worker:latest` tag, so one rebuild updates both and neither can
be left running older code.

Leaving the value empty keeps the original behaviour of one worker claiming
every kind. Run at least one worker that claims `analysis`, or analyses stay
queued forever; a Compose deployment that starts only the `worker` service has
no analysis worker.

## Speech-to-text strategy

The worker converts supported inputs through ffmpeg and sends the normalized
WAV to faster-whisper. It preserves segment start/end, text, detected language,
word timestamps, and available confidence metadata.

`WHISPER_MODEL=small`, CPU, and `int8` are conservative local defaults. They are
not the highest-accuracy production profile. Japanese conversations with
English or Cantonese content should normally leave `WHISPER_LANGUAGE` blank so
the model can detect language. Set `ja` only when the audio is reliably
Japanese. A larger model such as `large-v3` improves accuracy but needs much
more memory and is slow on CPU.

The supplied `worker` Docker target is intentionally CPU-only. Merely changing
`WHISPER_DEVICE=cuda` is not enough to make that image GPU-capable. For NVIDIA
deployment, build a separate worker image with a compatible CUDA/cuDNN runtime,
matching GPU PyTorch and CTranslate2 wheels, and a Compose/device allocation;
then set `WHISPER_DEVICE=cuda` and an appropriate compute type such as
`float16`. Keep the API image CPU-only. Never silently fall back to CPU when a
GPU worker is explicitly requested, because that hides configuration errors and
can make the queue appear hung.

## Speaker diarization

The default provider uses the gated
`pyannote/speaker-diarization-community-1` model. Before enabling it:

1. Sign in to Hugging Face and accept the model's access conditions.
2. Create a least-privilege read token.
3. Put the token in the gitignored `.env` as `HUGGINGFACE_TOKEN`.
4. Keep `HF_HOME` and `TORCH_HOME` on persistent cache storage.

`PYANNOTE_METRICS_ENABLED=0` disables pyannote telemetry in the supplied
environment. Do not put the token in Docker build arguments, image layers,
commands, logs, or source control.

When enabled, a diarization failure is visible and does not silently downgrade
quality. Set `DIARIZATION_ENABLED=false` explicitly for transcription-only
operation; those segments use an unknown speaker label. The MVP assigns labels
such as `SPEAKER_00` and never attempts to identify a real person.

### Timestamp merge

Whisper and pyannote operate on the same normalized WAV but produce different
boundaries. The merge module remains independent from both providers and uses a
deterministic heuristic:

1. For each Whisper word, choose the speaker turn with the greatest actual
   overlap.
2. If there is no overlap, use a turn containing the word midpoint, then the
   nearest turn within the documented short tolerance; otherwise use
   `SPEAKER_UNKNOWN`.
3. Resolve ties by midpoint match, earlier turn, then speaker label.
4. Keep adjacent words for the same speaker together within a Whisper segment.
5. If word timestamps are unavailable, fall back to the greatest overlap for
   the whole Whisper segment.

When people speak simultaneously, the MVP stores the exclusive primary speaker
and marks the segment as overlapping. It does not duplicate text across all
active speakers. The heuristic and overlap behavior are unit-tested and can be
replaced later without changing API routes or provider implementations.

## Optional analysis

Analysis consumes only merged transcript segments through an `AnalysisProvider`
boundary. It is disabled by default and records `skipped`; transcription,
diarization, and transcript retrieval still complete normally. Enabling it
uses the official `lmstudio` Python SDK from the worker only. The dependency
starts at `1.6.0b1`, the first official release with API-token support:

```env
LLM_ENABLED=true
LLM_PROVIDER=lmstudio
LM_STUDIO_HOST=192.168.51.10:1234
LM_STUDIO_API_KEY=replace-with-lm-studio-token
LM_STUDIO_TIMEOUT_SECONDS=600
LM_STUDIO_CHUNK_CHARS=12000
LM_STUDIO_MAX_TOKENS=4096
```

On the 5090 computer, enable LM Studio's server and **Serve on Local Network**,
enable API-token authentication, and create a least-privilege token that can
list loaded models and run inference. Firewall the port to the audio server's
trusted LAN address. Docker must use the 5090 computer's LAN address, not
`127.0.0.1`; test routing from the worker container. Load exactly one LLM in
the LM Studio GUI. The worker lists already loaded LLMs and uses the only
handle without providing a model name, downloading a model, or loading one.
Zero or multiple loaded LLMs produce a safe visible failure.

See LM Studio's official documentation for [server settings](https://lmstudio.ai/docs/developer/core/server/settings),
[authentication](https://lmstudio.ai/docs/developer/core/authentication),
[listing loaded models](https://lmstudio.ai/docs/python/manage-models/list-loaded),
and [structured responses](https://lmstudio.ai/docs/python/llm-prediction/structured-response).

Long transcripts are split by segments into bounded chunks and combined with
hierarchical structured map/reduce. Every response layer is Pydantic-validated;
new results contain a concise bilingual description plus a more detailed
bilingual summary. Japanese quote cards are retained only when their text occurs
in the referenced transcript segment, and the server—not the model—adds start/end
timestamps and speaker labels. Logs never contain
prompts, transcripts, responses, or token content.

The original audio and normalized WAV always remain on this audio server. Only
transcript text crosses the trusted LAN to LM Studio. A 5090 accelerates this
LLM analysis; it does not accelerate faster-whisper unless the worker itself is
separately rebuilt/configured for that GPU. The analysis-only endpoint reads
the transcript from PostgreSQL and never touches audio, Whisper, or pyannote.
Sentence playback streams audio only from this server to the authenticated
browser; it never sends audio to LM Studio.

## Database migrations

Alembic migrations live in `migrations/`. Apply all committed migrations before
starting a new API or worker version:

```bash
.venv/bin/alembic upgrade head
```

Compose does this with its one-shot `migrate` service. Production rollout
should run that step once under deployment control rather than letting every
replica race migrations. Back up PostgreSQL and the recording store together
before destructive schema or retention changes.

Revision `0003_processing_activity` backfills lifecycle events from existing
jobs and therefore requires an online database connection. Generating an
offline SQL script across that revision intentionally fails rather than
producing an incomplete activity history.

Compose uses one PostgreSQL login for migrations and runtime access to keep the
MVP easy to operate. A hardened production deployment should split the schema
owner/migration role from a restricted API/worker runtime role and grant only
the tables and operations each runtime needs. The Compose migration service is
already isolated from API, AI-provider, and recording-storage credentials.

## Tests and checks

Ordinary tests use generated temporary audio and fake AI providers. They do not
require PostgreSQL, ffmpeg, GPU hardware, downloaded models, a Hugging Face
token, or an LLM API key:

```bash
.venv/bin/python -m pytest -m 'not integration'
.venv/bin/ruff check .
.venv/bin/mypy src
.venv/bin/python -m compileall -q src tests
```

Runtime-heavy checks are marked `integration`:

```bash
.venv/bin/python -m pytest -m integration
```

Only run real AI integration tests when their model cache, access token, system
dependencies, and adequate resources are intentionally available. PostgreSQL
integration tests should use an isolated disposable database.

## Security and privacy

This service stores workplace conversations. Treat recordings, transcript
segments, metadata, and analyses as private data.

- Terminate HTTPS at a trusted reverse proxy or load balancer.
- Set `WEB_COOKIE_SECURE=true` and `WEB_ALLOWED_ORIGIN` to the exact public
  `https://` origin before making the UI available beyond localhost.
- Enforce a reverse-proxy request-body cap matching or slightly exceeding the
  application's derived multipart cap; do not rely on ASGI validation as an
  excuse to buffer unlimited bodies at the ingress.
- Rotate a compromised API token and database credential immediately.
- Do not expose PostgreSQL publicly in production; the Compose port is for
  local development.
- Keep `.env`, original/derived audio, database files, logs, and model caches
  out of Git and backups with unsuitable access controls.
- Restrict permissions on storage and database backups.
- Never log Authorization headers, tokens, audio, full transcripts, raw client
  metadata, or unsanitized ffmpeg/provider errors.
- Preserve the original filename only as metadata; never interpret it as a
  filesystem path.
- Keep upload limits, checksum validation, ffprobe validation, parameterized
  SQL, and bounded worker concurrency enabled.
- Do not implement cleanup that can remove an original for a queued,
  processing, or retryable failed job.

The MVP provides one web administrator and a separate shared machine API token,
not multi-user ownership or per-recording authorization. Encryption at rest,
additional roles, a user-access audit log, formal retention enforcement, and
key management are deployment roadmap work.

## Troubleshooting

### Compose reports a missing variable

Set non-empty `API_TOKEN` and `POSTGRES_PASSWORD` in `.env`. Use hexadecimal or
another URL-safe password because the internal database URL is assembled by
Compose.

### API is not ready

Check database and migration state:

```bash
docker compose ps
docker compose logs postgres migrate api
```

Do not start API replicas against an unapplied schema.

If PostgreSQL reports `password authentication failed` while its log says that
the database directory already exists, the named volume was initialized with a
different password. Changing `POSTGRES_PASSWORD` in `.env` does not rotate an
existing database role. Preserve the data by restoring the original password
or rotating the role through an authorized database session. Recreate only the
PostgreSQL volume when its contents are explicitly confirmed disposable; never
use `docker compose down -v` as a generic migration fix because that also
removes the recordings and model-cache volumes.

### Web setup or login is rejected

Confirm `WEB_ALLOWED_ORIGIN` exactly matches the URL shown in the browser,
including scheme and port. `127.0.0.1` and `localhost` are different origins.
Setup also requires a non-empty `WEB_SETUP_TOKEN` of at least 32 characters.
After an administrator exists, an empty setup token is the safer normal state.
If the browser uses HTTPS, set `WEB_COOKIE_SECURE=true`; do not set it for plain
HTTP local development because browsers will not return Secure cookies over
HTTP.

### Using the web UI on a private LAN

Do not expose plain HTTP authentication to the LAN. Put the web service behind
a trusted HTTPS reverse proxy, publish only that proxy, configure a valid
certificate, set `WEB_ALLOWED_ORIGIN` to its exact HTTPS origin, and enable
`WEB_COOKIE_SECURE`. Keep ports 8000 and 5432 on loopback or remove their host
publishing. Firewall access to trusted devices and use a VPN where possible;
`WEB_BIND_HOST=0.0.0.0` alone does not provide transport security.

### Upload is rejected

Confirm the Bearer token, UUID, device ID, byte count, and all three SHA-256
values agree. Ensure timestamps include offsets. A filename extension or client
MIME type cannot make invalid bytes acceptable; `ffprobe` must recognize a
supported audio stream.

### A job remains queued

Inspect safe worker logs and its database connectivity. If diarization is
enabled, confirm the model agreement was accepted and the Hugging Face token can
read it. A deliberately misconfigured AI worker does not consume job attempts.
After an unclean worker stop, allow the lease to expire before recovery.

### Model download repeats

Confirm the worker mounts the `model-cache` volume and that `HF_HOME` and
`TORCH_HOME` point below `/models`. Confirm the non-root container user can write
there.

### CPU processing is too slow or runs out of memory

Use a smaller Whisper model, keep `PROCESSING_WORKERS=1`, and avoid concurrent
model copies. Move only the worker to a correctly built GPU host when needed;
the API and PostgreSQL do not require GPU access.

## MVP limitations and roadmap

Current deliberate limitations:

- local filesystem storage only
- one web administrator plus one shared machine Bearer token; no user ownership model
- login rate limiting is process-local and assumes one API instance
- no cleanup job yet for expired or revoked browser sessions
- PostgreSQL at-least-once queue and restart-from-original retries
- CPU-only supplied worker image
- anonymous, exclusive speaker labels rather than real-person identification
- deterministic overlap heuristic rather than multi-speaker text attribution
- disabled analysis provider and no paid LLM requirement
- retention settings without automatic deletion
- no job-aware janitor for staging/work artifacts left by hard process or host
  crashes
- one PostgreSQL owner/runtime role in the supplied Compose stack
- management UI has reprocessing and permanent deletion, but no upload,
  playback, search, transcript editing, or analysis controls
- no full-text search, embeddings, vector database, or RAG
- the separate Pi client currently retries permanent non-2xx failures

Likely next steps, after ingestion and transcript durability are proven:

1. Add a shared object-storage backend for multi-host deployments.
2. Publish and verify an NVIDIA worker image/profile.
3. Add multi-user roles, per-recording authorization, access auditing,
   distributed login throttling, session cleanup, encryption at rest, and
   tested retention cleanup.
4. Improve overlap attribution and add optional speaker identity mappings with
   explicit consent.
5. Add a real optional analysis provider and versioned result schemas.
6. Add PostgreSQL full-text search before evaluating embeddings and RAG.
7. Update the separate Pi uploader to stop retrying permanent 4xx responses.
