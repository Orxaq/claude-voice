#!/bin/bash
# Deploy Claude Voice to Google Cloud Run
# Ensures the service runs even when the laptop is off
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Configuration
GCP_PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${GCP_REGION:-us-central1}"
SERVICE_NAME="claude-voice"
IMAGE="gcr.io/${GCP_PROJECT}/${SERVICE_NAME}"

if [ -z "$GCP_PROJECT" ]; then
    echo "ERROR: No GCP project set."
    echo "Run: gcloud config set project YOUR_PROJECT_ID"
    exit 1
fi

# Check for API key
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    if [ -f "$PROJECT_DIR/.env" ]; then
        set -a
        source "$PROJECT_DIR/.env"
        set +a
    fi
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ERROR: ANTHROPIC_API_KEY not set."
    exit 1
fi

echo "Deploying Claude Voice to Cloud Run..."
echo "  Project: $GCP_PROJECT"
echo "  Region:  $REGION"
echo ""

# Build and push
cd "$PROJECT_DIR"
gcloud builds submit --tag "$IMAGE" --quiet

# Deploy
gcloud run deploy "$SERVICE_NAME" \
    --image "$IMAGE" \
    --region "$REGION" \
    --platform managed \
    --allow-unauthenticated \
    --set-env-vars "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" \
    --memory 256Mi \
    --cpu 1 \
    --min-instances 0 \
    --max-instances 2 \
    --timeout 300 \
    --concurrency 10 \
    --quiet

# Get URL
URL=$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --format 'value(status.url)')
echo ""
echo "Deployed! Access Claude Voice at:"
echo "  $URL"
echo ""
echo "This URL works from anywhere — phone, hotspot, even when your laptop is off."
echo "Bookmark it on your phone for road trips."
