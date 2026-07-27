# hpa-prescaler-controller local commands
# Requires: just, uv, and a .env file (see .env.example)
# kind recipes target macOS (Darwin) with Docker Desktop / OrbStack / Colima

set shell := ["bash", "-euo", "pipefail", "-c"]

python := ".venv/bin/python"
kopf := ".venv/bin/kopf"

cluster_name := "hpa-prescaler"
kind_config := "dev/kind/cluster.yaml"
charts_dir := "dev/charts"
crds_dir := "deploy/crds"
prescaler_dir := "dev/prescaler"
kube_context := "kind-" + cluster_name
argocd_namespace := "argocd"
argocd_apps_dir := "dev/argocd"
# Official stable install; pin by replacing stable with e.g. v3.4.2 if needed
argocd_install_url := "https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml"
# Matches extraPortMappings in dev/kind/cluster.yaml
argocd_http_nodeport := "30080"
argocd_https_nodeport := "30443"
argocd_local_http := "http://localhost:18080"

# List available recipes
default:
    @just --list

# Install / sync Python deps into .venv
sync:
    uv sync

# Run unit tests
test:
    {{ python }} -m unittest discover -s tests -v

# Run the controller locally (loads .env)
run: _require-env
    #!/usr/bin/env bash
    set -euo pipefail
    set -a && source .env && set +a
    {{ kopf }} run src/hpa_prescaler.py \
        --liveness=http://0.0.0.0:8080/healthz \
        --all-namespaces \
        --log-format=plain

# Same as run, with kopf --debug
run-debug: _require-env
    #!/usr/bin/env bash
    set -euo pipefail
    set -a && source .env && set +a
    {{ kopf }} run src/hpa_prescaler.py \
        --liveness=http://0.0.0.0:8080/healthz \
        --all-namespaces \
        --log-format=plain \
        --debug

# Create .env from the example if missing (no Argo CD token)
env:
    @if [[ -f .env ]]; then \
        echo ".env already exists — use: just env-setup  to refresh for local kind/Argo CD"; \
    else \
        cp .env.example .env; \
        echo "Created .env from .env.example — prefer: just env-setup"; \
    fi

# Write .env for running the controller against local kind + Argo CD
env-setup: _require-macos _require-kind _require-kind-cluster
    #!/usr/bin/env bash
    set -euo pipefail
    ctx="{{ kube_context }}"
    ns="{{ argocd_namespace }}"
    endpoint="{{ argocd_local_http }}"

    if ! kubectl --context "$ctx" get ns "$ns" >/dev/null 2>&1; then
        echo "Argo CD not installed — run: just argocd-install"
        exit 1
    fi
    if ! curl -sf "$endpoint/api/version" >/dev/null; then
        echo "Argo CD not reachable at $endpoint"
        exit 1
    fi

    password="$(just argocd-password)"
    token="$(curl -sf -X POST "$endpoint/api/v1/session" \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"admin\",\"password\":\"${password}\"}" \
        | {{ python }} -c 'import sys,json; print(json.load(sys.stdin)["token"])')"

    kubectl config use-context "$ctx" >/dev/null

    cp .env.example .env
    # macOS sed; writes kind endpoint + session token into .env
    sed -i '' \
        -e "s|^export ARGOCD_ENDPOINT=.*|export ARGOCD_ENDPOINT=\"${endpoint}\"|" \
        -e "s|^export ARGOCD_TOKEN=.*|export ARGOCD_TOKEN=\"${token}\"|" \
        .env

    echo "Wrote .env for local kind/Argo CD"
    echo "  ARGOCD_ENDPOINT=${endpoint}"
    echo "  kubectl context: ${ctx}"
    echo "Run: just run"

# --- kind (macOS) ---

