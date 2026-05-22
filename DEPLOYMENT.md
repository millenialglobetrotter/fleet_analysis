# Fleet Analysis Deployment

This application can now be deployed either as plain Python code or as a Docker container.

## Before you deploy

The current local config file contains real credentials. Do not deploy with that file baked into an image or committed to source control. Use environment variables instead.

If `config.json` is already tracked in git, untrack it before pushing:

```powershell
git rm --cached config.json
```

## Required configuration

Set these environment variables in your host:

```text
HOST=0.0.0.0
PORT=8000
FLEET_MAX_WORKERS=40
FLEET_AUTH_BASE_URL=
FLEET_AUTH_ENDPOINT=
FLEET_AUTH_CLIENT_ID=
FLEET_AUTH_CLIENT_SECRET=
FLEET_VEHICLE_REGISTRY_BASE_URL=
FLEET_VEHICLE_REGISTRY_ENDPOINT=
FLEET_PREDICTION_SERVICE_BASE_URL=
FLEET_PREDICTION_SERVICE_URL_TEMPLATE=
FLEET_DECRYPTION_SERVICE_BASE_URL=
FLEET_DECRYPTION_SERVICE_ENDPOINT=
FLEET_DECRYPTION_SERVICE_CLIENT_ID=
FLEET_DECRYPTION_SERVICE_CLIENT_SECRET=
FLEET_DB_HOST=
FLEET_DB_USER=
FLEET_DB_PASSWORD=
FLEET_DB_NAME=
```

Use [.env.example](.env.example) or [config.example.json](config.example.json) as the template.

## Option 1: Deploy as Python code

This works on hosts like Azure App Service, Render, Railway, or any Linux VM.

Startup command:

```text
python fleet_analysis.py
```

Install command:

```text
pip install -r requirements.deploy.txt
```

Health check path:

```text
/health
```

## Option 2: Deploy as Docker container

Build locally:

```powershell
docker build -t fleet-analysis .
```

Run locally with environment variables:

```powershell
docker run --env-file .env -p 8000:8000 fleet-analysis
```

The container does not copy `config.json`, so it is safe to use for hosted deployment as long as you provide the environment variables.

## Railway notes

This repo is already compatible with Railway because it includes a [Dockerfile](Dockerfile) that starts the app with `python fleet_analysis.py`.

Railway pricing changes over time, so verify whether a free trial or free usage allowance is available on your account before you rely on it for production.

### Deploy from GitHub

1. Push this project to a GitHub repository.
2. In Railway, create a new project.
3. Choose `Deploy from GitHub repo`.
4. Select your repository.
5. Railway should detect the [Dockerfile](Dockerfile) automatically and build from it.
6. In the Railway service settings, add all variables from [.env.example](.env.example).
7. Keep `PORT` set to `8000` unless Railway injects its own value. The app already honors the platform `PORT` variable.
8. Set the health check path to `/health`.
9. After the deploy finishes, open the generated Railway domain and verify the app loads.

### Deploy without GitHub using the Railway CLI

1. Install the Railway CLI.
2. Run `railway login`.
3. From the project folder, run `railway init`.
4. Run `railway up`.
5. Add the required environment variables in the Railway dashboard or with the CLI.

### Required Railway variables

Copy these from [.env.example](.env.example):

```text
FLEET_MAX_WORKERS
FLEET_AUTH_BASE_URL
FLEET_AUTH_ENDPOINT
FLEET_AUTH_CLIENT_ID
FLEET_AUTH_CLIENT_SECRET
FLEET_VEHICLE_REGISTRY_BASE_URL
FLEET_VEHICLE_REGISTRY_ENDPOINT
FLEET_PREDICTION_SERVICE_BASE_URL
FLEET_PREDICTION_SERVICE_URL_TEMPLATE
FLEET_DECRYPTION_SERVICE_BASE_URL
FLEET_DECRYPTION_SERVICE_ENDPOINT
FLEET_DECRYPTION_SERVICE_CLIENT_ID
FLEET_DECRYPTION_SERVICE_CLIENT_SECRET
FLEET_DB_HOST
FLEET_DB_PORT
FLEET_DB_USER
FLEET_DB_PASSWORD
FLEET_DB_NAME
```

Do not upload [config.json](config.json) to Railway. Put secrets into Railway environment variables instead.

### Railway MySQL service wiring

If you add a MySQL database service in Railway, map these values into your app service variables:

```text
FLEET_DB_HOST=${{MySQL.MYSQLHOST}}
FLEET_DB_PORT=${{MySQL.MYSQLPORT}}
FLEET_DB_USER=${{MySQL.MYSQLUSER}}
FLEET_DB_PASSWORD=${{MySQL.MYSQLPASSWORD}}
FLEET_DB_NAME=${{MySQL.MYSQLDATABASE}}
```

If your Railway project uses a different database service name than `MySQL`, use that service name in the variable references.

## Azure App Service notes

For code deployment:

1. Create a Linux Web App for Python.
2. Set the application settings from [.env.example](.env.example).
3. Set the startup command to `python fleet_analysis.py`.
4. Set the health check path to `/health`.

For container deployment:

1. Build and push the image to Azure Container Registry or another registry.
2. Configure the Web App to use that image.
3. Set `WEBSITES_PORT=8000` if Azure does not detect the port automatically.
4. Add the same application settings as environment variables.

## Local smoke test

```powershell
python -m pip install -r requirements.deploy.txt
python fleet_analysis.py
```

If you are using hosted build automation, prefer `requirements.deploy.txt` because the legacy `requirements.txt` in this repo uses a non-standard encoding.

Open `http://localhost:8000` and verify `http://localhost:8000/health` returns a healthy response.