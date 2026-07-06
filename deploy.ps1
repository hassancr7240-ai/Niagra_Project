# ============================================================
#  PM Automation — Azure Deployment Script
#  Run this on your Windows PC inside the pm_project folder.
#  Prerequisites: Azure CLI installed (https://aka.ms/installazurecliwindows)
# ============================================================

# ── FILL THESE IN BEFORE RUNNING ────────────────────────────
$RESOURCE_GROUP    = "pm-automation-rg"          # name for the Azure resource group
$LOCATION          = "eastus"                     # Azure region (ask IT which one your company uses)
$APP_NAME          = "pm-automation-niagara"      # must be globally unique — add your company name
$ACR_NAME          = "pmautomationacr"            # Azure Container Registry name (letters/numbers only)
$PLAN_NAME         = "pm-automation-plan"         # App Service Plan name
$OPENAI_KEY        = "PASTE-YOUR-OPENAI-KEY-HERE"
$OPENAI_BASE_URL   = ""                           # leave blank for regular OpenAI, or paste Azure OpenAI endpoint
$STORAGE_CONN_STR  = ""                           # paste Azure Storage connection string (or leave blank for local)
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

# Step 2: Create Resource Group
Write-Host "[2/8] Creating resource group '$RESOURCE_GROUP' in '$LOCATION'..." -ForegroundColor Yellow
az group create --name $RESOURCE_GROUP --location $LOCATION
if ($LASTEXITCODE -ne 0) { Write-Host "Failed to create resource group." -ForegroundColor Red; exit 1 }

# Step 3: Create Azure Container Registry (stores your Docker image)
Write-Host "[3/8] Creating Container Registry '$ACR_NAME'..." -ForegroundColor Yellow
az acr create --resource-group $RESOURCE_GROUP --name $ACR_NAME --sku Basic --admin-enabled true
if ($LASTEXITCODE -ne 0) { Write-Host "Failed to create Container Registry." -ForegroundColor Red; exit 1 }

# Step 4: Build and push Docker image
Write-Host "[4/8] Building and pushing Docker image (this takes 3-5 minutes)..." -ForegroundColor Yellow
az acr build --registry $ACR_NAME --image pm-automation:latest .
if ($LASTEXITCODE -ne 0) { Write-Host "Docker build failed." -ForegroundColor Red; exit 1 }

# Step 5: Create App Service Plan (B2 = 2 cores, 3.5GB RAM — good for production)
Write-Host "[5/8] Creating App Service Plan (B2 tier)..." -ForegroundColor Yellow
az appservice plan create `
    --name $PLAN_NAME `
    --resource-group $RESOURCE_GROUP `
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

$STORAGE_TARGET = if ($STORAGE_CONN_STR) { "azure" } else { "local" }
$OPENAI_URL     = if ($OPENAI_BASE_URL) { $OPENAI_BASE_URL } else { "" }

az webapp config appsettings set `
    --resource-group $RESOURCE_GROUP `
    --name $APP_NAME `
    --settings `
        APP_ENV=production `
        APP_DEBUG=false `
        APP_SECRET_KEY="$SECRET_KEY" `
        DEV_API_KEY="$DEV_API_KEY" `
        AI_PROVIDER=openai `
        OPENAI_API_KEY="$OPENAI_KEY" `
        OPENAI_BASE_URL="$OPENAI_URL" `
        OPENAI_MODEL_GENERATION=gpt-4o `
        OPENAI_MODEL_CLASSIFICATION=gpt-4o-mini `
        OPENAI_EMBEDDING_MODEL=text-embedding-3-large `
        OPENAI_EMBEDDING_DIMS=3072 `
        DEFAULT_STORAGE_TARGET="$STORAGE_TARGET" `
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
