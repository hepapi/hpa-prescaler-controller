# hpa-prescaler-controller

Kubernetes Controller for prescaling HPA replicas of ArgoCD Apps

## Kubernetes Deployment

### Create ArgoCD Account and API Token

**Login to ArgoCD Server with CLI**

```bash
# define without https:// or http://
export ARGOCD_ENDPOINT="a4c345906685b46...........eu-west-1.elb.amazonaws.com"

argocd login $ARGOCD_ENDPOINT
```

**Create `hpaprescaler` local account**

```bash
kubectl patch configmap argocd-cm -n argocd --type merge -p '{"data":{"accounts.hpaprescaler":"apiKey","accounts.hpaprescaler.enabled":"true"}}'
```

**Create `hpaprescalerrole` role and permissions**

```bash
kubectl edit -n argocd cm argocd-rbac-cm
```

Add the following lines to `policy.csv`:

```yaml
policy.csv: |
  p, role:hpaprescalerrole, applications, *, */*, allow
  p, role:hpaprescalerrole, projects, *, *, allow
  g, hpaprescaler, role:hpaprescalerrole
```

**Restart argocd-server deployment**

```bash
kubectl -n argocd rollout restart deployment/argocd-server
```

**Generate `hpaprescaler` Api Token**

```bash
argocd account list

argocd account generate-token --account hpaprescaler
```


### Create CRDs

```
kubectl apply -f https://raw.githubusercontent.com/hepapi/hpa-prescaler-controller/main/deploy/crds/HpaPrescaler.yaml
kubectl apply -f https://raw.githubusercontent.com/hepapi/hpa-prescaler-controller/main/deploy/crds/HpaPrescalerProfile.yaml
```


### Monitoring & Prometheus Alarms Setup

- Do this step if you want to enable Prometheus Rules in the `values.yaml` to generate Alarms .
- We'll use `kube-state-metrics` to export the CRDs object info as Prometheus Metrics.
- We need to customize the `kube-state-metrics` metrics installation with new helm values.


Assuming you have `kube-prometheus-stack` deployed on the `monitoring` namespace: 


- Get the current values of your Helm release: 
  ```bash
  helm -n monitoring get values kube-prometheus-stack -o yaml > current-stack-values.yaml
  ```
- Find out the current chart version used and make a note of it: 
  ```bash
  helm -n monitoring list -o yaml
  ```
- Add/Merge the new [kube-state-metrics values](docs/kube-prometheus-stack.values.patch.yaml) to `current-stack-values.yaml` file: 
  ```yaml
  kube-state-metrics:
    prometheus:
      # deploy a ServiceMonitor so the metrics are collected by Prometheus
      monitor:
        enabled: true

    rbac:
      create: true
      extraRules:
      - apiGroups: ["hepapi.com"]
        resources: ["hpaprescalers", "hpaprescalerprofiles", "hpaprescalers/status", "hpaprescalerprofiles/status"]
        verbs: ["list", "watch"]

    customResourceState:
      enabled: true
      config: 
        kind: CustomResourceStateMetrics
        spec:
          resources:
          - groupVersionKind:
              group: hepapi.com
              version: v1
              kind: HpaPrescaler
            labelsFromPath:
              name: ["metadata", "name"]
              namespace: ["metadata", "namespace"]
            metrics:
            - name: hpaprescaler_info
              help: "Exposes details about HpaPrescaler objects"
              each:
                type: Info
                info:
                  labelsFromPath:
                    argoAppName: ["spec", "argocdAppName"]
                    targetProfileName: ["spec", "targetProfileName"]
                    targetTime:  ["spec", "timeStart"]
                    state: ["status", "state"]
                    message: ["status", "message"]
                    processedAt: ["status", "processedAt"]
  ```
- Helm upgrade with new values yaml, using the same version: 
  ```bash
  helm upgrade --install \
    kube-prometheus-stack prometheus-community/kube-prometheus-stack \
    --version "51.2.0" \
    -f current-stack-values.yaml \
    --namespace monitoring --atomic
  ```


### Helm Deployment

**update Values.yaml**

```bash
vim deploy/helm-chart/values.yaml
```

**Helm Chart Installation**

```bash
helm upgrade --install hpaprescaler deploy/helm-chart
```











## Uninstall

**[optional] Remove objects**

```bash
kubectl get hpaprescaler -o json | jq '.items[] | del(.metadata.finalizers)' | kubectl replace -f -
kubectl delete hpaprescaler --all
```


```bash
kubectl get hpaprescalerprofiles -o json | jq '.items[] | del(.metadata.finalizers)' | kubectl replace -f -
kubectl delete hpaprescalerprofiles --all
```

**Helm Uninstall**

```bash
helm uninstall hpaprescaler deploy/helm-chart

kubectl delete crd hpaprescalers.hepapi.com
kubectl delete crd hpaprescalerprofiles.hepapi.com
```