# Create local kind cluster (idempotent if cluster already exists)
kind-create: _require-macos _require-docker _require-kind
    #!/usr/bin/env bash
    set -euo pipefail
    just _prepare-demo-charts-git
    if kind get clusters 2>/dev/null | grep -qx "{{ cluster_name }}"; then
        echo "kind cluster {{ cluster_name }} already exists"
        kubectl cluster-info --context "kind-{{ cluster_name }}"
        if ! docker exec "{{ cluster_name }}-control-plane" test -d /demo-charts; then
            echo
            echo "WARNING: cluster is missing /demo-charts mount (needed for local Argo apps)."
            echo "Recreate with: just kind-delete && just kind-create"
        fi
    else
        charts_host_path="$(cd '{{ charts_dir }}' && pwd)"
        tmp_config="$(mktemp)"
        sed "s|__CHARTS_HOST_PATH__|${charts_host_path}|g" '{{ kind_config }}' > "$tmp_config"
        echo "Creating kind cluster {{ cluster_name }} (charts mount: ${charts_host_path})..."
        kind create cluster --name "{{ cluster_name }}" --config "$tmp_config"
        rm -f "$tmp_config"
        kubectl cluster-info --context "kind-{{ cluster_name }}"
        kubectl get nodes -o wide
        kubectl wait --for=condition=Ready node --all --context "kind-{{ cluster_name }}" --timeout=120s
    fi
    just crds-install

# Install HpaPrescaler CRDs into the kind cluster
crds-install: _require-macos _require-kind _require-kind-cluster
    #!/usr/bin/env bash
    set -euo pipefail
    ctx="{{ kube_context }}"
    echo "Applying CRDs from {{ crds_dir }}..."
    kubectl --context "$ctx" apply -f "{{ crds_dir }}/HpaPrescaler.yaml"
    kubectl --context "$ctx" apply -f "{{ crds_dir }}/HpaPrescalerProfile.yaml"
    kubectl --context "$ctx" apply -f "{{ crds_dir }}/HpaPrescalerCronjob.yaml"
    kubectl --context "$ctx" wait --for=condition=Established \
        crd/hpaprescalers.hepapi.com \
        crd/hpaprescalerprofiles.hepapi.com \
        crd/hpaprescalercronjobs.hepapi.com \
        --timeout=60s
    echo "CRDs installed:"
    kubectl --context "$ctx" get crd | grep hepapi.com || true

# Apply demo HpaPrescalerProfile objects (namespace: default)
demo-profiles: _require-macos _require-kind _require-kind-cluster
    #!/usr/bin/env bash
    set -euo pipefail
    ctx="{{ kube_context }}"
    if ! kubectl --context "$ctx" get crd hpaprescalerprofiles.hepapi.com >/dev/null 2>&1; then
        echo "CRDs missing — run: just crds-install"
        exit 1
    fi
    kubectl --context "$ctx" apply -f "{{ prescaler_dir }}/profiles.yaml"
    kubectl --context "$ctx" get hpaprescalerprofiles -n default

# Create in-grace-window demo HpaPrescaler objects (targets just argocd-apps)
demo-prescalers: _require-macos _require-kind _require-kind-cluster
    #!/usr/bin/env bash
    set -euo pipefail
    ctx="{{ kube_context }}"
    if ! kubectl --context "$ctx" get crd hpaprescalers.hepapi.com >/dev/null 2>&1; then
        echo "CRDs missing — run: just crds-install"
        exit 1
    fi
    if ! kubectl --context "$ctx" get hpaprescalerprofile demo-scale-up -n default >/dev/null 2>&1; then
        echo "Profiles missing — run: just demo-profiles"
        exit 1
    fi

    # Match env-setup default; override via .env if present
    algo="accept_window_is_after_target_time"
    grace_mins=2
    if [[ -f .env ]]; then
        # shellcheck disable=SC1091
        set -a && source .env && set +a
        algo="${GRACE_WINDOW_ALGORITHM_NAME:-$algo}"
        grace_mins="${GRACE_TIME_DELTA_MINS:-$grace_mins}"
    fi

    # Place timeStart inside the active grace window for the configured algorithm
    if [[ "$algo" == *"before"* ]]; then
        # before-target: now < timeStart <= now+grace
        offset_secs=30
        time_start="$({{ python }} -c "from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(seconds=${offset_secs})).strftime('%Y-%m-%dT%H:%M:%SZ'))")"
        echo "Algorithm=${algo} grace=${grace_mins}m → timeStart ~${offset_secs}s in the future: ${time_start}"
    else
        # after-target (default): timeStart <= now < timeStart+grace
        offset_secs=30
        time_start="$({{ python }} -c "from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)-timedelta(seconds=${offset_secs})).strftime('%Y-%m-%dT%H:%M:%SZ'))")"
        echo "Algorithm=${algo} grace=${grace_mins}m → timeStart ~${offset_secs}s in the past: ${time_start}"
    fi

    # Refresh demo prescalers so re-runs get a fresh grace window.
    # Clear kopf finalizers first — delete hangs if the controller is not running.
    for name in demo-prescaler-app demo-prescaler-appset-a demo-prescaler-appset-b; do
        if kubectl --context "$ctx" -n default get hpaprescaler "$name" >/dev/null 2>&1; then
            kubectl --context "$ctx" -n default patch hpaprescaler "$name" \
                --type merge -p '{"metadata":{"finalizers":[]}}' >/dev/null
            kubectl --context "$ctx" -n default delete hpaprescaler "$name" --wait=false >/dev/null
        fi
    done
    # Wait until gone (finalizers already cleared)
    for name in demo-prescaler-app demo-prescaler-appset-a demo-prescaler-appset-b; do
        kubectl --context "$ctx" -n default wait --for=delete "hpaprescaler/$name" --timeout=30s 2>/dev/null || true
    done

    tmp_manifest="$(mktemp)"
    sed "s|__TIME_START__|${time_start}|g" "{{ prescaler_dir }}/prescalers.yaml.tmpl" > "$tmp_manifest"
    kubectl --context "$ctx" apply -f "$tmp_manifest"
    rm -f "$tmp_manifest"

    kubectl --context "$ctx" get hpaprescalers -n default

