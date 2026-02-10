#!/bin/bash
set -e

echo "══════════════════════════════════════════════"
echo "  RAG-RBAC PoC — Waiting for Ollama..."
echo "══════════════════════════════════════════════"

# Wait for Ollama to be ready
until curl -s "${OLLAMA_BASE_URL}/api/tags" > /dev/null 2>&1; do
    echo "  Ollama not ready yet, retrying in 3s..."
    sleep 3
done
echo "  ✓ Ollama is up"

# Pull models if not already present
for MODEL in "${LLM_MODEL}" "${EMBED_MODEL}"; do
    echo "  Checking model: ${MODEL}"
    if ! curl -s "${OLLAMA_BASE_URL}/api/tags" | grep -q "\"${MODEL}\""; then
        echo "  Pulling ${MODEL} (this may take a few minutes on first run)..."
        curl -s "${OLLAMA_BASE_URL}/api/pull" -d "{\"name\": \"${MODEL}\"}" | while read -r line; do
            STATUS=$(echo "$line" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('status',''))" 2>/dev/null || echo "")
            if [ -n "$STATUS" ]; then echo "    $STATUS"; fi
        done
        echo "  ✓ ${MODEL} pulled"
    else
        echo "  ✓ ${MODEL} already available"
    fi
done

echo "══════════════════════════════════════════════"
echo "  Starting RAG-RBAC application on :8000"
echo "══════════════════════════════════════════════"

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
