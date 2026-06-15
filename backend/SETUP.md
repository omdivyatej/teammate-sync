# teammate-sync cloud backend — setup

Stateless FastAPI service. Handles GitHub OAuth, returns the user's verified
identity + GitHub-org-based teammate list, and (Phase 4b) mints scoped
AWS STS credentials so each engineer's daemon can only write to their own
S3 prefix.

## Phase 4a — what's done

- ✅ FastAPI skeleton (`main.py`) with 7 endpoints
- ✅ GitHub OAuth login + callback
- ✅ `/v1/me` and `/v1/teammates` — stateless, validate via GitHub on every call
- ✅ `/v1/sts/{write,read}` — stub endpoints (real STS in 4b)
- ✅ Dockerfile + fly.toml for cheap deployment
- ✅ Local smoke tests pass

## Phase 4a — what YOU need to do (manual steps)

These can't be automated — they require web UI clicks on GitHub + Fly.io.
~15 minutes total.

### 1. Register a GitHub OAuth app

1. Go to **https://github.com/settings/developers**
   (Or: GitHub → Settings → Developer settings → OAuth Apps)
2. Click **"New OAuth App"**
3. Fill in:
   - **Application name:** `teammate-sync` (anything)
   - **Homepage URL:** `https://teammate-sync-backend.fly.dev` (placeholder — update after deploy)
   - **Authorization callback URL:** `https://teammate-sync-backend.fly.dev/auth/github/callback` (same — update after deploy)
4. Click **"Register application"**
5. On the next page:
   - Copy the **Client ID** — you'll need it
   - Click **"Generate a new client secret"** → copy that too

Save those two values somewhere safe for the next step.

### 2. Sign up for Fly.io + install flyctl

1. Sign up at **https://fly.io/app/sign-up** (free, no credit card needed for the smallest tier)
2. Install the CLI:
   ```bash
   brew install flyctl
   ```
3. Log in:
   ```bash
   fly auth login
   ```

### 3. Deploy the backend

From the project root:

```bash
cd backend/
fly launch --no-deploy --copy-config
```

When prompted:
- App name: accept `teammate-sync-backend` (or pick another — must be globally unique)
- Region: keep `sin` (Singapore, matches our S3 bucket)
- Postgres: **no**
- Redis: **no**

Then set the secrets (replace placeholders with your real values from step 1):

```bash
fly secrets set GITHUB_CLIENT_ID=your-client-id
fly secrets set GITHUB_CLIENT_SECRET=your-client-secret
fly secrets set OAUTH_REDIRECT_URI=https://<your-app-name>.fly.dev/auth/github/callback
```

Update your GitHub OAuth app's callback URL to match what you just set (back at github.com/settings/developers — edit the app you created in step 1).

Deploy:

```bash
fly deploy
```

After deploy completes, hit the health check:

```bash
curl https://<your-app-name>.fly.dev/health
# → {"status":"ok","version":"0.1.0"}
```

### 4. Verify the OAuth flow end-to-end

In your browser, visit:

```
https://<your-app-name>.fly.dev/auth/github/login
```

You should be redirected to GitHub, asked to authorize the app, then redirected back to your backend. The backend will display a copy-pasteable token.

To verify the token works:

```bash
curl -H "Authorization: Bearer <paste-token>" https://<your-app-name>.fly.dev/v1/me
# → {"github_handle":"omdivyatej","email":"om@turgon.ai","name":"Om"}
```

And the teammate listing:

```bash
curl -H "Authorization: Bearer <paste-token>" "https://<your-app-name>.fly.dev/v1/teammates?org=<your-github-org>"
# → {"org":"...","teammates":[{"github_handle":"saketh"}, {"github_handle":"baljeet"}, ...]}
```

If both of those work, Phase 4a is done.

## Phase 4b — next

- Replace STS stubs with real boto3 `AssumeRole` (~1 hour of code)
- Set up the IAM role: a single role the backend can assume, with an inline
  policy that the backend overrides per-request to scope to
  `arn:aws:s3:::teammate-sync-bucket/<org>/<user>/*`
- Document the IAM setup in `backend/AWS-SETUP.md`

## Phase 4c — after that

- New CLI surface: `teammate-sync init`, `teammate-sync whoami`, `teammate-sync teammates`
- CLI opens browser, runs local HTTP listener on `localhost:PORT/callback`, captures token
- Saves token to `~/.teammate-sync/auth.json`
- Daemon + MCP read auth.json, call backend for STS creds before each S3 operation
- Drop the hard-coded AWS keys from the MCP env vars and `cloud/bootstrap-vm.sh`

## Local development

```bash
cd backend/
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Set GitHub OAuth creds (use a "dev" OAuth app pointed at http://localhost:8000)
export GITHUB_CLIENT_ID=...
export GITHUB_CLIENT_SECRET=...
export OAUTH_REDIRECT_URI=http://localhost:8000/auth/github/callback

.venv/bin/uvicorn main:app --reload
```

Open http://localhost:8000/docs for the auto-generated OpenAPI explorer.
