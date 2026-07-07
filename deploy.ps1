# ============================================================
#  PM Automation — Azure Deployment Script
#  Run this on your Windows PC inside the pm_project folder.
#  Prerequisites: Azure CLI installed (https://aka.ms/installazurecliwindows)
# ============================================================

# ── FILL THESE IN BEFORE RUNNING ────────────────────────────
$RESOURCE_GROUP    = "rg-dev-IntegrationTeamAI"      # existing Niagara resource group
$LOCATION          = "westus2"                        # West US 2 — matches existing resources
$APP_NAME          = "pm-automation-niagara"          # must be globally unique
$ACR_NAME          = "acrpmautomation"                # new Container Registry for this project
$PLAN_NAME         = "asp-pm-automation"              # new App Service Plan for this project
$WATSONX_API_KEY   = "PASTE-YOUR-WATSONX-API-KEY-HERE"
$WATSONX_PROJECT   = "PASTE-YOUR-WATSONX-PROJECT-ID-HERE"
$WATSONX_URL       = "https://us-south.ml.cloud.ibm.com"
$STORAGE_CONN_STR  = "PASTE-YOUR-STORAGE-CONNECTION-STRING-HERE"
$SECRET_KEY        = "CHANGE-ME-32-CHARS-RANDOM-STRING-HERE"
$DEV_API_KEY       = "CHANGE-ME-SECRET-KEY"
# ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "=== PM Automation — Azure Deployment ===" -ForegroundColor Cyan
Write-Host ""

# Step 1: Login to Azure
Write-Host "[1/8] Logging in to Azure..." -ForegroundColor Yellow
az login
if ($LASTEXITCODE -ne 0) { Write-Host "Login failed. Exiting." -ForegroundColor Red; exit 1 }

# Step 2: Use existing Resource Group (no need to create)
Write-Host "[2/8] Using existing resource group '$RESOURCE_GROUP'..." -ForegroundColor Yellow
az group show --name $RESOURCE_GROUP
if ($LASTEXITCODE -ne 0) { Write-Host "Resource group not found. Check the name." -ForegroundColor Red; exit 1 }

# Step 3: Create new Container Registry for PM project
Write-Host "[3/8] Creating Container Registry '$ACR_NAME'..." -ForegroundColor Yellow
az acr create --resource-group $RESOURCE_GROUP --name $ACR_NAME --sku Basic --admin-enabled true
if ($LASTEXITCODE -ne 0) { Write-Host "Failed to create Container Registry." -ForegroundColor Red; exit 1 }

# Step 4: Build and push Docker image
Write-Host "[4/8] Building and pushing Docker image (this takes 3-5 minutes)..." -ForegroundColor Yellow
az acr build --registry $ACR_NAME --image pm-automation:latest .
if ($LASTEXITCODE -ne 0) { Write-Host "Docker build failed." -ForegroundColor Red; exit 1 }

# Step 5: Create App Service Plan for PM project
Write-Host "[5/8] Creating App Service Plan '$PLAN_NAME' (B2 tier)..." -ForegroundColor Yellow
az appservice plan create `
    --name $PLAN_NAME `
    --resource-group $RESOURCE_GROUP `
    --location $LOCATION `
    --is-linux `
    --sku B2
if ($LASTEXITCODE -ne 0) { Write-Host "Failed to create App Service Plan." -ForegroundColor Red; exit 1 }

# Step 6: Get ACR credentials
$ACR_SERVER   = "$ACR_NAME.azurecr.io"
$ACR_USERNAME = (az acr credential show --name $ACR_NAME --query username -o tsv)
$ACR_PASSWORD = (az acr credential show --name $ACR_NAME --query "passwords[0].value" -o tsv)

# Step 7: Create the Web App
Write-Host "[6/8] Creating Web App '$APP_NAME'..." -ForegroundColor Yellow
az webapp create `
    --resource-group $RESOURCE_GROUP `
    --plan $PLAN_NAME `
    --name $APP_NAME `
    --deployment-container-image-name "$ACR_SERVER/pm-automation:latest" `
    --docker-registry-server-url "https://$ACR_SERVER" `
    --docker-registry-server-user $ACR_USERNAME `
    --docker-registry-server-password $ACR_PASSWORD
if ($LASTEXITCODE -ne 0) { Write-Host "Failed to create Web App." -ForegroundColor Red; exit 1 }

# Step 8: Configure environment variables
Write-Host "[7/8] Setting environment variables..." -ForegroundColor Yellow

az webapp config appsettings set `
    --resource-group $RESOURCE_GROUP `
    --name $APP_NAME `
    --settings `
        APP_ENV=production `
        APP_DEBUG=false `
        APP_SECRET_KEY="$SECRET_KEY" `
        DEV_API_KEY="$DEV_API_KEY" `
        AI_PROVIDER=watsonx `
        WATSONX_API_KEY="$WATSONX_API_KEY" `
        WATSONX_PROJECT_ID="$WATSONX_PROJECT" `
        WATSONX_URL="$WATSONX_URL" `
        WATSONX_MODEL_GENERATION=ibm/granite-3-3-2b-instruct `
        WATSONX_EMBEDDING_MODEL=ibm/slate-30m-english-rtrvr `
        DEFAULT_STORAGE_TARGET=azure `
        AZURE_STORAGE_CONNECTION_STRING="$STORAGE_CONN_STR" `
        AZURE_STORAGE_CONTAINER_NAME=pm-docs `
        LOCAL_STORAGE_PATH=/home/output/pm-docs `
        DATABASE_URL="" `
        RAG_CHUNK_SIZE=500 `
        RAG_CHUNK_OVERLAP=103 `
        RAG_TOP_K=10 `
        RATE_LIMIT_PER_MINUTE=100 `
        WEBSITES_PORT=8000

if ($LASTEXITCODE -ne 0) { Write-Host "Failed to set environment variables." -ForegroundColor Red; exit 1 }

# Enable persistent storage (/home directory survives restarts)
Write-Host "[8/8] Enabling persistent storage..." -ForegroundColor Yellow
az webapp config appsettings set `
    --resource-group $RESOURCE_GROUP `
    --name $APP_NAME `
    --settings WEBSITES_ENABLE_APP_SERVICE_STORAGE=true

Write-Host ""
Write-Host "=== DEPLOYMENT COMPLETE ===" -ForegroundColor Green
Write-Host ""
Write-Host "Your app is live at:" -ForegroundColor Cyan
Write-Host "  https://$APP_NAME.azurewebsites.net" -ForegroundColor White
Write-Host ""
Write-Host "Health check:" -ForegroundColor Cyan
Write-Host "  https://$APP_NAME.azurewebsites.net/health" -ForegroundColor White
Write-Host ""
Write-Host "Dashboard:" -ForegroundColor Cyan
Write-Host "  https://$APP_NAME.azurewebsites.net/frontend/dashboard.html" -ForegroundColor White
Write-Host ""
Write-Host "NEXT STEPS:" -ForegroundColor Yellow
Write-Host "  1. Visit the health URL above — should show: {status: ok}" -ForegroundColor White
Write-Host "  2. Visit the dashboard URL — same UI as localhost:8000" -ForegroundColor White
Write-Host "  3. For Teams integration: use the dashboard URL as your Teams Tab URL" -ForegroundColor White
Write-Host "  4. Share the dashboard URL with your team — they can log in immediately" -ForegroundColor White
Write-Host ""
