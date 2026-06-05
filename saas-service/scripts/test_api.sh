#!/usr/bin/env bash
set -euo pipefail

BASE_URL="http://localhost:8000"
TOKEN=""

echo "=== API Test Script ==="
echo "Base URL: $BASE_URL"
echo ""

# Function to print status
print_result() {
    local name="$1"
    local status="$2"
    local response="$3"
    
    if [[ "$status" -ge 200 && "$status" -lt 300 ]]; then
        echo "✓ $name: HTTP $status"
    else
        echo "✗ $name: HTTP $status"
    fi
    
    # Print first 200 chars of response
    if [[ -n "$response" ]]; then
        echo "  Response: $(echo "$response" | head -c 200)..."
    fi
    echo ""
}

# 1. Health check
echo "1. Health check..."
response=$(curl -s -w "\n%{http_code}" "$BASE_URL/health" || echo -e "\n000")
status=$(echo "$response" | tail -n1)
body=$(echo "$response" | head -n -1)
print_result "GET /health" "$status" "$body"

# 2. Register test user
echo "2. Register test user..."
register_payload='{"email":"test@test.com","password":"test123","full_name":"Test","tenant_name":"TestCo","tenant_slug":"testco"}'
response=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/api/v1/auth/register" \
    -H "Content-Type: application/json" \
    -d "$register_payload" || echo -e "\n000")
status=$(echo "$response" | tail -n1)
body=$(echo "$response" | head -n -1)
print_result "POST /api/v1/auth/register" "$status" "$body"

# Extract token from registration response
if [[ "$status" -eq 200 || "$status" -eq 201 ]]; then
    TOKEN=$(echo "$body" | python3 -c "import sys, json; print(json.load(sys.stdin).get('access_token', ''))" 2>/dev/null || echo "")
    if [[ -n "$TOKEN" ]]; then
        echo "  Extracted access_token: ${TOKEN:0:20}..."
    else
        echo "  Warning: Could not extract access_token"
    fi
fi

# 3. Login to get token (fallback if registration didn't return token)
if [[ -z "$TOKEN" ]]; then
    echo "3. Login to get token..."
    login_payload='{"username":"test@test.com","password":"test123"}'
    response=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/api/v1/auth/login" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -d "$login_payload" || echo -e "\n000")
    status=$(echo "$response" | tail -n1)
    body=$(echo "$response" | head -n -1)
    print_result "POST /api/v1/auth/login" "$status" "$body"
    
    if [[ "$status" -eq 200 ]]; then
        TOKEN=$(echo "$body" | python3 -c "import sys, json; print(json.load(sys.stdin).get('access_token', ''))" 2>/dev/null || echo "")
        if [[ -n "$TOKEN" ]]; then
            echo "  Extracted access_token: ${TOKEN:0:20}..."
        fi
    fi
fi

# 4. List pipelines
if [[ -n "$TOKEN" ]]; then
    echo "4. List pipelines..."
    response=$(curl -s -w "\n%{http_code}" "$BASE_URL/api/v1/pipelines/" \
        -H "Authorization: Bearer $TOKEN" || echo -e "\n000")
    status=$(echo "$response" | tail -n1)
    body=$(echo "$response" | head -n -1)
    print_result "GET /api/v1/pipelines/" "$status" "$body"
else
    echo "4. Skipping pipelines list (no token)"
    echo ""
fi

# 5. List tenants
if [[ -n "$TOKEN" ]]; then
    echo "5. List tenants..."
    response=$(curl -s -w "\n%{http_code}" "$BASE_URL/api/v1/tenants/" \
        -H "Authorization: Bearer $TOKEN" || echo -e "\n000")
    status=$(echo "$response" | tail -n1)
    body=$(echo "$response" | head -n -1)
    print_result "GET /api/v1/tenants/" "$status" "$body"
else
    echo "5. Skipping tenants list (no token)"
    echo ""
fi

echo "=== API Tests Complete ==="