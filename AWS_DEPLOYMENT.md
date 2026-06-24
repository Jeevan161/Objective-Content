# AWS Deployment — Step by Step (Console)

Deploy the app as **one Docker container on EC2** talking to **RDS PostgreSQL** (with
pgvector). Region used throughout: **ap-south-1 (Mumbai)** — adjust if you use another.

To keep it simple and avoid networking pitfalls, this guide uses the **default VPC**
(its subnets already span multiple AZs and are public, which is all we need). RDS is
kept private via *Public access = No* + a security group locked to the EC2 instance.

```
Browser ──HTTP:80──▶ EC2 (Docker: objective-content) ──5432──▶ RDS PostgreSQL (pgvector)
```

---

## 0. (If starting over) Tear down old resources — in THIS order
Dependencies block deletion, so go in order:

1. **EC2** → Instances → select old instance → **Instance state → Terminate**.
2. **RDS** → Databases → select old DB → **Actions → Delete** (uncheck "create final
   snapshot" if you don't need it). Takes a few minutes.
3. **EC2 → Elastic IPs** → select any old EIP → **Actions → Release** (only after it's
   disassociated from a terminated instance).
4. **RDS → Subnet groups** → delete old `oc-db-subnets` (after the DB is gone).
5. **EC2 → Security Groups** → delete old `oc-ec2-sg` / `oc-rds-sg` (after EC2 + RDS gone).
6. *(Only if you made a custom VPC)* VPC → delete it. **Keep the default VPC.**
   - No default VPC? VPC console → **Actions → Create default VPC**.

---

## 1. Create two security groups (EC2 → Security Groups → Create security group)
Both in the **default VPC**.

**`oc-ec2-sg`** (the app server)
- Inbound: **SSH 22** → Source **My IP**
- Inbound: **HTTP 80** → Source **Anywhere-IPv4 (0.0.0.0/0)**

**`oc-rds-sg`** (the database)
- Inbound: **PostgreSQL 5432** → Source **Custom** → type `oc-ec2-sg` and select it
  (so only the app can reach the DB).
  - If it doesn't appear, create `oc-ec2-sg` first, then come back. Fallback: use the
    default VPC's CIDR (e.g. `172.31.0.0/16`) as the source.

---

## 2. Create the RDS database

### 2a. DB subnet group (RDS → Subnet groups → Create DB subnet group)
- Name: `oc-db-subnets`; VPC: **default VPC**
- Availability Zones: pick **two** (`ap-south-1a` and `ap-south-1b`)
- Subnets: add **one subnet per AZ** (the default subnets listed for each) → **Create**.
  (Two AZs is mandatory or RDS rejects it.)

### 2b. Create database (RDS → Databases → Create database)
- Method: **Standard create**
- Engine: **PostgreSQL**, Version **16.x** (needed for pgvector ≥ 0.5)
- Template: **Dev/Test** (or Free tier)
- **Settings:** DB identifier `objective-content-db`; Master username `postgres`;
  Master password → **save it**
- Instance: **db.t3.micro**; Storage: gp3 20 GiB
- **Connectivity:**
  - Compute resource: **Don't connect to an EC2 compute resource**
  - VPC: **default VPC**; DB subnet group: **`oc-db-subnets`**
  - Public access: **No**
  - VPC security group → **Choose existing** → **`oc-rds-sg`** (remove `default`)
  - Port: 5432
- **Additional configuration → Initial database name: `objective_content`** ← REQUIRED
  (the app connects to this DB; without it migrations fail)
- Backups: 7 days. **Create database.**

Wait for status **Available**, then open it → **Connectivity & security** → copy the
**Endpoint** (e.g. `objective-content-db.xxxx.ap-south-1.rds.amazonaws.com`).

> pgvector: nothing to do — the app's first migration runs `CREATE EXTENSION IF NOT
> EXISTS vector` as the `postgres` master user (which has `rds_superuser`).

---

## 3. Launch the EC2 instance (EC2 → Instances → Launch instances)
- **Name:** `objective-content-app`
- **AMI:** Amazon Linux 2023 (x86_64)
- **Instance type:** **t3.medium** (4 GB RAM; the image bundles LangChain/numpy)
- **Key pair:** Create new → name `oc-key` → **download `oc-key.pem`** and keep it safe
- **Network settings → Edit:**
  - VPC: **default VPC**
  - Subnet: **No preference** (any default subnet — they're all public)
  - **Auto-assign public IP: Enable**
  - Firewall → **Select existing security group → `oc-ec2-sg`**
- **Storage:** root volume **30 GiB gp3** (8 GiB default is too small to build the image)
- **Launch instance.** Wait for **Running** + **Status checks 2/2**.

---

## 4. Give it a stable IP (Elastic IP) — recommended
Avoids the IP changing on every stop/start.
- EC2 → **Elastic IPs → Allocate Elastic IP address → Allocate**
- Select it → **Actions → Associate** → choose your `objective-content-app` instance → Associate.
- Use **this Elastic IP** everywhere below as `<EC2-IP>`.

---

## 5. Connect via SSH (from your laptop, where `oc-key.pem` is)
```bash
chmod 400 oc-key.pem
ssh -i oc-key.pem ec2-user@<EC2-IP>
```
Troubleshooting:
- **`Connection refused`** → sshd not up yet; wait ~1–2 min after "running" and retry.
- **timeout / hangs** → SG SSH rule isn't your current IP → EC2 → Security Groups →
  `oc-ec2-sg` → SSH 22 → set Source = **My IP** again.
- **`Permission denied (publickey)`** → wrong key file or wrong user (must be `ec2-user`).
- Use the **IP**, not a partial DNS name.

---

## 6. Install Docker + Compose (on EC2)
```bash
sudo dnf -y install docker git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user        # then: exit, SSH back in (so docker works w/o sudo)

sudo mkdir -p /usr/libexec/docker/cli-plugins
sudo curl -fsSL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
  -o /usr/libexec/docker/cli-plugins/docker-compose
sudo chmod +x /usr/libexec/docker/cli-plugins/docker-compose

docker --version && docker compose version
```

---

## 7. Get the code (on EC2)
```bash
git clone https://github.com/Jeevan161/Objective-Content.git
# Username: your GitHub user   |   Password: your fine-grained PAT (Contents: Read)
cd Objective-Content
git checkout main        # deployment branch (skip if main is the repo's default — clone lands on it)
git config --global credential.helper store   # optional: remember the PAT for next pull
```

---

## 8. Configure `backend/.env` (on EC2)
```bash
cp backend/.env.example backend/.env
nano backend/.env
```
Fill these in. **Required for the app to run & generate MCQs:**

| Key | Set to |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg://postgres:YOUR_DB_PASSWORD@<RDS-ENDPOINT>:5432/objective_content?sslmode=require` |
| `JWT_SECRET` | a random string — generate: `python3 -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `LLM_SECRET_KEY` | another random string (encrypts stored LLM keys at rest) |
| `OPENROUTER_API_KEY` | your OpenRouter key (RAG embeddings + judge + generation) |

**Required for portal sync / course fetch:**
`PORTAL_PROD_BASE_URL`, `PORTAL_PROD_PASSWORD`, `PORTAL_BETA_BASE_URL`,
`PORTAL_BETA_PASSWORD`, `PORTAL_LEARNING_COURSE_URL`, `PORTAL_LEARNING_COURSE_BETA_URL`.

**Required for "Generate ZIP" / beta load (S3):**
`BETA_ADMIN_BASE_URL`, `BETA_ADMIN_USERNAME`, `BETA_ADMIN_PASSWORD`.

**Optional — "Prepare & Load" (Google Sheets):** put the service-account JSON on the box,
set `GOOGLE_SA_CREDENTIALS_FILE=/app/google-sa.json`, and uncomment the `volumes:` block
in `docker-compose.prod.yml`. Skip if you don't use that feature.

> `DATABASE_URL` must point at RDS — it's the one value that makes the prod compose use
> RDS instead of a local DB. `?sslmode=require` because RDS PG 16 enforces TLS.

---

## 9. Build & run (on EC2)
```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs -f        # watch boot + migrations; Ctrl-C to stop following
```
On start it runs `alembic upgrade head` (creates the pgvector extension + all tables),
then serves on host **:80**.

**Verify:**
```bash
curl -s localhost/health                                 # {"status":"ok"}
curl -so /dev/null -w '%{http_code}\n' localhost/        # 200
```
Open **`http://<EC2-IP>/`** in your browser.

**Make your first user an admin** (registration creates an inactive user). Easiest:
register in the UI, then from a machine that can reach RDS run:
```sql
UPDATE users SET role='admin', is_active=true WHERE email='you@example.com';
```
(or temporarily allow your IP on `oc-rds-sg` to connect with `psql`).

---

## 10. Updating later
```bash
cd ~/Objective-Content
git pull
docker compose -f docker-compose.prod.yml up -d --build   # rebuild + restart; migrations re-run
```

---

## 11. HTTPS (optional, for public use)
The app is plain HTTP on :80. For TLS, front it with **ALB + ACM cert** (target the
instance on 80), or run **Caddy/nginx + certbot** on the box proxying to `127.0.0.1:8000`
(set `APP_HOST_PORT=8000` so the app isn't directly on :80). Since the SPA + API are
same-origin, one reverse proxy to the app port is all you need.

---

## Troubleshooting cheatsheet

| Symptom | Likely cause / fix |
|---|---|
| SSH `Connection refused` | sshd still booting — wait 1–2 min; or instance unhealthy → reboot/recreate |
| SSH times out | `oc-ec2-sg` SSH 22 not allowing your current IP → reset Source = My IP |
| SSH `Permission denied (publickey)` | wrong `.pem` or user (use `ec2-user`) |
| App starts but DB errors in logs | `DATABASE_URL` wrong, or `oc-rds-sg` doesn't allow `oc-ec2-sg` on 5432, or RDS not in same VPC |
| `database "objective_content" does not exist` | you skipped the **Initial database name** on RDS → create it: `CREATE DATABASE objective_content;` |
| `CREATE EXTENSION vector` permission error | connect as the RDS **master** user (`postgres`), not a limited role |
| Browser EC2 Instance Connect fails | it comes from an AWS IP range — either SSH from your terminal, or add SSH 22 from `0.0.0.0/0` / the `ec2-instance-connect` prefix list |
| Can't reach `http://<IP>/` | `oc-ec2-sg` missing HTTP 80 inbound; or container not up (`docker compose -f docker-compose.prod.yml ps`) |
