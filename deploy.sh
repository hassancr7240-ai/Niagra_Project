#!/bin/bash
# ============================================================
#  PM Automation — Azure Deployment Script
#  Resource Group : rg-dev-IntegrationTeamAI
#  Region         : West US 2
#  Container Reg  : acrregistrymcp
# ============================================================
set -e

# ── Config ────────────────────────────────────────────────────
RESOURCE_GROUP="rg-dev-IntegrationTeamAI"
LOCATION="westus2"
ACR_NAME="acrregistrymcp"
APP_SERVICE_PLAN="asp-registry-mcp"      # reuse existing plan
APP_NAME="pm-automation"
IMAGE_NAME="pm-automation"
IMAGE_TAG="latest"

echo "=== Step 1: Login to Azure Container Registry ==="
az acr login --name $ACR_NAME

echo "=== Step 2: Build Docker image ==="
docker build -t $IMAGE_NAME:$IMAGE_TAG .

echo "=== Step 3: Tag and push image to ACR ==="
ACR_LOGIN_SERVER=$(az acr show --name $ACR_NAME --query loginServer -o tsv)
docker tag $IMAGE_NAME:$IMAGE_TAG $ACR_LOGIN_SERVER/$IMAGE_NAME:$IMAGE_TAG
docker push $ACR_LOGIN_SERVER/$IMAGE_NAME:$IMAGE_TAG

echo "=== Step 4: Create App Service (skip if already exists) ==="
az webapp create \
    --resource-group $RESOURCE_GROUP \
    --plan $APP_SERVICE_PLAN \
    --name $APP_NAME \
    --deployment-container-image-name $ACR_LOGIN_SERVER/$IMAGE_NAME:$IMAGE_TAG \
    --output none 2>/dev/null || echo "App Service already exists — skipping create"

echo "=== Step 5: Configure App Service to pull from ACR ==="
az webapp config container set \
    --name $APP_NAME \
    --resource-group $RESOURCE_GROUP \
    --container-image-name $ACR_LOGIN_SERVER/$IMAGE_NAME:$IMAGE_TAG \
    --container-registry-url https://$ACR_LOGIN_SERVER

echo "=== Step 6: Set environment variables from .env.production ==="
# Read .env.production and set each non-comment, non-empty line as app setting
SETTINGS=""
while IFS= read -r line; do
    # skip blank lines and comments
    [[ -z "$line" || "$line" == \#* ]] && continue
    SETTINGS="$SETTINGS \"$line\""
done < .env.production

az webapp config appsettings set \
    --name $APP_NAME \
    --resource-group $RESOURCE_GROUP \
    --settings $SETTINGS \
    --output none

echo "=== Step 7: Enable always-on and configure startup ==="
az webapp config set \
    --name $APP_NAME \
    --resource-group $RESOURCE_GROUP \
    --always-on true \
    --output none

echo "=== Step 8: Restart App Service ==="
az webapp restart --name $APP_NAME --resource-group $RESOURCE_GROUP

echo ""
echo "=== DEPLOYMENT COMPLETE ==="
echo "App URL : https://$APP_NAME.azurewebsites.net"
echo "Health  : https://$APP_NAME.azurewebsites.net/health"
echo "Logs    : az webapp log tail --name $APP_NAME --resource-group $RESOURCE_GROUP"
