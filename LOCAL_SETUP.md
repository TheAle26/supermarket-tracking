# Local Setup

## 1. Activate the virtual environment

```powershell
cd "C:\Proyectos\supermarket tracking"
.\venv\Scripts\activate
```

## 2. Create the PostgreSQL database

Open a terminal where `psql` is available and connect as the PostgreSQL admin user:

```powershell
psql -U postgres
```

Then run:

```sql
CREATE USER supermarket_app WITH PASSWORD 'supermarket_app_password';
CREATE DATABASE price_compare OWNER supermarket_app;
GRANT ALL PRIVILEGES ON DATABASE price_compare TO supermarket_app;
```

Exit `psql`:

```sql
\q
```

## 3. Connect Django to PostgreSQL

In the same PowerShell terminal where you run Django:

```powershell
$env:DB_NAME="price_compare"
$env:DB_USER="supermarket_app"
$env:DB_PASSWORD="supermarket_app_password"
$env:DB_HOST="localhost"
$env:DB_PORT="5432"
```

You can test the connection directly:

```powershell
psql -h localhost -U supermarket_app -d price_compare
```

## 4. Create tables and seed demo data

```powershell
python manage.py migrate
python manage.py pgpartition --yes
python manage.py seed_demo
python manage.py runserver
```

Open http://127.0.0.1:8000/.

## 5. Optional background services

Redis is required for Celery jobs:

```powershell
celery -A config.celery worker -l info
celery -A config.celery beat -l info
```