# Apply demo CRs and change Argo apps autoscaling 1/2 → 2/3 (visible before/after)
demo-apply: _require-macos _require-kind _require-kind-cluster _require-env
    #!/usr/bin/env bash
    set -euo pipefail
    set -a && source .env && set +a

    echo "=== Reset Argo apps to baseline min=1 max=2 ==="
    {{ python }} "{{ prescaler_dir }}/demo_hpa_cli.py" set 1 2
    echo
    echo "=== BEFORE (expect 1/2) ==="
    {{ python }} "{{ prescaler_dir }}/demo_hpa_cli.py" show
    echo

    just demo-profiles
    echo
    just demo-prescalers
    echo

    echo "=== Apply profile demo-scale-up (min=2 max=3) via same updater the controller uses ==="
    {{ python }} "{{ prescaler_dir }}/demo_hpa_cli.py" set 2 3
    echo
    echo "=== AFTER (expect 2/3) ==="
    {{ python }} "{{ prescaler_dir }}/demo_hpa_cli.py" show
    echo
    echo "Done. demo-scale-up profile = 2/3; Argo apps updated to match."
    echo "Re-run anytime: just demo-apply   |  cleanup: just demo-destroy"

# Delete demo profiles/prescalers and restore Argo apps to min=1 max=2
demo-destroy: _require-macos _require-kind _require-kind-cluster
    #!/usr/bin/env bash
    set -euo pipefail
    ctx="{{ kube_context }}"

    echo "Deleting demo HpaPrescaler objects..."
    for name in demo-prescaler-app demo-prescaler-appset-a demo-prescaler-appset-b; do
        if kubectl --context "$ctx" -n default get hpaprescaler "$name" >/dev/null 2>&1; then
            kubectl --context "$ctx" -n default patch hpaprescaler "$name" \
                --type merge -p '{"metadata":{"finalizers":[]}}' >/dev/null
            kubectl --context "$ctx" -n default delete hpaprescaler "$name" --wait=false --ignore-not-found
        fi
    done
    for name in demo-prescaler-app demo-prescaler-appset-a demo-prescaler-appset-b; do
        kubectl --context "$ctx" -n default wait --for=delete "hpaprescaler/$name" --timeout=30s 2>/dev/null || true
    done

    echo "Deleting demo HpaPrescalerProfile objects..."
    kubectl --context "$ctx" -n default delete hpaprescalerprofile \
        demo-scale-up demo-scale-down \
        --ignore-not-found

    if [[ -f .env ]]; then
        set -a && source .env && set +a
        echo "Restoring Argo apps to baseline min=1 max=2..."
        {{ python }} "{{ prescaler_dir }}/demo_hpa_cli.py" set 1 2
        echo
        {{ python }} "{{ prescaler_dir }}/demo_hpa_cli.py" show
    else
        echo "No .env — skipped Argo app restore (run: just env-setup)"
    fi
    echo "Demo resources destroyed."

# Show kind cluster status
kind-status: _require-macos _require-kind
    #!/usr/bin/env bash
    set -euo pipefail
    echo "kind clusters:"
    kind get clusters || true
    if kind get clusters 2>/dev/null | grep -qx "{{ cluster_name }}"; then
        echo
        kubectl get nodes -o wide --context "kind-{{ cluster_name }}"
    else
        echo "Cluster {{ cluster_name }} not found — run: just kind-create"
    fi

