# Deploying to AWS — EC2 (app) + RDS (Postgres)

The whole app ships as **one Docker image** (React SPA served by the FastAPI backend
on port 8000 — SPA, `/api`, and the WebSocket share one origin). In production it runs
on an **EC2** instance and talks to a managed **RDS PostgreSQL** database with the
**pgvector** extension. The bundled Postgres in `docker-compose.yml` is for local dev
only — production uses `docker-compose.prod.yml` (app container, no DB).

```
Browser ──HTTP:80──▶ EC2 (Docker: objective-content) ──5432──▶ RDS PostgreSQL (pgvector)
```

> S3 uploads (beta content loading) authenticate with short-lived creds the app scrapes
> from the beta admin login (`BETA_ADMIN_USERNAME`/`PASSWORD`), **not** the EC2 instance
> role — so no IAM role is required for the app to function.

---

## 1. Create the RDS PostgreSQL database

1. **Engine:** PostgreSQL **16.x** (or ≥ 15.4) — needed for pgvector ≥ 0.5 (the schema uses
   an HNSW vector index).
2. **Instance:** `db.t3.micro`/`small` is fine to start; 20 GB gp3 storage.
3. **Settings:** master username (e.g. `postgres`), a strong master password, initial
   database name **`objective_content`**.
4. **Network:** put RDS in the **same VPC** as the EC2 instance. **Do not** make it
   publicly accessible.
5. **Security group (RDS):** inbound **PostgreSQL 5432** allowed **from the EC2 instance's
   security group** (source = the EC2 SG, not `0.0.0.0/0`).
6. **pgvector:** the app's first migration runs `CREATE EXTENSION IF NOT EXISTS vector`.
   The RDS **master user** has `rds_superuser` and can create it — so connect the app as
   the master user (or a role granted `rds_superuser`). If you prefer, pre-create it once:
   ```sql
   -- connected to the objective_content DB as the master user:
   CREATE EXTENSION IF NOT EXISTS vector;
   ```

Note the **endpoint** (e.g. `mydb.abc123.ap-south-1.rds.amazonaws.com`).

---

## 2. Launch the EC2 instance

1. **AMI:** Amazon Linux 2023 (or Ubuntu 22.04). **Type:** `t3.medium` recommended
   (the image bundles LangChain/numpy; `t3.small` is the practical minimum).
2. **Same VPC** as RDS so it can reach port 5432.
3. **Security group (EC2):** inbound **HTTP 80** from `0.0.0.0/0` (or just your ALB), and
   **SSH 22** from your IP only.
4. **Install Docker** (Amazon Linux 2023):
   ```bash
   sudo dnf -y install docker git
   sudo systemctl enable --now docker
   sudo usermod -aG docker ec2-user      # log out/in so `docker` works without sudo
   # Compose v2 plugin:
   sudo mkdir -p /usr/libexec/docker/cli-plugins
   sudo curl -fsSL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
     -o /usr/libexec/docker/cli-plugins/docker-compose
   sudo chmod +x /usr/libexec/docker/cli-plugins/docker-compose
   docker compose version
   ```

---

## 3. Get the code and configure secrets

```bash
git clone <your-repo-url> objective-content && cd objective-content
cp backend/.env.example backend/.env      # then edit with real values
```

Edit **`backend/.env`** — fill in the portal creds, beta-admin creds, LLM keys, JWT/Fernet
secret, etc., and **set `DATABASE_URL` to your RDS endpoint** (master user, `sslmode=require`):

```dotenv
DATABASE_URL=postgresql+psycopg://postgres:YOUR_DB_PASSWORD@mydb.abc123.ap-south-1.rds.amazonaws.com:5432/objective_content?sslmode=require
```

- **(Optional) Google Sheets "Prepare & Load":** put the service-account JSON on the host
  (e.g. `backend/secrets/google-sa.json`), set `GOOGLE_SA_CREDENTIALS_FILE=/app/google-sa.json`
  in `backend/.env`, and uncomment the `volumes:` block in `docker-compose.prod.yml`.
  Skip entirely if you don't use that feature.

`backend/.env` is gitignored and never baked into the image — it's read at runtime.

---

## 4. Build and run

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs -f          # watch boot + migrations
```

On start the container runs `alembic upgrade head` (creating the pgvector extension and
all tables) and then serves on port 8000, published to host **:80**.

**Verify:**
```bash
curl -s localhost/health           # {"status":"ok"}
curl -so /dev/null -w '%{http_code}\n' localhost/   # 200 (the SPA)
```
Then open `http://<EC2-PUBLIC-IP>/`. The first registered user is inactive until an admin
approves them — promote your first account to admin directly in the DB if needed:
```sql
UPDATE users SET role='admin', is_active=true WHERE email='you@example.com';
```

---

## 5. Updating / redeploying

```bash
cd objective-content
git pull
docker compose -f docker-compose.prod.yml up -d --build    # rebuild + restart; migrations re-run
```

---

## 6. HTTPS (recommended for anything public)

The app is plain HTTP on :80. For TLS, front it with one of:
- **AWS ALB + ACM certificate** → target group to the instance on port 80 (set the EC2 SG
  to accept 80 from the ALB only). Simplest on AWS.
- **Caddy / nginx + certbot** on the same box reverse-proxying to `127.0.0.1:8000` (set
  `APP_HOST_PORT=8000` so the app isn't on :80 directly).

Because the SPA and API are same-origin, a single reverse proxy to the app port is all
that's needed — no separate frontend/back end routing.

---

## 7. Production hardening notes

- **Secrets:** for a real deployment, prefer AWS **Secrets Manager / SSM Parameter Store**
  over a plaintext `backend/.env` (inject values into the environment at launch).
- **RDS:** keep it private (no public access); enable automated backups; rotate the master
  password; consider a least-privilege app role granted `rds_superuser` only if you let the
  app create the extension (otherwise pre-create it and use a normal role).
- **DB migrations on multiple instances:** if you ever run more than one app container,
  run `alembic upgrade head` as a one-off before scaling out rather than concurrently.
- **Image size** ≈ 600 MB; build on the instance or push to **ECR** and `docker pull` on
  deploy if you prefer not to build on EC2.
