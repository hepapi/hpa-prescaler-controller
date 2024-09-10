## Setup Demo EKS Cluster

```
eksdemo create cluster "prescaler-demo" \
    --instance "m5.large" \
    --nodes 1 \
    --version "1.28" \
    --os "AmazonLinux2"


eksdemo use-context "prescaler-demo"

kubectl config current-context

kubectl get pod -A
kubectl get nodes -o wide
```

## Install ArgoCD

```
eksdemo install argo cd -c "prescaler-demo" --admin-pass "deneme123"
```

#### Get LB Address

```
kubectl get svc -n argocd argocd-server -oyaml | yq '.status.loadBalancer.ingress[0].hostname'
```

## Install Monitoring

- kube-state-metrics
- prometheus
- grafana

```
eksdemo install kube-prometheus stack -c prescaler-demo --grafana-pass "deneme123"
```

## Delete the Cluster

```
eksdemo delete cluster "prescaler-demo"
```


## ECR

```
docker build -t prescaler . \
  && docker tag prescaler:latest 995194808144.dkr.ecr.us-east-1.amazonaws.com/prescaler:latest \
  && docker push 995194808144.dkr.ecr.us-east-1.amazonaws.com/prescaler:latest 

```