# Delete local kind cluster
kind-delete: _require-macos _require-kind
    #!/usr/bin/env bash
    set -euo pipefail
    if ! kind get clusters 2>/dev/null | grep -qx "{{ cluster_name }}"; then
        echo "kind cluster {{ cluster_name }} does not exist"
        exit 0
    fi
    kind delete cluster --name "{{ cluster_name }}"
    echo "Deleted kind cluster {{ cluster_name }}"

# --- Argo CD (kind / macOS) ---

# Install Argo CD into the kind cluster (idempotent apply)
argocd-install: _require-macos _require-kind _require-kind-cluster
    #!/usr/bin/env bash
    set -euo pipefail
    ctx='{{ kube_context }}'
    ns='{{ argocd_namespace }}'

    kubectl --context "$ctx" get ns "$ns" >/dev/null 2>&1 || \
        kubectl --context "$ctx" create namespace "$ns"

    echo "Applying Argo CD manifests from {{ argocd_install_url }}..."
    kubectl --context "$ctx" apply -n "$ns" --server-side --force-conflicts \
        -f '{{ argocd_install_url }}'

    # Allow plain HTTP for local kind access (matches ARGOCD_SSL_VERIFY=false)
    kubectl --context "$ctx" -n "$ns" patch configmap argocd-cmd-params-cm --type merge \
        -p '{"data":{"server.insecure":"true"}}'

    # Expose via NodePorts mapped by kind (host 18080/18443)
    kubectl --context "$ctx" -n "$ns" patch svc argocd-server --type merge -p "{
      \"spec\": {
        \"type\": \"NodePort\",
        \"ports\": [
          {\"name\": \"http\", \"port\": 80, \"targetPort\": 8080, \"nodePort\": {{ argocd_http_nodeport }}},
          {\"name\": \"https\", \"port\": 443, \"targetPort\": 8080, \"nodePort\": {{ argocd_https_nodeport }}}
        ]
      }
    }"

    just _mount-demo-charts-in-repo-server

    kubectl --context "$ctx" -n "$ns" rollout restart deployment argocd-server
    echo "Waiting for Argo CD server..."
    kubectl --context "$ctx" -n "$ns" rollout status deployment/argocd-server --timeout=180s
    kubectl --context "$ctx" -n "$ns" wait --for=condition=available deployment --all --timeout=180s

    echo
    echo "Argo CD installed."
    echo "  UI / API: {{ argocd_local_http }}"
    echo "  user:     admin"
    echo "  password: $(just argocd-password)"
    echo
    echo "Login example:"
    echo "  argocd login localhost:18080 --username admin --password \"\$(just argocd-password)\" --insecure"
    echo "Next: just argocd-apps"

# Apply example Application + ApplicationSet (local demo-app chart)
argocd-apps: _require-macos _require-kind _require-kind-cluster
    #!/usr/bin/env bash
    set -euo pipefail
    ctx='{{ kube_context }}'
    ns='{{ argocd_namespace }}'

    if ! kubectl --context "$ctx" get ns "$ns" >/dev/null 2>&1; then
        echo "Argo CD not installed — run: just argocd-install"
        exit 1
    fi
    if ! docker exec "{{ cluster_name }}-control-plane" test -d /demo-charts; then
        echo "kind node is missing /demo-charts mount."
        echo "Recreate cluster: just kind-delete && just kind-create && just argocd-install && just argocd-apps"
        exit 1
    fi

    just _prepare-demo-charts-git
    just _mount-demo-charts-in-repo-server

    echo "Registering local chart repo + applying example apps..."
    kubectl --context "$ctx" apply -f '{{ argocd_apps_dir }}/repository-demo-charts.yaml'
    kubectl --context "$ctx" apply -f '{{ argocd_apps_dir }}/application-demo.yaml'
    kubectl --context "$ctx" apply -f '{{ argocd_apps_dir }}/applicationset-demo.yaml'

    echo "Waiting for Applications to become Healthy/Synced..."
    for app in demo-app demo-appset-a demo-appset-b; do
        kubectl --context "$ctx" -n "$ns" wait --for=jsonpath='{.status.sync.status}'=Synced \
            "application/$app" --timeout=180s
        kubectl --context "$ctx" -n "$ns" wait --for=jsonpath='{.status.health.status}'=Healthy \
            "application/$app" --timeout=180s
    done

    echo
    kubectl --context "$ctx" -n "$ns" get applications
    echo
    kubectl --context "$ctx" -n demo get deploy,hpa,svc
    echo
    echo "Example apps applied (namespace: demo)."

