#!/bin/sh

CLUSTER_NAME=example
NAMESPACE=k8s-scheduler-tester
CONTEXT=kind-$CLUSTER_NAME

command kind &>/dev/null || { echo "Kind not installed. See https://kind.sigs.k8s.io/ for more information."; exit 1; }

if ! kind get clusters | grep -q -E "^${CLUSTER_NAME}$"; then
  echo "[!] Cluster not running. Creating now ..."
  kind create cluster --config ./3-node-cluster.yaml --name $CLUSTER_NAME
else
  echo "[=] Cluster $CLUSTER_NAME is running"
fi

echo "[=] Waiting for API server to respond."
while :; do
  kubectl --context $CONTEXT version &>/dev/null && break
  sleep 0.5
done
echo "[*] Cluster ready."

echo "[*] Deploying tester to cluster."
kubectl --context $CONTEXT apply -f ./k8s-scheduler-tester.yaml