# Print initial Argo CD admin password
argocd-password: _require-macos _require-kind _require-kind-cluster
    @kubectl --context '{{ kube_context }}' -n '{{ argocd_namespace }}' \
        get secret argocd-initial-admin-secret \
        -o jsonpath='{.data.password}' | base64 -d
    @echo

# Show Argo CD pods / service
argocd-status: _require-macos _require-kind _require-kind-cluster
    #!/usr/bin/env bash
    set -euo pipefail
    ctx='{{ kube_context }}'
    ns='{{ argocd_namespace }}'
    if ! kubectl --context "$ctx" get ns "$ns" >/dev/null 2>&1; then
        echo "Namespace '$ns' not found — run: just argocd-install"
        exit 1
    fi
    kubectl --context "$ctx" -n "$ns" get pods,svc
    echo
    echo "Local endpoint: {{ argocd_local_http }}"
    if kubectl --context "$ctx" -n "$ns" get applications >/dev/null 2>&1; then
        echo
        kubectl --context "$ctx" -n "$ns" get applications
    fi

# Ensure demo charts dir is a git repo (required for Argo CD file://)
_prepare-demo-charts-git:
    #!/usr/bin/env bash
    set -euo pipefail
    charts_dir='{{ charts_dir }}'
    if [[ ! -d "$charts_dir/demo-app" ]]; then
        echo "Missing $charts_dir/demo-app"
        exit 1
    fi
    if [[ ! -d "$charts_dir/.git" ]]; then
        git -C "$charts_dir" init -q
        git -C "$charts_dir" config user.email "dev@localhost"
        git -C "$charts_dir" config user.name "local-dev"
    fi
    git -C "$charts_dir" add -A
    if git -C "$charts_dir" diff --cached --quiet; then
        echo "Demo charts git repo is up to date"
    else
        git -C "$charts_dir" commit -q -m "demo charts"
        echo "Committed local demo charts for Argo CD file:// repo"
    fi

# Mount kind node /demo-charts into argocd-repo-server
_mount-demo-charts-in-repo-server: _require-kind-cluster
    #!/usr/bin/env bash
    set -euo pipefail
    ctx="{{ kube_context }}"
    ns="{{ argocd_namespace }}"
    if ! docker exec "{{ cluster_name }}-control-plane" test -d /demo-charts; then
        echo "kind node is missing /demo-charts mount."
        echo "Recreate cluster: just kind-delete && just kind-create"
        exit 1
    fi
    kubectl --context "$ctx" -n "$ns" patch deployment argocd-repo-server \
        --type strategic \
        --patch-file "{{ argocd_apps_dir }}/repo-server-demo-charts-patch.yaml"
    kubectl --context "$ctx" -n "$ns" rollout status deployment/argocd-repo-server --timeout=180s

_require-env:
    @if [[ ! -f .env ]]; then \
        echo "Missing .env — run: just env-setup"; \
        exit 1; \
    fi

_require-macos:
    @if [[ "$(uname -s)" != "Darwin" ]]; then \
        echo "kind recipes currently target macOS only (uname=$(uname -s))"; \
        exit 1; \
    fi

_require-docker:
    @if ! command -v docker >/dev/null 2>&1; then \
        echo "docker not found — install Docker Desktop, OrbStack, or Colima"; \
        exit 1; \
    fi
    @if ! docker info >/dev/null 2>&1; then \
        echo "docker is installed but not reachable — start Docker Desktop / OrbStack / Colima"; \
        exit 1; \
    fi

_require-kind:
    @if ! command -v kind >/dev/null 2>&1; then \
        echo "kind not found — install with: brew install kind"; \
        exit 1; \
    fi
    @if ! command -v kubectl >/dev/null 2>&1; then \
        echo "kubectl not found — install with: brew install kubectl"; \
        exit 1; \
    fi

_require-kind-cluster:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! kind get clusters 2>/dev/null | grep -qx "{{ cluster_name }}"; then
        echo "kind cluster {{ cluster_name }} not found — run: just kind-create"
        exit 1
    fi
